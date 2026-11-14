"""测试：M1-03 多会话入口（会话列表投影 + /api/sessions + 标题）。

纯离线：SessionStore 用 tmp SQLite；WebAdapter 用 TestClient。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core.adapter.web.adapter import WebAdapter
from core.session.store import SessionStore


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "sessions.db").open()
    yield s
    s.close()


# ---------- sessions_for_web ----------

def test_sessions_for_web_only_web_platform(store):
    """WebUI 列表只给 web 平台的会话，不枚举 QQ/CLI（越权观察）。"""
    store.ensure_session("web:a", platform="web", owner="local")
    store.ensure_session("qq:p123", platform="qq", owner="123")
    store.ensure_session("cli:local", platform="cli", owner="local")

    ids = [s["id"] for s in store.sessions_for_web()]
    assert ids == ["web:a"]


def test_sessions_for_web_fields(store):
    """投影字段：title 兜底 / count / preview / date。"""
    store.ensure_session("web:ab12cd34", platform="web", owner="local")
    store.append("web:ab12cd34", "user", "我下周去北京，记得提醒我")
    store.append("web:ab12cd34", "assistant", "好呀，我记下了")

    rows = store.sessions_for_web()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "web:ab12cd34"
    assert row["title"] == "会话 ab12cd34"          # 没设标题 → 兜底名
    assert row["count"] == 2
    assert row["preview"] == "好呀，我记下了"        # 最后一条
    assert len(row["date"]) == 5                    # "MM-DD"


def test_sessions_for_web_count_respects_reset(store):
    """&newtale 划重置线之后，count 只算线之后的消息（与历史一致）。"""
    store.ensure_session("web:x", platform="web", owner="local")
    store.append("web:x", "user", "旧消息")
    store.mark_reset("web:x")
    store.append("web:x", "user", "新消息")

    row = store.sessions_for_web()[0]
    assert row["count"] == 1
    assert row["preview"] == "新消息"


# ---------- set_title_if_empty ----------

def test_set_title_if_empty_sets_once(store):
    """标题只定一次：后续消息不覆盖。"""
    store.ensure_session("web:t", platform="web", owner="local")
    store.set_title_if_empty("web:t", "第一句标题")

    assert store.sessions_for_web()[0]["title"] == "第一句标题"

    store.set_title_if_empty("web:t", "第二句不该覆盖")
    assert store.sessions_for_web()[0]["title"] == "第一句标题"


def test_set_title_clips_and_flattens_newlines(store):
    """标题截断到 20 字、换行变空格。"""
    store.ensure_session("web:t", platform="web", owner="local")
    store.set_title_if_empty("web:t", "第一行\n" + "很长的内容" * 10)

    title = store.sessions_for_web()[0]["title"]
    assert len(title) <= 20
    assert "\n" not in title


def test_set_title_empty_is_noop(store):
    """空标题不写——留给列表兜底名，别把 title 写成空串。"""
    store.ensure_session("web:t", platform="web", owner="local")
    store.set_title_if_empty("web:t", "   ")
    assert store.sessions_for_web()[0]["title"] == "会话 t"


# ---------- GET /api/sessions ----------

def test_api_sessions_returns_provider_data():
    """注入 provider → 路由原样返回（ok=true）。"""
    fake = [{"id": "web:a", "title": "会话 a", "kind": "private",
             "last_active": 1.0, "date": "10-18", "count": 2, "preview": "嗨"}]
    adapter = WebAdapter(sessions_provider=lambda: fake)
    client = TestClient(adapter.app)

    r = client.get("/api/sessions")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["sessions"] == fake


def test_api_sessions_without_provider_is_404():
    """不注入 provider → 不挂路由（单会话模式，现有行为不变）。"""
    adapter = WebAdapter()
    client = TestClient(adapter.app)
    assert client.get("/api/sessions").status_code == 404


def test_api_sessions_provider_error_is_200_with_ok_false():
    """provider 抛异常 → 200 + ok=false（前端统一处理，不炸页面）。"""
    def boom():
        raise RuntimeError("boom")

    adapter = WebAdapter(sessions_provider=boom)
    client = TestClient(adapter.app)

    r = client.get("/api/sessions")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False
    assert "error" in j


def test_api_sessions_end_to_end_with_store(store):
    """真 store 注入 → 投影字段原样过 HTTP。"""
    store.ensure_session("web:e2e", platform="web", owner="local")
    store.append("web:e2e", "user", "你好")
    store.set_title_if_empty("web:e2e", "你好")

    adapter = WebAdapter(sessions_provider=store.sessions_for_web)
    client = TestClient(adapter.app)

    j = client.get("/api/sessions").json()
    assert j["ok"] is True
    assert j["sessions"][0]["id"] == "web:e2e"
    assert j["sessions"][0]["title"] == "你好"
