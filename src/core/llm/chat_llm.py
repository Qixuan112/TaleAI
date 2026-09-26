"""ChatLLM：把装配好的上下文发给 LLM，拿回回复。

这是 M0-03 的第二半（正式版）：
- Persona 负责"人格 system 提示词"（M0-03 第一半，已实现）
- ChatLLM 负责"装配上下文 + 发送请求 + 拿回复"
- XmlParser 负责把 <msg> 标签摘掉（M0-04）
- 入口：ChatLLM.chat(user_question) -> 解析后给用户看的文本

用法（配合 main.py）：
    from core.llm.chat_llm import ChatLLM
    bot = ChatLLM()
    print(bot.chat("你好"))
"""

import asyncio
import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

from core.adapter.base import Reply
from core.config.loader import Config
from core.executor import ToolCall, ToolExecutor
from core.llm.context import ContextAssembler
from core.llm.persona_llm.base import Persona
from core.plugin.registry import PluginRegistry
from core.xml_parser import ParsedOutput, XmlParser

logger = logging.getLogger(__name__)
# 库不该在应用没配日志时往 stderr 喷东西（Python 的 lastResort 会兜底打印，
# 结果内部 warning 直接糊到用户脸上——实测过）。NullHandler 让它静默；
# main 启动时调 Logging.init() 配好 root logger 后，照常经 propagate 输出到文件。
logger.addHandler(logging.NullHandler())

# 本模块在 src/core/llm/ 下，prompts/ 是它的同级目录
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_fallback_text() -> str:
    """读兜底文案（§19-2：没有 <msg> 时发它）。文件缺失也不该让对话崩掉。"""
    path = _PROMPTS_DIR / "fallback.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("读不到兜底文案 %s，用内置默认", path)
        return "……（初念好像卡了一下，你再说一遍？）"


def _parse_arguments(raw: str) -> tuple[dict, bool]:
    """把 FC 传回的参数字符串解析成 dict。返回 (参数, 是否解析成功)。

    模型给出非法 JSON 是真实会发生的（小模型尤其），所以"是否成功"
    要单独返回——静默当成空参数会让错误变得难以追查。
    """
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}, False
    if not isinstance(value, dict):
        return {}, False
    return value, True


def _canonical_signature(raw: str) -> str:
    """参数的规范化写法，用于判断"同工具同参数"。

    模型每次给的 JSON 字符串可能空格/键序不同，直接比字符串会漏判，
    所以解析后按键排序重新序列化。
    """
    args, ok = _parse_arguments(raw)
    if not ok:
        return raw or ""
    return json.dumps(args, sort_keys=True, ensure_ascii=False)


class ChatLLM:
    """对话机器人：组装上下文并调用真实 LLM。"""

    def __init__(self) -> None:
        # 读配置（服务商 / 模型 / key）
        cfg = Config.load("config")
        secrets = Config.load("secrets")
        llm = cfg.get("llm", {})
        # base_url / model 是用户配置（默认值为空），缺了就没法工作。
        # 这里主动校验并给出可操作的提示，而不是等 provider 返回难懂的 403。
        self.base_url: str = llm.get("base_url", "")
        self.model: str = llm.get("model", "")
        if not self.base_url or not self.model:
            raise ValueError(
                "llm.base_url / llm.model 未配置。请在 data/config/config.json 里填写"
                " 服务商网关地址（以 /v1 结尾）与模型名。"
            )
        self.api_key: str = secrets.get("llm", {}).get("api_key", "")

        # 人格（M0-03 第一半：初念）
        self.persona = Persona()
        # 输出解析（M0-04）：把 <msg> 标签从模型输出里摘出来
        self.parser = XmlParser()
        # 历史裁剪策略：弹簧窗口（默认以回合为单位）
        # 可通过 data/config/config.json 的 llm 字段配置：
        #   "history_max_turns": 40, "history_trim_to": 10
        self.history_max_turns: int = int(llm.get("history_max_turns", 40))
        self.history_trim_to: int = int(llm.get("history_trim_to", 10))
        # FC 循环上限（§19-2：全局 3 轮不变；配置只是让它可调，不是让它可变大）
        self.max_agent_steps: int = int(llm.get("max_agent_steps", 3))
        # 异步客户端（openai 3.x 是异步优先）
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        # 上下文装配（M0-03 欠的那半）：动态块由它准备，本类不自己拼（§18.5 硬规则 6）
        self.context = ContextAssembler()
        # 工具链（M0-05/06/07）：注册表提供工具表，执行器负责跑
        self.registry = PluginRegistry()
        self.executor = ToolExecutor(self.registry)
        # 兜底文案（§19-2：模型什么都没说时发它；写在 prompts 里可改）
        self.fallback_text = _load_fallback_text()

    def assemble_messages(
        self, user_question: str, history: list[dict[str, str]] | None = None
    ) -> list[dict[str, str]]:
        """装配：稳定前缀（人格 system）+ 会动尾巴（历史 + 最新提问）。

        动态块（M0 只有环境块=当前时间）拼到**最新 user 消息头部**，
        用 <system_reminder> 包裹（§十二）。

        关键：reminder 只进本次请求，**不写回 history**（persist=False）。
        否则每轮都往历史里塞一份时间戳，上下文会越滚越大。
        history 是调用方的 list，这里绝不改它。
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.persona.build_system_prompt()},  # 稳定 → 前缀
        ]

        # 如果提供了历史，先按弹簧窗口策略裁剪（只有超限才裁）
        if history:
            trimmed = self._trim_history_if_needed(history)
            messages.extend(trimmed)

        # 动态块包在 <system_reminder> 里，贴在最新提问前面
        reminder = self.context.render_reminder(self.context.assemble("chat"))
        content = f"{reminder}\n{user_question}" if reminder else user_question

        # 最新提问放在最末尾（会动部分）
        messages.append({"role": "user", "content": content})
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

    async def _request(self, messages: list, tools: list[dict] | None = None):
        """发一次请求，返回 assistant message（可能带 tool_calls）。

        为什么不用 responses.create：本项目的 provider 是 OpenAI **兼容**网关
        （见 config.json 的 base_url），这类服务只实现 /chat/completions，
        没有 OpenAI 私有的 /responses——实测打到 /responses 会 404。

        没有工具时不传 tools 参数：空的工具表在某些网关上会被判为非法请求。
        """
        kwargs: dict = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        resp = await self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message

    def _collect_messages(self, content: str) -> list[str]:
        """解析一轮的模型输出，返回它要说给用户的话（已摘掉 <msg> 标签）。

        兜底时记 warning：模型没按契约输出是可观测事件，不该悄悄放过。
        但用户仍能看到原文——解析失败不能让他收到空白（§19-1）。
        """
        parsed: ParsedOutput = self.parser.parse(content)
        if parsed.is_fallback:
            logger.warning("模型输出未含 <msg>，按纯文本兜底：%r", content[:120])
        return parsed.messages

    def _tool_message(self, sdk_call) -> dict:
        """执行一次工具调用，返回回喂给模型的 tool 消息。"""
        raw_args = sdk_call.function.arguments
        args, ok = _parse_arguments(raw_args)

        if not ok:
            # 模型给出非法 JSON 是真实会发生的，得把原因回喂让它自己改
            logger.warning("工具 %r 的参数不是合法 JSON：%r",
                           sdk_call.function.name, raw_args)
            payload = {"error": f"参数不是合法 JSON：{raw_args}"}
        else:
            result = self.executor.execute(
                ToolCall(call_id=sdk_call.id, name=sdk_call.function.name, arguments=args)
            )
            # 成功回结果，失败回原因——模型得知道为什么失败才能纠正
            payload = (
                result.data if result.ok and result.data is not None
                else {"error": result.error, "permission_denied": result.permission_denied}
            )

        return {
            "role": "tool",
            "tool_call_id": sdk_call.id,
            "content": json.dumps(payload, ensure_ascii=False),
        }

    async def run_loop(
        self, user_question: str, history: list[dict[str, str]] | None = None,
        session_id: str = "",
    ) -> Reply:
        """FC 循环（§18.1 第 6 步）：最多 max_agent_steps 轮。

        每轮：调模型 → 摘出 <msg> → 有工具调用就执行并回喂 → 没有就收工。

        ⚠️ 与 §19-2 的偏离（实测逼出来的）：文档写「每轮必须先产出 <msg>
        才允许携带 FC」，但真实模型第一轮就是**纯工具调用、content 为 None**
        （这是原生 FC 的标准行为）。照文档字面执行会把第一轮直接拒掉，
        FC 永远跑不起来。这里改为：允许没有 <msg> 的工具轮，改用
        「整轮同工具同参数重复 → 熔断」+ 全局轮次上限来兜底，
        并在最终一句都没有时发兜底文案——保住了 §19-2 的意图（不让用户
        面对空白、不让循环失控），又不跟模型的原生行为对着干。
        """
        messages = self.assemble_messages(user_question, history)
        tools = self.registry.tool_schemas()

        collected: list[str] = []
        tool_calls_made = 0
        stop_reason = "ok"
        last_round: tuple | None = None

        for _ in range(self.max_agent_steps):
            assistant = await self._request(messages, tools)
            collected.extend(self._collect_messages(assistant.content or ""))

            calls = getattr(assistant, "tool_calls", None) or []
            if not calls:
                break  # 模型说完了

            # 熔断（§19-2/3）：只有"同工具 + 同参数"才计数，比较整轮签名。
            # 参数先规范化，否则空格/键序不同会漏判成"不同参数"。
            signature = tuple(
                (c.function.name, _canonical_signature(c.function.arguments))
                for c in calls
            )
            if signature == last_round:
                logger.warning("同工具同参数重复调用，熔断：%s", signature)
                stop_reason = "loop_cut"
                break
            last_round = signature

            messages.append(assistant)
            for call in calls:
                messages.append(self._tool_message(call))
                tool_calls_made += 1
        else:
            stop_reason = "max_steps"  # 没 break = 撞到轮次上限

        if not collected:
            # 模型一轮都没说出话（比如一直在调工具）——兜底，别让用户面对空白
            collected.append(self.fallback_text)
            if stop_reason == "ok":
                stop_reason = "parse_fallback"

        return Reply(
            session_id=session_id,
            messages=collected,
            tool_calls_made=tool_calls_made,
            stop_reason=stop_reason,
        )

    def chat(
        self, user_question: str, history: list[dict[str, str]] | None = None,
        session_id: str = "",
    ) -> Reply:
        """同步入口：说一句话，拿回一次对话的完整结果（Reply，§18.3）。

        参数顺序与 §22 类名表写的 chat(session_id, text) 不一致：这里保持
        text 在第一位，因为 history 是 M0 的临时物（M0-09 的 SessionStore
        会接手），而调换前两个参数会让现有调用静默传错。
        """
        return asyncio.run(self.run_loop(user_question, history, session_id))
