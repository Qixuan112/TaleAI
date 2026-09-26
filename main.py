"""TaleAI 入口：点它就能跟初念聊上一句。"""

import sys
from pathlib import Path

# 让 main.py 能找到 src/ 下的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from core.llm.chat_llm import ChatLLM
from core.log import Logging
from core.plugin.registry import PluginRegistry


def main() -> None:
    Logging.init()  # 启动第 2 步：日志落 data/logs/，按天轮转
    PluginRegistry().scan()  # 启动第 4 步：扫内置 + data/plugin 注册工具
    bot = ChatLLM()
    history: list[dict[str, str]] = []  # 聊天记忆（role + content）

    print("===== 初念在等你聊天（输入 退出/quit 结束） =====\n")

    while True:
        user_input = input("你: ").strip()
        if not user_input:
            continue  # 空行（直接回车）不发请求，重新等待输入
        if user_input.lower() in {"退出", "quit", "exit", "q"}:
            print("\n初念: 那我们就聊到这吧~ 下次见！")
            break

        # 带上历史去问初念（稳定前缀 + 会动尾巴）
        reply = bot.chat(user_input, history)

        # 把这一轮记进记忆，下次它还记得
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": reply})

        print(f"\n初念: {reply}\n")


if __name__ == "__main__":
    main()
