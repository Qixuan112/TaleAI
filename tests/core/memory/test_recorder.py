"""测试事件落盘接线（M1-02）。

验收标准：
  - 一次 WS 对话后，data/memory/events.jsonl 出现成对的 received/sent 行
  - 先落盘、后 publish（总线丢了，落盘不丢）
  - 落盘失败不炸前台对话
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.event_bus import EventBus
from src.core.memory.recorder import record_event
from src.core.memory.store import EventLog


class TestRecordEvent:
    """测试 record_event helper。"""

    def test_append_then_publish(self, tmp_path: Path):
        """先落盘、后 publish（§18.2 硬规则）。"""
        log = EventLog(tmp_path / "events.jsonl")
        bus = EventBus()
        calls = []

        def handler(event):
            calls.append(event)

        bus.subscribe("message.received", handler)

        # 调用 record_event
        record_event(
            bus=bus,
            eventlog=log,
            event_type="message.received",
            session_id="s1",
            owner="u1",
            data={"content": "你好", "role": "user"},
        )

        # 验证落盘
        batch = list(log.read_batch(after_seq=0))
        assert len(batch) == 1
        assert batch[0]["type"] == "message.received"
        assert batch[0]["data"]["content"] == "你好"

        # 验证 publish
        assert len(calls) == 1
        assert calls[0].type == "message.received"
        assert calls[0].data["content"] == "你好"

    def test_append_failure_does_not_block_publish(self, tmp_path: Path):
        """落盘失败不阻塞 publish（记忆是增强，不是主链路）。"""
        # 用一个只读路径模拟落盘失败
        readonly_path = tmp_path / "readonly"
        readonly_path.mkdir()
        readonly_path.chmod(0o444)  # 只读
        log = EventLog(readonly_path / "events.jsonl")

        bus = EventBus()
        calls = []
        bus.subscribe("test", lambda e: calls.append(e))

        # 落盘会失败，但 publish 照常
        record_event(
            bus=bus,
            eventlog=log,
            event_type="test",
            session_id="s1",
            owner="u1",
            data={"foo": "bar"},
        )

        # publish 照常发出
        assert len(calls) == 1
        assert calls[0].type == "test"

        # 恢复权限（cleanup）
        readonly_path.chmod(0o755)

    def test_message_received_schema(self, tmp_path: Path):
        """message.received 的 data schema。"""
        log = EventLog(tmp_path / "events.jsonl")
        bus = EventBus()

        record_event(
            bus=bus,
            eventlog=log,
            event_type="message.received",
            session_id="s1",
            owner="u1",
            data={"content": "测试内容", "role": "user"},
        )

        batch = list(log.read_batch(after_seq=0))
        assert batch[0]["data"]["content"] == "测试内容"
        assert batch[0]["data"]["role"] == "user"

    def test_message_sent_schema(self, tmp_path: Path):
        """message.sent 的 data schema。"""
        log = EventLog(tmp_path / "events.jsonl")
        bus = EventBus()

        record_event(
            bus=bus,
            eventlog=log,
            event_type="message.sent",
            session_id="s1",
            owner="u1",
            data={
                "messages": ["回答1", "回答2"],
                "stop_reason": "ok",
            },
        )

        batch = list(log.read_batch(after_seq=0))
        assert batch[0]["data"]["messages"] == ["回答1", "回答2"]
        assert batch[0]["data"]["stop_reason"] == "ok"

    def test_session_closed_schema(self, tmp_path: Path):
        """session.closed 的 data schema。"""
        log = EventLog(tmp_path / "events.jsonl")
        bus = EventBus()

        record_event(
            bus=bus,
            eventlog=log,
            event_type="session.closed",
            session_id="s1",
            owner="u1",
            data={"reason": "user_quit"},
        )

        batch = list(log.read_batch(after_seq=0))
        assert batch[0]["data"]["reason"] == "user_quit"

    def test_multiple_events_sequential(self, tmp_path: Path):
        """连续记录多条事件（一次对话）。"""
        log = EventLog(tmp_path / "events.jsonl")
        bus = EventBus()

        # 模拟一次对话：received → sent
        record_event(
            bus=bus,
            eventlog=log,
            event_type="message.received",
            session_id="s1",
            owner="u1",
            data={"content": "问题", "role": "user"},
        )

        record_event(
            bus=bus,
            eventlog=log,
            event_type="message.sent",
            session_id="s1",
            owner="u1",
            data={
                "messages": ["回答"],
                "stop_reason": "ok",
            },
        )

        # 验证：成对出现
        batch = list(log.read_batch(after_seq=0))
        assert len(batch) == 2
        assert batch[0]["type"] == "message.received"
        assert batch[1]["type"] == "message.sent"

    def test_owner_isolation_in_events(self, tmp_path: Path):
        """不同 owner 的事件都进同一个 events.jsonl。"""
        log = EventLog(tmp_path / "events.jsonl")
        bus = EventBus()

        # A 的消息
        record_event(
            bus=bus,
            eventlog=log,
            event_type="message.received",
            session_id="s1",
            owner="userA",
            data={"content": "A 的问题", "role": "user"},
        )

        # B 的消息
        record_event(
            bus=bus,
            eventlog=log,
            event_type="message.received",
            session_id="s2",
            owner="userB",
            data={"content": "B 的问题", "role": "user"},
        )

        # 验证：都在同一个文件，但 owner 不同
        batch = list(log.read_batch(after_seq=0))
        assert len(batch) == 2
        assert batch[0]["owner"] == "userA"
        assert batch[1]["owner"] == "userB"
