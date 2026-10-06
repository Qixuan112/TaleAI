"""聊天命令（后端）：&newtale 重置会话——QQ 也能重置。

此前重置只做在 WebUI 前端（index.html 拦截后发 clear 控制帧）——QQ 的消息
根本不经过那段 JS，所以 QQ 里没法重置（用户报的坑）。判定提到后端
`handle_message`（core/commands.py），任何平台都能用；WebUI 前端保留快路径
（本地抹气泡、不往返）。

本文件钉住：判定语义、重置行为（清库+回执、不调模型、不留痕）、
唤醒门优先级（群里没叫它不给重置）、前后端命令名一致。
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router
from core.commands import command_of, is_reset_command
from core.event_bus import EventBus
from core.session.store import SessionStore


# ================= 判定层：command_of / is_reset_command =================


def test_accepts_both_prefixes():
    assert command_of("&newtale") == "newtale"
    assert command_of("/newtale") == "newtale"


def test_case_insensitive_and_trimmed():
    assert command_of("  /NewTale  ") == "newtale"
    assert is_reset_command("&NEWTALE") is True


def test_whole_line_required():
    """"你好 &newtale" / "&newtale 你好" 都不是命令——整行才算。"""
    assert command_of("你好 &newtale") == ""
    assert command_of("&newtale 你好") != "newtale"
    assert is_reset_command("&newtale 你好") is False


def test_plain_text_is_not_a_command():
    assert command_of("你好") == ""
    assert command_of("") == ""
    assert command_of("&") == ""
    assert is_reset_command("newtale") is False   # 没前缀不算


def test_other_commands_are_not_reset():
    assert command_of("&help") == "help"
    assert is_reset_command("&help") is False


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
                       *, session_type="", owner="", images=None, quoted="",
                       quoted_images=None):
        self.calls.append((user_question, list(history or []), session_id))
        return Reply(session_id=session_id, messages=["好呀~"])


SID = "qq:p20002000"      # QQ 私聊
GSID = "qq:g30003000"     # QQ 群聊


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "s.db").open()
    yield s
    s.close()


@pytest.fixture
def wiring(store, clean_bus):
    import main

    adapter = FakeAdapter(bus=clean_bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    return main, adapter, Router(reg), FakeBot(), store, clean_bus


def qq_msg(content, *, session_id=SID, session_type="private", addressed=None,
           **extra):
    return Message(
        id="m1", platform="fake", session_id=session_id, owner="u1",
        direction="in", role="user", content=content,
        session_type=session_type, addressed=addressed, **extra,
    )


async def test_private_reset_clears_and_replies(wiring):
    """QQ 私聊 &newtale：清掉历史、回执给用户、不调模型。"""
    main, adapter, router, bot, store, bus = wiring
    store.ensure_session(SID, platform="qq", kind="private", owner="u1")
    store.append(SID, "user", "旧事一")
    store.append(SID, "assistant", "旧事二")

    reply = await main.handle_message(
        qq_msg("&newtale"), router=router, store=store, bot=bot, bus=bus,
    )
    assert store.count(SID) == 0              # 清干净
    assert bot.calls == []                    # 没调模型
    assert reply is not None and reply.messages  # 有回执
    assert adapter.sent and adapter.sent[-1].messages == reply.messages
    assert "2" in reply.messages[0]           # 回执里报了条数


async def test_reset_leaves_no_trace(wiring):
    """命令本身与回执都不落库——重置完历史必须是干净的（下次对话无前情）。"""
    main, adapter, router, bot, store, bus = wiring
    store.ensure_session(SID, platform="qq", kind="private", owner="u1")
    store.append(SID, "user", "旧事")

    await main.handle_message(
        qq_msg("&newtale"), router=router, store=store, bot=bot, bus=bus,
    )
    assert store.count(SID) == 0              # 既没留命令，也没留回执


async def test_reset_on_empty_session_is_graceful(wiring):
    """没有历史时重置：回执换个说法，不报错。"""
    main, adapter, router, bot, store, bus = wiring
    store.ensure_session(SID, platform="qq", kind="private", owner="u1")

    reply = await main.handle_message(
        qq_msg("/newtale"), router=router, store=store, bot=bot, bus=bus,
    )
    assert reply is not None and reply.messages
    assert bot.calls == []


async def test_group_reset_requires_wake(wiring):
    """群聊没叫它 → 命令也不给重置（只落库，跟普通未唤醒消息一致）。

    否则群里任何人丢一句命令都能抹掉塔利的上下文。
    """
    from core.wake import WakePolicy

    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")
    store.ensure_session(GSID, platform="qq", kind="group", owner="u1")
    store.append(GSID, "user", "群里的旧事")

    reply = await main.handle_message(
        qq_msg("&newtale", session_id=GSID, session_type="group"),
        router=router, store=store, bot=bot, bus=bus, wake=policy,
    )
    assert reply is None
    assert store.count(GSID) == 2   # 旧事 + 这条命令（按未唤醒落库）
    assert bot.calls == []


async def test_group_reset_works_when_addressed(wiring):
    """群里被 @ 了（addressed=True）→ 命令生效。"""
    from core.wake import WakePolicy

    main, adapter, router, bot, store, bus = wiring
    policy = WakePolicy(words=("塔利",), scope="group")
    store.ensure_session(GSID, platform="qq", kind="group", owner="u1")
    store.append(GSID, "user", "群里的旧事")

    reply = await main.handle_message(
        qq_msg("&newtale", session_id=GSID, session_type="group", addressed=True),
        router=router, store=store, bot=bot, bus=bus, wake=policy,
    )
    assert store.count(GSID) == 0
    assert reply is not None and adapter.sent


async def test_image_attached_not_treated_as_command(wiring):
    """带图的 "&newtale" 不当命令——那是"发图说话"，跟 WebUI 前端判定一致。"""
    main, adapter, router, bot, store, bus = wiring
    store.ensure_session(SID, platform="qq", kind="private", owner="u1")

    await main.handle_message(
        qq_msg("&newtale", images=["a.png"]),
        router=router, store=store, bot=bot, bus=bus,
    )
    assert bot.calls, "带图消息应走模型，而不是被当命令吞掉"


async def test_non_reset_command_goes_to_model(wiring):
    """后端只认 newtale；&help 之类不归它管（WebUI 前端另有快路径）。"""
    main, adapter, router, bot, store, bus = wiring
    store.ensure_session(SID, platform="qq", kind="private", owner="u1")

    await main.handle_message(
        qq_msg("&help"), router=router, store=store, bot=bot, bus=bus,
    )
    assert bot.calls, "&help 不该被后端吞掉"


# ================= 前后端一致性 =================


def test_frontend_and_backend_agree_on_command_name():
    """两处并存（前端快路径 + 后端正路），命令名必须一致——从页面提取验证。

    漂移的后果：WebUI 里能重置、QQ 里不能（或反过来）——正是这次修的坑。
    """
    from core.adapter.web.adapter import WEBUI_DIR

    page = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
    m = re.search(r'RESET_CMD\s*=\s*"([^"]+)"', page)
    assert m, "前端 index.html 里找不到 RESET_CMD"
    assert is_reset_command(m.group(1)), (
        f"前端命令 {m.group(1)!r} 后端不认——两处漂移了"
    )
