"""WebSocket 适配器：WebUI 接入（FastAPI）。"""

from core.adapter.websocket.adapter import DEFAULT_SESSION_ID, WebSocketAdapter

__all__ = ["WebSocketAdapter", "DEFAULT_SESSION_ID"]
