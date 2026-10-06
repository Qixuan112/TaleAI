"""测试记忆存储地基（M1-01）。

验收标准：
  - 并发追加 100 条 → seq 是 1..100 无重复无空洞
  - 写一半崩溃 → 按 seq 重扫能幂等补齐
  - 时钟可注入（测试衰减用）
"""

import asyncio
import json
import tempfile
import threading
from pathlib import Path

import pytest

from src.core.memory.store import CuratedStore, EventLog


class FakeClock:
    """可控时钟（测试用）。"""

    def __init__(self, start_ts: float = 1000000.0):
        self.ts = start_ts
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.ts

    def advance(self, seconds: float):
        """前进 N 秒。"""
        with self._lock:
            self.ts += seconds


class TestEventLog:
    """测试 EventLog（原始事件日志）。"""

    def test_append_and_read(self, tmp_path: Path):
        """基本追加与读取。"""
        log = EventLog(tmp_path / "events.jsonl")

        # 追加三条
        seq1 = log.append(
            type="message.received",
            session_id="s1",
            owner="u1",
            data={"content": "你好", "role": "user"},
        )
        seq2 = log.append(
            type="message.sent",
            session_id="s1",
            owner="u1",
            data={"messages": [{"role": "assistant", "content": "嗨"}]},
        )
        seq3 = log.append(
            type="session.closed",
            session_id="s1",
            owner="u1",
            data={"reason": "user_quit"},
        )

        # seq 递增
        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

        # 读批
        batch = list(log.read_batch(after_seq=0, limit=10))
        assert len(batch) == 3
        assert batch[0]["seq"] == 1
        assert batch[0]["type"] == "message.received"
        assert batch[1]["seq"] == 2
        assert batch[2]["seq"] == 3

    def test_concurrent_append_no_duplicate_seq(self, tmp_path: Path):
        """并发追加 100 条 → seq 无重复无空洞（验收标准①）。"""
        log = EventLog(tmp_path / "events.jsonl")
        results = []

        def worker(i: int):
            seq = log.append(
                type="test",
                session_id="s1",
                owner="u1",
                data={"index": i},
            )
            results.append(seq)

        # 100 线程并发写
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 验证 seq 是 1..100
        assert sorted(results) == list(range(1, 101))
        assert len(set(results)) == 100  # 无重复

    def test_recovery_after_crash(self, tmp_path: Path):
        """崩溃恢复：重新加载能继续递增 seq（验收标准②）。"""
        filepath = tmp_path / "events.jsonl"

        # 第一次写 3 条
        log1 = EventLog(filepath)
        log1.append("test", "s1", "u1", {})
        log1.append("test", "s1", "u1", {})
        log1.append("test", "s1", "u1", {})
        assert log1.get_max_seq() == 3

        # 模拟崩溃：重新加载
        log2 = EventLog(filepath)
        assert log2.get_max_seq() == 3  # 恢复正确

        # 继续写
        seq = log2.append("test", "s1", "u1", {})
        assert seq == 4  # 继续递增

    def test_injectable_clock(self, tmp_path: Path):
        """时钟可注入（测试衰减用）。"""
        clock = FakeClock(start_ts=1000.0)
        log = EventLog(tmp_path / "events.jsonl", clock_fn=clock)

        log.append("test", "s1", "u1", {}, ts=None)  # 用 clock
        log.append("test", "s1", "u1", {}, ts=2000.0)  # 显式指定

        batch = list(log.read_batch(after_seq=0))
        assert batch[0]["ts"] == 1000.0
        assert batch[1]["ts"] == 2000.0

    def test_read_batch_with_cursor(self, tmp_path: Path):
        """按游标读批（幂等）。"""
        log = EventLog(tmp_path / "events.jsonl")

        for i in range(10):
            log.append("test", "s1", "u1", {"i": i})

        # 游标 0 → 读全部
        batch1 = list(log.read_batch(after_seq=0, limit=5))
        assert len(batch1) == 5
        assert batch1[0]["seq"] == 1

        # 游标 5 → 读 6..10
        batch2 = list(log.read_batch(after_seq=5, limit=10))
        assert len(batch2) == 5
        assert batch2[0]["seq"] == 6

        # 幂等：重读同一游标
        batch3 = list(log.read_batch(after_seq=5, limit=10))
        assert batch3 == batch2


class TestCuratedStore:
    """测试 CuratedStore（提炼记忆）。"""

    def test_append_and_read(self, tmp_path: Path):
        """基本追加与读取。"""
        store = CuratedStore(tmp_path / "curated.jsonl")

        # 追加一条记忆
        entry_id = store.append(
            owner="u1",
            session_id="s1",
            content="用户下周去北京",
            importance=4,
            refs=[{"event_seq": 1, "quote": "我下周去北京"}],
            created_at=1000.0,
        )

        # 读取
        entries = store.read_all(owner="u1")
        assert len(entries) == 1
        assert entries[0]["id"] == entry_id
        assert entries[0]["content"] == "用户下周去北京"
        assert entries[0]["importance"] == 4
        assert entries[0]["status"] == "active"
        assert len(entries[0]["refs"]) == 1

    def test_tombstone_marks_forgotten(self, tmp_path: Path):
        """墓碑标记遗忘（不删除原条目）。"""
        store = CuratedStore(tmp_path / "curated.jsonl")

        # 追加记忆
        entry_id = store.append(
            owner="u1",
            session_id="s1",
            content="琐事",
            importance=1,
            refs=[],
            created_at=1000.0,
        )

        # 追加墓碑
        store.append_tombstone(entry_id, reason="decay")

        # 读取：status 变 forgotten
        entries = store.read_all(owner="u1")
        assert len(entries) == 1
        assert entries[0]["id"] == entry_id
        assert entries[0]["status"] == "forgotten"
        assert "forgotten_at" in entries[0]

    def test_update_last_access(self, tmp_path: Path):
        """更新访问时间（retrieve 命中时调用）。"""
        clock = FakeClock(start_ts=1000.0)
        store = CuratedStore(tmp_path / "curated.jsonl", clock_fn=clock)

        # 创建记忆
        entry_id = store.append(
            owner="u1",
            session_id="s1",
            content="重要事",
            importance=5,
            refs=[],
            created_at=1000.0,
        )

        # 前进 10 天，更新访问时间
        clock.advance(10 * 86400)
        store.update_last_access(entry_id)

        # 读取：last_access 已更新
        entries = store.read_all(owner="u1")
        assert len(entries) == 1
        assert entries[0]["last_access"] == 1000.0 + 10 * 86400

    def test_owner_isolation(self, tmp_path: Path):
        """owner 隔离：A 的记忆不进 B 的查询（验收标准③）。"""
        store = CuratedStore(tmp_path / "curated.jsonl")

        # A 的记忆
        store.append(
            owner="userA",
            session_id="s1",
            content="A 的秘密",
            importance=3,
            refs=[],
        )

        # B 的记忆
        store.append(
            owner="userB",
            session_id="s2",
            content="B 的秘密",
            importance=3,
            refs=[],
        )

        # A 只能看到自己的
        entries_a = store.read_all(owner="userA")
        assert len(entries_a) == 1
        assert entries_a[0]["content"] == "A 的秘密"

        # B 只能看到自己的
        entries_b = store.read_all(owner="userB")
        assert len(entries_b) == 1
        assert entries_b[0]["content"] == "B 的秘密"

    def test_read_batch_with_cursor(self, tmp_path: Path):
        """按游标读批（原始行，未合并）。"""
        store = CuratedStore(tmp_path / "curated.jsonl")

        # 写 3 条
        id1 = store.append("u1", "s1", "记忆1", 3, [])
        id2 = store.append("u1", "s1", "记忆2", 4, [])
        id3 = store.append("u1", "s1", "记忆3", 2, [])

        # 更新 id1 的访问时间（产生第 4 行）
        store.update_last_access(id1)

        # 读批：游标 0 → 全部 4 行（未合并）
        batch = list(store.read_batch(after_seq=0, limit=10))
        assert len(batch) == 4

        # 游标 2 → 读 seq=3,4
        batch2 = list(store.read_batch(after_seq=2, limit=10))
        assert len(batch2) == 2
        assert batch2[0]["seq"] == 3

    def test_seq_continues_across_types(self, tmp_path: Path):
        """curated 的 seq 跨追加/更新/墓碑持续递增。"""
        store = CuratedStore(tmp_path / "curated.jsonl")

        # 追加 → seq=1
        entry_id = store.append("u1", "s1", "记忆", 3, [])
        assert store.get_max_seq() == 1

        # 更新访问 → seq=2
        store.update_last_access(entry_id)
        assert store.get_max_seq() == 2

        # 墓碑 → seq=3
        store.append_tombstone(entry_id)
        assert store.get_max_seq() == 3

        # 验证文件有 3 行
        batch = list(store.read_batch(after_seq=0, limit=10))
        assert len(batch) == 3


class TestConfigMemoryBlock:
    """测试 memory 配置块已添加到 default.py。"""

    def test_memory_config_exists(self):
        """验证 memory 配置块存在且有必需字段。"""
        from src.core.config.default import DEFAULT_CONFIG

        assert "memory" in DEFAULT_CONFIG
        mem_cfg = DEFAULT_CONFIG["memory"]

        # 衰减参数
        assert "half_life_days" in mem_cfg
        assert "forget_threshold" in mem_cfg
        assert "importance_never_forget" in mem_cfg

        # 触发参数
        assert "extract_idle_minutes" in mem_cfg
        assert "consolidate_interval_hours" in mem_cfg

        # 检索参数
        assert "retrieve_top_k" in mem_cfg
        assert "retrieve_token_budget" in mem_cfg
