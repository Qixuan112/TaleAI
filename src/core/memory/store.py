"""记忆存储地基：EventLog（原始事件）+ CuratedStore（提炼记忆）。

契约（§18.3）：
  EventEntry  : {seq, type, ts, session_id, owner, data}
  CuratedEntry: {id, owner, session_id, content, importance(1-5),
                 refs:[{event_seq, quote}], created_at, last_access, status}

守住的约束：
  - seq 全局递增（不能两条并发拿到同一个 seq）
  - 只追加（JSONL，O_APPEND + 单写锁）
  - 删除 = 追加墓碑，绝不 rewrite 文件
  - schema 字段只增不改
  - 时钟可注入（测试用）

M1-01 产出。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4


class EventLog:
    """原始事件追加日志（events.jsonl）。

    全局 seq 递增、只追加、支持按游标读批。
    用于记录对话原文（message.received/sent、session.closed），
    extract 从这里提炼记忆。
    """

    def __init__(self, filepath: Path, clock_fn=None):
        """
        Args:
            filepath: events.jsonl 路径
            clock_fn: 可注入时钟函数（测试用），默认 time.time()
        """
        self.filepath = filepath
        self.clock_fn = clock_fn or time.time
        self._lock = threading.Lock()  # 单写锁：保证 seq 不重复
        self._seq_counter = 0  # 当前最大 seq（启动时从文件扫描）

        # 启动时扫描文件，恢复 seq 计数器
        self._recover_seq()

    def _recover_seq(self):
        """从文件扫描恢复 seq 计数器（幂等）。"""
        if not self.filepath.exists():
            return

        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    seq = entry.get("seq", 0)
                    if seq > self._seq_counter:
                        self._seq_counter = seq
                except json.JSONDecodeError:
                    # 脏行跳过（崩溃时可能写一半）
                    continue

    def append(
        self,
        type: str,
        session_id: str,
        owner: str,
        data: Dict[str, Any],
        ts: Optional[float] = None,
    ) -> int:
        """追加一条事件，返回分配的 seq。

        Args:
            type: 事件类型（如 "message.received"）
            session_id: 会话 ID
            owner: 所有者（QQ号/用户标识）
            data: 事件数据（如 {content, role}）
            ts: 时间戳（可注入，默认当前时间）

        Returns:
            分配的全局 seq
        """
        ts = ts if ts is not None else self.clock_fn()

        with self._lock:
            # 读改写：递增 seq
            self._seq_counter += 1
            seq = self._seq_counter

            entry = {
                "seq": seq,
                "type": type,
                "ts": ts,
                "session_id": session_id,
                "owner": owner,
                "data": data,
            }

            # O_APPEND 模式写入（原子追加）
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return seq

    def read_batch(
        self, after_seq: int = 0, limit: int = 100
    ) -> Iterator[Dict[str, Any]]:
        """按游标读批次（幂等）。

        Args:
            after_seq: 游标（读取 seq > after_seq 的事件）
            limit: 最多读几条

        Yields:
            EventEntry 字典
        """
        if not self.filepath.exists():
            return

        count = 0
        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                    if entry.get("seq", 0) > after_seq:
                        yield entry
                        count += 1
                        if count >= limit:
                            break
                except json.JSONDecodeError:
                    continue

    def read_session_batch(
        self, session_id: str, after_seq: int = 0, limit: int = 200
    ) -> Iterator[Dict[str, Any]]:
        """按会话 + 游标读批次（M1-05 extract 用）。

        与 read_batch 的区别：在扫描时就按 session_id 过滤。extract 的游标是
        **每会话一份**，若先按全局游标取再过滤，别的会话事件会把批次额度吃光，
        当前会话反而取不到东西。
        """
        if not self.filepath.exists():
            return

        count = 0
        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                    if (
                        entry.get("seq", 0) > after_seq
                        and entry.get("session_id") == session_id
                    ):
                        yield entry
                        count += 1
                        if count >= limit:
                            break
                except json.JSONDecodeError:
                    continue

    def get_max_seq(self) -> int:
        """返回当前最大 seq（用于游标初始化）。"""
        with self._lock:
            return self._seq_counter

    def size_bytes(self) -> int:
        """events.jsonl 当前字节数（未读游标基线用）。"""
        try:
            return self.filepath.stat().st_size
        except OSError:
            return 0

    def read_from_offset(
        self, offset: int = 0, limit: int = 100
    ) -> tuple[List[Dict[str, Any]], int]:
        """从字节偏移增量读事件（M1-08 未读增量用）。

        为什么是字节偏移而不是 seq：未读注入在**每轮对话**的热路径上，
        按 seq 从头扫 events.jsonl 会随文件增长越来越慢。events 只追加，
        偏移是稳定游标；偏移超过文件大小（文件被换过/截断）时退回 0。

        Returns:
            (entries, 下次该用的偏移)。脏行跳过但偏移照常前进——坏一行
            不能让整个未读通道卡死。
        """
        if not self.filepath.exists():
            return [], max(0, int(offset))

        try:
            size = self.filepath.stat().st_size
        except OSError:
            return [], max(0, int(offset))
        if offset < 0 or offset > size:
            offset = 0

        entries: List[Dict[str, Any]] = []
        with open(self.filepath, "rb") as f:
            f.seek(offset)
            for _ in range(max(0, limit)):
                line = f.readline()
                if not line:
                    break
                try:
                    entries.append(json.loads(line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            new_offset = f.tell()
        return entries, new_offset


class CuratedStore:
    """提炼记忆存储（curated.jsonl）。

    存储 MemoryLLM extract 的产物：带 importance、原文引用、status。
    支持追加、读取、更新 status（追加墓碑）。
    """

    def __init__(self, filepath: Path, clock_fn=None):
        """
        Args:
            filepath: curated.jsonl 路径
            clock_fn: 可注入时钟函数（测试用），默认 time.time()
        """
        self.filepath = filepath
        self.clock_fn = clock_fn or time.time
        self._lock = threading.Lock()  # 单写锁
        self._seq_counter = 0  # curated 也分配全局 seq（便于游标统一）

        self._recover_seq()

    def _recover_seq(self):
        """从文件扫描恢复 seq 计数器。"""
        if not self.filepath.exists():
            return

        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    seq = entry.get("seq", 0)
                    if seq > self._seq_counter:
                        self._seq_counter = seq
                except json.JSONDecodeError:
                    continue

    def append(
        self,
        owner: str,
        session_id: str,
        content: str,
        importance: int,
        refs: List[Dict[str, Any]],
        tags: Optional[List[str]] = None,
        created_at: Optional[float] = None,
    ) -> str:
        """追加一条提炼记忆，返回分配的 id。

        Args:
            owner: 所有者
            session_id: 来源会话
            content: 记忆内容
            importance: 重要度 1-5
            refs: 原文引用 [{event_seq, quote}, ...]
            tags: 封闭词表标签（提炼端归一，供检索粗筛/关系网用）
            created_at: 创建时间（可注入，默认当前）

        Returns:
            记忆条目 id（UUID）
        """
        created_at = created_at if created_at is not None else self.clock_fn()
        entry_id = str(uuid4())

        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter

            entry = {
                "seq": seq,  # 新增：便于游标统一
                "id": entry_id,
                "owner": owner,
                "session_id": session_id,
                "content": content,
                "importance": importance,
                "refs": refs,
                "tags": list(tags or []),  # 字段只增不改（§18.3）：M1-04 新增
                "created_at": created_at,
                "last_access": created_at,  # 初始等于创建时间
                "status": "active",  # active / forgotten
            }

            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return entry_id

    def append_tombstone(self, entry_id: str, reason: str = "decay"):
        """追加墓碑（标记遗忘），不删除原条目。

        Args:
            entry_id: 要遗忘的条目 id
            reason: 遗忘原因（如 "decay"）
        """
        ts = self.clock_fn()

        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter

            tombstone = {
                "seq": seq,
                "id": entry_id,  # 同 id = 更新
                "status": "forgotten",
                "forgotten_at": ts,
                "reason": reason,
            }

            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(tombstone, ensure_ascii=False) + "\n")

    def update_last_access(self, entry_id: str):
        """更新访问时间（追加更新记录）。

        retrieve 命中时调用，供衰减计算用。
        """
        ts = self.clock_fn()

        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter

            update = {
                "seq": seq,
                "id": entry_id,
                "last_access": ts,
            }

            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(update, ensure_ascii=False) + "\n")

    def read_all(
        self,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """读取所有记忆（合并同 id 的更新）。

        Args:
            owner: 过滤所有者（None = 不过滤）
            session_id: 过滤会话（None = 不过滤）
            status: 过滤状态（None = 不过滤）

        Returns:
            CuratedEntry 列表（已合并更新）
        """
        if not self.filepath.exists():
            return []

        # 按 id 分组，同 id 的后续行是更新
        entries: Dict[str, Dict[str, Any]] = {}

        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                    entry_id = record.get("id")
                    if not entry_id:
                        continue

                    if entry_id not in entries:
                        # 新条目
                        entries[entry_id] = record
                    else:
                        # 更新：合并字段
                        entries[entry_id].update(record)
                except json.JSONDecodeError:
                    continue

        # 过滤
        results = list(entries.values())
        if owner:
            results = [e for e in results if e.get("owner") == owner]
        if session_id:
            results = [e for e in results if e.get("session_id") == session_id]
        if status:
            results = [e for e in results if e.get("status") == status]

        return results

    def read_batch(
        self, after_seq: int = 0, limit: int = 100
    ) -> Iterator[Dict[str, Any]]:
        """按游标读批次（幂等，返回原始行）。

        Args:
            after_seq: 游标（读取 seq > after_seq 的记录）
            limit: 最多读几条

        Yields:
            原始 JSON 行（可能是新条目或更新）
        """
        if not self.filepath.exists():
            return

        count = 0
        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                    if record.get("seq", 0) > after_seq:
                        yield record
                        count += 1
                        if count >= limit:
                            break
                except json.JSONDecodeError:
                    continue

    def get_max_seq(self) -> int:
        """返回当前最大 seq。"""
        with self._lock:
            return self._seq_counter
