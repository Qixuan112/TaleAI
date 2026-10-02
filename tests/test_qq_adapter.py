"""测试：QQAdapter 的反向 WS 行为（用 TestClient 假装 SnowLuma 连进来）。

反向 WS 的一个意外好处：**适配器逻辑可以自动化测试**——不需要真 QQ 账号，
让 TestClient 扮演 SnowLuma、连上我们的 /qq 端点、发事件、收回调即可。
真登录/真收发才需要人工冒烟（M0-13 的清单里那份）。

契约来源：OneBot 11 communication/ws-reverse.md（X-Self-ID / X-Client-Role 头）。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core.adapter.base import Reply
from core.adapter.qq.adapter import QQAdapter
from core.bus.event_bus import EventBus


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


def private_event(user_id=20002000, text="你好呀"):
    return {
        "time": 1720000000, "self_id": 10001,
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "message_id": 123456, "user_id": user_id,
        "message": text, "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": "老板", "sex": "male", "age": 30},
    }


def _drain(adapter):
    return asyncio.run(asyncio.wait_for(adapter.recv(), timeout=1))


# ---------- 反向 WS：SnowLuma 连进来 ----------


def test_reverse_ws_accepts_connection_with_onebot_headers():
    """OneBot 反向 WS 握手带 X-Self-ID / X-Client-Role（规范要求读它）。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001", "X-Client-Role": "Universal"}
    ):
        assert a.connected() is True


def test_connected_bot_id_is_learned_from_handshake():
    """机器人自己的 QQ 号从 X-Self-ID 学来——不用手填配置。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001", "X-Client-Role": "Universal"}
    ):
        assert a.bot_id == "10001"


def test_private_message_lands_in_inbox():
    a = QQAdapter()
    with TestClient(a.app).websocket_connect("/qq") as ws:
        ws.send_json(private_event(text="在吗"))
        m = _drain(a)
    assert m.platform == "qq"
    assert m.content == "在吗"
    assert m.session_id == "qq:p20002000"


def test_duplicate_message_id_is_ignored():
    """平台可能重发同一条（断线重连时常见）——按 message_id 去重。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect("/qq") as ws:
        ws.send_json(private_event(text="只发一次"))
        ws.send_json(private_event(text="只发一次"))  # 同一个 message_id
        m = _drain(a)
    assert m.content == "只发一次"
    assert a._inbox.qsize() == 0  # 第二条被丢弃


def test_meta_event_is_not_delivered():
    a = QQAdapter()
    with TestClient(a.app).websocket_connect("/qq") as ws:
        ws.send_json({"post_type": "meta_event", "meta_event_type": "heartbeat"})
        ws.send_json(private_event(text="真消息"))
        m = _drain(a)
    assert m.content == "真消息"


def test_disconnect_clears_connection():
    a = QQAdapter()
    with TestClient(a.app).websocket_connect("/qq"):
        assert a.connected()
    assert not a.connected()


# ---------- 发：Reply → OneBot 动作 ----------


class FakeLink:
    """假连接：记下发出去的 OneBot 动作。"""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def test_send_private_reply_uses_private_action():
    a = QQAdapter()
    link = FakeLink()
    a._link = link
    await a.send(Reply(session_id="qq:p20002000", messages=["你好呀~"]))
    assert link.sent[0]["action"] == "send_private_msg"
    assert link.sent[0]["params"]["user_id"] == 20002000


async def test_send_group_reply_uses_group_action():
    a = QQAdapter()
    link = FakeLink()
    a._link = link
    await a.send(Reply(session_id="qq:g30003000", messages=["大家好"]))
    assert link.sent[0]["action"] == "send_group_msg"
    assert link.sent[0]["params"]["group_id"] == 30003000


async def test_send_splits_multiple_messages_into_separate_actions():
    """多条 <msg> 应当分条发出去，不是拼成一大段。

    QQ 没有"气泡"概念，但**可以发多条消息**——每条一个动作。拼成一段会让
    塔利"先应一声、再答正文"这种节奏没了（WebUI 里是分开气泡，QQ 该一致）。
    """
    a = QQAdapter()
    link = FakeLink()
    a._link = link
    await a.send(Reply(session_id="qq:p1", messages=["第一句", "第二句"]))
    assert len(link.sent) == 2
    texts = [x["params"]["message"][0]["data"]["text"] for x in link.sent]
    assert texts == ["第一句", "第二句"]


async def test_split_sends_keep_order_and_target():
    a = QQAdapter()
    link = FakeLink()
    a._link = link
    await a.send(Reply(session_id="qq:g30003000", messages=["一", "二", "三"]))
    assert [x["params"]["message"][0]["data"]["text"] for x in link.sent] == ["一", "二", "三"]
    assert all(x["action"] == "send_group_msg" for x in link.sent)
    assert all(x["params"]["group_id"] == 30003000 for x in link.sent)


async def test_blank_messages_are_skipped_when_splitting():
    """空的 <msg> 不该发出去——白气泡很突兀。"""
    a = QQAdapter()
    link = FakeLink()
    a._link = link
    await a.send(Reply(session_id="qq:p1", messages=["有内容", "", "  ", "也有"]))
    assert len(link.sent) == 2


async def test_single_message_still_one_action():
    a = QQAdapter()
    link = FakeLink()
    a._link = link
    await a.send(Reply(session_id="qq:p1", messages=["就一句"]))
    assert len(link.sent) == 1


async def test_split_waits_between_messages_but_not_before_first(monkeypatch):
    """分条之间要停一下（第一条不等待），停顿节奏交给 pacing.typing_delay。

    这里只验证"调用时机与参数"：停在第 2、3 条**之前**，且问的是**下一条**的
    长度（真人打多久取决于接下来要敲多少）。真正的时长公式由 test_pacing 覆盖。
    """
    import core.adapter.qq.adapter as qq_mod

    asked: list[str] = []
    monkeypatch.setattr(qq_mod, "typing_delay", lambda text: asked.append(text) or 0.0)

    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(qq_mod.asyncio, "sleep", fake_sleep)

    a = QQAdapter()
    a._link = FakeLink()
    await a.send(Reply(session_id="qq:p1", messages=["一", "二", "三"]))

    assert asked == ["二", "三"]      # 问的是下一条，且第一条不问
    assert len(slept) == 2            # 三条之间停两次


async def test_send_without_connection_is_skipped_not_raised():
    """没连上就发 → 记日志跳过，不抛（跟 WebSocketAdapter 一致）。"""
    a = QQAdapter()
    await a.send(Reply(session_id="qq:p1", messages=["x"]))  # 不抛即通过


def test_api_response_is_consumed_not_delivered():
    """带 echo 的是我们发出动作的应答，不该当成用户消息。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect("/qq") as ws:
        ws.send_json({"status": "ok", "retcode": 0, "data": {}, "echo": "abc"})
        ws.send_json(private_event(text="我是真消息"))
        m = _drain(a)
    assert m.content == "我是真消息"


async def test_send_failure_swallowed():
    class BadLink:
        async def send_json(self, payload):
            raise RuntimeError("断了")

    a = QQAdapter()
    a._link = BadLink()
    await a.send(Reply(session_id="qq:p1", messages=["x"]))  # 不抛即通过


# ---------- 群聊过滤：只回 @（避免在活跃群里刷屏） ----------


def group_event_at(bot_id=10001, user_id=20002000, text=" 塔利在吗"):
    """群里 @ 了机器人的消息。"""
    return {
        "time": 1720000000, "self_id": bot_id,
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "message_id": 555, "group_id": 30003000, "user_id": user_id,
        "anonymous": None, "font": 0,
        "message": [{"type": "at", "data": {"qq": str(bot_id)}},
                    {"type": "text", "data": {"text": text}}],
        "sender": {"user_id": user_id, "nickname": "老板", "card": "老板", "role": "member"},
    }


def group_event_plain(user_id=20002000, text="今天天气不错"):
    """群里没 @ 机器人的普通消息。"""
    return {
        "time": 1720000000, "self_id": 10001,
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "message_id": 556, "group_id": 30003000, "user_id": user_id,
        "anonymous": None, "font": 0,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": user_id, "nickname": "老板", "card": "老板", "role": "member"},
    }


def test_group_message_without_at_is_delivered_with_addressed_false():
    """群聊没 @ 机器人 → **仍投递**，只把 addressed 标成 False。

    UX-03 改了这里：适配器不再自行丢弃未 @ 的群消息（那样"未唤醒也存进历史"
    就无从谈起），改由前台按唤醒策略决定——所以适配器只给平台事实。
    """
    a = QQAdapter()
    a.bot_id = "10001"
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(group_event_plain(text="别人在聊天"))
        m = _drain(a)
    assert m.content == "别人在聊天"
    assert m.addressed is False


def test_group_message_with_at_is_answered():
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(group_event_at())
        m = _drain(a)
    assert m.session_id == "qq:g30003000"
    assert m.addressed is True


def test_at_mention_is_stripped_from_content():
    """@ 机器人那段要从正文里去掉——模型不需要看到自己的 QQ 号被 at。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(group_event_at(text=" 塔利在吗"))
        m = _drain(a)
    assert m.content.strip() == "塔利在吗"
    assert "10001" not in m.content


def test_private_message_needs_no_at():
    """私聊不需要 @——1:1 会话，有人在说话就是在跟我说话。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(private_event(text="私聊直接说"))
        m = _drain(a)
    assert m.content == "私聊直接说"


def test_group_at_via_cq_string_also_recognized():
    """messageFormat=string 时，@ 藏在 CQ 码里——也要认出来。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ev = group_event_plain(text="")
        ev["message"] = "[CQ:at,qq=10001] 在吗"
        ws.send_json(ev)
        m = _drain(a)
    assert m.session_id == "qq:g30003000"
    assert m.addressed is True


def test_group_at_other_person_marks_not_addressed():
    """@ 的是别人不是机器人 → addressed=False（前台会按唤醒策略决定回不回）。"""
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ev = group_event_at()
        ev["message"] = [{"type": "at", "data": {"qq": "99999"}},
                         {"type": "text", "data": {"text": " @的是别人"}}]
        ws.send_json(ev)
        m = _drain(a)
    assert m.addressed is False
