"""内置插件：time_query。

设计文档 §23：M0 的有参工具，用来证明「参数传递 + 结果回喂」这条链，
以及演示 <msg> 和 FC 并存。零权限、不碰网络。

关于时区（实测出来的坑）：
Python 的 zoneinfo 在 Windows 上没有系统时区库，ZoneInfo("Asia/Shanghai")
直接抛 ZoneInfoNotFoundError。要支持全部 IANA 时区就得引入 tzdata 依赖。

此处**不引依赖**，改用固定偏移表，并只收录不实行夏令时的时区——
因为固定偏移对实行夏令时的地区（如 America/New_York）会有一半时间算错，
而给用户一个错的当地时间，比明确说"不支持"更坏。
表外输入一律返回错误，让模型转告用户，不做静默降级。

（ContextAssembler 已经把当前时间注入了 system_reminder，所以模型平时
不需要这个工具；它存在的意义是验证参数传递，不是"告诉模型几点了"。）
"""

from datetime import datetime, timedelta, timezone

from core.plugin.registry import register

# 只收不实行夏令时的时区：UTC 偏移常年不变，固定偏移才安全
_FIXED_OFFSET_ZONES = {
    "UTC": 0,
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Asia/Bangkok": 7,
    "Asia/Kolkata": 5.5,
    "Asia/Dubai": 4,
}

_WEEKDAYS = "一二三四五六日"

_SCHEMA = {
    "description": (
        "查询指定时区当前的日期、时间与星期。"
        "只有用户明确问到具体时刻/日期、且你无法从上下文判断时才调用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tz": {
                "type": "string",
                "description": (
                    "IANA 时区名，例如 Asia/Shanghai。默认 Asia/Shanghai。"
                    "本版本只支持不实行夏令时的时区。"
                ),
                "default": "Asia/Shanghai",
            }
        },
        "required": [],
    },
}


@register.tool(name="time_query", schema=_SCHEMA)
def time_query(tz: str = "Asia/Shanghai") -> str:
    """返回指定时区的当前时间；时区不支持时返回可读的错误说明。"""
    if tz not in _FIXED_OFFSET_ZONES:
        supported = "、".join(sorted(_FIXED_OFFSET_ZONES))
        return f"错误：暂不支持时区 {tz!r}（本版本只支持无夏令时的时区：{supported}）"

    hours = _FIXED_OFFSET_ZONES[tz]
    offset = timedelta(hours=hours)
    now = datetime.now(timezone(offset))

    sign = "+" if hours >= 0 else "-"
    whole = abs(hours)
    # 5.5 这种半小时时区要显示成 +05:30，不能截成 +05
    hh, mm = int(whole), int(round((whole - int(whole)) * 60))
    label = f"{sign}{hh:02d}:{mm:02d}"

    return (
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"{tz}（UTC{label}，星期{_WEEKDAYS[now.weekday()]}）"
    )
