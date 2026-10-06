"""聊天规则：人设内容之外，用户可写的「全局 / 私聊 / 群聊」三组规则。

设计文档 §十四（v4.14「人设内容 + 聊天规则」）/ §十二（静态块缓存）/
§十九-22（字节稳定守卫）。拍板（2026-10-06）：纯增量——写了注入、
留空/不存在不注入、无内置默认；三文件零解析；场景规则压 system 最尾。

这个文件同时守三样东西：
- 注入选择：哪种会话拿哪份规则；
- 字节守卫：规则缺席时拼出的字节与"没有这功能"逐字节一致（不留空行噪音）；
- 缓存守卫：按类型分叉后每型各自字节稳定、跨类型共享前缀（§19-22 扩展）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.adapter.base import Message
from core.llm.chat_llm import ChatLLM
from core.llm.context import ContextAssembler, SessionContext
from core.llm.persona_llm.base import Persona, _has_real_content, ensure_user_rules

# 三个独特标记：断言认它们，绝不与本机框架文件撞车
GLOBAL_RULE = "# 全局\n\nRULES-GLOBAL 全局测试规则。\n"
PRIVATE_RULE = "# 私聊\n\nRULES-PRIVATE 私聊测试规则。\n"
GROUP_RULE = "# 群聊\n\nRULES-GROUP 群聊测试规则。\n"


def make_persona(tmp_path):
    """密闭构造：人设/规则都指向 tmp，绝不读本机 data/config。"""
    return Persona(persona_path=tmp_path / "persona.md", rules_dir=tmp_path / "rules")


def write_rule(rules_dir, name, text):
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / f"{name}.md").write_text(text, encoding="utf-8")


def legacy_bytes(p):
    """旧版拼装（base + chat + persona，无规则层）——字节守卫的参照物。"""
    return (
        p.base_path.read_text(encoding="utf-8") + "\n\n"
        + p.chat_path.read_text(encoding="utf-8") + "\n\n"
        + p._persona_source().read_text(encoding="utf-8")
    )


# ---------- A 注入选择：哪种会话拿哪份规则 ----------


def test_global_rules_injected_for_all_session_types(tmp_path):
    write_rule(tmp_path / "rules", "global", GLOBAL_RULE)
    p = make_persona(tmp_path)
    for st in ("", "private", "group"):
        assert "RULES-GLOBAL" in p.build_system_prompt(st)


def test_private_rules_only_in_private(tmp_path):
    write_rule(tmp_path / "rules", "private", PRIVATE_RULE)
    p = make_persona(tmp_path)
    assert "RULES-PRIVATE" in p.build_system_prompt("private")
    assert "RULES-PRIVATE" not in p.build_system_prompt("group")
    assert "RULES-PRIVATE" not in p.build_system_prompt("")


def test_group_rules_only_in_group(tmp_path):
    write_rule(tmp_path / "rules", "group", GROUP_RULE)
    p = make_persona(tmp_path)
    assert "RULES-GROUP" in p.build_system_prompt("group")
    assert "RULES-GROUP" not in p.build_system_prompt("private")
    assert "RULES-GROUP" not in p.build_system_prompt("")


def test_empty_session_type_gets_no_scene_rules(tmp_path):
    """"" = 调用方没给会话身份（CLI/单测）——只注入全局规则。"""
    write_rule(tmp_path / "rules", "global", GLOBAL_RULE)
    write_rule(tmp_path / "rules", "private", PRIVATE_RULE)
    write_rule(tmp_path / "rules", "group", GROUP_RULE)
    prompt = make_persona(tmp_path).build_system_prompt("")
    assert "RULES-GLOBAL" in prompt
    assert "RULES-PRIVATE" not in prompt
    assert "RULES-GROUP" not in prompt


def test_unknown_session_type_gets_no_scene_rules(tmp_path):
    """未知类型不误读 private.md——显式查表，不猜。"""
    write_rule(tmp_path / "rules", "private", PRIVATE_RULE)
    p = make_persona(tmp_path)
    assert "RULES-PRIVATE" not in p.build_system_prompt("wechat")


# ---------- B 字节守卫：缺席/判空不留字节，热增删即时生效 ----------


def test_missing_rules_dir_identical_to_legacy_bytes(tmp_path):
    """规则目录不存在（CI 干净检出）→ 拼出的字节与旧版逐字节一致。"""
    p = make_persona(tmp_path)
    for st in ("", "private", "group"):
        assert p.build_system_prompt(st) == legacy_bytes(p)


def test_blank_skeleton_files_skip_without_byte_noise(tmp_path):
    """落了骨架但没写正文 → 与"目录不存在"逐字节一致（抓空行噪音）。"""
    created = ensure_user_rules(tmp_path / "rules")
    assert len(created) == 3
    p = make_persona(tmp_path)
    for st in ("", "private", "group"):
        assert p.build_system_prompt(st) == legacy_bytes(p)


def test_rule_file_deleted_between_builds_removes_live(tmp_path):
    """删掉规则文件，下一句即失效——不留残字节、不抛。"""
    rules = tmp_path / "rules"
    write_rule(rules, "global", GLOBAL_RULE)
    p = make_persona(tmp_path)
    assert "RULES-GLOBAL" in p.build_system_prompt("")
    (rules / "global.md").unlink()
    assert p.build_system_prompt("") == legacy_bytes(p)


def test_rule_written_between_builds_applies_next_turn(tmp_path):
    """热重载对偶：写完下一句即生效（每回合现拼）。"""
    p = make_persona(tmp_path)
    assert "RULES-GROUP" not in p.build_system_prompt("group")
    write_rule(tmp_path / "rules", "group", GROUP_RULE)
    assert "RULES-GROUP" in p.build_system_prompt("group")


def test_bad_bytes_rule_falls_back_to_last_good_same_type(tmp_path):
    """读坏（坏字节）→ 沿用同类型上次成功那份，不炸回合。"""
    rules = tmp_path / "rules"
    write_rule(rules, "group", GROUP_RULE)
    p = make_persona(tmp_path)
    good = p.build_system_prompt("group")
    (rules / "group.md").write_bytes(b"\xff\xfe\xfd")
    assert p.build_system_prompt("group") == good


# ---------- C 缓存守卫（§19-22 扩展）：按类型稳定、跨类型共享前缀 ----------


def test_byte_stable_per_session_type(tmp_path):
    """每型：同实例重复 + 跨实例，逐字节一致。"""
    rules = tmp_path / "rules"
    write_rule(rules, "global", GLOBAL_RULE)
    write_rule(rules, "private", PRIVATE_RULE)
    write_rule(rules, "group", GROUP_RULE)
    p = make_persona(tmp_path)
    q = make_persona(tmp_path)
    for st in ("", "private", "group"):
        assert p.build_system_prompt(st) == p.build_system_prompt(st)
        assert q.build_system_prompt(st) == p.build_system_prompt(st)


def test_prefix_shared_across_types(tmp_path):
    """两场景都写了内容：私聊/群聊都以基础型为公共前缀。"""
    rules = tmp_path / "rules"
    write_rule(rules, "global", GLOBAL_RULE)
    write_rule(rules, "private", PRIVATE_RULE)
    write_rule(rules, "group", GROUP_RULE)
    p = make_persona(tmp_path)
    common = p.build_system_prompt("")
    assert p.build_system_prompt("private").startswith(common)
    assert p.build_system_prompt("group").startswith(common)


def test_empty_scene_keeps_types_prefix_of_each_other(tmp_path):
    """只有群聊规则写了内容：私聊型 == 基础型，群聊型以它为前缀。"""
    write_rule(tmp_path / "rules", "group", GROUP_RULE)
    p = make_persona(tmp_path)
    base = p.build_system_prompt("")
    assert p.build_system_prompt("private") == base
    assert p.build_system_prompt("group").startswith(base)


def test_block_order_index_increasing(tmp_path):
    """块序守卫：base < chat < persona < global < 场景——分叉点必须在最尾。"""
    p = make_persona(tmp_path)
    p.base_path = tmp_path / "base.md"
    p.base_path.write_text("BLOCK-BASE", encoding="utf-8")
    p.chat_path = tmp_path / "chat.md"
    p.chat_path.write_text("BLOCK-CHAT", encoding="utf-8")
    p.user_persona_path = tmp_path / "my-persona.md"
    p.user_persona_path.write_text("BLOCK-PERSONA", encoding="utf-8")
    write_rule(tmp_path / "rules", "global", "BLOCK-GLOBAL")
    write_rule(tmp_path / "rules", "group", "BLOCK-SCENE")
    prompt = p.build_system_prompt("group")
    idx = [
        prompt.index(t) for t in
        ("BLOCK-BASE", "BLOCK-CHAT", "BLOCK-PERSONA", "BLOCK-GLOBAL", "BLOCK-SCENE")
    ]
    assert idx == sorted(idx)


# ---------- D 回退正确性：绝不跨类型 ----------


def test_group_failure_never_returns_private_prompt(tmp_path):
    """群聊规则读坏时回退到上次**群聊**版——绝不能把私聊规则顶进群聊。"""
    rules = tmp_path / "rules"
    write_rule(rules, "private", PRIVATE_RULE)
    write_rule(rules, "group", GROUP_RULE)
    p = make_persona(tmp_path)
    last_group = p.build_system_prompt("group")
    (rules / "group.md").write_bytes(b"\xff\xfe\xfd")
    fallback = p.build_system_prompt("group")
    assert fallback == last_group
    assert "RULES-GROUP" in fallback
    assert "RULES-PRIVATE" not in fallback


def test_first_group_build_failure_returns_base_prompt(tmp_path):
    """起点即坏（从没成功拼过群聊版）→ 回退基础型，绝不是私聊。"""
    rules = tmp_path / "rules"
    write_rule(rules, "private", PRIVATE_RULE)
    (rules / "group.md").write_bytes(b"\xff\xfe\xfd")
    p = make_persona(tmp_path)
    fallback = p.build_system_prompt("group")
    assert fallback == p.build_system_prompt("")
    assert "RULES-PRIVATE" not in fallback


# ---------- E 骨架种子 ----------


def test_ensure_user_rules_seeds_three_skeletons(tmp_path):
    rules = tmp_path / "rules"
    created = ensure_user_rules(rules)
    assert len(created) == 3
    for name in ("global", "private", "group"):
        text = (rules / f"{name}.md").read_text(encoding="utf-8")
        assert _has_real_content(text) is False  # 骨架 = 判空 = 不注入


def test_ensure_user_rules_never_overwrites(tmp_path):
    rules = tmp_path / "rules"
    write_rule(rules, "group", GROUP_RULE)
    created = ensure_user_rules(rules)
    assert len(created) == 2  # 只补缺的两份
    assert (rules / "group.md").read_text(encoding="utf-8") == GROUP_RULE


def test_ensure_user_rules_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b"
    ensure_user_rules(nested)
    assert (nested / "global.md").is_file()


# ---------- F 接线：装配/端到端真的用上了规则 ----------


def make_bot(persona):
    bot = ChatLLM.__new__(ChatLLM)
    bot.persona = persona
    bot.context = ContextAssembler()
    bot.history_keep_messages = 10
    bot.history_lookback_extra = 5
    return bot


def test_assemble_messages_group_uses_group_rules(tmp_path):
    rules = tmp_path / "rules"
    write_rule(rules, "private", PRIVATE_RULE)
    write_rule(rules, "group", GROUP_RULE)
    bot = make_bot(make_persona(tmp_path))
    session = SessionContext(session_id="qq:g1", session_type="group", owner="u1")
    msgs = bot.assemble_messages("你好", None, session=session)
    assert "RULES-GROUP" in msgs[0]["content"]
    assert "RULES-PRIVATE" not in msgs[0]["content"]


def test_webui_default_private_gets_private_rules(tmp_path):
    """WebUI 不设 session_type → Message 默认 private → 吃私聊规则。"""
    write_rule(tmp_path / "rules", "private", PRIVATE_RULE)
    bot = make_bot(make_persona(tmp_path))
    m = Message(id="1", platform="web", session_id="web:x", owner="local",
                direction="in", role="user", content="hi")
    assert m.session_type == "private"  # WebUI 路径的前提
    session = SessionContext(session_id=m.session_id, session_type=m.session_type,
                             owner=m.owner)
    msgs = bot.assemble_messages("你好", None, session=session)
    assert "RULES-PRIVATE" in msgs[0]["content"]


def test_run_loop_system_message_gets_scene_rules(tmp_path):
    """端到端：run_loop → 请求里的 system（messages[0]）带上群聊规则。"""
    import asyncio
    from types import SimpleNamespace

    captured = {}

    class FakeClient:
        def __init__(self):
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

    write_rule(tmp_path / "rules", "group", GROUP_RULE)
    reg = PluginRegistry()
    reg.reset()
    reg.scan()
    bot = ChatLLM.__new__(ChatLLM)
    bot.model = "fake"
    bot.persona = make_persona(tmp_path)
    bot.context = ContextAssembler()
    bot.parser = XmlParser()
    bot.registry = reg
    bot.executor = ToolExecutor(reg, PermissionGuard(reg))
    bot.max_agent_steps = 3
    bot.history_keep_messages = 10
    bot.history_lookback_extra = 5
    bot.fallback_text = "（兜底）"
    bot.client = FakeClient()

    asyncio.run(bot.run_loop("你好", None, "qq:g123", session_type="group"))
    assert captured["messages"][0]["role"] == "system"
    assert "RULES-GROUP" in captured["messages"][0]["content"]
    reg.reset()
