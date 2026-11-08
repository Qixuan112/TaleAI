"""记忆检索（M1-07，含 M1-06 的 score 计算）。

v1 不做向量检索（§十九-7）：最近 N 条 + owner/session 过滤 + 固定 token
预算，按衰减分排序截断。先跑通"记得住 → 想得起"的闭环，相关性模型后置。

作用域（§19-9）：
- **私聊**：按 owner 过滤、跨会话取——验收①"隔天新会话答对"靠的就是它；
  同一个人的记忆不该因为换了会话就消失。
- **群聊**：额外按 session 过滤——群里的记忆是"这个群"的，不串到别的群。
- A 的记忆永远不进 B 的对话：owner 是第一道硬过滤。

score（§19-8，M1-06 正式接入遗忘时复用）：
    score = importance × 2^(-age_days / half_life)
age 现在用 `created_at`（提炼时间）。等 M1-06 给 curated 补 `event_ts`
后改成按事件时间衰减——计划类记忆的"年龄"才准。

last_access 回写不在检索里做：检索在**热路径**（每轮对话），写盘会拖慢
前台、还会让 curated.jsonl 每轮多一行。M1-06 用后台批量回写。
"""

import logging
import time
from typing import Callable

from core.config.loader import Config
from core.memory.store import CuratedStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_TOP_K = 20
DEFAULT_TOKEN_BUDGET = 2000
DEFAULT_HALF_LIFE_DAYS = 30.0


def score(entry: dict, now: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """衰减分：importance × 2^(-age_days / half_life)。"""
    try:
        importance = float(entry.get("importance") or 0)
    except (TypeError, ValueError):
        importance = 0.0
    try:
        created = float(entry.get("created_at") or now)
    except (TypeError, ValueError):
        created = now
    age_days = max(0.0, (now - created) / 86400.0)
    half_life = max(0.1, float(half_life_days))
    return importance * (2.0 ** (-age_days / half_life))


def _estimate_tokens(text: str) -> int:
    """粗估 token：中文约 1 字 1 token，英文更少。宁可高估，不撑爆上下文。"""
    return len(text)


def _memory_config() -> dict:
    return dict(Config.load("config").get("memory", {}))


class MemoryRetriever:
    """按 owner/session 取记忆，按衰减分排序并截断。"""

    def __init__(
        self,
        store: CuratedStore,
        clock: Callable[[], float] | None = None,
        config_reader: Callable[[], dict] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or time.time
        self._config_reader = config_reader or _memory_config

    def retrieve(
        self,
        owner: str,
        session_id: str = "",
        session_type: str = "",
        now: float | None = None,
    ) -> list[dict]:
        """返回要注入当前对话的记忆（已排序、已截断）。

        检索失败不抛：记忆是增强，不是主链路——回调方按空结果继续对话。
        """
        if not owner:
            return []

        memory = self._config_reader() or {}
        top_k = int(memory.get("retrieve_top_k", DEFAULT_TOP_K))
        budget = int(memory.get("retrieve_token_budget", DEFAULT_TOKEN_BUDGET))
        half_life = float(memory.get("half_life_days", DEFAULT_HALF_LIFE_DAYS))
        now = self._clock() if now is None else now

        try:
            entries = self._store.read_all(owner=owner, status="active")
        except Exception:
            logger.exception("读取记忆失败，本轮不注入（owner=%s）", owner)
            return []

        # 群聊是会话级的：同一个群里的记忆不串到别的群。
        # 私聊只按 owner 过滤（跨会话），这是验收①的前提。
        if session_type == "group" and session_id:
            entries = [e for e in entries if e.get("session_id") == session_id]

        scored = [(score(e, now, half_life), e) for e in entries]
        scored.sort(
            key=lambda pair: (
                pair[0],
                float(pair[1].get("created_at") or 0),
            ),
            reverse=True,
        )

        selected: list[dict] = []
        used = 0
        for _s, entry in scored:
            if len(selected) >= top_k:
                break
            content = str(entry.get("content") or "")
            cost = _estimate_tokens(content)
            if budget > 0:
                if cost > budget:
                    continue  # 单条就超预算：跳过，不截断内容（防过度压缩）
                if used + cost > budget:
                    break
            selected.append(entry)
            used += cost

        return selected
