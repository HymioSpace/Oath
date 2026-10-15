"""命令在某个执行环境里跑；core 只认这个接口，不认宿主机还是沙箱。

执行环境负责三件 core 不该管的事：命令超时时收手、输出留有界、进程起不来时给出
「环境不可用」。它是 bash 工具唯一的外部依赖 —— 换实现不改工具与 server。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """一条命令跑完的样子。

    **非零 `exit_code` 不是错误**：那是命令的结果，调用方要读它并据此判断。环境本身
    跑不起来时抛异常，不落在这里。
    """

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    #: 命令超时被收手。`exit_code` 此时为 `None`，已产生的输出照样返回。
    timed_out: bool = False
    #: 输出超过去掉了一部分中段。
    truncated: bool = False


class ExecutionEnvironment(ABC):
    """在某个目录里跑一条命令。"""

    @abstractmethod
    def run(
        self, command: str, *, cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> ExecutionResult:
        """跑一条命令，等它结束或到 `timeout`。

        `env` **每次调用给**：一个进程里有多个 scope，把某个身份写进构造参数等于所有
        session 共用它。它是这次调用要注入子进程的环境变量，不替换继承来的整个环境。

        **超时由实现自己收手**，返回 `timed_out=True`。调用方的 deadline 到点只是不再
        等这个线程，杀不掉它 —— 有界的责任在这里。
        """
        ...


__all__ = ["ExecutionEnvironment", "ExecutionResult"]
