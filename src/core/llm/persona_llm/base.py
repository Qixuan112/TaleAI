"""拼 base.md + chat.md + persona.md，组静态 system 提示词。

设计文档 §12（静态块字节稳定）/ §13–14（人格是用户的域）/ §19-13（改动重启生效）。

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

三分文件都是静态块，启动时拼一次、此后不变，所以 system 提示词字节稳定，
可命中 provider 端缓存（§12）。base/chat 是框架契约，不给用户改——改了
`<msg>` 就没法解析了。
"""

import re
from pathlib import Path

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

#: 首次运行落到用户处的**空白骨架**：只有标题 + 一段说明注释，没有正经内容。
#: 留空即走内置默认——默认不替用户把话说满（§13）。
_USER_PERSONA_SKELETON = """\
# 塔利

<!--
这是你的人设文件，你说了算（§13：data/config/ 是用户的域）。

- 留空——或者只留这段注释和下面的小标题——就用内置的「塔利」默认人设。
- 只要你写了一行正经内容，就整份按你写的来，内置默认不再掺和。

照小标题分块写就行，标题随意增删。改完重启生效（§19-13）。
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

    三分文件都是静态块，启动时拼一次、此后不变，
    因此 system 提示词内容字节稳定，可支持 provider 端缓存（方案 A）。
    """

    def __init__(self, persona_path: Path | str | None = None) -> None:
        # 三份静态块都放在 prompts/ 下，随源码一起进仓库（CI 才能验字节稳定）
        self.base_path = PROMPTS_DIR / "base.md"
        self.chat_path = PROMPTS_DIR / "chat.md"
        # 人格正文双轨：用户那份写了内容就优先，否则退回内置默认
        self.builtin_persona_path = BUILTIN_PERSONA_PATH
        self.user_persona_path = Path(persona_path) if persona_path is not None else USER_PERSONA_PATH
        # 方案 A：创建实例时拼一次并缓存
        self.system_prompt = self._build()

    def _persona_source(self) -> Path:
        """选人格正文的来源：用户写了内容就用用户的，否则用内置默认。

        判据是「有没有正经内容」，不是「文件在不在」——用户文件通常存在
        （启动时落的空白骨架），但没写东西时应当仍走内置默认。
        每次构造时判断一次（不是每轮请求），符合 §19-13「改动重启生效」。
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
        """现拼一次：读三个文件 + 拼接（私有方法，只被 __init__ 调用）。"""
        return (
            self.base_path.read_text(encoding="utf-8") + "\n\n"
            + self.chat_path.read_text(encoding="utf-8") + "\n\n"
            + self._persona_source().read_text(encoding="utf-8")
        )

    def build_system_prompt(self) -> str:
        """返回缓存的 system 提示词（不再重新读文件）。"""
        return self.system_prompt
