from pathlib import Path

# base.py 自己所在目录
BASE_DIR = Path(__file__).resolve().parent
# prompts/ 在上一级
PROMPTS_DIR = BASE_DIR.parent / "prompts"


class Persona:
    """拼 base.md + chat.md + persona.md，组静态 system 提示词。

    三份文件都是静态块，启动时拼一次、此后不变,
    因此 system 提示词内容字节稳定，可支持 provider 端缓存（方案 A）。
    """

    def __init__(self):
        # 三份静态块都放在 prompts/ 下，随源码一起进仓库（CI 才能验字节稳定）
        self.base_path = PROMPTS_DIR / "base.md"
        self.chat_path = PROMPTS_DIR / "chat.md"
        self.persona_path = PROMPTS_DIR / "persona.md"
        # 方案 A：创建实例时拼一次并缓存
        self.system_prompt = self._build()

    def _build(self) -> str:
        """现拼一次：读三个文件 + 拼接（私有方法，只被 __init__ 调用）。"""
        return (
            self.base_path.read_text(encoding="utf-8") + "\n\n"
            + self.chat_path.read_text(encoding="utf-8") + "\n\n"
            + self.persona_path.read_text(encoding="utf-8")
        )

    def build_system_prompt(self) -> str:
        """返回缓存的 system 提示词（不再重新读文件）。"""
        return self.system_prompt
