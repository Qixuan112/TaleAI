"""适配器名册（设计文档 §18.1 启动第 8 步 / §22）。

只干一件事：按平台名记住"哪个适配器负责哪个平台"。Router 靠它做
"消息该由谁发回"的查表，跨平台路由（M3）也靠它找目标适配器。

不做单例：它由 main 在组合根处创建、注入给 Router，这样测试可以造一个
干净的实例、不碰全局状态。（§22 明确标了单例的只有 PluginRegistry
和 EventBus。）
"""

import logging

from core.adapter.base import AdapterBase

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class AdapterRegistry:
    """平台名 → 适配器。"""

    def __init__(self) -> None:
        self._adapters: dict[str, AdapterBase] = {}

    def register(self, adapter: AdapterBase) -> None:
        """登记一个适配器，键是它的 `name`。

        同名冲突要响一声再覆盖：静默覆盖会让"两个适配器抢同一个 key"
        变成一个查不出来的 bug（跟插件注册表同一教训，§五）。
        """
        name = adapter.name
        if not name:
            raise ValueError("适配器必须有 name（平台标识），否则路由无法寻址")
        if name in self._adapters and self._adapters[name] is not adapter:
            logger.warning("适配器名冲突：%r 被覆盖（新实例替换旧实例）", name)
        self._adapters[name] = adapter

    def lookup(self, name: str) -> AdapterBase | None:
        """按平台名找适配器；没有则返回 None（由调用方决定怎么处理）。"""
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return list(self._adapters)

    def reset(self) -> None:
        """清空名册（测试用）。"""
        self._adapters.clear()
