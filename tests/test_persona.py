"""M0-03 验收：system 提示词字节稳定。

三份静态块（base.md / chat.md / persona.md）拼好后不得随调用次数变化，
否则 provider 端缓存失效。同一实例重复取、不同实例分别取，都必须完全一致。

注：本文件测的是**框架装配**（拼接顺序、字节稳定），不测用户人设内容——
所以下面凡是断言"人设里有什么"的用例，一律用**内置模板**当受控输入
（`persona_path=BUILTIN_PERSONA_PATH`），不去碰 `data/config/persona.md`。
那是用户的域：用户把人设改成"林星遥"之后，硬编码"塔利"的断言就会挂
（真挂过一次）。
"""

import os
import sys

# 让测试能找到 src 下的代码（与 test_config.py 保持一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.llm.persona_llm.base import BUILTIN_PERSONA_PATH, Persona


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
    """静态块内容齐全：内置人设 + 输出契约占位符（<msg>）。

    用内置模板当输入（不读用户的 persona 文件）——本用例验的是"装配把
    人设那份拼进去了"，不是"用户写了什么"。
    """
    p = Persona(persona_path=BUILTIN_PERSONA_PATH)
    prompt = p.build_system_prompt()
    assert "塔利" in prompt          # 内置模板里的角色名
    assert "<msg>" in prompt