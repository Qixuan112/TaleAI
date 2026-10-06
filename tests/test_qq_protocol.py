"""测试：OneBot 11 报文解析/构造（QQ 适配器的纯函数层）。

契约来源：OneBot 11 官方规范（botuniverse/onebot-11），SnowLuma 声明兼容。
- 消息事件：event/message.md（private / group）
- 发送动作：api/public.md（send_private_msg / send_group_msg）
- 信封：communication/ws.md（事件带 post_type，API 响应带 echo）

这层是纯函数，不碰网络——QQ 适配器最容易写错的就是这里（段数组 vs
CQ 码字符串、私聊 vs 群聊字段差异），所以单独拆出来测穿。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.adapter.qq.protocol import (
    build_send_action,
    extract_message_text,
    has_image,
    parse_event,
    parse_session_id,
)


# ---------- 私聊消息事件 ----------


def private_event(**over):
    """一份真实的私聊消息事件（字段取自 OneBot 11 规范）。"""
    e = {
        "time": 1720000000,
        "self_id": 10001,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 123456,
        "user_id": 20002000,
        "message": "你好呀",
        "raw_message": "你好呀",
        "font": 0,
        "sender": {"user_id": 20002000, "nickname": "老板", "sex": "male", "age": 30},
    }
    e.update(over)
    return e


def test_parse_private_event_to_message():
    m = parse_event(private_event())
    assert m is not None
    assert m.platform == "qq"
    assert m.role == "user"
    assert m.direction == "in"
    assert m.content == "你好呀"
    assert m.owner == "20002000"


def test_private_session_id_is_stable_and_prefixed():
    """私聊会话 ID = qq:p<对方QQ>——稳定、可反解、不与群聊撞。"""
    m = parse_event(private_event(user_id=20002000))
    assert m.session_id == "qq:p20002000"


def test_group_and_private_sessions_do_not_collide():
    """同一个数字出现在 user_id 和 group_id 上时，会话不能撞（前缀区分）。"""
    p = parse_event(private_event(user_id=555))
    g = parse_event(group_event(group_id=555))
    assert p.session_id != g.session_id


# ---------- 群消息事件 ----------


def group_event(**over):
    e = {
        "time": 1720000000,
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 999,
        "group_id": 30003000,
        "user_id": 20002000,
        "anonymous": None,
        "message": "大家好啊",
        "raw_message": "大家好啊",
        "font": 0,
        "sender": {"user_id": 20002000, "nickname": "老板", "card": "老板",
                   "role": "member"},
    }
    e.update(over)
    return e


def test_parse_group_event_to_message():
    m = parse_event(group_event())
    assert m.session_id == "qq:g30003000"
    assert m.owner == "20002000"  # 记忆隔离维度是发言者，不是群（§十八-4）
    assert m.content == "大家好啊"


def test_group_owner_is_the_speaker_not_the_group():
    """§十八-4：owner 是发言者（记忆按人隔离），群号只在 session_id 里。"""
    m = parse_event(group_event(group_id=30003000, user_id=20002000))
    assert m.owner == "20002000"
    assert "30003000" not in m.owner


# ---------- 消息内容：段数组 与 CQ 码字符串 ----------


def test_array_format_text_segments_joined():
    """messageFormat=array：message 是段数组，text 段要拼起来。"""
    m = parse_event(private_event(message=[
        {"type": "text", "data": {"text": "第一段"}},
        {"type": "text", "data": {"text": "第二段"}},
    ]))
    assert m.content == "第一段第二段"


def test_array_format_ignores_non_text_segments():
    m = parse_event(private_event(message=[
        {"type": "text", "data": {"text": "看图"}},
        {"type": "image", "data": {"file": "x.jpg"}},
        {"type": "text", "data": {"text": "好看吗"}},
    ]))
    assert m.content == "看图好看吗"


def test_string_format_with_cq_code_is_cleaned():
    """messageFormat=string：CQ 码要剥掉，不能把 [CQ:...] 原样喂给模型。"""
    m = parse_event(private_event(message="[CQ:at,qq=10001] 你好"))
    assert "[CQ:" not in m.content
    assert "你好" in m.content


def test_string_format_plain_text_untouched():
    m = parse_event(private_event(message="就是普通一句话"))
    assert m.content == "就是普通一句话"


# ---------- 图片 URL 提取（UX-08）----------


def test_array_image_segment_url_extracted():
    m = parse_event(private_event(message=[
        {"type": "text", "data": {"text": "看图"}},
        {"type": "image", "data": {"url": "https://x/a.png"}},
    ]))
    assert m.meta["image_urls"] == ["https://x/a.png"]
    assert m.content == "看图"   # 图片不进正文


def test_array_image_file_as_url_fallback():
    """有些实现把 URL 放 data.file 里。"""
    m = parse_event(private_event(message=[
        {"type": "image", "data": {"file": "https://x/b.jpg"}},
    ]))
    assert m.meta["image_urls"] == ["https://x/b.jpg"]


def test_array_image_without_url_is_ignored():
    m = parse_event(private_event(message=[
        {"type": "image", "data": {"file": "local.jpg"}},   # 不是 http
    ]))
    assert m.meta["image_urls"] == []


def test_string_cq_image_url_extracted():
    m = parse_event(private_event(
        message="[CQ:image,file=x.jpg,url=https://x/c.png] 看看"))
    assert m.meta["image_urls"] == ["https://x/c.png"]
    assert "[CQ:" not in m.content


def test_no_images_gives_empty_list():
    m = parse_event(private_event(message="纯文字"))
    assert m.meta["image_urls"] == []


def test_pure_image_message_has_image_but_no_text():
    """纯图片消息：正文空，但 image_urls 有值（不该被当成空消息丢掉）。"""
    m = parse_event(private_event(message=[
        {"type": "image", "data": {"url": "https://x/only.png"}},
    ]))
    assert m.content == ""
    assert m.meta["image_urls"] == ["https://x/only.png"]


# ---------- 提及（@） ----------


def test_array_format_at_segment_becomes_mention():
    m = parse_event(group_event(message=[
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": " 在吗"}},
    ]))
    assert "10001" in m.mentions


def test_mentions_empty_when_no_at():
    m = parse_event(private_event(message="没有 at"))
    assert m.mentions == []


# ---------- 引用回复（PR2）----------


def test_reply_segment_extracts_id():
    """array 形态的 reply 段 → reply_to 填被引用消息 ID；段本身不进正文。"""
    m = parse_event(private_event(message=[
        {"type": "reply", "data": {"id": "777"}},
        {"type": "text", "data": {"text": "这句怎么样"}},
    ]))
    assert m.reply_to == "777"
    assert m.content == "这句怎么样"


def test_reply_segment_inline_text_is_kept():
    """个别实现在 reply 段里内联被引用原文——有就直接用（省一次 get_msg）。"""
    m = parse_event(private_event(message=[
        {"type": "reply", "data": {"id": "777", "text": "被引用的原话"}},
        {"type": "text", "data": {"text": "回你"}},
    ]))
    assert m.reply_to == "777"
    assert m.quoted == "被引用的原话"


def test_reply_id_numeric_is_stringified():
    """message_id 是 number——统一转成字符串（与 Message.id 同形）。"""
    m = parse_event(private_event(message=[
        {"type": "reply", "data": {"id": 777}},
        {"type": "text", "data": {"text": "x"}},
    ]))
    assert m.reply_to == "777"


def test_reply_string_form_cq_code():
    """messageFormat=string：[CQ:reply,id=777] 也要认出来，且从正文剥掉。"""
    m = parse_event(private_event(message="[CQ:reply,id=777] 回你"))
    assert m.reply_to == "777"
    assert m.quoted == ""
    assert "[CQ:" not in m.content


def test_no_reply_means_none_and_empty():
    m = parse_event(private_event(message="普通消息"))
    assert m.reply_to is None
    assert m.quoted == ""


def test_malformed_reply_segment_ignored():
    """reply 段畸形（没有 id / 缺 data）→ 当作没有引用，不猜、不抛。"""
    m = parse_event(private_event(message=[
        {"type": "reply", "data": {}},
        {"type": "text", "data": {"text": "x"}},
    ]))
    assert m.reply_to is None


# ---------- extract_message_text（get_msg 响应复用）----------


def test_extract_message_text_array_joins_text_segments():
    text = extract_message_text([
        {"type": "text", "data": {"text": "第一段"}},
        {"type": "image", "data": {"url": "https://x/a.png"}},
        {"type": "text", "data": {"text": "第二段"}},
    ])
    assert text == "第一段第二段"


def test_extract_message_text_string_strips_cq():
    assert extract_message_text("[CQ:image,file=x.jpg]看图") == "看图"


def test_extract_message_text_malformed_gives_empty():
    assert extract_message_text(None) == ""
    assert extract_message_text(123) == ""


# ---------- has_image（"有没有图"这个事实，与"下不下载得到"无关）----------


def test_has_image_array_and_string_forms():
    assert has_image([{"type": "image", "data": {"url": "https://x/a.png"}}]) is True
    assert has_image([{"type": "text", "data": {"text": "看图"}}]) is False
    # 字符串形态：CQ 码里有没有 image
    assert has_image("[CQ:image,file=x.jpg]") is True
    assert has_image("[CQ:face,id=1]你好") is False


def test_has_image_true_even_without_downloadable_url():
    """只给本地文件名（没有 http URL，下载不了）也算"有图"——
    "这里有过一张图"这个事实不该因为下载不了就消失（渲染 [图片] 占位）。"""
    assert has_image([{"type": "image", "data": {"file": "abc.jpg"}}]) is True


def test_has_image_malformed_is_false():
    assert has_image(None) is False
    assert has_image(123) is False
    assert has_image([None, "x", {"no": "type"}]) is False


# ---------- 信封分流：事件 vs API 响应 vs 其他 ----------


def test_meta_event_is_not_a_message():
    """meta_event（心跳/生命周期）不该当消息处理。"""
    assert parse_event({"post_type": "meta_event", "meta_event_type": "heartbeat"}) is None


def test_notice_event_is_not_a_message():
    assert parse_event({"post_type": "notice", "notice_type": "group_recall"}) is None


def test_api_response_is_not_an_event():
    """带 echo 的是 API 响应，没有 post_type——不该当事件解析。"""
    assert parse_event({"status": "ok", "retcode": 0, "data": {}, "echo": "1"}) is None


def test_malformed_event_returns_none_not_crash():
    """坏数据只跳过，不许抛——外来报文不能拖垮适配器。"""
    assert parse_event({}) is None
    assert parse_event({"post_type": "message"}) is None  # 缺 message_type
    assert parse_event({"post_type": "message", "message_type": "weird"}) is None


# ---------- 会话 ID 反解 ----------


def test_parse_session_id_private():
    assert parse_session_id("qq:p20002000") == ("private", "20002000")


def test_parse_session_id_group():
    assert parse_session_id("qq:g30003000") == ("group", "30003000")


def test_parse_session_id_rejects_foreign():
    """不是 QQ 会话 ID 就返回 None，绝不猜（发错地方的代价很高）。"""
    assert parse_session_id("web:local") is None
    assert parse_session_id("qq:x123") is None
    assert parse_session_id("") is None


# ---------- 构造发送动作 ----------


def test_build_send_action_private():
    action, params = build_send_action("qq:p20002000", "你好")
    assert action == "send_private_msg"
    assert params["user_id"] == 20002000
    assert params["message"]


def test_build_send_action_group():
    action, params = build_send_action("qq:g30003000", "大家好啊")
    assert action == "send_group_msg"
    assert params["group_id"] == 30003000


def test_build_send_action_sends_array_not_cq_string():
    """发送一律用段数组，不用 CQ 字符串。

    理由（安全问题）：CQ 字符串里如果出现 [CQ:at,qq=all] 会被后端当指令执行——
    模型输出的文本是不可信的，绝不能让它有机会注入 CQ 码。
    """
    _action, params = build_send_action("qq:g1", "看这个 [CQ:at,qq=all] 哈哈")
    assert isinstance(params["message"], list)
    text = params["message"][0]["data"]["text"]
    assert "[CQ:at,qq=all]" in text  # 原样当文本，不被解释
    assert params["message"][0]["type"] == "text"


def test_build_send_action_rejects_foreign_session():
    with pytest.raises(ValueError, match="QQ 会话"):
        build_send_action("web:local", "x")


def test_build_send_action_numeric_ids_are_ints():
    """OneBot 的 user_id/group_id 是 number，不能发字符串。"""
    _a, p1 = build_send_action("qq:p123", "x")
    _a, p2 = build_send_action("qq:g456", "x")
    assert isinstance(p1["user_id"], int)
    assert isinstance(p2["group_id"], int)
