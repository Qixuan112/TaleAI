"""TaleAI 入口：点它就能跟初念聊上一句。"""

import logging
import sys
from pathlib import Path

# 让 main.py 能找到 src/ 下的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from core.llm.chat_llm import ChatLLM
from core.log import Logging
from core.plugin.registry import PluginRegistry
from core.session.store import SessionStore

logger = logging.getLogger(__name__)

# 命令行会话固定在同一个 session_id 下——重启进程后读到的是同一条历史，
# 这就是 M0 验收标准⑤「重启后历史完整」的兑现（§二十一）
SESSION_ID = "cli:local"

# 本轮失败时的占位回复。落库只为保住「1 回合 = 2 行」，内容是次要的。
_FAILED_TURN_PLACEHOLDER = "……（初念这边出了点问题，你再试一次？）"


def _close_turn_on_error(store: SessionStore, exc: Exception) -> None:
    """对话轮失败时补一条占位 assistant 行，把这一回合收尾。

    用户消息已经先落库了（收即存），若这轮就此中断，历史里会留下一条
    单挂的 user 行——而弹簧窗口按「1 回合 = 2 行」计数（§18.1），
    单挂会让回合计数错位。所以失败也要把这一路补平。
    """
    logger.exception("对话轮失败：%s", exc)
    try:
        store.append(SESSION_ID, "assistant", _FAILED_TURN_PLACEHOLDER)
    except Exception:
        # 连占位都写不进去（多半是库/磁盘坏了）——记日志就好，
        # 绝不能在这里再炸一次，否则用户看到的是堆栈而非提示
        logger.exception("补写占位 assistant 也失败")


def main() -> None:
    Logging.init()  # 启动第 2 步：日志落 data/logs/，按天轮转
    store = SessionStore().open()  # 启动第 3 步：SQLite + WAL + 建表
    PluginRegistry().scan()  # 启动第 4 步：扫内置 + data/plugin 注册工具
    bot = ChatLLM()

    store.ensure_session(SESSION_ID)
    history: list[dict[str, str]] = store.history(SESSION_ID)

    if history:
        print(f"===== 初念记得你之前来过（已恢复 {len(history)} 条历史）=====\n")
    else:
        print("===== 初念在等你聊天（输入 退出/quit 结束） =====\n")

    try:
        while True:
            user_input = input("你: ").strip()
            if not user_input:
                continue  # 空行（直接回车）不发请求，重新等待输入
            if user_input.lower() in {"退出", "quit", "exit", "q"}:
                print("\n初念: 那我们就聊到这吧~ 下次见！")
                break

            # 收即存（§18.1 第 4 步）：用户消息**先落库**，崩溃不丢。
            # 落库失败就没必要往下走——这轮干脆不发请求，免得用户以为发出去了。
            try:
                store.append(SESSION_ID, "user", user_input)
            except Exception as exc:
                logger.exception("用户消息落库失败")
                print(f"\n  〔这条消息没存下来，这轮跳过：{exc}〕")
                continue

            # 带上**落库前**的 history 去问初念（history 是上一轮末尾刷新的）。
            # 用户消息已进库，若在这里再取一次会把它读回来，装配时就重复发一遍
            # （装配 = history + 最新提问）。所以传旧的 history。
            try:
                reply = bot.chat(user_input, history, session_id=SESSION_ID)
                # assistant 真实消息落库；tool_json 一并存（§18.1 第 7 步）
                store.append(
                    SESSION_ID,
                    "assistant",
                    "\n\n".join(reply.messages),
                    tool_json={"calls": reply.tool_calls_made,
                               "stop_reason": reply.stop_reason} if reply.tool_calls_made else None,
                )
            except Exception as exc:
                # 对话或落库失败：补平这一回合，别让 user 行单挂，然后继续下一轮
                _close_turn_on_error(store, exc)
                print(f"\n  〔初念这次没答上来：{exc}〕")
                history = store.history(SESSION_ID)
                continue

            # 让内存里的 history 跟库里保持一致，供下一轮装配用
            history = store.history(SESSION_ID)

            for message in reply.messages:
                print(f"\n初念: {message}")

            # 工具调用是内部动作，但 M0 的验收要求它"可观测"，所以显式打出来
            if reply.tool_calls_made:
                print(f"\n  〔调用了 {reply.tool_calls_made} 次工具，结束原因：{reply.stop_reason}〕")
    finally:
        store.close()


if __name__ == "__main__":
    main()

