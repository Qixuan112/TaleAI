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

from core.config.default import DEFAULT_CONFIG

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
    "persona": {"format": "md", "version": "v1"},
    "platforms": {},
    "plugins": {},
    "secrets": {},
}


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
