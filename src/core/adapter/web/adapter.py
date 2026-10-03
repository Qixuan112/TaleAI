"""Web 适配器：WebUI 接入（设计文档 §1.6 / §18.1 / §22）。

「Web」是**平台名**（跟 `qq` 对称）——它走 WebSocket 协议，但平台本身叫 Web/WebUI，
所以不叫 WebSocketAdapter（那是把协议名当平台名，见 PR 评审）。

用 FastAPI 起服务（文档 §1.6 的 WebUI 演进方向；M0 就一步到位，省得以后迁）。
本模块只做"接入"这件事：收 WebUI 的原始数据 → 归一成 `Message`；
收 `Reply` → 发回对应连接。它不认识 ChatLLM，也不认识工具（§22 import 单向）。

**会话 ID 为什么由客户端给**：WebUI 一旦刷新就断连重连，若每次连接都新分配
一个 session_id，历史就跟着断了——而"M0 验收⑤：重启后历史完整"正要求它在
重连后还认得同一个会话。所以 session_id 走查询参数（`/ws?session_id=web:local`），
默认值保证裸连也能用。这和 QQ 用"对方 ID"、CLI 用固定串是同一种做法：
**稳定 ID 由平台侧决定，适配器负责把它带进 Message**。

连接表放在适配器里（session_id → WebSocket）：`send(reply)` 只拿得到
`Reply.session_id`，得靠它反查该往哪条连接发。同会话重连 = 覆盖旧连接，
最后一条连接为准。
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.adapter.base import AdapterBase, Message, Reply
from core.event_bus import EventBus
from core.image_store import MAX_IMAGES_PER_MESSAGE
from core.log_stream import LogStream

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# webui/ 在项目根下（src/core/adapter/web/ → parents[4]）
WEBUI_DIR = Path(__file__).resolve().parents[4] / "webui"

# 没带 session_id 时的默认会话——裸连（比如直接 ws 客户端测试）也能用
DEFAULT_SESSION_ID = "web:local"

# WebUI 会话 id 的固定前缀。服务端只认这个前缀，客户端不能借 `/ws` 去读/写
# 别的平台的会话（cli:local、qq:p<号> …）——那些 id 是可预测的（PR #10）。
WEB_SESSION_PREFIX = "web:"

# 允许的 Origin 主机（浏览器 WebSocket 不受 CORS 限制，必须自己查）。
# 只放本机：M0 是单机自用服务，页面和 WS 同源。缺 Origin（非浏览器客户端，
# 如测试/脚本）也放行——拦截的目标是"别的网页"，不是命令行。
_ALLOWED_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}



class WebAdapter(AdapterBase):
    """WebUI 的接入（走 WebSocket 协议）。"""

    name = "web"

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        history_provider: Callable[[str], list[dict]] | None = None,
        clearer: Callable[[str], int] | None = None,
        stream: LogStream | None = None,
        extra_routes: Callable[[FastAPI], None] | None = None,
    ) -> None:
        super().__init__(bus=bus)
        self.host = host
        self.port = port
        # 读历史的回调（session_id → [{role, content}]）。由 main 注入 store.history。
        # 为什么用回调而不是直接传 store：适配器不该认识 SessionStore（§22 import 单向）。
        # 不注入就退化成"连上不补历史"——测试/裸连能用。
        self._history_provider = history_provider
        # 清空历史的回调（session_id → 删掉的条数）。由 main 注入 store.clear。
        # 同样不直接传 store，理由同上；不注入就忽略清空请求。
        self._clearer = clearer
        # 实时日志流（可选）。注入才挂 /events（SSE）与 /logs（调试页）——
        # 不注入时行为跟以前完全一样（大量测试构造裸适配器，不能被波及）。
        self._stream = stream
        # 控制面路由注册器（可选）。由 main 注入一个 `(app) -> None`，在里面挂
        # 设置读写的 /api/* 等业务路由。用回调而不是把 Config 塞进来——同样是
        # §22 import 单向：适配器只管"接",不认识配置/存储。不注入＝不多挂任何路由。
        self._extra_routes = extra_routes
        # session_id → 连接。同会话重连覆盖旧连接（后连的说了算）
        self._connections: dict[str, WebSocket] = {}
        self.app = self._build_app()

    # ---------- 归一：原始数据 → Message ----------

    def normalize(self, raw: dict) -> Message:
        """把 WebUI 发来的 JSON 归一成 Message。

        期望形状：`{"content": "说了什么", "session_id": "web:local",
        "images": ["<文件名>"]}`（images 可省，UX-07）。
        session_id 缺省用默认值；id / ts 服务端生成（客户端不该操心全局唯一性）。
        images 是上传端点返回的文件名——客户端先 POST /api/upload 拿到名字，
        再放进消息帧；这里只透传，不校验存在性（喂模型时读不到会自然跳过）。
        """
        content = str(raw.get("content", "")).strip()
        # session_id 走 _web_session_id 约束到 web: 前缀内（PR #10 鉴权）。
        session_id = self._web_session_id(str(raw.get("session_id") or ""))
        # images（UX-07）：上传端点返回的文件名，只透传、不校验存在性。
        raw_images = raw.get("images") or []
        images = [str(x) for x in raw_images if str(x).strip()] if isinstance(raw_images, list) else []

        return Message(
            id=uuid.uuid4().hex,
            platform=self.name,
            session_id=session_id,
            # WebUI 是本人单机使用，owner **固定** local（记忆隔离维度，§十八-4）。
            # 曾经读客户端的 owner 字段——叠加 /ws 无鉴权时，第一条消息就能把
            # 会话 owner 永久写成任意字符串（PR #10）。这里是 WebUI 侧，就该是 local。
            owner="local",
            direction="in",
            role="user",
            content=content,
            images=images[:MAX_IMAGES_PER_MESSAGE],  # 上限，防滥用（图太多也顶上下文）
            ts=time.time(),
            meta={},
        )

    def _web_session_id(self, raw_id: str) -> str:
        """把客户端给的 id 约束到 `web:` 前缀内，否则回落默认会话。

        为什么：`/ws` 不校验来源时，客户端能传 `cli:local` / `qq:p<QQ号>` 这类
        可预测的跨平台 id，去读/清空/写入**别的平台**的会话（PR #10）。只认
        `web:` 前缀，就把 WebUI 关在它自己那一小片会话里。
        """
        if raw_id.startswith(WEB_SESSION_PREFIX) and raw_id != WEB_SESSION_PREFIX:
            return raw_id
        if raw_id:
            logger.warning("拒绝非 web: 前缀的会话 id %r，回落默认会话", raw_id)
        return DEFAULT_SESSION_ID

    def _origin_allowed(self, websocket: WebSocket) -> bool:
        """握手来源校验：只放行本机 Origin（缺 Origin 放行，给脚本/测试用）。

        为什么必须自己查：浏览器的 WebSocket **不受同源策略/CORS 限制**，本机
        任意网页都能 `new WebSocket("ws://127.0.0.1:8000/ws?session_id=...")`
        去连、读历史、清空、甚至写消息烧钱（PR #10）。所以要在 accept 之前
        看 Origin 头，不是本机就拒。

        **鉴权接缝**：M0 的鉴权就这一层。将来要远程访问时，在这里追加
        token / Cookie 校验即可，调用点（ws_endpoint）不用动。
        """
        origin = websocket.headers.get("origin")
        if not origin:
            return True  # 非浏览器客户端（curl / 测试 / 原生 app）没有 Origin
        try:
            host = urlparse(origin).hostname
        except ValueError:
            return False
        return host in _ALLOWED_ORIGIN_HOSTS

    # ---------- 发：Reply → 连接 ----------

    async def send(self, reply: Reply) -> None:
        """把一次对话结果发回该会话的那条连接。

        会话已断开（连接表里没有）→ 记日志跳过，不抛：用户可能刚关了页面，
        这是正常情况，不该让前台循环崩在"发不出去"上。
        """
        socket = self._connections.get(reply.session_id)
        if socket is None:
            logger.warning("会话 %r 已断开，丢弃这条回复", reply.session_id)
            return

        payload = {
            "session_id": reply.session_id,
            "messages": reply.messages,
            "tool_calls_made": reply.tool_calls_made,
            "stop_reason": reply.stop_reason,
        }
        try:
            await socket.send_json(payload)
        except Exception:
            # 连接可能刚好在这一刻断掉（发送时才暴露）——不是程序错误
            logger.warning("向会话 %r 发送失败，连接可能已断", reply.session_id, exc_info=True)

    # ---------- FastAPI 应用 ----------

    async def _push_history(self, websocket: WebSocket, session_id: str) -> None:
        """连上时补发该会话的历史。

        这是一个独立帧（type=history），跟对话回复分开——前端据此把历史一次
        渲染成气泡，不用跟后续回复抢消息顺序。没有 provider 就跳过。
        """
        if self._history_provider is None:
            return
        try:
            history = self._history_provider(session_id)
        except Exception:
            logger.exception("读历史失败：session=%s", session_id)
            return
        if not history:
            return
        try:
            await websocket.send_json({"type": "history", "messages": history})
        except Exception:
            logger.warning("补发历史失败：session=%s", session_id, exc_info=True)

    async def _clear_and_reply(self, websocket: WebSocket, session_id: str) -> None:
        """清空该会话历史，并回一帧告诉前端清了几条。

        前端据此把页面气泡也抹掉。没注入 clearer 就只回 ok=0——
        不报错（清空不是核心链路），也别让前端一直等回执。
        """
        deleted = 0
        ok = True
        if self._clearer is not None:
            try:
                deleted = self._clearer(session_id)
            except Exception:
                ok = False
                logger.exception("清空历史失败：session=%s", session_id)
        else:
            logger.warning("收到清空请求但未注入 clearer：session=%s", session_id)
        try:
            await websocket.send_json(
                {"type": "cleared", "session_id": session_id, "deleted": deleted, "ok": ok}
            )
        except Exception:
            logger.warning("回清空结果失败：session=%s", session_id, exc_info=True)

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="TaleAI WebUI")

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket) -> None:
            # 来源校验必须在 accept 之前：拒绝就关，既不推历史也不登记连接。
            # 1008 = policy violation（WS 关闭码约定）。
            if not self._origin_allowed(websocket):
                logger.warning(
                    "拒绝非本机 Origin 的 /ws 连接：%r",
                    websocket.headers.get("origin"),
                )
                await websocket.close(code=1008)
                return
            await websocket.accept()
            # 会话 ID 从查询参数取（刷新重连带同一个 ID，历史接得上），
            # 但只认 web: 前缀——不认就回落默认，堵住跨平台会话注入。
            session_id = self._web_session_id(
                websocket.query_params.get("session_id") or ""
            )
            self._connections[session_id] = websocket
            logger.info("WebUI 已连接：session=%s", session_id)
            # 先把该会话的历史推过去——否则刷新后页面是空的（M0 验收⑤）
            await self._push_history(websocket, session_id)
            try:
                while True:
                    # 按帧 try：一条坏帧（非法 JSON / 非 object）只丢这一条，
                    # 不能让它拆掉整条连接（PR #10）。receive_json 对非法 JSON
                    # 会抛，所以这里连"取帧"一起保护。
                    try:
                        raw = await websocket.receive_json()
                    except WebSocketDisconnect:
                        raise
                    except Exception:
                        logger.warning(
                            "收到无法解析的帧，已忽略：session=%s", session_id
                        )
                        continue
                    if not isinstance(raw, dict):
                        logger.warning("收到非对象帧，已忽略：session=%s", session_id)
                        continue
                    # 控制帧（清空历史）不是聊天消息，先分流——
                    # 它带 action 字段，normalize 会把它当空内容丢掉。
                    if raw.get("action") == "clear":
                        await self._clear_and_reply(websocket, session_id)
                        continue
                    message = self.normalize(raw)
                    # 空消息不发请求（跟 CLI 的空行一致）。但**纯图消息**（只有图、
                    # 没文字）是合法输入——图就是内容，不能按空丢掉（UX-07）。
                    if not message.content and not message.images:
                        continue
                    self._deliver(message)
            except WebSocketDisconnect:
                pass
            except Exception:
                logger.exception("WebSocket 连接处理出错：session=%s", session_id)
            finally:
                # 只在"当前这条连接"仍是登记的那条时才清理——避免把重连后的
                # 新连接误删（旧连接断开事件可能晚于新连接的登记）
                if self._connections.get(session_id) is websocket:
                    self._connections.pop(session_id, None)
                logger.info("WebUI 已断开：session=%s", session_id)

        # 实时日志（可选）：SSE 流 + 调试页。没注入 stream 就不挂这两条路由——
        # 大量测试构造裸适配器，行为必须跟以前一样。
        if self._stream is not None:
            stream = self._stream

            @app.get("/events")
            async def events() -> StreamingResponse:
                """SSE：把日志与关键事件实时推给页面。

                X-Accel-Buffering: no —— 万一前面挂了 nginx，别让它把流缓冲起来，
                否则「实时」就变成「攒一堆一次性吐」。没有反代时这个头无害。
                """
                return StreamingResponse(
                    stream.stream(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            @app.get("/logs")
            async def logs() -> RedirectResponse:
                """日志调试页入口，跟 / → index.html 同构。"""
                return RedirectResponse("/static/logs.html")

        # 静态聊天页。挂到 / 下；webui/index.html 就是入口（M0-12）。
        if WEBUI_DIR.is_dir():
            @app.get("/")
            async def index() -> RedirectResponse:
                return RedirectResponse("/static/index.html")

            @app.get("/settings")
            async def settings() -> RedirectResponse:
                """设置页入口，跟 / → index.html、/logs → logs.html 同构。"""
                return RedirectResponse("/static/settings.html")

            app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")

        # 控制面路由（设置读写等）由 main 用回调注入——适配器不认识 Config（§22）。
        # 放在最后：/api/* 与 /ws、/static 等不重叠，顺序无碍。
        if self._extra_routes is not None:
            self._extra_routes(app)

        return app

    # ---------- 起服务 ----------

    async def start(self) -> None:
        """跑 uvicorn（阻塞到进程结束）。由 main 用 create_task 挂成常驻任务。"""
        import uvicorn

        config = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="warning"
        )
        server = uvicorn.Server(config)
        logger.info("Web 适配器监听 ws://%s:%d/ws", self.host, self.port)
        await server.serve()

    # ---------- 排障辅助 ----------

    def connected_sessions(self) -> list[str]:
        return list(self._connections)


__all__ = ["WebAdapter", "DEFAULT_SESSION_ID"]
