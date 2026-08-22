"""TaleAI 默认配置。

M0 阶段只需要 llm / bot / platforms 三块（见设计文档 §1.6 配置选型）。
"""

DEFAULT_CONFIG = {
    # 模型相关：provider / base_url / model / api_key
    "llm": {
        "provider": "openai-compatible",  # OpenAI 兼容服务
        "base_url": "https://opencode.ai/zen/go/v1",  # 服务商网关（必须是 /v1 结尾的 API 地址）
        "model": "deepseek-v4-flash",  # 模型名
        "api_keys": [],  # 真正的 key 放 secrets.json
    },
    # 角色配置
    "bot": {
        "name": "初念",
    },
    # 平台接入（M0 留空即可，YAGNI）
    "platforms": {},
}
