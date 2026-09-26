"""M0-08 验收：ChatLLM FC 循环。

设计文档 §四 / §18.1 第 6 步 / §19-2 / §19-3 / §18.3。

这里用假客户端把模型「脚本化」，因为熔断、撞轮次上限、模型一言不发
这些路径很难靠真实模型稳定复现——但它们恰恰是最需要守住的分支。
真实模型的端到端验证另外做（见 README / 提交记录）。
"""

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.llm.chat_llm import ChatLLM
from core.plugin.guard import PermissionGuard
from core.plugin.registry import PluginRegistry


# ---------- 假客户端：把模型输出写成脚本，按轮次吐出 ----------


def tool_call(id_, name, arguments):
    return SimpleNamespace(
        id=id_, type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def tool_messages(request) -> list[dict]:
    """从一次请求的 messages 里挑出回喂的 tool 消息。

    循环会把 SDK 的 assistant message 对象原样 append 进 messages
    （SDK 接受自己的对象），所以这里不能假设每项都是 dict。
    """
    return [m for m in request["messages"]
            if isinstance(m, dict) and m.get("role") == "tool"]


def assistant(content=None, calls=None):
    return SimpleNamespace(content=content, tool_calls=calls)


class FakeClient:
    """按脚本逐轮返回 assistant message，并记下每次请求。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.requests.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=outer.script.pop(0))])

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture(autouse=True)
def registry():
    reg = PluginRegistry()
    reg.reset()
    reg.scan()
    yield reg
    reg.reset()


def make_bot(script, **overrides):
    """做一个不跑 __init__ 的 ChatLLM（那会读配置、建真客户端）。"""
    from core.executor import ToolExecutor
    from core.llm.context import ContextAssembler
    from core.llm.persona_llm.base import Persona
    from core.xml_parser import XmlParser

    reg = PluginRegistry()
    bot = ChatLLM.__new__(ChatLLM)
    bot.model = "fake-model"
    bot.persona = Persona()
    bot.context = ContextAssembler()
    bot.parser = XmlParser()
    bot.registry = reg
    bot.executor = ToolExecutor(reg, PermissionGuard(reg))
    bot.max_agent_steps = 3
    bot.history_max_turns = 40
    bot.history_trim_to = 10
    bot.fallback_text = "（兜底）"
    bot.client = FakeClient(script)
    for k, v in overrides.items():
        setattr(bot, k, v)
    return bot


# ---------- 正常路径 ----------


def test_plain_reply_without_tools():
    """模型直接说话，不调工具。"""
    bot = make_bot([assistant("<msg>你好呀~</msg>")])
    reply = bot.chat("你好")
    assert reply.messages == ["你好呀~"]
    assert reply.tool_calls_made == 0
    assert reply.stop_reason == "ok"


def test_multiple_msg_in_one_round():
    bot = make_bot([assistant("<msg>第一句</msg>\n\n<msg>第二句</msg>")])
    reply = bot.chat("说吧")
    assert reply.messages == ["第一句", "第二句"]


def test_tool_call_then_reply():
    """调一次工具，拿到结果后再说人话；两轮的话都收进 messages。"""
    bot = make_bot([
        assistant("<msg>我查查~</msg>", [tool_call("c1", "ping", "{}")]),
        assistant("<msg>pong，通了</msg>"),
    ])
    reply = bot.chat("测试一下连通性")
    assert reply.messages == ["我查查~", "pong，通了"]
    assert reply.tool_calls_made == 1
    assert reply.stop_reason == "ok"


def test_tool_result_is_fed_back():
    """工具结果必须真的回喂给模型——这是「结果反哺」的核心。"""
    bot = make_bot([
        assistant(None, [tool_call("c1", "ping", "{}")]),
        assistant("<msg>收到</msg>"),
    ])
    bot.chat("测试")
    second_request = bot.client.requests[1]
    tool_msgs = tool_messages(second_request)
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"]) == {"result": "pong"}
    assert tool_msgs[0]["tool_call_id"] == "c1"


def test_tools_are_passed_to_api():
    bot = make_bot([assistant("<msg>hi</msg>")])
    bot.chat("hi")
    sent = bot.client.requests[0]["tools"]
    names = {t["function"]["name"] for t in sent}
    assert {"ping", "time_query"} <= names


def test_tool_error_is_fed_back_for_correction():
    """工具失败时把原因回喂，模型才能自己纠正。"""
    bot = make_bot([
        assistant(None, [tool_call("c1", "不存在的工具", "{}")]),
        assistant("<msg>抱歉，我没这个能力</msg>"),
    ])
    reply = bot.chat("干点什么")
    tool_msg = tool_messages(bot.client.requests[1])[0]
    payload = json.loads(tool_msg["content"])
    assert "error" in payload
    assert reply.tool_calls_made == 1


def test_malformed_arguments_reported_not_crash():
    """模型给出非法 JSON 参数：不该崩，要把原因回喂。"""
    bot = make_bot([
        assistant(None, [tool_call("c1", "ping", "{不是JSON")]),
        assistant("<msg>我再试试</msg>"),
    ])
    reply = bot.chat("测试")
    tool_msg = tool_messages(bot.client.requests[1])[0]
    assert "合法 JSON" in json.loads(tool_msg["content"])["error"]
    assert reply.stop_reason == "ok"


# ---------- 熔断（§19-2 / §19-3） ----------


def test_same_tool_same_args_cuts_loop():
    """同工具同参数连续两轮 → 熔断，不再执行第二次。"""
    bot = make_bot([
        assistant(None, [tool_call("c1", "ping", "{}")]),
        assistant(None, [tool_call("c2", "ping", "{}")]),  # 与上轮完全一致
        assistant("<msg>不该走到这里</msg>"),
    ])
    reply = bot.chat("测试")
    assert reply.stop_reason == "loop_cut"
    assert reply.tool_calls_made == 1  # 第二次没执行
    assert len(bot.client.requests) == 2


def test_signature_ignores_whitespace_and_key_order():
    """参数写法不同但语义相同，仍应判为同一循环（否则熔断会漏判）。"""
    bot = make_bot([
        assistant(None, [tool_call("c1", "time_query", '{"tz": "UTC"}')]),
        assistant(None, [tool_call("c2", "time_query", '{ "tz" : "UTC" }')]),
    ])
    reply = bot.chat("几点了")
    assert reply.stop_reason == "loop_cut"


def test_different_args_do_not_cut():
    """不同参数不算同一循环（§19-3），由轮次上限兜底。"""
    bot = make_bot([
        assistant(None, [tool_call("c1", "time_query", '{"tz": "UTC"}')]),
        assistant(None, [tool_call("c2", "time_query", '{"tz": "Asia/Tokyo"}')]),
        assistant(None, [tool_call("c3", "time_query", '{"tz": "Asia/Seoul"}')]),
    ])
    reply = bot.chat("各地时间")
    assert reply.stop_reason == "max_steps"
    assert reply.tool_calls_made == 3


# ---------- 轮次上限 ----------


def test_respects_max_agent_steps():
    script = [assistant(None, [tool_call(f"c{i}", "time_query", f'{{"tz": "UTC{i}"}}')])
              for i in range(5)]
    bot = make_bot(script, max_agent_steps=3)
    reply = bot.chat("一直查")
    assert len(bot.client.requests) == 3
    assert reply.stop_reason == "max_steps"


# ---------- 兜底 ----------


def test_no_message_at_all_uses_fallback_text():
    """模型只调工具、一句话没说 → 发兜底文案，不能让用户面对空白（§19-2）。"""
    bot = make_bot([
        assistant(None, [tool_call("c1", "ping", "{}")]),
        assistant(None),
    ])
    reply = bot.chat("测试")
    assert reply.messages == ["（兜底）"]
    assert reply.stop_reason == "parse_fallback"


def test_plain_text_without_msg_still_shown():
    """模型没打标签但说了话 → 原文照给（XmlParser 的兜底），不是空白。"""
    bot = make_bot([assistant("我没打标签就直接说了")])
    reply = bot.chat("在吗")
    assert reply.messages == ["我没打标签就直接说了"]


# ---------- 上下文与历史的配合 ----------


def test_dynamic_reminder_not_in_history():
    """动态块只进最新 user 消息，且不改动调用方的 history（persist=False）。"""
    bot = make_bot([assistant("<msg>好</msg>")])
    history = [{"role": "user", "content": "早"}]
    snapshot = [dict(m) for m in history]
    bot.chat("现在几点", history)
    assert history == snapshot
    sent = bot.client.requests[0]["messages"]
    assert "system_reminder" in sent[-1]["content"]
    assert "system_reminder" not in sent[1]["content"]


def test_session_id_carried_into_reply():
    bot = make_bot([assistant("<msg>好</msg>")])
    assert bot.chat("hi", session_id="cli").session_id == "cli"


def test_reply_is_reply_object():
    from core.adapter.base import Reply
    bot = make_bot([assistant("<msg>好</msg>")])
    assert isinstance(bot.chat("hi"), Reply)
