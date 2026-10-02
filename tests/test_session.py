"""M0-09 验收：SessionStore「重开后历史完整」。

设计文档 §18.3（schema）/ §18.1 第 3、4、7 步 / §十二（persist=False）。

「重开」在测试里体现为**换一个 SessionStore 实例指向同一个文件**——
这正是进程重启后发生的事。
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.session.store import SessionStore


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "test.db").open()
    s.ensure_session("s1")
    yield s
    s.close()


# ---------- 验收核心：重开后历史完整 ----------


def test_history_survives_reopen(tmp_path):
    """写几条 → 关掉 → 换新实例重开 → 历史完整。"""
    db = tmp_path / "sessions.db"

    first = SessionStore(db).open()
    first.ensure_session("s1")
    first.append("s1", "user", "你好")
    first.append("s1", "assistant", "哟，老板来啦~")
    first.append("s1", "user", "今天天气怎么样")
    first.append("s1", "assistant", "我又没长眼睛看窗外")
    first.close()

    # 换一个全新实例 = 进程重启
    second = SessionStore(db).open()
    history = second.history("s1")
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "哟，老板来啦~"},
        {"role": "user", "content": "今天天气怎么样"},
        {"role": "assistant", "content": "我又没长眼睛看窗外"},
    ]
    second.close()


def test_reopen_does_not_duplicate_session(tmp_path):
    """重开后 ensure_session 不该重复插入会话行。"""
    db = tmp_path / "s.db"
    a = SessionStore(db).open()
    a.ensure_session("s1", title="首次")
    a.close()

    b = SessionStore(db).open()
    b.ensure_session("s1", title="再次")  # 幂等
    assert len(b.sessions()) == 1
    b.close()


def test_history_order_is_chronological(tmp_path):
    """历史按旧→新返回——装配 messages 需要的正是这个顺序。"""
    db = tmp_path / "s.db"
    s = SessionStore(db).open()
    s.ensure_session("s1")
    for i in range(5):
        s.append("s1", "user", f"第{i}条")
    contents = [m["content"] for m in s.history("s1")]
    assert contents == [f"第{i}条" for i in range(5)]
    s.close()


def test_history_limit_returns_most_recent_in_order(tmp_path):
    """limit 取最近 N 条，但返回仍是正序（不能给调用方倒序）。"""
    db = tmp_path / "s.db"
    s = SessionStore(db).open()
    s.ensure_session("s1")
    for i in range(10):
        s.append("s1", "user", f"第{i}条")
    got = s.history("s1", limit=3)
    assert [m["content"] for m in got] == ["第7条", "第8条", "第9条"]
    s.close()


# ---------- 不变量 1：动态块绝不落库（§十二 persist=False） ----------


def test_system_reminder_is_rejected(store):
    """带 <system_reminder> 的内容必须被拒绝，不能悄悄写进去。"""
    with pytest.raises(ValueError, match="system_reminder"):
        store.append("s1", "user", "<system_reminder>\n当前时间：...\n</system_reminder>\n你好")


def test_rejection_leaves_no_partial_row(store):
    """拒绝之后库里不该留下任何痕迹。"""
    before = store.count("s1")
    with pytest.raises(ValueError):
        store.append("s1", "user", "<system_reminder>污染</system_reminder>")
    assert store.count("s1") == before


def test_normal_content_still_accepted(store):
    """守卫不能误伤正常内容——提到标签名但没有尖括号的，应当放行。"""
    store.append("s1", "user", "什么是 system_reminder？")
    assert store.count("s1") == 1


# ---------- 不变量 2：tool_json 挂在 assistant 行上 ----------


def test_tool_json_stored_on_assistant_row(store):
    """tool_json 跟着 assistant 消息走，不单独占行——保住「1 回合 = 2 行」。"""
    store.append("s1", "user", "测试连通性")
    store.append("s1", "assistant", "pong~", tool_json={"calls": 1, "stop_reason": "ok"})

    rows = store.messages("s1")
    assert len(rows) == 2  # 就两行
    assert rows[0]["tool_json"] is None
    assert json.loads(rows[1]["tool_json"]) == {"calls": 1, "stop_reason": "ok"}


def test_tool_json_accepts_str_and_dict(store):
    store.append("s1", "assistant", "a", tool_json={"x": 1})
    store.append("s1", "assistant", "b", tool_json='{"y": 2}')
    rows = store.messages("s1")
    assert json.loads(rows[0]["tool_json"]) == {"x": 1}
    assert json.loads(rows[1]["tool_json"]) == {"y": 2}


def test_history_excludes_metadata(store):
    """history() 只给 role/content——装配 messages 的形状。"""
    store.append("s1", "assistant", "话", tool_json={"calls": 1})
    assert store.history("s1") == [{"role": "assistant", "content": "话"}]


# ---------- 会话与标签 ----------


def test_append_requires_known_session(store):
    """外键约束要生效：写不存在的会话应当报错（而不是静默建孤儿行）。"""
    with pytest.raises(sqlite3.IntegrityError):
        store.append("不存在的会话", "user", "内容")


def test_invalid_role_rejected(store):
    with pytest.raises(ValueError, match="role"):
        store.append("s1", "robot", "内容")


def test_ensure_session_is_idempotent(store):
    store.ensure_session("s1")
    store.ensure_session("s1")
    assert len(store.sessions()) == 1


def test_sessions_sorted_by_recent_activity(tmp_path):
    s = SessionStore(tmp_path / "s.db").open()
    s.ensure_session("old")
    s.append("old", "user", "x")
    # 不能靠两次 ensure_session 之间的 time.time() 拉开差距：Windows 时钟粒度
    # 约 15ms，两次调用可能拿到同一个 last_active，而 ORDER BY last_active DESC
    # 对并列行不定义顺序——测试会随机失败。显式把 old 拨早。
    s._db.execute("UPDATE sessions SET last_active = last_active - 10 WHERE id = 'old'")
    s.ensure_session("new")
    assert s.sessions()[0]["id"] == "new"
    s.close()


def test_by_tag_matches_kind(tmp_path):
    s = SessionStore(tmp_path / "s.db").open()
    s.ensure_session("g1", kind="group")
    s.ensure_session("p1", kind="private")
    assert [x["id"] for x in s.by_tag("group")] == ["g1"]
    assert [x["id"] for x in s.by_tag("private")] == ["p1"]
    s.close()


def test_sessions_are_isolated(store):
    """一个会话的消息不会串到另一个会话里。"""
    store.ensure_session("other")
    store.append("s1", "user", "属于 s1")
    store.append("other", "user", "属于 other")
    assert [m["content"] for m in store.history("s1")] == ["属于 s1"]
    assert [m["content"] for m in store.history("other")] == ["属于 other"]


# ---------- 清空会话历史 ----------


def test_clear_removes_only_that_session(store):
    """清空只动目标会话，别的会话不受影响。"""
    store.ensure_session("other")
    store.append("s1", "user", "一")
    store.append("s1", "assistant", "二")
    store.append("other", "user", "别动我")

    assert store.clear("s1") == 2  # 返回删掉的条数
    assert store.history("s1") == []
    assert store.history("other") == [{"role": "user", "content": "别动我"}]


def test_clear_keeps_the_session_row(store):
    """清空历史但保留会话本身——清了还是同一个会话（platform/kind/owner 不该丢）。"""
    store.append("s1", "user", "一")
    store.clear("s1")
    assert store.count("s1") == 0
    assert [s["id"] for s in store.sessions()] == ["s1"]  # 会话行还在


def test_clear_empty_returns_zero(store):
    """本来就空的会话，清空返回 0，不报错。"""
    assert store.clear("s1") == 0


# ---------- WAL 与生命周期 ----------


def test_wal_mode_enabled(store):
    mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_schema_tables_created(store):
    names = {r[0] for r in store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"sessions", "messages", "session_cursors"} <= names


def test_open_is_idempotent(tmp_path):
    s = SessionStore(tmp_path / "s.db")
    s.open().open()
    s.close()


def test_context_manager(tmp_path):
    with SessionStore(tmp_path / "s.db") as s:
        s.ensure_session("s1")
        s.append("s1", "user", "hi")
    assert s._conn is None  # 退出后连接已关


def test_using_without_open_raises(tmp_path):
    with pytest.raises(RuntimeError, match="未打开"):
        SessionStore(tmp_path / "s.db").history("s1")


def test_accepts_str_path(tmp_path):
    s = SessionStore(str(tmp_path / "s.db")).open()
    s.ensure_session("s1")
    assert s.path.name == "s.db"
    s.close()


def test_new_db_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "s.db"
    s = SessionStore(nested).open()
    assert nested.exists()
    s.close()
