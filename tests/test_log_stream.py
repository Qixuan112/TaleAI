"""SSE 日志流单测：广播器的分发、回放、总线转帧、镜像 handler。

测的是契约（§18.2 事件目录里「订阅者为日志」的那几条），不测 HTTP——
端点接线由 test_sse_endpoint.py 管。
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.event_bus import EventBus
from core.log_stream import LogStream, StreamLogHandler, to_sse


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


# ---------- publish / subscribe ----------


def test_publish_reaches_subscriber():
    s = LogStream()
    q = s.subscribe()
    s.publish({"type": "log", "message": "hi"})
    assert q.get_nowait() == {"type": "log", "message": "hi"}


def test_publish_fans_out_to_all_subscribers():
    """一对多：每个订阅者都收到（多个页面同时看日志）。"""
    s = LogStream()
    q1, q2 = s.subscribe(), s.subscribe()
    s.publish({"type": "log", "n": 1})
    assert q1.get_nowait() == q2.get_nowait() == {"type": "log", "n": 1}


def test_unsubscribe_stops_delivery():
    s = LogStream()
    q = s.subscribe()
    s.unsubscribe(q)
    s.publish({"type": "log"})
    assert q.empty()


def test_recent_ring_keeps_latest_only():
    """环形缓冲：超出上限后最旧的被挤掉，且只留上限那么多条。"""
    from core.log_stream import RECENT_MAX

    s = LogStream()
    for i in range(RECENT_MAX + 50):
        s.publish({"type": "log", "n": i})
    recent = s.recent()
    assert len(recent) == RECENT_MAX
    assert recent[-1]["n"] == RECENT_MAX + 49
    assert recent[0]["n"] == 50  # 最前面的被挤掉了


def test_publish_never_raises_when_subscriber_queue_full():
    """订阅者卡住（队列满）→ 丢帧，不抛、不阻塞发布方。"""
    s = LogStream()
    q = s.subscribe()
    # 灌爆队列：不设 _QUEUE_MAX 边界，直接灌到满
    from core.log_stream import _QUEUE_MAX

    for i in range(_QUEUE_MAX + 10):
        s.publish({"type": "log", "n": i})  # 不抛即通过
    assert q.full()


# ---------- 总线转帧 ----------


def test_subscribe_bus_turns_events_into_frames(clean_bus):
    s = LogStream()
    s.subscribe_bus(clean_bus)
    q = s.subscribe()
    clean_bus.publish("message.received", session_id="web:local", content="你好")
    frame = q.get_nowait()
    assert frame["type"] == "event"
    assert frame["name"] == "message.received"
    assert frame["session_id"] == "web:local"
    assert frame["content"] == "你好"
    assert "ts" in frame


def test_subscribe_bus_covers_the_log_catalog(clean_bus):
    """事件目录里订阅者为「日志」的三条都要转：received / tool.called / sent。"""
    s = LogStream()
    s.subscribe_bus(clean_bus)
    q = s.subscribe()
    clean_bus.publish("message.received", session_id="s")
    clean_bus.publish("tool.called", tool="ping", ok=True)
    clean_bus.publish("message.sent", session_id="s")
    names = [q.get_nowait()["name"] for _ in range(3)]
    assert names == ["message.received", "tool.called", "message.sent"]


# ---------- SSE 生成器 ----------


async def test_stream_replays_recent_then_delivers_new():
    s = LogStream()
    s.publish({"type": "log", "n": "old"})  # 连接前的既有帧
    agen = s.stream()
    first = await agen.__anext__()  # 回放帧
    assert "old" in first
    s.publish({"type": "log", "n": "new"})  # 连接后的新帧
    got = await asyncio.wait_for(agen.__anext__(), timeout=2)
    assert "new" in got
    await agen.aclose()


async def test_stream_unsubscribes_on_close():
    """生成器被取消/关闭后要退订——否则订阅表随重连一直涨。"""
    s = LogStream()
    # 先放一帧，让 stream() 一开跑就有回放帧可 yield；否则它要等到 15s 的心跳
    # 才吐第一帧，这个用例就会白等 15 秒（它只关心订阅表，不关心帧内容）。
    s.publish({"type": "log", "n": "x"})
    agen = s.stream()
    await agen.__anext__()  # 回放帧立刻返回，订阅已登记
    assert len(s._subscribers) == 1
    await agen.aclose()
    assert s._subscribers == []


def test_to_sse_format():
    """SSE 帧以 data: 开头、\\n\\n 结尾；中文不转义。"""
    text = to_sse({"type": "log", "message": "你好"})
    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    assert "你好" in text


# ---------- 镜像 handler ----------


def test_stream_log_handler_publishes_log_frame():
    s = LogStream()
    q = s.subscribe()
    h = StreamLogHandler(s)
    rec = logging.LogRecord(
        "core.test", logging.INFO, __file__, 1, "塔利说了：%s", ("嗨",), None
    )
    h.emit(rec)
    frame = q.get_nowait()
    assert frame["type"] == "log"
    assert frame["level"] == "INFO"
    assert frame["logger"] == "core.test"
    assert frame["message"] == "塔利说了：嗨"  # getMessage() 做了 % 格式化


def test_stream_log_handler_never_raises():
    """镜像出问题也绝不向外抛——logging 的 emit 契约。"""

    class Boom(LogStream):
        def publish(self, frame):
            raise RuntimeError("炸了")

    h = StreamLogHandler(Boom())
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", (), None)
    h.emit(rec)  # 不抛即通过
