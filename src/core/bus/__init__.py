"""事件总线：进程内 pub/sub（旁路通知，不存状态）。

M0-10 落 `EventBus` 与 `Event`。事件目录（message.received / message.sent /
tool.called / session.closed / memory.extracted / plan.fired，§18.2）由生产者
从 M0-11 起逐个接入——总线本身不认识具体事件名，只认字符串。
"""

from core.bus.event_bus import Event, EventBus

__all__ = ["Event", "EventBus"]
