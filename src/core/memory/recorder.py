"""事件落盘接线（M1-02）。

record_event() helper：先落盘、后 publish。
守住"总线丢、落盘不丢"（§18.2）。

三个接线点：
  - message.received（适配器 _deliver）
  - message.sent（main handle_message）
  - session.closed（目前无实现，M1-05 后台会用）
"""

import logging
from typing import Any, Dict

from src.core.event_bus import EventBus
from src.core.memory.store import EventLog

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def record_event(
    bus: EventBus,
    eventlog: EventLog,
    event_type: str,
    session_id: str,
    owner: str,
    data: Dict[str, Any],
    ts: float | None = None,
) -> None:
    """先落盘、后广播（§18.2 硬规则）。

    Args:
        bus: 事件总线
        eventlog: EventLog 实例
        event_type: 事件类型（message.received/sent/session.closed）
        session_id: 会话 ID
        owner: 所有者（用户标识）
        data: 事件数据（各类型 schema 见 M1-02 文档）
        ts: 时间戳（可注入，默认由 eventlog 的 clock_fn 生成）

    设计约束：
      - 先落盘、后 publish：落盘失败不影响前台对话（记忆是增强，不是主链路）
      - 内部吞异常记日志：不能让落盘失败炸掉调用方
      - 总线丢了无所谓：事实源在 events.jsonl
    """
    try:
        # 1. 先落盘
        eventlog.append(
            type=event_type,
            session_id=session_id,
            owner=owner,
            data=data,
            ts=ts,
        )
    except Exception:
        # 落盘失败不能炸掉前台对话
        logger.exception(
            "事件落盘失败（已忽略）：type=%s session=%s owner=%s",
            event_type,
            session_id,
            owner,
        )
        # 继续往下走：总线照常发，订阅者能收到通知（虽然事实源没写上）

    # 2. 后 publish（§18.2）
    # 总线丢了无所谓——事实源在 events.jsonl，MemoryWorker 按游标重扫
    bus.publish(
        event_type,
        session_id=session_id,
        owner=owner,
        **data,  # 把 data 展开成 kwargs
    )
