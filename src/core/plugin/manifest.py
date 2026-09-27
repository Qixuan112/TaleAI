"""插件声明格式：manifest.json 的解析与校验。

设计文档 §五：插件 = 一个目录（manifest.json + main.py），装饰器注册。
本模块只管"声明"这一半，注册与加载在 registry.py。

permissions 是「给用户看的说明书」+「执行前硬拦的依据」（§19-11）：
v1 的拦截只针对 **LLM 发起的工具调用**，不约束插件代码本身。
这是信任模型，不是沙箱——文档要求写明，不许给人安全错觉。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"


class ManifestError(ValueError):
    """manifest 缺失、格式错或字段非法。"""


@dataclass
class Manifest:
    """一份插件声明。

    name 是插件的身份（同名冲突判定、卸载反查都用它），以 manifest 里
    写的为准——不要求跟目录名一致，目录只是发现单位。
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    permissions: list[str] = field(default_factory=list)
    path: Path | None = None  # 插件目录，便于排障时定位

    @classmethod
    def load(cls, plugin_dir: Path | str) -> "Manifest":
        """从插件目录读 manifest.json 并校验。

        校验只在"读外部文件"这个边界上做——manifest 可能是用户手写的，
        错误要在这里拦成可读的 ManifestError，而不是让后面某个环节
        以奇怪的姿势炸掉。
        """
        directory = Path(plugin_dir)
        manifest_path = directory / MANIFEST_NAME

        if not manifest_path.is_file():
            raise ManifestError(f"缺少 {MANIFEST_NAME}: {manifest_path}")

        try:
            # encoding 显式 utf-8：中文 Windows 默认 gbk，描述里写中文会读坏
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ManifestError(f"{MANIFEST_NAME} 不是合法 JSON: {e}") from e

        if not isinstance(raw, dict):
            raise ManifestError(f"{MANIFEST_NAME} 顶层必须是对象，实际是 {type(raw).__name__}")

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(f"{MANIFEST_NAME} 缺少非空的 name 字段")

        version = raw.get("version", "0.0.0")
        if not isinstance(version, str):
            raise ManifestError("version 必须是字符串")

        description = raw.get("description", "")
        if not isinstance(description, str):
            raise ManifestError("description 必须是字符串")

        permissions = raw.get("permissions", [])
        if not isinstance(permissions, list) or not all(
            isinstance(p, str) for p in permissions
        ):
            raise ManifestError("permissions 必须是字符串列表，例如 [\"network\"]")

        return cls(
            name=name.strip(),
            version=version,
            description=description,
            permissions=list(permissions),
            path=directory,
        )
