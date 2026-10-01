"""QQ 适配器：SnowLuma/OneBot 11 反向 WebSocket（设计文档 §18.1 / §22）。

**反向 WS**：SnowLuma 作为客户端主动连**我们**的 `/qq` 端点（它在
`wsClients` 里配置我们的地址）。所以这里要起一个 WS 服务端——跟
WebSocketAdapter 同一套 FastAPI app 也可以是独立的，M0 先独立成自己的
app，main 里按需挂载。

握手带两个头（OneBot 11 规范要求读）：
- `X-Self-ID`：机器人自己的 QQ 号 → 我们不用手填配置就能知道 bot_id
- `X-Client-Role`：`Api` / `Event` / `Universal`

同一条连接上跑两种报文，靠字段区分（规范定义）：
- 带 `post_type` → 事件（用户消息）
- 带 `echo` → 我们发出动作的响应（不处理，只记 debug）

**为什么 M0 不做正向 WS**：文档 §18-18 平台优先级只要求 QQ 能连上；
反向 WS 省掉了"在代码里配 QQ 后端地址 + 自己实现断线重连"——重连由
SnowLuma 负责（它的 `reconnectIntervalMs`）。少一半代码。
"""

import asyncio
import logging
from collections import OrderedDict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.qq.protocol import build_send_action, parse_event
from core.bus.event_bus import EventBus

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 去重窗口：平台重发（断线重连）会带同一个 message_id
_DEDUP_MAX = 1000


class QQAdapter(AdapterBase):
    """QQ 接入（SnowLuma / OneBot 11，反向 WebSocket）。"""

    name = "qq"

    def __init__(self, bus: EventBus | None = None, *, path: str = "/qq") -> None:
        super().__init__(bus=bus)
        self.path = path
        # 当前连接。反向 WS 只有一条（SnowLuma 单连接），断了就置 None
        self._link: WebSocket | None = None
        # 学来的机器人 QQ 号（X-Self-ID）
        self.bot_id: str = ""
        # 已见过的 message_id（FIFO 淘汰）
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self.app = self._build_app()

    # ---------- 状态 ----------

    def connected(self) -> bool:
        return self._link is not None

    # ---------- AdapterBase ----------

    def normalize(self, raw: dict) -> Message | None:  # type: ignore[override]
        """OneBot 事件 → Message。不是消息事件返回 None。

        覆写了基类的返回类型（允许 None）——因为 OneBot 会推很多非消息事件
        （心跳、通知），"不是消息"是这里的常规情况而非异常。
        """
        return parse_event(raw)

    async def send(self, reply: Reply) -> None:
        """把一次对话结果发回 QQ。多条消息合并成一条。"""
        if self._link is None:
            logger.warning("QQ 未连接，丢弃这条回复：session=%s", reply.session_id)
            return

        text = "\n".join(m for m in reply.messages if m)
        if not text:
            return
        try:
            action, params = build_send_action(reply.session_id, text)
        except ValueError:
            logger.warning("会话 ID 不是 QQ 的，发不出去：%r", reply.session_id)
            return

        try:
            await self._link.send_json({"action": action, "params": params,
                                        "echo": f"send-{reply.session_id}"})
        except Exception:
            logger.warning("向 QQ 发送失败，连接可能已断", exc_info=True)

    async def start(self) -> None:
        """起一个独立的 uvicorn 服务跑 /qq。

        M0 独立端口：SnowLuma 的 `wsClients.url` 指向它即可，跟 WebUI 的
        WS 端口分开，互不干扰（用户已在跑一个 WebUI 服务在 8000）。
        """
        import uvicorn

        from core.config.loader import Config

        try:
            cfg = Config.load("platforms").get("qq", {})
        except Exception:
            cfg = {}
        host = cfg.get("host", "127.0.0.1")
        port = int(cfg.get("port", 8866))

        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning")
        logger.info("QQ 适配器监听 ws://%s:%d%s（反向 WS，等 SnowLuma 连入）",
                    host, port, self.path)
        await uvicorn.Server(config).serve()

    # ---------- FastAPI 应用 ----------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="TaleAI QQ Adapter")
        path = self.path

        @app.websocket(path)
        async def qq_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            # 规范：握手头带机器人 QQ 号和客户端类型
            self.bot_id = websocket.headers.get("x-self-id", "") or self.bot_id
            role = websocket.headers.get("x-client-role", "")
            logger.info("QQ 已连接：bot_id=%s role=%s", self.bot_id, role)

            prev, self._link = self._link, websocket
            try:
                while True:
                    raw = await websocket.receive_json()
                    self._on_frame(raw)
            except WebSocketDisconnect:
                pass
            except Exception:
                logger.exception("QQ 连接处理出错")
            finally:
                # 只在仍是当前这条连接时清理，避免误删重连后的新连接
                if self._link is websocket:
                    self._link = None
                logger.info("QQ 已断开：bot_id=%s", self.bot_id)

        return app

    def _on_frame(self, raw: dict) -> None:
        """处理一帧：消息事件 → 入队；API 响应 → 忽略。"""
        if not isinstance(raw, dict):
            return

        # 我们发出动作的应答（带 echo）：M0 不关心发送结果，记 debug 即可
        if "echo" in raw and "post_type" not in raw:
            logger.debug("QQ API 响应：%s", raw.get("status"))
            return

        message = self.normalize(raw)
        if message is None:
            return

        # 去重：断线重连时平台会重发，同一个 message_id 只收一次
        if message.id:
            if message.id in self._seen_ids:
                logger.debug("QQ 重复消息，已忽略：id=%s", message.id)
                return
            self._seen_ids[message.id] = None
            while len(self._seen_ids) > _DEDUP_MAX:
                self._seen_ids.popitem(last=False)

        if not message.content:
            return  # 纯图片/表情等无文本——M0 模型看不到，跳过

        self._deliver(message)
