"""会话存储：SQLite + WAL。

M0-09。验收标准：**重开后历史完整**（§二十一）。

为什么是 SQLite 而不是 JSONL：会话数据要按 session 检索、要按时间排序、
还要支持"打开历史提问"这种随机读；JSONL 只擅长追加。文档把权威落点
分得很清楚（§18.2 数据落点总表）：会话 → SQLite，事件/记忆/清单 → JSONL。

两个必须守住的不变量：
1. **system_reminder 永不进 messages 表**（§十二 persist=False）。动态块是
   每次请求现拼的环境信息，写进历史就等于每轮塞一份时间戳，上下文会滚雪球。
   这里用显式校验把这条钉死，而不是靠调用方自觉。
   ——**只管我们自己产出的正文**（assistant / system）。用户说的话原样收，
   他说什么都算他的话：用户粘贴一段含 `<system_reminder>` 的提示词是正当行为，
   不该被当成攻击拒之门外（此前会因此丢掉整条消息，PR #10 已复现）。
   用户文本里若夹带该标记、又会被拼进动态块，由装配侧转义（见
   context.escape_tag_markers），不靠这里的拒绝。
2. **tool_json 落在 assistant 行上**，不单独占行。这样"1 回合 = 2 行"
   始终成立，历史裁剪的回合假设才不会失效。

关于写并发：§18.5 的最终形态是写盘走 to_thread 单写线程。M0 的 CLI 是
单线程调用，这里用一把锁 + 同步 sqlite3 就够，不提前引入线程池——
等 M0-11 适配器接进来、真有并发写的时候再按文档改。
"""

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# store.py 位于 src/core/session/ → parents[3] 是项目根
DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "sessions.db"

# 动态块的标记。出现即拒绝落库——见模块 docstring 的不变量 1。
#
# 为什么不区分大小写：`<system_reminder` 是标签名，HTML/XML 语义里大小写不该
# 改变含义。此前用大小写敏感的子串判断，`<System_Reminder>` 能整段绕过落库、
# 与真正的动态块拼进同一条 user 消息，等于伪造一段系统提示（审查 PR #10 已复现）。
_PERSIST_FORBIDDEN = re.compile(r"<system_reminder", re.IGNORECASE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  kind TEXT NOT NULL,
  owner TEXT NOT NULL,
  title TEXT,
  created_at REAL NOT NULL,
  last_active REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  mentions TEXT,
  reply_to TEXT,
  tool_json TEXT,
  -- assistant 分条数组(JSON);NULL=旧行/单条,读时回落启发式。content 仍是权威纯文本
  parts_json TEXT,
  attachments TEXT,                 -- 图片文件名 JSON 数组（UX-06 多模态）
  ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS session_cursors(
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  event_seq INTEGER NOT NULL DEFAULT 0
);
"""

# 建表之后要确保存在的列：列名 → 建列 DDL 的类型部分。
# 旧库（parts_json 之前建的）不会有这一列，靠 _ensure_columns 补。
# 为什么不引入 migration 框架：M0 是单机 SQLite，`ALTER TABLE ADD COLUMN`
# 是纯元数据操作（不重写表、不复制数据），旧行自动为 NULL，瞬时完成——
# 一个探测 + 补列就够了，没必要为一行列号上 Alembic 那种重器。
_ADDED_COLUMNS = {
    "messages": {"parts_json": "TEXT", "attachments": "TEXT"},
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已存在的表补上新增列（旧库迁移）。

    `CREATE TABLE IF NOT EXISTS` 对**已存在**的表是空操作——老库的 messages
    表不会因为 _SCHEMA 里多写了一列就自动长出这列。所以建表后单独探测一次，
    缺哪列补哪列。新库走 _SCHEMA 已含全部列，这里查出来齐全、什么都不做。
    """
    for table, columns in _ADDED_COLUMNS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, ddl_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


class SessionStore:
    """会话/消息的持久化。schema 见 §18.3。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self._conn: sqlite3.Connection | None = None
        # 写锁：SQLite 连接在多线程下不安全，而 to_thread 模型是文档
        # 写明的方向，先用一把锁把并发挡在门外
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    def open(self) -> "SessionStore":
        """打开（或新建）数据库，建表，开 WAL（§二十一「开 WAL」）。"""
        if self._conn is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        # 建表中途任何一步失败（磁盘满、库损坏、PRAGMA 被拒），都必须把这条
        # 连接关掉再抛——否则 self._conn 永远是 None，调用方既拿不到句柄、
        # 也没有 close() 能关它，连接就泄漏到进程退出为止。
        try:
            conn.row_factory = sqlite3.Row
            # WAL：读写不互相阻塞，崩溃后可恢复（文档明确要求开了 WAL）
            conn.execute("PRAGMA journal_mode=WAL")
            # 外键约束默认关闭，messages.session_id 的 REFERENCES 要它才生效
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            # 旧库补列：新建库已含全部列（executescript 的 IF NOT EXISTS 建表
            # 语句只在表不存在时生效，老库的表不会因此被改成新形状），所以这里
            # 专门探测 + 补——两条路径都幂等。parts_json（#11）与 attachments
            # （#12）都在 _ADDED_COLUMNS 里，统一走这一条。
            _ensure_columns(conn)
            conn.commit()
        except Exception:
            conn.close()
            raise
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SessionStore":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SessionStore 未打开，请先调用 open()")
        return self._conn

    # ---------- 会话 ----------

    def ensure_session(
        self,
        session_id: str,
        *,
        platform: str = "cli",
        kind: str = "private",
        owner: str = "local",
        title: str | None = None,
    ) -> None:
        """会话不存在就建；已存在则只更新 last_active。

        幂等是必须的：每次收到消息都会调它，不能重复插入。
        """
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO sessions(id, platform, kind, owner, title, created_at, last_active)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET last_active = excluded.last_active
                """,
                (session_id, platform, kind, owner, title, now, now),
            )
            self._db.commit()

    def sessions(self) -> list[dict]:
        """列出全部会话，最近活跃在前。"""
        rows = self._db.execute(
            "SELECT * FROM sessions ORDER BY last_active DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def by_tag(self, tag: str) -> list[dict]:
        """按标签找会话（§22 的 `by_tag()`）。

        M0 的 schema 里还没有独立的标签表——能当"标签"用的只有 kind
        （private / group）和 title。这里先按这两者匹配，等 WebUI（M0-12）
        真的需要多标签时再补表，不提前造。
        """
        rows = self._db.execute(
            "SELECT * FROM sessions WHERE kind = ? OR title = ? ORDER BY last_active DESC",
            (tag, tag),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 消息 ----------

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        tool_json: str | dict | list | None = None,
        parts: list[str] | None = None,
        mentions: list[str] | None = None,
        reply_to: str | None = None,
        attachments: list[str] | None = None,
        ts: float | None = None,
    ) -> int:
        """追加一条消息，返回它的 seq。

        收即存（§18.1 第 4/7 步）：用户消息一收到就落库，崩溃不丢。

        parts：assistant 回复的**分条数组**（一条 <msg> = 一个元素）。content
        是拼好的纯文本（权威展示/检索形态），parts 是可选的往返 sidecar——
        下一轮补 <msg> 标签时按它精确还原，不必去猜 content 里的空行（审查
        PR #10：content 用 \\n\\n 拼/拆无法往返）。user 行不需要传，传了也
        只对 assistant 有意义。parts 为空/None → 该列存 NULL，读时回落旧启发式。

        attachments：本条消息带的图片文件名列表（UX-06）。只存文件名，
        图片本体在 data/temp/img/（可回收）——两者生命周期不同，不混存。
        """
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"非法的 role: {role!r}")

        # 不变量 1：动态块绝不落库——但**只管我们自己产出的正文**。
        # assistant/system 的正文若出现真动态块标记，是程序把动态块写进了
        # 本该字面文本的地方，必须拦（否则每轮重喂会让上下文滚雪球）。
        # user 的正文是用户的话，不拦：用户粘贴含该词的提示词是正当行为，
        # 拦了反而会丢掉他的整条消息（PR #10 复现的正是这个）。
        if role in {"assistant", "system"} and _PERSIST_FORBIDDEN.search(content):
            raise ValueError(
                "拒绝把 <system_reminder> 写入历史（§十二 persist=False）："
                "动态块是每次请求现拼的，落库会让上下文滚雪球"
            )

        if isinstance(tool_json, (dict, list)):
            tool_json = json.dumps(tool_json, ensure_ascii=False)
        if mentions is not None:
            mentions = json.dumps(mentions, ensure_ascii=False)
        if parts:
            parts_json = json.dumps(parts, ensure_ascii=False)
        else:
            parts_json = None
        if attachments is not None:
            attachments = json.dumps(attachments, ensure_ascii=False)

        with self._lock:
            cur = self._db.execute(
                """
                INSERT INTO messages(session_id, role, content, mentions, reply_to,
                                     tool_json, parts_json, attachments, ts)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, role, content, mentions, reply_to, tool_json,
                 parts_json, attachments, time.time() if ts is None else ts),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def history(
        self, session_id: str, limit: int | None = None, *, with_parts: bool = False
    ) -> list[dict]:
        """读回历史，按时间正序（旧 → 新）。

        返回 [{"role", "content"}]——正是 assemble_messages 需要的形状，
        这样调用方不用再做一次转换。

        ❗attachments 有意**不进返回值**：history() 的消费者（ChatLLM 装配、
        WebUI 历史帧）走的是"文本为主"的路径，喂历史里的图给模型是 UX-07/08
        的事。要图请用 messages()。这样也保住了 `history() == [{role, content}]`
        这个被多处测试钉死的契约。

        limit 取的是**最近** N 条（尾部），但返回时仍是正序——
        直接 `ORDER BY seq DESC LIMIT n` 会得到倒序，得在内存里翻回来。

        with_parts：默认 False，形状与历史版本**完全一致**（不改既有调用方）。
        True 时，凡该行的 parts_json 非空（assistant 分条），额外带上
        `"parts": [...]`——这是给「补 <msg> 标签喂模型」和「WebUI 分气泡」
        用的；没有分条信息的行（user、旧行）**不加这个键**，让消费方对称降级
        （无 parts 就走旧的纯文本路径）。
        """
        columns = "role, content, parts_json" if with_parts else "role, content"
        if limit is None:
            rows = self._db.execute(
                f"SELECT {columns} FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        else:
            rows = self._db.execute(
                f"SELECT {columns} FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            rows = list(reversed(rows))

        result: list[dict] = []
        for r in rows:
            entry: dict = {"role": r["role"], "content": r["content"]}
            if with_parts and r["parts_json"]:
                try:
                    parts = json.loads(r["parts_json"])
                except (ValueError, TypeError):
                    parts = None  # 库里存坏了就当没有，回落到纯文本，别让整条历史炸掉
                if isinstance(parts, list) and parts:
                    entry["parts"] = parts
            result.append(entry)
        return result

    def history_with_attachments(self, session_id: str) -> list[dict]:
        """给 WebUI 历史帧用：[{role, content, images, parts}]。

        - images：图片文件名列表（UX-06），网页回放要显示图。
        - parts：assistant 的分条数组（#11），网页要把一次多段回复拆成多气泡。

        跟 history() 分开的原因：history() 是喂模型/给记忆的**文本契约**
        （被多处测试钉着，不含附件/parts）；网页回放需要这两样。两者别合并——
        模型不该因为界面上有图就改变喂进去的历史形状。
        """
        rows = self._db.execute(
            "SELECT role, content, attachments, parts_json FROM messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        out = []
        for r in rows:
            imgs = []
            if r["attachments"]:
                try:
                    imgs = json.loads(r["attachments"])
                except (json.JSONDecodeError, TypeError):
                    imgs = []
            parts = None
            if r["parts_json"]:
                try:
                    parts = json.loads(r["parts_json"])
                except (json.JSONDecodeError, TypeError):
                    parts = None
            out.append({"role": r["role"], "content": r["content"],
                        "images": imgs, "parts": parts})
        return out

    def messages(self, session_id: str, limit: int | None = None) -> list[dict]:
        """读回带元信息的完整行（排障用：看得到 tool_json / mentions / ts）。"""
        sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY seq"
        params: tuple = (session_id,)
        if limit is not None:
            sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT ?"
            params = (session_id, limit)
        rows = self._db.execute(sql, params).fetchall()
        if limit is not None:
            rows = list(reversed(rows))
        return [dict(r) for r in rows]

    def count(self, session_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["n"])

    def clear(self, session_id: str) -> int:
        """清空某个会话的全部消息（不删会话行本身）。返回删掉的行数。

        为什么留着 sessions 行：会话的身份（platform / kind / owner）不该因为
        "清空聊天记录"就没了——清了历史它还是同一个会话，下次来消息时
        ensure_session 也就不用重建。这跟聊天软件里"清空聊天记录"是一致的。

        用 rowcount 返回条数，调用方能据此给用户一个交代（"清了 N 条"）。
        """
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            self._db.commit()
            return int(cur.rowcount)
