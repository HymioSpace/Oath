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
from core.subagents.storage import finish
from core.subagents.tools import SubagentTool
from core.tasks.prompts import INTERACTION, LEAF, TASK_AGENT
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

        def invoke(action, content, user="live_user", *, choose=False):
            tool = (
                SubagentTool(storage, action.split(".")[1])
                if action.startswith("subagent.")
                else TaskTool(storage, action)
            )
            role = storage.call("task.context", user_id=user)
            prompt = TASK_AGENT if role and role["role"] == "task_agent" else INTERACTION
            response = provider.complete(
                LlmRequest(
                    messages=[
                        Message(
                            "system",
                            (
                                (
                                    "Choose whether this request needs durable work. "
                                    "Use tools when appropriate.\n"
                                    if choose
                                    else (
                                        "You are testing the Oath task API. "
                                        "Call the specified tool once, "
                                        "with valid arguments. Do not invent tool results.\n"
                                    )
                                )
                                + prompt
                            ),
                        ),
                        Message("user", content),
                    ],
                    tools=(tool.definition,),
                    max_output_tokens=2500,
                    extra_params={
                        **(
                            {}
                            if choose
                            else {
                                "tool_choice": {
                                    "type": "function",
                                    "function": {"name": tool.definition.name},
                                }
                            }
                        ),
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

        immediate = provider.complete(
            LlmRequest(
                messages=[
                    Message("system", INTERACTION),
                    Message("user", "What is 2+2? Give just the answer."),
                ],
                tools=(TaskTool(storage, "create").definition,),
                max_output_tokens=80,
                extra_params={"thinking": {"type": "disabled"}},
            )
        )
        assert not immediate.tool_calls and "4" in immediate.content
        print("PASS: role prompt handles immediate question without creating a task")

        reminder = invoke(
            "create",
            "每天20:00（Asia/Shanghai）提醒我喝水，固定提醒文字是 Drink water。"
            "请直接登记后台任务，使用合理且有限的默认预算。",
            choose=True,
        )
        reminder_detail = storage.call(
            "task.get", user_id="live_user", task_id=reminder["task"]["task_id"], view="detail"
        )
        assert reminder_detail["spec"]["executor"]["kind"] == "program"
        assert reminder_detail["spec"]["executor"]["action"] == "notify"
        assert reminder_detail["spec"]["trigger"]["mode"] == "daily"
        assert reminder_detail["spec"]["trigger"]["time"] == "20:00"
        assert not storage.call("subagent.list", user_id="live_user")
        print("PASS: natural-language water reminder selects program/notify, with no TaskAgent")
        if os.environ.get("OATH_LIVE_ROUTING_ONLY") == "1":
            return

        requested = specification()
        requested["intent_key"] = "live_reminder"
        made = invoke(
            "create",
            "Please schedule this background reminder and return immediately. "
            "Use these exact task requirements: " + json.dumps(requested),
            choose=True,
        )
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
        requested["title"] = "核对计算结果"
        requested["objective"] = (
            "核对输入 2 + 2 的计算结果，用中文简短说明结论，并引用输入来源 provided_math_input。"
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
        child = invoke(
            "subagent.spawn",
            "Delegate to one bounded leaf. Its objective must include the actual input: "
            "Provided arithmetic fixture: 2 + 2. Source: provided_math_input. "
            "Title: 计算核对. Success criteria: answer 4 with fixture reference.",
            run["context_id"],
        )
        waiting = invoke(
            "report",
            "Report decision wait with checkpoint progress awaiting arithmetic check, "
            "wait.kind=subagents, wait.reason=collect verified fact, wait.agent_ids="
            + json.dumps([child["agent_id"]]),
            run["context_id"],
        )
        assert waiting["status"] == "waiting"
        leaf = provider.complete(
            LlmRequest(
                messages=[
                    Message("system", LEAF),
                    Message(
                        "user",
                        child["objective"] + "\nReturn the answer as an Arabic digit with source.",
                    ),
                ],
                tools=(),
                max_output_tokens=300,
                extra_params={"thinking": {"type": "disabled"}},
            )
        )
        assert "4" in leaf.content, leaf.content
        with storage.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM subagents WHERE agent_id=?", (child["agent_id"],)
            ).fetchone()
            finish(db, row, "succeeded", leaf.content)
        storage.call("supervisor.tick", interval=1)
        storage.call("supervisor.tick", interval=1)
        resumed = storage.call("task.get", user_id="live_user", task_id=tid, view="detail")
        assert resumed["run"]["status"] == "running"
        assert resumed["execution"]["subagents"][0]["result"] == leaf.content
        report = invoke(
            "report",
            "Integrate verified leaf result "
            + leaf.content
            + " and report decision complete for: "
            + json.dumps(requested),
            run["context_id"],
        )
        assert report["status"] == "succeeded"
        detail = storage.call("task.get", user_id="live_user", task_id=tid, view="detail")
        d = storage.call("task.delivery.claim", user_id="live_user", owner="test_sink")
        assert d is not None
        assert detail["run"]["status"] == "succeeded"
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
            "PASS: real model creates agent task → leaf delegation → sleep/wake/join → report → "
            "completion → independent notification receipt"
        )
        if output := os.environ.get("OATH_TASK_REVIEW_OUTPUT"):
            import asyncio
            import sys

            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Hymio/backend"))
            from hymio_backend.tasks.remote import RemoteTasks

            bridge = object.__new__(RemoteTasks)

            async def oath(uid, path, data, request_id=None):
                return storage.call("task." + path.rsplit("/", 1)[1], user_id=uid, **data)

            bridge.oath = oath
            value = asyncio.run(bridge.get("live_user", tid))
            Path(output).write_text(json.dumps(value, ensure_ascii=False))
            print("PASS: exported actual live-model task through Hymio's task adapter")


if __name__ == "__main__":
    main()
