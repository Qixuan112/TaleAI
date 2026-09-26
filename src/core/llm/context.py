"""上下文装配：每次对话前，把该让模型看见的背景信息准备好。

这是设计文档 §十二 / §十九-19 的 M0 子集。

分工（§十二 定论，三件事分开）：
- 上下文管理 = 这个模块，**纯代码**，不上 LLM（不新增 ContextLLM）
- 静态块（人格、输出契约）→ system 消息，字节稳定，吃 provider 缓存
- 动态块（时间、记忆、清单…）→ 拼到**最新那条 user 消息头部**，
  用 <system_reminder> 包裹，让模型能区分"环境信息"和"用户说的话"
- persist=False：动态块**绝不写回聊天历史**，否则每轮都往里塞一份
  时间戳，上下文会滚雪球

M0 只有「环境块」一个感知源——记忆(M1)、公共清单(M2) 还没有生产者。
这不是砍功能：单独一条时间对模型就立刻有用（它现在不知道现在几点）。
§19-17 的"总预算 + 分区优先级截断"要有 ≥2 个源才有意义（优先级是源
之间的取舍），留到 M1 有记忆时一起做。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

_WEEKDAYS = "一二三四五六日"


@dataclass
class ContextBlock:
    """一个感知块。字段形状见设计文档 §18.3。"""

    name: str  # chat_env / memory_hits / ledger_digest / unread_inbox
    kind: str  # static / dynamic
    order: int  # 注入顺序
    content: str
    tokens: int = 0  # M0 不裁切，暂不填（§19-17 的预算机制留到 M1）


@dataclass
class SessionContext:
    """装配时的会话信息。

    M0 没有适配器层（那是 M0-11），所以这些字段都可缺省——
    命令行对话时全为空，装配出来就只有时间，没有噪音。
    """

    session_id: str = ""
    session_type: str = ""  # private / group
    owner: str = ""


@dataclass
class ContextSpec:
    """每个 LLM 的感知清单。

    「代码内声明，不读配置文件」（§18.3）——配额是架构常量，不是用户偏好。
    sources 每项是 (源名, 条数配额, token 配额)；M0 不裁切，两个配额先占位。
    """

    llm_name: str
    sources: list[tuple[str, int, int]] = field(default_factory=list)


def _build_chat_env(session: SessionContext, now: datetime) -> ContextBlock:
    """环境块：当前时间（M1 起再加会话环境等）。

    时间用「现在」是有意的——模型的知识截止到训练时，不给它当前时间，
    它会把"今天"理解成训练数据里的某一天。
    """
    lines = [f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}（星期{_WEEKDAYS[now.weekday()]}）"]
    if session.session_type:
        lines.append(f"会话类型：{session.session_type}")
    return ContextBlock(name="chat_env", kind="dynamic", order=10, content="\n".join(lines))


# 源名 → 构造函数。M1/M2 的新源（memory_hits / ledger_digest / unread_inbox）
# 在这里登记，同时在下面的 spec 里挂上配额。
_BUILDERS: dict[str, Callable[[SessionContext, datetime], ContextBlock]] = {
    "chat_env": _build_chat_env,
}

# M0 只声明 ChatLLM 的感知清单（§22 备注）。M1 补：
#   ("memory_hits", 20, 2000), ("unread_inbox", ...)
# M2 补：("ledger_digest", ...)
_SPECS: dict[str, ContextSpec] = {
    "chat": ContextSpec(llm_name="chat", sources=[("chat_env", 0, 0)]),
}


class ContextAssembler:
    """按 ContextSpec 装配感知块。各 LLM 不许自己拼上下文（§18.5 硬规则 6）。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        # 时钟是个可替换的接缝：测试里注入固定时间，装配结果就完全确定，
        # 不用 sleep、也不用 mock 时间库。
        self._clock = clock or datetime.now

    def assemble(
        self, llm_name: str, session: SessionContext | None = None
    ) -> list[ContextBlock]:
        """装配某个 LLM 的感知块，按 order 排序返回。

        未知 llm_name 直接报错——静默返回空列表会让"忘了声明 spec"
        变成一个查不出来的 bug。
        """
        if llm_name not in _SPECS:
            raise ValueError(f"未知的 LLM 感知清单: {llm_name!r}（见 _SPECS）")

        spec = _SPECS[llm_name]
        session = session or SessionContext()
        now = self._clock()

        blocks: list[ContextBlock] = []
        for source, _count_quota, _token_quota in spec.sources:
            builder = _BUILDERS.get(source)
            if builder is None:
                continue  # 该源还没有生产者，跳过（M1/M2 会补上）
            blocks.append(builder(session, now))

        blocks.sort(key=lambda b: b.order)
        return blocks

    def render_reminder(self, blocks: list[ContextBlock]) -> str:
        """把动态块渲染成一个 <system_reminder> 文本块。

        没有动态块（或内容全空）→ 返回空串，调用方据此不拼任何东西。
        """
        dynamic = [b for b in blocks if b.kind == "dynamic" and b.content.strip()]
        if not dynamic:
            return ""
        body = "\n".join(b.content.strip() for b in dynamic)
        return f"<system_reminder>\n{body}\n</system_reminder>"
