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
import re
from pathlib import Path
from typing import Callable

from openai import AsyncOpenAI

from core.adapter.base import Reply
from core.event_bus import EventBus
from core.config.loader import Config
from core.executor import ToolCall, ToolExecutor
from core.image_store import IMAGE_PLACEHOLDER, MAX_IMAGES_PER_MESSAGE, to_data_uri
from core.llm.context import ContextAssembler, SessionContext, escape_tag_markers
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

#: 包裹正文用的标签本身（小写契约，见 xml_parser）。正文里若原样出现它们，
#: 补标签喂模型时会被提前闭合/嵌套——所以包裹前先转义。
_MSG_TAG = re.compile(r"</?msg>", re.IGNORECASE)


def _escape_msg_tags(text: str) -> str:
    """把正文里可能被误当标签的 `<msg>` / `</msg>` 转义掉，再包进 <msg>。

    为什么需要：非兜底的 part 由解析器保证不含标签（它在第一个 </msg> 处收尾，
    且拒绝嵌套），所以正常回复转义是空操作。只有**兜底** part（模型已破坏
    契约、整段原文当一条）可能含裸标签——不转义的话，"用 </msg> 结束" 这类
    正文补上外层 <msg> 后会提前闭合，下一轮模型读到的是残缺的标签结构，
    正是 PR #10 复现的格式漂移。转义保证补回的始终是**无歧义**的标签结构。
    """
    return _MSG_TAG.sub(lambda m: "&lt;" + m.group(0)[1:], text)


def _load_fallback_text() -> str:
    """读兜底文案（§19-2：没有 <msg> 时发它）。文件缺失也不该让对话崩掉。"""
    path = _PROMPTS_DIR / "fallback.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("读不到兜底文案 %s，用内置默认", path)
        return "……嗯？我刚走神了，你再说一遍？"


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


def _quoted_line(quoted: str) -> str:
    """「他在回哪句话」那一行的**唯一**渲染口径（实时与历史两条路共用）。

    措辞只写一次：两条路各写一份的话，将来改了一处必然漏另一处，
    模型看到的历史与实时格式就会悄悄分叉。
    转义不在这里做——调用方各自负责（实时路径在 assemble_messages 里转，
    历史路径由 _history_turn_for_model 注入后统一过 escape_tag_markers）。
    """
    return f"（引用了一条消息：{quoted}）"


def _history_turn_for_model(entry: dict) -> dict:
    """把一条历史 entry 转成**喂给模型**的形状（补回 <msg> 标签）。

    为什么需要这一步：历史正文在库里/界面里是**纯文本**——存储按原文存
    （记忆、检索都用它），WebUI 用 textContent 渲染（不能塞标签）。但
    模型学格式是"看前文怎么写"，历史里的 assistant 若是裸文本，它会跟着
    裸文本走——不再输出 <msg>，分条立刻失效（实测：裸文本历史 11/15 轮
    丢契约，补回标签后 0/15）。所以「存储形态」与「模型形态」必须分开：
    进模型上下文时把 assistant 的正文按空行还原成一条条 <msg>，正是
    QQ 适配器当初分条发送的那个边界。

    还原用哪份数据（PR #10 修）：
    - **有 `parts`**（新落库的 assistant 行，store.append 的 sidecar）：直接按
      分条数组逐条包裹——这是**严格的逆变换**，不再去猜 content 里的空行。
      旧实现按 `\\n\\n` 切，而 chat.md 明确允许 `<msg>` 内含换行，于是正文里
      的一个空行就被误当成"分条边界"；正文含 `<msg>` 时又因 `"<msg>" in content`
      整段跳过包裹。三种情况实测全部往返失败，这里一并修掉。
    - **无 `parts`**（旧行、或 role=user）：回落到旧启发式（`\\n\\n` 切 + 已带
      `<msg>` 则不重复包）——有损，但只影响加列之前写入的历史，且保住了
      既有幂等语义（见 test_context 的 already-tagged 用例）。

    返回值必须是**纯净的 {role, content}**：只喂模型认得的字段，不能把
    `parts` 一并带进请求体（严格网关会因未知字段 400）。

    媒体与引用（entry 里由 `store.history(with_media=True)` 带出的键）：
    历史里的图**不喂图本身**（UX-07 硬规则：历史是数据不是指令，
    图只随最新一条消息发），但**必须让模型知道"这里有过一张图"**——
    否则纯图消息在上下文里就是一条空白 user 消息，实测模型会直接答
    "我没收到你的图"。带图 → 补 `[图片]` 占位（几张补几个）；带引用 →
    补「（引用了一条消息：原文）」。只落了 ID 没原文（迁移前的旧行、
    当轮 get_msg 没拉到）→ 退化成不带原文的短行，别装作没这回事。

    ❗注入必须发生在本函数内 = 一定早于 assemble_messages 的
    `escape_tag_markers`。`quoted` 是外部文本且会逐轮重喂，转义晚一步
    就复现 PR #10 那个伪造系统块的洞。assistant 分支一行不动——
    `[图片]`/引用行只可能挂在 user 行上（生产路径上只有 user 行有这些列）。
    """
    role = entry.get("role")
    content = entry.get("content", "")

    if role == "assistant":
        parts = entry.get("parts")
        if isinstance(parts, list) and parts:
            tagged = "\n\n".join(
                f"<msg>{_escape_msg_tags(p)}</msg>" for p in parts
            )
            return {"role": "assistant", "content": tagged}
        # 旧行降级：无分条信息，按空行启发式补（幂等守卫保持不变）
        if content and "<msg>" not in content:
            split = [p.strip() for p in content.split("\n\n") if p.strip()]
            if split:
                return {
                    "role": "assistant",
                    "content": "\n\n".join(f"<msg>{p}</msg>" for p in split),
                }
        return {"role": "assistant", "content": content}

    # user（或其它角色）：不该有 <msg>，但也必须只留 role/content——
    # 历史 entry 可能带了 parts/其它 sidecar 键，一并丢干净。
    prefix = ""
    quoted = entry.get("quoted")
    if isinstance(quoted, str) and quoted:
        prefix = _quoted_line(quoted) + "\n"
    elif entry.get("reply_to"):
        prefix = "（引用了一条消息）\n"
    attachments = entry.get("attachments")
    placeholder = (
        " ".join([IMAGE_PLACEHOLDER] * len(attachments))
        if isinstance(attachments, list) and attachments else ""
    )
    body = " ".join(p for p in (content, placeholder) if p)
    return {"role": role, "content": prefix + body}


class ChatLLM:
    """对话机器人：组装上下文并调用真实 LLM。

    配置热重载（PR3）：run_loop 每轮开头经 settings_reader 读一次配置快照，
    有变化就搬进活实例——这是对 §19-13「改动重启生效」的**用户拍板偏离**
    （设置页改完不想重启）。类属性默认 None = 不重载：测试用
    `ChatLLM.__new__(ChatLLM)` 造裸实例（不跑 __init__）走的正是这条，
    读到类属性 None 直接跳过，永远不会在没装部件的实例上炸。
    """

    #: 配置整快照读取器：() -> {"llm": {...}, "api_key": "..."}。None = 不重载。
    #: 类属性兜底 + __init__ 赋实例属性遮盖——`__new__` 裸实例拿到的就是 None。
    settings_reader: "Callable[[], dict] | None" = None

    def __init__(self, bus: EventBus | None = None) -> None:
        # 读配置（服务商 / 模型 / key）
        cfg = Config.load("config")
        secrets = Config.load("secrets")
        llm = cfg.get("llm", {})
        # base_url / model 是用户配置（默认值为空），缺了就没法工作。
        # 这里主动校验并给出可操作的提示，而不是等 provider 返回难懂的 403。
        # （这是**启动**的 fail-fast，保留不变；热重载路径读空值则回退上次，
        #   见 _refresh_config——启动即错要当场暴露，运行中变错不该炸回合。）
        self.base_url: str = llm.get("base_url", "")
        self.model: str = llm.get("model", "")
        if not self.base_url or not self.model:
            raise ValueError(
                "llm.base_url / llm.model 未配置。请在 data/config/config.json 里填写"
                " 服务商网关地址（以 /v1 结尾）与模型名。"
            )
        self.api_key: str = secrets.get("llm", {}).get("api_key", "")
        # 热重载的读配置入口（PR3）：读 config + secrets 两个域，返回整快照。
        # 赋成实例属性 = 遮盖类属性的 None，真实例每轮 run_loop 都会过一遍它。
        self.settings_reader = self._read_settings_snapshot

        # 人格（M0-03 第一半：塔利）
        self.persona = Persona()
        # 输出解析（M0-04）：把 <msg> 标签从模型输出里摘出来
        self.parser = XmlParser()
        # 模型上下文窗口：每次请求往回带多少历史。
        # 保留最新 history_keep_messages 条，再往前多带 history_lookback_extra
        # 条旧消息（边界不硬切，上下文更连贯）。两个都按「条」算。
        # 可通过 data/config/config.json 的 llm 字段配置：
        #   "history_keep_messages": 10, "history_lookback_extra": 5
        self.history_keep_messages: int = int(llm.get("history_keep_messages", 10))
        self.history_lookback_extra: int = int(llm.get("history_lookback_extra", 5))
        # FC 循环上限（§19-2：全局 3 轮不变；配置只是让它可调，不是让它可变大）
        self.max_agent_steps: int = int(llm.get("max_agent_steps", 3))
        # 异步客户端（openai 3.x 是异步优先）
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        # 上下文装配（M0-03 欠的那半）：动态块由它准备，本类不自己拼（§18.5 硬规则 6）
        self.context = ContextAssembler()
        # 工具链（M0-05/06/07）：注册表提供工具表，执行器负责跑。
        # bus 透传给执行器——它执行完发 tool.called（§18.2），供日志页订阅。
        self.registry = PluginRegistry()
        self.executor = ToolExecutor(self.registry, bus=bus)
        # 当前处理的会话 ID：_tool_message 发 tool.called 时带上，标注谁触发的。
        # run_loop 每轮进来时刷新（单实例串行处理，不担心并发）。
        self._current_session_id: str = ""
        # 兜底文案（§19-2：模型什么都没说时发它；写在 prompts 里可改）
        self.fallback_text = _load_fallback_text()

    # ---------- 配置热重载（PR3） ----------

    @staticmethod
    def _read_settings_snapshot() -> dict:
        """读配置整快照：{"llm": {...}, "api_key": "..."}（config + secrets 两个域）。

        读失败（坏 JSON/文件被写坏）直接抛——由 _refresh_config 兜住回退。
        返回**整快照**而不是逐字段查询：热重载的原子单位是"一份配置"，
        半份新半份旧比全旧更糟。
        """
        cfg = Config.load("config")
        secrets = Config.load("secrets")
        return {
            "llm": dict(cfg.get("llm", {})),
            "api_key": str(secrets.get("llm", {}).get("api_key", "")),
        }

    async def _refresh_config(self) -> None:
        """把改过的配置搬进活实例（热重载）。run_loop 每轮开头调，失败绝不抛。

        - reader 为 None（裸实例/测试）→ 什么都不做；
        - 读取/校验失败（坏 JSON、base_url 或 model 为空）→ 记 warning，
          整快照保持**上一次成功**那份——绝不逐字段半量应用；
        - 成功 → 全量应用：model / 历史窗口 / 轮次上限每轮取新值；
          client 仅在 (base_url, api_key) 变化时重建（旧 client 尽力 close，
          关不掉只记 debug，不挡热重载）。
        """
        reader = self.settings_reader
        if reader is None:
            return
        try:
            snapshot = reader()
            llm = dict(snapshot.get("llm") or {})
            base_url = str(llm.get("base_url") or "")
            model = str(llm.get("model") or "")
            api_key = str(snapshot.get("api_key") or "")
            keep = int(llm.get("history_keep_messages", 10))
            extra = int(llm.get("history_lookback_extra", 5))
            steps = int(llm.get("max_agent_steps", 3))
            if not base_url or not model:
                raise ValueError("llm.base_url / llm.model 为空")
        except Exception:
            logger.warning(
                "配置热重载读取失败，沿用上一次的配置（改完下一句再试）",
                exc_info=True,
            )
            return

        if (base_url, api_key) != (self.base_url, self.api_key):
            old = self.client
            self.base_url = base_url
            self.api_key = api_key
            self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
            try:
                await old.close()
            except Exception:
                # 旧连接池随 GC 回收，关不掉不算错误——别为它挡下热重载
                logger.debug("关闭旧 LLM 客户端失败（忽略）", exc_info=True)
        self.model = model
        self.history_keep_messages = keep
        self.history_lookback_extra = extra
        self.max_agent_steps = steps

    def assemble_messages(
        self, user_question: str, history: list[dict[str, str]] | None = None,
        session: "SessionContext | None" = None,
        images: list[str] | None = None,
        quoted: str = "",
        quoted_images: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """装配：稳定前缀（人格 system）+ 会动尾巴（历史 + 最新提问）。

        动态块（时间 + 会话类型）拼到**最新 user 消息头部**，用
        <system_reminder> 包裹（§十二）。

        关键：reminder 只进本次请求，**不写回 history**（persist=False）。
        否则每轮都往历史里塞一份时间戳，上下文会越滚越大。
        history 是调用方的 list，这里绝不改它。

        session 是可缺省的——命令行/单测不带会话信息时，装配出来就只有时间，
        没有噪音（M0-11 之前就是这个行为，保持兼容）。

        images（UX-06）：最新这条消息带的图片文件名。**只作用于最新 user 消息**
        ——历史里的图不回流喂模型（历史是"数据"不是"指令"），这也是为什么
        只有最后一条能带图。有图时最新 user 的 content 变成 OpenAI 多模态
        content 数组；没图时**保持纯字符串**（文本路径一个字节都不变）。

        quoted（PR2）：被引用消息的文本（QQ 引用回复由适配器补出来）。
        插在动态块与正文之间**独立一行**，让模型知道"他在回哪句话"。
        转义要求同用户提问——见下。（它同时也落库，下一轮由历史渲染补回，
        见 _history_turn_for_model；所以这里不是它唯一的出场机会。）

        quoted_images（本次修复）：被引用消息里的图片文件名，与 images 一起
        进多模态数组——引用一张图却看不见它，等于没引用。
        **当前消息的图优先**占张数上限：合并后按 MAX_IMAGES_PER_MESSAGE 截断，
        防单请求体积失控（每张最多 8MB，8 张就是 85MB 量级，网关多半直接拒）。

        session（v4.14 补充）：它带来的 session_type 还决定 system 里注入哪份
        场景规则（私聊/群聊；见 Persona.build_system_prompt）。不给 session
        时只注入全局规则——命令行/单测的旧行为零变化。
        """
        # system 的规则层按会话类型分叉一次（规则是静态文件，分叉点压在
        # system 最尾）；会话类型本体仍在下面的 reminder 动态块里，不进 system。
        session_type = session.session_type if session is not None else ""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.persona.build_system_prompt(session_type)},  # 稳定 → 前缀
        ]

        # 如果提供了历史，先按窗口裁到最近若干条（再多带几条旧的）
        if history:
            trimmed = self._recent_history(history)
            # 补回 <msg> 标签再喂模型——历史正文在库里是纯文本，直接喂会让
            # 模型跟着裸文本走、丢掉输出契约（分条失效）。详见该函数 docstring。
            # 历史正文也转义系统块标记：历史里的旧伪造文本每轮都会被重喂，
            # 不转义就等于每轮都给模型一次"这是用户说的，但长得像系统块"的
            # 误导（escape 只动 <system_reminder，不影响助手回答的 <msg> 标签）。
            messages.extend(
                {
                    "role": turn["role"],
                    "content": escape_tag_markers(turn["content"]),
                }
                for turn in (_history_turn_for_model(m) for m in trimmed)
            )

        # 动态块包在 <system_reminder> 里，贴在最新提问前面
        reminder = self.context.render_reminder(self.context.assemble("chat", session))
        # 用户提问先转义可能冒充系统块的标记，再拼到动态块后面——否则用户
        # 写一句 <system_reminder>…</system_reminder> 就能伪造系统提示
        # （PR #10 High）。转义只动标记字符，用户的话其余照旧。
        safe_question = escape_tag_markers(user_question)
        parts: list[str] = []
        if reminder:
            parts.append(reminder)
        if quoted:
            # 引用内容也是**外部文本**（别人在 QQ 里说的话）——同样要转义，
            # 否则被引用的原话里写 <system_reminder>… 就能伪造系统块（同一个洞）。
            parts.append(_quoted_line(escape_tag_markers(quoted)))
        parts.append(safe_question)
        # 没 reminder 没 quoted 时 join 结果 == safe_question，文本路径不变
        text = "\n".join(parts)

        # 有图：最新提问发成 content 数组（图 + 文本）。图读不出（已被回收/
        # 文件缺失）就跳过它——不能让"图没了"把整条消息变得发不出去。
        # 被引用的图跟着一起发；当前消息的图排在前面（截断时优先保住它们）。
        names = [*(images or []), *(quoted_images or [])][:MAX_IMAGES_PER_MESSAGE]
        image_parts = [
            {"type": "image_url", "image_url": {"url": uri}}
            for uri in (to_data_uri(name) for name in names)
            if uri
        ]
        if image_parts:
            final: list[dict] = [{"type": "text", "text": text}, *image_parts]
        else:
            final = text  # 无图 → 纯字符串，文本路径不变

        # 最新提问放在最末尾（会动部分）
        messages.append({"role": "user", "content": final})
        return messages

    def _recent_history(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """滑动窗口：保留最新 N 条，再往前多带 M 条旧消息。

        「时刻保持最新」由它保证：无论会话多长，喂给模型的永远是"最近这一截"，
        窗口随新消息向前滑。多带那 M 条是为了让**窗口边界不硬切**——只留最新
        N 条时，模型会看不到紧邻窗口外那几句，衔接处容易断；往前多带一截，
        上一句在聊什么就接得上了（相当于给窗口一个缓冲带）。

        两个值都按「条」算（每条消息 = 1）。M=0 就是纯滑动窗口。
        条数基于 history 的实际行——不假设"1 轮 = 2 行"，末尾有单挂的
        收尾行也不影响：切出来的永远是"最新的那截"，且不会切出半条。

        history 是调用方的 list，这里绝不改它——只读、返回一个新切片。
        """
        if not history:
            return history
        total = self.history_keep_messages + self.history_lookback_extra
        if total <= 0:
            return history  # 0/负 = 不限窗口（调试用）
        return history[-total:]

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
                ToolCall(call_id=sdk_call.id, name=sdk_call.function.name, arguments=args),
                session_id=self._current_session_id,
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
        session_id: str = "", *, session_type: str = "", owner: str = "",
        images: list[str] | None = None, quoted: str = "",
        quoted_images: list[str] | None = None,
    ) -> Reply:
        """FC 循环（§18.1 第 6 步）：最多 max_agent_steps 轮。

        每轮：调模型 → 摘出 <msg> → 有工具调用就执行并回喂 → 没有就收工。

        session_type / owner 由调用方（前台循环）从 Message 里带过来，构成
        SessionContext 交给装配——模型据此知道自己在群聊还是私聊（§十二）。
        不传也能跑，只是模型看不到会话类型（命令行/单测如此）。

        quoted（PR2）：被引用消息的文本（QQ 引用回复），插到最新提问前。
        quoted_images（本次修复）：被引用消息里的图，与 images 合并进多模态。
        两者只在**发生的那一轮**发图；文本另外落库，历史里由装配补回。

        ⚠️ 与 §19-2 的偏离（实测逼出来的）：文档写「每轮必须先产出 <msg>
        才允许携带 FC」，但真实模型第一轮就是**纯工具调用、content 为 None**
        （这是原生 FC 的标准行为）。照文档字面执行会把第一轮直接拒掉，
        FC 永远跑不起来。这里改为：允许没有 <msg> 的工具轮，改用
        「整轮同工具同参数重复 → 熔断」+ 全局轮次上限来兜底，
        并在最终一句都没有时发兜底文案——保住了 §19-2 的意图（不让用户
        面对空白、不让循环失控），又不跟模型的原生行为对着干。
        """
        session = SessionContext(
            session_id=session_id, session_type=session_type, owner=owner
        )
        # 配置热重载（PR3）：每轮开头把改过的配置搬进来（model/密钥/窗口）。
        # 失败只会记 warning 并沿用上次成功的配置——热重载失败不该让对话失败。
        await self._refresh_config()
        messages = self.assemble_messages(user_question, history, session=session,
                                          images=images, quoted=quoted,
                                          quoted_images=quoted_images)
        tools = self.registry.tool_schemas()
        # 记下本次会话 ID，_tool_message 发 tool.called 时用（旁路事件标注来源）
        self._current_session_id = session_id

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
        self, session_id: str, user_question: str,
        history: list[dict[str, str]] | None = None, *,
        session_type: str = "", owner: str = "",
        images: list[str] | None = None,
    ) -> Reply:
        """同步入口：说一句话，拿回一次对话的完整结果（Reply，§18.3）。

        签名对齐 §22 类名表：`chat(session_id, text)`。session_id 放第一位，
        因为它是一等公民——决定回复发回哪个会话（M0-11 起有了真适配器）。
        history 作为可选的第三参数，M1 记忆接入后由 ContextAssembler 接管。

        注意：这是**同步**入口，内部 asyncio.run。前台循环（已在事件循环里）
        必须 await `run_loop()`，不能调这个——在运行中的循环里再 run 会报错。
        """
        return asyncio.run(self.run_loop(
            user_question, history, session_id,
            session_type=session_type, owner=owner, images=images,
        ))
