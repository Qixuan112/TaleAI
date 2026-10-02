"""M0-11 验收：前台单条消息的完整处理（§18.1 第 3~9 步）。

设计文档 §18.1 控制流 / §18.2 事件 / §十九-2。

前台循环的核心是"一条消息进来，走完这一串，回复发出去"。这里把它测穿：
收即存的**顺序**、落库、发回、事件发布、失败时把回合收平。
用假 bot + 真 SessionStore（tmp_path）——既验证真实落库，又不碰网络。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# 项目根也加进来：前台循环按文档住在 main.py（§18.1「控制流从 main() 开始」）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router
from core.bus.event_bus import EventBus
from core.session.store import SessionStore


# ---------- 假件 ----------


class FakeAdapter(AdapterBase):
    name = "fake"

    def __init__(self, bus=None):
        super().__init__(bus=bus)
        self.sent: list[Reply] = []

    def normalize(self, raw):
        raise NotImplementedError

    async def send(self, reply):
        self.sent.append(reply)

    async def start(self):
        pass


class FakeBot:
    """只实现前台循环用得到的 run_loop（async）。"""

    def __init__(self, reply=None, error=None, on_call=None):
        self.reply = reply if reply is not None else Reply(messages=["好呀~"])
        self.error = error
        self.on_call = on_call
        self.calls: list[tuple] = []

    async def run_loop(self, user_question, history=None, session_id="",
                       *, session_type="", owner=""):
        self.calls.append((user_question, list(history or []), session_id))
        if self.on_call:
            self.on_call(user_question, history, session_id)
        if self.error:
            raise self.error
        reply = self.reply
        reply.session_id = session_id
        return reply


SID = "web:local"


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "s.db").open()
    s.ensure_session(SID)
    yield s
    s.close()


@pytest.fixture
def wiring(store, clean_bus):
    """返回 (handle_message 可调用, adapter, bot, store, bus)。"""
    import main

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    router = Router(reg)
    bot = FakeBot()

    def call(message, **kw):
        return main.handle_message(
            message, router=router, store=store, bot=kw.pop("bot", bot),
            bus=clean_bus, **kw
        )

    return call, adapter, bot, store, clean_bus


def make_message(content="你好", *, platform="fake", session_id=SID):
    return Message(
        id="m1", platform=platform, session_id=session_id, owner="local",
        direction="in", role="user", content=content,
    )


# ---------- 正常路径 ----------


async def test_happy_path_persists_and_sends(wiring):
    call, adapter, bot, store, bus = wiring
    reply = await call(make_message("你好"))
    assert reply.messages == ["好呀~"]
    # 两条都落库（1 回合 = 2 行）
    assert [r["role"] for r in store.messages(SID)] == ["user", "assistant"]
    assert store.history(SID) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "好呀~"},
    ]
    # 发回了原会话
    assert len(adapter.sent) == 1
    assert adapter.sent[0].session_id == SID


async def test_user_message_is_persisted_before_model_call(store, clean_bus):
    """收即存（§18.1 第 4 步）：用户消息必须在调模型**之前**进库。

    这是崩溃不丢的关键——模型调一半崩了，用户那句话也已经在库里。
    """
    import main

    seen = {}

    def on_call(user_question, history, session_id):
        # 模型被调用的这一刻，库里应该已经有这条 user 消息
        seen["rows"] = store.count(session_id)

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    bot = FakeBot(on_call=on_call)

    await main.handle_message(
        make_message("我一会儿要去开会"),
        router=Router(reg), store=store, bot=bot, bus=clean_bus,
    )
    assert seen["rows"] == 1  # 调模型时已存 1 条（user），还没 assistant


async def test_model_receives_pre_write_history_not_the_new_question_twice(store, clean_bus):
    """传给模型的历史是**落库前**的快照——否则最新提问会被发两遍。

    装配 = history + 最新提问。若这里给的是落库后的 history，最新提问
    就同时躺在 history 末尾和"最新提问"里，重复发送。
    """
    import main

    store.append(SID, "user", "早")
    store.append(SID, "assistant", "早呀~")

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    bot = FakeBot()

    await main.handle_message(
        make_message("现在几点"), router=Router(reg), store=store, bot=bot, bus=clean_bus,
    )
    _q, history, _sid = bot.calls[0]
    # 历史里不该出现"现在几点"——它作为最新提问单独传
    assert all("现在几点" not in m["content"] for m in history)
    assert history[-1]["content"] == "早呀~"


async def test_blank_message_is_ignored(store, clean_bus):
    """空白消息不发请求、不落库（跟 CLI 空行一致）。"""
    import main

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    bot = FakeBot()

    reply = await main.handle_message(
        make_message("   "), router=Router(reg), store=store, bot=bot, bus=clean_bus,
    )
    assert reply is None
    assert bot.calls == []
    assert store.count(SID) == 0
    assert adapter.sent == []


# ---------- 事件（§18.2 / §18.1 第 9 步） ----------


async def test_message_sent_is_published(wiring):
    call, adapter, bot, store, bus = wiring
    seen = []
    bus.subscribe("message.sent", lambda e: seen.append(e.data))
    await call(make_message("你好"))
    assert len(seen) == 1
    assert seen[0]["session_id"] == SID


async def test_message_sent_fires_after_adapter_send(wiring):
    """事件名是"已发送"——必须在真的 send 之后才发（§18.1 第 9 步排在 8 之后）。"""
    call, adapter, bot, store, bus = wiring
    order = []
    bus.subscribe("message.sent", lambda e: order.append("event"))
    adapter_send = adapter.send

    async def spy_send(reply):
        order.append("send")
        await adapter_send(reply)

    adapter.send = spy_send
    await call(make_message("你好"))
    assert order == ["send", "event"]


# ---------- 失败路径：把回合收平 ----------


async def test_model_failure_still_closes_the_turn(store, clean_bus):
    """模型抛异常：补一条占位 assistant 行，别让 user 行单挂。

    user 已落库，若就此中断，历史里会剩一条单挂的 user 行——而弹簧窗口
    按「1 回合 = 2 行」计数，单挂会让回合计数错位。
    """
    import main

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    bot = FakeBot(error=RuntimeError("provider 挂了"))

    reply = await main.handle_message(
        make_message("你好"), router=Router(reg), store=store, bot=bot, bus=clean_bus,
    )
    rows = store.messages(SID)
    assert [r["role"] for r in rows] == ["user", "assistant"]  # 回合完整
    assert rows[0]["content"] == "你好"
    assert reply is not None and reply.stop_reason == "error"
    # 失败也要给用户一个交代，而不是界面卡住
    assert adapter.sent and adapter.sent[-1].messages


async def test_unknown_platform_does_not_crash_the_loop(store, clean_bus):
    """平台不认识：记日志、跳过，不能让前台循环死掉。"""
    import main

    router = Router(AdapterRegistry())  # 空名册
    bot = FakeBot()
    reply = await main.handle_message(
        make_message(platform="qq"), router=router, store=store, bot=bot, bus=clean_bus,
    )
    assert reply is None
    assert bot.calls == []  # 没路由成功就不该调模型


async def test_user_write_failure_skips_the_model_call(store, clean_bus, monkeypatch):
    """user 落库失败：这轮不调模型（免得用户以为发出去了），但**要回一帧**。

    契约修正（PR #10）：旧实现直接 return None，适配器收不到任何帧，WebUI
    永远停在「塔利在想…」。现在补发一条 error 回复让界面解开；仍返回 None
    （user 行没落库，不能补写 assistant 行，保住「1 回合 = 2 行」）。
    """
    import main

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    bot = FakeBot()

    def boom(*a, **k):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(store, "append", boom)
    reply = await main.handle_message(
        make_message("你好"), router=Router(reg), store=store, bot=bot, bus=clean_bus,
    )
    assert reply is None          # 不补 assistant 行，回合不成立
    assert bot.calls == []        # 没调模型
    assert len(adapter.sent) == 1  # 但回了一帧，界面不会卡住
    assert adapter.sent[0].stop_reason == "error"


# ---------- serve_forever：把适配器挂起来 ----------


async def test_serve_forever_processes_one_message_then_stops(store, clean_bus):
    """serve_forever 从适配器收件箱取消息并走完处理链，直到被取消。"""
    import main

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    bot = FakeBot()

    task = asyncio.create_task(
        main.serve_forever(
            [adapter], router=Router(reg), store=store, bot=bot, bus=clean_bus
        )
    )
    # 模拟适配器收到一条消息
    adapter._deliver(make_message("你好"))
    await asyncio.sleep(0.05)  # 给循环一点时间跑完
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.history(SID) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "好呀~"},
    ]
    assert len(adapter.sent) == 1
