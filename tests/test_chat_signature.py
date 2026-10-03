"""M0-11 欠账：`chat()` 签名对齐 §22 类名表的 `chat(session_id, text)`。

原签名 `chat(text, history=None, session_id="")` 与文档不一致。现在有真适配器
了，session_id 是一等公民（决定回复发回去哪个会话），理应先出现。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.llm.chat_llm import ChatLLM


def assistant(content=None, calls=None):
    return SimpleNamespace(content=content, tool_calls=calls)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.requests.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=outer.script.pop(0))])

        self.chat = SimpleNamespace(completions=_Completions())


def make_bot(script):
    from core.executor import ToolExecutor
    from core.llm.context import ContextAssembler
    from core.llm.persona_llm.base import Persona
    from core.plugin.guard import PermissionGuard
    from core.plugin.registry import PluginRegistry
    from core.xml_parser import XmlParser

    reg = PluginRegistry()
    reg.reset()
    reg.scan()
    bot = ChatLLM.__new__(ChatLLM)
    bot.model = "fake-model"
    bot.persona = Persona()
    bot.context = ContextAssembler()
    bot.parser = XmlParser()
    bot.registry = reg
    bot.executor = ToolExecutor(reg, PermissionGuard(reg))
    bot.max_agent_steps = 3
    bot.history_keep_messages = 10
    bot.history_lookback_extra = 5
    bot.fallback_text = "（兜底）"
    bot.client = FakeClient(script)
    return bot


def test_chat_takes_session_id_first():
    """§22：chat(session_id, text)。session_id 决定回复发回哪个会话，先给。"""
    bot = make_bot([assistant("<msg>好</msg>")])
    reply = bot.chat("cli:local", "你好")
    assert reply.session_id == "cli:local"


def test_chat_accepts_history_as_third_positional():
    bot = make_bot([assistant("<msg>好</msg>")])
    history = [{"role": "user", "content": "早"}]
    reply = bot.chat("cli:local", "现在几点", history)
    sent = bot.client.requests[0]["messages"]
    assert {"role": "user", "content": "早"} in sent
    assert reply.session_id == "cli:local"


def test_chat_session_id_defaults_empty():
    """不传 session_id 也能用（旧调用点/测试的兼容口）。"""
    bot = make_bot([assistant("<msg>好</msg>")])
    assert bot.chat("", "你好").session_id == ""
