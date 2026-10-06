"""统一消息模型与适配器基类（设计文档 §18.3 / §22）。

两个 dataclass + 一个抽象基类：

- `Reply`：一次对话的结果（M0-08 已落）。
- `Message`：**平台消息的统一形状**（M0-11）。不管来自 WebSocket、QQ 还是
  以后别的平台，进了系统内部一律是它——上层（Router / ChatLLM / SessionStore）
  因此完全不认识"平台"这回事，新接一个平台不需要动它们一行。
- `AdapterBase`：适配器的共同骨架。子类只管三件事——把平台原始数据归一成
  `Message`、把 `Reply` 发回去、把服务跑起来。收件箱与 `recv()` 由基类提供，
  因为"收进来的消息怎么排队"对所有平台都是一样的。

方向约定：`direction` 只区分 in/out，不承担别的语义；`role` 才决定它进历史时
是 user 还是 assistant。
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from core.event_bus import EventBus


@dataclass
class Reply:
    """一次对话的返回（形状见 §18.3）。

    messages 是**要发给用户的话**，可能不止一条：模型可以在调工具前先说
    "帮你查查"、拿到结果后再说答案，这些是分开的两条消息而不是一段长文。

    stop_reason 的取值对应循环是怎么结束的：
      ok             —— 模型自己说完了（正常结束）
      max_steps      —— 撞到轮次上限
      loop_cut       —— 同工具同参数连续重复，被熔断
      parse_fallback —— 模型没按 <msg> 契约输出，走了纯文本兜底
      error          —— 调用/落库过程中出错（§18.3 四条之外的补充；一次
                        对话在到达模型之前就失败时，没有模型给的结束原因可用）
    """

    session_id: str = ""
    messages: list[str] = field(default_factory=list)
    tool_calls_made: int = 0
    stop_reason: str = "ok"


@dataclass
class Message:
    """平台消息的统一形状（§18.3）。

    字段顺序与文档一致；带默认值的字段只是构造时省事——平台给得出什么，
    适配器就填什么（QQ 已反解 @ 与引用；其余平台留空）。
    """

    id: str  # 全局唯一消息 ID
    platform: str  # web / qq / wechat
    session_id: str  # 稳定会话 ID
    owner: str  # 用户 ID（群聊 = 发言者）；记忆隔离维度
    direction: str  # in / out
    role: str  # user / assistant / system
    content: str  # 正文（不含 system_reminder）
    mentions: list[str] = field(default_factory=list)  # @ 提及（QQ 已反解）
    reply_to: str | None = None  # 引用回复的消息 ID（None = 没有引用；QQ 已填）
    # 被引用消息的**文本内容**（reply_to 是 ID，这个是内容本身）。
    #
    # ⚠️ 对 §18.3 的补充（基线 Message 没有这个字段）：QQ 的 reply 段只给
    # 消息 ID，模型拿 ID 没有任何用处——"他在回哪句话"必须把内容补出来。
    # 适配器负责（QQ：发 get_msg 拉原文；拉不到才回退 reply 段内联的文本）。
    # 空串 = 没有引用，或内容没拉到（降级为"只知有引用、不知内容"）。
    # 被引用的是纯图消息时，这里是 [图片] 占位而不是空串——否则装配侧
    # `if quoted:` 不成立，模型连"有引用"都不知道。
    #
    # 落库（对设计文档 v4.13「只进本次请求、不落库」的**用户拍板偏离**）：
    # 原文随消息写进 messages.quoted 列。原约定会让引用在下一轮就从历史里
    # 消失，模型事后完全看不出"他在回哪句话"——用户实测的正是这个症状。
    quoted: str = ""
    # 被引用消息里带的图片（本地文件名列表，存放约定同 images）。
    #
    # ⚠️ 对 §18.3 的补充：引用图必须"看得见"——只补文字的话，被引用的那张图
    # 等于不存在。装配时它们与当前消息的图一起进多模态数组（共享张数上限）。
    # 与 images 一样**不落库**：历史里的图不回流喂模型（UX-07 硬规则），
    # 历史里被引用的图只剩 quoted 文本中那个 [图片] 占位。
    quoted_images: list[str] = field(default_factory=list)
    ts: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)  # 平台私有字段，只透传
    # 会话类型：private / group。与 sessions.kind（§18.3）同义。
    #
    # ⚠️ 对 §18.3 的补充（基线 Message 没有这个字段）：基线把 kind 放在
    # sessions 表里，但装配提示词时需要它——模型得知道"这是群聊还是私聊"
    # （群里有别人在场，说话方式不同）。适配器在 normalize 时就最清楚这一点，
    # 让它顺手带上来，比事后从 session_id 前缀反推可靠（反推是猜，§19-5 精神）。
    session_type: str = "private"
    # "这条消息是不是直接冲着塔利来的"——适配器给的**平台事实**、不是判断。
    #
    # 为什么由适配器给：判断"有没有 @"需要 bot_id（从握手学来的），这是平台知识，
    # 只有适配器懂。唤醒的**策略**（什么范围生效、什么算叫它）不在这里——那是
    # 跨平台配置，放 core/wake.py，由 handle_message 汇合两者做决定。
    # 取值：True=确认在叫我（如 QQ 群被 @）；None=适配器没判定，网关按关键词判。
    #
    # ⚠️ 对 §18.3 的补充（基线 Message 没有这个字段），同 session_type 的理由。
    addressed: bool | None = None
    # 这条消息带的图片（文件名列表，UX-06）。空 = 纯文本消息。
    #
    # ⚠️ 对 §18.3 的补充（基线 Message 没有这个字段）：多模态输入的载体。
    # 为什么存**文件名**而不是内容/base64：图片本体在 data/temp/img/（可回收，
    # 超 100MB 删最早），库里/SQLite 里只留引用——两者生命周期不同，不混存。
    #
    # 硬规则：**只有最后一条**带图才生效（ChatLLM 只把最新提问发成 content 数组）。
    # 历史里的图是"数据"，不是"指令"——不回流喂模型（UX-07）。
    images: list[str] = field(default_factory=list)


class AdapterBase(ABC):
    """适配器基类：一个平台接一个适配器。

    子类必须实现三件事（§22：`normalize()` / `send()`，外加把服务跑起来的
    `start()`）：

    - normalize(raw) 平台原始数据 → Message
    - send(reply)    把结果发回该会话
    - start()        起服务（阻塞，通常由 main 用 create_task 挂成常驻任务）

    `recv()` 和收件箱由基类给：任何平台的"收"都是同一件事——把归一好的
    Message 投进队列，让前台循环 await 取走。子类只管 `self._deliver(msg)`。

    **事件总线在这里正好派上用场**：`_deliver` 在投递的同时 publish
    `message.received`（§18.2 把该事件的生产者记为 Adapter）。总线是旁路，
    所以这一步只是"喊一嗓子"，订阅者（日志/memory 触发）收不收都不影响收消息。
    """

    #: 平台标识，同时用作 AdapterRegistry 的键（web / qq / ...）
    name: str = ""

    def __init__(self, bus: EventBus | None = None) -> None:
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._bus = bus if bus is not None else EventBus()

    # ---------- 收（基类提供） ----------

    async def recv(self) -> Message:
        """等一条归一好的入站消息。前台循环的主阻塞点（§18.1 第 1 步）。"""
        return await self._inbox.get()

    def _deliver(self, message: Message) -> None:
        """把一条 Message 投进收件箱，并在旁路喊一声 message.received。

        用 put_nowait：这是从平台回调（可能在自己的协程里）进入的，
        绝不能在这里阻塞。
        """
        self._inbox.put_nowait(message)
        self._bus.publish(
            "message.received",
            session_id=message.session_id,
            platform=message.platform,
            owner=message.owner,
            content=message.content,
            message_id=message.id,
        )

    # ---------- 子类必须实现 ----------

    @abstractmethod
    def normalize(self, raw: Any) -> Message:
        """平台原始数据 → 统一 Message。"""

    @abstractmethod
    async def send(self, reply: Reply) -> None:
        """把一次对话结果发回对应会话。"""

    @abstractmethod
    async def start(self) -> None:
        """把平台服务跑起来（通常阻塞到进程结束）。"""
