"""PR3 热重载：配置改完不重启即生效（对 §19-13「改动重启生效」的用户拍板偏离）。

四块覆盖：
- ChatLLM：注入 reader → model/密钥/窗口即生效；坏快照回退 last-good（整份，
  不半量应用）；裸实例（`__new__`，reader=None）永不重载
- Persona：同一实例改文件即变；读失败回退上次成功那份；没改 → 逐字节一致
- serve_forever：唤醒策略不缓存——改完唤醒词下一句生效
- 落库/密钥等"没改时"的行为一个字节不变（热重载不能破坏 §12 缓存命中）

全离线：假 client 脚本化模型输出（同 test_chat_loop），没有真网络。
"""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# 项目根也加进来：serve_forever 住在 main.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router
from core.event_bus import EventBus
from core.llm.chat_llm import ChatLLM
from core.session.store import SessionStore


# ---------- 假件 ----------


class FakeClient:
    """按脚本返回 assistant message，记请求；close() 记录是否被关。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.closed = False
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.requests.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=outer.script.pop(0))]
                )

        self.chat = SimpleNamespace(completions=_Completions())

    async def close(self):
        self.closed = True


def assistant(content=None, calls=None):
    return SimpleNamespace(content=content, tool_calls=calls)


def snap(base_url="https://old.example/v1", model="old-model", key="sk-old",
         keep=10, extra=5, steps=3):
    """一份配置整快照（形状同 ChatLLM._read_settings_snapshot 的返回）。"""
    return {
        "llm": {
            "base_url": base_url, "model": model,
            "history_keep_messages": keep,
            "history_lookback_extra": extra,
            "max_agent_steps": steps,
        },
        "api_key": key,
    }


@pytest.fixture(autouse=True)
def registry():
    from core.plugin.registry import PluginRegistry
    reg = PluginRegistry()
    reg.reset()
    reg.scan()
    yield reg
    reg.reset()


def make_bot(script=None, **overrides):
    """裸 ChatLLM（不跑 __init__）——settings_reader 默认就是类属性 None。"""
    from core.executor import ToolExecutor
    from core.llm.context import ContextAssembler
    from core.llm.persona_llm.base import Persona
    from core.plugin.guard import PermissionGuard
    from core.plugin.registry import PluginRegistry
    from core.xml_parser import XmlParser

    reg = PluginRegistry()
    bot = ChatLLM.__new__(ChatLLM)
    bot.base_url = "https://old.example/v1"
    bot.api_key = "sk-old"
    bot.model = "old-model"
    bot.persona = Persona()
    bot.context = ContextAssembler()
    bot.parser = XmlParser()
    bot.registry = reg
    bot.executor = ToolExecutor(reg, PermissionGuard(reg))
    bot.max_agent_steps = 3
    bot.history_keep_messages = 10
    bot.history_lookback_extra = 5
    bot.fallback_text = "（兜底）"
    bot.client = FakeClient(script if script is not None else [assistant("<msg>好</msg>")])
    for k, v in overrides.items():
        setattr(bot, k, v)
    return bot


# ---------- ChatLLM：注入 reader 即生效 ----------


async def test_model_change_applies_without_rebuilding_client():
    """改 model：下一轮即生效，client 不重建（网关/密钥没变）。"""
    bot = make_bot()
    old_client = bot.client
    bot.settings_reader = lambda: snap(model="new-model")
    await bot._refresh_config()
    assert bot.model == "new-model"
    assert bot.client is old_client
    assert old_client.closed is False


async def test_key_or_gateway_change_rebuilds_client_and_closes_old():
    """改 base_url/密钥：client 必须重建（否则还用旧凭据），旧的尽力关。"""
    bot = make_bot()
    old_client = bot.client
    bot.settings_reader = lambda: snap(base_url="https://new.example/v1", key="sk-new")
    await bot._refresh_config()
    assert (bot.base_url, bot.api_key) == ("https://new.example/v1", "sk-new")
    assert bot.client is not old_client
    assert old_client.closed is True
    await bot.client.close()  # 收尾（真 AsyncOpenAI，别留给 GC 报未关闭）


async def test_window_and_step_limits_apply():
    bot = make_bot()
    bot.settings_reader = lambda: snap(keep=4, extra=1, steps=2)
    await bot._refresh_config()
    assert (bot.history_keep_messages, bot.history_lookback_extra,
            bot.max_agent_steps) == (4, 1, 2)


def test_run_loop_uses_fresh_model_in_request():
    """接线的证明：run_loop 调用链里，改过的 model 真的进了请求参数。"""
    bot = make_bot(script=[assistant("<msg>好</msg>")])
    bot.settings_reader = lambda: snap(model="new-model")
    reply = bot.chat("", "你好")
    assert reply.messages == ["好"]
    assert bot.client.requests[0]["model"] == "new-model"


# ---------- ChatLLM：失败回退 last-good ----------


async def test_reader_error_keeps_last_good():
    """读配置抛异常（坏 JSON/文件写坏）：不改任何字段，也不抛。"""
    bot = make_bot()
    old_client = bot.client

    def boom():
        raise RuntimeError("坏 JSON")

    bot.settings_reader = boom
    await bot._refresh_config()  # 不抛即通过
    assert (bot.base_url, bot.api_key, bot.model) == \
        ("https://old.example/v1", "sk-old", "old-model")
    assert bot.client is old_client


async def test_empty_base_url_keeps_whole_snapshot():
    """空 base_url = 读到坏配置：**整快照**不应用——model 也不许变。

    半量应用（换了 model 却没换网关）比全旧更糟，所以校验失败 = 整份丢弃。
    """
    bot = make_bot()
    bot.settings_reader = lambda: snap(base_url="", model="new-model", keep=1)
    await bot._refresh_config()
    assert bot.base_url == "https://old.example/v1"
    assert bot.model == "old-model"
    assert bot.history_keep_messages == 10


async def test_bare_instance_without_reader_never_refreshes():
    """裸实例（无 __init__、无 reader）→ 热重载路径直接跳过，不炸。"""
    bot = ChatLLM.__new__(ChatLLM)
    assert bot.settings_reader is None  # 类属性兜底
    await bot._refresh_config()  # 不抛即通过


def test_bare_instance_run_loop_unaffected():
    """test_chat_loop 那套裸实例走 run_loop 的路径不受热重载影响（全绿回归）。"""
    bot = make_bot(script=[assistant("<msg>hi</msg>")])
    assert bot.settings_reader is None
    assert bot.chat("", "hi").messages == ["hi"]


# ---------- Persona：每回合现拼 ----------


def test_persona_same_instance_rebuilds_when_file_changes(tmp_path):
    from core.llm.persona_llm.base import Persona

    pfile = tmp_path / "persona.md"
    pfile.write_text("## 我是谁\n我是甲号人格", encoding="utf-8")
    p = Persona(persona_path=pfile)
    first = p.build_system_prompt()
    assert "甲号人格" in first
    # 没改文件 → 逐字节一致（§12：内容不变，缓存照样命中）
    assert p.build_system_prompt() == first
    # 改了 → 同一实例下一句即生效
    pfile.write_text("## 我是谁\n我是乙号人格", encoding="utf-8")
    second = p.build_system_prompt()
    assert "乙号人格" in second
    assert "甲号人格" not in second


def test_persona_bad_bytes_falls_back_to_last_good(tmp_path):
    """用户文件写坏（非法 UTF-8）→ 沿用上次成功那份，不炸回合。"""
    from core.llm.persona_llm.base import Persona

    pfile = tmp_path / "persona.md"
    pfile.write_text("## 我是谁\n我是丙号人格", encoding="utf-8")
    p = Persona(persona_path=pfile)
    good = p.build_system_prompt()
    pfile.write_bytes(b"\xff\xfe\x00bad")  # 不是合法 UTF-8
    assert p.build_system_prompt() == good


def test_persona_missing_base_file_falls_back(tmp_path, monkeypatch):
    """框架文件被删（OSError）→ 同样回退，不抛。"""
    from core.llm.persona_llm.base import Persona

    p = Persona(persona_path=tmp_path / "p.md")
    good = p.build_system_prompt()
    monkeypatch.setattr(p, "base_path", tmp_path / "nope.md")
    assert p.build_system_prompt() == good


# ---------- serve_forever：唤醒策略不缓存 ----------


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
    async def run_loop(self, user_question, history=None, session_id="",
                       *, session_type="", owner="", images=None, quoted=""):
        return Reply(session_id=session_id, messages=["好呀~"])


def _group_msg(mid, content):
    return Message(id=mid, platform="fake", session_id="qq:g1", owner="u1",
                   direction="in", role="user", content=content,
                   session_type="group")


async def test_serve_forever_reads_wake_policy_per_message(tmp_path, monkeypatch):
    """改完唤醒词不用重启：serve_forever 不再缓存策略，下一句就按新词判。

    旧实现启动时读一次、整个进程用那份（§19-13）。这里第一句被"还没配词"
    的策略挡下，改成有词后第二句立即通过——不重启、不重建服务。
    """
    import main
    from core.wake import WakePolicy

    store = SessionStore(tmp_path / "s.db").open()
    store.ensure_session("qq:g1", platform="fake", kind="group", owner="u1")
    bus = EventBus()
    bus.reset()
    adapter = FakeAdapter(bus=bus)
    reg = AdapterRegistry()
    reg.register(adapter)

    box = {"policy": WakePolicy(words=(), scope="group")}  # 还没配唤醒词
    monkeypatch.setattr(main, "load_wake_policy", lambda: box["policy"])

    task = asyncio.create_task(main.serve_forever(
        [adapter], router=Router(reg), store=store, bot=FakeBot(), bus=bus,
    ))  # wake=None：服务默认路径（现读）
    adapter._deliver(_group_msg("m1", "塔利在吗"))
    await asyncio.sleep(0.05)
    assert adapter.sent == []  # 没词 → 未唤醒，不回（但已存库）

    box["policy"] = WakePolicy(words=("塔利",), scope="group")  # 设置页刚改完
    adapter._deliver(_group_msg("m2", "塔利在吗"))
    await asyncio.sleep(0.05)
    assert len(adapter.sent) == 1  # 新词立即生效

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()
    bus.reset()
