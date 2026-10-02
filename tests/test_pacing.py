"""测试：分条发送的真人打字节奏（src/core/adapter/pacing.py）。

节奏是个纯函数——正是为了能这样直接测：不碰时钟、不碰 WS，
把「停多久」当成一个可断言的数。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.adapter import pacing


def test_longer_text_waits_longer():
    """核心性质：下一条越长，停得越久（这是"像真人"的关键）。

    取多次采样比中位数，避开随机抖动的干扰。
    """
    short = sorted(pacing.typing_delay("嗯") for _ in range(50))[25]
    long = sorted(pacing.typing_delay("我看看你今天写的那个东西啊") for _ in range(50))[25]
    assert long > short


def test_delay_never_below_minimum():
    """空串 / 极短也要有下限——否则多条几乎同时到，客户端会合并。"""
    for _ in range(100):
        assert pacing.typing_delay("") >= pacing.MIN_DELAY_SEC
        assert pacing.typing_delay("嗯") >= pacing.MIN_DELAY_SEC


def test_delay_never_above_maximum():
    """超长文本也不能停到几十秒——那是卡死不是打字。"""
    for _ in range(100):
        assert pacing.typing_delay("字" * 5000) <= pacing.MAX_DELAY_SEC


def test_jitter_makes_delays_vary():
    """同样的文本，多次结果不应完全相同——真人不会每次都敲一样快。"""
    delays = {pacing.typing_delay("你好呀朋友们") for _ in range(50)}
    assert len(delays) > 1


def test_no_jitter_would_make_delay_equal_to_formula():
    """把抖动关掉（上限 0）时，延迟应正好落在公式值上——验证公式本身。"""
    import random

    orig = pacing.JITTER_SEC
    pacing.JITTER_SEC = 0.0
    try:
        random.seed(0)  # 即使有 uniform 调用也确定
        d = pacing.typing_delay("你好呀朋友们")  # 6 字 → 6 * 0.22 = 1.32
        assert abs(d - 6 * pacing.PER_CHAR_SEC) < 1e-9
    finally:
        pacing.JITTER_SEC = orig


def test_none_text_is_treated_as_empty():
    """容错：None 不该炸，按空串处理（下限）。"""
    assert pacing.typing_delay(None) >= pacing.MIN_DELAY_SEC  # type: ignore[arg-type]
