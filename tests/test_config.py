"""config 模块测试 —— 说明 Config 类的行为。

模块:
- src/core/config/default.py:  DEFAULT_CONFIG 字典,存默认配置
- src/core/config/loader.py:   Config 类,五域加载 + 默认合并 + 原子写
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


def test_load_config_returns_defaults_when_no_file(tmp_path):
    """没有配置文件时,Config.load() 写入并返回默认配置。"""
    from core.config.loader import Config

    path = tmp_path / "config.json"
    cfg = Config.load("config", path=path)
    assert cfg.get("bot", {}).get("name") == "初念"
    assert isinstance(cfg.get("llm", {}).get("api_keys"), list)


def test_load_config_merges_user_file(tmp_path):
    """有配置文件时,用户配置覆盖默认配置,没写的字段保留默认值。"""
    import json

    from core.config.loader import Config

    # 造一个临时配置文件,只覆盖 bot 名字
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bot": {"name": "测试名"}}), encoding="utf-8")

    cfg = Config.load("config", path=path)
    assert cfg.get("bot", {}).get("name") == "测试名"    # 用户值生效
    assert cfg.get("llm", {}).get("provider") is not None  # 默认值保留


def test_config_unknown_domain(tmp_path):
    """未知配置域应抛 ValueError。"""
    from core.config.loader import Config

    try:
        Config.load("nope", path=tmp_path / "x.json")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass


def test_config_save_and_reload(tmp_path):
    """save() 原子写入,再次 load 能读回。"""
    import json

    from core.config.loader import Config

    path = tmp_path / "config.json"
    cfg = Config.load("config", path=path)
    cfg.data["bot"] = {"name": "改名"}
    cfg.save()

    # 重新加载应读到改成后的内容
    cfg2 = Config.load("config", path=path)
    assert cfg2.get("bot", {}).get("name") == "改名"
