"""TaleAI 入口：点它就能跟初念聊上一句。"""

import sys
from pathlib import Path

# 让 main.py 能找到 src/ 下的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from core.llm.chat_llm import ChatLLM


def main() -> None:
    bot = ChatLLM()
    reply = bot.chat("你好呀，今天想跟我说点什么？")
    print("\n===== 初念的回复 =====\n")
    print(reply)


if __name__ == "__main__":
    main()
