"""TaleAI 默认配置。

M0 阶段只需要 llm / bot / platforms 三块（见设计文档 §1.6 配置选型）。
"""

DEFAULT_CONFIG = {
    # 模型相关：provider / model / api_keys
    "llm": {
        "provider": "openai",  # 占位值，真实接入时再改；不能为 None
        "model": "gpt-4o-mini",  # 占位值
        "api_keys": [],  # 必须是列表，多 key 可扩展
    },
    # 角色配置
    "bot": {
        "name": "初念",
    },
    # 平台接入（M0 留空即可，YAGNI）
    "platforms": {},
}
