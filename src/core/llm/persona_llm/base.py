from pathlib import Path

# base.py 自己所在目录
BASE_DIR = Path(__file__).resolve().parent
# prompts/ 在上一级
PROMPTS_DIR = BASE_DIR.parent / "prompts"
# 项目根（爬 3 层）
PROJECT_ROOT = BASE_DIR.parents[3]
# data/config/
CONFIG_DIR = PROJECT_ROOT / "data" / "config"


class Persona:
    """拼 base.md + chat.md + persona.md，组静态 system 提示词。

    三份文件都是静态块，启动时拼一次、此后不变,
    因此 system 提示词内容字节稳定，可支持 provider 端缓存（方案 A）。
    """

    def __init__(self):
        # 用上面定义好的目录常量，拼出三个文件的完整路径
        self.base_path = PROMPTS_DIR / "base.md"
        self.chat_path = PROMPTS_DIR / "chat.md"
        self.persona_path = CONFIG_DIR / "persona.md"
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