import unittest

from biz.trace.api import query_trace
from core.protocol.datatype.trace import Span, SpanEvent
from core.protocol.datatype.user import User
from core.tool.impl.threaded_tool_system import ThreadedToolSystem


class SyntheticTraces:
    def __init__(self, spans):
        self._spans = spans

    def trace_ids(self, user):
        return tuple(dict.fromkeys(span.trace_id for span in self._spans))

    def spans(self, user, trace_id):
        return tuple(span for span in self._spans if span.trace_id == trace_id)


class RecordingTraces(SyntheticTraces):
    def __init__(self):
        super().__init__(())
        self.written = []

    def append(self, _user, span):
        self.written.append(span)


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

    def test_tool_request_and_result_payloads_are_user_trace_events(self):
        trace_id = "c" * 32
        span = Span(
            trace_id=trace_id,
            span_id="d" * 16,
            name="execute_tool browser",
            start_unix_nano=2_000_000,
            end_unix_nano=8_000_000,
            attributes={"oath.tool.name": "browser", "oath.tool.outcome": "succeeded"},
            events=(
                SpanEvent(
                    "oath.tool.request",
                    2_000_000,
                    {"body": '{"description":"Check the page","url":"https://example.test"}'},
                ),
                SpanEvent(
                    "oath.tool.response",
                    8_000_000,
                    {"body": '{"content":{"title":"Example"}}'},
                ),
            ),
        )
        full = query_trace(SyntheticTraces((span,)), User("alice"), trace_id)
        events = full["spans"][0]["events"]
        self.assertEqual([event["name"] for event in events], [
            "oath.tool.request",
            "oath.tool.response",
        ])
        self.assertIn("Example", events[1]["attributes"]["body"])

    def test_runtime_tool_span_contains_semantic_call_and_result(self):
        traces = RecordingTraces()
        system = object.__new__(ThreadedToolSystem)
        system._traces = traces
        self.addCleanup(setattr, system, "_traces", None)
        system._trace_tool(
            User("alice"),
            "invocation-1",
            "browser",
            "succeeded",
            2_000_000,
            {"trace_id": "e" * 32, "step_id": "f" * 16, "channel": "web", "queue_ms": 4},
            {"description": "Read the destination page", "url": "https://example.test"},
            {"title": "Example"},
            None,
        )
        span = traces.written[0]
        self.assertEqual([event.name for event in span.events], [
            "oath.tool.request",
            "oath.tool.response",
        ])
        self.assertIn("Read the destination page", span.events[0].attributes["body"])
        self.assertIn("Example", span.events[1].attributes["body"])


if __name__ == "__main__":
    unittest.main()
