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
        # 模型上下文窗口：每次请求往回带的历史。
        # 保留最新 history_keep_messages 条，再往前多带 history_lookback_extra
        # 条旧消息（边界不硬切，上下文更连贯）。两个都按「条」算（每条消息=1）。
        "history_keep_messages": 10,
        "history_lookback_extra": 5,
        "max_agent_steps": 3,  # FC 循环上限（§19-2：全局 3 轮不变）
    },
    # 角色配置
    "bot": {
        "name": "塔利",
    },
    # 关键词唤醒（UX-03）：群里只有"叫它"的消息才回，其余的存进历史但不回。
    #   words = 唤醒词列表（默认叫名字），正文里**包含**任一即算叫它、不剥离
    #   scope = 生效范围：group(仅群聊，默认) / all(所有会话) / off(关闭)
    # 私聊与 WebUI 默认不受影响（scope=group）——打开就是在找它，不必喊名字。
    "wake": {
        "words": ["塔利"],
        "scope": "group",
    },
    # 平台接入（M0 留空即可，YAGNI）
    "platforms": {},
    # 记忆系统配置（M1 新增）
    "memory": {
        # 衰减参数
        "half_life_days": 30,  # 半衰期：30 天后 importance 权重减半
        "forget_threshold": 0.5,  # 遗忘阈值：score < 0.5 且超龄 → 打墓碑
        "min_age_for_forget_days": 30,  # 最小遗忘年龄：新记忆不会被立刻忘掉
        "importance_never_forget": 4,  # importance ≥ 4 永不自动遗忘
        # 触发参数
        "extract_idle_minutes": 10,  # 会话静默 N 分钟后触发 extract
        "consolidate_interval_hours": 6,  # 定时 consolidate 间隔
        "consolidate_event_threshold": 50,  # 新事件 ≥ N 条触发 consolidate
        # 检索参数
        "retrieve_top_k": 20,  # 检索最多返回 N 条记忆
        "retrieve_token_budget": 2000,  # 记忆注入的 token 预算
    },
}
