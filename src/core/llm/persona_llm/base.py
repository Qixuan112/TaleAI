"""拼 base.md + chat.md + persona.md，组静态 system 提示词。

设计文档 §12（静态块字节稳定）/ §13–14（人格是用户的域）。
§19-13「改动重启生效」**经用户拍板偏离**（PR3 热重载）：每回合现拼，
改完 `data/config/persona.md` 下一句对话即生效——文件没变时拼出来逐字节
一致，§12 缓存命中的目的不破（缓存的前提是"内容不变"，不是"读得少"）。

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

三个分文件拼出的 system 提示词，只要文件没变就逐字节稳定——它现在
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


def ensure_user_persona(path: Path | str | None = None) -> bool:
    """用户人设文件不存在就落一份空白骨架。返回是否真的创建了。

    幂等，且**绝不覆盖**已存在的文件——那是用户的东西（§13）。
    落骨架而不是内置人设：默认不替用户把话说满，留空即走内置默认。
    """
    target = Path(path) if path is not None else USER_PERSONA_PATH
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_USER_PERSONA_SKELETON, encoding="utf-8")
    return True


class Persona:
    """拼 base.md + chat.md + 人格正文，组静态 system 提示词。

    build_system_prompt 每次现拼（热重载，PR3）——但"内容不变 → 字节不变"，
    所以每次请求发出去的 system 前缀完全一致，provider 端缓存照样命中。
    """

    def __init__(self, persona_path: Path | str | None = None) -> None:
        # 三份静态块都放在 prompts/ 下，随源码一起进仓库（CI 才能验字节稳定）
        self.base_path = PROMPTS_DIR / "base.md"
        self.chat_path = PROMPTS_DIR / "chat.md"
        # 人格正文双轨：用户那份写了内容就优先，否则退回内置默认
        self.builtin_persona_path = BUILTIN_PERSONA_PATH
        self.user_persona_path = Path(persona_path) if persona_path is not None else USER_PERSONA_PATH
        # 上次成功拼出的提示词：热重载时读文件失败就回退到它（一个坏文件
        # 不该让每回合都炸）。这里首发拼一次——文件缺失要启动即暴露，不拖到第一句。
        self._last_good = self._build()

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

    def _build(self) -> str:
        """现拼一次：读三个文件 + 拼接。读不到就抛（由调用方兜底）。"""
        return (
            self.base_path.read_text(encoding="utf-8") + "\n\n"
            + self.chat_path.read_text(encoding="utf-8") + "\n\n"
            + self._persona_source().read_text(encoding="utf-8")
        )

    def build_system_prompt(self) -> str:
        """返回 system 提示词——**每次现拼**（热重载，PR3）。

        对模块 docstring 所述 §19-13 的偏离（用户拍板）：
        - 文件没改 → 拼出来与上次逐字节一致，§12 的 provider 缓存照样命中；
        - 文件改了（如 persona.md）→ 下一句对话即生效；
        - 读失败（被删/写坏/坏字节）→ 沿用上次成功那份，不炸回合。
        """
        try:
            prompt = self._build()
        except (OSError, UnicodeError):
            logger.warning("读人格文件失败，沿用上一次的提示词", exc_info=True)
            return self._last_good
        self._last_good = prompt
        return prompt
