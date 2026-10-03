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

**为什么不用正则**（PR #10 修）：原先用 `re.compile(r"<msg>(.*?)</msg>", DOTALL)`
非贪婪匹配，有两个硬伤：
1. **在第一个 `</msg>` 处就截断**。正文里只要出现 `</msg>`（模型原样复述、
   用户教它格式），后面全被吞掉，且 `is_fallback=False` 报告"成功"——静默丢内容。
2. **O(n²)**：有 `<msg>` 但没有匹配的 `</msg>` 时，每个起点都要扫到串尾。
   8000 个未闭合 `<msg>`（约 40KB）实测 7 秒，同步跑在事件循环上会卡死整个服务。
改成一个游标单调右移的单次扫描：既 O(n)，又能在扫描中发现"契约被破坏"
（嵌套/未闭合/多余闭标签）时**整段兜底**，而不是抢救出半截。
"""

from dataclasses import dataclass

_OPEN = "<msg>"
_CLOSE = "</msg>"


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
        一个都没找到、或**契约被破坏** → 整段原文兜底，is_fallback=True。

        契约破坏指（任何一种都整段兜底，不抢救半截）：
        - 有 <msg> 但找不到后面的 </msg>（未闭合）；
        - 一对 <msg>...</msg> 中间又冒出 <msg>（嵌套，契约不允许）；
        - 出现对不上任何 <msg> 的 </msg>（多余闭标签）。
        这么定是因为：截断报"成功"会让用户看到残缺内容且无人告警（原 bug），
        而"兜底"至少把原文完整交回、让调用方按 parse_fallback 记 warning。
        """
        messages: list[str] = []
        pos = 0
        broken = False

        while True:
            open_i = text.find(_OPEN, pos)
            close_i = text.find(_CLOSE, pos)

            if open_i == -1 and close_i == -1:
                break  # 尾巴干净，没有残留标签

            # </msg> 出现在下一个 <msg> 之前 = 对不上开标签的多余闭标签
            if close_i != -1 and (open_i == -1 or close_i < open_i):
                broken = True
                break

            if close_i == -1:
                broken = True  # 有开无闭
                break

            inner = text[open_i + len(_OPEN):close_i]
            if _OPEN in inner:
                broken = True  # 嵌套开标签
                break

            stripped = inner.strip()
            if stripped:  # 空 <msg></msg> 丢弃，但不算破坏契约
                messages.append(stripped)
            pos = close_i + len(_CLOSE)

        if messages and not broken:
            return ParsedOutput(messages=messages, is_fallback=False)

        # 兜底：没有可用的 <msg>、或契约被破坏，把整段原文交给用户，
        # 别让他看到空白、也别让他看到被截断的半句。
        fallback = text.strip()
        return ParsedOutput(messages=[fallback] if fallback else [], is_fallback=True)
