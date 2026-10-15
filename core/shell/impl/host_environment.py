"""宿主机执行环境：在本机 shell 里跑命令。沙箱实现将来换掉这一个类即可。"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Final

from core.protocol.interface.execution import ExecutionEnvironment, ExecutionResult

#: 单个输出流留多少字符（头尾各一半）。**读的时候就在限，不是读完再截。** 命令开头的
#: 错误和结尾的结论最有信息量，中间丢得起；没有上限的话一条 `yes` 就能把内存吃满，
#: 早于任何超时。
_MAX_STREAM_CHARS = 64 * 1024

#: 从 Oath 自己的进程里继承给命令的环境变量。白名单，不是排除法：`dict(os.environ)`
#: 会把模型 API key 这类服务端凭证交给任意一条 bash 命令。
_INHERITED: Final = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
)


class _BoundedStream:
    """一个流的收尾：留够上限的头尾，中间丢掉。

    边读边丢，所以内存占用与命令输出多长无关。没超上限时头尾拼起来就是全部，
    超了才插省略标记 —— 截断与否由它自己算，调用方不猜。
    """

    def __init__(self, limit: int = _MAX_STREAM_CHARS) -> None:
        self._head_size = limit // 2
        self._tail_size = limit - self._head_size
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0

    def feed(self, chunk: bytes) -> None:
        self._total += len(chunk)
        room = self._head_size - len(self._head)
        self._head += chunk[:room]
        self._tail += chunk[max(room, 0) :]
        if len(self._tail) > self._tail_size:
            del self._tail[: len(self._tail) - self._tail_size]

    @property
    def truncated(self) -> bool:
        return self._total > self._head_size + self._tail_size

    def text(self) -> str:
        """取回留住的那些字。

        头尾是按字节切的，切口可能落在多字节字符中间 —— 拼回去前把切口上的替换字符
        去掉，否则每条超限的命令开头都会多一个 `�`。
        """
        head = _decode(bytes(self._head)).rstrip("�")
        tail = _decode(bytes(self._tail)).lstrip("�")
        if not self.truncated:
            return f"{head}{tail}"
        dropped = self._total - len(self._head) - len(self._tail)
        return f"{head}\n[中间 {dropped} 字已省略]\n{tail}"


def _decode(raw: bytes) -> str:
    """命令输出的编码不保证。解不出可读文本就落回替换字符，不让一条命令整个失败。"""
    return raw.decode("utf-8", errors="replace")


def _drain(source: IO[bytes], sink: _BoundedStream) -> None:
    """把一个管道读干，边读边丢。

    不读干的话命令写满管道缓冲区就会卡住等读者，双方互等。两个流各一个线程：单线程
    轮流读的话，一个流写满就足以把整个命令挂住。
    """
    with contextlib.suppress(OSError, ValueError):
        while chunk := source.read(65536):
            sink.feed(chunk)


def _shell() -> str:
    """跑命令用哪个 shell。

    工具叫 `bash`，就该真的是 bash —— `/bin/sh` 在不少系统上是 dash，bash 语法直接
    报错。系统里没有 bash 时退回 `/bin/sh`：这时候语法按 POSIX 写。
    """
    found = shutil.which("bash")
    return found or "/bin/sh"


class HostEnvironment(ExecutionEnvironment):
    """在宿主机的子进程里跑命令。

    `root` 是默认工作目录，`bin_dirs` 前置到子进程的 `PATH` —— `oath-cli` 就是靠这个
    找到的。两个都不写进执行逻辑：沙箱实现里对应的是别的东西，接口不变。
    """

    def __init__(self, root: Path, *, bin_dirs: Sequence[Path] = ()) -> None:
        self._root = root
        self._bin_dirs = tuple(str(path) for path in bin_dirs)
        self._shell = _shell()

    @property
    def root(self) -> Path:
        """默认工作目录。工具没给 `cwd` 时用它。"""
        return self._root

    def run(
        self, command: str, *, cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> ExecutionResult:
        # 目录不存在就建出来：配置给的 workspace 还没有人往里放过东西。
        cwd.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        # 命令就是模型给的文本，这里就是它该去的地方。起不来（cwd 进不去、fork 失败）
        # 直接抛 OSError —— 那不是命令的结果，是环境不可用。
        #
        # `start_new_session` 是为了超时那次能连孙进程一起杀：`sh -c "a; b"` 一般不
        # exec 到底，kill 掉 shell 之后 `b` 还活着抱着管道不放。
        process = subprocess.Popen(
            [self._shell, "-c", command],
            cwd=str(cwd),
            env=self._environ(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        out, err = _BoundedStream(), _BoundedStream()
        readers = [
            threading.Thread(target=_drain, args=(pipe, sink), daemon=True)
            for pipe, sink in ((process.stdout, out), (process.stderr, err))
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(process)
            process.wait()
        finally:
            # 进程死了管道就到底了，读线程自己会结束；join 只是不留下还在跑的读。
            for reader in readers:
                reader.join(timeout=5.0)

        return ExecutionResult(
            exit_code=None if timed_out else process.returncode,
            stdout=out.text(),
            stderr=err.text(),
            duration_ms=(time.monotonic() - started) * 1000,
            timed_out=timed_out,
            truncated=out.truncated or err.truncated,
        )

    def _kill_group(self, process: subprocess.Popen[bytes]) -> None:
        """连着命令自己 fork 出来的进程一起杀。

        只 kill shell 的话，`sleep` 之类的后台进程会接着跑、继续占着管道，
        超时反而变成干等 —— 它跑多久，这个工具就卡多久。
        """
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    def _environ(self, env: Mapping[str, str]) -> dict[str, str]:
        """子进程的环境：白名单继承 + bin_dirs 改过的 PATH + 调用方给的这几个。

        调用方给的那几个排在最后：它们要盖得住继承来的同名值。
        """
        child = {name: os.environ[name] for name in _INHERITED if name in os.environ}
        path = child.get("PATH", os.defpath)
        child["PATH"] = os.pathsep.join((*self._bin_dirs, path)) if self._bin_dirs else path
        child.update(env)
        return child


__all__ = ["HostEnvironment"]
