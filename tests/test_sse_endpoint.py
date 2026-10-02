"""SSE 端点接线：/events 与 /logs 的挂载与契约。

关键约束：**只有注入了 stream 才挂**这两个路由——不注入时行为跟以前完全一样，
否则几十个构造裸适配器的用例会集体回归。

注意：用「有限帧的假 stream」来测端点。真的 `LogStream.stream()` 是无限生成器，
TestClient 读它会一直阻塞（实测）。无限流的语义另有 test_log_stream.py 覆盖；
这里只证明「路由挂对了、接了广播器、帧格式是 SSE」。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

from core.adapter.websocket.adapter import WebSocketAdapter
from core.bus.event_bus import EventBus
from core.log_stream import LogStream


class OneShotStream:
    """只吐固定几帧就结束的假 stream——让 /events 能测完不挂住。"""

    def __init__(self, frames):
        self._frames = frames

    async def stream(self):
        for f in self._frames:
            yield "data: " + json.dumps(f, ensure_ascii=False) + "\n\n"


def _paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


# ---------- 不注入 stream：老行为不变 ----------


def test_no_stream_means_no_events_route():
    """裸构造的适配器不该有 /events——大量用例依赖它保持原样。"""
    a = WebSocketAdapter()
    assert "/events" not in _paths(a.app)
    assert "/logs" not in _paths(a.app)


# ---------- 注入 stream：两条路由都在 ----------


def test_stream_adds_events_and_logs_routes():
    a = WebSocketAdapter(stream=LogStream())
    paths = _paths(a.app)
    assert "/events" in paths
    assert "/logs" in paths


def test_logs_redirects_to_debug_page():
    a = WebSocketAdapter(stream=LogStream())
    r = TestClient(a.app, follow_redirects=False).get("/logs")
    assert r.status_code in (307, 308)
    assert "logs.html" in r.headers["location"]


def test_events_serves_published_frames_as_sse():
    """推一帧能经 SSE 读到——证明端点真的接了广播器、且是 event-stream 类型。"""
    a = WebSocketAdapter(stream=OneShotStream([
        {"type": "log", "level": "INFO", "logger": "t", "message": "你好"},
    ]))
    r = TestClient(a.app).get("/events")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "data: " in r.text
    assert "你好" in r.text


def test_chat_ws_still_works_with_stream_injected():
    """/events 与 /logs 不能挡住 /ws——聊天照常。"""
    import asyncio

    a = WebSocketAdapter(stream=LogStream())
    with TestClient(a.app).websocket_connect("/ws?session_id=x") as ws:
        ws.send_json({"content": "还在吗"})
        got = asyncio.run(asyncio.wait_for(a.recv(), timeout=1))
    assert got.content == "还在吗"
