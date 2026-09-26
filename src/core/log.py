"""日志初始化：让全项目的日志有正经去处（设计文档 §18.1 启动第 2 步）。

落在 data/logs/，按天轮转（§19-16）。纯标准库、零第三方依赖。

为什么文件叫 log.py 而不是 logging.py：
§22 命名约定要求"文件 snake_case ↔ 类 PascalCase 一一对应"，按此规则
该叫 logging.py——但那会遮蔽标准库：只要 core/ 被放进 sys.path（例如
`python src/core/logging.py` 直跑调试），`import logging` 就会拿到本文件，
把自己套进无限递归。实测确认过。改用 log.py 规避，代价是轻微偏离命名表。

只写文件、不加控制台 handler：控制台是给人看对话的，往里掺日志会污染
聊天输出（此前 NullHandler 修的就是这个）。要看日志请 tail data/logs/。
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# log.py 位于 src/core/ → parents[2] 是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 默认日志目录：项目根下的 data/logs/
DEFAULT_LOG_DIR = PROJECT_ROOT / "data" / "logs"

# 保留天数：轮转后旧文件保留 7 天，再旧自动删除
_BACKUP_DAYS = 7

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 第三方库调噪：把 root 设成 INFO 后，它们每个请求都记一条（实测 httpx2 会
# 把每次 /chat/completions 调用写进日志），我们自己的日志会被淹掉。
# 降到 WARNING 而非静默（DISABLE）：网络/协议真出问题时仍能看见。
# 注意 logger 名是 httpx2 / httpcore2——本项目的 SDK 把 httpx 内置改名了，
# 只屏蔽 httpx 拦不住。
_NOISY_LOGGERS = ("httpx2", "httpcore2", "openai", "asyncio")
_NOISY_LEVEL = logging.WARNING


class Logging:
    """日志管家。init() 在 main 启动第 2 步调用一次。"""

    _initialized = False

    @classmethod
    def init(cls, log_dir: Path | None = None, level: int = logging.INFO) -> None:
        """配置 root logger：写 data/logs/taleai.log，按天轮转。

        - 幂等：重复调用不会叠加 handler（否则日志会成倍重复）。
        - encoding="utf-8" 是必须的：中文 Windows 默认 gbk，中文日志会写坏。
        - 只写文件、不加控制台 handler：控制台是给人看对话的，掺日志会污染输出。
        """
        if cls._initialized:
            return

        directory = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)

        handler = TimedRotatingFileHandler(
            directory / "taleai.log",
            when="midnight",  # 每天零点切一个文件
            backupCount=_BACKUP_DAYS,
            encoding="utf-8",  # 中文日志必须显式指定，否则 Windows 上按 gbk 写坏
            delay=True,  # 首次写日志时才真正打开文件
        )
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(handler)

        # 压低第三方库的噪音（我们自己的 logger 不受影响）
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(_NOISY_LEVEL)

        cls._initialized = True

    @classmethod
    def reset(cls) -> None:
        """移除本模块加过的 handler、撤销噪音降级，回到未初始化状态（测试用）。"""
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, TimedRotatingFileHandler):
                root.removeHandler(h)
                h.close()
        # 撤销第三方库的降级：logger 是全局单例，不还原会污染后续用例
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
        cls._initialized = False
