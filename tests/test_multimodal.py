"""UX-06：多模态后端骨架（图片存储 + 装配 + 落库）。

- image_store：落盘/取用/回收（100MB 删最早）
- store：attachments 列存在、history() 契约不变、messages() 能看到附件
- ChatLLM：带图 → content 数组；无图 → 纯字符串（文本路径不变）
- handle_message：纯图消息不被空白守卫拦掉；附件落库
"""

import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import image_store
from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router
from core.bus.event_bus import EventBus
from core.session.store import SessionStore

# 最小合法 PNG（1x1 透明）——真实字节头，供 sniff 认出来
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ================= image_store =================


def test_sniff_mime_recognizes_png():
    assert image_store.sniff_mime(PNG_1PX) == "image/png"


def test_sniff_mime_rejects_non_image():
    assert image_store.sniff_mime(b"not an image") is None


def test_save_and_to_data_uri_roundtrip(tmp_path):
    name = image_store.save_bytes(PNG_1PX, base=tmp_path)
    assert name.endswith(".png")
    uri = image_store.to_data_uri(name, base=tmp_path)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == PNG_1PX


def test_save_is_content_addressed_dedup(tmp_path):
    """同样内容只占一份（sha1 命名）。"""
    a = image_store.save_bytes(PNG_1PX, base=tmp_path)
    b = image_store.save_bytes(PNG_1PX, base=tmp_path)
    assert a == b
    assert len(list(tmp_path.iterdir())) == 1


def test_to_data_uri_missing_file_returns_none(tmp_path):
    """图可能已被回收——取不到要优雅返回 None，不抛。"""
    assert image_store.to_data_uri("nope.png", base=tmp_path) is None


def test_save_rejects_non_image(tmp_path):
    with pytest.raises(ValueError):
        image_store.save_bytes(b"plain text", base=tmp_path)


def test_cleanup_deletes_oldest_when_over_limit(tmp_path, monkeypatch):
    """超上限就删最早的（按 mtime），删到阈值以下。"""
    monkeypatch.setattr(image_store, "MAX_DIR_BYTES", 1000)
    # 造三个各 400 字节的"图"（内容不同，避免去重）
    for i, payload in enumerate((b"A" * 390, b"B" * 390, b"C" * 390)):
        data = image_store._MAGIC[0][0] + payload
        name = image_store.save_bytes(data, base=tmp_path)
        # 手动把 mtime 拉开，让"最早"明确
        p = tmp_path / name
        os.utime(p, (1000 + i, 1000 + i))
    # 三次写完后总大小 1170 > 1000，应删到 ≤1000
    assert image_store.total_bytes(tmp_path) <= 1000


def test_cleanup_noop_when_under_limit(tmp_path):
    assert image_store.cleanup(tmp_path) == 0


# ================= store：attachments =================


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "s.db").open()
    s.ensure_session("s1")
    yield s
    s.close()


def test_attachments_column_exists(store):
    store.append("s1", "user", "看图", attachments=["a.png", "b.jpg"])
    rows = store.messages("s1")
    import json
    assert json.loads(rows[0]["attachments"]) == ["a.png", "b.jpg"]


def test_history_shape_unchanged_by_attachments(store):
    """history() 契约不变：仍是 [{role, content}]，不带 attachments。"""
    store.append("s1", "user", "看图", attachments=["a.png"])
    assert store.history("s1") == [{"role": "user", "content": "看图"}]


def test_append_without_attachments_stores_null(store):
    store.append("s1", "user", "纯文本")
    assert store.messages("s1")[0]["attachments"] is None


def test_old_db_gets_attachments_column(tmp_path):
    """旧库（无 attachments 列）打开时自动补列。"""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, platform TEXT, kind TEXT,
            owner TEXT, title TEXT, created_at REAL, last_active REAL);
        CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, mentions TEXT,
            reply_to TEXT, tool_json TEXT, ts REAL);
        CREATE TABLE session_cursors(session_id TEXT PRIMARY KEY, event_seq INTEGER DEFAULT 0);
    """)
    conn.commit()
    conn.close()

    s = SessionStore(db).open()
    s.ensure_session("s1")
    s.append("s1", "user", "看图", attachments=["x.png"])  # 能写附件即证明补列成功
    assert s.messages("s1")[0]["attachments"] is not None
    s.close()


# ================= ChatLLM：多模态装配 =================


@pytest.fixture
def bot(monkeypatch, tmp_path):
    """造一个 ChatLLM，配置指到 tmp，图片目录也指到 tmp。"""
    from core.config import loader
    # config 域要能构造出 ChatLLM（需要 base_url/model）
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.json").write_text(
        '{"llm": {"base_url": "https://x/v1", "model": "m"}}', encoding="utf-8")
    # openai 客户端构造时要求有 key（这里不会真发请求，给个假的）
    (cfgdir / "secrets.json").write_text(
        '{"llm": {"api_key": "sk-test"}}', encoding="utf-8")
    monkeypatch.setattr(loader, "DEFAULT_DIR", cfgdir)
    monkeypatch.setattr(image_store, "DEFAULT_DIR", tmp_path / "img")

    from core.llm.chat_llm import ChatLLM
    return ChatLLM()


def test_no_images_keeps_plain_string_content(bot):
    """无图 → 最新 user 的 content 是**纯字符串**（文本路径一个字节不变）。"""
    msgs = bot.assemble_messages("你好")
    assert isinstance(msgs[-1]["content"], str)
    assert "你好" in msgs[-1]["content"]


def test_images_make_content_array(bot, tmp_path):
    name = image_store.save_bytes(PNG_1PX)
    msgs = bot.assemble_messages("这是什么", images=[name])
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "这是什么" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_missing_image_degrades_to_text(bot):
    """图读不到（已回收）→ 退回纯文本，不让整条消息发不出去。"""
    msgs = bot.assemble_messages("看图", images=["gone.png"])
    assert isinstance(msgs[-1]["content"], str)  # 没有可用图 → 纯字符串


# ================= handle_message：纯图 + 附件落库 =================


class FakeAdapter(AdapterBase):
    name = "fake"

    def __init__(self, bus=None):
        super().__init__(bus=bus)
        self.sent: list[Reply] = []

    def normalize(self, raw):
        raise NotImplementedError

    async def send(self, reply):
        self.sent.append(reply)

    async def start(self):
        pass


class FakeBot:
    def __init__(self):
        self.calls = []

    async def run_loop(self, user_question, history=None, session_id="",
                       *, session_type="", owner="", images=None):
        self.calls.append((user_question, session_id, list(images or [])))
        return Reply(session_id=session_id, messages=["看到了"])


@pytest.fixture
def wiring(tmp_path):
    import main
    bus = EventBus()
    s = SessionStore(tmp_path / "s.db").open()
    s.ensure_session("s1")
    adapter = FakeAdapter(bus=bus)
    reg = AdapterRegistry()
    reg.register(adapter)
    return main, adapter, Router(reg), FakeBot(), s, bus


def make_msg(content="", images=None):
    return Message(id="m", platform="fake", session_id="s1", owner="local",
                   direction="in", role="user", content=content, images=images or [])


async def test_pure_image_message_is_not_dropped(wiring):
    """纯图片（无文字）是合法输入——不能被空白守卫拦掉。"""
    main, adapter, router, bot, store, bus = wiring
    reply = await main.handle_message(
        make_msg("", images=["a.png"]), router=router, store=store, bot=bot, bus=bus)
    assert reply is not None
    assert bot.calls and bot.calls[0][2] == ["a.png"]


async def test_blank_message_still_dropped(wiring):
    """真正的空白（无文字也无图）仍然丢弃。"""
    main, adapter, router, bot, store, bus = wiring
    reply = await main.handle_message(
        make_msg("   "), router=router, store=store, bot=bot, bus=bus)
    assert reply is None
    assert bot.calls == []


async def test_images_persisted_and_passed_to_model(wiring):
    main, adapter, router, bot, store, bus = wiring
    await main.handle_message(
        make_msg("看看这张", images=["a.png"]), router=router, store=store,
        bot=bot, bus=bus)
    import json
    rows = store.messages("s1")
    assert json.loads(rows[0]["attachments"]) == ["a.png"]  # 落库
    assert bot.calls[0][2] == ["a.png"]                      # 传给模型
    # 历史契约不变（不含 attachments）
    assert store.history("s1")[0] == {"role": "user", "content": "看看这张"}


# ================= 兔老师审查抓到的缺口（2026-10-03）=================


async def test_unwoken_group_image_message_persists_attachments(wiring, monkeypatch):
    """群聊未唤醒的**带图**消息，图也要落库（不能只落文字丢图）。

    兔老师抓到：未唤醒分支的 store.append 漏了 attachments。
    """
    main, adapter, router, bot, store, bus = wiring
    from core.wake import WakePolicy
    policy = WakePolicy(words=("塔利",), scope="group")

    msg = Message(id="m", platform="fake", session_id="g1", owner="u1",
                  direction="in", role="user", content="随便说说",
                  session_type="group", images=["pic.png"])
    reply = await main.handle_message(
        msg, router=router, store=store, bot=bot, bus=bus, wake=policy)
    assert reply is None                        # 未唤醒：不回
    assert bot.calls == []                      # 不调模型
    import json
    rows = store.messages("g1")
    assert rows and rows[0]["role"] == "user"
    assert json.loads(rows[0]["attachments"]) == ["pic.png"]   # 图没丢
