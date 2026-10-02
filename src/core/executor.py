"""工具执行器：FC → 权限 → 插件 → 结果。

设计文档 §四 / §18.1 / §22 / §18.3（数据结构）。

**纯执行，不做理解**（§四）：拿到 FC 调用就直接走「查表 → 权限 → 调插件」，
不解析参数语义、不做自然语言转换——那些是模型原生 FC 的能力，不需要中间层。

它不认识任何具体插件：只通过注册表拿 handler。这样新增工具不需要动执行器，
符合「新增工具 = 新增插件目录」的原则（§23）。
"""

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from core.bus.event_bus import EventBus
from core.plugin.guard import PermissionGuard
from core.plugin.registry import PluginRegistry

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


@dataclass
class ToolCall:
    """一次工具调用（形状见 §18.3）。"""

    call_id: str
    name: str
    arguments: dict


@dataclass
class ToolResult:
    """一次工具调用的结果（形状见 §18.3）。

    permission_denied 单独拎出来，是因为"被拦"和"执行失败"性质不同：
    前者是策略决定（不该重试），后者是技术故障（可以重试/换参数）。
    """

    call: ToolCall
    ok: bool
    data: dict | None = None
    error: str | None = None
    permission_denied: bool = False


class ToolExecutor:
    """按 FC 调用执行工具。"""

    def __init__(
        self,
        registry: PluginRegistry | None = None,
        guard: PermissionGuard | None = None,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self._registry = registry if registry is not None else PluginRegistry()
        self._guard = guard if guard is not None else PermissionGuard(self._registry)
        # 事件总线（可选）：每次执行完发一条 tool.called（§18.2 目录，生产者=
        # ToolExecutor，订阅者=日志）。不注入就不发——单测/CLI 不需要。
        self._bus = bus

    def execute(self, call: ToolCall, *, session_id: str = "") -> ToolResult:
        """执行一次工具调用。任何失败都变成 ToolResult，不向上抛。

        为什么不抛异常：模型可能调一个不存在的工具、或给出参数不匹配的
        参数——这是**正常情况**而不是程序错误，得把"为什么失败"回喂给模型
        让它自己纠正，而不是让整条链路崩掉。

        session_id 只用于旁路事件（tool.called 想记是谁触发的）；ToolCall
        本身不含它（形状 §18.3 固定），所以由调用方顺手带进来。
        """
        result = self._execute(call)
        self._publish_called(call, result, session_id)
        return result

    def _publish_called(
        self, call: ToolCall, result: ToolResult, session_id: str
    ) -> None:
        """旁路喊一声"调了工具"（失败也算——失败更值得看）。"""
        if self._bus is None:
            return
        self._bus.publish(
            "tool.called",
            session_id=session_id,
            tool=call.name,
            ok=result.ok,
            error=result.error,
        )

    def _execute(self, call: ToolCall) -> ToolResult:
        tool = self._registry.tools().get(call.name)

        if tool is None:
            logger.warning("模型调了不存在的工具：%r", call.name)
            return ToolResult(
                call=call, ok=False, error=f"没有名为 {call.name!r} 的工具"
            )

        if not self._guard.check(call.name):
            reason = self._guard.deny_reason(call.name) or "权限不足"
            logger.warning("工具调用被权限守卫拦下：%s（%s）", call.name, reason)
            return ToolResult(call=call, ok=False, error=reason, permission_denied=True)

        # 先单独校验参数，再真正调用。
        # 不这么做的话，插件内部自己抛的 TypeError 会被误归成"参数不对"，
        # 把模型引向错误的修正方向。bind() 只检查签名匹配，不执行函数。
        try:
            inspect.signature(tool.handler).bind(**call.arguments)
        except TypeError as e:
            logger.warning("工具 %r 参数不匹配：%s", call.name, e)
            return ToolResult(
                call=call, ok=False, error=f"参数不正确：{e}"
            )

        try:
            raw = tool.handler(**call.arguments)
        except Exception as e:  # 插件代码是外来的，坏了不能拖垮链路
            logger.exception("工具 %r 执行出错", call.name)
            return ToolResult(
                call=call, ok=False, error=f"工具执行出错：{type(e).__name__}: {e}"
            )

        return ToolResult(call=call, ok=True, data=_normalize(raw))

    def execute_all(self, calls: list[ToolCall], *, session_id: str = "") -> list[ToolResult]:
        """按顺序执行一批调用。

        串行而非并行：M0 的工具都是零权限、瞬时的，并行带来的复杂度
        （竞态、结果顺序）换不来收益。等真有慢工具（网络类）再说。
        """
        return [self.execute(call, session_id=session_id) for call in calls]


def _normalize(raw: Any) -> dict:
    """把插件返回值规整成 dict（ToolResult.data 的形状，§18.3）。

    插件可以返回 dict（结构化，如 {"weather": "晴"}），也可以返回 str
    （如 ping 的 "pong"）。非 dict 统一包成 {"result": ...}，回喂模型时
    序列化成 JSON 也整齐。
    """
    if isinstance(raw, dict):
        return raw
    return {"result": raw}
