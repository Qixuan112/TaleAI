"""TaleAI 默认配置。

M0 阶段只需要 llm / bot / platforms 三块（见设计文档 §1.6 配置选型）。
"""

DEFAULT_CONFIG = {
    # 模型相关：provider / base_url / model / api_keys / 历史窗口
    #
    # base_url / model 故意留空：用哪家服务商、哪个模型是「用户配置」而非「项目默认值」
    # （设计文档 §十三：data/config/ 才是用户的域，default.py 只做兜底）。
    # 实际值写在 data/config/config.json；缺失时 ChatLLM 启动即报错，
    # 避免默默打到一个没订阅的网关、拿到难懂的 403。
    "llm": {
        "provider": "openai-compatible",  # OpenAI 兼容服务
        "base_url": "",  # 服务商网关（必须是 /v1 结尾的 API 地址）
        "model": "",  # 模型名
        "api_keys": [],  # 真正的 key 放 secrets.json
        "history_max_turns": 40,  # 弹簧窗口：超过这个回合数才裁剪
        "history_trim_to": 10,  # 裁剪后保留的回合数
    },
    # 角色配置
    "bot": {
        "name": "初念",
    },
    # 平台接入（M0 留空即可，YAGNI）
    "platforms": {},
}
