"""工具结果的全文存放与截断。"""

from core.detail.shrink import Detail, ResultShrinker
from core.detail.tool import NAME as DETAIL_READ
from core.detail.tool import DetailReadTool

__all__ = ["DETAIL_READ", "Detail", "DetailReadTool", "ResultShrinker"]
