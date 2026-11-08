"""测试未读增量（M1-08）。

验收：
  - 换会话时补"我不在时发生的事"（别的会话、同一 owner 的事件 + 新记忆）
  - 当前会话自己的内容不重复注入
  - 读完推进游标：再读为空、重启不重复
  - owner 隔离
  - 第一次见到的会话以当前末尾为基线，不把历史当未读
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.llm.context import SessionContext
from core.memory.store import CuratedStore, EventLog
from core.memory.unread import UnreadInbox, UNREAD_EVENT_LIMIT
from core.session.store import SessionStore

NOW = 1_800_000_000.0


@pytest.fixture
def env(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db").open()
    sessions.ensure_session("A", owner="u1", kind="private")
    sessions.ensure_session("B", owner="u1", kind="private")
    eventlog = EventLog(tmp_path / "events.jsonl")
    curated = CuratedStore(tmp_path / "curated.jsonl")
    yield sessions, eventlog, curated
    sessions.close()


def make_inbox(env, clock=lambda: NOW):
    sessions, eventlog, curated = env
    return UnreadInbox(sessions, eventlog, curated, clock=clock)


def session_a():
    return SessionContext(session_id="A", owner="u1", session_type="private")


def prime(env, clock=lambda: NOW):
    """把会话 A 的游标钉在当前末尾（模拟"用户上次在 A 聊过"）。"""
    return make_inbox(env, clock=clock).collect(session_a())


class TestCollect:
    def test_first_meeting_does_not_replay_history(self, env):
        _, eventlog, curated = env
        eventlog.append("message.received", "B", "u1", {"content": "很久以前的事"})
        curated.append(
            owner="u1", session_id="B", content="很久以前记下的。", importance=4,
            refs=[{"event_seq": 1, "quote": "x"}], created_at=NOW - 100,
        )
        inbox = make_inbox(env)

        collected = inbox.collect(session_a())

        assert collected == {"events": [], "memories": []}

    def test_collects_other_session_events(self, env):
        _, eventlog, _ = env
        prime(env)
        eventlog.append("message.received", "B", "u1", {"content": "我今天去了医院"})
        eventlog.append("message.sent", "B", "u1", {"messages": ["怎么了吗"]})
        inbox = make_inbox(env)

        collected = inbox.collect(session_a())

        assert [e["session_id"] for e in collected["events"]] == ["B", "B"]
        text = inbox.render(collected)
        assert "我今天去了医院" in text
        assert "怎么了吗" in text

    def test_current_session_events_are_excluded(self, env):
        _, eventlog, _ = env
        prime(env)
        eventlog.append("message.received", "A", "u1", {"content": "我在 A 说过的"})
        eventlog.append("message.received", "B", "u1", {"content": "我在 B 说的"})
        inbox = make_inbox(env)

        collected = inbox.collect(session_a())

        assert [e["data"]["content"] for e in collected["events"]] == ["我在 B 说的"]

    def test_other_owner_events_are_excluded(self, env):
        _, eventlog, _ = env
        prime(env)
        eventlog.append("message.received", "B", "u2", {"content": "别人的私事"})
        inbox = make_inbox(env)

        collected = inbox.collect(session_a())

        assert collected["events"] == []

    def test_cursor_advances_and_restart_does_not_repeat(self, env):
        _, eventlog, _ = env
        prime(env)
        eventlog.append("message.received", "B", "u1", {"content": "第一次"})
        inbox = make_inbox(env)

        assert len(inbox.collect(session_a())["events"]) == 1
        # 同一进程再读：空了
        assert make_inbox(env).collect(session_a())["events"] == []

        # 新事件只补新的，不重放旧的（模拟重启：同一个库、新实例）
        eventlog.append("message.received", "B", "u1", {"content": "第二次"})
        collected = make_inbox(env).collect(session_a())
        assert [e["data"]["content"] for e in collected["events"]] == ["第二次"]

    def test_event_limit(self, env):
        _, eventlog, _ = env
        prime(env)
        for i in range(UNREAD_EVENT_LIMIT + 5):
            eventlog.append("message.received", "B", "u1", {"content": f"第{i}条"})
        inbox = make_inbox(env)

        collected = inbox.collect(session_a())

        assert len(collected["events"]) == UNREAD_EVENT_LIMIT

    def test_new_memories_are_included(self, env):
        _, _, curated = env
        prime(env, clock=lambda: NOW)
        old = curated.append(
            owner="u1", session_id="B", content="很早记下的。", importance=3,
            refs=[{"event_seq": 1, "quote": "x"}], created_at=NOW - 100,
        )
        current = curated.append(
            owner="u1", session_id="A", content="本会话自己的。", importance=4,
            refs=[{"event_seq": 2, "quote": "y"}], created_at=NOW + 5,
        )
        fresh = curated.append(
            owner="u1", session_id="B", content="刚记下的。", importance=4,
            refs=[{"event_seq": 3, "quote": "z"}], created_at=NOW + 10,
        )
        inbox = make_inbox(env, clock=lambda: NOW + 20)

        collected = inbox.collect(session_a())

        ids = [m["id"] for m in collected["memories"]]
        assert fresh in ids
        assert old not in ids        # 上次已看过
        assert current not in ids    # 本会话的记忆不重复注入


class TestRender:
    def test_render_has_sections_and_stamps(self, env):
        _, eventlog, curated = env
        prime(env)
        eventlog.append("message.received", "B", "u1", {"content": "在 B 说的话"},
                        ts=NOW + 5)
        curated.append(
            owner="u1", session_id="B", content="新记住的事。", importance=4,
            refs=[{"event_seq": 1, "quote": "x"}], created_at=NOW + 10,
        )
        inbox = make_inbox(env, clock=lambda: NOW + 20)

        text = inbox.render(inbox.collect(session_a()))

        assert "【新记忆】" in text
        assert "【别的会话】" in text
        assert "在 B 说的话" in text
        assert "用户：" in text
