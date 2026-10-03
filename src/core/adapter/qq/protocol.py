"""OneBot 11 报文解析与构造（纯函数，不碰网络）。

契约来源：**OneBot 11 官方规范**（botuniverse/onebot-11），SnowLuma 声明兼容。
- 消息事件：`event/message.md`（private / group）
- 发送动作：`api/public.md`（send_private_msg / send_group_msg）
- 信封：`communication/ws.md`（事件带 `post_type`，API 响应带 `echo`）

为什么单独拆一层：QQ 适配器最容易写错的就是这里——段数组 vs CQ 码字符串、
私聊 vs 群聊的字段差异、CQ 码注入。全做成纯函数，就能脱离网络测穿。

**会话 ID 约定**：`qq:p<QQ号>`（私聊）/ `qq:g<群号>`（群聊）。
前缀有两个作用：① 群和私聊不会撞（同一个数字在两边含义不同）；
② 发消息时能反推出该用哪个动作。取 `p`/`g` 而不是 `private`/`group`，
只是为了短。
"""

import re

from core.adapter.base import Message

#: 会话 ID 前缀
_PRIVATE_PREFIX = "qq:p"
_GROUP_PREFIX = "qq:g"

#: CQ 码：[CQ:at,qq=123] / [CQ:image,file=x.jpg]
#: 用非贪婪匹配到第一个 `]`；CQ 码本身不允许在参数里出现裸 `]`。
_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")

#: 只认这两种 post_type 之外的都忽略
_MESSAGE_POST_TYPE = "message"
_SUPPORTED_MESSAGE_TYPES = ("private", "group")


def _extract_text_and_mentions(message) -> tuple[str, list[str]]:
    """把 OneBot 的 message 字段规整成 (纯文本, 被 @ 的 QQ 号列表)。

    message 有两种形态，取决于 SnowLuma 的 `messageFormat` 配置：
    - **array**：`[{"type":"text","data":{"text":"..."}}, {"type":"at",...}]`
    - **string**：`"[CQ:at,qq=10001] 你好"`

    两种都要处理：**不能假设用户配的是哪种**，配错一种就整个适配器哑掉，
    而这是配置项不是代码 bug，用户很难自己查出来。

    图片段（type=image / CQ:image）**不在这里取**——它的 url 要下载落地，
    是适配器层的活（要网络、要 image_store），保持本模块纯函数（见
    `extract_image_urls`）。
    """
    if isinstance(message, list):
        texts: list[str] = []
        mentions: list[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "text":
                texts.append(str(data.get("text", "")))
            elif seg_type == "at":
                qq = data.get("qq")
                if qq is not None:
                    mentions.append(str(qq))
            # 其余段（face / record...）不处理。
        return "".join(texts), mentions

    if isinstance(message, str):
        # 字符串形态：@ 从 CQ 码里捡，正文把 CQ 码剥掉（含 image 码，见下）
        mentions = [
            m.group(1)
            for m in re.finditer(r"\[CQ:at,qq=([^,\]]+)", message)
        ]
        text = _CQ_CODE.sub("", message)
        return text, mentions

    return "", []


def extract_image_urls(message) -> list[str]:
    """从 message 段里捞出图片 URL（UX-08）。纯函数，只认结构、不下载。

    - array：`[{"type":"image","data":{"url":"http://..."}}]`
    - string：`[CQ:image,file=x,url=http://...]`（url 参数可选，缺了就没有）

    有些实现把 url 放在 `data.file` 里（OneBot 允许 file 是 URL）——
    兜底也看一眼。取到什么算什么，下载失败在适配器层降级（当无图）。
    """
    urls: list[str] = []
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            data = seg.get("data") or {}
            url = data.get("url") or data.get("file")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
    elif isinstance(message, str):
        for m in re.finditer(r"\[CQ:image,([^\]]*)\]", message):
            params = m.group(1)
            url = None
            for part in params.split(","):
                k, _, v = part.partition("=")
                if k.strip() in ("url", "file") and v.startswith(("http://", "https://")):
                    url = v
                    break
            if url:
                urls.append(url)
    return urls


def _session_id(message_type: str, target_id) -> str:
    if message_type == "private":
        return f"{_PRIVATE_PREFIX}{target_id}"
    return f"{_GROUP_PREFIX}{target_id}"


def parse_event(data: dict) -> Message | None:
    """把一条 OneBot 上报解析成 Message；不是消息事件就返回 None。

    宽容处理：字段缺失、类型不认识、报文畸形一律返回 None 而不是抛异常——
    外来报文不能让适配器崩掉（同 ToolExecutor「永不抛」的取舍）。
    """
    if not isinstance(data, dict):
        return None
    # 带 echo 的是我们发出动作的应答，不是事件
    if "echo" in data:
        return None
    if data.get("post_type") != _MESSAGE_POST_TYPE:
        return None

    message_type = data.get("message_type")
    if message_type not in _SUPPORTED_MESSAGE_TYPES:
        return None

    if message_type == "private":
        # 私聊：对方是 user_id
        target = data.get("user_id")
        owner = data.get("user_id")
    else:
        # 群聊：会话是群，owner 仍是发言者（§十八-4：记忆按人隔离）
        target = data.get("group_id")
        owner = data.get("user_id")

    if target is None:
        return None

    content, mentions = _extract_text_and_mentions(data.get("message"))
    # 图片 URL 先捞出来放 meta——真正的下载落地是适配器层的事（要网络），
    # 本模块保持纯函数。适配器读完 meta 会 download → 填进 Message.images。
    image_urls = extract_image_urls(data.get("message"))

    return Message(
        id=str(data.get("message_id") or ""),
        platform="qq",
        session_id=_session_id(message_type, target),
        owner=str(owner) if owner is not None else "",
        direction="in",
        role="user",
        content=content.strip(),
        mentions=mentions,
        reply_to=None,
        ts=float(data.get("time") or 0),
        meta={"message_type": message_type, "self_id": data.get("self_id"),
              "image_urls": image_urls},
        # 群聊有别人在场，说话方式该不一样——模型需要知道（§十二）
        session_type=message_type,
    )


def parse_session_id(session_id: str) -> tuple[str, str] | None:
    """反解会话 ID → (kind, id)；不是 QQ 会话 ID 返回 None。

    解析失败**返回 None 而不是猜**（§十九-5 精神）：猜错会把消息发到不相干
    的人那里，代价远大于明确失败。
    """
    if not isinstance(session_id, str):
        return None
    if session_id.startswith(_PRIVATE_PREFIX):
        rest = session_id[len(_PRIVATE_PREFIX):]
        return ("private", rest) if rest.isdigit() else None
    if session_id.startswith(_GROUP_PREFIX):
        rest = session_id[len(_GROUP_PREFIX):]
        return ("group", rest) if rest.isdigit() else None
    return None


def build_send_action(session_id: str, text: str) -> tuple[str, dict]:
    """构造 OneBot 发送动作 → (action, params)。

    **一律用段数组发，绝不用 CQ 字符串**。理由不只是「更规范」——是安全：
    CQ 字符串里如果出现 `[CQ:at,qq=all]` 会被后端当指令执行，而 text 来自
    模型输出（不可信）。用段数组，文本就永远是文本。
    """
    parsed = parse_session_id(session_id)
    if parsed is None:
        raise ValueError(f"不是合法的 QQ 会话 ID：{session_id!r}")

    kind, target = parsed
    if kind == "private":
        return "send_private_msg", {
            "user_id": int(target),  # OneBot 要 number，不能给字符串
            "message": [{"type": "text", "data": {"text": text}}],
        }
    return "send_group_msg", {
        "group_id": int(target),
        "message": [{"type": "text", "data": {"text": text}}],
    }
