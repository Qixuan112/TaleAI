"""权限守卫：工具执行前的硬拦。

设计文档 §十五 / §3.6 / §19-11 / §22。

权限三件套（§3.6）：
1. 插件在 manifest 里声明自己需要什么权限（network / file_read / file_write / exec）
2. **执行前由本模块用纯代码硬查**——不经过 LLM、不依赖 LLM 的任何输出
3. 模型的工具列表里根本没有"变更权限"这个工具

为什么不让模型判断权限（§3.6）：防提示词注入、防模型自授权。权限判断是
确定性工程，交给模型只会多一个失败点和一条攻击路径。

⚠️ 这是信任模型，不是沙箱（§19-11，文档明确要求写明，不许给人安全错觉）：
本守卫只拦 **LLM 发起的工具调用**，不约束插件代码本身——插件里写
`import socket` 就能直接联网，守卫拦不住。v1 接受这个边界；不可信插件
需要后期进程级隔离（§5 局限性）。
"""

import logging

from core.plugin.registry import PluginRegistry

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class PermissionGuard:
    """执行前权限硬拦。纯代码，不经过 LLM。

    默认姿态是 deny（§十五）：
    - 插件声明的权限默认**都不授予**，要由用户显式 grant。
    - 外部插件默认**停用**（§19-11：新装外部插件默认停用，用户确认后启用），
      内置插件默认启用（随代码发布、经过评审）。
    """

    def __init__(self, registry: PluginRegistry | None = None) -> None:
        self._registry = registry if registry is not None else PluginRegistry()
        # 启停状态**由注册表持有**（tool_schemas 过滤和这里都要看它，只有一处
        # 真相才不会两边打架）。守卫只保留"已授予的权限"。
        # 已授予的权限：{插件名: {权限, ...}}。默认空 = 默认 deny
        self._granted: dict[str, set[str]] = {}

    # ---------- 用户侧（面板 / 测试）改状态 ----------

    def enable(self, plugin_name: str) -> None:
        """启用插件（权限仍需单独 grant）。委派给注册表，保持单一真相。"""
        self._registry.enable(plugin_name)

    def disable(self, plugin_name: str) -> None:
        """停用插件：它的所有工具立即不可调用（也不进模型工具表）。"""
        self._registry.disable(plugin_name)

    def grant(self, plugin_name: str, *permissions: str) -> None:
        """授予插件权限。这是唯一能开权限的入口——不在工具表里，模型够不着。"""
        self._granted.setdefault(plugin_name, set()).update(permissions)

    def revoke(self, plugin_name: str, *permissions: str) -> None:
        """收回权限。"""
        current = self._granted.get(plugin_name)
        if current is None:
            return
        current.difference_update(permissions)

    # ---------- 检查 ----------

    def check(self, tool_name: str) -> bool:
        """这个工具现在能不能执行（§22 的签名）。"""
        allowed, _ = self._evaluate(tool_name)
        return allowed

    def deny_reason(self, tool_name: str) -> str | None:
        """被拦的原因；允许时返回 None。

        执行器拿它填 ToolResult.error——给模型一句能转述给用户的话，
        而不是让它面对一个没有解释的失败。
        """
        allowed, reason = self._evaluate(tool_name)
        return None if allowed else reason

    def _evaluate(self, tool_name: str) -> tuple[bool, str]:
        tools = self._registry.tools()
        tool = tools.get(tool_name)
        if tool is None:
            return False, f"没有名为 {tool_name!r} 的工具"

        if not self._registry.plugin_enabled(tool.plugin):
            return False, f"插件 {tool.plugin!r} 未启用，无法调用 {tool_name!r}"

        missing = self._missing_permissions(tool.plugin)
        if missing:
            return False, (
                f"插件 {tool.plugin!r} 未获授权限 {','.join(missing)}，"
                f"无法调用 {tool_name!r}"
            )

        return True, ""

    def _missing_permissions(self, plugin_name: str) -> list[str]:
        """插件声明了但还没被授予的权限。"""
        record = self._registry.plugins().get(plugin_name)
        if record is None:
            return []
        granted = self._granted.get(plugin_name, set())
        return [p for p in record.manifest.permissions if p not in granted]
