"""插件系统：目录即插件（manifest.json + main.py），装饰器注册。

模块划分（§三 / §五）：
- manifest.py  插件声明格式与校验
- registry.py  内置 + 外部统一注册表（单例），汇总工具表
- guard.py     权限执行前硬拦（M0-06）
- builtin/     内置插件，随代码发布
"""

from core.plugin.manifest import Manifest, ManifestError
from core.plugin.registry import PluginRegistry, register

__all__ = ["Manifest", "ManifestError", "PluginRegistry", "register"]
