"""测试 MemoryWorker 后台触发（M1-05）。

验收标准（docs/M1推进计划.md）：
  - 发一轮对话 → 静默（测试里把 idle 调成 0）→ 自动出现 extract
  - 进程杀掉重启 → 不重复提炼（游标已推进）
  - 提炼失败 → 游标不推进，下一轮重试
  - session.closed → 不等静默，立刻提炼
"""

import asyncio
import os
import sys
import time

import pytest

# 与 test_chat_loop.py 一致：把 src 放进 path，按运行时的方式导入（core.*）。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.event_bus import Event
from core.memory.store import EventLog
from core.memory.worker import MemoryWorker
from core.session.store import SessionStore


class FakeMemoryLLM:
    """记录每次 extract 的入参；可切换成失败模式。"""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    async def extract(self, events, session=None):
        self.calls.append({"events": list(events), "session": session})
        if self.fail:
            raise RuntimeError("模型调用失败")
        return [{"id": f"m{len(self.calls)}", "content": "记一条", "importance": 3}]


@pytest.fixture
def store(tmp_path):
    session_store = SessionStore(tmp_path / "sessions.db").open()
    yield session_store
    session_store.close()


def make_worker(store, tmp_path, llm, *, idle_minutes=0, clock=None, tick_seconds=30.0):
    return MemoryWorker(
        store=store,
        eventlog=EventLog(tmp_path / "events.jsonl"),
        llm=llm,
        config_reader=lambda: {"extract_idle_minutes": idle_minutes},
        clock=clock or time.time,
        tick_seconds=tick_seconds,
    )


def add_turn(log: EventLog, session_id="s1", owner="u1", seq_text="你好"):
    log.append("message.received", session_id, owner, {"content": seq_text, "role": "user"})
    log.append("message.sent", session_id, owner, {"messages": ["在呢"], "stop_reason": "ok"})


class TestTick:
    async def test_idle_session_triggers_extract(self, store, tmp_path):
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm)

        await worker.tick()

        assert len(llm.calls) == 1
        assert [e["seq"] for e in llm.calls[0]["events"]] == [1, 2]
        assert store.get_cursor("s1") == 2

    async def test_active_session_is_not_extracted(self, store, tmp_path):
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm, idle_minutes=10)

        await worker.tick()

        assert llm.calls == []

    async def test_no_new_events_skips(self, store, tmp_path):
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)
        store.set_cursor("s1", 2)
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm)

        await worker.tick()

        assert llm.calls == []

    async def test_cursor_prevents_duplicate_after_restart(self, store, tmp_path):
        """验收：进程重启后按游标重扫，不重复提炼。"""
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)

        first = FakeMemoryLLM()
        await make_worker(store, tmp_path, first).tick()
        assert len(first.calls) == 1

        # 模拟重启：同一份磁盘状态，全新的 worker
        second = FakeMemoryLLM()
        await make_worker(store, tmp_path, second).tick()
        assert second.calls == []

    async def test_failure_keeps_cursor_and_retries(self, store, tmp_path):
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)
        llm = FakeMemoryLLM(fail=True)
        worker = make_worker(store, tmp_path, llm)

        await worker.tick()
        assert len(llm.calls) == 1
        assert store.get_cursor("s1") == 0  # 失败不推进

        llm.fail = False
        await worker.tick()
        assert len(llm.calls) == 2
        assert store.get_cursor("s1") == 2

    async def test_batches_are_capped_and_resume_from_cursor(self, store, tmp_path, monkeypatch):
        monkeypatch.setattr("core.memory.worker.EXTRACT_BATCH", 2)
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)  # seq 1, 2
        log.append("message.received", "s1", "u1", {"content": "再说一句"})  # seq 3
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm)

        await worker.tick()
        assert [e["seq"] for e in llm.calls[0]["events"]] == [1, 2]
        assert store.get_cursor("s1") == 2

        await worker.tick()
        assert [e["seq"] for e in llm.calls[1]["events"]] == [3]
        assert store.get_cursor("s1") == 3

    async def test_session_closed_forces_extract(self, store, tmp_path):
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm, idle_minutes=10)

        worker._on_event(Event(type="session.closed", data={"session_id": "s1"}, ts=0.0))
        await worker.tick()

        assert len(llm.calls) == 1

    async def test_message_sent_doorbell_does_not_force(self, store, tmp_path):
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm, idle_minutes=10)

        worker._on_event(Event(type="message.sent", data={"session_id": "s1"}, ts=0.0))
        await worker.tick()

        assert llm.calls == []

    async def test_only_this_sessions_events_are_read(self, store, tmp_path):
        store.ensure_session("s1", owner="u1", kind="private")
        store.ensure_session("s2", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        log.append("message.received", "s2", "u1", {"content": "别的会话"})
        add_turn(log, session_id="s1")
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm)

        await worker.tick()

        # 每个会话各提炼各的，批次里不能混进别的会话的事件
        by_session = {call["session"].session_id: call["events"] for call in llm.calls}
        assert set(by_session) == {"s1", "s2"}
        assert all(e["session_id"] == "s1" for e in by_session["s1"])
        assert all(e["session_id"] == "s2" for e in by_session["s2"])


class TestRunLoop:
    async def test_doorbell_wakes_worker(self, store, tmp_path):
        """常驻循环：门铃一响就跑一轮 tick。"""
        store.ensure_session("s1", owner="u1", kind="private")
        log = EventLog(tmp_path / "events.jsonl")
        add_turn(log)
        llm = FakeMemoryLLM()
        worker = make_worker(store, tmp_path, llm, tick_seconds=60)

        worker._queue.put_nowait("s1")
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(100):
                if llm.calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(llm.calls) == 1
