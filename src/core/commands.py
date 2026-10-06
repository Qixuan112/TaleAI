"""聊天命令（跨平台）：&newtale 重置会话。

**为什么后端还有一份**：重置最初只做在 WebUI 前端（index.html 识别后发
clear 控制帧）——QQ 的消息根本不经过那段 JS，所以 QQ 里没法重置。命令判定
放到这里、由 handle_message 统一处理，任何平台（QQ/微信/未来的）都能用。

**两处并存，命令名要同步改**：
- 前端 `webui/index.html` 的 `commandOf` / `RESET_CMD`：WebUI 的快路径
  （本地识别、即时抹气泡、不往返）；
- 本模块：其他平台的正路 + 跨平台兜底。
判定语义必须一致：前缀 & 或 /、**整行就是命令**、大小写不敏感。

纯代码判定，不经过模型——"这行是不是命令"是确定性问题，交给 LLM 判断
只会引入不确定性（与唤醒门同一条纪律，§十二）。
"""

#: 命令前缀：& 与 / 都认（& 跟正文不撞，/ 是大家更熟的习惯）
PREFIXES = ("&", "/")

#: 重置命令名（小写比较）
_RESET_NAMES = ("newtale",)


def command_of(text: str) -> str:
    """把一行文字归一成命令名；不是命令时返回空串。

    与前端 commandOf 同一语义：掐掉首尾空白、首字符必须是 & 或 /、其余部分
    小写（&NewTale 也算）。**整行就是命令**——"你好 &newtale" 不算。
    """
    t = (text or "").strip()
    if not t or t[0] not in PREFIXES:
        return ""
    return t[1:].lower()


def is_reset_command(text: str) -> bool:
    """这一行是不是"重置会话"命令。"""
    return command_of(text) in _RESET_NAMES
