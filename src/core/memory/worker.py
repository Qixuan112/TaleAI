"""MemoryWorker：后台记忆提炼（M1-05）。

模型（§18.4 定论）：
- **队列只是门铃，任务本体从磁盘取**。总线 handler 只做 put_nowait 叫醒；
  worker 被叫醒后自己按游标从 events.jsonl 取批，游标落在 SQLite
  （session_cursors 表）。队列丢了 / 进程重启 → 按游标重扫，幂等。
- 没有「会话结束」信号：用户只是不说话了，连接可能还开着。所以靠 **tick**
  自己扫 sessions.last_active：静默超过 extract_idle_minutes 且有新事件 →
  extract。`session.closed` 事件到达时立刻处理该会话（强制，不等静默）。
- consolidate（6h / 新事件 ≥50）是 M1-06 的事，这里不实现。

并发：worker 跑在同一个 asyncio 事件循环里，SQLite 写是短同步调用；三套存储
（SessionStore / EventLog / CuratedStore）各有自己的锁，没有任何路径同时持有
两把锁，因此不存在锁顺序死锁。§18.5 的「写盘 to_thread 单写线程」是后续
整体改造，不在这一步引入（LLM 调用 await 期间本就会让出事件循环）。
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from core.config.loader import Config
from core.event_bus import Event, EventBus
from core.llm.context import SessionContext
from core.llm.memory_llm import MemoryLLM
from core.memory.consolidator import MemoryConsolidator
from core.memory.store import EventLog
from core.session.store import SessionStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 多久扫一次会话活跃度。静默阈值默认 10 分钟，30 秒的扫描粒度足够。
DEFAULT_TICK_SECONDS = 30.0

#: 单次 extract 最多取多少条事件。超出的下一轮按游标继续（不丢、不重）。
EXTRACT_BATCH = 200

#: 门铃队列上限。满了就丢门铃——事实源在磁盘，下一轮 tick 照样会取到。
_DOORBELL_MAXSIZE = 100


def _memory_config() -> dict:
    """现读 memory 配置域（热重载：改完下一轮 tick 生效）。"""
    return dict(Config.load("config").get("memory", {}))


class MemoryWorker:
    """常驻 asyncio 任务：静默 / session.closed 触发 extract。"""

    def __init__(
        self,
        store: SessionStore,
        eventlog: EventLog,
        llm: MemoryLLM,
        bus: EventBus | None = None,
        config_reader: Callable[[], dict] | None = None,
        clock: Callable[[], float] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        consolidator: MemoryConsolidator | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._store = store
        self._eventlog = eventlog
        self._llm = llm
        self._config_reader = config_reader or _memory_config
        self._clock = clock or time.time
        self.tick_seconds = tick_seconds

        # consolidate（M1-06）：默认从 MemoryLLM 的 store 构造——worker 本来就
        # 管着它的生命周期，不必让 main 多传一份 curated。测试给假 llm 时没有
        # store，就不开整合（保持旧行为）。
        curated = getattr(llm, "store", None)
        self._consolidator = consolidator
        if self._consolidator is None and curated is not None:
            self._consolidator = MemoryConsolidator(
                curated, clock=self._clock, config_reader=self._config_reader
            )
        if state_path is None and curated is not None:
            state_path = Path(curated.filepath).parent / "consolidate_state.json"
        self._state_path = Path(state_path) if state_path is not None else None

        # 门铃：只装 session_id，不装任务本体（§18.4）
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_DOORBELL_MAXSIZE)
        # session.closed 的会话：不等静默，下一次 tick 立刻提炼
        self._forced: set[str] = set()

        if bus is not None:
            bus.subscribe("message.sent", self._on_event)
            bus.subscribe("session.closed", self._on_event)

    # ---------- 门铃（总线 handler：只许 put_nowait / 记日志） ----------

    def _on_event(self, event: Event) -> None:
        session_id = str(event.data.get("session_id") or "")
        if event.type == "session.closed" and session_id:
            self._forced.add(session_id)
        try:
            self._queue.put_nowait(session_id)
        except asyncio.QueueFull:
            # 门铃丢了无所谓：任务本体在 events.jsonl，tick 会按游标重扫
            logger.debug("memory 门铃队列已满，丢弃一次唤醒（session=%s）", session_id)

    # ---------- 常驻循环 ----------

    async def run(self) -> None:
        """常驻任务：被门铃叫醒或每 tick_seconds 扫一次。取消即退出。"""
        logger.info("MemoryWorker 启动：tick=%.0fs", self.tick_seconds)
        while True:
            try:
                await asyncio.wait_for(self._queue.get(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            # 一次对话会连响很多下门铃；它们只代表"有事发生"，扫一次就够。
            # 不排空的话，活跃会话每说一句都会让 worker 扫一遍全部会话。
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单轮失败不能让后台 worker 整个死掉——下一轮重试
                logger.exception("MemoryWorker tick 失败（下一轮重试）")

    async def tick(self) -> None:
        """扫一遍会话：静默够久（或收到 closed）且有新事件的，提炼一批。"""
        memory = self._config_reader() or {}
        idle_seconds = float(memory.get("extract_idle_minutes", 10)) * 60
        now = self._clock()
        forced = set(self._forced)
        self._forced.clear()

        for row in self._store.sessions():
            session_id = str(row.get("id") or "")
            if not session_id:
                continue

            # 先判静默再读文件：活跃会话每句都会响门铃，先读日志是白费功夫。
            if session_id not in forced:
                last_active = float(row.get("last_active") or 0)
                if now - last_active < idle_seconds:
                    continue  # 还在聊，先不提炼

            cursor = self._store.get_cursor(session_id)
            events = list(
                self._eventlog.read_session_batch(
                    session_id, after_seq=cursor, limit=EXTRACT_BATCH
                )
            )
            if not events:
                continue

            await self._extract(session_id, row, events)

        self._maybe_consolidate(memory)

    async def _extract(self, session_id: str, row: dict, events: list[dict]) -> None:
        session = SessionContext(
            session_id=session_id,
            session_type=str(row.get("kind") or ""),
            owner=str(row.get("owner") or ""),
        )
        try:
            written = await self._llm.extract(events, session=session)
        except Exception:
            # 游标**不推进**：失败的事件下一轮还在，重试即可（幂等）
            logger.exception(
                "extract 失败，游标保持不动（session=%s，%d 条事件）",
                session_id,
                len(events),
            )
            return

        last_seq = max(int(event.get("seq") or 0) for event in events)
        self._store.set_cursor(session_id, last_seq)
        logger.info(
            "extract 完成：session=%s 新记忆=%d 条，游标→%d",
            session_id,
            len(written),
            last_seq,
        )

    # ---------- consolidate（M1-06） ----------

    def _maybe_consolidate(self, memory: dict) -> None:
        """到点/够量就跑一轮遗忘判定。

        两条触发（§十一）：距上次 ≥ consolidate_interval_hours，或新事件数
        ≥ consolidate_event_threshold。状态落在磁盘（last_run + event_seq），
        重启不会每次都重跑；跑失败不推进状态，下一轮重试。
        """
        if self._consolidator is None:
            return

        now = self._clock()
        state = self._load_state()
        interval = float(memory.get("consolidate_interval_hours", 6)) * 3600
        event_threshold = int(memory.get("consolidate_event_threshold", 50))
        max_seq = self._eventlog.get_max_seq()

        due_by_time = now - float(state.get("last_run") or 0) >= interval
        due_by_events = max_seq - int(state.get("event_seq") or 0) >= event_threshold
        if not (due_by_time or due_by_events):
            return

        try:
            result = self._consolidator.run(now=now)
        except Exception:
            logger.exception("consolidate 失败（状态不推进，下一轮重试）")
            return

        self._save_state({"last_run": now, "event_seq": max_seq})
        logger.info(
            "consolidate 完成：检查 %d 条，遗忘 %d 条",
            result.get("checked", 0),
            len(result.get("forgotten", [])),
        )

    def _load_state(self) -> dict:
        if self._state_path is None:
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            logger.exception("保存 consolidate 状态失败（下一轮重试）")
