"""会话管理：SQLite 存储。

M0-09 落 `SessionStore`（会话/消息/游标）。`session_cursors` 表建好了但
用法在 M1（未读增量注入的游标，§十二），现在只是 schema 占位。
"""

from core.session.store import SessionStore

__all__ = ["SessionStore"]
