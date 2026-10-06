"""拼 base.md + chat.md + persona.md + 聊天规则，组静态 system 提示词。

设计文档 §12（静态块字节稳定）/ §13–14（人格是用户的域）。
§19-13「改动重启生效」**经用户拍板偏离**（PR3 热重载）：每回合现拼，
改完 `data/config/persona.md` 下一句对话即生效——文件没变时拼出来逐字节
一致，§12 缓存命中的目的不破（缓存的前提是"内容不变"，不是"读得少"）。

**提示词分层（v4.14，用户拍板「人设内容 + 聊天规则」）**：system 三部分——
① 框架契约：base.md（世界观/回应原则）+ chat.md（输出契约），用户不可改；
② 人设内容：persona.md（双轨，见下）；③ 聊天规则：`data/config/rules/` 下
global.md / private.md / group.md 三个用户文件。规则是**纯增量**：写了就注入、
留空/不存在就不注入，没有内置默认轨（框架底线已在 ①，不给"整份替换"的口子）。

注入顺序固定 `base → chat → persona → global → 场景`，场景规则（私聊/群聊
二选一）**压在 system 最尾**：私聊型与群聊型的 system 共享前面整段前缀，
差异只在末尾一小段——provider 前缀缓存的命中因此几乎无损（§十二 修订）。
两条禁令（由块序守卫测试钉住）：全局规则与场景规则之间不许再插任何块；
不许对文件文本做 strip/规范化。

**人格正文为什么有两份 + 为什么默认留空**（这是个补回来的设计）：

人格正文是最该让用户改的东西——§13 明说「data/config/ 才是用户的域」，
§14 说「面板 md 编辑器填写，用户不直接碰文件」。所以它的家在
`data/config/persona.md`。

但 `data/` 整个被 gitignore，CI 干净检出时那儿是空的——早先真因此挂过
（3 failed）。当时的急就章是把 persona.md 挪进 `src/`，CI 是绿了，可用户
改人设就得去动源码，跟上面两条设计相反。

现在补成双轨，再加一条「留空即默认」：

- `src/core/llm/prompts/persona.md` —— **内置默认**，随代码走、CI 靠它；
- `data/config/persona.md` —— **用户的**，写了内容就以它为准；
- `ensure_user_persona()`（启动时调）—— 用户那份不存在就落一份**空白骨架**
  （只有标题和说明注释）。落骨架而不是抄内置：按 §13，默认不该替用户把话说满，
  用户第一次打开又有地方可写；
- 判「空」：抹掉 HTML 注释和标题行后没有正经内容，就当没写，退回内置默认
  （见 `_has_real_content`）。想改人设，在标题下面写正文即可。

这些静态块拼出的 system 提示词，只要文件没变就逐字节稳定——它现在
每回合重拼（热重载），但"内容不变 → 字节不变"依然成立，provider 端
缓存照样命中（§12）。base/chat 是框架契约，不给用户改——改了
`<msg>` 就没法解析了。
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# base.py 自己所在目录
BASE_DIR = Path(__file__).resolve().parent
# prompts/ 在上一级
PROMPTS_DIR = BASE_DIR.parent / "prompts"

#: 内置默认：随代码走，CI 干净检出时唯一可用的一份
BUILTIN_PERSONA_PATH = PROMPTS_DIR / "persona.md"

# base.py 位于 src/core/llm/persona_llm/ → parents[4] 是项目根
PROJECT_ROOT = BASE_DIR.parents[3]
#: 用户的人格正文（§13 的用户的域）。gitignore，不进仓库
USER_PERSONA_PATH = PROJECT_ROOT / "data" / "config" / "persona.md"

#: 用户的聊天规则（v4.14：§14「人设内容 + 聊天规则」的第二块）。
#: 三个文件各管一段：global 哪儿都注入；private 仅私聊（含 WebUI）；
#: group 仅群聊。同样是用户的域、同样 gitignore。
RULES_DIR = PROJECT_ROOT / "data" / "config" / "rules"

#: 首次运行落到用户处的**空白骨架**：只有占位抬头 + 一段说明注释，没有正经内容。
#: 抬头用占位「角色名」而不是写死默认名——这是用户的文件，默认名是内置那份的事。
#: 留空即走内置默认——默认不替用户把话说满（§13）。
_USER_PERSONA_SKELETON = """\
# 角色名

<!--
这是你的人设文件，你说了算（§13：data/config/ 是用户的域）。

- 把「角色名」换成你给它的名字，照小标题分块写就行，标题随意增删。
- 留空——或者只留这段注释和下面的小标题——就用内置的「塔利」默认人设。
- 只要你写了一行正经内容，就整份按你写的来，内置默认不再掺和。

改完下一句对话就生效，不用重启。
-->

## 我是谁

## 我说话的样子

## 我的边界
"""

# 三个规则骨架：内容会被 `_has_real_content` 判空（只剩注释和标题）→ 不注入。
# 注释负责回答用户的三个问题：这里管哪儿、能写什么、写完怎么生效。
# 与 persona 骨架不同：写完后没有"内置默认"退回，那段注释也没法进正文——
# 它们只会随规则一起发给模型，所以注释里明说"写完可以整段删掉"。
_GLOBAL_RULES_SKELETON = """\
# 全局规则

<!--
这里写「在哪儿聊都管用」的规则——网页、QQ 私聊、群里，都会带上它。

可以写的东西：
- 聊什么：哪些话题多接、哪些点到为止
- 怎么回：话的长短、语气、要不要先反问
- 什么时候收敛：聊到哪该收尾、什么时候别硬接话

- 只留这段注释和标题 = 没写，不注入任何内容。
- 写了下一句对话就生效，不用重启；删掉正文就恢复成没写。
- 和框架自带的内置底线同类叠加（底线改不了、永远在）。
- 正文写在标题下面。写完这段注释可以整段删掉——留着的话，它会随规则一起发给模型。
-->

## 通用要求
"""

_PRIVATE_RULES_SKELETON = """\
# 私聊规则

<!--
这里写「只在一对一聊天里管用」的规则——网页上聊和 QQ 私聊都算私聊，群里不会带上它。

可以写的东西：
- 私聊里想要的氛围：更放松、更亲近，还是更克制
- 只适合两个人说的话：称呼、玩笑和吐槽的尺度
- 什么时候收敛：深夜、情绪不好、聊累了的时候怎么应对

- 只留这段注释和标题 = 没写，不注入。
- 写了下一句对话就生效，不用重启；删掉正文就恢复成没写。
- 和全局规则叠加生效；这条只在私聊里出现。
- 正文写在标题下面。写完这段注释可以整段删掉——留着的话，它会随规则一起发给模型。
-->

## 私聊里的要求
"""

_GROUP_RULES_SKELETON = """\
# 群聊规则

<!--
这里写「只在群聊里管用」的规则——QQ 群里才会带上它，私聊和网页那边不受影响。

可以写的东西：
- 群里对谁说话、对什么话装没看见（人多、话杂）
- 说话的长度和分寸：群聊刷屏讨人嫌
- 别人（不是主人）搭话时怎么办；什么时候安静看着、不插嘴

- 只留这段注释和标题 = 没写，不注入。
- 写了下一句对话就生效，不用重启；删掉正文就恢复成没写。
- 和全局规则叠加生效；这条只在群聊里出现。
- 正文写在标题下面。写完这段注释可以整段删掉——留着的话，它会随规则一起发给模型。
-->

## 群聊里的要求
"""

# 「有没有正经内容」的判定：抹掉注释和标题行，看还剩不剩东西。
# HTML 注释天然「不是给人设本身看的」，还支持多行，适合当骨架里的说明。
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t].*$", re.MULTILINE)


def _has_real_content(text: str) -> bool:
    """用户文件里是否写了正经人设内容（而不是只剩骨架）。

    抹掉 HTML 注释与标题行后还有非空白字符 → 算写了。于是空白骨架
    （注释 + 标题）被判成「没写」，自动退回内置默认。
    """
    without_comments = _COMMENT_RE.sub("", text)
    without_headings = _HEADING_RE.sub("", without_comments)
    return bool(without_headings.strip())


def _seed_if_missing(path: Path, skeleton: str) -> bool:
    """目标文件不存在就落一份骨架（内容 skeleton）。返回是否真的创建了。

    幂等，且**绝不覆盖**已存在的文件——那是用户的东西（§13）。
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skeleton, encoding="utf-8")
    return True


def ensure_user_persona(path: Path | str | None = None) -> bool:
    """用户人设文件不存在就落一份空白骨架。返回是否真的创建了。

    幂等，且**绝不覆盖**已存在的文件——那是用户的东西（§13）。
    落骨架而不是内置人设：默认不替用户把话说满，留空即走内置默认。
    """
    target = Path(path) if path is not None else USER_PERSONA_PATH
    return _seed_if_missing(target, _USER_PERSONA_SKELETON)


def ensure_user_rules(rules_dir: Path | str | None = None) -> list[Path]:
    """三个规则文件不存在就各落一份空白骨架。返回本次新建的路径列表。

    同 persona：幂等、绝不覆盖（§13）。与 persona 不同的是**没有内置默认轨**
    ——规则是纯增量的口子，留空/不存在就不注入（框架底线在 base.md，永远在，
    见模块 docstring 的"提示词分层"）。骨架负责"第一次打开有地方可写、
    知道能写什么"，判空后自动跳过。
    """
    directory = Path(rules_dir) if rules_dir is not None else RULES_DIR
    created: list[Path] = []
    for filename, skeleton in (
        ("global.md", _GLOBAL_RULES_SKELETON),
        ("private.md", _PRIVATE_RULES_SKELETON),
        ("group.md", _GROUP_RULES_SKELETON),
    ):
        path = directory / filename
        if _seed_if_missing(path, skeleton):
            created.append(path)
    return created


class Persona:
    """拼 base.md + chat.md + 人格正文 + 聊天规则，组静态 system 提示词。

    build_system_prompt 每次现拼（热重载，PR3）——但"内容不变 → 字节不变"，
    所以每次请求发出去的 system 前缀完全一致，provider 端缓存照样命中。
    按会话类型选一份场景规则（v4.14），分叉点压在最尾（见模块 docstring）。
    """

    def __init__(
        self,
        persona_path: Path | str | None = None,
        rules_dir: Path | str | None = None,
    ) -> None:
        # 三份静态块都放在 prompts/ 下，随源码一起进仓库（CI 才能验字节稳定）
        self.base_path = PROMPTS_DIR / "base.md"
        self.chat_path = PROMPTS_DIR / "chat.md"
        # 人格正文双轨：用户那份写了内容就优先，否则退回内置默认
        self.builtin_persona_path = BUILTIN_PERSONA_PATH
        self.user_persona_path = Path(persona_path) if persona_path is not None else USER_PERSONA_PATH
        # 聊天规则三文件（§14 v4.14）：同 persona 一样是用户的域，同一套
        # 三态语义（不存在/判空跳过、读坏整份回退）。
        self.rules_dir = Path(rules_dir) if rules_dir is not None else RULES_DIR
        self.global_rules_path = self.rules_dir / "global.md"
        self.private_rules_path = self.rules_dir / "private.md"
        self.group_rules_path = self.rules_dir / "group.md"
        # 上次成功拼出的提示词，**按会话类型各存一份**：热重载读文件失败就回退
        # 到同类型那份，绝不跨类型（私聊的提示词顶给群聊 = 注入错误场景的规则，
        # 比缺规则更糟）。只预填基础型 ""：框架文件坏了启动即暴露；用户的场景
        # 规则文件坏字节只让那类会话降级，不拦启动（private/group 懒构建）。
        self._last_good: dict[str, str] = {"": self._build()}

    def _persona_source(self) -> Path:
        """选人格正文的来源：用户写了内容就用用户的，否则用内置默认。

        判据是「有没有正经内容」，不是「文件在不在」——用户文件通常存在
        （启动时落的空白骨架），但没写东西时应当仍走内置默认。
        每次现拼时判断一次（热重载）：写完内容下一句对话即生效。
        """
        if self.user_persona_path.is_file():
            try:
                text = self.user_persona_path.read_text(encoding="utf-8")
            except OSError:
                return self.builtin_persona_path
            if _has_real_content(text):
                return self.user_persona_path
        return self.builtin_persona_path

    def _scene_rule_path(self, session_type: str) -> Path | None:
        """会话类型 → 场景规则文件；没有对应场景（含 ""/未知类型）返回 None。

        显式查表，不做"默认当私聊"的猜测：WebUI/QQ 私聊都会显式带 "private"；
        "" 只意味着调用方没提供会话身份（CLI、单测），此时只注入全局规则。
        """
        return {
            "private": self.private_rules_path,
            "group": self.group_rules_path,
        }.get(session_type)

    def _rule_texts(self, session_type: str) -> list[str]:
        """按会话类型取要注入的规则文本：全局规则 + 场景规则（可能为空表）。

        三态语义（规则没有内置默认轨，与 persona 的"留空即默认"不同）：
        - 文件不存在 → 跳过：这是正常状态（删掉即关闭该组规则），不注入不留字节；
        - 存在但判空（骨架只剩注释/标题）→ 跳过：同样不留字节；
        - 存在有内容但读坏（OSError/UnicodeError）→ **不在这里吞**：向上冒泡，
          由 build_system_prompt 走整份回退（对齐 persona 坏字节的既有先例）。

        规则文本不做 tag 转义（与 persona.md 一致）：escape_tag_markers 的威胁
        模型是"外部来源文本每轮被重喂、可伪造系统块"，而规则文件是主人自己
        机器上写的静态配置，与 persona 同信任域。若未来开放远程/多人编辑，
        信任模型变了，此处需重评。
        """
        out: list[str] = []
        for path in (self.global_rules_path, self._scene_rule_path(session_type)):
            if path is None or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if _has_real_content(text):
                out.append(text)
        return out

    def _build(self, session_type: str = "") -> str:
        """现拼一次：读框架/人设/规则文件 + 拼接。读不到就抛（由调用方兜底）。

        join 只在实到元素之间插 "\\n\\n"：某组规则被跳过（不存在/判空）时
        不产生多余空行——拼出的字节与"没有这组规则"时完全相同（缓存守卫
        测试对旧拼装做逐字节相等断言）。

        两条禁令（缓存边界，§十二）：
        - 不许在全局规则与场景规则之间插任何块——分叉点必须在最尾，
          否则私聊/群聊共享的那段长前缀被截短；
        - 不许对文件文本做 strip/规范化——任何"顺手清理"都会改变字节。
        """
        parts = [
            self.base_path.read_text(encoding="utf-8"),
            self.chat_path.read_text(encoding="utf-8"),
            self._persona_source().read_text(encoding="utf-8"),
            *self._rule_texts(session_type),
        ]
        return "\n\n".join(parts)

    def build_system_prompt(self, session_type: str = "") -> str:
        """返回 system 提示词——**每次现拼**（热重载，PR3），按会话类型选规则（v4.14）。

        session_type 只用来决定"哪份场景规则进 system"：private → 私聊规则、
        group → 群聊规则，其余（含缺省 ""）只注入全局规则。会话类型本体仍走
        reminder 动态块——这里不产生任何"按会话生成"的内容，只是**选一份内容
        与会话无关的静态文件**，所以每种类型内部依然"内容不变 → 字节不变"，
        §12 的缓存目的不破（分叉点在最尾，跨类型共享前缀）。

        对模块 docstring 所述 §19-13 的偏离（用户拍板）：
        - 文件没改 → 拼出来与上次逐字节一致，§12 的 provider 缓存照样命中；
        - 文件改了（如 persona.md / 规则文件）→ 下一句对话即生效；
        - 读失败（被删/写坏/坏字节）→ 沿用**同类型**上次成功那份，其次基础型
          （""）；绝不跨类型回退。不炸回合。

        并发前提：本函数全同步（无 await），在 asyncio 单线程事件循环里相对
        其它协程是原子的。若将来把文件读取改成 async/线程池，原子性前提消失，
        `_last_good` 需重新评估（加锁或传快照）。
        """
        try:
            prompt = self._build(session_type)
        except (OSError, UnicodeError):
            logger.warning(
                "读提示词文件失败，沿用上一次成功那份（session_type=%r）",
                session_type, exc_info=True,
            )
            return self._last_good.get(session_type) or self._last_good[""]
        self._last_good[session_type] = prompt
        return prompt
