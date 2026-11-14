"""测试记忆整合与遗忘（M1-06）。

验收（§19-8）：
  - importance 低、够老、分数低于阈值 → 追加墓碑，检索不到
  - importance ≥ 4 永不自动遗忘
  - 年轻的记忆不会被立刻忘掉
  - 遗忘 = 追加墓碑，不是删文件
"""

import os
import sys

import pytest

# 与 test_chat_loop.py 一致：把 src 放进 path，按运行时的方式导入（core.*）。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.memory.consolidator import MemoryConsolidator
from core.memory.retriever import MemoryRetriever
from core.memory.store import CuratedStore

NOW = 1_800_000_000.0
DAY = 86400.0


def make_store(tmp_path):
    return CuratedStore(tmp_path / "curated.jsonl", clock_fn=lambda: NOW)


def add(store, content, *, importance=3, age_days=0.0, owner="u1"):
    return store.append(
        owner=owner,
        session_id="s1",
        content=content,
        importance=importance,
        refs=[{"event_seq": 1, "quote": content[:10]}],
        created_at=NOW - age_days * DAY,
    )


def make_consolidator(store, cfg=None):
    return MemoryConsolidator(
        store, clock=lambda: NOW, config_reader=lambda: dict(cfg or {})
    )


class TestForget:
    def test_old_low_importance_is_forgotten(self, tmp_path):
        store = make_store(tmp_path)
        # 半衰期 10 天：30 天前 importance=2 → 2×2^-3 = 0.25 < 0.5
        entry_id = add(store, "用户某天随口提过的事。", importance=2, age_days=31)
        consolidator = make_consolidator(store, {"half_life_days": 10})

        result = consolidator.run()

        assert result["forgotten"] == [entry_id]
        assert store.read_all(status="forgotten")[0]["id"] == entry_id
        assert store.read_all(status="active") == []

    def test_high_importance_is_never_forgotten(self, tmp_path):
        """importance ≥ importance_never_forget（默认 4）永不自动遗忘。"""
        store = make_store(tmp_path)
        add(store, "用户的身份级信息。", importance=5, age_days=3650)
        add(store, "用户的重要偏好。", importance=4, age_days=3650)
        consolidator = make_consolidator(store, {"half_life_days": 10})

        result = consolidator.run()

        assert result["forgotten"] == []
        assert len(store.read_all(status="active")) == 2

    def test_young_memory_is_kept(self, tmp_path):
        store = make_store(tmp_path)
        add(store, "昨天刚记的事。", importance=1, age_days=1)
        consolidator = make_consolidator(store, {"half_life_days": 10})

        assert consolidator.run()["forgotten"] == []

    def test_score_above_threshold_is_kept(self, tmp_path):
        store = make_store(tmp_path)
        # 半衰期 30 天：30 天前 importance=2 → 2×2^-1 = 1.0 > 0.5
        add(store, "半年前的普通事实。", importance=2, age_days=30)
        consolidator = make_consolidator(store, {"half_life_days": 30})

        assert consolidator.run()["forgotten"] == []

    def test_forgotten_entry_is_skipped_next_round(self, tmp_path):
        store = make_store(tmp_path)
        add(store, "会被忘的事。", importance=1, age_days=100)
        consolidator = make_consolidator(store, {"half_life_days": 10})

        assert len(consolidator.run()["forgotten"]) == 1
        assert consolidator.run()["forgotten"] == []

    def test_forgotten_entry_disappears_from_retrieval(self, tmp_path):
        store = make_store(tmp_path)
        add(store, "会被忘的事。", importance=1, age_days=100)
        make_consolidator(store, {"half_life_days": 10}).run()

        retriever = MemoryRetriever(
            store, clock=lambda: NOW, config_reader=lambda: {}
        )
        assert retriever.retrieve("u1") == []


class TestConfig:
    def test_custom_threshold_is_respected(self, tmp_path):
        store = make_store(tmp_path)
        # score = 2×2^(-30/10) = 0.25；阈值调到 0.1 就不该忘
        add(store, "低分但阈值也低。", importance=2, age_days=31)
        consolidator = make_consolidator(
            store, {"half_life_days": 10, "forget_threshold": 0.1}
        )

        assert consolidator.run()["forgotten"] == []


def test_default_half_life_is_two_days():
    """用户 2026-11-08 拍板：记忆两天衰减一个周期；锁住默认值防漂移。"""
    from core.config.default import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["memory"]["half_life_days"] == 2
