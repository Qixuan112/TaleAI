"""实时日志流：把「塔利在干什么」广播给浏览器（SSE）。

设计文档 §18.2（事件目录）的兑现补充：事件目录里 `message.received` /
`tool.called` / `message.sent` 这三条的订阅者都写着「日志」——本模块就是那个
「日志」订阅者，把总线事件转成页面能看的帧。

为什么单开一个模块、而不是塞进 core/log.py：
- core/log.py 管的是「日志落盘」，职责单一；这里是「日志外送」，两件事。
- 更硬的理由：log.py 的 init() 有个明确契约——**只加一个文件 handler**
  （tests/test_log.py::test_no_console_handler 钉着）。往里面塞第二个
  handler 会打破它。所以镜像 handler 由服务启动时**单独挂**（见 main._serve）。

传输入口（HTTP / SSE 端点）不在这里——本模块**不认识 FastAPI**，只持
订阅队列。这样将来换传输（WS / 甚至写文件）不用动它，符合 §22 import 单向：
适配器认识总线，本模块只认识 stdlib。

为什么订阅队列用 stdlib `queue.Queue` 而不是 asyncio.Queue：
logging.Handler.emit() 可能在**任意线程**被调用（logging 不保证线程），
asyncio.Queue 不是线程安全的，跨线程 put 要 call_soon_threadsafe、还要先
绑定事件循环，复杂度陡增。stdlib Queue 自带锁，publish 从哪个线程调都安全。
代价是消费端要轮询（无法 await 队列），100~200ms 的延迟在日志页上肉眼
不可见——这笔账划算。
"""

import asyncio
import json
import logging
import queue
import time
from collections import deque
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)
# 本模块不发 INFO 日志——它自己就是日志出口，往里写会自我循环刷屏
logger.addHandler(logging.NullHandler())

#: 环形缓冲上限：新连上的页面回放这么多条「最近发生的事」
RECENT_MAX = 500

#: 消费端轮询间隔（秒）。日志页对延迟不敏感，200ms 足够跟手。
POLL_SEC = 0.2

#: 心跳间隔（秒）。SSE 长时间只有单向流时，中间代理可能掐连接；定期发注释行保活。
HEARTBEAT_SEC = 15.0

#: 每个订阅者的队列上限。满了就丢帧（丢日志无妨），绝不阻塞发布方。
_QUEUE_MAX = 1000

#: 要转成事件的类型（§18.2 目录里订阅者为「日志」的那三条）
_BUS_EVENTS = ("message.received", "tool.called", "message.sent")


def to_sse(frame: dict[str, Any]) -> str:
    """一帧 → SSE 文本。SSE 规范：`data: <一行>\n\n` 结尾。

    ensure_ascii=False：中文原样发（页面本来就 utf-8），省带宽也好看。
    帧里不含换行（都是结构化字段），不怕 SSE 的「一个 data 行」限制。
    """
    return "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"


class LogStream:
    """日志/事件广播器：一对多分发 + 环形回放。

    生命周期：main 启动时建一个，订阅总线、被适配器拿去当 `/events` 的数据源。
    进程内单实例即可（不需要单例，main 拿着传下去就行）。
    """

    def __init__(self) -> None:
        # 订阅者的收件队列。publish 时逐个投递（满了丢帧）。
        self._subscribers: list[queue.Queue] = []
        # 最近帧的环形缓冲：新页面连上先回放，不然只看到「从现在起」的日志
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT_MAX)

    # ---------- 发布 ----------

    def publish(self, frame: dict[str, Any]) -> None:
        """广播一帧。线程安全（stdlib Queue 自带锁），绝不抛、绝不阻塞。"""
        self._recent.append(frame)
        for q in list(self._subscribers):
            try:
                q.put_nowait(frame)
            except queue.Full:
                # 订阅者卡住（页面没在消费）——丢这一帧，不拖累别人
                pass

    def subscribe_bus(self, bus) -> None:
        """订阅总线里「订阅者为日志」的三个事件，转成帧广播出去。

        EventBus.subscribe 幂等，重复调用无害。事件名从总线来（§18.2 定稿目录），
        本模块不自造事件类型。
        """
        for name in _BUS_EVENTS:
            bus.subscribe(name, self._make_bus_handler(name))

    def _make_bus_handler(self, name: str):
        def handler(event) -> None:
            frame: dict[str, Any] = {"type": "event", "name": name, "ts": event.ts}
            frame.update(event.data)
            self.publish(frame)

        return handler

    # ---------- 订阅（面向传输层） ----------

    def subscribe(self) -> queue.Queue:
        """登记一个订阅者，返回它的收件队列。"""
        q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """退订（页面断开时调用，别让订阅表一直涨）。"""
        if q in self._subscribers:
            self._subscribers.remove(q)

    def recent(self) -> list[dict[str, Any]]:
        """最近帧的快照（测试与回放用）。"""
        return list(self._recent)

    # ---------- SSE 生成器 ----------

    async def stream(self) -> AsyncIterator[str]:
        """SSE 数据流：先回放最近帧，再持续推新帧，定期发心跳。

        客户端断开时 FastAPI 会取消这个生成器 → 触发 finally 里的退订。
        """
        q = self.subscribe()
        try:
            # 1) 回放：让刚打开的页面立刻看到「刚才发生了什么」
            for frame in self.recent():
                yield to_sse(frame)
            # 2) 增量：轮询队列
            last_beat = time.monotonic()
            while True:
                try:
                    while True:
                        yield to_sse(q.get_nowait())
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now - last_beat >= HEARTBEAT_SEC:
                    yield ": keep-alive\n\n"  # SSE 注释行，给中间代理保活用
                    last_beat = now
                await asyncio.sleep(POLL_SEC)
        finally:
            self.unsubscribe(q)


class StreamLogHandler(logging.Handler):
    """把 root logger 的每条日志（INFO+）镜像成 Stream 帧。

    **由服务启动时单独挂**，不进 Logging.init()——init() 的契约是「只加一个
    文件 handler」（tests/test_log.py 钉着），加在这里会打破它。
    """

    def __init__(self, stream: LogStream, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            frame = {
                "type": "log",
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            self._stream.publish(frame)
        except Exception:
            # logging 约定：emit 绝不向外抛（否则会污染调用链）。照 handleError 办。
            self.handleError(record)


__all__ = ["LogStream", "StreamLogHandler", "to_sse", "RECENT_MAX", "POLL_SEC"]
