"""工具结果与来源 step 的因果链接。"""

from collections.abc import Sequence

from core.journal.runtime_store import State
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.trace import SPAN_ID_LENGTH, SpanLink
from core.trace.attributes import OATH_STEP_ID


def source_links(state: State, entries: Sequence[State]) -> list[SpanLink]:
    """这次认领的输入里，每条工具结果各指向产出它的那个 step 的 span。

    step 之间不是父子关系（第二步发生在第一步结束之后），所以唤醒关系只能靠 link
    表达。用户输入没有来源，不产生 link。

    用户消息和工具结果被认领进同一个 step 时，那句话开的是新 trace，而工具结果指向
    的是上一条 trace 的 span —— link 自带 `trace_id`，跨 trace 的因果因此还读得回来。
    找不到来源 step（journal 里已经没有）就跳过，不猜。
    """
    links: list[SpanLink] = []
    seen: set[str] = set()
    for entry in entries:
        item = entry.get("item") or {}
        if item.get("kind") != QueueName.TOOL or item.get("source") != "tool.terminal":
            continue
        batch_id = str((item.get("payload") or {}).get("batch_id") or "")
        source = state["steps"].get(batch_id)
        if not batch_id or source is None or batch_id in seen:
            continue
        seen.add(batch_id)
        links.append(
            SpanLink(
                trace_id=source["trace_id"],
                span_id=batch_id[:SPAN_ID_LENGTH],
                attributes={OATH_STEP_ID: batch_id},
            )
        )
    return links
