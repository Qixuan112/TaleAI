"""拼 base.md + chat.md + persona.md，组静态 system 提示词。

设计文档 §12（静态块字节稳定）/ §13–14（人格是用户的域）/ §19-13（改动重启生效）。

**人格正文为什么有两份**（这是个补回来的设计）：

人格正文是最该让用户改的东西——§13 明说「data/config/ 才是用户的域」，
§14 说「面板 md 编辑器填写，用户不直接碰文件」。所以它的家在
`data/config/persona.md`。

但 `data/` 整个被 gitignore，CI 干净检出时那儿是空的——早先真因此挂过
（3 failed）。当时的急就章是把 persona.md 挪进 `src/`，CI 是绿了，可用户
改人设就得去动源码，跟上面两条设计相反。

现在补成双轨：
- `src/core/llm/prompts/persona.md` —— **内置默认模板**，随代码走、CI 靠它；
- `data/config/persona.md` —— **用户的**，存在就优先，用户随便改；
- `ensure_user_persona()`（启动时调）—— 用户那份不存在就从内置复制一份，
  让用户第一次打开就有东西可改，同时又不会覆盖已经改过的。

三分文件都是静态块，启动时拼一次、此后不变，所以 system 提示词字节稳定，
可命中 provider 端缓存（§12）。base/chat 是框架契约，不给用户改——改了
`<msg>` 就没法解析了。
"""

import shutil
from pathlib import Path

# base.py 自己所在目录
BASE_DIR = Path(__file__).resolve().parent
# prompts/ 在上一级
PROMPTS_DIR = BASE_DIR.parent / "prompts"

#: 内置默认模板：随代码走，CI 干净检出时唯一可用的一份
BUILTIN_PERSONA_PATH = PROMPTS_DIR / "persona.md"

# base.py 位于 src/core/llm/persona_llm/ → parents[4] 是项目根
PROJECT_ROOT = BASE_DIR.parents[3]
#: 用户的人格正文（§13 的用户的域）。gitignore，不进仓库
USER_PERSONA_PATH = PROJECT_ROOT / "data" / "config" / "persona.md"


def ensure_user_persona(path: Path | str | None = None) -> bool:
    """用户人设文件不存在就从内置模板落一份。返回是否真的创建了。

    幂等，且**绝不覆盖**已存在的文件——那是用户的东西（§13）。
    启动时调一次，用户第一次跑就有模板可改，不是对着一片空白。
    """
    target = Path(path) if path is not None else USER_PERSONA_PATH
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUILTIN_PERSONA_PATH, target)
    return True


class Persona:
    """拼 base.md + chat.md + persona.md，组静态 system 提示词。

    三分文件都是静态块，启动时拼一次、此后不变，
    因此 system 提示词内容字节稳定，可支持 provider 端缓存（方案 A）。
    """

    def __init__(self, persona_path: Path | str | None = None) -> None:
        # 三份静态块都放在 prompts/ 下，随源码一起进仓库（CI 才能验字节稳定）
        self.base_path = PROMPTS_DIR / "base.md"
        self.chat_path = PROMPTS_DIR / "chat.md"
        # 人格正文双轨：用户那份优先，没有就退回内置模板
        self.builtin_persona_path = BUILTIN_PERSONA_PATH
        self.user_persona_path = Path(persona_path) if persona_path is not None else USER_PERSONA_PATH
        # 方案 A：创建实例时拼一次并缓存
        self.system_prompt = self._build()

    def _persona_source(self) -> Path:
        """选人格正文的来源：用户的优先，没有就用内置模板。

        每次构造时判断一次（不是每轮请求）——所以运行中换文件不影响已
        构造的实例，符合 §19-13「改动重启生效」。
        """
        if self.user_persona_path.is_file():
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
