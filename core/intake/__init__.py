"""系统入口与队列消费。"""

from core.intake.journal_intake import COMMAND_KEY_PREFIX, JournalIntake, TypeRouter
from core.intake.tool_result_sink import RESULT_KEY_PREFIX, ToolResultRouter

__all__ = [
    "COMMAND_KEY_PREFIX",
    "RESULT_KEY_PREFIX",
    "JournalIntake",
    "ToolResultRouter",
    "TypeRouter",
]
