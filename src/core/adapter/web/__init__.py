"""Web 适配器：WebUI 接入（FastAPI），走 WebSocket 协议。"""

from core.adapter.web.adapter import DEFAULT_SESSION_ID, WebAdapter

__all__ = ["WebAdapter", "DEFAULT_SESSION_ID"]
