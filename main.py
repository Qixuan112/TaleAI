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
import os
import sys
from pathlib import Path

# 让 main.py 能找到 src/ 下的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from core.adapter.base import AdapterBase, Message, Reply
from core.adapter.registry import AdapterRegistry
from core.adapter.router import Router, UnknownPlatformError
from core.event_bus import EventBus
from core.llm.chat_llm import ChatLLM
from core.llm.persona_llm.base import USER_PERSONA_PATH, ensure_user_persona
from core.log import Logging
from core.log_stream import LogStream, StreamLogHandler
from core.plugin.registry import PluginRegistry
from core.session.store import SessionStore
from core.wake import WakePolicy, load_wake_policy

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
    wake: "WakePolicy | None" = None,
) -> Reply | None:
    """处理一条入站消息：§18.1 第 3~9 步。

    返回发给用户的 Reply；被忽略（空白/平台不认识/未唤醒/落库失败）时返回 None。

    为什么是 async：第 6 步要 `await` 模型调用。它必须 await `run_loop()`
    而不是调同步的 `chat()`——后者内部 `asyncio.run()`，在已在跑的事件循环里
    再 run 会直接报错。
    """
    session_id = message.session_id

    # 空白消息：不发请求、不落库（跟 CLI 的空行一致，省一次无意义调用）。
    # 例外：带图的消息即使没文字也算"有内容"——纯图片是合法输入（UX-06），
    # 模型看图不需要配文。
    if not message.content.strip() and not message.images:
        return None

    # 唤醒门（§UX-03）：群里没叫它 → 存进历史但不回。
    #
    # 位置讲究：放在空白守卫之后、路由之前。理由——
    # ① 在空白之后再判，空消息不会先落库（空白消息本就不该进历史）；
    # ② 在路由之前判，未唤醒消息不发请求、不读历史、不建"回合"；
    # ③ 但**仍要落库**（用户定："存进历史但不回"）——保住记忆原始素材。
    # 门本身是纯代码（core/wake.py），不经过模型：让模型自己判断"叫没叫我"
    # 等于把确定性的事交给不确定的 LLM（§十二 定论）。
    policy = wake if wake is not None else load_wake_policy()
    if policy.gates(message.session_type) and not policy.is_woken(
        message.content, message.addressed
    ):
        try:
            store.ensure_session(
                session_id, platform=message.platform, owner=message.owner,
                kind=message.session_type or "private",
            )
            # 图也要落库——未唤醒的群消息同样可能带图（纯图消息更是只有图），
            # 漏了 attachments 会让这些图在历史里凭空消失。
            store.append(session_id, "user", message.content,
                         attachments=list(message.images or []) or None)
        except Exception:
            logger.exception("未唤醒消息落库失败，跳过")
        logger.debug("群聊未唤醒，已存库但不回：session=%s", session_id)
        return None

    # 第 3 步：Router 定位会话。M0 只是一个"内循环"查表——确认平台认识，
    # 顺便拿到发回时用的适配器。不认识就记日志跳过，不让前台循环死掉。
    try:
        adapter = router.route(message)
    except UnknownPlatformError:
        logger.warning("消息来自未登记的平台 %r，已忽略", message.platform)
        return None

    # 会话行必须先存在（messages 有外键指过来）。幂等。
    # kind 必须跟着消息走——此前漏传，导致群聊会话在库里被记成 private
    # （与 §18.3 的 sessions.kind 定义不符）
    store.ensure_session(
        session_id, platform=message.platform, owner=message.owner,
        kind=message.session_type or "private",
    )

    # 第 4 步：收即存——**先读历史、再落库**。
    # 顺序不能反：装配 = history + 最新提问，若先落库再读，最新提问会同时
    # 躺在 history 末尾和"最新提问"里，重复发一遍。
    # with_parts=True：带出 assistant 的分条数组，供 run_loop 精确还原 <msg>
    # 标签（PR #10：纯文本形态无法往返）。
    history = store.history(session_id, with_parts=True)
    try:
        store.append(session_id, "user", message.content,
                     attachments=list(message.images or []) or None)
    except Exception:
        # 用户消息存不下来，这轮干脆不调模型——免得用户以为已经发出去了。
        # 但**要回一帧**：否则前端收不到任何东西，永远停在"塔利在想…"
        # （PR #10 复现）。回一帧 error 让界面解开；仍 return None——
        # user 行没落库，绝不能补写 assistant 行（否则破坏「1 回合 = 2 行」）。
        logger.exception("用户消息落库失败，跳过本轮")
        await _safe_send(adapter, Reply(
            session_id=session_id,
            messages=["……（这条消息我这边没存下来，你再发一次？）"],
            tool_calls_made=0,
            stop_reason="error",
        ))
        return None

    # 第 5~6 步：装配 + FC 循环（装配在 run_loop 内部完成，§18.5 硬规则 6）。
    # 会话类型/owner 一路带给装配——模型据此知道自己在群聊还是私聊（§十二）
    # images（UX-06）：这条消息带的图，只作用于最新提问，不回流历史。
    try:
        reply = await bot.run_loop(
            message.content, history, session_id,
            session_type=message.session_type, owner=message.owner,
            images=list(message.images or []),
        )
    except Exception as exc:
        reply = _close_turn_on_error(store, session_id, exc)
        await _safe_send(adapter, reply)
        return reply

    # 第 7 步：assistant 真实消息落库；tool_json 与分条一并存
    try:
        store.append(
            session_id,
            "assistant",
            "\n\n".join(reply.messages),
            parts=list(reply.messages),
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


async def _safe_start(adapter: AdapterBase) -> None:
    """起一个适配器；起不来只记日志，**不向上抛**。

    为什么不让它抛：这些 start() 是 `asyncio.gather` 的并列分支，任何一个抛
    异常都会让整个 gather 提前结束——**一个平台端口被占（如 QQ 的 8866 撞了
    别的进程），连 WebUI 都一起没了**。而"这个平台起不来"和"整个服务该不该
    活着"是两回事：能起的照常服务，起不来的记一笔，别互相拖垮。

    为什么捕 `BaseException` 而不是 `Exception`：uvicorn 端口绑定失败时走的是
    `sys.exit(3)` → 抛 **`SystemExit`**，它不继承 `Exception`，`except Exception`
    根本拦不住（实测踩到）。两种"该停"的信号必须放行、其余一律吞：
      - `KeyboardInterrupt`（Ctrl-C）——用户要停，必须上抛
      - `CancelledError`（asyncio 取消）——正常退出信号，必须上抛
    """
    name = getattr(adapter, "name", None) or type(adapter).__name__
    try:
        await adapter.start()
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except BaseException:
        # SystemExit / OSError(端口被占) / 其它——都当"这个平台没起来"，不拖垮别的
        logger.exception("适配器 %r 启动失败，已跳过（其余平台照常服务）", name)


async def serve_forever(
    adapters: list[AdapterBase],
    *,
    router: Router,
    store: SessionStore,
    bot,
    bus: EventBus,
    wake: "WakePolicy | None" = None,
) -> None:
    """常驻前台循环：每个适配器一个 recv 协程（§18.5「每平台 1 个 recv 协程」）。

    取消（Ctrl-C / 进程退出）时整体停下并向上抛 CancelledError。
    """
    # 唤醒策略启动时读一次（§19-13：配置改动重启生效，不做热加载）。
    # 传 None 时 handle_message 会自己按消息读——测试/裸调用能走，但服务里用的是这份。
    policy = wake if wake is not None else load_wake_policy()

    async def pump(adapter: AdapterBase) -> None:
        while True:
            message = await adapter.recv()
            # handle_message 内部已把所有失败收成返回值，不会抛；
            # 这里再兜一层，保证单条消息的意外绝不终止整个前台循环。
            try:
                await handle_message(
                    message, router=router, store=store, bot=bot, bus=bus,
                    wake=policy,
                )
            except Exception:
                logger.exception("处理消息时发生未预期的错误，已跳过这条")

    await asyncio.gather(*(pump(a) for a in adapters))


# ---------- 启动：组装依赖（§18.1 第 1~9 步） ----------


def _resolve_ws_port() -> int:
    """WebUI 端口：环境变量 > 配置 > 默认 8000。

    为什么环境变量优先：联调/冒烟时常常要换个端口避开占用（8000 是很抢手的
    端口，本机就撞过一次），命令行能覆盖就不必改配置文件。
    """
    env = os.environ.get("TALEAI_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            logger.warning("TALEAI_PORT 不是数字：%r，改用配置值", env)
    from core.config.loader import Config

    try:
        port = Config.load("platforms").get("web", {}).get("port")
        return int(port) if port else 8000
    except Exception:
        return 8000


def _qq_enabled() -> bool:
    """QQ 是否启用：platforms.json 的 qq.enabled，默认 false。

    默认关：QQ 要额外跑一个 SnowLuma 后端，没配的人不该多起一个端口、
    更不该因为连不上而报错。要用的自己打开。
    """
    try:
        from core.config.loader import Config

        return bool(Config.load("platforms").get("qq", {}).get("enabled", False))
    except Exception:
        return False


def _build(bus: EventBus | None = None):
    """按 §18.1 组装出 (store, router, bot, bus, adapters, stream, qq_service)。"""
    bus = bus if bus is not None else EventBus()
    store = SessionStore().open()  # 第 3 步：SQLite + WAL + 建表
    PluginRegistry().scan()  # 第 4 步：扫内置 + data/plugin 注册工具

    from core.adapter.web.adapter import WebAdapter

    # 实时日志流（§18.2 目录里订阅者为「日志」的那几条）：订阅总线，
    # 供 WebUI 的 /logs 页看「塔利在干什么」。日志镜像 handler 在 _serve 里挂。
    stream = LogStream()
    stream.subscribe_bus(bus)

    bot = ChatLLM(bus=bus)

    # 第 8 步：适配器名册 + Router
    registry = AdapterRegistry()
    # 注入读历史的回调：连上时补发历史，否则刷新页面后是空的（M0 验收⑤）。
    # 用回调而不是把 store 塞给适配器——适配器不该认识 SessionStore（§22 import 单向）。
    # clearer 同理：网页上「清空本次历史」要能删库，但适配器只认「(session_id)->删了几条」。
    # extra_routes 同理：设置读写要碰 Config，但适配器不该认识它——main 把
    # 「注册 /api/* 的业务路由」这件事当回调递进去（UX-01 的注入缝）。
    # QQ（M0-14）：**无条件构造**——构造只是把 FastAPI app 搭好，不联网、不起端口
    # （token 也在 start() 才读）。启停归 QQService：设置页的开关控制它，
    # 失败不再是"记一笔就没了"（页面黄色告示条 + 重试）。Router 先认识它：
    # 不在跑时没有连接、没有消息，注册本身无副作用。
    from core.adapter.qq.adapter import QQAdapter
    from core.adapter.qq.service import QQService

    qq_adapter = QQAdapter(bus=bus)
    registry.register(qq_adapter)

    async def _on_qq_message(message: Message) -> None:
        # router 在本函数后面才创建——闭包按引用解析，只有真收到 QQ 消息
        # （那时一切早已就位）才会被调用。
        await handle_message(
            message, router=router, store=store, bot=bot, bus=bus,
        )

    qq_service = QQService(adapter=qq_adapter, on_message=_on_qq_message)

    # 设置读写（UX-05）+ 图片上传（UX-07）+ QQ 开关（QQService）各造一个
    # 注册器，串起来一起挂。
    from core.config.api import build_platforms_routes, build_settings_routes
    from core.image_api import build_upload_routes

    def _control_plane(app) -> None:
        build_settings_routes()(app)
        build_upload_routes()(app)
        build_platforms_routes(qq_service)(app)

    web_adapter = WebAdapter(
        bus=bus, port=_resolve_ws_port(),
        # 历史帧要同时带 parts（#11 分条往返）与 images（#12 网页显示图），
        # 所以用 history_with_attachments（它一并给了这两样）。文本契约
        # history() 不变——那是喂模型的，两者别混。
        history_provider=store.history_with_attachments,
        clearer=store.clear, stream=stream,
        extra_routes=_control_plane,
    )
    registry.register(web_adapter)
    adapters = [web_adapter]  # QQ 不进这个列表——它的启停归 QQService

    router = Router(registry)

    return store, router, bot, bus, adapters, stream, qq_service


async def _serve() -> None:
    """默认跑法：起 WebSocket 服务（WebUI 聊天 + /logs 日志页）。"""
    store, router, bot, bus, adapters, stream, qq_service = _build()
    # 日志镜像：把 root 的每条日志也推到 /logs 页。**单独挂**而不是塞进
    # Logging.init()——init 的契约是「只加一个文件 handler」（test_log.py 钉着），
    # 破了它日志会重复落盘。服务退出时摘掉，保持干净。
    root = logging.getLogger()
    mirror = StreamLogHandler(stream)
    root.addHandler(mirror)
    # 起服务前先喊一嗓子：终端沉默是老毛病（服务模式本就不打印），
    # 这行 + /logs 页让"它到底起来没"一目了然。
    port = _resolve_ws_port()
    logger.info("塔利已就绪 → 聊天 http://127.0.0.1:%d/ · 日志 http://127.0.0.1:%d/logs",
                port, port)
    try:
        # QQ 的启停归 QQService（设置页开关）：按配置决定启动时试不试；
        # 失败只落"跳闸"态（页面告示 + 重试按钮），绝不拖垮 web。
        if _qq_enabled():
            st = await qq_service.start()
            logger.info("QQ 服务：state=%s %s", st["state"], st["detail"] or "（无详情）")
        else:
            logger.info("QQ 未启用（platforms.qq.enabled=false）——可在设置页打开")

        # 第 9 步：把适配器挂成常驻任务，然后前台循环等消息。
        # 每个 start() 各自兜异常（_safe_start）——某平台端口被占不该拖垮别的平台。
        wake = load_wake_policy()
        logger.info("唤醒策略：范围=%s 关键词=%s", wake.scope, list(wake.words) or "（无）")
        await asyncio.gather(
            *(_safe_start(a) for a in adapters),
            serve_forever(adapters, router=router, store=store, bot=bot, bus=bus,
                          wake=wake),
        )
    finally:
        root.removeHandler(mirror)
        # QQ 的关停先于 store：给它机会把在途的 serve 任务收干净。
        try:
            await qq_service.stop()
        except Exception:
            logger.exception("停止 QQ 服务时出错（不影响退出）")
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
    history: list[dict[str, str]] = store.history(CLI_SESSION_ID, with_parts=True)

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
                    parts=list(reply.messages),
                    tool_json={"calls": reply.tool_calls_made,
                               "stop_reason": reply.stop_reason} if reply.tool_calls_made else None,
                )
            except Exception as exc:
                reply = _close_turn_on_error(store, CLI_SESSION_ID, exc)
                print(f"\n  〔塔利这次没答上来：{exc}〕")
                history = store.history(CLI_SESSION_ID, with_parts=True)
                continue

            history = store.history(CLI_SESSION_ID, with_parts=True)

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
    # 第 1 步的一部分：人格文件不存在就从内置模板落一份到 data/config/，
    # 用户第一次跑就有得改（§13：人格是用户的域）。幂等，绝不覆盖已改过的。
    if ensure_user_persona():
        logger.info("已从内置模板生成 %s（要改人设就改它，改完重启）", USER_PERSONA_PATH)
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
