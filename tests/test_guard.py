"""M0-06 验收：PermissionGuard「超范围调用 100% 被拦」。

设计文档 §十五 / §3.6 / §19-11 / §22。

权限默认姿态是 deny：插件在 manifest 里声明要什么，用户显式授予才放行。
外部插件还多一层——默认停用（§19-11）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.plugin.guard import PermissionGuard
from core.plugin.registry import PluginRegistry


@pytest.fixture(autouse=True)
def registry():
    reg = PluginRegistry()
    reg.reset()
    yield reg
    reg.reset()


def write_plugin(root, name, *, permissions=None, tool_name=None, main_py=None):
    """造一个插件目录。默认注册一个同名工具。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps(
            {"name": name, "version": "0.1.0", "permissions": permissions or []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tool = tool_name or name
    (d / "main.py").write_text(
        main_py
        or (
            "from core.plugin.registry import register\n"
            f"@register.tool(name={tool!r}, schema={{}})\n"
            f"def {tool}() -> str:\n"
            "    return 'ok'\n"
        ),
        encoding="utf-8",
    )
    return d


# ---------- 内置工具（无权限声明）应当放行 ----------


def test_builtin_tool_without_permissions_allowed(registry):
    """ping/time_query 声明 permissions 为空，不需要任何授权就能调。"""
    registry.scan()
    guard = PermissionGuard(registry)
    assert guard.check("ping") is True
    assert guard.check("time_query") is True
    assert guard.deny_reason("ping") is None


def test_unknown_tool_is_denied(registry):
    """不存在的工具直接拦——不能因为"查不到"就放行。"""
    registry.scan()
    guard = PermissionGuard(registry)
    assert guard.check("根本没有这个工具") is False
    assert "没有名为" in guard.deny_reason("根本没有这个工具")


# ---------- 权限默认 deny ----------


def test_declared_permission_not_granted_is_denied(registry, tmp_path):
    """插件声明了 network 但没授予 → 拦。"""
    ext = tmp_path / "ext"
    write_plugin(ext, "netplug", permissions=["network"], tool_name="net_tool")
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("netplug")  # 先排除"外部默认停用"的干扰，单看权限
    assert guard.check("net_tool") is False
    assert "network" in guard.deny_reason("net_tool")


def test_grant_then_allowed(registry, tmp_path):
    ext = tmp_path / "ext"
    write_plugin(ext, "netplug", permissions=["network"], tool_name="net_tool")
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("netplug")
    guard.grant("netplug", "network")
    assert guard.check("net_tool") is True


def test_partial_grant_still_denied(registry, tmp_path):
    """声明两项权限只给一项 → 还拦（不能部分放行）。"""
    ext = tmp_path / "ext"
    write_plugin(ext, "multi", permissions=["network", "file_read"], tool_name="m_tool")
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("multi")
    guard.grant("multi", "network")
    assert guard.check("m_tool") is False
    assert "file_read" in guard.deny_reason("m_tool")


def test_revoke_restores_denial(registry, tmp_path):
    """收回权限后立刻又不可调——权限是可逆的。"""
    ext = tmp_path / "ext"
    write_plugin(ext, "netplug", permissions=["network"], tool_name="net_tool")
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("netplug")
    guard.grant("netplug", "network")
    assert guard.check("net_tool") is True

    guard.revoke("netplug", "network")
    assert guard.check("net_tool") is False


def test_revoke_unknown_permission_is_safe(registry, tmp_path):
    ext = tmp_path / "ext"
    write_plugin(ext, "p", permissions=[], tool_name="t")
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)
    guard = PermissionGuard(registry)
    guard.revoke("从来没授过权的插件", "network")  # 不该炸


# ---------- 外部插件默认停用（§19-11） ----------


def test_external_plugin_disabled_by_default(registry, tmp_path):
    ext = tmp_path / "ext"
    write_plugin(ext, "extplug", tool_name="ext_tool")  # 零权限
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    assert guard.check("ext_tool") is False
    assert "未启用" in guard.deny_reason("ext_tool")


def test_enable_external_plugin_without_permissions(registry, tmp_path):
    """零权限的外部插件，启用后即可调用。"""
    ext = tmp_path / "ext"
    write_plugin(ext, "extplug", tool_name="ext_tool")
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("extplug")
    assert guard.check("ext_tool") is True


def test_builtin_enabled_by_default(registry):
    """内置插件随代码发布，默认启用（与外部相反）。"""
    registry.scan()
    guard = PermissionGuard(registry)
    assert guard.check("ping") is True


def test_disable_builtin_plugin(registry):
    """内置也能被停用——停用后它的工具立刻不可调。"""
    registry.scan()
    guard = PermissionGuard(registry)
    assert guard.check("ping") is True

    guard.disable("ping")
    assert guard.check("ping") is False
    assert "未启用" in guard.deny_reason("ping")


def test_reenable_after_disable(registry):
    registry.scan()
    guard = PermissionGuard(registry)
    guard.disable("ping")
    guard.enable("ping")
    assert guard.check("ping") is True


# ---------- 边界 ----------


def test_guard_does_not_touch_registry(registry, tmp_path):
    """守卫只读注册表：检查不该改动工具表。"""
    registry.scan()
    guard = PermissionGuard(registry)
    before = sorted(registry.tools())
    guard.check("ping")
    guard.check("不存在的")
    assert sorted(registry.tools()) == before


def test_check_is_pure(registry):
    """重复检查结果一致（纯函数性质）。"""
    registry.scan()
    guard = PermissionGuard(registry)
    first, second = guard.check("ping"), guard.check("ping")
    assert first is True and second is True
