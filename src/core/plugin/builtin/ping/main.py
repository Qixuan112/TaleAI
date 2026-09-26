"""内置插件：ping。

设计文档 §23：M0 的冒烟工具。无参、零权限、不碰网络。

两个用途：
1. 证明 FC 链路通（模型能调出一个工具并拿到结果）；
2. 测循环检测——同一个无参工具连续被调时，正好触发「同工具同参数 ×2
   即熔断」（§19-2/3）。
"""

from core.plugin.registry import register

_SCHEMA = {
    "description": (
        "连通性测试工具，调用后返回 pong。"
        "仅当用户明确要求测试连通性（例如「测试一下」「ping 一下」）时使用，"
        "其他任何情况都不要调用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


@register.tool(name="ping", schema=_SCHEMA)
def ping() -> str:
    """返回固定字符串 pong。"""
    return "pong"
