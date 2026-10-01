"""会话感知：模型必须知道「自己在哪种会话里说话」。

设计文档 §18.3（sessions.kind / SessionContext）/ §十二（感知清单）/
§十八-4（记忆按 owner 隔离）。

**为什么单独一个文件**：M0 的模型是"会话盲"的——装配时 session 从没传进去，
所以群里说"你好"和私聊说"你好"得到的回复完全一样，模型不知道有没有别人在场。
这是 M0-11 适配器落地后留下的一根断线，这里补上。

同时修一个真 bug：ensure_session 没传 kind，导致群聊会话在库里被记成 private。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.adapter.base import Message
from core.llm.chat_llm import ChatLLM
from core.llm.context import ContextAssembler, SessionContext
from core.llm.persona_llm.base import Persona
from core.session.store import SessionStore


def make_bot():
    bot = ChatLLM.__new__(ChatLLM)
    bot.persona = Persona()
    bot.context = ContextAssembler()
    bot.history_max_turns = 40
    bot.history_trim_to = 10
    return bot


# ---------- Message 带上会话类型 ----------


def test_message_has_session_type_defaulting_private():
    """默认 private——WebUI 是 1:1 会话，不写就是私聊。"""
    m = Message(id="1", platform="websocket", session_id="web:x", owner="local",
                direction="in", role="user", content="hi")
    assert m.session_type == "private"


# ---------- 装配把会话信息带给模型 ----------


def test_group_session_shows_as_group_in_reminder():
    bot = make_bot()
    session = SessionContext(session_id="qq:g1", session_type="group", owner="u1")
    msgs = bot.assemble_messages("你好", None, session=session)
    assert "群聊" in msgs[-1]["content"]


def test_private_session_shows_as_private_in_reminder():
    bot = make_bot()
    session = SessionContext(session_id="qq:p1", session_type="private", owner="u1")
    msgs = bot.assemble_messages("你好", None, session=session)
    assert "私聊" in msgs[-1]["content"]


def test_no_session_means_no_session_line():
    """不给 session 就不该冒出会话行——保持命令行/测试场景干净。"""
    bot = make_bot()
    msgs = bot.assemble_messages("你好", None)
    assert "会话类型" not in msgs[-1]["content"]


def test_session_info_does_not_leak_raw_ids_into_prompt():
    """给模型看的是中文类型，不是群号/QQ 号。

    理由：LLM 的 tokenizer 对长数字串不友好，很难原样复述。M0 不需要它
    记住任何 ID（跨会话寻址是 M3 的事，届时走 §19-5 的名称寻址）。
    """
    bot = make_bot()
    session = SessionContext(session_id="qq:g30003000", session_type="group",
                             owner="20002000")
    msgs = bot.assemble_messages("你好", None, session=session)
    body = msgs[-1]["content"]
    assert "30003000" not in body
    assert "20002000" not in body


def test_session_info_goes_to_dynamic_block_not_system():
    """会话类型是动态信息——绝不进 system（§18.5 硬规则 5：静态字节稳定）。"""
    bot = make_bot()
    session = SessionContext(session_id="qq:g1", session_type="group", owner="u1")
    msgs = bot.assemble_messages("你好", None, session=session)
    assert msgs[0]["role"] == "system"
    # 用「会话类型：群聊」这个动态块**精确产物**做锚，而不是裸词「群聊」：
    # 静态提示词里本就该出现「群聊」（base.md 有群聊场景的行为指引），那是
    # 行为描述，不是会话类型泄漏。真正要防的是这行按会话生成的信息进 system。
    assert "会话类型：群聊" not in msgs[0]["content"]
    assert msgs[0]["content"] == bot.persona.build_system_prompt()


# ---------- ensure_session 的 kind 不再永远是 private（真 bug） ----------


def test_group_message_records_kind_group(tmp_path):
    """群聊进来的消息，库里的 sessions.kind 必须是 group。

    此前 handle_message 调 ensure_session 没传 kind，默认 private——
    群聊会话被记成私聊，与 §18.3 的 sessions.kind 定义不符。
    """
    import asyncio

    import main
    from core.adapter.base import AdapterBase, Reply
    from core.adapter.registry import AdapterRegistry
    from core.adapter.router import Router
    from core.bus.event_bus import EventBus

    class FakeAdapter(AdapterBase):
        name = "qq"

        def normalize(self, raw):
            raise NotImplementedError

        async def send(self, reply):
            pass

        async def start(self):
            pass

    class FakeBot:
        async def run_loop(self, q, history=None, session_id=""):
            return Reply(session_id=session_id, messages=["好"])

    store = SessionStore(tmp_path / "s.db").open()
    bus = EventBus()
    bus.reset()
    reg = AdapterRegistry()
    reg.register(FakeAdapter(bus=bus))

    msg = Message(id="1", platform="qq", session_id="qq:g30003000", owner="u1",
                  direction="in", role="user", content="大家好啊", session_type="group")
    asyncio.run(main.handle_message(msg, router=Router(reg), store=store,
                                    bot=FakeBot(), bus=bus))

    row = [s for s in store.sessions() if s["id"] == "qq:g30003000"][0]
    assert row["kind"] == "group"
    store.close()


def test_private_message_records_kind_private(tmp_path):
    import asyncio

    import main
    from core.adapter.base import AdapterBase, Reply
    from core.adapter.registry import AdapterRegistry
    from core.adapter.router import Router
    from core.bus.event_bus import EventBus

    class FakeAdapter(AdapterBase):
        name = "qq"

        def normalize(self, raw):
            raise NotImplementedError

        async def send(self, reply):
            pass

        async def start(self):
            pass

    class FakeBot:
        async def run_loop(self, q, history=None, session_id=""):
            return Reply(session_id=session_id, messages=["好"])

    store = SessionStore(tmp_path / "s.db").open()
    bus = EventBus()
    bus.reset()
    reg = AdapterRegistry()
    reg.register(FakeAdapter(bus=bus))

    msg = Message(id="1", platform="qq", session_id="qq:p1", owner="u1",
                  direction="in", role="user", content="你好", session_type="private")
    asyncio.run(main.handle_message(msg, router=Router(reg), store=store,
                                    bot=FakeBot(), bus=bus))

    row = [s for s in store.sessions() if s["id"] == "qq:p1"][0]
    assert row["kind"] == "private"
    store.close()


# ---------- 端到端：模型真的看到了会话类型 ----------


def test_run_loop_passes_session_into_assembly():
    """run_loop 必须把 session 传进装配——这是那根断线本身。"""
    from types import SimpleNamespace

    captured = {}

    class FakeClient:
        def __init__(self):
            outer = self

            class C:
                async def create(self, **kw):
                    captured["messages"] = kw["messages"]
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(
                            content="<msg>好</msg>", tool_calls=None))]
                    )

            self.chat = SimpleNamespace(completions=C())

    from core.executor import ToolExecutor
    from core.plugin.guard import PermissionGuard
    from core.plugin.registry import PluginRegistry
    from core.xml_parser import XmlParser

    reg = PluginRegistry()
    reg.reset()
    reg.scan()
    bot = ChatLLM.__new__(ChatLLM)
    bot.model = "fake"
    bot.persona = Persona()
    bot.context = ContextAssembler()
    bot.parser = XmlParser()
    bot.registry = reg
    bot.executor = ToolExecutor(reg, PermissionGuard(reg))
    bot.max_agent_steps = 3
    bot.history_max_turns = 40
    bot.history_trim_to = 10
    bot.fallback_text = "（兜底）"
    bot.client = FakeClient()

    import asyncio

    asyncio.run(bot.run_loop("你好", None, "qq:g123", session_type="group"))
    assert "群聊" in captured["messages"][-1]["content"]
    reg.reset()
