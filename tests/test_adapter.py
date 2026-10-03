"""M0-11 验收：统一消息模型 + 适配器基类 + 名册 + 路由（§21 任务表）。

设计文档 §3.7 / §18.1 / §18.3 / §19-4、5 / §22。

这里用**假适配器**（不碰网络）验证骨架：归一、收件箱、路由查表。
真 WebSocket 收发的验证在 test_web_adapter.py。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router, UnknownPlatformError
from core.event_bus import EventBus


# ---------- 假适配器：只实现抽象方法，不碰网络 ----------


class FakeAdapter(AdapterBase):
    name = "fake"

    def __init__(self, bus=None, *, name="fake"):
        super().__init__(bus=bus)
        self.name = name
        self.sent: list[Reply] = []
        self.started = False

    def normalize(self, raw):
        return Message(
            id=str(raw.get("id", "m1")),
            platform=self.name,
            session_id=raw.get("session_id", "s1"),
            owner=raw.get("owner", "u1"),
            direction="in",
            role="user",
            content=raw.get("content", ""),
        )

    async def send(self, reply):
        self.sent.append(reply)

    async def start(self):
        self.started = True


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


# ---------- Message 数据结构 ----------


def test_message_has_documented_fields():
    """字段齐全（§18.3）：id/平台/会话/owner/方向/role/正文 + 后四个。"""
    m = Message(
        id="m1", platform="web", session_id="web:local", owner="local",
        direction="in", role="user", content="你好",
    )
    assert m.mentions == []  # M0 空列表占位（方案 B）
    assert m.reply_to is None
    assert m.meta == {}


def test_message_meta_is_per_instance():
    """默认值不能是共享可变对象——否则两个 Message 会共用同一个 list。"""
    a = Message(id="a", platform="p", session_id="s", owner="o",
                direction="in", role="user", content="x")
    b = Message(id="b", platform="p", session_id="s", owner="o",
                direction="in", role="user", content="y")
    a.mentions.append("@someone")
    assert b.mentions == []


def test_reply_defaults():
    r = Reply()
    assert r.messages == [] and r.tool_calls_made == 0 and r.stop_reason == "ok"


# ---------- AdapterBase：收件箱 + recv ----------


async def test_recv_returns_delivered_message():
    adapter = FakeAdapter()
    adapter._deliver(adapter.normalize({"content": "在吗"}))
    got = await adapter.recv()
    assert got.content == "在吗"


async def test_recv_blocks_until_a_message_arrives():
    """recv 是前台循环的阻塞点：没消息时该等，不该空转返回。"""
    adapter = FakeAdapter()
    task = asyncio.create_task(adapter.recv())
    await asyncio.sleep(0)
    assert not task.done()  # 还没消息 → 仍挂着
    adapter._deliver(adapter.normalize({"content": "来了"}))
    got = await asyncio.wait_for(task, timeout=1)
    assert got.content == "来了"


async def test_deliver_does_not_block_when_queue_unread():
    """put_nowait：投递方（平台回调）绝不能因为没人取而阻塞。"""
    adapter = FakeAdapter()
    for i in range(100):
        adapter._deliver(adapter.normalize({"content": f"第{i}"}))
    assert (await adapter.recv()).content == "第0"


def test_deliver_publishes_message_received(clean_bus):
    """§18.2 把 message.received 的生产者记为 Adapter：投递时旁路喊一声。"""
    adapter = FakeAdapter(bus=clean_bus)
    seen = []
    clean_bus.subscribe("message.received", lambda e: seen.append(e.data))
    adapter._deliver(adapter.normalize({"content": "你好", "session_id": "web:local"}))
    assert len(seen) == 1
    assert seen[0]["session_id"] == "web:local"
    assert seen[0]["content"] == "你好"


def test_deliver_still_queues_when_bus_handler_explodes(clean_bus):
    """总线是旁路：订阅者坏掉不能影响消息被收下（§18.5 硬规则 2 精神）。"""
    adapter = FakeAdapter(bus=clean_bus)

    def bad(event):
        raise RuntimeError("坏的订阅者")

    clean_bus.subscribe("message.received", bad)
    adapter._deliver(adapter.normalize({"content": "照样要收下"}))
    assert adapter._inbox.qsize() == 1


def test_adapter_base_cannot_be_instantiated_directly():
    """抽象基类：漏实现 normalize/send/start 的子类不许实例化。"""
    class Incomplete(AdapterBase):
        name = "x"

    with pytest.raises(TypeError):
        Incomplete()


# ---------- AdapterRegistry ----------


def test_registry_register_and_lookup():
    reg = AdapterRegistry()
    a = FakeAdapter(name="web")
    reg.register(a)
    assert reg.lookup("web") is a


def test_registry_lookup_unknown_returns_none():
    assert AdapterRegistry().lookup("nope") is None


def test_registry_rejects_nameless_adapter():
    """没有平台标识就没法寻址——必须在登记时就挡下。"""
    reg = AdapterRegistry()
    with pytest.raises(ValueError, match="name"):
        reg.register(FakeAdapter(name=""))


def test_registry_names_lists_registered():
    reg = AdapterRegistry()
    reg.register(FakeAdapter(name="a"))
    reg.register(FakeAdapter(name="b"))
    assert set(reg.names()) == {"a", "b"}


def test_registry_reset_clears():
    reg = AdapterRegistry()
    reg.register(FakeAdapter(name="a"))
    reg.reset()
    assert reg.lookup("a") is None


# ---------- Router（M0 内循环） ----------


def test_router_routes_to_matching_platform():
    reg = AdapterRegistry()
    a = FakeAdapter(name="web")
    reg.register(a)
    router = Router(reg)
    msg = a.normalize({"content": "hi"})
    assert router.route(msg) is a


def test_router_unknown_platform_raises_not_guesses():
    """平台不认识就报错，禁止猜（§19-5 精神：猜错会把回复发错地方）。"""
    router = Router(AdapterRegistry())
    msg = Message(id="m", platform="qq", session_id="s", owner="o",
                  direction="in", role="user", content="x")
    with pytest.raises(UnknownPlatformError, match="qq"):
        router.route(msg)


def test_router_picks_the_right_one_among_many():
    reg = AdapterRegistry()
    ws = FakeAdapter(name="web")
    qq = FakeAdapter(name="qq")
    reg.register(ws)
    reg.register(qq)
    router = Router(reg)
    assert router.route(qq.normalize({"content": "x"})) is qq
