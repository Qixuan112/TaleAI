"""测试：QQService —— 设置页"空气开关"背后的启停与状态机（PR1）。

全离线：FakeAdapter 假扮 QQAdapter（service 眼里它只有 start / serving /
stop / connected / recv 五件事），不需要真 uvicorn、真端口。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.adapter.qq.service import QQService


class FakeAdapter:
    """service 眼里的 QQAdapter：就这五个接口。"""

    def __init__(self, fail_with: BaseException | None = None):
        self.fail_with = fail_with
        self.serving_flag = False
        self.start_calls = 0
        self._stop_evt = asyncio.Event()
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def start(self):
        self.start_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.serving_flag = True
        await self._stop_evt.wait()  # 像真 uvicorn：一直服务到有人喊停

    def serving(self):
        return self.serving_flag

    def stop(self):
        self.serving_flag = False
        self._stop_evt.set()

    def connected(self):
        return self.serving_flag

    async def recv(self):
        return await self._inbox.get()


def _service(adapter, received=None):
    async def on_message(message):
        if received is not None:
            received.append(message)
    return QQService(adapter=adapter, on_message=on_message)


async def test_start_success_reaches_running():
    a = FakeAdapter()
    svc = _service(a)
    st = await svc.start()
    assert st["state"] == "running"
    assert st["connected"] is True
    assert a.start_calls == 1
    await svc.stop()


async def test_start_failure_reports_reason_not_raised():
    """SystemExit(3)（uvicorn 端口绑定失败的老路）→ failed + 可读的原因。"""
    a = FakeAdapter(fail_with=SystemExit(3))
    svc = _service(a)
    st = await svc.start()
    assert st["state"] == "failed"
    assert "端口可能被占用" in st["detail"]


async def test_oserror_failure_detail():
    a = FakeAdapter(fail_with=OSError("bind boom"))
    svc = _service(a)
    st = await svc.start()
    assert st["state"] == "failed"
    assert "bind boom" in st["detail"]


async def test_retry_after_failure_succeeds():
    """失败态下再 start（页面"重试"按钮）= 重新起一条 serve——端口释放后能自愈。"""
    a = FakeAdapter(fail_with=SystemExit(3))
    svc = _service(a)
    assert (await svc.start())["state"] == "failed"
    a.fail_with = None  # 端口释放了
    st = await svc.start()
    assert st["state"] == "running"
    assert a.start_calls == 2
    await svc.stop()


async def test_start_idempotent_when_running():
    a = FakeAdapter()
    svc = _service(a)
    await svc.start()
    st = await svc.start()
    assert st["state"] == "running"
    assert a.start_calls == 1  # 不会起出第二条
    await svc.stop()


async def test_concurrent_starts_only_one_serve_task():
    """连点/开关与重试同点：锁保证只起一条。"""
    a = FakeAdapter()
    svc = _service(a)
    st1, st2 = await asyncio.gather(svc.start(), svc.start())
    assert a.start_calls == 1
    assert st1["state"] == "running" and st2["state"] == "running"
    await svc.stop()


async def test_stop_idempotent_and_safe_before_start():
    a = FakeAdapter()
    svc = _service(a)
    assert (await svc.stop())["state"] == "disabled"  # 没启动就停：不抛
    await svc.start()
    assert (await svc.stop())["state"] == "disabled"
    assert (await svc.stop())["state"] == "disabled"


async def test_unexpected_exit_marks_failed():
    """非我们主动 stop 的退出（serve 自己返回）→ failed，别让状态停在 running。"""
    a = FakeAdapter()
    svc = _service(a)
    await svc.start()
    a._stop_evt.set()  # 模拟 serve 任务意外结束
    await asyncio.sleep(0.02)  # 让 done 回调跑
    assert svc.status()["state"] == "failed"


async def test_pump_feeds_messages_to_on_message():
    """常驻 pump：adapter 收到的消息喂给 on_message（与 serve_forever 的 pump 同形）。"""
    received = []
    a = FakeAdapter()
    svc = _service(a, received)
    await svc.start()
    a._inbox.put_nowait("m1")
    await asyncio.sleep(0.05)
    assert received == ["m1"]
    await svc.stop()
