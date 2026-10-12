"""崩溃写入子进程。

被父进程 SIGKILL，用来验证「已返回成功的 append 在崩溃后不会消失」。
每成功一条就往 stdout 打一行游标并立刻 flush；第一条成功之后还会落一个
ready 文件，好让父进程确定「已经开始写了」再去杀 —— 否则测试结果取决于
机器快慢，没有意义。

用法：crash_writer.py <root> <user_id> <session_id> <ready 文件> [最大记录数]
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.journal.impl import FileJournal
from core.protocol.datatype import EventType, Scope


def main() -> None:
    root = Path(sys.argv[1])
    scope = Scope(user_id=sys.argv[2], session_id=sys.argv[3])
    ready = Path(sys.argv[4])
    limit = int(sys.argv[5]) if len(sys.argv) > 5 else 1_000_000

    # content 故意做大：拉长单次 append 的耗时，让父进程更容易打在中途。
    # payload schema 不接受未知字段，所以只能用合法的 content。
    blob = "x" * 4096
    journal = FileJournal(root)
    for i in range(limit):
        result = journal.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": f"{i}:{blob}"},
            payload_schema_version=1,
            idempotency_key=f"k{i}",
        )
        print(f"{i}\t{result.cursor.value}", flush=True)
        if i == 0:
            ready.touch()


if __name__ == "__main__":
    main()
