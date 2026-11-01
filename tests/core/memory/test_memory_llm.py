"""测试 MemoryLLM extract（M1-04）。

验收标准（docs/M1推进计划.md）：
  - 假 client 脚本化返回固定 JSON → 写出的 curated 条目带 importance 和 refs
  - refs 能指回 events 的 seq（指不回去的条目必须被丢弃）
  - 已有相关 curated 必须进入 prompt（防重复 / 防冲突 / 支持更新）
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

# 与 test_chat_loop.py 一致：把 src 放进 path，按运行时的方式导入（core.*）。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.llm.memory_llm import MemoryLLM
from core.memory.store import CuratedStore


class FakeClient:
    """按脚本返回一段固定文本，并记下每次请求。"""

    def __init__(self, text: str):
        self.text = text
        self.requests: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.requests.append(kwargs)
                message = SimpleNamespace(content=outer.text)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        self.chat = SimpleNamespace(completions=_Completions())


def make_llm(tmp_path, text: str) -> MemoryLLM:
    """造一个不跑 __init__ 的裸实例（与 ChatLLM 测试同一套做法）。"""
    llm = MemoryLLM.__new__(MemoryLLM)
    llm.model = "fake-model"
    llm.client = FakeClient(text)
    llm.store = CuratedStore(tmp_path / "curated.jsonl")
    from core.llm.context import ContextAssembler

    llm.context = ContextAssembler()
    llm.system_prompt = "记忆提炼"
    return llm


def received(seq: int, content: str, owner: str = "u1", session_id: str = "s1") -> dict:
    return {
        "seq": seq,
        "type": "message.received",
        "ts": 1000.0,
        "session_id": session_id,
        "owner": owner,
        "data": {"content": content, "role": "user"},
    }


def sent(seq: int, messages: list[str], owner: str = "u1", session_id: str = "s1") -> dict:
    return {
        "seq": seq,
        "type": "message.sent",
        "ts": 1000.0,
        "session_id": session_id,
        "owner": owner,
        "data": {"messages": messages, "stop_reason": "ok"},
    }


class TestExtract:
    async def test_writes_entries_with_importance_and_refs(self, tmp_path):
        text = json.dumps(
            [
                {
                    "content": "用户下周三要去北京出差。",
                    "importance": 4,
                    "refs": [{"event_seq": 1, "quote": "下周三去北京出差"}],
                }
            ],
            ensure_ascii=False,
        )
        llm = make_llm(tmp_path, text)
        written = await llm.extract([received(1, "我下周三去北京出差"), sent(2, ["一路顺风"])])

        assert len(written) == 1
        assert written[0]["importance"] == 4

        entries = llm.store.read_all(owner="u1", status="active")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["content"] == "用户下周三要去北京出差。"
        assert entry["importance"] == 4
        assert entry["refs"] == [{"event_seq": 1, "quote": "下周三去北京出差"}]
        assert entry["session_id"] == "s1"

    async def test_refs_must_point_back_to_batch_events(self, tmp_path):
        """模型编了一个不存在的 seq → 该条丢弃，不写盘。"""
        text = json.dumps(
            [
                {"content": "用户养了一只猫。", "importance": 3,
                 "refs": [{"event_seq": 999, "quote": "养了猫"}]},
                {"content": "用户下周三去北京。", "importance": 4,
                 "refs": [{"event_seq": 1, "quote": "去北京"}]},
            ],
            ensure_ascii=False,
        )
        llm = make_llm(tmp_path, text)
        written = await llm.extract([received(1, "我下周三去北京")])

        assert [item["content"] for item in written] == ["用户下周三去北京。"]
        assert len(llm.store.read_all(status="active")) == 1

    async def test_parses_fenced_json(self, tmp_path):
        body = json.dumps(
            [{"content": "用户喜欢喝乌龙茶。", "importance": 3,
              "refs": [{"event_seq": 1, "quote": "乌龙茶"}]}],
            ensure_ascii=False,
        )
        llm = make_llm(tmp_path, f"```json\n{body}\n```")
        written = await llm.extract([received(1, "我只喝乌龙茶")])
        assert len(written) == 1

    async def test_existing_memory_is_injected(self, tmp_path):
        """§十二：extract 必须看见已有相关记忆（防重复 / 防冲突 / 支持更新）。"""
        llm = make_llm(tmp_path, "[]")
        old_id = llm.store.append(
            owner="u1", session_id="s1", content="用户住在上海。",
            importance=4, refs=[{"event_seq": 1, "quote": "我在上海"}],
        )

        await llm.extract([received(2, "我下个月搬到杭州")])

        assert len(llm.client.requests) == 1
        user_content = llm.client.requests[0]["messages"][1]["content"]
        assert "已有记忆" in user_content
        assert "用户住在上海。" in user_content
        assert old_id in user_content  # id 要露出来，supersedes 才指得回去

    async def test_supersedes_appends_tombstone(self, tmp_path):
        llm = make_llm(
            tmp_path,
            json.dumps(
                [{
                    "content": "用户搬到杭州了。",
                    "importance": 4,
                    "refs": [{"event_seq": 2, "quote": "搬到杭州"}],
                    "supersedes": ["old-id"],
                }],
                ensure_ascii=False,
            ),
        )
        old_id = llm.store.append(
            owner="u1", session_id="s1", content="用户住在上海。",
            importance=4, refs=[{"event_seq": 1, "quote": "在上海"}],
        )
        # 模型看到的是真实 id，这里把脚本里的占位换成它
        llm.client.text = llm.client.text.replace("old-id", old_id)

        await llm.extract([received(2, "我搬到杭州了")])

        active = llm.store.read_all(status="active")
        forgotten = llm.store.read_all(status="forgotten")
        assert [entry["content"] for entry in active] == ["用户搬到杭州了。"]
        assert [entry["id"] for entry in forgotten] == [old_id]

    async def test_unknown_supersedes_id_is_ignored(self, tmp_path):
        llm = make_llm(
            tmp_path,
            json.dumps(
                [{"content": "用户搬到杭州了。", "importance": 4,
                  "refs": [{"event_seq": 1, "quote": "搬到杭州"}],
                  "supersedes": ["hallucinated-id"]}],
                ensure_ascii=False,
            ),
        )
        await llm.extract([received(1, "我搬到杭州了")])
        assert llm.store.read_all(status="forgotten") == []

    async def test_importance_is_clamped(self, tmp_path):
        llm = make_llm(
            tmp_path,
            json.dumps(
                [
                    {"content": "用户要角色记住他的生日。", "importance": 99,
                     "refs": [{"event_seq": 1, "quote": "记住我生日"}]},
                    {"content": "用户今天心情一般。", "importance": "x",
                     "refs": [{"event_seq": 1, "quote": "心情一般"}]},
                ],
                ensure_ascii=False,
            ),
        )
        written = await llm.extract([received(1, "记住我生日，今天心情一般")])
        assert [item["importance"] for item in written] == [5, 3]

    async def test_bad_output_returns_empty_without_raising(self, tmp_path):
        llm = make_llm(tmp_path, "抱歉，我无法提炼。")
        assert await llm.extract([received(1, "随便聊聊")]) == []
        assert llm.store.read_all() == []

    async def test_empty_batch_skips_request(self, tmp_path):
        llm = make_llm(tmp_path, "[]")
        assert await llm.extract([]) == []
        assert llm.client.requests == []

    async def test_non_message_events_are_skipped(self, tmp_path):
        llm = make_llm(tmp_path, "[]")
        closed = {
            "seq": 1, "type": "session.closed", "ts": 1.0,
            "session_id": "s1", "owner": "u1", "data": {},
        }
        assert await llm.extract([closed]) == []
        assert llm.client.requests == []

    async def test_tags_normalized_to_closed_vocab(self, tmp_path):
        """词表外丢弃、去重、截到 5 个（归一在提炼端，2026-10-18 定）。"""
        llm = make_llm(
            tmp_path,
            json.dumps(
                [{
                    "content": "用户是学生，性格阳光开朗。",
                    "importance": 4,
                    "tags": ["阳光开朗", "幽默", "蘑菇", "业缘", "业缘",
                             "有耐心", "细心", "认真"],
                    "refs": [{"event_seq": 1, "quote": "阳光开朗"}],
                }],
                ensure_ascii=False,
            ),
        )
        written = await llm.extract([received(1, "我是学生，挺阳光开朗的")])
        # "阳光开朗"不在词表 → 丢；"蘑菇"不在词表 → 丢；重复"业缘"只收一次
        assert written[0]["tags"] == ["幽默", "业缘", "有耐心", "细心", "认真"]
        stored = llm.store.read_all(status="active")[0]
        assert stored["tags"] == written[0]["tags"]

    async def test_tags_default_to_empty(self, tmp_path):
        llm = make_llm(
            tmp_path,
            json.dumps(
                [{"content": "用户下周三去北京。", "importance": 4,
                  "refs": [{"event_seq": 1, "quote": "去北京"}]}],
                ensure_ascii=False,
            ),
        )
        written = await llm.extract([received(1, "下周三去北京")])
        assert written[0]["tags"] == []
