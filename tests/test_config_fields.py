"""M0-12：配置面板字段定义（§13 / §22 的 `FieldSpec`）。

设计文档 §二十四 待办明写「M0 只做聊天页，配置面板排 M1」——所以本模块
在 M0 只落**字段类型定义**（面板将来据此渲染表单），不建面板本身。

这里验的是「定义本身是自洽的」：类型合法、select 有选项、密钥标了 sensitive。
这些是 M1 面板的输入契约，现在就得站得住。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.config.fields import FIELDS, FieldSpec, FIELD_TYPES


# ---------- FieldSpec 本体 ----------


def test_field_spec_has_documented_fields():
    f = FieldSpec(key="llm.model", label="模型", type="text")
    assert f.key == "llm.model"
    assert f.label == "模型"
    assert f.type == "text"
    assert f.sensitive is False  # 默认非敏感
    assert f.choices == ()


def test_field_spec_is_frozen():
    """字段定义是常量，不该被运行时改（改它等于改面板行为，得走代码评审）。"""
    f = FieldSpec(key="k", label="l", type="text")
    with pytest.raises(Exception):
        f.type = "password"


# ---------- 5 个域都要有定义 ----------


def test_all_five_domains_present():
    """§13：配置按域分 5 个文件。面板按域取字段，缺一个域面板就少一页。"""
    assert set(FIELDS) == {"config", "persona", "platforms", "plugins", "secrets"}


def test_every_field_type_is_legal():
    for domain, fields in FIELDS.items():
        for f in fields:
            assert f.type in FIELD_TYPES, f"{domain}.{f.key} 的类型 {f.type!r} 不在 {FIELD_TYPES}"


def test_keys_match_each_domains_json_shape():
    """key 是点号路径，且**与该域 JSON 的实际嵌套一致**。

    - config.json 嵌套（{"llm": {"model": ...}}）→ llm.model、bot.name，带点
    - persona.json 是平的（{"format": ...}）→ format、version，不带点
    - plugins.json 是平的（插件名 → 配置）→ enabled，不带点
    - platforms / secrets 嵌套 → 带点
    """
    nested = {"config": True, "persona": False, "platforms": True,
              "plugins": False, "secrets": True}
    for domain, fields in FIELDS.items():
        for f in fields:
            if nested[domain]:
                assert "." in f.key, f"{domain}.{f.key!r} 该是点号路径"
            else:
                assert "." not in f.key, f"{domain}.{f.key!r} 该是顶层扁平键"


def test_select_fields_have_choices():
    """select 没选项 = 面板渲染出空下拉框，用户没法选。"""
    for domain, fields in FIELDS.items():
        for f in fields:
            if f.type == "select":
                assert f.choices, f"{domain}.{f.key} 是 select 但没有 choices"


# ---------- 密钥字段（§13 sensitive） ----------


def test_secrets_domain_fields_are_sensitive():
    """§13：密钥标 sensitive——面板用密码框显示、读出来打码。"""
    for f in FIELDS["secrets"]:
        assert f.sensitive, f"secrets.{f.key} 必须标 sensitive"
        assert f.type == "password"


def test_non_secret_domains_have_no_sensitive_fields():
    """反过来也要成立：非密钥域不该冒出敏感字段（否则面板会把普通值打码）。"""
    for domain in ("config", "persona", "platforms", "plugins"):
        for f in FIELDS[domain]:
            assert not f.sensitive, f"{domain}.{f.key} 不该是 sensitive"


# ---------- 关键字段确实在里面 ----------


def test_core_llm_fields_declared():
    """用户在面板最想改的几个：服务商、模型、名字。"""
    keys = {f.key for f in FIELDS["config"]}
    assert {"llm.provider", "llm.base_url", "llm.model", "bot.name"} <= keys


def test_bot_name_has_default():
    """塔利是默认人设（改名那次定的）——面板上得有个默认值。"""
    bot_name = next(f for f in FIELDS["config"] if f.key == "bot.name")
    assert bot_name.default == "塔利"


def test_persona_domain_has_format_toggle():
    """§14：编辑器支持 md / 纯文本 两种模式切换。"""
    fmt = next(f for f in FIELDS["persona"] if f.key == "format")
    assert fmt.type == "select"
    assert set(fmt.choices) >= {"markdown", "text"}
