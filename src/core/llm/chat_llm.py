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
        # base_url / model 必须存在（来自 config.json 或默认值），否则抛错更早暴露配置问题
        self.base_url: str = llm["base_url"]
        self.model: str = llm["model"]
        self.api_key: str = secrets.get("llm", {}).get("api_key", "")

        # 人格（M0-03 第一半：初念）
        self.persona = Persona()
        # 历史裁剪策略：弹簧窗口（默认以回合为单位）
        # 可通过 data/config/config.json 的 llm 字段配置：
        #   "history_max_turns": 40, "history_trim_to": 10
        self.history_max_turns: int = int(llm.get("history_max_turns", 40))
        self.history_trim_to: int = int(llm.get("history_trim_to", 10))
        # 异步客户端（openai 3.x 是异步优先）
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)

    def assemble_messages(
        self, user_question: str, history: list[dict[str, str]] | None = None
    ) -> list[dict[str, str]]:
        """装配：稳定前缀（人格 system）+ 会动尾巴（历史 + 最新提问）。

        这就是你在 demo_context.py 里亲手写的那套思路：
        把稳定的放前面当前缀，把会动的放最后面。
        历史对话 = 会动尾巴的一部分（user/assistant 交替，原样追加，不改身体）。
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.persona.build_system_prompt()},  # 稳定 → 前缀
        ]

        # 如果提供了历史，先按弹簧窗口策略裁剪（只有超限才裁）
        if history:
            trimmed = self._trim_history_if_needed(history)
            messages.extend(trimmed)

        # 最新提问放在最末尾（会动部分）
        messages.append({"role": "user", "content": user_question})
        return messages

    def _trim_history_if_needed(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """弹簧窗口策略：以回合（user+assistant）为单位计数。

        - history 是消息列表（交替的 user/assistant）。
        - 计算回合数 = ceil(len(history) / 2).
        - 若回合数 <= history_max_turns: 不裁剪，返回原样。
        - 若回合数 > history_max_turns: 裁剪到最后 `history_trim_to` 回合并返回。

        备注：裁剪会改动前缀，但只在超限时发生——符合弹簧窗口策略。
        """
        if not history:
            return history

        # 以回合为单位计算（每回合约两条消息 user+assistant）
        total_msgs = len(history)
        total_turns = (total_msgs + 1) // 2

        if total_turns <= self.history_max_turns:
            return history

        # 需要裁剪：保留最后 N 回合
        keep_turns = max(1, min(self.history_trim_to, total_turns))
        # 每回合近似 2 条消息；保留消息数 = keep_turns * 2
        keep_msgs = keep_turns * 2

        # 从尾部切割，确保以完整消息为单位
        return history[-keep_msgs:]

    async def _request(self, messages: list[dict[str, str]]) -> str:
        """真正发请求（/chat/completions）。

        为什么不用 responses.create：本项目的 provider 是 OpenAI **兼容**网关
        （见 config.json 的 base_url），这类服务只实现 /chat/completions，
        没有 OpenAI 私有的 /responses——实测打到 /responses 会 404。
        messages 的 {role, content} 是业界事实标准，兼容面最宽。
        """
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
        )
        return resp.choices[0].message.content or ""

    def chat(self, user_question: str, history: list[dict[str, str]] | None = None) -> str:
        """同步入口：输入一句话（可带历史），返回初念的回复。"""
        messages = self.assemble_messages(user_question, history)
        return asyncio.run(self._request(messages))
