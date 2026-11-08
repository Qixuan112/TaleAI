"""测试记忆检索（M1-07）。

验收：
  - owner 隔离：A 的记忆不进 B 的对话（③）
  - 私聊跨会话召回：换会话仍能取到同一个人的记忆（① 的前提）
  - 群聊按会话隔离：别的群的记忆不串进来
  - 排序按衰减分；token 预算截断；条数上限
"""

import time

import pytest

from src.core.memory.retriever import MemoryRetriever, score
from src.core.memory.store import CuratedStore

NOW = 1_800_000_000.0
DAY = 86400.0


def make_retriever(tmp_path, memory_cfg=None):
    store = CuratedStore(tmp_path / "curated.jsonl")
    cfg = memory_cfg or {}
    return store, MemoryRetriever(
        store, clock=lambda: NOW, config_reader=lambda: dict(cfg)
    )


def add(store, content, *, owner="u1", session="s1", importance=3, age_days=0.0):
    return store.append(
        owner=owner,
        session_id=session,
        content=content,
        importance=importance,
        refs=[{"event_seq": 1, "quote": content[:10]}],
        created_at=NOW - age_days * DAY,
    )


class TestScoping:
    def test_private_recalls_across_sessions(self, tmp_path):
        """验收①的前提：隔天新会话也要能取到同一 owner 的旧记忆。"""
        store, retriever = make_retriever(tmp_path)
        add(store, "用户住在上海。", session="s1")

        hits = retriever.retrieve("u1", session_id="s_new", session_type="private")

        assert [h["content"] for h in hits] == ["用户住在上海。"]

    def test_owner_isolation(self, tmp_path):
        """验收③：A 的私人记忆不进 B 的对话。"""
        store, retriever = make_retriever(tmp_path)
        add(store, "用户住在上海。", owner="u1", session="s1")
        add(store, "用户住在北京。", owner="u2", session="s2")

        hits = retriever.retrieve("u2", session_id="s9", session_type="private")

        assert [h["content"] for h in hits] == ["用户住在北京。"]

    def test_group_is_scoped_to_session(self, tmp_path):
        """群聊的记忆是"这个群"的，不串到别的群。"""
        store, retriever = make_retriever(tmp_path)
        add(store, "群 A 的约定。", session="group_a")
        add(store, "群 B 的约定。", session="group_b")

        hits = retriever.retrieve("u1", session_id="group_a", session_type="group")

        assert [h["content"] for h in hits] == ["群 A 的约定。"]

    def test_no_owner_returns_nothing(self, tmp_path):
        store, retriever = make_retriever(tmp_path)
        add(store, "用户住在上海。")
        assert retriever.retrieve("") == []

    def test_forgotten_entries_are_excluded(self, tmp_path):
        store, retriever = make_retriever(tmp_path)
        entry_id = add(store, "用户住在上海。")
        store.append_tombstone(entry_id)

        assert retriever.retrieve("u1") == []


class TestRanking:
    def test_higher_importance_wins_at_same_age(self, tmp_path):
        store, retriever = make_retriever(tmp_path)
        add(store, "一般事实。", importance=2)
        add(store, "重要事实。", importance=5)

        hits = retriever.retrieve("u1")

        assert [h["content"] for h in hits] == ["重要事实。", "一般事实。"]

    def test_decay_can_sink_old_memories(self, tmp_path):
        """30 天半衰期：同 importance 下，旧的排后面。"""
        store, retriever = make_retriever(tmp_path)
        add(store, "很久以前。", importance=3, age_days=60)
        add(store, "最近。", importance=3, age_days=1)

        hits = retriever.retrieve("u1")

        assert [h["content"] for h in hits] == ["最近。", "很久以前。"]

    def test_score_formula(self):
        entry = {"importance": 4, "created_at": NOW - 30 * DAY}
        assert score(entry, NOW, half_life_days=30) == pytest.approx(2.0)
        assert score(entry, NOW, half_life_days=60) == pytest.approx(4 * 2 ** -0.5)


class TestTruncation:
    def test_top_k_limit(self, tmp_path):
        store, retriever = make_retriever(tmp_path, {"retrieve_top_k": 1})
        add(store, "第一。", importance=5)
        add(store, "第二。", importance=4)

        assert len(retriever.retrieve("u1")) == 1

    def test_token_budget_truncates(self, tmp_path):
        store, retriever = make_retriever(
            tmp_path, {"retrieve_top_k": 20, "retrieve_token_budget": 10}
        )
        add(store, "一二三四五六七八九十", importance=5)  # 10 字
        add(store, "甲乙", importance=4)

        hits = retriever.retrieve("u1")

        assert [h["content"] for h in hits] == ["一二三四五六七八九十"]

    def test_single_entry_over_budget_is_skipped(self, tmp_path):
        store, retriever = make_retriever(
            tmp_path, {"retrieve_top_k": 20, "retrieve_token_budget": 3}
        )
        add(store, "这是一条很长的记忆内容", importance=5)
        add(store, "短", importance=4)

        hits = retriever.retrieve("u1")

        assert [h["content"] for h in hits] == ["短"]

    def test_budget_zero_means_unlimited(self, tmp_path):
        store, retriever = make_retriever(
            tmp_path, {"retrieve_top_k": 20, "retrieve_token_budget": 0}
        )
        add(store, "一二三四五六七八九十")
        add(store, "甲乙")

        assert len(retriever.retrieve("u1")) == 2
