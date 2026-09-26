"""M0-05 验收：插件注册表「注册/卸载可逆」。

设计文档 §五 / §18.1 启动第 4 步 / §22 / §23。

测试用真实的内置插件目录（builtin/ping、builtin/time_query），
外部插件的场景用 tmp_path 现场造——这样测的就是用户装插件的真实路径。
"""

import json
import os
import sys

# 让测试能找到 src 下的代码（与 test_config.py 保持一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.plugin.manifest import Manifest, ManifestError
from core.plugin.registry import PluginRegistry


@pytest.fixture(autouse=True)
def clean_registry():
    """单例是全局的，每个用例前后都要清干净，否则互相污染。"""
    registry = PluginRegistry()
    registry.reset()
    yield registry
    registry.reset()


def write_plugin(root, name, *, manifest=None, main_py="", subdir=None):
    """在 root 下造一个插件目录，返回目录路径。"""
    d = root / (subdir or name)
    d.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "version": "0.1.0"} if manifest is None else manifest
    if payload is not None:
        (d / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    if main_py:
        (d / "main.py").write_text(main_py, encoding="utf-8")
    return d


# ---------- Manifest：声明解析与校验 ----------


def test_manifest_load_ok(tmp_path):
    d = write_plugin(tmp_path, "demo", manifest={
        "name": "demo", "version": "1.2.3",
        "description": "演示", "permissions": ["network"],
    })
    m = Manifest.load(d)
    assert m.name == "demo"
    assert m.version == "1.2.3"
    assert m.description == "演示"
    assert m.permissions == ["network"]
    assert m.path == d


def test_manifest_missing_file(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(ManifestError, match="缺少"):
        Manifest.load(d)


def test_manifest_bad_json(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "manifest.json").write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(ManifestError, match="合法 JSON"):
        Manifest.load(d)


def test_manifest_missing_name(tmp_path):
    d = write_plugin(tmp_path, "x", manifest={"version": "1.0"})
    with pytest.raises(ManifestError, match="name"):
        Manifest.load(d)


def test_manifest_blank_name(tmp_path):
    d = write_plugin(tmp_path, "x", manifest={"name": "   "})
    with pytest.raises(ManifestError, match="name"):
        Manifest.load(d)


def test_manifest_permissions_must_be_string_list(tmp_path):
    d = write_plugin(tmp_path, "x", manifest={"name": "x", "permissions": "network"})
    with pytest.raises(ManifestError, match="permissions"):
        Manifest.load(d)


def test_manifest_defaults(tmp_path):
    d = write_plugin(tmp_path, "min", manifest={"name": "min"})
    m = Manifest.load(d)
    assert m.version == "0.0.0"
    assert m.description == ""
    assert m.permissions == []


def test_manifest_accepts_str_path(tmp_path):
    d = write_plugin(tmp_path, "s", manifest={"name": "s"})
    assert Manifest.load(str(d)).name == "s"


# ---------- 扫描内置 ----------


def test_scan_loads_builtin_plugins(clean_registry):
    loaded = clean_registry.scan()
    assert "ping" in loaded
    assert "time_query" in loaded


def test_builtin_tools_registered(clean_registry):
    clean_registry.scan()
    tools = clean_registry.tools()
    assert set(tools) >= {"ping", "time_query"}
    assert tools["ping"].plugin == "ping"


def test_ping_returns_pong(clean_registry):
    clean_registry.scan()
    assert clean_registry.tools()["ping"].handler() == "pong"


def test_time_query_default_and_param(clean_registry):
    clean_registry.scan()
    fn = clean_registry.tools()["time_query"].handler
    assert "Asia/Shanghai" in fn()
    assert "Asia/Tokyo" in fn("Asia/Tokyo")


def test_time_query_unsupported_zone_is_explicit(clean_registry):
    """表外时区要明确报错，不能静默给个可能错的当地时间。"""
    clean_registry.scan()
    out = clean_registry.tools()["time_query"].handler("America/New_York")
    assert out.startswith("错误")
    assert "不支持" in out


def test_external_dir_missing_is_fine(clean_registry, tmp_path):
    """data/plugin 不存在是正常的（用户没装外部插件），不该报错。"""
    loaded = clean_registry.scan(
        builtin_dir=tmp_path / "nope", external_dir=tmp_path / "also-nope"
    )
    assert loaded == []


# ---------- 工具表（原生 FC 格式） ----------


def test_tool_schemas_shape(clean_registry):
    clean_registry.scan()
    schemas = {s["function"]["name"]: s for s in clean_registry.tool_schemas()}
    assert set(schemas) >= {"ping", "time_query"}

    ping = schemas["ping"]
    assert ping["type"] == "function"
    assert ping["function"]["description"]
    assert ping["function"]["parameters"]["type"] == "object"

    # 有参工具的 schema 要带出参数
    props = schemas["time_query"]["function"]["parameters"]["properties"]
    assert "tz" in props


def test_tool_schemas_are_json_serializable(clean_registry):
    """工具表要能直接塞进 FC 请求体，必须可 JSON 序列化。"""
    clean_registry.scan()
    json.dumps(clean_registry.tool_schemas(), ensure_ascii=False)


# ---------- 验收核心：注册/卸载可逆 ----------


def test_unregister_removes_only_that_plugin(clean_registry):
    clean_registry.scan()
    before = set(clean_registry.tools())

    assert clean_registry.unregister("ping") is True
    after = set(clean_registry.tools())

    assert "ping" not in after
    assert "time_query" in after  # 别的插件不受影响
    assert after == before - {"ping"}


def test_unregister_then_rescan_restores(clean_registry):
    """卸载后重扫应装回来——这才叫可逆。"""
    clean_registry.scan()
    clean_registry.unregister("ping")
    assert "ping" not in clean_registry.tools()

    clean_registry.scan()
    assert "ping" in clean_registry.tools()
    assert clean_registry.tools()["ping"].handler() == "pong"


def test_unregister_unknown_returns_false(clean_registry):
    clean_registry.scan()
    assert clean_registry.unregister("不存在的插件") is False


def test_double_unregister_is_safe(clean_registry):
    clean_registry.scan()
    assert clean_registry.unregister("ping") is True
    assert clean_registry.unregister("ping") is False


def test_rescan_does_not_duplicate(clean_registry):
    """重复扫描同一个插件不该产生重复注册，也不该误报名冲突。"""
    clean_registry.scan()
    n = len(clean_registry.tools())
    clean_registry.scan()
    assert len(clean_registry.tools()) == n


def test_external_plugin_registers(clean_registry, tmp_path):
    """外部插件的完整路径：写 manifest + main.py → 扫 → 工具可用。"""
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    external = tmp_path / "external"
    write_plugin(external, "echo", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='echo', schema={'description': '回声'})\n"
        "def echo(text: str = '') -> str:\n"
        "    return text\n"
    ))
    clean_registry.scan(builtin_dir=builtin, external_dir=external)

    assert "echo" in clean_registry.tools()
    assert clean_registry.tools()["echo"].handler("哈") == "哈"
    assert clean_registry.plugins()["echo"].source == "external"


# ---------- 冲突处理（§五：内置优先） ----------


def test_builtin_wins_over_external_same_name(clean_registry, tmp_path):
    """同名冲突：内置生效，外部跳过并告警（§五）。"""
    external = tmp_path / "external"
    write_plugin(external, "ping", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='ping', schema={'description': '外部的 ping'})\n"
        "def ping() -> str:\n"
        "    return '这是外部的'\n"
    ))
    # builtin 用真实目录，external 用临时目录，故意撞名
    clean_registry.scan(external_dir=external)

    assert clean_registry.tools()["ping"].handler() == "pong"  # 内置生效
    assert clean_registry.plugins()["ping"].source == "builtin"


# ---------- 坏插件不能拖垮启动 ----------


def test_broken_manifest_skipped_others_still_load(clean_registry, tmp_path):
    external = tmp_path / "external"
    write_plugin(external, "good", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='good', schema={})\n"
        "def good() -> str:\n"
        "    return 'ok'\n"
    ))
    # 坏插件：manifest 无 name
    write_plugin(tmp_path / "external", "broken", manifest={"version": "1.0"})

    clean_registry.scan(builtin_dir=tmp_path / "none", external_dir=external)
    assert "good" in clean_registry.tools()


def test_plugin_raising_on_import_is_skipped(clean_registry, tmp_path):
    external = tmp_path / "external"
    write_plugin(external, "boom", main_py="raise RuntimeError('插件炸了')")
    write_plugin(external, "ok", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='ok', schema={})\n"
        "def ok() -> str:\n"
        "    return 'ok'\n"
    ))

    clean_registry.scan(builtin_dir=tmp_path / "none", external_dir=external)
    assert "ok" in clean_registry.tools()
    assert "boom" not in clean_registry.plugins()


def test_partial_registration_cleaned_after_failure(clean_registry, tmp_path):
    """加载中途炸掉：它已经注册的工具要被清掉，不留半拉状态。"""
    external = tmp_path / "external"
    write_plugin(external, "half", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='half_tool', schema={})\n"
        "def half_tool() -> str:\n"
        "    return 'x'\n"
        "raise RuntimeError('注册完工具之后才炸')\n"
    ))

    clean_registry.scan(builtin_dir=tmp_path / "none", external_dir=external)
    assert "half_tool" not in clean_registry.tools()


# ---------- 单例与归属 ----------


def test_registry_is_singleton():
    assert PluginRegistry() is PluginRegistry()


def test_register_outside_loading_context_is_rejected(clean_registry):
    """无归属的注册无法被卸载，直接拒绝（守住"可逆"）。"""
    with pytest.raises(RuntimeError, match="加载上下文"):
        clean_registry.register_tool("野工具", {}, lambda: "x")


def test_missing_main_py_skipped(clean_registry, tmp_path):
    external = tmp_path / "external"
    write_plugin(external, "nomod", manifest={"name": "nomod"})  # 只有 manifest
    loaded = clean_registry.scan(builtin_dir=tmp_path / "none", external_dir=external)
    assert loaded == []
