"""装配与入口：读配置、实例化、注入 CoreRuntime。

依赖方向单向 —— `bootstrap` → `core`。core 不反向 import 这里任何东西。
"""

from bootstrap.config import from_dict, load_config
from bootstrap.runtime import AllowAllPolicy, Runtime, TokenAuthenticator

__all__ = [
    "AllowAllPolicy",
    "Runtime",
    "TokenAuthenticator",
    "from_dict",
    "load_config",
]
