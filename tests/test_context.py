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

    # 给了 session_type 才出现（渲染成中文，不给模型看 group/private 这种英文值）
    blocks2 = make_assembler().assemble("chat", SessionContext(session_type="group"))
    assert "会话类型：群聊" in blocks2[0].content


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
    # 历史窗口（__init__ 里从 config 读，这里给默认值）
    bot.history_keep_messages = 10
    bot.history_lookback_extra = 5
    return bot


def test_system_message_stays_byte_stable():
    """动态块绝不污染 system——system 必须等于 persona 提示词原文。"""
    bot = make_bot()
    msgs = bot.assemble_messages("你好")
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == bot.persona.build_system_prompt()
    # 精确锚「动态块的实际渲染产物」（含注入的固定时间），而不是裸词
    # 「system_reminder」——静态提示词里本就该出现该标签（base.md 要教模型
    # 怎么对待它），那是指令，不是泄漏。真正要防的是渲染出来的 reminder
    # 整块进 system。
    reminder = bot.context.render_reminder(bot.context.assemble("chat"))
    assert reminder  # 前提：确实渲染出了东西，否则下面断言是空谈
    assert reminder not in msgs[0]["content"]


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
    assert body[0]["content"] == "你好"  # 历史 user 原样
    # 历史 assistant 会被补回 <msg>（见 test_history_assistant_gets_msg_tags_restored），
    # 本条只关心「没有 reminder 漏进历史」——正文本身（去掉标签后）不变即可。
    assert body[1]["content"] == "<msg>哟，老板来啦~</msg>"
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


# ---------- 历史进模型时补回 <msg>（真实模型分条失效的修复） ----------


def test_history_assistant_gets_msg_tags_restored():
    """历史里的 assistant 裸文本要补回 <msg> 再喂模型。

    存储按原文存纯文本（记忆/WebUI 都用它），但模型学格式看的是前文——
    裸文本历史会让模型跟着不写 <msg>，QQ 分条就失效（实测裸文本 11/15 轮
    丢契约，补回标签 0/15）。这条钉死「存储形态 ≠ 模型形态」。
    """
    bot = make_bot()
    history = [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在呢~\n\n咋了"},
    ]
    msgs = bot.assemble_messages("在忙啥", history)
    body = [m for m in msgs if m["role"] != "system"]
    # 历史 user 原样，不被打标签
    assert body[0]["content"] == "在吗"
    # 历史 assistant 的两段被分别包上 <msg>
    assert body[1]["content"] == "<msg>在呢~</msg>\n\n<msg>咋了</msg>"
    # 最新提问是用户的话，也不带 <msg>
    assert "<msg>" not in body[-1]["content"]


def test_history_restore_does_not_mutate_caller_list():
    """补标签是新造 dict，绝不就地改动调用方的 history（配套 persist=False）。"""
    bot = make_bot()
    history = [{"role": "assistant", "content": "在呢~\n\n咋了"}]
    snapshot = [dict(m) for m in history]
    bot.assemble_messages("在吗", history)
    assert history == snapshot, "history 被就地改了——补标签必须造新 dict"


def test_history_already_tagged_is_left_alone():
    """已带 <msg> 的历史是幂等的，不再重复包一层。"""
    bot = make_bot()
    history = [{"role": "assistant", "content": "<msg>早</msg>"}]
    msgs = bot.assemble_messages("早", history)
    body = [m for m in msgs if m["role"] != "system"]
    assert body[0]["content"] == "<msg>早</msg>"


# ---------- 滑动窗口：最新 N 条 + 往前多带 M 条 ----------


def test_window_keeps_newest_plus_extra():
    """保留最新 N 条，再往前多带 M 条——窗口外的旧消息不全丢，边界不硬切。"""
    bot = make_bot()
    bot.history_keep_messages = 4
    bot.history_lookback_extra = 2
    # 6 轮 = 12 条，应留 4+2 = 6 条（最近 3 轮的 6 条消息）
    history = []
    for i in range(6):
        history.append({"role": "user", "content": f"问{i}"})
        history.append({"role": "assistant", "content": f"答{i}"})
    msgs = bot.assemble_messages("新问题", history)
    body = [m for m in msgs if m["role"] != "system"]
    assert [m["content"] for m in body[:-1]] == [
        "问3", "<msg>答3</msg>", "问4", "<msg>答4</msg>", "问5", "<msg>答5</msg>",
    ]
    assert body[-1]["content"].endswith("新问题")


def test_window_extra_zero_is_plain_sliding():
    """M=0 就是纯滑动窗口：只留最新 N 条。"""
    bot = make_bot()
    bot.history_keep_messages = 3
    bot.history_lookback_extra = 0
    history = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    msgs = bot.assemble_messages("问", history)
    body = [m for m in msgs if m["role"] != "system"]
    assert [m["content"] for m in body[:-1]] == ["m7", "m8", "m9"]


def test_window_under_limit_keeps_everything():
    """没超窗口就不裁——短会话原样带上。"""
    bot = make_bot()
    bot.history_keep_messages = 10
    bot.history_lookback_extra = 5
    history = [
        {"role": "user", "content": "问1"},
        {"role": "assistant", "content": "答1"},
    ]
    msgs = bot.assemble_messages("问2", history)
    body = [m for m in msgs if m["role"] != "system"]
    assert [m["content"] for m in body[:-1]] == ["问1", "<msg>答1</msg>"]


def test_window_zero_zero_means_unlimited():
    """两个值都 0 = 不限（调试用）：历史全带上。"""
    bot = make_bot()
    bot.history_keep_messages = 0
    bot.history_lookback_extra = 0
    history = [{"role": "x", "content": f"m{i}"} for i in range(30)]
    msgs = bot.assemble_messages("问", history)
    body = [m for m in msgs if m["role"] != "system"]
    assert len(body) == 31  # 30 条历史 + 1 条最新提问


def test_window_does_not_mutate_caller_history():
    """窗口只读切片，不改调用方的 list。"""
    bot = make_bot()
    bot.history_keep_messages = 2
    bot.history_lookback_extra = 1
    history = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    snapshot = [dict(m) for m in history]
    bot.assemble_messages("问", history)
    assert history == snapshot
