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
            # 正在加载的插件名。装饰器靠它给注册打归属，卸载才能撤销干净。
            instance._loading: str | None = None
            cls._instance = instance
        return cls._instance

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
                name, existing.plugin, self._loading,
            )
            return False

        self._tools[name] = ToolSpec(
            name=name, schema=schema or {}, handler=handler, plugin=self._loading
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

        sys.modules.pop(record.module_name, None)
        return True

    # ---------- 查询 ----------

    def tool_schemas(self) -> list[dict]:
        """汇总成原生 FC 的工具表（§五：工具 schema 即上下文）。

        顺序 = 扫描顺序（内置在前，各自按目录名排序），是确定的。
        """
        return [
            {
                "type": "function",
                "function": {"name": t.name, **t.schema},
            }
            for t in self._tools.values()
        ]

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

        self._loading = manifest.name
        try:
            spec = importlib.util.spec_from_file_location(module_name, main_py)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法为 {main_py} 建立模块规格")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:
            # 插件代码是外来的，坏了只跳过，不能让启动挂掉
            logger.exception("插件 %r 加载失败，已跳过", manifest.name)
            sys.modules.pop(module_name, None)
            # 清掉它加载途中已经注册的工具，别留半拉的
            for tool_name in [
                n for n, t in self._tools.items() if t.plugin == manifest.name
            ]:
                del self._tools[tool_name]
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
