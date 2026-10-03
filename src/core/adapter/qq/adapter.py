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
import uuid
from collections import OrderedDict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.pacing import typing_delay
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
        # 握手 token（OneBot access_token 约定）。空 = 不校验（单机自用默认）。
        self.access_token: str = self._load_access_token()
        # 已见过的 message_id（FIFO 淘汰）
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self.app = self._build_app()

    @staticmethod
    def _load_access_token() -> str:
        """读 platforms.qq.access_token；读配置失败就当作没配（不拦）。

        为什么要 token：反向 WS 谁都能连，任意客户端连上就会把 SnowLuma 顶掉，
        拿到 bot 的控制权（PR #10）。配了 token 就要求握手带上——单机自用可留空。
        """
        try:
            from core.config.loader import Config

            return str(Config.load("platforms").get("qq", {}).get("access_token", "") or "")
        except Exception:
            return ""

    def _token_ok(self, websocket: WebSocket) -> bool:
        """校验握手 token：查询参数 access_token 或 Authorization: Bearer。

        没配 token（空串）→ 一律放行（保持 M0 默认行为，不破坏现有部署）。
        """
        if not self.access_token:
            return True
        supplied = websocket.query_params.get("access_token", "")
        if not supplied:
            auth = websocket.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        return supplied == self.access_token

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
        """把一次对话结果发回 QQ：**每条 <msg> 分开发**。

        为什么不拼成一段：塔利一次可能分两句（先应一声、再答正文），WebUI 里
        是分开的气泡。QQ 虽然没有"气泡"概念，但能连发多条消息——拼成一段会
        把这个节奏压没，两边体验不一致。

        条与条之间按"真人打字"的节奏停一下（见 `core.adapter.pacing`）：停多久
        取决于**下一条有多长**——刚才那句短、下一句长，就多等一会儿；再叠
        一点随机抖动，不匀速。不隔的话多条消息几乎同时到达，客户端可能合并、
        顺序也可能乱。人说话本来就有停顿，这个节奏也更自然。
        """
        if self._link is None:
            logger.warning("QQ 未连接，丢弃这条回复：session=%s", reply.session_id)
            return

        # 空 <msg> 跳过——白气泡很突兀
        parts = [m for m in reply.messages if m and m.strip()]
        if not parts:
            return

        for i, text in enumerate(parts):
            if i:
                await asyncio.sleep(typing_delay(text))
            await self._send_one(reply.session_id, text)

    async def _send_one(self, session_id: str, text: str) -> None:
        """发一条消息。任何失败只记日志，不抛——发不出去不该炸掉处理链。"""
        try:
            action, params = build_send_action(session_id, text)
        except ValueError:
            logger.warning("会话 ID 不是 QQ 的，发不出去：%r", session_id)
            return
        try:
            await self._link.send_json({
                "action": action, "params": params,
                "echo": f"send-{session_id}-{uuid.uuid4().hex[:6]}",
            })
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
            # token 校验在 accept 之前：不符就关，不做任何登记（1008 = policy）。
            if not self._token_ok(websocket):
                logger.warning("QQ 反向 WS 握手 token 不符，拒绝连接")
                await websocket.close(code=1008)
                return
            await websocket.accept()
            # 规范：握手头带机器人 QQ 号和客户端类型
            self.bot_id = websocket.headers.get("x-self-id", "") or self.bot_id
            role = websocket.headers.get("x-client-role", "")
            logger.info("QQ 已连接：bot_id=%s role=%s", self.bot_id, role)

            # 新连接顶上：**主动关闭旧连接**。旧实现只是替换 _link、不关旧的，
            # 结果旧连接还开着（能收事件），而 _link 指向新的；新连接一断就
            # 把 _link 置 None，旧连接却被当成"已死"——机器人彻底变哑（PR #10）。
            prev, self._link = self._link, websocket
            if prev is not None and prev is not websocket:
                try:
                    await prev.close(code=1000)  # 让旧连接走它自己的 finally
                except Exception:
                    logger.warning("关闭上一个 QQ 连接失败", exc_info=True)
            try:
                while True:
                    try:
                        raw = await websocket.receive_json()
                    except WebSocketDisconnect:
                        raise
                    except Exception:
                        # 单帧坏了只丢这一帧，不拆连接（PR #10）
                        logger.warning("QQ 收到无法解析的帧，已忽略")
                        continue
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

        # 群聊过滤：只在被 @ 时响应。
        #
        # 为什么必须过滤：OneBot 的反向 WS 是全局的，接上就会收到该账号能看到的
        # **所有**群消息。在活跃群里逐条回复 = 刷屏 + 烧钱 + 被踢。私聊不需要 @
        # （1:1 会话，有人在说话就是在跟我说话）。
        if message.session_type == "group" and not self._mentions_bot(message):
            logger.debug("群聊未 @ 我，忽略：session=%s", message.session_id)
            return

        self._deliver(self._strip_bot_mention(message))

    def _mentions_bot(self, message: Message) -> bool:
        """这条群消息有没有 @ 机器人。

        bot_id 从握手头的 X-Self-ID 学来；还没学到就保守地不响应
        （宁可漏回，也不要在群里乱说话）。
        """
        if not self.bot_id:
            return False
        return str(self.bot_id) in message.mentions

    def _strip_bot_mention(self, message: Message) -> Message:
        """把 @ 机器人那一段从正文里去掉。

        messageFormat=array 时，正文里本来就不含 @ 段（protocol 只取 text 段），
        这里主要是清掉残留空格；string 形态下 CQ 码已在 protocol 层剥掉。
        模型没必要看到自己 QQ 号被 at。
        """
        message.content = message.content.strip()
        return message
