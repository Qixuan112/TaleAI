"""config 模块测试 —— 这是说明书,照着实现 src/core/config/ 下的代码。

要实现的模块:
- src/core/config/default.py:  DEFAULT_CONFIG 字典,存默认配置
- src/core/config/loader.py:   load_config() 函数,加载配置并和默认值合并

M0 阶段配置只需要这几个字段(都来自设计文档 §1.6 / 附录 E):
- llm: 模型相关(provider / model / api_keys 数组)
- bot:  角色名称
"""

import os
import sys

# 让测试能找到 src 下的代码
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_default_config_has_required_fields():
    """默认配置必须包含 M0 需要的三个字段:llm、bot、platforms。"""
    from core.config.default import DEFAULT_CONFIG

    assert "llm" in DEFAULT_CONFIG
    assert "bot" in DEFAULT_CONFIG
    assert "platforms" in DEFAULT_CONFIG


def test_default_llm_config_shape():
    """llm 配置块要有 provider、model、api_keys 三个键,api_keys 是列表。"""
    from core.config.default import DEFAULT_CONFIG

    llm = DEFAULT_CONFIG["llm"]
    assert "provider" in llm
    assert "model" in llm
    assert "api_keys" in llm
    assert isinstance(llm["api_keys"], list)


def test_default_bot_name():
    """bot 配置里有名字,默认叫 初念。"""
    from core.config.default import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["bot"]["name"] == "初念"


def test_load_config_returns_defaults_when_no_file():
    """没有配置文件时,load_config() 返回默认配置。"""
    from core.config.loader import load_config

    cfg = load_config()
    assert cfg["bot"]["name"] == "初念"
    assert isinstance(cfg["llm"]["api_keys"], list)


def test_load_config_merges_user_file():
    """有配置文件时,用户配置覆盖默认配置,没写的字段保留默认值。"""
    import json
    import tempfile

    from core.config.loader import load_config

    # 造一个临时配置文件,只覆盖 bot 名字
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump({"bot": {"name": "测试名"}}, tmp)
    tmp.close()

    try:
        cfg = load_config(tmp.name)
        assert cfg["bot"]["name"] == "测试名"          # 用户值生效
        assert cfg["llm"]["provider"] is not None      # 默认值保留
    finally:
        os.unlink(tmp.name)
