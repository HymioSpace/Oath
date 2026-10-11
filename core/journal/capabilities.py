"""adapter 声明自己支持哪些能力，conformance 套件据此决定跑还是跳过哪一项检查。

  REOPEN         关闭后重新打开能读到全部已提交内容
  SIMULATE_CRASH 支持在提交中途注入崩溃
  MULTI_PROCESS  同一份日志被多个进程同时写仍然正确
  BLOB_STORE     自带不可变 blob 存储
"""
from __future__ import annotations

from typing import Final

REOPEN: Final = "reopen"
SIMULATE_CRASH: Final = "simulate_crash"
MULTI_PROCESS: Final = "multi_process"
BLOB_STORE: Final = "blob_store"