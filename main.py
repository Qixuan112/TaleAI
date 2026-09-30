"""TaleAI 入口：点它就能跟初念聊上一句。"""

import sys
from pathlib import Path

# 让 main.py 能找到 src/ 下的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from core.llm.chat_llm import ChatLLM
from core.log import Logging
from core.plugin.registry import PluginRegistry
from core.session.store import SessionStore

# 命令行会话固定在同一个 session_id 下——重启进程后读到的是同一条历史，
# 这就是 M0 验收标准⑤「重启后历史完整」的兑现（§二十一）
SESSION_ID = "cli:local"


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

            # 带上历史去问初念（稳定前缀 + 会动尾巴），可能触发若干轮工具调用
            reply = bot.chat(user_input, history, session_id=SESSION_ID)

            # 收即存（§18.1 第 4/7 步）：两边都落库，崩溃不丢。
            # 存的是**解析后**的话（不含 <msg> 标签）——它是这轮对话真实的内容。
            store.append(SESSION_ID, "user", user_input)
            store.append(
                SESSION_ID,
                "assistant",
                "\n\n".join(reply.messages),
                tool_json={"calls": reply.tool_calls_made,
                           "stop_reason": reply.stop_reason} if reply.tool_calls_made else None,
            )

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
