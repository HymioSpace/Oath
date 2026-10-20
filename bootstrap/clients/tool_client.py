"""Brain 与 Consumer 通过工具服务提交意图，不执行工具。"""

from dataclasses import asdict

from core.protocol.datatype.tool import BatchExecution, ToolDefinition, ToolHandle
from core.protocol.datatype.user import User


class ToolClient:
    def __init__(self, client, fence=None):
        self.client, self.fence = client, fence

    def definitions(self):
        child = bool(self.fence and User.from_storage_id(self.fence["user_id"]).agent_id)
        return tuple(
            ToolDefinition(**d)
            for d in self.client.call(
                "tools.definitions", **({"user_id": self.fence["user_id"]} if self.fence else {})
            )
            if child or d["name"] != "subagent_notify"
        )

    def submit_batch(self, user, batch_id, requests):
        result = self.client.call(
            "tools.submit",
            user_id=user.storage_id,
            batch_id=batch_id,
            requests=[asdict(r) for r in requests],
            fence=self.fence,
        )
        return BatchExecution(tuple(ToolHandle(**h) for h in result["handles"]))

    def apply_permission(self, user, invocation_id, decision, command_id):
        return self.client.call(
            "tools.permission",
            user_id=user.storage_id,
            invocation_id=invocation_id,
            decision=decision,
            command_id=command_id,
        )

    def cancel(self, user, run_id=None):
        self.client.call("tools.cancel", user_id=user.storage_id, run_id=run_id)

    def batch_status(self, user, batch_id):
        return self.client.call("tools.batch", user_id=user.storage_id, batch_id=batch_id)
