"""插件注册表：内置 + 外部，统一入口。

设计文档 §五 / §18.1 启动第 4 步 / §22。

三条关键约定：
- **工具即插件**：LLM 调工具走注册表统一入口，不认识任何具体插件实现。
- **工具 schema 即上下文**：注册表把工具汇总成原生 FC 的工具表，
  绝不手改 ChatLLM 的 prompt（§23 原则）。
- **可逆副作用**：卸载一个插件要撤销它做过的一切注册（§五 借鉴 DSH/Cordis），
  所以每次注册都必须知道"是哪个插件干的"——归属信息在加载上下文里。

内置优先：扫描顺序是 builtin → data/plugin，先到先得，因此同名冲突时
内置生效、外部跳过并告警（§五）。
"""

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.plugin.manifest import Manifest, ManifestError

logger = logging.getLogger(__name__)
# 库不该在应用没配日志时往 stderr 喷（lastResort 会把 warning 糊到用户脸上）
logger.addHandler(logging.NullHandler())

# registry.py 位于 src/core/plugin/ → parents[3] 是项目根
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
EXTERNAL_DIR = Path(__file__).resolve().parents[3] / "data" / "plugin"


@dataclass
class ToolSpec:
    """一个已注册的工具。

    plugin 字段是卸载的关键：unregister(name) 靠它反查要撤销哪些注册。
    """

    name: str
    schema: dict  # {"description": ..., "parameters": {JSON Schema}}
    handler: Callable[..., Any]
    plugin: str  # 注册它的插件名


@dataclass
class PluginRecord:
    """一个已加载插件的账。"""

    manifest: Manifest
    source: str  # builtin / external
    module_name: str  # 记下来，卸载时从 sys.modules 摘掉


class _LoadToken:
    """插件加载上下文的令牌。

    为什么用对象而不是裸字符串：注册归属靠"当前正在加载谁"，若用可变字符串，
    插件代码可以 `registry._loading = "ping"` 把工具伪装成别的插件的——卸载时
    就撤销不干净（PR #10）。令牌是私有类实例，插件要伪造得先拿到这个类，
    门槛高得多。注意这仍是**信任模型不是沙箱**（同 guard.py 的声明）。
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class PluginRegistry:
    """内置 + 外部统一注册表。单例（§22）。

    单例的理由：插件在 import 时通过装饰器注册，装饰器需要一个全局可达的
    落点；同时"工具表"全进程只该有一份，多个实例会导致工具表不一致。
    """

    _instance: "PluginRegistry | None" = None

    def __new__(cls) -> "PluginRegistry":
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._tools: dict[str, ToolSpec] = {}
            instance._plugins: dict[str, PluginRecord] = {}
            # 正在加载的插件令牌（见 _LoadToken）。装饰器靠它给注册打归属，
            # 卸载才能撤销干净。None = 当前不在插件加载上下文里。
            instance._loading: _LoadToken | None = None
            # 启停覆盖：{插件名: bool}。不在表里就按来源取默认（内置启用/外部停用）。
            # 放在注册表而不是守卫里：工具表过滤（tool_schemas）和执行前硬拦
            # （守卫）都要看"哪些插件是启用的"，只有一处真相才不会两边打架。
            instance._enabled_override: dict[str, bool] = {}
            cls._instance = instance
        return cls._instance

    # ---------- 启停 ----------

    def enable(self, plugin_name: str) -> None:
        """启用插件（权限仍需另外授予——启停与授权是两件事）。"""
        self._enabled_override[plugin_name] = True

    def disable(self, plugin_name: str) -> None:
        """停用插件：它的所有工具立即不进工具表、也不可调用。"""
        self._enabled_override[plugin_name] = False

    def plugin_enabled(self, plugin_name: str) -> bool:
        """内置默认启用、外部默认停用（§19-11）；显式覆盖优先。"""
        if plugin_name in self._enabled_override:
            return self._enabled_override[plugin_name]
        record = self._plugins.get(plugin_name)
        if record is None:
            return False  # 插件都没加载，谈不上启用
        return record.source == "builtin"

    # ---------- 注册 ----------

    def register_tool(
        self, name: str, schema: dict | None = None, handler: Callable | None = None
    ) -> bool:
        """登记一个工具。返回是否真的注册成功。

        必须在插件加载上下文里调用（由装饰器代劳）——没有归属的注册
        无法被卸载，等于给"可逆"留个洞。
        """
        if self._loading is None:
            raise RuntimeError(
                f"工具 {name!r} 不在插件加载上下文中，拒绝注册"
                "（无归属的注册无法被 unregister 撤销）"
            )
        if handler is None:
            raise ValueError(f"工具 {name!r} 没有 handler")
        if not name:
            raise ValueError("工具名不能为空")

        # 同名工具：先到先得（builtin 先扫），后到的跳过并告警
        existing = self._tools.get(name)
        if existing is not None:
            logger.warning(
                "工具名冲突：%r 已由插件 %r 注册，%r 的注册被跳过",
                name, existing.plugin, self._loading.name,
            )
            return False

        self._tools[name] = ToolSpec(
            name=name, schema=schema or {}, handler=handler, plugin=self._loading.name
        )
        return True

    def unregister(self, plugin_name: str) -> bool:
        """卸载一个插件：撤销它注册过的全部工具。返回是否确实卸掉了东西。

        这是 §五 的"可逆副作用"——卸载后注册表应回到它没来过时的样子，
        再扫一次还能重新装上（所以要从 sys.modules 摘掉模块，
        否则 import 命中缓存，装饰器不会再跑一遍）。
        """
        record = self._plugins.pop(plugin_name, None)
        if record is None:
            return False

        for tool_name in [n for n, t in self._tools.items() if t.plugin == plugin_name]:
            del self._tools[tool_name]

        # 启停覆盖也一并清掉，避免插件重装后继承上一个残留的开关
        self._enabled_override.pop(plugin_name, None)
        sys.modules.pop(record.module_name, None)
        return True

    # ---------- 查询 ----------

    def tool_schemas(self, *, enabled_only: bool = True) -> list[dict]:
        """汇总成原生 FC 的工具表（§五：工具 schema 即上下文）。

        顺序 = 扫描顺序（内置在前，各自按目录名排序），是确定的。

        enabled_only=True（默认）：**只导出已启用插件的工具**。未启用插件的
        工具不进模型工具表——否则模型会看到一个它调不动（会被守卫拦）的工具，
        白占上下文，且重复的工具名会让整个请求非法（PR #10）。判定见
        plugin_enabled（内置默认启用 / 外部默认停用）。传 False 可拿全量（排障用）。

        名字只认**注册名**，丢掉 schema 里的 `name` 字段：注册表以注册名为准
        （冲突判定、卸载反查都用它），schema 里再写一个 name 会把它盖掉，
        外部插件就能用诱导性的 schema.name 顶替内置工具名（PR #10）。
        """
        schemas: list[dict] = []
        for t in self._tools.values():
            if enabled_only and not self.plugin_enabled(t.plugin):
                continue
            # schema 里除 name 外的字段照带（description / parameters）；name
            # 一律用注册名，schema 自己的 name 丢弃。
            rest = {k: v for k, v in t.schema.items() if k != "name"}
            schemas.append({"type": "function", "function": {"name": t.name, **rest}})
        return schemas

    def tools(self) -> dict[str, ToolSpec]:
        """工具名 → ToolSpec（执行器用，M0-07 接入）。"""
        return dict(self._tools)

    def plugins(self) -> dict[str, PluginRecord]:
        """已加载插件名 → 记录。"""
        return dict(self._plugins)

    # ---------- 扫描加载 ----------

    def scan(
        self,
        builtin_dir: Path | str | None = None,
        external_dir: Path | str | None = None,
    ) -> list[str]:
        """扫内置与外部目录，加载插件。返回本次成功加载的插件名。

        单个插件坏掉不该拖垮启动：声明不合法或 import 抛异常都只告警跳过
        （外部插件是用户手放的，不能假设它一定正确）。
        """
        loaded: list[str] = []
        targets = (
            (Path(builtin_dir) if builtin_dir else BUILTIN_DIR, "builtin"),
            (Path(external_dir) if external_dir else EXTERNAL_DIR, "external"),
        )
        for directory, source in targets:
            if not directory.is_dir():
                continue  # data/plugin 不存在是正常的（用户没装外部插件）
            for entry in sorted(directory.iterdir()):
                if not entry.is_dir():
                    continue
                name = self._load_plugin(entry, source)
                if name is not None:
                    loaded.append(name)
        return loaded

    def _load_plugin(self, directory: Path, source: str) -> str | None:
        """加载单个插件目录。成功返回插件名，跳过返回 None。"""
        try:
            manifest = Manifest.load(directory)
        except ManifestError as e:
            logger.warning("插件 %s 声明不合法，跳过：%s", directory.name, e)
            return None

        existing = self._plugins.get(manifest.name)
        if existing is not None:
            if existing.manifest.path == manifest.path:
                # 同一个目录被重复扫描（比如卸载后再扫一次）：已经装过了，静默跳过
                logger.debug("插件 %r 已加载，跳过重复扫描", manifest.name)
            else:
                # 扫描顺序 builtin → external，所以先到的必是内置
                logger.warning(
                    "插件名冲突：%r 已由 %s（%s）提供，%s 的 %s 被跳过",
                    manifest.name, existing.source, existing.manifest.path,
                    source, directory,
                )
            return None

        main_py = directory / "main.py"
        if not main_py.is_file():
            logger.warning("插件 %r 缺少 main.py，跳过：%s", manifest.name, directory)
            return None

        # 模块名加前缀避免跟真实包撞名；非标识符字符换成下划线
        module_name = "taleai_plugin_" + "".join(
            c if c.isalnum() or c == "_" else "_" for c in manifest.name
        )

        # 加载前拍一张工具表快照：失败时要回到"这个插件没来过"的状态。
        # 为什么用快照而不是"删掉 plugin==name 的工具"——插件代码能篡改
        # registry（它就在同进程里），把已注册的别的工具删掉、或把自己的工具
        # 挂到别的 plugin 名下；快照回滚能把这类改动一并抹平（PR #10）。
        snapshot = dict(self._tools)
        self._loading = _LoadToken(manifest.name)
        try:
            spec = importlib.util.spec_from_file_location(module_name, main_py)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法为 {main_py} 建立模块规格")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except BaseException as e:
            # 插件代码是外来的，坏了只跳过，不能让启动挂掉。
            # 捕 BaseException 而不是 Exception：插件顶层 `sys.exit()` 抛的
            # SystemExit、或协程里的 CancelledError，都不是 Exception 子类，
            # 用 except Exception 会让它们穿过去结束整个进程 / 拆掉前台循环
            # （PR #10 已复现进程被 sys.exit 带走）。
            # 唯一例外是 KeyboardInterrupt——用户主动 Ctrl-C 该照常退出。
            if isinstance(e, KeyboardInterrupt):
                raise
            logger.exception("插件 %r 加载失败，已跳过", manifest.name)
            sys.modules.pop(module_name, None)
            self._tools = snapshot  # 回到加载前的工具表，不留半拉状态
            return None
        finally:
            self._loading = None

        self._plugins[manifest.name] = PluginRecord(
            manifest=manifest, source=source, module_name=module_name
        )
        return manifest.name

    # ---------- 测试辅助 ----------

    def reset(self) -> None:
        """清空全部注册与已加载插件（测试隔离用；生产不该调用）。"""
        for name in list(self._plugins):
            self.unregister(name)
        self._tools.clear()
        self._plugins.clear()
        self._enabled_override.clear()
        self._loading = None


class _Register:
    """插件用的装饰器入口（§五：register.tool(name, schema)）。

    插件写：

        from core.plugin.registry import register

        @register.tool(name="ping", schema={...})
        def ping() -> str: ...
    """

    @staticmethod
    def tool(name: str, schema: dict | None = None):
        """装饰器：把一个函数登记成工具。"""

        def decorate(fn: Callable) -> Callable:
            PluginRegistry().register_tool(name=name, schema=schema, handler=fn)
            return fn

        return decorate


# 模块级门面：插件 import 它，不必自己拿单例
register = _Register()
