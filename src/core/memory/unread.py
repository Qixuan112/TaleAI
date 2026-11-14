"""未读增量（M1-08）：补上"我不在时发生的事"。

场景：用户先在会话 A 聊，切到会话 B，再回到 A——A 的历史里没有 B 的内容，
但角色应该知道"这期间用户还在别处说过什么、记下了什么"。

原料（§十二 感知清单）：
- **新事件**：events.jsonl 里 `last_seen` 之后、**别的会话**、同一 owner 的
  对话原文。按字节偏移增量读——这是每轮对话都会走的路径，不能从头扫文件。
- **新记忆**：`last_seen_at` 之后提炼的 curated 条目（别的会话、同一 owner）。
  当前会话自己的记忆不重复注入（历史里已经有了）。

游标落在 SQLite（session_cursors.last_seen_*），**读完即推进**：
重启不重复，队列丢了也不丢事实。三列分工见 SessionStore.get_last_seen。

永不经过 LLM：未读注入是规则拼装（§十九-19），语言加工只在 extract。
"""

import logging
import time

from core.memory.store import EventLog
from core.session.store import SessionStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 单轮最多注入多少条未读事件 / 新记忆 / 多少字。宁少勿多：未读是补背景，
#: 不该挤掉当前对话与检索到的记忆。
UNREAD_EVENT_LIMIT = 20
UNREAD_MEMORY_LIMIT = 10
UNREAD_TOKEN_BUDGET = 1000

#: 单轮最多扫多少行事件。扫不完的留到下一轮（偏移已推进到扫过的位置）。
UNREAD_SCAN_LIMIT = 200

_MESSAGE_TYPES = ("message.received", "message.sent")


def _event_line(event: dict) -> str:
    """一条事件 → 「时间 用户：…」/「时间 我：…」。"""
    payload = event.get("data") or {}
    if event.get("type") == "message.received":
        who, text = "用户", str(payload.get("content") or "")
    else:
        who, text = "我", "\n".join(payload.get("messages") or [])
    if not text.strip():
        return ""
    ts = float(event.get("ts") or 0)
    stamp = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
    prefix = f"{stamp} " if stamp else ""
    return f"{prefix}{who}：{text.strip()}"


class UnreadInbox:
    """按会话游标收集未读事件与未读新记忆。"""

    def __init__(
        self,
        sessions: SessionStore,
        eventlog: EventLog,
        curated,
        clock=None,
    ) -> None:
        self._sessions = sessions
        self._eventlog = eventlog
        self._curated = curated
        self._clock = clock or time.time

    def collect(self, session, now: float | None = None) -> dict:
        """收集这个会话的未读增量，并推进游标。

        session 需要 duck-typed 的 session_id / owner（SessionContext）。
        """
        now = self._clock() if now is None else now
        session_id = str(getattr(session, "session_id", "") or "")
        owner = str(getattr(session, "owner", "") or "")
        if not session_id or not owner:
            return {"events": [], "memories": []}

        last_seq, offset, last_at = self._sessions.get_last_seen(session_id)

        # 第一次见到这条会话：把游标钉在当前末尾，**不把历史当未读**。
        # "未读"的前提是有一个"上次看到"的基线；新会话没有基线，硬塞
        # 一堆旧事件只会跟历史/检索记忆重复。
        if last_seq == 0 and offset == 0 and last_at == 0:
            self._sessions.set_last_seen(
                session_id,
                self._eventlog.get_max_seq(),
                self._eventlog.size_bytes(),
                now,
            )
            return {"events": [], "memories": []}

        events, new_offset, scanned_max = self._read_events(
            session_id, owner, last_seq, offset
        )
        memories = self._read_memories(session_id, owner, last_at)

        # 读完即推进：即使这轮模型请求失败，事实也已"看过"，重启不重复。
        # 失败最多丢一次提醒，换来的是不会每轮重放同一段未读。
        max_seq = max([last_seq, scanned_max, *(int(e.get("seq") or 0) for e in events)])
        self._sessions.set_last_seen(session_id, max_seq, new_offset, now)

        return {"events": events, "memories": memories}

    def _read_events(
        self, session_id: str, owner: str, last_seq: int, offset: int
    ) -> tuple[list[dict], int, int]:
        events: list[dict] = []
        scanned_max = last_seq
        new_offset = offset
        try:
            batch, new_offset = self._eventlog.read_from_offset(
                offset, limit=UNREAD_SCAN_LIMIT
            )
        except Exception:
            logger.exception("读取未读事件失败（本轮跳过未读）")
            return [], offset, last_seq

        for event in batch:
            seq = int(event.get("seq") or 0)
            scanned_max = max(scanned_max, seq)
            if seq <= last_seq:
                continue
            if event.get("type") not in _MESSAGE_TYPES:
                continue
            if event.get("session_id") == session_id:
                continue  # 当前会话的内容历史里已有，不重复
            if str(event.get("owner") or "") != owner:
                continue  # 作用域：同一 owner，绝不串别人的对话
            events.append(event)
            if len(events) >= UNREAD_EVENT_LIMIT:
                break
        return events, new_offset, scanned_max

    def _read_memories(self, session_id: str, owner: str, last_at: float) -> list[dict]:
        try:
            entries = self._curated.read_all(owner=owner, status="active")
        except Exception:
            logger.exception("读取未读新记忆失败（本轮跳过）")
            return []

        fresh = []
        for entry in entries:
            try:
                created = float(entry.get("created_at") or 0)
            except (TypeError, ValueError):
                continue
            if created <= last_at:
                continue
            if entry.get("session_id") == session_id:
                continue
            fresh.append(entry)
        fresh.sort(key=lambda e: float(e.get("created_at") or 0))
        return fresh[-UNREAD_MEMORY_LIMIT:]

    @staticmethod
    def render(collected: dict) -> str:
        """把收集结果渲染成注入块正文（调用方负责转义系统标记）。"""
        memories = collected.get("memories") or []
        events = collected.get("events") or []

        lines: list[str] = []
        used = 0

        if memories:
            lines.append("【新记忆】你不在时记下的：")
            for entry in memories:
                text = str(entry.get("content") or "").strip()
                if not text:
                    continue
                if used + len(text) > UNREAD_TOKEN_BUDGET:
                    break
                lines.append(f"- {text}")
                used += len(text)

        if events:
            lines.append("【别的会话】你不在时，用户在别的会话里说过（从旧到新）：")
            for event in events:
                line = _event_line(event)
                if not line:
                    continue
                if used + len(line) > UNREAD_TOKEN_BUDGET:
                    break
                lines.append(f"- {line}")
                used += len(line)

        return "\n".join(lines)
