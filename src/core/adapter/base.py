"""统一消息模型（设计文档 §18.3）。

M0-08 先落 `Reply`：一次对话的完整结果。它替代原先"chat() 返回一个字符串"
的做法——因为有了 FC 循环之后，一次对话可能产生多条消息、调用若干次工具，
还会因为熔断/超轮次而提前结束，这些信息都需要如实带出来，不能压成一个字符串。

`Message`（平台消息模型）与 `AdapterBase` 随 M0-11 补齐——它们要等
WebSocket / QQ 适配器接入时才真正成型，现在写就是凭空猜。
"""

from dataclasses import dataclass, field


@dataclass
class Reply:
    """一次对话的返回（形状见 §18.3）。

    messages 是**要发给用户的话**，可能不止一条：模型可以在调工具前先说
    "帮你查查"、拿到结果后再说答案，这些是分开的两条消息而不是一段长文。

    stop_reason 的取值对应循环是怎么结束的：
      ok             —— 模型自己说完了（正常结束）
      max_steps      —— 撞到轮次上限
      loop_cut       —— 同工具同参数连续重复，被熔断
      parse_fallback —— 模型没按 <msg> 契约输出，走了纯文本兜底
    """

    session_id: str = ""
    messages: list[str] = field(default_factory=list)
    tool_calls_made: int = 0
    stop_reason: str = "ok"
