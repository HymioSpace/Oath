"""权限判定与裁定。审批状态和工具提交意图共用 RuntimeStore 的一次原子提交。"""

from __future__ import annotations

import copy
import time
from typing import cast

from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype import User
from core.protocol.datatype.inbound import Inbound
from core.protocol.error import ToolRequestError
from core.protocol.interface.permission import PermissionPolicy
from core.tool.lifecycle import terminal
from core.utils.deadlines import Deadlines
from core.utils.encoding import JsonObject

DEFAULT_TTL_SECONDS = 300.0


class PermissionManager:
    def __init__(
        self,
        store: RuntimeStore,
        policy: PermissionPolicy,
        deadlines: Deadlines,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.store, self._policy = store, policy
        self._deadlines, self._ttl = deadlines, ttl_seconds

    def authorize(self, user: User, tool: str, arguments: JsonObject) -> str:
        """纯判定；ask 的登记随完整 batch 接纳提交，不在判定时提前展示授权卡。"""
        return self._policy.check(user, tool, copy.deepcopy(arguments))

    def get(self, user: User, invocation_id: str) -> State | None:
        call = self.store.read(user)["calls"].get(invocation_id)
        return cast(State, call) if call and call["permission"] is not None else None

    def pending(self, user: User) -> tuple[State, ...]:
        return tuple(
            c for c in self.store.read(user)["calls"].values() if c["status"] == "waiting_approval"
        )

    def decide(
        self, user: User, invocation_id: str, decision: str, command_id: str
    ) -> tuple[bool, tuple[Inbound, ...]]:
        """落一条审批裁定。返回 `(是否生效, 本次终结出来的终态)`。

        **终态要交给调用方发布。** 拒绝和过期走的是这里，不经过执行器 —— 采集它们要在
        这个事务里做，投递要在这个事务之外做，两件事分开。
        """
        if decision not in {"approved", "denied", "expired"}:
            raise ToolRequestError("无效审批结果")
        produced: list[Inbound] = []

        def apply(state: State) -> bool:
            known = state["controls"].get(command_id)
            identity = {"invocation_id": invocation_id, "decision": decision}
            if known:
                if known.get("identity") != identity:
                    raise ToolRequestError("审批 command_id 冲突")
                return bool(known["applied"])
            call = state["calls"].get(invocation_id)
            applied = call is not None and call["status"] == "waiting_approval"
            state["controls"][command_id] = {"identity": identity, "applied": applied}
            if not applied:
                return False
            if state["meta"].get(f"cancelled:{call['run_id']}"):
                collected = terminal(state, call, "cancelled", error="运行已取消，未执行")
                if collected is not None:
                    produced.append(collected)
                state["controls"][command_id]["applied"] = False
                return False
            now = time.time()
            expired = (
                self._deadlines.remaining(
                    (user, invocation_id, "approval"), call["approval_deadline"], self._ttl
                )
                <= 0
            )
            verdict = "expired" if expired or now >= call["approval_deadline"] else decision
            call.update(permission=verdict, source="system")
            if verdict == "approved":
                call.update(status="queued", deadline=now + call["admission"]["timeout"])
            else:
                collected = terminal(state, call, "failed", error=f"审批{verdict}，工具未执行")
                if collected is not None:
                    produced.append(collected)
            return True

        result = self.store.transact(user, "permission.decided", apply)
        return bool(result), tuple(produced)
