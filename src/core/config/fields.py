"""面板字段定义（设计文档 §13 / §22 的 `FieldSpec`）。

**M0 只落定义，不建面板**——§二十四 待办写死了「M0 只做聊天页，配置面板排 M1」。
但定义现在就得立起来，因为它是 M1 面板的**输入契约**：面板读这份定义自动
渲染表单，用户永远不碰 JSON 文件（§13「面板驱动，不用 .env」）。

三条来自 §13 的硬要求，靠结构而不是靠注释守住：

1. **5 个域**：config / persona / platforms / plugins / secrets。面板按域分页，
   少一个域就少一页。
2. **sensitive**：密钥字段标出来——面板用密码框显示、读出来打码，值只存
   secrets.json。
3. **choices**：select 必须带选项，否则面板渲染出一个空下拉框。

字段的 `key` 用点号路径（`llm.model`），与 §13 的 JSON 结构对齐；面板据此
知道该写回哪个域的哪一层。
"""

from dataclasses import dataclass, field

#: 面板认得的控件类型（M1 渲染表单时按它分支）
#:   text     单行文本
#:   password 密钥（配合 sensitive：密码框 + 读出来打码）
#:   number   数值
#:   select   下拉（必须带 choices）
#:   textarea 多行文本（人格正文之类）
#:   bool     开关（插件启停之类）
FIELD_TYPES = ("text", "password", "number", "select", "textarea", "bool")


@dataclass(frozen=True)
class FieldSpec:
    """一个可面板编辑的配置字段。

    frozen：这是常量定义，运行时不该被改——改它等于改面板行为，得走代码评审。
    """

    key: str  # 点号路径，如 llm.model / bot.name
    label: str  # 面板上显示的中文名
    type: str  # FIELD_TYPES 之一
    default: object = None  # 默认值（与 default.py 保持一致）
    choices: tuple[str, ...] = ()  # select 的选项
    sensitive: bool = False  # 密钥：密码框 + 打码，只存 secrets.json
    help: str = ""  # 面板上的说明文字


# ---------------------------------------------------------------------------
# 5 个域的字段定义
# ---------------------------------------------------------------------------

_CONFIG_FIELDS = (
    FieldSpec(
        key="llm.provider", label="服务商类型", type="select",
        default="openai-compatible",
        choices=("openai-compatible",),
        help="目前只支持 OpenAI 兼容网关",
    ),
    FieldSpec(
        key="llm.base_url", label="网关地址", type="text", default="",
        help="必须以 /v1 结尾，如 https://api.example.com/v1",
    ),
    FieldSpec(
        key="llm.model", label="模型名", type="text", default="",
        help="服务商提供的模型标识",
    ),
    FieldSpec(
        key="llm.history_keep_messages", label="历史窗口（条）", type="number",
        default=10,
        help="每次请求保留最新这么多条消息（每条消息 = 1）",
    ),
    FieldSpec(
        key="llm.history_lookback_extra", label="窗口外多带（条）", type="number",
        default=5,
        help="在窗口之外再往前多带几条旧消息，让边界不硬切、上下文更连贯",
    ),
    FieldSpec(
        key="llm.max_agent_steps", label="工具调用轮次上限", type="number",
        default=3, help="FC 循环最多几轮（§19-2：默认 3，不建议调大）",
    ),
    FieldSpec(
        key="bot.name", label="角色名", type="text", default="塔利",
        help="人格自称。改这里只改配置；人格正文的人格名在 persona.md",
    ),
    FieldSpec(
        key="wake.words", label="唤醒词", type="text", default=["塔利"],
        help="多个用逗号分隔。群聊里正文含任一唤醒词（或 @ 塔利）才算在叫它",
    ),
    FieldSpec(
        key="wake.scope", label="唤醒范围", type="select", default="group",
        choices=("group", "all", "off"),
        help="group=仅群聊（私聊/网页直接回）；all=所有会话都要唤醒词；off=关闭",
    ),
)

_PERSONA_FIELDS = (
    FieldSpec(
        key="format", label="人格正文格式", type="select",
        default="markdown", choices=("markdown", "text"),
        help="编辑器按此模式打开 persona.md（§14：支持 md / 纯文本切换）",
    ),
    FieldSpec(key="version", label="版本", type="text", default="v1"),
)

# 平台域：键名必须与 loader.py 的 DEFAULT_SOURCES["platforms"] 对齐，
# 否则面板读不到真实配置值。
_PLATFORM_FIELDS = (
    FieldSpec(
        key="websocket.enabled", label="启用 WebUI 接入", type="bool", default=True,
    ),
    FieldSpec(
        key="websocket.port", label="WebUI 端口", type="number", default=8000,
        help="浏览器聊天页监听的端口",
    ),
    FieldSpec(
        key="qq.enabled", label="启用 QQ 接入", type="bool", default=False,
        help="需要先跑起 SnowLuma 后端并登录；M0-14",
    ),
    FieldSpec(
        key="qq.host", label="QQ 适配器监听地址", type="text", default="127.0.0.1",
        help="SnowLuma 反向 WS 连过来的地址",
    ),
    FieldSpec(
        key="qq.port", label="QQ 适配器端口", type="number", default=8866,
        help="SnowLuma 的 wsClients.url 要指向这个端口",
    ),
    FieldSpec(
        key="qq.access_token", label="QQ 访问令牌", type="text", default="",
        help="留空=不校验（单机自用）；填了则握手须带同一 token，防止别人顶掉 SnowLuma",
    ),
)

# 插件域：插件清单在运行时由 PluginRegistry 汇总，面板按插件动态生成开关；
# 这里定义的是"每个插件都有的公共字段"。
_PLUGIN_FIELDS = (
    FieldSpec(
        key="enabled", label="启用", type="bool", default=False,
        help="外部插件默认停用，用户确认权限声明后才启用（§19-11）",
    ),
)

_SECRET_FIELDS = (
    FieldSpec(
        key="llm.api_key", label="API 密钥", type="password", default="",
        sensitive=True, help="只存 secrets.json，面板显示时打码",
    ),
)


#: 域 → 字段列表。面板按域分页渲染。
FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    "config": _CONFIG_FIELDS,
    "persona": _PERSONA_FIELDS,
    "platforms": _PLATFORM_FIELDS,
    "plugins": _PLUGIN_FIELDS,
    "secrets": _SECRET_FIELDS,
}


def fields_for(domain: str) -> tuple[FieldSpec, ...]:
    """取某个域的字段定义；未知域报错而不是返回空（同 ContextAssembler 的取舍）。"""
    if domain not in FIELDS:
        raise ValueError(f"未知配置域: {domain!r}（见 FIELDS）")
    return FIELDS[domain]
