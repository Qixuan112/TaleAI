"""QQ 适配器：SnowLuma / OneBot 11 反向 WebSocket。

- `protocol.py`  OneBot 报文的解析与构造（纯函数，可脱网测试）
- `adapter.py`   QQAdapter：反向 WS 端点 + 收发

不搬运早期 Tale 的 QQ 实现——那份代码没有测试、接口是旧 BaseAdapter。
按当前契约（AdapterBase / Message）重写，逻辑与控制流一致。
"""

from core.adapter.qq.adapter import QQAdapter

__all__ = ["QQAdapter"]
