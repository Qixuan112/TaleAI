"""路由（设计文档 §3.7 / §18.1 第 3 步 / §十九-4、5 / §22）。

"运营商式两级路由"里的**路由层**：

- **内循环**（同平台）：消息默认走这条，是快路径。M0 只有它。
- **大循环**（跨平台）：`<session_send to="私:老板" platform="微信">` 这类
  要跨平台时，才走路由层查名册 → 找目标适配器。**M3 才做**。

所以 M0 的 `route()` 是诚实的"内循环"：确认平台认识、把发回这条消息的
适配器找出来。会话 ID 不由它决定——那是适配器 `normalize()` 的活（各平台
自己最清楚"一连接/一群聊"对应哪个稳定 ID）。

M3 的两件事（§十九-4 防死循环、§十九-5 名称寻址）不在这里提前造：
没有跨会话发消息的需求，防死循环就是空转，名称寻址也没有解析对象。
"""

import logging

from core.adapter.base import AdapterBase, Message
from core.adapter.registry import AdapterRegistry

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class UnknownPlatformError(Exception):
    """消息来自一个没登记适配器的平台——没有谁能把它发回去。"""


class Router:
    """同平台内循环（M0）。跨平台大循环留 M3。"""

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry

    def route(self, message: Message) -> AdapterBase:
        """定位处理这条消息的适配器（同时就是发回结果的适配器）。

        平台不认识就抛 UnknownPlatformError 而不是猜一个——猜错会把回复
        发给不相干的会话（§十九-5：解析失败禁止猜测，这条精神同样适用）。
        """
        adapter = self._registry.lookup(message.platform)
        if adapter is None:
            raise UnknownPlatformError(
                f"没有登记平台 {message.platform!r} 的适配器"
                f"（已登记：{self._registry.names()}）"
            )
        return adapter
