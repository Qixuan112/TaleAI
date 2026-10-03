"""UX-03：关键词唤醒（群聊门 + 未唤醒存库不回）。

两层：
- **策略层**（wake.py）：策略怎么判——范围、关键词命中、@ 事实的汇合。纯函数，脱网测穿。
- **接线层**（main.handle_message）：门真的拦住了模型调用、又真的落了库。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router
from core.event_bus import EventBus
from core.session.store import SessionStore
from core.wake import WakePolicy, load_wake_policy


# ================= 策略层 =================


def test_default_policy_is_group_scope_named_塔利():
    p = WakePolicy()
    assert p.scope == "group"
    assert "塔利" in p.words


def test_group_scope_gates_group_not_private():
    """默认只拦群聊：私聊/WebUI 不受影响。"""
    p = WakePolicy(scope="group")
    assert p.gates("group") is True
    assert p.gates("private") is False


def test_scope_all_gates_everything():
    p = WakePolicy(scope="all")
    assert p.gates("group") is True
    assert p.gates("private") is True


def test_scope_off_gates_nothing():
    p = WakePolicy(scope="off")
    assert p.gates("group") is False
    assert p.gates("private") is False


def test_keyword_substring_wakes():
    p = WakePolicy(words=("塔利",))
    assert p.is_woken("塔利，在吗", None) is True
    assert p.is_woken("今天塔利好忙", None) is True  # 子串包含即可
    assert p.is_woken("大家早", None) is False


def test_addressed_true_wakes_even_without_keyword():
    """被 @ 了就算叫它，不看关键词。"""
    p = WakePolicy(words=("塔利",))
    assert p.is_woken("随便说点什么", True) is True


def test_multiple_wake_words():
    p = WakePolicy(words=("塔利", "小塔"))
    assert p.is_woken("小塔帮我看下", None) is True


def test_empty_words_means_only_at_wakes():
    """没有唤醒词 → 群里只能靠 @ 叫它（returns False 让 @ 之外都不唤醒）。"""
    p = WakePolicy(words=())
    assert p.is_woken("塔利在吗", None) is False
    assert p.is_woken("随便", True) is True


def test_load_wake_policy_defaults_when_unconfigured(tmp_path, monkeypatch):
    """配置里没有 wake → 回默认策略，不炸。"""
    from core.config import loader

    monkeypatch.setattr(loader, "DEFAULT_DIR", tmp_path)
    p = load_wake_policy()
    assert p.scope == "group"
    assert "塔利" in p.words


def test_load_wake_policy_reads_config(tmp_path, monkeypatch):
    from core.config import loader

    monkeypatch.setattr(loader, "DEFAULT_DIR", tmp_path)
    cfg = loader.Config.load("config")
    cfg.data["wake"] = {"words": ["小塔", "阿利"], "scope": "all"}
    cfg.save()

    p = load_wake_policy()
    assert p.words == ("小塔", "阿利")
    assert p.scope == "all"


def test_load_wake_policy_accepts_comma_string(tmp_path, monkeypatch):
    """面板用逗号分隔的文本框；字符串要按逗号拆成多个词。"""
    from core.config import loader

    monkeypatch.setattr(loader, "DEFAULT_DIR", tmp_path)
    cfg = loader.Config.load("config")
    cfg.data["wake"] = {"words": "塔利, 小塔"}
    cfg.save()
    p = load_wake_policy()
    assert p.words == ("塔利", "小塔")


def test_load_wake_policy_bad_scope_falls_back_to_group(tmp_path, monkeypatch):
    from core.config import loader

    monkeypatch.setattr(loader, "DEFAULT_DIR", tmp_path)
    cfg = loader.Config.load("config")
    cfg.data["wake"] = {"words": ["塔利"], "scope": "nonsense"}
    cfg.save()
    assert load_wake_policy().scope == "group"


# ================= 接线层：handle_message =================


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
    def __init__(self):
        self.calls = []

    async def run_loop(self, user_question, history=None, session_id="",
                       *, session_type="", owner="", images=None, quoted=""):
        self.calls.append((user_question, session_id))
        return Reply(session_id=session_id, messages=["好呀~"])


SID = "qq:g30003000"


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "s.db").open()
    s.ensure_session(SID, platform="qq", kind="group", owner="u1")
    yield s
    s.close()


@pytest.fixture
def wiring(store, clean_bus):
    import main

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    return main, adapter, Router(reg), FakeBot(), store, clean_bus


def group_msg(content, addressed=None, **extra):
    return Message(
        id="m1", platform="fake", session_id=SID, owner="u1",
        direction="in", role="user", content=content,
        session_type="group", addressed=addressed, **extra,
    )


async def test_group_message_without_wake_is_stored_but_not_answered(wiring):
    """群聊没叫它 → 落库、但不调模型、不发回（用户定：存进历史但不回）。"""
    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")

    reply = await main.handle_message(
        group_msg("今天天气不错"), router=router, store=store, bot=bot,
        bus=bus, wake=policy,
    )
    assert reply is None
    assert bot.calls == []                      # 没调模型
    assert adapter.sent == []                   # 没发回
    assert store.count(SID) == 1                # 但落库了
    assert store.messages(SID)[0]["content"] == "今天天气不错"


async def test_unwoken_message_still_persists_reply_to(wiring):
    """未唤醒也照落 reply_to——"引用了哪条"是消息事实，与唤没唤醒无关。"""
    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")

    await main.handle_message(
        group_msg("这消息没叫我", reply_to="m0"),
        router=router, store=store, bot=bot, bus=bus, wake=policy,
    )
    assert store.messages(SID)[0]["reply_to"] == "m0"


async def test_group_message_with_keyword_is_answered(wiring):
    """群里喊了名字 → 正常走完整链路。"""
    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")

    reply = await main.handle_message(
        group_msg("塔利，在吗"), router=router, store=store, bot=bot,
        bus=bus, wake=policy,
    )
    assert reply is not None
    assert bot.calls and bot.calls[0][0] == "塔利，在吗"
    assert [r["role"] for r in store.messages(SID)] == ["user", "assistant"]


async def test_group_message_addressed_answer_even_without_keyword(wiring):
    """被 @ 了（addressed=True）——即使正文没有唤醒词也回。"""
    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")

    reply = await main.handle_message(
        group_msg("这个问题谁会", addressed=True), router=router, store=store,
        bot=bot, bus=bus, wake=policy,
    )
    assert reply is not None
    assert bot.calls


async def test_private_message_not_gated(wiring):
    """私聊不受唤醒门约束（scope=group）——直接回。"""
    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")
    store.ensure_session("qq:p1", platform="qq", kind="private", owner="u1")

    msg = Message(
        id="m2", platform="fake", session_id="qq:p1", owner="u1",
        direction="in", role="user", content="随便说点什么", session_type="private",
    )
    reply = await main.handle_message(
        msg, router=router, store=store, bot=bot, bus=bus, wake=policy,
    )
    assert reply is not None
    assert bot.calls


async def test_scope_off_answers_everything(wiring):
    """scope=off → 门形同虚设，群聊没叫它也回。"""
    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="off")

    reply = await main.handle_message(
        group_msg("今天天气不错"), router=router, store=store, bot=bot,
        bus=bus, wake=policy,
    )
    assert reply is not None
    assert bot.calls


async def test_unwoken_message_does_not_double_store(wiring):
    """未唤醒消息只落一条 user 行（不读历史、不建回合），不会重复。"""
    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")

    await main.handle_message(
        group_msg("路人甲说话"), router=router, store=store, bot=bot,
        bus=bus, wake=policy,
    )
    await main.handle_message(
        group_msg("路人乙说话"), router=router, store=store, bot=bot,
        bus=bus, wake=policy,
    )
    assert [r["role"] for r in store.messages(SID)] == ["user", "user"]
    assert all(r["role"] != "assistant" for r in store.messages(SID))
