"""配置加载与合并。

Config 类：五域加载 + 默认合并 + 原子写。
- 5 个域文件位于 data/config/ 下（见设计文档 §十三 / M0-02）
- load(domain): 读某个域，文件不存在写默认，存在时合并默认补缺
- get(key): 查配置
- save(): 原子写回（temp + os.replace）
"""

import copy
import json
import os
import tempfile
from pathlib import Path

from src.core.config.default import DEFAULT_CONFIG

# 默认配置目录：项目根下的 data/config/
# parents[0]=config/ parents[1]=core/ parents[2]=src/ parents[3]=项目根
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "config"

# 5 个域：域名 -> 文件名
DOMAINS = {
    "config": "config.json",
    "persona": "persona.json",
    "platforms": "platforms.json",
    "plugins": "plugins.json",
    "secrets": "secrets.json",
}

# 每个域对应的默认值来源（secrets 无默认，用空 dict）
DEFAULT_SOURCES = {
    "config": DEFAULT_CONFIG,
    # §14 定的取值是 text / markdown（"format 预留(text/markdown)"），
    # 与 fields.py 的选项保持一致——两处写不同的值面板会对不上。
    "persona": {"format": "markdown", "version": "v1"},
    # 平台开关。默认：Web 开、QQ 关（QQ 要另跑 SnowLuma 后端，
    # 没配的人不该多起一个端口）。键名与 fields.py 的 platforms 域一致。
    # 注：旧版键叫 "websocket"，_migrate_platform_keys 会把旧键并到 "web"。
    "platforms": {
        "web": {"enabled": True, "port": 8000},
        "qq": {"enabled": False, "host": "127.0.0.1", "port": 8866, "access_token": ""},
    },
    "plugins": {},
    "secrets": {},
}


def _migrate_platform_keys(user_data: dict) -> dict:
    """把旧配置里的 `websocket` 平台键并到新键 `web`（rename 之后的一次性兼容）。

    为什么需要：`adapter/websocket → web` 重命名把平台标识与配置键一起改了。
    老用户 `platforms.json` 里是 `websocket`——不迁移的话，`_deep_merge` 会让
    新键 `web` 取默认值（端口回 8000），而用户改过的值躺在孤儿旧键里被忽略。
    这里把旧键的值**并入**新键（新键已有的字段优先，不覆盖用户显式设过的）。

    只在 platforms 域、且确实含旧键时动作；纯函数，不改入参。
    """
    if not isinstance(user_data, dict):
        return user_data
    ws = user_data.get("websocket")
    if not isinstance(ws, dict):
        return user_data
    out = copy.deepcopy(user_data)
    out.pop("websocket", None)
    web = out.get("web")
    # 新键不存在 → 直接用旧键的值；存在 → 旧键补齐新键缺的字段（新键优先）
    if not isinstance(web, dict):
        out["web"] = ws
    else:
        for k, v in ws.items():
            web.setdefault(k, v)
    return out


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并：两边都是 dict 的键递归合并，其余直接覆盖。

    不修改入参，返回一个新字典。
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """配置管家：负责读取、查询、保存某个配置域。"""

    def __init__(self, domain: str = "config", data: dict | None = None, path: Path | None = None):
        self.domain = domain
        self.data: dict = data if data is not None else {}
        self.path: Path = path if path is not None else DEFAULT_DIR / DOMAINS[domain]

    @classmethod
    def load(cls, domain="config", path=None):
        """读取一个域，返回 Config 实例。

        - domain: 5 域之一（config/persona/platforms/plugins/secrets）
        - path: 可选，覆盖默认路径（测试用）
        - 文件不存在 -> 写默认值到文件，并返回默认
        - 文件存在 -> 读取并与默认值合并补缺
        """
        if domain not in DOMAINS:
            raise ValueError(f"未知配置域: {domain}")

        file_path = path if path else DEFAULT_DIR / DOMAINS[domain]
        default = DEFAULT_SOURCES[domain]

        if not file_path.exists():
            # 文件不存在：写入默认值
            data = copy.deepcopy(default)
            cls(domain=domain, data=data, path=file_path).save()
            return cls(domain=domain, data=data, path=file_path)

        with open(file_path, "r", encoding="utf-8") as f:
            user_data = json.load(f)

        # 旧键迁移：老配置里平台键叫 "websocket"，现在统一成 "web"。
        # 不迁移的话，用户改过的端口会被静默重置回默认（旧键成孤儿、新键取默认）。
        user_data = _migrate_platform_keys(user_data)

        merged = _deep_merge(default, user_data)
        return cls(domain=domain, data=merged, path=file_path)

    def get(self, key, default=None) -> dict:
        """查询某个 key 的配置值，不存在返回 default。"""
        return self.data.get(key, default)

    def save(self):
        """原子写回本域数据（temp + os.replace），防并发覆盖。"""
        # 确保目录存在
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 先写到临时文件，再替换，避免中途崩溃留下损坏文件
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)  # 原子替换
        except Exception:
            # 出错时清理临时文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
