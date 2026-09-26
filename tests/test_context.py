"""M0-03 补测：ContextAssembler 装配动态块。

对应设计文档 §十二 / §十九-19：动态块拼到最新 user 消息头部、
<system_reminder> 包裹、persist=False（不写回历史）。

时钟用注入的固定时间——装配结果完全确定，不 mock 时间库、不用 sleep。
"""

import os
import sys
from datetime import datetime

# 让测试能找到 src 下的代码（与 test_config.py 保持一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.llm.context import (
    ContextAssembler,
    ContextBlock,
    SessionContext,
)

FIXED = datetime(2026, 9, 26, 16, 30)  # 测试用固定时刻（星期六）


def make_assembler() -> ContextAssembler:
    return ContextAssembler(clock=lambda: FIXED)


# ---------- 装配本身 ----------


def test_assemble_chat_includes_env_block():
    """M0 的 chat 清单只有环境块，且带当前时间。"""
    blocks = make_assembler().assemble("chat")
    assert [b.name for b in blocks] == ["chat_env"]
    assert "2026-09-26 16:30" in blocks[0].content
    assert "星期六" in blocks[0].content


def test_assemble_unknown_llm_raises():
    """未声明 spec 的 LLM 直接报错，不静默返回空。"""
    try:
        make_assembler().assemble("memory")  # M1 才声明
        assert False, "应该抛 ValueError"
    except ValueError:
        pass


def test_blocks_are_dynamic_kind():
    """M0 的 chat_env 是动态块（会变的时间不能进 system）。"""
    for b in make_assembler().assemble("chat"):
        assert b.kind == "dynamic"


def test_blocks_sorted_by_order():
    """多源时按 order 排序。"""
    blocks = make_assembler().assemble("chat")
    orders = [b.order for b in blocks]
    assert orders == sorted(orders)


def test_session_fields_optional():
    """M0 无适配器：不给 session 也能装配（只有时间，无噪音）。"""
    blocks = make_assembler().assemble("chat", SessionContext())
    assert len(blocks) == 1
    assert "会话类型" not in blocks[0].content

    # 给了 session_type 才出现
    blocks2 = make_assembler().assemble("chat", SessionContext(session_type="group"))
    assert "会话类型：group" in blocks2[0].content


# ---------- 渲染 <system_reminder> ----------


def test_render_wraps_in_system_reminder():
    asm = make_assembler()
    text = asm.render_reminder(asm.assemble("chat"))
    assert text.startswith("<system_reminder>")
    assert text.endswith("</system_reminder>")
    assert "当前时间" in text


def test_render_empty_returns_empty_string():
    """没有动态块 → 空串，调用方据此不拼任何东西（不留多余空行）。"""
    assert make_assembler().render_reminder([]) == ""


def test_render_ignores_static_and_blank_blocks():
    """静态块不进 reminder；内容全为空白也不进。"""
    blocks = [
        ContextBlock(name="persona", kind="static", order=1, content="人格"),
        ContextBlock(name="blank", kind="dynamic", order=2, content="   \n  "),
    ]
    assert make_assembler().render_reminder(blocks) == ""


# ---------- 接进 ChatLLM：静态/动态分离 + persist=False ----------


def make_bot():
    """不跑 ChatLLM.__init__（会读配置、建客户端），只装装配所需部件。"""
    from core.llm.chat_llm import ChatLLM
    from core.llm.persona_llm.base import Persona

    bot = ChatLLM.__new__(ChatLLM)
    bot.persona = Persona()
    bot.context = make_assembler()
    # 历史裁剪阈值（__init__ 里从 config 读，这里给默认值）
    bot.history_max_turns = 40
    bot.history_trim_to = 10
    return bot


def test_system_message_stays_byte_stable():
    """动态块绝不污染 system——system 必须等于 persona 提示词原文。"""
    bot = make_bot()
    msgs = bot.assemble_messages("你好")
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == bot.persona.build_system_prompt()
    assert "system_reminder" not in msgs[0]["content"]


def test_reminder_goes_to_latest_user_message_head():
    """reminder 贴在最新 user 消息**头部**，原文在它后面。"""
    bot = make_bot()
    msgs = bot.assemble_messages("现在几点了？")
    last = msgs[-1]
    assert last["role"] == "user"
    assert last["content"].startswith("<system_reminder>")
    assert last["content"].endswith("现在几点了？")


def test_reminder_not_in_history_messages():
    """历史消息不被注入 reminder（只有最新那条 user 带）。"""
    bot = make_bot()
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "哟，老板来啦~"},
    ]
    msgs = bot.assemble_messages("今天星期几？", history)
    body = [m for m in msgs if m["role"] != "system"]
    assert body[0]["content"] == "你好"  # 历史原样
    assert body[1]["content"] == "哟，老板来啦~"
    assert "system_reminder" not in body[0]["content"]
    assert "system_reminder" not in body[1]["content"]
    assert "system_reminder" in body[-1]["content"]


def test_history_list_not_mutated():
    """persist=False 的守卫：装配不许改动调用方的 history。"""
    bot = make_bot()
    history = [{"role": "user", "content": "你好"}]
    snapshot = [dict(m) for m in history]

    bot.assemble_messages("再见", history)
    assert history == snapshot, "history 被就地修改了——动态块泄漏进了历史"

    # 连续两轮装配，历史也不该长出新消息
    bot.assemble_messages("还在吗", history)
    assert history == snapshot


def test_repeated_calls_produce_identical_reminder():
    """同一时刻重复装配结果一致（纯函数性质，可安全缓存）。"""
    bot = make_bot()
    a = bot.assemble_messages("一")[-1]["content"]
    b = bot.assemble_messages("一")[-1]["content"]
    assert a == b
