"""适配器层：平台接入与统一消息模型。

M0-08 先落 `Reply`——ChatLLM 循环的返回值，属于"对话一次的结果"
这个统一模型的一部分。`Message` 与 `AdapterBase` 随 M0-11 补齐
（它们要等 WebSocket / QQ 适配器真正接入时才成型）。
"""

from core.adapter.base import Reply

__all__ = ["Reply"]
