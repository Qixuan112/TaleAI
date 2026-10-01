"""Persona 的双层来源：内置模板 + 用户可改。

设计文档 §13 / §14：人格正文是**用户的域**（data/config/），代码里那份是
「默认带塔利模板」。§19-13：改动重启生效。

背景（为什么要有这个双轨）：persona.md 早先真在 data/config/ 下，但 data/ 整个
被 gitignore，CI 干净检出时找不到它、直接挂。当时把文件挪进 src/ 救急——问题
解决了，可用户改人设就得去动源码，跟 §14「面板里写、用户不碰文件」相反。
这里补回双轨：src/ 那份当内置默认，data/config/ 那份存在就优先。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.llm.persona_llm.base import (
    BUILTIN_PERSONA_PATH,
    USER_PERSONA_PATH,
    Persona,
    ensure_user_persona,
)


# ---------- 内置模板必须存在（CI 靠它活） ----------


def test_builtin_template_exists_and_is_in_src():
    """内置模板随代码走——CI 干净检出时 data/ 不存在，全指望这一份。"""
    assert BUILTIN_PERSONA_PATH.is_file()
    assert "src" in BUILTIN_PERSONA_PATH.parts
    assert "塔利" in BUILTIN_PERSONA_PATH.read_text(encoding="utf-8")


def test_user_path_points_into_data_config():
    """用户那份在 data/config/ 下（§13 的用户的域）。"""
    assert USER_PERSONA_PATH.parts[-3:] == ("data", "config", "persona.md")


# ---------- 读：用户文件优先，没有就用内置 ----------


def test_no_user_file_falls_back_to_builtin():
    """用户还没建文件时，用内置模板——开箱即用。"""
    missing = Path("/nonexistent/persona.md")
    p = Persona(persona_path=missing)
    assert "塔利" in p.build_system_prompt()


def test_user_file_overrides_builtin(tmp_path):
    """用户文件存在就听用户的。"""
    custom = tmp_path / "persona.md"
    custom.write_text("# 我的角色\n\n我是自定义人格。", encoding="utf-8")
    p = Persona(persona_path=custom)
    prompt = p.build_system_prompt()
    assert "我是自定义人格" in prompt
    assert "塔利" not in prompt  # 内置那份不该混进来


def test_base_and_chat_still_included_with_custom_persona(tmp_path):
    """自定义的只是人格正文；base/chat 这两份框架提示词仍要在。

    base.md（世界观/回应原则）与 chat.md（输出契约）是框架的一部分，
    不是用户能改的——改了 <msg> 契约就没人解析得出话。
    """
    custom = tmp_path / "persona.md"
    custom.write_text("我是自定义人格。", encoding="utf-8")
    prompt = Persona(persona_path=custom).build_system_prompt()
    assert "<msg>" in prompt  # chat.md 的输出契约
    assert "数字生命" in prompt  # base.md 的世界观


def test_default_construction_uses_user_file_when_present(tmp_path, monkeypatch):
    """不带参数构造时，走「用户文件优先」这条默认路径。"""
    import core.llm.persona_llm.base as base

    custom = tmp_path / "persona.md"
    custom.write_text("默认路径下的人格。", encoding="utf-8")
    monkeypatch.setattr(base, "USER_PERSONA_PATH", custom)
    p = Persona()
    assert "默认路径下的人格" in p.build_system_prompt()


# ---------- 种子：首次运行把内置模板落到用户处 ----------


def test_ensure_user_persona_seeds_from_builtin(tmp_path):
    """用户文件不存在 → 用内置模板生成一份，用户才有得改。"""
    target = tmp_path / "persona.md"
    created = ensure_user_persona(target)
    assert created is True
    assert target.is_file()
    assert "塔利" in target.read_text(encoding="utf-8")


def test_ensure_user_persona_does_not_overwrite(tmp_path):
    """用户改过的人设绝不能被覆盖——这是用户的域。"""
    target = tmp_path / "persona.md"
    target.write_text("我精心写的人设", encoding="utf-8")
    created = ensure_user_persona(target)
    assert created is False
    assert target.read_text(encoding="utf-8") == "我精心写的人设"


def test_ensure_user_persona_creates_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "persona.md"
    ensure_user_persona(target)
    assert target.is_file()


# ---------- 字节稳定（原有不变量不能被破坏） ----------


def test_byte_stable_with_user_file(tmp_path):
    custom = tmp_path / "persona.md"
    custom.write_text("稳定测试", encoding="utf-8")
    p = Persona(persona_path=custom)
    assert p.build_system_prompt() == p.build_system_prompt()
    assert Persona(persona_path=custom).build_system_prompt() == p.build_system_prompt()


def test_seeded_file_reads_back_identical(tmp_path):
    """种子写出的内容与内置模板逐字节一致——落盘不该改变人格。"""
    target = tmp_path / "persona.md"
    ensure_user_persona(target)
    assert target.read_text(encoding="utf-8") == BUILTIN_PERSONA_PATH.read_text(encoding="utf-8")
