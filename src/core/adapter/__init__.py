"""适配器层：平台接入与统一消息模型（§三 / §18.3 / §22）。

模块划分：
- base.py      统一消息模型（Message / Reply）+ AdapterBase 基类
- registry.py  适配器名册（平台名 → 适配器）
- router.py    Router：M0 只做同平台内循环；跨平台大循环留 M3
- web/         WebAdapter：WebUI 接入（FastAPI，走 WebSocket 协议）

import 单向（§22 第 4 条）：适配器 → Router / Bus，不认识 ChatLLM 内部。
"""

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router, UnknownPlatformError

__all__ = [
    "AdapterBase",
    "Message",
    "Reply",
    "AdapterRegistry",
    "Router",
    "UnknownPlatformError",
]
