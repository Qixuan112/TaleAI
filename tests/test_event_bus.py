"""M0-10 验收：事件总线「pub/sub 单测过」（§21 任务表）。

设计文档 §18.1 旁路 / §18.2 事件目录与实现 / §18.5 硬规则 2 / §19-10。

重点验的是**契约**而不是实现细节：订阅顺序、异常不阻断、不存状态。
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.bus.event_bus import Event, EventBus


@pytest.fixture(autouse=True)
def clean_bus():
    """单例是全局的，每个用例前后清干净，否则互相污染。"""
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


# ---------- 基本收发 ----------


def test_subscribe_then_publish_delivers(clean_bus):
    got = []
    clean_bus.subscribe("message.sent", got.append)
    clean_bus.publish("message.sent", session_id="s1")
    assert len(got) == 1
    assert got[0].type == "message.sent"
    assert got[0].data == {"session_id": "s1"}


def test_publish_without_subscribers_is_silent(clean_bus):
    """没人订阅就静默返回——事件丢了无所谓，事实源在 events.jsonl。"""
    clean_bus.publish("nobody.listens")  # 不抛异常即通过


def test_multiple_subscribers_all_receive(clean_bus):
    a, b = [], []
    clean_bus.subscribe("evt", a.append)
    clean_bus.subscribe("evt", b.append)
    clean_bus.publish("evt")
    assert len(a) == 1 and len(b) == 1


def test_handlers_called_in_subscribe_order(clean_bus):
    """顺序 = 订阅顺序（§18.2：同一事件循环内天然成立）。"""
    order = []
    for name in ("first", "second", "third"):
        clean_bus.subscribe("evt", lambda e, n=name: order.append(n))
    clean_bus.publish("evt")
    assert order == ["first", "second", "third"]


def test_only_matching_type_receives(clean_bus):
    """订阅是按事件类型隔离的，别的类型不该收到。"""
    got = []
    clean_bus.subscribe("a", got.append)
    clean_bus.publish("b")
    assert got == []


# ---------- 订阅生命周期 ----------


def test_duplicate_subscribe_is_idempotent(clean_bus):
    """重复订阅同一个 handler 只收一份——不然是"不报错但诡异"的 bug。"""
    got = []
    clean_bus.subscribe("evt", got.append)
    clean_bus.subscribe("evt", got.append)
    clean_bus.publish("evt")
    assert len(got) == 1


def test_unsubscribe_stops_delivery(clean_bus):
    got = []
    clean_bus.subscribe("evt", got.append)
    clean_bus.unsubscribe("evt", got.append)
    clean_bus.publish("evt")
    assert got == []


def test_unsubscribe_unknown_handler_is_silent(clean_bus):
    clean_bus.unsubscribe("evt", lambda e: None)  # 不抛异常即通过


# ---------- 不变量：handler 抛异常不阻断（§19-10） ----------


def test_one_bad_handler_does_not_block_others(clean_bus):
    good = []

    def bad(event):
        raise RuntimeError("我坏了")

    clean_bus.subscribe("evt", bad)
    clean_bus.subscribe("evt", good.append)
    clean_bus.publish("evt")  # 不该抛出来
    assert len(good) == 1  # 坏 handler 之后的订阅者照样收到


def test_publish_never_raises_to_caller(clean_bus):
    """总线在旁路，绝不能把异常抛回调用链（污染主流程）。"""

    def bad(event):
        raise ValueError("boom")

    clean_bus.subscribe("evt", bad)
    clean_bus.publish("evt")  # 不抛异常即通过


def test_bad_handler_failure_is_logged(clean_bus, caplog):
    def bad(event):
        raise RuntimeError("我坏了")

    clean_bus.subscribe("evt", bad)
    with caplog.at_level(logging.ERROR, logger="core.bus.event_bus"):
        clean_bus.publish("evt")
    assert any("handler" in r.message for r in caplog.records)


# ---------- 不变量：不存状态（§18.5 硬规则 2） ----------


def test_bus_exposes_no_state_query_api(clean_bus):
    """接口上就不该有读事件历史/查状态的方法——从设计上断掉"存状态"这条路。"""
    for forbidden in ("history", "events", "get", "replay", "query", "state"):
        assert not hasattr(clean_bus, forbidden), f"EventBus 不该有 {forbidden}()"


# ---------- 旁路模型：handler 入队而非干活 ----------


def test_handler_can_put_nowait_into_its_own_queue(clean_bus):
    """标准用法（§19-10）：订阅者把自己那份投进自己的收件箱，不阻塞发布方。"""
    inbox: asyncio.Queue = asyncio.Queue()

    def on_event(event):
        inbox.put_nowait(event)

    clean_bus.subscribe("message.received", on_event)
    clean_bus.publish("message.received", content="你好")
    assert inbox.qsize() == 1
    assert inbox.get_nowait().data["content"] == "你好"


def test_six_canonical_event_types_can_coexist(clean_bus):
    """§18.2 定稿的 6 个事件名，总线只当字符串认，不该挑食。"""
    seen = []
    for name in (
        "message.received",
        "message.sent",
        "tool.called",
        "session.closed",
        "memory.extracted",
        "plan.fired",
    ):
        clean_bus.subscribe(name, lambda e: seen.append(e.type))
        clean_bus.publish(name)
    assert seen == [
        "message.received",
        "message.sent",
        "tool.called",
        "session.closed",
        "memory.extracted",
        "plan.fired",
    ]


# ---------- Event 与单例 ----------


def test_event_is_frozen():
    """通知是只读的，订阅者只看不改。"""
    event = Event(type="evt", data={}, ts=0.0)
    with pytest.raises(Exception):
        event.type = "改了"


def test_publish_stamps_timestamp(clean_bus):
    got = []
    clean_bus.subscribe("evt", got.append)
    clean_bus.publish("evt")
    assert got[0].ts > 0


def test_is_a_singleton(clean_bus):
    assert EventBus() is EventBus()
