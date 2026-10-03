"""M0-11 验收：WebSocketAdapter 的收发与归一（§21「WS 收发 + 统一消息模型」）。

分两半：
- **收**用 FastAPI 的 TestClient 驱动真实 WS 端点（不绑真实端口）；
- **发**用一个假 socket 直接测 `send()` 的组装与去向（不必起 WS）。

测的是归一与收发逻辑，不是 uvicorn 能不能启动——那是 M0-13 联调冒烟的事。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core.adapter.base import Reply
from core.adapter.websocket.adapter import DEFAULT_SESSION_ID, WebSocketAdapter
from core.bus.event_bus import EventBus


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


class FakeSocket:
    """假连接：记下 send_json 收到的东西，模拟一条已建立的 WS。"""

    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    async def send_json(self, payload):
        if self.fail:
            raise RuntimeError("连接已断")
        self.sent.append(payload)


def _drain_inbox(adapter: WebSocketAdapter):
    """同步取一条（在 TestClient 上下文内调用，消息已投递）。"""
    return asyncio.run(asyncio.wait_for(adapter.recv(), timeout=1))


# ---------- normalize：原始 JSON → Message ----------


def test_normalize_basic():
    a = WebSocketAdapter()
    m = a.normalize({"content": "你好", "session_id": "web:local"})
    assert m.platform == "websocket"
    assert m.session_id == "web:local"
    assert m.role == "user"
    assert m.direction == "in"
    assert m.content == "你好"


def test_normalize_generates_unique_ids():
    """id 由服务端生成：客户端不该操心全局唯一性，两条消息不能撞 id。"""
    a = WebSocketAdapter()
    m1 = a.normalize({"content": "a"})
    m2 = a.normalize({"content": "b"})
    assert m1.id and m2.id and m1.id != m2.id


def test_normalize_defaults_session_id():
    """裸连（没带 session_id）也要能用——落到默认会话。"""
    a = WebSocketAdapter()
    assert a.normalize({"content": "x"}).session_id == DEFAULT_SESSION_ID


def test_normalize_strips_content():
    a = WebSocketAdapter()
    assert a.normalize({"content": "  有空格  "}).content == "有空格"


# ---------- 收：WS → 收件箱 ----------


def test_incoming_ws_message_lands_in_inbox():
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws?session_id=web:local") as ws:
        ws.send_json({"content": "在吗"})
        msg = _drain_inbox(a)
    assert msg.content == "在吗"
    assert msg.session_id == "web:local"
    assert msg.platform == "websocket"


def test_empty_message_is_not_delivered():
    """空消息不发请求（跟 CLI 的空行一致）——省一次无意义的模型调用。"""
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws") as ws:
        ws.send_json({"content": "   "})
        ws.send_json({"content": "有内容了"})
        msg = _drain_inbox(a)
    assert msg.content == "有内容了"
    assert a._inbox.qsize() == 0


def test_pure_image_message_is_delivered():
    """纯图消息（只有图、无文字）是合法输入——不能被空守卫丢掉（UX-07）。

    兔老师抓到：空守卫只看 content，纯图消息会被当空 `continue` 掉。
    """
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws") as ws:
        ws.send_json({"content": "", "images": ["pic.png"]})
        msg = _drain_inbox(a)
    assert msg.images == ["pic.png"]
    assert msg.content == ""


def test_truly_empty_message_still_dropped():
    """既无文字又无图，才是真空——仍要丢。"""
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws") as ws:
        ws.send_json({"content": "  "})
        ws.send_json({"content": "真消息"})
        msg = _drain_inbox(a)
    assert msg.content == "真消息"
    assert a._inbox.qsize() == 0


def test_connection_registers_and_cleans_up_session():
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws?session_id=web:abc"):
        assert "web:abc" in a.connected_sessions()
    # 断开后应清理（否则连接表会一直涨）
    assert "web:abc" not in a.connected_sessions()


def test_two_sessions_are_tracked_separately():
    a = WebSocketAdapter()
    client = TestClient(a.app)
    with client.websocket_connect("/ws?session_id=a"):
        with client.websocket_connect("/ws?session_id=b"):
            assert set(a.connected_sessions()) == {"a", "b"}


# ---------- 发：Reply → 连接 ----------


async def test_send_pushes_reply_to_the_right_connection():
    a = WebSocketAdapter()
    sock = FakeSocket()
    a._connections["web:local"] = sock
    await a.send(Reply(session_id="web:local", messages=["你好呀~"]))
    assert sock.sent[0]["session_id"] == "web:local"
    assert sock.sent[0]["messages"] == ["你好呀~"]


async def test_send_payload_shape():
    """回包字段：messages 是列表、带 tool_calls_made 与 stop_reason。"""
    a = WebSocketAdapter()
    sock = FakeSocket()
    a._connections["s1"] = sock
    await a.send(Reply(session_id="s1", messages=["一", "二"], tool_calls_made=2))
    assert sock.sent[0] == {
        "session_id": "s1",
        "messages": ["一", "二"],
        "tool_calls_made": 2,
        "stop_reason": "ok",
    }


async def test_send_to_unknown_session_is_skipped_not_raised():
    """会话已断开（用户关了页面）→ 跳过，不抛：不该让前台循环崩在发不出去。"""
    a = WebSocketAdapter()
    await a.send(Reply(session_id="never-connected", messages=["x"]))  # 不抛即通过


async def test_send_failure_on_broken_socket_is_swallowed():
    """连接刚好在发送时断掉——记日志跳过，不把异常抛回前台循环。"""
    a = WebSocketAdapter()
    sock = FakeSocket()
    sock.fail = True
    a._connections["s1"] = sock
    await a.send(Reply(session_id="s1", messages=["x"]))  # 不抛即通过


# ---------- 重连时补发历史（M0 验收⑤：刷新后历史完整） ----------


def test_connect_pushes_history_to_client():
    """连上就把该会话的历史推给前端。

    没有这一步，"刷新后历史完整"只是数据完整——用户看到的是空白页。
    session_id 靠 localStorage 保住了、库里也有历史，但没人把它送到页面上。
    """
    a = WebSocketAdapter(history_provider=lambda sid: [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "哟，来啦~"},
    ])
    with TestClient(a.app).websocket_connect("/ws?session_id=web:local") as ws:
        first = ws.receive_json()
    assert first["type"] == "history"
    assert first["messages"] == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "哟，来啦~"},
    ]


def test_history_provider_is_asked_for_the_right_session():
    asked = []

    def provider(sid):
        asked.append(sid)
        return [{"role": "user", "content": "hi"}]

    a = WebSocketAdapter(history_provider=provider)
    with TestClient(a.app).websocket_connect("/ws?session_id=web:xyz") as ws:
        ws.receive_json()  # 有历史才会推帧，这里等一下让它发出来
    assert asked == ["web:xyz"]


def test_empty_history_sends_no_frame():
    """历史为空就不推帧——前端不会收到一个空的历史帧。"""
    a = WebSocketAdapter(history_provider=lambda sid: [])
    with TestClient(a.app).websocket_connect("/ws?session_id=s") as ws:
        ws.send_json({"content": "在吗"})
        import asyncio

        assert asyncio.run(asyncio.wait_for(a.recv(), timeout=1)).content == "在吗"


def test_no_history_provider_means_no_history_frame():
    """没配 history_provider 时不发历史帧——保持零依赖可用（测试/裸连场景）。"""
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws?session_id=x") as ws:
        ws.send_json({"content": "在吗"})
        # 下一条应该是收件箱里的消息，而不是历史帧
        import asyncio

        assert asyncio.run(asyncio.wait_for(a.recv(), timeout=1)).content == "在吗"


def test_history_frame_is_not_a_chat_message():
    """历史帧带 type=history，不能跟对话回复混——前端据此分流。"""
    a = WebSocketAdapter(history_provider=lambda sid: [{"role": "user", "content": "hi"}])
    with TestClient(a.app).websocket_connect("/ws?session_id=s") as ws:
        frame = ws.receive_json()
    assert frame["type"] == "history"
    assert "tool_calls_made" not in frame


# ---------- 清空历史（网页上的「清空历史」按钮） ----------


def test_clear_frame_calls_clearer_and_replies():
    """收到 {action: clear} → 调 clearer(session) → 回 {type: cleared, deleted}。"""
    cleared = []

    def clearer(sid):
        cleared.append(sid)
        return 3

    a = WebSocketAdapter(clearer=clearer)
    with TestClient(a.app).websocket_connect("/ws?session_id=web:me") as ws:
        ws.send_json({"action": "clear", "session_id": "web:me"})
        frame = ws.receive_json()
    assert cleared == ["web:me"]
    assert frame["type"] == "cleared"
    assert frame["deleted"] == 3
    assert frame["ok"] is True


def test_clear_frame_is_not_a_chat_message():
    """清空帧不能被当成聊天消息投进收件箱（它没有 content）。"""
    a = WebSocketAdapter(clearer=lambda sid: 0)
    with TestClient(a.app).websocket_connect("/ws?session_id=s") as ws:
        ws.send_json({"action": "clear", "session_id": "s"})
        ws.receive_json()  # cleared 回执
    # 收件箱里不该有东西（若被误当消息，这里会取到一条）
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(a.recv(), timeout=0.3))


def test_clear_without_clearer_replies_ok_but_zero():
    """没注入 clearer 时不报错——回 ok 且 deleted=0（清空不在核心链路上）。"""
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws?session_id=s") as ws:
        ws.send_json({"action": "clear", "session_id": "s"})
        frame = ws.receive_json()
    assert frame["type"] == "cleared"
    assert frame["deleted"] == 0


def test_clear_failure_reports_not_ok():
    """clearer 抛异常 → 回 ok=False，不把异常炸回 WS 循环。"""
    def boom(sid):
        raise RuntimeError("库坏了")

    a = WebSocketAdapter(clearer=boom)
    with TestClient(a.app).websocket_connect("/ws?session_id=s") as ws:
        ws.send_json({"action": "clear", "session_id": "s"})
        frame = ws.receive_json()
    assert frame["ok"] is False


# ---------- 静态页（M0-12 的挂钩，这里确认路由不挡 WS） ----------


def test_ws_endpoint_works_regardless_of_webui_dir():
    """webui/ 还没建时，/ws 也必须能用——聊天页是 M0-12，不该挡 WS 验收。"""
    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws?session_id=x") as ws:
        ws.send_json({"content": "still works"})
        assert _drain_inbox(a).content == "still works"
