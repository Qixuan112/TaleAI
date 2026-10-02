"""M0-07 验收：ToolExecutor「FC → 权限 → 插件 → 结果」。

设计文档 §四 / §22 / §18.3。

关键性质：**执行器永不抛异常**。模型调不存在的工具、给错参数，都是
正常情况而不是程序错误——要把原因回喂给它自己纠正，而不是崩掉链路。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.bus.event_bus import EventBus
from core.executor import ToolCall, ToolExecutor
from core.plugin.guard import PermissionGuard
from core.plugin.registry import PluginRegistry


@pytest.fixture(autouse=True)
def registry():
    reg = PluginRegistry()
    reg.reset()
    yield reg
    reg.reset()


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


def write_plugin(root, name, *, permissions=None, main_py):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"name": name, "version": "0.1.0", "permissions": permissions or []},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    (d / "main.py").write_text(main_py, encoding="utf-8")
    return d


def call(name, args=None, call_id="call_1"):
    return ToolCall(call_id=call_id, name=name, arguments=args or {})


# ---------- 正常路径 ----------


def test_execute_builtin_ping(registry):
    registry.scan()
    ex = ToolExecutor(registry)
    result = ex.execute(call("ping"))
    assert result.ok is True
    assert result.permission_denied is False
    assert result.data == {"result": "pong"}
    assert result.error is None


def test_result_carries_the_call_back(registry):
    """结果要带回原始调用——多轮循环里靠 call_id 配对回喂。"""
    registry.scan()
    ex = ToolExecutor(registry)
    c = call("ping", call_id="call_abc")
    assert ex.execute(c).call is c


def test_time_query_with_argument(registry):
    registry.scan()
    ex = ToolExecutor(registry)
    result = ex.execute(call("time_query", {"tz": "Asia/Tokyo"}))
    assert result.ok is True
    assert "Asia/Tokyo" in result.data["result"]


def test_dict_return_preserved(registry, tmp_path):
    """插件返回 dict 时原样保留，不包一层 result。"""
    ext = tmp_path / "ext"
    write_plugin(ext, "structured", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='weather', schema={})\n"
        "def weather(city='上海'):\n"
        "    return {'weather': '晴', 'temp': 25}\n"
    ))
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("structured")
    ex = ToolExecutor(registry, guard)
    result = ex.execute(call("weather", {"city": "上海"}))
    assert result.ok is True
    assert result.data == {"weather": "晴", "temp": 25}


# ---------- 失败路径：全部变成 ToolResult，不抛异常 ----------


def test_unknown_tool_returns_error_not_raise(registry):
    registry.scan()
    ex = ToolExecutor(registry)
    result = ex.execute(call("不存在的工具"))
    assert result.ok is False
    assert result.permission_denied is False
    assert "不存在的工具" in result.error


def test_permission_denied_flagged(registry, tmp_path):
    ext = tmp_path / "ext"
    write_plugin(ext, "netplug", permissions=["network"], main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='net_tool', schema={})\n"
        "def net_tool():\n"
        "    return 'ok'\n"
    ))
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("netplug")  # 启用了，但没给 network 权限
    ex = ToolExecutor(registry, guard)

    result = ex.execute(call("net_tool"))
    assert result.ok is False
    assert result.permission_denied is True
    assert "network" in result.error


def test_bad_arguments_reported(registry):
    """给无参工具传参数 → 参数错误，而不是崩溃。"""
    registry.scan()
    ex = ToolExecutor(registry)
    result = ex.execute(call("ping", {"不存在的参数": 1}))
    assert result.ok is False
    assert result.permission_denied is False
    assert "参数" in result.error


def test_plugin_raising_is_caught(registry, tmp_path):
    ext = tmp_path / "ext"
    write_plugin(ext, "boom", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='boom_tool', schema={})\n"
        "def boom_tool():\n"
        "    raise ValueError('工具内部炸了')\n"
    ))
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)

    guard = PermissionGuard(registry)
    guard.enable("boom")
    ex = ToolExecutor(registry, guard)

    result = ex.execute(call("boom_tool"))
    assert result.ok is False
    assert "ValueError" in result.error
    assert "工具内部炸了" in result.error


def test_plugin_internal_typeerror_not_reported_as_bad_args(registry, tmp_path):
    """插件内部自己抛的 TypeError 不该被误报成"参数不正确"。

    这两类错误的修正方向完全不同：一个是模型改参数，一个是插件有 bug。
    用 inspect.signature().bind() 预先校验参数，把两者分开。
    """
    ext = tmp_path / "ext"
    write_plugin(ext, "buggy", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='buggy_tool', schema={})\n"
        "def buggy_tool():\n"
        "    return 1 + 'string'\n"  # 内部 TypeError
    ))
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)
    guard = PermissionGuard(registry)
    guard.enable("buggy")

    result = ToolExecutor(registry, guard).execute(call("buggy_tool"))
    assert result.ok is False
    assert "参数不正确" not in result.error
    assert "工具执行出错" in result.error


def test_missing_required_argument_reported(registry, tmp_path):
    """缺必填参数 → 明确报参数问题（模型漏填时很常见）。"""
    ext = tmp_path / "ext"
    write_plugin(ext, "needsarg", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='needs_arg', schema={})\n"
        "def needs_arg(required_one):\n"
        "    return required_one\n"
    ))
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)
    guard = PermissionGuard(registry)
    guard.enable("needsarg")

    result = ToolExecutor(registry, guard).execute(call("needs_arg"))
    assert result.ok is False
    assert "参数不正确" in result.error


def test_tool_returning_none(registry, tmp_path):
    """插件返回 None 也要能规整，不能把 data 留成 None 之外的怪东西。"""
    ext = tmp_path / "ext"
    write_plugin(ext, "silent", main_py=(
        "from core.plugin.registry import register\n"
        "@register.tool(name='silent_tool', schema={})\n"
        "def silent_tool():\n"
        "    return None\n"
    ))
    registry.scan(builtin_dir=tmp_path / "none", external_dir=ext)
    guard = PermissionGuard(registry)
    guard.enable("silent")

    result = ToolExecutor(registry, guard).execute(call("silent_tool"))
    assert result.ok is True
    assert result.data == {"result": None}


# ---------- 批量执行 ----------


def test_execute_all_keeps_order_and_count(registry):
    registry.scan()
    ex = ToolExecutor(registry)
    calls = [call("ping", call_id=f"c{i}") for i in range(3)]
    results = ex.execute_all(calls)
    assert len(results) == 3
    assert [r.call.call_id for r in results] == ["c0", "c1", "c2"]
    assert all(r.ok for r in results)


def test_execute_all_mixed_success_and_failure(registry):
    """一个失败不该影响后面的调用。"""
    registry.scan()
    ex = ToolExecutor(registry)
    results = ex.execute_all([call("ping", call_id="a"),
                              call("不存在", call_id="b"),
                              call("ping", call_id="c")])
    assert [r.ok for r in results] == [True, False, True]


def test_data_is_json_serializable(registry):
    """结果要能序列化回喂给模型。"""
    registry.scan()
    ex = ToolExecutor(registry)
    result = ex.execute(call("ping"))
    json.dumps(result.data, ensure_ascii=False)


# ---------- tool.called 事件（§18.2 目录，生产者=ToolExecutor） ----------


def test_publishes_tool_called_on_success(registry, clean_bus):
    """成功也要发——日志页据此显示"调了哪个工具"。"""
    registry.scan()
    ex = ToolExecutor(registry, bus=clean_bus)
    seen = []
    clean_bus.subscribe("tool.called", lambda e: seen.append(e.data))
    ex.execute(call("ping"), session_id="web:abc")
    assert len(seen) == 1
    assert seen[0]["tool"] == "ping"
    assert seen[0]["ok"] is True
    assert seen[0]["session_id"] == "web:abc"


def test_publishes_tool_called_on_failure(registry, clean_bus):
    """失败更值得看：也发，且带上错误原因。"""
    registry.scan()
    ex = ToolExecutor(registry, bus=clean_bus)
    seen = []
    clean_bus.subscribe("tool.called", lambda e: seen.append(e.data))
    ex.execute(call("不存在的工具"), session_id="s")
    assert seen and seen[0]["ok"] is False
    assert seen[0]["error"]


def test_no_bus_means_no_publish(registry):
    """不注入 bus（裸构造）就不发事件——单测/CLI 不需要，行为跟以前一样。"""
    registry.scan()
    ex = ToolExecutor(registry)  # 无 bus
    ex.execute(call("ping"))  # 不抛即通过（没法订阅到，只为确认不炸）
