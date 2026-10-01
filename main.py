"""TaleAI 入口：控制流从这里开始（§18.1）。

三种跑法：
- 默认：起 WebSocket 服务，WebUI 连上来聊天（§18.1 第 9 步 adapter.start）
- `--cli`：终端里直接跟塔利聊（保留的冒烟路径，M0 验收⑤靠它验重启历史）
- `--once`：只处理一条 stdin 消息后退出（脚本化冒烟用）

**前台单条消息的处理链**在 `handle_message()`，它就是 §18.1 第 3~9 步的代码化。
总线只在旁路发通知，控制流一律沿调用链走（§18.1 开宗明义）——所以这里
没有任何"等总线驱动"的逻辑。
"""

import asyncio
import logging
import sys
from pathlib import Path

# 让 main.py 能找到 src/ 下的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router, UnknownPlatformError
from core.bus.event_bus import EventBus
from core.llm.chat_llm import ChatLLM
from core.log import Logging
from core.plugin.registry import PluginRegistry
from core.session.store import SessionStore

logger = logging.getLogger(__name__)

# 命令行会话固定在同一个 session_id 下——重启进程后读到的是同一条历史，
# 这就是 M0 验收标准⑤「重启后历史完整」的兑现（§二十一）
CLI_SESSION_ID = "cli:local"
# 兼容旧名字（此前 main.py 里就叫 SESSION_ID）
SESSION_ID = CLI_SESSION_ID

# 本轮失败时的占位回复。落库只为保住「1 回合 = 2 行」，内容是次要的。
_FAILED_TURN_PLACEHOLDER = "……（塔利这边出了点问题，你再试一次？）"


def _close_turn_on_error(
    store: SessionStore, session_id: str, exc: Exception
) -> Reply:
    """对话轮失败时补一条占位 assistant 行，把这一回合收尾，并返回可发回的 Reply。

    用户消息已经先落库了（收即存），若这轮就此中断，历史里会留下一条
    单挂的 user 行——而弹簧窗口按「1 回合 = 2 行」计数（§18.1），
    单挂会让回合计数错位。所以失败也要把这一路补平。

    返回 Reply 而不是 None：用户那边得看到一句交代，界面不能无声卡住。
    """
    logger.exception("对话轮失败：%s", exc)
    try:
        store.append(session_id, "assistant", _FAILED_TURN_PLACEHOLDER)
    except Exception:
        # 连占位都写不进去（多半是库/磁盘坏了）——记日志就好，
        # 绝不能在这里再炸一次，否则用户看到的是堆栈而非提示
        logger.exception("补写占位 assistant 也失败")
    return Reply(
        session_id=session_id,
        messages=[_FAILED_TURN_PLACEHOLDER],
        tool_calls_made=0,
        stop_reason="error",
    )


async def handle_message(
    message: Message,
    *,
    router: Router,
    store: SessionStore,
    bot,
    bus: EventBus,
) -> Reply | None:
    """处理一条入站消息：§18.1 第 3~9 步。

    返回发给用户的 Reply；被忽略（空白/平台不认识/落库失败）时返回 None。

    为什么是 async：第 6 步要 `await` 模型调用。它必须 await `run_loop()`
    而不是调同步的 `chat()`——后者内部 `asyncio.run()`，在已在跑的事件循环里
    再 run 会直接报错。
    """
    session_id = message.session_id

    # 空白消息：不发请求、不落库（跟 CLI 的空行一致，省一次无意义调用）
    if not message.content.strip():
        return None

    # 第 3 步：Router 定位会话。M0 只是一个"内循环"查表——确认平台认识，
    # 顺便拿到发回时用的适配器。不认识就记日志跳过，不让前台循环死掉。
    try:
        adapter = router.route(message)
    except UnknownPlatformError:
        logger.warning("消息来自未登记的平台 %r，已忽略", message.platform)
        return None

    # 会话行必须先存在（messages 有外键指过来）。幂等。
    store.ensure_session(
        session_id, platform=message.platform, owner=message.owner
    )

    # 第 4 步：收即存——**先读历史、再落库**。
    # 顺序不能反：装配 = history + 最新提问，若先落库再读，最新提问会同时
    # 躺在 history 末尾和"最新提问"里，重复发一遍。
    history = store.history(session_id)
    try:
        store.append(session_id, "user", message.content)
    except Exception:
        # 用户消息存不下来，这轮干脆不调模型——免得用户以为已经发出去了
        logger.exception("用户消息落库失败，跳过本轮")
        return None

    # 第 5~6 步：装配 + FC 循环（装配在 run_loop 内部完成，§18.5 硬规则 6）
    try:
        reply = await bot.run_loop(message.content, history, session_id)
    except Exception as exc:
        reply = _close_turn_on_error(store, session_id, exc)
        await _safe_send(adapter, reply)
        return reply

    # 第 7 步：assistant 真实消息落库；tool_json 一并存
    try:
        store.append(
            session_id,
            "assistant",
            "\n\n".join(reply.messages),
            tool_json={"calls": reply.tool_calls_made,
                       "stop_reason": reply.stop_reason} if reply.tool_calls_made else None,
        )
    except Exception:
        # 落库失败不该妨碍用户看到回复——记日志，继续往下发
        logger.exception("assistant 消息落库失败")

    # 第 8 步：发回
    await _safe_send(adapter, reply)

    # 第 9 步：旁路喊一声"已发送"（订阅者：on_message_sent 钩子、memory 触发）
    bus.publish(
        "message.sent",
        session_id=session_id,
        platform=message.platform,
        messages=list(reply.messages),
        tool_calls_made=reply.tool_calls_made,
        stop_reason=reply.stop_reason,
    )
    return reply


async def _safe_send(adapter: AdapterBase, reply: Reply) -> None:
    """发回复，失败只记日志。

    发送失败（连接刚断）是正常情况，不能让它把整条处理链炸掉——那条消息
    历史已经落库了，用户重连后能看到。
    """
    try:
        await adapter.send(reply)
    except Exception:
        logger.exception("发送回复失败：session=%s", reply.session_id)


async def serve_forever(
    adapters: list[AdapterBase],
    *,
    router: Router,
    store: SessionStore,
    bot,
    bus: EventBus,
) -> None:
    """常驻前台循环：每个适配器一个 recv 协程（§18.5「每平台 1 个 recv 协程」）。

    取消（Ctrl-C / 进程退出）时整体停下并向上抛 CancelledError。
    """

    async def pump(adapter: AdapterBase) -> None:
        while True:
            message = await adapter.recv()
            # handle_message 内部已把所有失败收成返回值，不会抛；
            # 这里再兜一层，保证单条消息的意外绝不终止整个前台循环。
            try:
                await handle_message(
                    message, router=router, store=store, bot=bot, bus=bus
                )
            except Exception:
                logger.exception("处理消息时发生未预期的错误，已跳过这条")

    await asyncio.gather(*(pump(a) for a in adapters))


# ---------- 启动：组装依赖（§18.1 第 1~9 步） ----------


def _build(bus: EventBus | None = None):
    """按 §18.1 组装出 (store, router, bot, bus, adapters)。"""
    bus = bus if bus is not None else EventBus()
    store = SessionStore().open()  # 第 3 步：SQLite + WAL + 建表
    PluginRegistry().scan()  # 第 4 步：扫内置 + data/plugin 注册工具

    from core.adapter.websocket.adapter import WebSocketAdapter

    bot = ChatLLM()

    # 第 8 步：适配器名册 + Router
    registry = AdapterRegistry()
    ws_adapter = WebSocketAdapter(bus=bus)
    registry.register(ws_adapter)
    # QQ 是 M0-14，不阻塞主线；接入时在这里 register 即可，Router 自动认识
    router = Router(registry)

    return store, router, bot, bus, [ws_adapter]


async def _serve() -> None:
    """默认跑法：起 WebSocket 服务（WebUI 聊天）。"""
    store, router, bot, bus, adapters = _build()
    try:
        # 第 9 步：把适配器挂成常驻任务，然后前台循环等消息
        await asyncio.gather(
            *(a.start() for a in adapters),
            serve_forever(adapters, router=router, store=store, bot=bot, bus=bus),
        )
    finally:
        store.close()


# ---------- 终端模式（冒烟用） ----------


def _run_cli(once: bool = False) -> None:
    """终端里直接聊——保留的冒烟路径（M0 验收⑤靠它验重启历史）。

    单线程同步：这里没有事件循环，所以可以放心用同步的 `bot.chat()`。
    """
    store = SessionStore().open()
    PluginRegistry().scan()
    bot = ChatLLM()

    store.ensure_session(CLI_SESSION_ID)
    history: list[dict[str, str]] = store.history(CLI_SESSION_ID)

    if history:
        print(f"===== 塔利记得你之前来过（已恢复 {len(history)} 条历史）=====\n")
    else:
        print("===== 塔利在等你聊天（输入 退出/quit 结束） =====\n")

    try:
        while True:
            user_input = input("你: ").strip()
            if not user_input:
                continue
            if user_input.lower() in {"退出", "quit", "exit", "q"}:
                print("\n塔利: 那我们就聊到这吧~ 下次见！")
                break

            try:
                store.append(CLI_SESSION_ID, "user", user_input)
            except Exception as exc:
                logger.exception("用户消息落库失败")
                print(f"\n  〔这条消息没存下来，这轮跳过：{exc}〕")
                continue

            try:
                # 签名对齐 §22：chat(session_id, text, history)
                reply = bot.chat(CLI_SESSION_ID, user_input, history)
                store.append(
                    CLI_SESSION_ID,
                    "assistant",
                    "\n\n".join(reply.messages),
                    tool_json={"calls": reply.tool_calls_made,
                               "stop_reason": reply.stop_reason} if reply.tool_calls_made else None,
                )
            except Exception as exc:
                reply = _close_turn_on_error(store, CLI_SESSION_ID, exc)
                print(f"\n  〔塔利这次没答上来：{exc}〕")
                history = store.history(CLI_SESSION_ID)
                continue

            history = store.history(CLI_SESSION_ID)

            for message in reply.messages:
                print(f"\n塔利: {message}")
            if reply.tool_calls_made:
                print(f"\n  〔调用了 {reply.tool_calls_made} 次工具，结束原因：{reply.stop_reason}〕")

            if once:
                break
    finally:
        store.close()


def main() -> None:
    Logging.init()  # 启动第 2 步：日志落 data/logs/，按天轮转
    argv = sys.argv[1:]
    if "--cli" in argv:
        _run_cli(once="--once" in argv)
        return
    if "--once" in argv:
        # --once 隐含走 CLI（读一条 stdin 就退出），方便脚本化冒烟
        _run_cli(once=True)
        return
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        print("\n塔利: 我先歇会儿，回头见~")


if __name__ == "__main__":
    main()
