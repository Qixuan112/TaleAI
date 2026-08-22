"""ChatLLM：把装配好的上下文发给 LLM，拿回回复。

这是 M0-03 的第二半（正式版）：
- Persona 负责"人格 system 提示词"（M0-03 第一半，已实现）
- ChatLLM 负责"装配上下文 + 发送请求 + 拿回复"
- 入口：ChatLLM.chat(user_question) -> 初念的回复文本

用法（配合 main.py）：
    from core.llm.chat_llm import ChatLLM
    bot = ChatLLM()
    print(bot.chat("你好"))
"""

import asyncio
from typing import Any

from openai import AsyncOpenAI

from core.config.loader import Config
from core.llm.persona_llm.base import Persona


class ChatLLM:
    """对话机器人：组装上下文并调用真实 LLM。"""

    def __init__(self) -> None:
        # 读配置（服务商 / 模型 / key）
        cfg = Config.load("config")
        secrets = Config.load("secrets")
        llm = cfg.get("llm", {})
        self.base_url: str = llm.get("base_url")
        self.model: str = llm.get("model")
        self.api_key: str = secrets.get("llm", {}).get("api_key", "")

        # 人格（M0-03 第一半：初念）
        self.persona = Persona()
        # 异步客户端（openai 3.x 是异步优先）
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)

    def assemble_messages(self, user_question: str) -> list[dict[str, str]]:
        """装配：稳定前缀（人格 system）+ 会动尾巴（最新提问）。

        这就是你在 demo_context.py 里亲手写的那套思路：
        把稳定的放前面当前缀，把会动的放最后面。
        """
        return [
            {"role": "system", "content": self.persona.build_system_prompt()},  # 稳定 → 前缀
            {"role": "user", "content": user_question},                       # 会动 → 尾巴
        ]

    async def _request(self, messages: list[dict[str, str]]) -> str:
        """真正发请求（openai 3.x Responses API）。"""
        resp = await self.client.responses.create(
            model=self.model,
            input=messages,
        )
        return resp.output_text if hasattr(resp, "output_text") else str(resp)

    def chat(self, user_question: str) -> str:
        """同步入口：输入一句话，返回初念的回复。"""
        messages = self.assemble_messages(user_question)
        return asyncio.run(self._request(messages))