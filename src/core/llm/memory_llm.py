"""MemoryLLM：记忆提炼（M1-04）。

与 ChatLLM 的分工：
- ChatLLM 生成对话消息，走 XML + FC 循环；
- MemoryLLM 只做「语言压缩」：输入一批事件原文 + 已有记忆，输出**结构化 JSON**
  （curated 条目数组）。它不是对话，所以**不做 FC 循环**，也不解析 <msg>。

两者只共用三件事：读 config/secrets → 构造 AsyncOpenAI → 经 ContextAssembler
装配感知块（§18.5 硬规则 6）。不为此抽基类（YAGNI）。

extract 的失败模式（§十一 MemFail）：
- 保留原文引用：每条产物必须带 refs，且 event_seq 必须落在本批事件里；
  校验不过的条目直接丢弃——编造来源比不记更糟。
- 原子追加：写盘全走 CuratedStore.append（JSONL 单写锁 + O_APPEND）。
- 模型输出坏了（非 JSON / 结构不对）→ 记 warning 返回空，不炸后台 worker。
"""

import json
import logging
import re
from pathlib import Path

from openai import AsyncOpenAI

from core.config.loader import Config
from core.llm.context import ContextAssembler, SessionContext
from core.memory.store import CuratedStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: 默认带给模型的已有记忆条数上限。不做相关性检索（v1 无向量，§十九-7），
#: 只取最近的这些条——足够防重复，又不会把 prompt 撑爆。
MAX_EXISTING = 30

#: tags 封闭词表（2026-10-18 定，见 docs/temp/评估-无向量检索与关系网.md）。
#: 来源是用户的标签关系网笔记：性格/相处标签 + 「三缘」关系类型。
#: 归一发生在**提炼端**（"阳光开朗"→`开朗`），检索端只做精确匹配；
#: 词表外的词直接不收——宁可少收一个标签，不收无法归一的噪音。
TAG_VOCAB = frozenset(
    {
        "开朗", "内向", "幽默", "有耐心", "细心", "认真",
        "热情", "冷静", "急躁", "敏感", "固执", "可靠",
        "亲缘", "业缘", "地缘",
    }
)

#: 单条记忆最多带几个标签。标签是粗筛用的粗粒度索引，不是画像本身。
MAX_TAGS = 5

#: 模型偶尔会无视"不要代码块"的叮嘱。剥掉包裹的 ``` / ```json。
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _parse_entries(text: str) -> list[dict]:
    """把模型输出解析成条目列表；坏输出返回 []（绝不抛）。"""
    if not text or not text.strip():
        return []

    cleaned = _FENCE.sub("", text.strip()).strip()
    candidates = [cleaned]

    # 模型可能在 JSON 前后写了解释文字：退一步取最外层 [...] 再试。
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        # 宽容一点：也接受 {"entries": [...]} / {"memories": [...]}
        if isinstance(data, dict):
            data = data.get("entries") or data.get("memories") or []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _valid_tags(raw_tags) -> list[str]:
    """把模型给的标签收敛到封闭词表：词表外丢弃、去重、截断到上限。"""
    tags: list[str] = []
    for tag in raw_tags or []:
        text = str(tag).strip()
        if text in TAG_VOCAB and text not in tags:
            tags.append(text)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _clamp_importance(value) -> int:
    """importance 收敛到 1–5 的整数；坏值按 3（一般事实）处理。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, number))


def _valid_refs(raw_refs, allowed_seqs: set[int]) -> list[dict]:
    """只保留能指回本批事件的引用；quote 原样截断到 200 字。"""
    refs: list[dict] = []
    for ref in raw_refs or []:
        if not isinstance(ref, dict):
            continue
        seq = ref.get("event_seq")
        if seq not in allowed_seqs:
            continue
        refs.append({"event_seq": seq, "quote": str(ref.get("quote") or "")[:200]})
    return refs


def _render_blocks(blocks) -> str:
    """把装配好的感知块渲染成 MemoryLLM 的 user 消息正文。

    标题只负责分区；源、顺序、配额都来自 ContextSpec（硬规则 6），
    这里不决定"看什么"。
    """
    labels = {"conversation_segment": "对话片段", "existing_curated": "已有记忆"}
    parts = []
    for block in blocks:
        if not block.content.strip():
            continue
        label = labels.get(block.name, block.name)
        parts.append(f"## 【{label}】\n{block.content}")
    return "\n\n".join(parts)


class MemoryLLM:
    """记忆提炼器：events → curated.jsonl。"""

    def __init__(self, store: CuratedStore) -> None:
        cfg = Config.load("config")
        secrets = Config.load("secrets")
        llm = cfg.get("llm", {})
        self.base_url: str = llm.get("base_url", "")
        self.model: str = llm.get("model", "")
        if not self.base_url or not self.model:
            raise ValueError(
                "llm.base_url / llm.model 未配置。请在 data/config/config.json 里填写"
                " 服务商网关地址（以 /v1 结尾）与模型名。"
            )
        self.api_key: str = secrets.get("llm", {}).get("api_key", "")
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        self.store = store
        self.context = ContextAssembler()
        self.system_prompt = (_PROMPTS_DIR / "memory.md").read_text(encoding="utf-8")

    async def extract(
        self,
        events: list[dict],
        existing: list[dict] | None = None,
        session: SessionContext | None = None,
    ) -> list[dict]:
        """从一批事件里提炼记忆，写盘并返回新写入的条目。

        Args:
            events: EventLog.read_batch() 的原始事件（按 seq 升序）。
            existing: 已有记忆；None = 按事件 owner 从 store 取最近 MAX_EXISTING 条。
            session: 装配用会话信息（owner/session_id 也会从事件兜底）。

        Returns:
            本次写入的条目列表（含 id / content / importance / refs）。
        """
        events = [
            event
            for event in events
            if event.get("type") in ("message.received", "message.sent")
        ]
        if not events:
            return []

        # 归属以会话为准：群聊里每条事件的 owner 是**发言者**，若拿第一条事件的
        # owner 当记忆主人，会把群里所有人的话记到第一个说话的人头上。
        # session.owner（会话主人）优先，没给才退回事件上的 owner。
        event_owner = str(events[0].get("owner") or "")
        event_session_id = str(events[0].get("session_id") or "")
        if session is None:
            session = SessionContext(session_id=event_session_id, owner=event_owner)
        owner = session.owner or event_owner
        session_id = session.session_id or event_session_id

        if existing is None:
            existing = self.store.read_all(owner=owner or None, status="active")
            existing = existing[-MAX_EXISTING:]
        existing_ids = {entry.get("id") for entry in existing if entry.get("id")}

        blocks = self.context.assemble(
            "memory", session, data={"events": events, "existing": existing}
        )
        user_content = _render_blocks(blocks)
        if not user_content.strip():
            return []

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
        text = response.choices[0].message.content or ""
        items = _parse_entries(text)
        if not items:
            logger.warning("MemoryLLM 输出无法解析为条目，本批跳过（seq 起点=%s）", events[0].get("seq"))
            return []

        allowed_seqs = {event.get("seq") for event in events}
        written: list[dict] = []
        for item in items:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            refs = _valid_refs(item.get("refs"), allowed_seqs)
            if not refs:
                # 没有可核对的原文来源 → 丢弃（防过度压缩/防编造）
                logger.warning("丢弃无有效 refs 的记忆条目：%s", content[:60])
                continue

            importance = _clamp_importance(item.get("importance"))
            tags = _valid_tags(item.get("tags"))
            entry_id = self.store.append(
                owner=owner,
                session_id=session_id,
                content=content,
                importance=importance,
                refs=refs,
                tags=tags,
            )
            written.append(
                {
                    "id": entry_id,
                    "content": content,
                    "importance": importance,
                    "refs": refs,
                    "tags": tags,
                }
            )

            # 支持更新：模型指认的旧条目作废（追加墓碑，不 rewrite 文件）。
            # 只认本次真的交给它看过的 id，防止幻觉 id 在库里留下垃圾墓碑。
            for old_id in item.get("supersedes") or []:
                if old_id in existing_ids:
                    self.store.append_tombstone(old_id, reason="superseded")

        return written
