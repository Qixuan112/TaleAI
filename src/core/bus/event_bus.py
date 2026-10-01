"""事件总线：进程内 pub/sub，只发通知、不存状态。

设计文档 §18.2（事件目录 + 总线实现）/ §18.5 硬规则 2 / §19-10 / §22。

三条从文档搬来的铁律：

1. **旁路，不是控制流**（§18.1）：控制流从 main() 沿调用链走（同步 + await），
   总线只在旁边喊一嗓子"发生什么事了"。谁都不靠总线驱动主流程。

2. **handler 只许 put_nowait 或记日志**（§19-10）：订阅者收到通知后做的事必须
   非阻塞——往自己的 asyncio.Queue 塞一下就够了，重活留给消费端在自己的协程里干。
   总线上跑同步重活会拖垮主循环。这条是使用规约，代码拦不住，但下一句能拦。

3. **不存状态**（§18.5 硬规则 2）：谁在 EventBus 里存状态就是设计错误。这条不靠
   自律——EventBus 的 API 里**根本没有**读/写/查的方法，从接口上就断了这条路。

"总线丢、落盘不丢"（§18.2）：事件原文由**生产者**在 publish 之前同步追加到
events.jsonl；总线只负责叫醒订阅者。所以这里没有事件历史、没有重放——重放是
MemoryWorker 从 events.jsonl 按游标重扫的事。

纯标准库、零第三方依赖。将来拆多进程时换实现，`subscribe` / `publish` 这两个
接口保持不变（§18.2）。
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)
# 库不该在应用没配日志时往 stderr 喷（与 plugin/registry 一致）
logger.addHandler(logging.NullHandler())


@dataclass(frozen=True)
class Event:
    """一条通知。

    字段刻意最小：总线的 Event 是**内存里的通知**，events.jsonl 是**事实源的
    落盘记录**，两者不该耦合。session_id / owner 这类上下文放进 data，由生产者
    决定要不要带。

    frozen：通知是只读的，订阅者只看不改。想加工就自己造新对象。
    """

    type: str
    data: dict[str, Any]
    ts: float


Handler = Callable[[Event], None]


class EventBus:
    """进程内事件总线。单例（§22）。

    只用一把 dict 订阅表 + publish 同步遍历（§18.2），不引队列、不引锁——
    单线程 asyncio 模型下 publish 本就串行（§18.5），加锁是多余的。

    单例的理由：订阅发生在启动时（main 第 7 步），发布发生在运行时各处，
    两边必须是同一张订阅表。
    """

    _instance: "EventBus | None" = None

    def __new__(cls) -> "EventBus":
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._handlers: dict[str, list[Handler]] = {}
            cls._instance = instance
        return cls._instance

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """订阅某类事件。幂等——重复订阅同一个 handler 不会收到两份。

        幂等是刻意的：重复收到通知属于"不报错但行为诡异"的 bug，从源头掐掉
        比事后排查便宜。
        """
        handlers = self._handlers.setdefault(event_type, [])
        if handler in handlers:
            return
        handlers.append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """退订。订阅表是可变全局状态，必须有办法清理（测试尤其需要）。"""
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def publish(self, event_type: str, **data: Any) -> None:
        """发布一条通知，同步、按订阅顺序分发给该类型的全部 handler。

        - 顺序 = 订阅顺序（同一事件循环内天然成立，§18.2）。
        - 单个 handler 抛异常 → 记日志、继续下一个（§19-10）。一个订阅者坏掉
          不能连累其他人收到通知，也不能把异常抛回发布者、污染调用链。
        - 没有订阅者 → 静默返回。事件丢了无所谓——事实源在 events.jsonl。
        """
        handlers = self._handlers.get(event_type)
        if not handlers:
            return

        event = Event(type=event_type, data=data, ts=time.time())
        # 拷贝一份再遍历：handler 里可能退订，直接遍历原表会漏发或报错
        for handler in list(handlers):
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "事件 handler 抛异常，已忽略：type=%s handler=%r",
                    event_type,
                    getattr(handler, "__qualname__", handler),
                )

    def reset(self) -> None:
        """清空订阅表（测试用）。"""
        self._handlers.clear()
