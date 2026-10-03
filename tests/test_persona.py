"""M0-03 验收：system 提示词字节稳定。

三份静态块（base.md / chat.md / persona.md）拼好后不得随调用次数变化，
否则 provider 端缓存失效。同一实例重复取、不同实例分别取，都必须完全一致。
"""

import os
import sys

# 让测试能找到 src 下的代码（与 test_config.py 保持一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.llm.persona_llm.base import Persona


def test_same_instance_byte_stable():
    p = Persona()
    first = p.build_system_prompt()
    second = p.build_system_prompt()
    third = p.build_system_prompt()
    assert first == second == third


def test_different_instances_identical():
    a = Persona()
    b = Persona()
    assert a.build_system_prompt() == b.build_system_prompt()


def test_system_prompt_contains_static_blocks():
    """静态块内容齐全：人格（塔利）+ 占位符（<msg>）。"""
    p = Persona()
    prompt = p.build_system_prompt()
    assert "塔利" in prompt
    assert "<msg>" in prompt