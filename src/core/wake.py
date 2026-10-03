"""关键词唤醒：群里只有叫它才回（UX-03）。

**问题**：QQ 群里消息很多，逐条回复 = 刷屏 + 烧钱 + 被踢。需要一道门：
只有"被叫到"的消息才交给模型。

**"被叫到"有两个来源，合起来判定**：
1. 平台事实——被 @ 了（QQ 群聊适配器认识 bot_id，知道有没有 @ 自己）
2. 关键词——正文里含唤醒词（"塔利，在吗"）

第 1 个是平台知识（只有适配器懂），第 2 个是跨平台配置（用户在设置页写）。
所以适配器只负责把第 1 个的**结论**标进 `Message.addressed`，两个来源在
`handle_message` 汇合成这道门——适配器不认识"唤醒词"这回事（§22 import 单向）。

**范围**：默认只约束群聊（`scope="group"`）。WebUI 打开就是找它、QQ 私聊
是 1:1，都不该逼着用户每次喊名字。`scope="all"` 可扩到所有会话，`off` 关闭。
"""

from dataclasses import dataclass

#: 默认唤醒词。默认叫名字——用户在设置页可改、可加。
DEFAULT_WORDS: tuple[str, ...] = ("塔利",)

#: 唤醒范围取值：group(仅群聊) / all(所有会话) / off(关闭)
_SCOPES = ("group", "all", "off")


@dataclass(frozen=True)
class WakePolicy:
    """唤醒门的策略：什么范围生效 + 什么算"在叫它"。"""

    words: tuple[str, ...] = DEFAULT_WORDS
    scope: str = "group"  # group / all / off

    def gates(self, session_type: str) -> bool:
        """这条会话的消息要不要过唤醒门。

        - off：不过门（所有消息都回，退化成"没有这个功能"）
        - all：都过门
        - group（默认）：只有群聊过门——私聊/WebUI 直接回，不逼用户喊名字
        """
        if self.scope == "off":
            return False
        if self.scope == "all":
            return True
        return session_type == "group"

    def is_woken(self, content: str, addressed: bool | None) -> bool:
        """这条消息算不算在叫它。

        addressed 是适配器给的平台结论（None = 没判定）：
        - True       → 确认被 @ 了，开口（不再看关键词）
        - 其余       → 看正文里有没有唤醒词

        关键词用**子串包含**判断、且**不剥离**（命中即原样送给模型）——
        比"剥掉名字再喂"简单，也少一处正文改写（用户定）。
        words 为空 = 没有关键词通道，此时群里只能靠 @ 叫它（returns False）。
        """
        if addressed:
            return True
        return any(w in content for w in self.words)


def load_wake_policy() -> WakePolicy:
    """从 config 域读唤醒配置，缺失/出错一律回默认策略（不能因为读配置失败就不回消息）。"""
    from core.config.loader import Config

    try:
        cfg = Config.load("config").get("wake", {}) or {}
    except Exception:
        return WakePolicy()

    words = cfg.get("words", list(DEFAULT_WORDS))
    if isinstance(words, str):  # 面板用逗号分隔的文本框；按逗号拆（help 里写的就是这个）
        words = words.split(",")
    words = tuple(str(w).strip() for w in words if str(w).strip())

    scope = str(cfg.get("scope", "group"))
    if scope not in _SCOPES:
        scope = "group"
    return WakePolicy(words=words, scope=scope)
