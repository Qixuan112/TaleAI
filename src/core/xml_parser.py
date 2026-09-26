"""XML 解析器：把模型的人格输出拆成结构化消息。

M0-04。协议见 prompts/chat.md：模型把「说给用户听的话」包在 <msg> 里。

为什么用正则而不是 ElementTree：
- 模型输出是一串**并列**标签（<msg>甲</msg><msg>乙</msg>），这不是合法 XML
  ——XML 要求单一根节点，ElementTree 会直接抛错。
- 我们要的是「能解析就解析、不能就兜底」，不是严格校验。
- 标签契约简单且扁平，正则最可控。

设计边界（设计文档 §19-1）：
- 只解析 assistant 输出。用户消息原样透传，不经过这里。
- 解析失败（没找到任何完整 <msg>）→ 整段原文当一条消息兜底。
- 解析器本身不写日志（保持纯函数、好测），由调用方拿 is_fallback
  决定是否记日志——对应 Reply.stop_reason 的 "parse_fallback"。
"""

import re
from dataclasses import dataclass

# 非贪婪 + DOTALL：一个回复里可以有多个 <msg>，且内容可跨行。
# 契约是小写标签（见 chat.md），这里不做大小写宽容——模型破坏契约时
# 应当被 is_fallback 暴露出来，而不是被悄悄修正。
_MSG_PATTERN = re.compile(r"<msg>(.*?)</msg>", re.DOTALL)


@dataclass
class ParsedOutput:
    """解析结果。

    messages: <msg> 标签里的正文，按出现顺序（一个回复可能有多段）。
    is_fallback: 是否走了兜底（没找到完整 <msg>，整段原文当一条消息）。
    """

    messages: list[str]
    is_fallback: bool = False


class XmlParser:
    """把 assistant 的人格输出解析成结构化消息。"""

    def parse(self, text: str) -> ParsedOutput:
        """解析一段模型输出。

        找到 N 个完整 <msg>...</msg> → messages 为 N 条正文（去首尾空白，
        空标签丢弃），is_fallback=False。
        一个都没找到 → messages 为 [原文]（原文为空则 []），is_fallback=True。
        """
        found = [m.strip() for m in _MSG_PATTERN.findall(text)]
        messages = [m for m in found if m]  # 丢弃 <msg></msg> 这种空标签

        if messages:
            return ParsedOutput(messages=messages, is_fallback=False)

        # 兜底：没有可用的 <msg>，把整段原文交给用户，别让他看到空白
        fallback = text.strip()
        return ParsedOutput(messages=[fallback] if fallback else [], is_fallback=True)
