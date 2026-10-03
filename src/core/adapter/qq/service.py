"""QQ 服务的运行时监督器：设置页"空气开关"背后的启停逻辑。

为什么需要它：QQAdapter.start() 失败（典型：8866 端口被占）以前只被
main._safe_start 记日志跳过——想再试只能重启整个进程。设置页要一个开关：
开=启动、关=停止、失败可重试（页面顶部黄色告示条 + 重试按钮）。

**两层状态，各管各的**：
- 用户意图 = platforms.json 的 qq.enabled（开关拨到哪边）；
- 运行态 = 本模块的状态机（disabled / starting / running / failed+原因）。
启动失败**不改写 enabled**：开关代表用户的意图，进程重启会再试一次
（端口临时被占能自愈）；"跳闸"只体现在运行态与页面告示上。

为什么不 import Config：适配器包保持对配置无感（§22 import 单向的延伸）。
配置的读写留在 main 与 config 路由侧，本模块只认三样东西——adapter、
on_message 回调、自己的状态机（鸭子接口，同 history_provider 那套约定）。
"""

import asyncio
import logging

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 就绪轮询：20ms × 100 ≈ 2 秒。uvicorn 绑定是毫秒级的事——这段时间里
#: 要么 started（就绪）、要么任务结束（失败）；超时是极罕见的慢机器情形。
_READY_POLL_INTERVAL = 0.02
_READY_POLL_TRIES = 100

#: stop 等待 serve 任务退出的上限；超时就取消（uvicorn 优雅退出很快，
#: 这里是兜底，防它卡住把关停流程拖住）。
_STOP_TIMEOUT = 5.0


class QQService:
    """QQAdapter 的启停与状态（供设置页开关调用）。

    并发保护：start/stop 走同一把锁——连点、开关与重试按钮同时点，
    只会串行执行，不会起出两条 serve 任务。
    """

    def __init__(self, adapter, on_message) -> None:
        # adapter：鸭子接口 start() / serving() / stop() / connected() / recv()
        # on_message：async def (Message) -> None，由 main 把 handle_message 包好
        self._adapter = adapter
        self._on_message = on_message
        self._task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._state = "disabled"  # disabled / starting / running / failed
        self._detail = ""
        self._stopping = False
        # serve 任务的失败原因（由 _run_adapter 吞下后存这里，见该方法注释）
        self._serve_exc: BaseException | None = None

    # ---------- 查询 ----------

    def status(self) -> dict:
        """状态快照（纯查询，不触发任何动作）。"""
        return {
            "state": self._state,
            "detail": self._detail,
            "connected": bool(self._adapter.connected()),
        }

    # ---------- 启停 ----------

    async def start(self) -> dict:
        """启动 QQ 服务；失败只落 failed 态，**绝不抛**。幂等：已在跑直接返回现状。"""
        async with self._lock:
            if self._task is not None and not self._task.done():
                return self.status()  # 已在跑（或正在启动）——不再起一条

            self._ensure_pump()
            self._state = "starting"
            self._detail = ""
            self._serve_exc = None
            self._task = asyncio.create_task(self._run_adapter(), name="qq-serve")
            self._task.add_done_callback(self._on_serve_done)

            # 轮询等就绪：要么 serving() 变真，要么任务结束（失败）。
            for _ in range(_READY_POLL_TRIES):
                if self._adapter.serving():
                    self._state = "running"
                    logger.info("QQ 服务已启动")
                    return self.status()
                if self._task.done():
                    self._state = "failed"
                    self._detail = self._failure_reason()
                    logger.warning("QQ 服务启动失败：%s", self._detail)
                    return self.status()
                await asyncio.sleep(_READY_POLL_INTERVAL)

            # 2 秒既没就绪也没退出（慢机器/极端负载）：保持 starting，
            # 页面刷新能看到后续结果——不武断判失败。
            logger.warning("QQ 服务 2s 内未就绪也未失败，保持 starting")
            return self.status()

    async def stop(self) -> dict:
        """停止 QQ 服务；幂等，绝不抛。"""
        async with self._lock:
            self._stopping = True
            try:
                if self._task is None or self._task.done():
                    self._state = "disabled"
                    self._detail = ""
                    return self.status()
                self._adapter.stop()  # 只置 should_exit，等待/兜底在这里做
                try:
                    await asyncio.wait_for(self._task, timeout=_STOP_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("QQ 服务 %ss 内没退出，强制取消", _STOP_TIMEOUT)
                    self._task.cancel()
                    try:
                        await self._task
                    except BaseException:
                        pass
                self._state = "disabled"
                self._detail = ""
                logger.info("QQ 服务已停止")
                return self.status()
            finally:
                self._stopping = False

    # ---------- 内部 ----------

    async def _run_adapter(self) -> None:
        """serve 任务体：把 adapter.start() 的异常收进任务结果。

        为什么包一层（而不是直接 create_task(adapter.start())）：
        SystemExit / KeyboardInterrupt 在 Task 里会被 asyncio**重新抛回事件
        循环**（Task.__step 的显式行为，3.8+）——uvicorn 端口绑定失败走的
        正是 sys.exit(3)，直接起任务的话整个进程被带走，"启动失败"根本轮不到
        状态机来收（实测踩到）。所以在这里吞住并把原因存下来；放行
        CancelledError / KeyboardInterrupt（"该停"的信号），同 main._safe_start。
        """
        try:
            await self._adapter.start()
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            self._serve_exc = exc

    def _ensure_pump(self) -> None:
        """起一条常驻收件循环（只起一次；不随 stop 取消）。

        为什么不随 stop 取消：停摆期间它只是阻塞在 recv() 上，无害；留着
        省掉"启停 × pump 生命周期"的一堆竞态（KISS）。镜像 serve_forever
        的单平台 pump：处理一条的意外绝不终止整条循环。
        """
        if self._pump_task is not None and not self._pump_task.done():
            return

        async def pump() -> None:
            while True:
                message = await self._adapter.recv()
                try:
                    await self._on_message(message)
                except Exception:
                    logger.exception("处理 QQ 消息时发生未预期的错误，已跳过这条")

        self._pump_task = asyncio.create_task(pump(), name="qq-pump")

    def _on_serve_done(self, task: asyncio.Task) -> None:
        """serve 任务结束的回调：把**非我们主动 stop** 的退出标成 failed
        （比如运行中途崩了）。异常原因已由 _run_adapter 存进 _serve_exc。"""
        if task.cancelled() or self._stopping:
            return
        self._detail = self._failure_reason()
        self._state = "failed"
        logger.warning("QQ 服务意外退出：%s", self._detail)

    def _failure_reason(self) -> str:
        """把退出原因翻译成给人看的一句话（页面告示条直接用）。"""
        if self._serve_exc is not None:
            return _reason_from(self._serve_exc)
        return "服务提前退出（详情见日志页）"


def _reason_from(exc: BaseException) -> str:
    """异常 → 页面告示用的一句话（与异常类型无关的兜底也在）。"""
    if isinstance(exc, SystemExit):
        # uvicorn 端口绑定失败走 sys.exit(3)（见 main._safe_start 的注释）
        return f"端口可能被占用（uvicorn 退出码 {exc.code}），详情见日志页"
    if isinstance(exc, OSError):
        return f"启动失败：{exc}"
    return f"启动失败：{type(exc).__name__}: {exc}"
