"""M0-01 补测：Logging.init() 让日志落盘并按天轮转。

设计文档 §18.1 启动第 2 步写的就是这件事（M0-01 声称完成，实际没做）。

测试全部用 tmp_path 的临时目录，不碰真的 data/logs/。
"""

import logging
import os
import sys
from pathlib import Path

# 让测试能找到 src 下的代码（与 test_config.py 保持一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.log import Logging


@pytest.fixture(autouse=True)
def clean_logging():
    """每个用例前后都复位，避免 handler 叠加污染其他测试。"""
    Logging.reset()
    yield
    Logging.reset()


def test_log_dir_created(tmp_path):
    """init 会把日志目录建出来（不存在则创建）。"""
    target = tmp_path / "logs"
    assert not target.exists()
    Logging.init(log_dir=target)
    assert target.is_dir()


def test_log_written_to_file(tmp_path):
    """写一条日志，落盘到 taleai.log。"""
    Logging.init(log_dir=tmp_path)

    logging.getLogger("core.test").warning("测试消息")
    for h in logging.getLogger().handlers:
        h.flush()

    log_file = tmp_path / "taleai.log"
    assert log_file.exists()
    assert "测试消息" in log_file.read_text(encoding="utf-8")


def test_chinese_not_mangled(tmp_path):
    """中文日志必须能正确读回——不显式指定 encoding 时 Windows 上按 gbk 写坏。"""
    Logging.init(log_dir=tmp_path)
    logging.getLogger("core.test").info("塔利说了：今天心情不错~")
    for h in logging.getLogger().handlers:
        h.flush()

    text = (tmp_path / "taleai.log").read_text(encoding="utf-8")
    assert "塔利说了：今天心情不错~" in text


def test_rotation_configured(tmp_path):
    """handler 是按天轮转的，且保留天数有上限（§19-16）。"""
    from logging.handlers import TimedRotatingFileHandler

    Logging.init(log_dir=tmp_path)
    handlers = [h for h in logging.getLogger().handlers
                if isinstance(h, TimedRotatingFileHandler)]
    assert len(handlers) == 1
    assert handlers[0].when == "MIDNIGHT"
    assert handlers[0].backupCount == 7


def test_no_console_handler(tmp_path):
    """不往控制台加 handler——控制台是给人看对话的，掺日志会污染聊天输出。

    只看 init 新增的 handler：pytest 自己会装捕获用的 file handler，
    那不是我们的，不该被算进来。
    """
    root = logging.getLogger()
    before = set(root.handlers)

    Logging.init(log_dir=tmp_path)

    added = [h for h in root.handlers if h not in before]
    assert len(added) == 1, f"init 应该只加一个 handler，实际加了 {len(added)} 个"
    # 新增的这个必须是文件轮转 handler，不能是控制台 StreamHandler
    from logging.handlers import TimedRotatingFileHandler

    assert isinstance(added[0], TimedRotatingFileHandler)
    assert not isinstance(added[0], logging.StreamHandler) or isinstance(
        added[0], TimedRotatingFileHandler
    )


def test_init_is_idempotent(tmp_path):
    """重复 init 不叠加 handler（否则日志会成倍重复）。"""
    Logging.init(log_dir=tmp_path)
    Logging.init(log_dir=tmp_path)
    Logging.init(log_dir=tmp_path)

    from logging.handlers import TimedRotatingFileHandler

    n = sum(1 for h in logging.getLogger().handlers
            if isinstance(h, TimedRotatingFileHandler))
    assert n == 1


def test_init_writes_only_once(tmp_path):
    """重复 init 后写一条日志，文件里只出现一次。"""
    Logging.init(log_dir=tmp_path)
    Logging.init(log_dir=tmp_path)
    logging.getLogger("core.test").warning("只该出现一次")
    for h in logging.getLogger().handlers:
        h.flush()

    text = (tmp_path / "taleai.log").read_text(encoding="utf-8")
    assert text.count("只该出现一次") == 1


def test_reset_allows_reinit(tmp_path):
    """reset 之后可以重新 init（测试隔离依赖这个）。"""
    Logging.init(log_dir=tmp_path / "a")
    Logging.reset()
    Logging.init(log_dir=tmp_path / "b")
    assert (tmp_path / "b").is_dir()


def test_noisy_third_party_loggers_quieted(tmp_path):
    """第三方库降到 WARNING：它们每个请求记一条 INFO，会把我们的日志淹掉。"""
    Logging.init(log_dir=tmp_path)
    assert logging.getLogger("httpx2").level == logging.WARNING
    assert logging.getLogger("openai").level == logging.WARNING
    # 降级不是静音——真出问题时（WARNING 及以上）仍要能看见
    assert logging.getLogger("httpx2").isEnabledFor(logging.WARNING)
    assert logging.getLogger("httpx2").isEnabledFor(logging.ERROR)


def test_our_own_logger_not_quieted(tmp_path):
    """只压第三方库；我们自己的 logger 照常记 INFO。"""
    Logging.init(log_dir=tmp_path)
    our = logging.getLogger("core.llm.chat_llm")
    our.info("我们的 INFO 不该被压掉")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "我们的 INFO 不该被压掉" in (tmp_path / "taleai.log").read_text(encoding="utf-8")


def test_reset_restores_noisy_loggers(tmp_path):
    """reset 要撤销降级，否则全局 logger 状态会污染后续用例。"""
    Logging.init(log_dir=tmp_path)
    Logging.reset()
    assert logging.getLogger("httpx2").level == logging.NOTSET


def test_accepts_str_path(tmp_path):
    """传字符串路径也要能用——调用方很自然会这么调。"""
    Logging.init(log_dir=str(tmp_path / "logs"))
    assert (tmp_path / "logs").is_dir()


def test_does_not_shadow_stdlib():
    """文件叫 log.py 而非 logging.py——确保标准库 logging 没被遮蔽。"""
    import logging as stdlib_logging

    assert "core" not in str(getattr(stdlib_logging, "__file__", "") or ""), (
        "标准库 logging 被同名的项目模块遮蔽了"
    )
    assert Path(stdlib_logging.__file__).stem in {"__init__", "logging"}
