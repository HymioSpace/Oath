import unittest

from biz.trace.api import query_trace
from core.protocol.datatype.trace import Span, SpanEvent
from core.protocol.datatype.user import User


class SyntheticTraces:
    def __init__(self, spans):
        self._spans = spans

    def trace_ids(self, user):
        return tuple(dict.fromkeys(span.trace_id for span in self._spans))

    def spans(self, user, trace_id):
        return tuple(span for span in self._spans if span.trace_id == trace_id)


class TraceSummaryTests(unittest.TestCase):
    def test_summary_keeps_event_metadata_without_payload_attributes(self):
        trace_id = "a" * 32
        span = Span(
            trace_id=trace_id,
            span_id="b" * 16,
            name="chat synthetic-model",
            start_unix_nano=1_000_000,
            end_unix_nano=9_000_000,
            attributes={"gen_ai.operation.name": "chat"},
            events=(
                SpanEvent(
                    "gen_ai.request",
                    2_000_000,
                    {"large_request": "synthetic prompt " * 1_000},
                ),
            ),
        )

        summary = query_trace(SyntheticTraces((span,)), User("alice"), trace_id, summary=True)
        full = query_trace(SyntheticTraces((span,)), User("alice"), trace_id)

        self.assertEqual(summary["view"], "summary")
        self.assertEqual(summary["spans"][0]["events"][0]["name"], "gen_ai.request")
        self.assertEqual(summary["spans"][0]["events"][0]["attributes"], {})
        self.assertTrue(full["spans"][0]["events"][0]["attributes"]["large_request"])


if __name__ == "__main__":
    unittest.main()
