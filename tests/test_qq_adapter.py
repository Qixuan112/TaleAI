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

from core.adapter import qq as qq_pkg
from core.adapter.base import Message, Reply
from core.adapter.qq import adapter as qq_adapter_mod
from core.adapter.qq.adapter import QUOTED_MAX_CHARS, QQAdapter
from core.event_bus import EventBus


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


# ---------- 端点鉴权与多连接（PR #10 High） ----------


def test_token_mismatch_is_rejected():
    """配了 access_token 后，token 不符的连接被拒——否则谁都能顶掉 SnowLuma。"""
    a = QQAdapter()
    a.access_token = "sekret"
    with pytest.raises(Exception):  # TestClient 连不上就是被 close 了
        with TestClient(a.app).websocket_connect("/qq?access_token=wrong"):
            pass
    assert a.connected() is False


def test_token_via_query_is_accepted():
    a = QQAdapter()
    a.access_token = "sekret"
    with TestClient(a.app).websocket_connect("/qq?access_token=sekret"):
        assert a.connected() is True


def test_token_via_bearer_is_accepted():
    a = QQAdapter()
    a.access_token = "sekret"
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"Authorization": "Bearer sekret"}
    ):
        assert a.connected() is True


def test_no_token_configured_means_open():
    """默认（没配 token）不校验——保持 M0 单机用法不变。"""
    a = QQAdapter()
    assert a.access_token == ""
    with TestClient(a.app).websocket_connect("/qq"):
        assert a.connected() is True


def test_new_connection_supersedes_and_closes_old():
    """新连接顶上：bot_id 跟随新连接，且旧连接被主动关闭而不是留成哑巴。

    旧实现的 bug：只替换 _link、不关旧的 → 旧连接还开着（能收事件），
    新连接一断 _link 置 None，旧连接却被当成"当前连接"以外的、来不及回退，
    机器人彻底变哑（PR #10）。
    """
    a = QQAdapter()
    client = TestClient(a.app)
    with client.websocket_connect("/qq", headers={"X-Self-ID": "10001"}) as first:
        assert a.bot_id == "10001"
        with client.websocket_connect("/qq", headers={"X-Self-ID": "10002"}) as second:
            # 新连接顶上但没断：仍算连接着，bot_id 已更新
            assert a.connected() is True
            assert a.bot_id == "10002"
        # 第二条断开后：没有残留的活连接挂在 _link 上
        assert a.connected() is False
    # 第一条（被服务端主动 close 过）退出时也不该把状态搞乱
    assert a.connected() is False


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
    """没连上就发 → 记日志跳过，不抛（跟 WebAdapter 一致）。"""
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


# ---------- 图片下载（UX-08）----------


def test_image_downloaded_into_images(monkeypatch):
    """带图消息：URL 被下载 → 文件名进 Message.images。"""
    monkeypatch.setattr(qq_adapter_mod, "download_image",
                        lambda url: "fake_" + url.rsplit("/", 1)[-1])
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(private_event(text=[
            {"type": "text", "data": {"text": "看图"}},
            {"type": "image", "data": {"url": "https://x/a.png"}},
        ]))
        m = _drain(a)
    assert m.images == ["fake_a.png"]
    assert m.content == "看图"


def test_pure_image_message_is_delivered(monkeypatch):
    """纯图片（无文字）不再被丢弃——图就是内容。"""
    monkeypatch.setattr(qq_adapter_mod, "download_image", lambda url: "img.png")
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(private_event(text=[
            {"type": "image", "data": {"url": "https://x/only.png"}},
        ]))
        m = _drain(a)
    assert m.images == ["img.png"]
    assert m.content == ""


def test_image_download_failure_degrades_to_text(monkeypatch):
    """下载失败（返回 None）→ 当无图，不能卡住链路。"""
    monkeypatch.setattr(qq_adapter_mod, "download_image", lambda url: None)
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(private_event(text=[
            {"type": "text", "data": {"text": "文字还在"}},
            {"type": "image", "data": {"url": "https://x/broken.png"}},
        ]))
        m = _drain(a)
    assert m.images == []
    assert m.content == "文字还在"


def test_image_download_capped(monkeypatch):
    monkeypatch.setattr(qq_adapter_mod, "download_image", lambda url: "i.png")
    monkeypatch.setattr(qq_adapter_mod, "MAX_IMAGES_PER_MESSAGE", 2)
    a = QQAdapter()
    with TestClient(a.app).websocket_connect(
        "/qq", headers={"X-Self-ID": "10001"}
    ) as ws:
        ws.send_json(private_event(text=[
            {"type": "image", "data": {"url": f"https://x/{i}.png"}} for i in range(5)
        ]))
        m = _drain(a)
    assert len(m.images) == 2


# ---------- 启停信号（PR1：QQService 靠它管生命周期）----------


def test_serving_false_before_start():
    a = QQAdapter()
    assert a.serving() is False


def test_stop_requests_uvicorn_shutdown():
    class FakeServer:
        def __init__(self):
            self.started = True
            self.should_exit = False

    a = QQAdapter()
    a._server = FakeServer()
    assert a.serving() is True
    a.stop()
    assert a._server.should_exit is True


def test_stop_without_server_is_safe():
    a = QQAdapter()
    a.stop()  # 不抛即通过


# ---------- 引用内容（PR2：get_msg 拉被引用消息）----------


def reply_event(reply_id="42", text="这句怎么样", inline=None):
    """引用了一条消息的私聊事件。inline 非 None 时模拟"reply 段内联原文"。"""
    data = {"id": reply_id}
    if inline is not None:
        data["text"] = inline
    return private_event(text=[
        {"type": "reply", "data": data},
        {"type": "text", "data": {"text": text}},
    ])


def test_quoted_content_is_fetched_via_get_msg():
    """引用消息：适配器应发 get_msg，把被引用原文填进 Message.quoted。

    这条测试同时钉住**不死锁**：_on_frame 若 inline await get_msg 应答，
    应答帧走同一个收包循环、永远送不进来——ws.receive_json 会在这里挂死。
    """
    a = QQAdapter()
    with TestClient(a.app).websocket_connect("/qq") as ws:
        ws.send_json(reply_event(reply_id="42"))
        # 服务端发出的下一个动作应当是 get_msg（在等它的应答）
        action = ws.receive_json()
        assert action["action"] == "get_msg"
        assert action["params"]["message_id"] == 42
        ws.send_json({
            "status": "ok", "retcode": 0,
            "data": {"message_id": 42, "message": [
                {"type": "text", "data": {"text": "被引用的原话"}},
            ]},
            "echo": action["echo"],
        })
        m = _drain(a)
    assert m.reply_to == "42"
    assert m.quoted == "被引用的原话"


def test_quoted_fetch_failure_degrades_to_no_quote(monkeypatch):
    """get_msg 超时：消息照常投递，只是 quoted 为空——不能卡死链路。"""
    monkeypatch.setattr(qq_adapter_mod, "_API_TIMEOUT", 0.05)
    a = QQAdapter()
    with TestClient(a.app).websocket_connect("/qq") as ws:
        ws.send_json(reply_event())
        ws.receive_json()  # get_msg 动作，但故意不应答
        m = _drain(a)
    assert m.quoted == ""
    assert m.content == "这句怎么样"


def test_inline_quoted_text_skips_get_msg():
    """reply 段自带原文：直接用，不发 get_msg 动作。"""
    a = QQAdapter()
    link = FakeLink()
    a._link = link
    msg = Message(
        id="m9", platform="qq", session_id="qq:p1", owner="u1",
        direction="in", role="user", content="回你",
        reply_to="777", quoted="内联的原文",
    )
    asyncio.run(a._finish_message(msg))
    assert link.sent == []       # 没发任何动作
    assert a._inbox.qsize() == 1  # 照常投递


async def test_fetch_quoted_truncates_to_limit():
    """被引用原文过长 → 截到 QUOTED_MAX_CHARS（只给模型看个大意）。"""
    a = QQAdapter()

    async def fake_call(action, params, *, timeout=None):
        assert action == "get_msg"
        return {"message": [{"type": "text", "data": {"text": "长" * 500}}]}

    a._call_api = fake_call
    text = await a._fetch_quoted("42")
    assert len(text) == QUOTED_MAX_CHARS


async def test_fetch_quoted_passes_numeric_message_id():
    """OneBot 的 message_id 是 number：合法数字串要转回 int 再发。"""
    a = QQAdapter()
    seen = {}

    async def fake_call(action, params, *, timeout=None):
        seen.update(params)
        return None

    a._call_api = fake_call
    await a._fetch_quoted("42")
    assert seen == {"message_id": 42}


async def test_call_api_resolves_on_ok_echo():
    """_call_api 发动作、收到同 echo 的 ok 应答 → 返回 data。"""
    a = QQAdapter()
    link = FakeLink()
    a._link = link

    async def respond():
        await asyncio.sleep(0)  # 让 _call_api 先把动作发出去
        assert link.sent[0]["action"] == "get_msg"
        await a._on_frame({"status": "ok", "retcode": 0, "data": {"k": 1},
                           "echo": link.sent[0]["echo"]})

    t = asyncio.create_task(respond())
    data = await a._call_api("get_msg", {"message_id": 1})
    await t
    assert data == {"k": 1}
    assert a._pending == {}  # 用完即清，不留残骸


async def test_call_api_timeout_returns_none(monkeypatch):
    monkeypatch.setattr(qq_adapter_mod, "_API_TIMEOUT", 0.02)
    a = QQAdapter()
    a._link = FakeLink()
    assert await a._call_api("get_msg", {"message_id": 1}) is None
    assert a._pending == {}


async def test_call_api_error_retcode_returns_none():
    """retcode != 0（如"消息不存在/无法获取"）→ 当失败，返回 None。"""
    a = QQAdapter()
    link = FakeLink()
    a._link = link

    async def respond():
        await asyncio.sleep(0)
        await a._on_frame({"status": "failed", "retcode": 100, "data": None,
                           "echo": link.sent[0]["echo"]})

    t = asyncio.create_task(respond())
    assert await a._call_api("get_msg", {}) is None
    await t


async def test_call_api_without_link_returns_none():
    assert await QQAdapter()._call_api("get_msg", {}) is None


async def test_cancel_pending_cancels_futures():
    """断连时在途请求全取消——不然等应答的那边要白等一整段超时。"""
    a = QQAdapter()
    fut = asyncio.get_running_loop().create_future()
    a._pending["call-x"] = fut
    a._cancel_pending()
    assert fut.cancelled()
    assert a._pending == {}


async def test_send_echo_still_ignored_when_nobody_waits():
    """没人等的应答（send_* 的回包）不进 pending、不该被当消息投递。"""
    a = QQAdapter()
    await a._on_frame({"status": "ok", "retcode": 0, "data": {},
                       "echo": "send-qq:p1-abc123"})
    assert a._inbox.qsize() == 0
