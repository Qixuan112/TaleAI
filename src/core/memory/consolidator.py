"""记忆整合与遗忘（M1-06）。

consolidate 的两个职责（§十一 / §十九-8）：
1. **整合**：只吃 curated 的增量，不重算历史。本版先实现**确定性遗忘判定**；
   语义合并（判断"两条记忆是否同一件事"）需要低频 LLM，等评测集证明有必要
   再接——现在硬做只会引入新的不稳。（触发条件见
   docs/temp/评估-无向量检索与关系网.md）
2. **遗忘**：软衰减分数低于阈值且够老 → 追加墓碑，status=forgotten。
   importance ≥ importance_never_forget（默认 4）永不自动遗忘。

两条铁律：
- **遗忘 ≠ 删除**：events 永存，curated 只追加墓碑，可审计、可恢复。
- **禁止"总结的总结"**：输入只有 curated 条目本身；注入用的临时视图绝不回流。

衰减公式（§19-8）：
    score = importance × 2^(-age_days / half_life)
age 目前按 `created_at`（提炼时间）算。M1-06 之后若要按事件时间算，
需要 extract 落 `event_ts`——那是下一步，不在本版伪造。
"""

import logging
import time
from typing import Callable

from core.config.loader import Config
from core.memory.retriever import score
from core.memory.store import CuratedStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_HALF_LIFE_DAYS = 2.0
DEFAULT_FORGET_THRESHOLD = 0.5
DEFAULT_MIN_AGE_DAYS = 30.0
DEFAULT_NEVER_FORGET = 4


def _memory_config() -> dict:
    return dict(Config.load("config").get("memory", {}))


class MemoryConsolidator:
    """定时扫描 active 记忆，给该忘的追加墓碑。"""

    def __init__(
        self,
        store: CuratedStore,
        clock: Callable[[], float] | None = None,
        config_reader: Callable[[], dict] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or time.time
        self._config_reader = config_reader or _memory_config

    def run(self, now: float | None = None) -> dict:
        """跑一轮遗忘判定，返回 {"checked": n, "forgotten": [id, ...]}。

        单条失败不整体回滚：已经追加的墓碑是合法事实，下一轮会跳过它
        （status 已是 forgotten）。异常只记日志，不炸后台 worker。
        """
        memory = self._config_reader() or {}
        half_life = float(memory.get("half_life_days", DEFAULT_HALF_LIFE_DAYS))
        threshold = float(memory.get("forget_threshold", DEFAULT_FORGET_THRESHOLD))
        min_age_days = float(
            memory.get("min_age_for_forget_days", DEFAULT_MIN_AGE_DAYS)
        )
        never_forget = int(
            memory.get("importance_never_forget", DEFAULT_NEVER_FORGET)
        )
        now = self._clock() if now is None else now

        try:
            entries = self._store.read_all(status="active")
        except Exception:
            logger.exception("读取记忆失败，本轮跳过整合")
            return {"checked": 0, "forgotten": []}

        forgotten: list[str] = []
        for entry in entries:
            try:
                importance = int(entry.get("importance") or 0)
                created = float(entry.get("created_at") or now)
            except (TypeError, ValueError):
                continue

            # importance 高的记忆永不自动遗忘（用户的身份级信息不能被衰减吃掉）
            if importance >= never_forget:
                continue

            age_days = max(0.0, (now - created) / 86400.0)
            if age_days <= min_age_days:
                continue
            if score(entry, now, half_life) >= threshold:
                continue

            entry_id = str(entry.get("id") or "")
            if not entry_id:
                continue
            try:
                self._store.append_tombstone(entry_id, reason="decay")
                forgotten.append(entry_id)
            except Exception:
                logger.exception("追加遗忘墓碑失败：id=%s", entry_id)

        if forgotten:
            logger.info("consolidate：遗忘 %d 条低重要度旧记忆", len(forgotten))
        return {"checked": len(entries), "forgotten": forgotten}
