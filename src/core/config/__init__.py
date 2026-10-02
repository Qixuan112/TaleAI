"""TaleAI 配置模块。

提供默认配置（default）与配置加载合并（loader）两个子模块。
"""

from core.config.default import DEFAULT_CONFIG
from core.config.loader import Config

__all__ = ["DEFAULT_CONFIG", "Config"]
