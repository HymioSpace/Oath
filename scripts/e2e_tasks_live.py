"""Opt-in task tool flow using a real model and an isolated SQLite database.

DEEPSEEK_API_KEY stays in the environment. No real notifications are sent.
This validates provider contracts and durable task behavior, not the full deployment.
"""

import json
import os
import tempfile
import uuid
from pathlib import Path

from core.llm.openai_provider import OpenAICompatibleProvider
from core.protocol.datatype.config import ModelProviderConfig
from core.protocol.datatype.llm import LlmRequest
from core.protocol.datatype.message import Message
from core.protocol.datatype.tool import ToolInvocation
from core.protocol.datatype.user import User
from core.storage.database import Database
from core.storage.service import StorageService
from core.tasks.tools import TaskTool
from tests.test_tasks_e2e import specification


class EnvironmentSecret:
    def get(self, alias):
        return os.environ["DEEPSEEK_API_KEY"]


def main():
    with tempfile.TemporaryDirectory(prefix="oath-live-task-") as temporary:
        storage = StorageService(Database(Path(temporary) / "tasks.sqlite"))
        provider = OpenAICompatibleProvider(
            ModelProviderConfig(
                model="deepseek-flash",
                api_base="https://api.deepseek.com",
                credential="test/environment",
                timeout_seconds=60,
            ),
            EnvironmentSecret(),
        )

        def invoke(action, content, user="live_user"):
            tool = TaskTool(storage, action)
            response = provider.complete(
                LlmRequest(
                    messages=[
                        Message(
                            "system",
                            (
                                "You are testing the Oath task API. Call the specified tool "
                                "once, with valid arguments. Do not invent tool results."
                            ),
                        ),
                        Message("user", content),
                    ],
                    tools=(tool.definition,),
                    max_output_tokens=2500,
                    extra_params={
                        "tool_choice": {
                            "type": "function",
                            "function": {"name": tool.definition.name},
                        },
                        "thinking": {"type": "disabled"},
                    },
                )
            )
            assert len(response.tool_calls) == 1, response.content
            call = response.tool_calls[0]
            result = tool.execute(
                ToolInvocation(
                    User.from_storage_id(user),
                    "live_" + action + uuid.uuid4().hex,
                    call.arguments,
                    None,
                )
            ).content
            print(
                json.dumps(
                    {"stage": action, "usage": response.usage, "status": "passed"},
                    ensure_ascii=False,
                )
            )
            return result

        requested = specification()
        requested["intent_key"] = "live_reminder"
        made = invoke("create", "Create this exact user-authorized task: " + json.dumps(requested))
        tid = made["task"]["task_id"]
        storage.call("task.tick")
        d = storage.call("task.delivery.claim", user_id="live_user", owner="test_sink")
        assert d["payload"]["text"] == "Drink water"
        storage.call(
            "task.delivery.ack",
            user_id="live_user",
            delivery_id=d["delivery_id"],
            owner="test_sink",
            lease_token=d["lease_token"],
            status="accepted",
            receipt={"provider_id": "test_sink_1", "accepted_at": "2026-11-15T12:00:00Z"},
            error=None,
        )
        result = invoke("get", f"Read task {tid} using view detail. Do not query other tasks.")
        assert result["run"]["status"] == "succeeded"
        assert result["task"]["lifecycle"] == "closed"
        print(
            "PASS: real model → task tool → SQLite → program reminder → "
            "isolated sink receipt → real model status query"
        )

        requested = specification("agent")
        requested["intent_key"] = "live_math_analysis"
        requested["objective"] = (
            "Compute 2 + 2 from the provided input. Report the answer and "
            "cite provided_math_input as external evidence."
        )
        requested["acceptance"]["criteria"] = (
            "Answer is 4 and evidence refers to the provided input."
        )
        requested["context"] = {
            "brief": "Provided fixture: 2 + 2. Source reference: provided_math_input.",
            "refs": ["provided_math_input"],
        }
        made = invoke("create", "Create this exact task: " + json.dumps(requested))
        tid = made["task"]["task_id"]
        storage.call("task.tick")
        with storage.database.transaction(readonly=True) as db:
            run = db.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,)).fetchone()
        report = invoke(
            "report",
            "Execute this task from the supplied input and report decision complete: "
            + json.dumps(requested),
            run["context_id"],
        )
        assert report["status"] == "waiting"
        detail = storage.call("task.get", user_id="live_user", task_id=tid, view="detail")
        reviewed = invoke(
            "review",
            (
                "Verify the answer is 4 using provided_math_input=2+2. If "
                "correct accept it, otherwise reject. "
            )
            + json.dumps(
                {
                    "task_id": tid,
                    "expected_revision": detail["task"]["revision"],
                    "run_id": run["run_id"],
                    "result": detail["run"]["result"],
                }
            ),
        )
        d = storage.call("task.delivery.claim", user_id="live_user", owner="test_sink")
        assert d is not None, reviewed
        storage.call(
            "task.delivery.ack",
            user_id="live_user",
            delivery_id=d["delivery_id"],
            owner="test_sink",
            lease_token=d["lease_token"],
            status="accepted",
            receipt={"provider_id": "test_analysis_sink", "accepted_at": "2026-11-15T12:00:00Z"},
            error=None,
        )
        assert (
            storage.call("task.get", user_id="live_user", task_id=tid, view="detail")["run"][
                "status"
            ]
            == "succeeded"
        )
        print(
            "PASS: real model creates agent task → isolated context report → "
            "real model verification → accepted delivery"
        )


if __name__ == "__main__":
    main()
