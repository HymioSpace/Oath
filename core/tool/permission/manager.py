"""权限判定与裁定。审批状态和工具提交意图共用 RuntimeStore 的一次原子提交。"""

from __future__ import annotations

import copy
import hashlib
import time
from typing import cast

from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype import (
    PermissionDecision,
    PermissionRequest,
    PermissionVerdict,
    User,
)
from core.protocol.datatype.inbound import Inbound
from core.protocol.error import ToolRequestError
from core.protocol.interface.permission import PermissionPolicy
from core.tool.lifecycle import terminal
from core.utils.deadlines import Deadlines
from core.utils.encoding import JsonObject, canonical_json

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

    def authorize(
        self,
        user: User,
        tool: str,
        arguments: JsonObject,
        *,
        invocation_id: str,
        batch_id: str,
        run_id: str,
        channel: str,
    ) -> PermissionDecision:
        """Evaluate one exact action and bind any approval to a stable digest."""
        request = PermissionRequest(
            user,
            tool,
            copy.deepcopy(arguments),
            invocation_id,
            batch_id,
            run_id,
            channel,
        )
        raw = self._policy.check(request)
        decision = (
            raw
            if isinstance(raw, PermissionDecision)
            else PermissionDecision(str(raw), policy_version="legacy")
        )
        if decision.verdict not in PermissionVerdict.ALL:
            raise ValueError("Unknown permission verdict")
        if decision.verdict != PermissionVerdict.ASK:
            return decision

        approval = dict(decision.approval or {})
        grant_key = decision.grant_key or approval.get("grant_key")
        if not isinstance(grant_key, str) or not 1 <= len(grant_key) <= 256:
            grant_key = hashlib.sha256(canonical_json([tool, arguments]).encode()).hexdigest()
        digest = hashlib.sha256(
            canonical_json(
                [
                    user.user_id,
                    user.agent_id,
                    run_id,
                    invocation_id,
                    tool,
                    arguments,
                    channel,
                    decision.policy_version,
                    grant_key,
                ]
            ).encode()
        ).hexdigest()
        approval.update(action_digest=digest, grant_key=grant_key)
        approval.setdefault("title", f"Allow {tool}?")
        approval.setdefault("summary", decision.reason or "This action needs your approval.")
        approval.setdefault("risk", "high")
        approval.setdefault("effects", [])
        approval.setdefault("scope_options", ["once"])
        if len(canonical_json(approval).encode()) > 16384:
            raise ValueError("Approval description is too large")

        grants = self.store.read(user)["meta"].get("permission_grants", {})
        grant = grants.get(grant_key) if isinstance(grants, dict) else None
        if (
            isinstance(grant, dict)
            and grant.get("scope") in {"run", "exact_rule"}
            and (grant.get("scope") != "run" or grant.get("run_id") == run_id)
            and (not grant.get("expires_at") or grant["expires_at"] > time.time())
        ):
            return PermissionDecision(
                PermissionVerdict.ALLOW,
                reason="Existing exact permission grant",
                policy_version=decision.policy_version,
            )
        return PermissionDecision(
            decision.verdict,
            decision.reason,
            approval,
            grant_key,
            decision.policy_version,
        )

    def get(self, user: User, invocation_id: str) -> State | None:
        call = self.store.read(user)["calls"].get(invocation_id)
        return cast(State, call) if call and call["permission"] is not None else None

    def pending(self, user: User) -> tuple[State, ...]:
        return tuple(
            c for c in self.store.read(user)["calls"].values() if c["status"] == "waiting_approval"
        )

    def decide(
        self,
        user: User,
        invocation_id: str,
        decision: str,
        command_id: str,
        *,
        scope: str = "once",
        client_id: str = "",
        channel: str = "",
        reason: str = "",
        action_digest: str = "",
    ) -> tuple[bool, tuple[Inbound, ...]]:
        """落一条审批裁定。返回 `(是否生效, 本次终结出来的终态)`。

        **终态要交给调用方发布。** 拒绝和过期走的是这里，不经过执行器 —— 采集它们要在
        这个事务里做，投递要在这个事务之外做，两件事分开。
        """
        if decision not in {"approved", "denied", "expired"} or scope not in {
            "once",
            "run",
            "exact_rule",
        }:
            raise ToolRequestError("无效审批结果")
        if decision == "approved":
            call = self.store.entries(user, "calls", [invocation_id]).get(invocation_id)
            if call and call["status"] == "waiting_approval":
                raw = self._policy.check(
                    PermissionRequest(
                        user,
                        call["tool"],
                        copy.deepcopy(call["arguments"]),
                        invocation_id,
                        call["batch_id"],
                        call["run_id"],
                        call.get("channel", "unknown"),
                    )
                )
                current = (
                    raw
                    if isinstance(raw, PermissionDecision)
                    else PermissionDecision(str(raw), policy_version="legacy")
                )
                if current.verdict == PermissionVerdict.DENY:
                    decision = "denied"
                    reason = current.reason or "Permission policy changed"
        produced: list[Inbound] = []

        def apply(state: State) -> bool:
            identity = {
                "invocation_id": invocation_id,
                "decision": decision,
                "scope": scope,
                "action_digest": action_digest,
                "reason": reason,
            }
            known = state["controls"].get(command_id)
            if known:
                if known.get("identity") != identity:
                    raise ToolRequestError("审批 command_id 冲突")
                return bool(known["applied"])
            call = state["calls"].get(invocation_id)
            applied = call is not None and call["status"] == "waiting_approval"
            state["controls"][command_id] = {"identity": identity, "applied": applied}
            if not applied:
                return False
            approval = call.get("approval") if isinstance(call.get("approval"), dict) else {}
            expected_digest = approval.get("action_digest")
            if decision in {"approved", "denied"}:
                if not action_digest or not expected_digest:
                    raise ToolRequestError("审批必须绑定 action_digest")
                if action_digest != expected_digest:
                    raise ToolRequestError("审批对应的动作已变化")
                options = approval.get("scope_options", ["once"])
                if not isinstance(options, list) or scope not in options:
                    raise ToolRequestError("审批范围不可用")
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
            call["permission_decision"] = {
                "status": verdict,
                "scope": scope,
                "client_id": client_id[:128],
                "channel": channel[:32],
                "decided_at": time.time(),
                "reason": reason[:500],
                "action_digest": action_digest,
            }
            if verdict == "approved":
                if scope in {"run", "exact_rule"}:
                    grant_key = approval.get("grant_key")
                    if isinstance(grant_key, str) and grant_key:
                        grants = state["meta"].setdefault("permission_grants", {})
                        if not isinstance(grants, dict):
                            grants = {}
                            state["meta"]["permission_grants"] = grants
                        grants[grant_key] = {
                            "scope": scope,
                            "run_id": call["run_id"] if scope == "run" else "",
                            "tool": call["tool"],
                            "title": str(approval.get("title", ""))[:160],
                            "created_at": now,
                            "expires_at": None if scope == "run" else now + 30 * 86400,
                        }
                call.update(status="queued", deadline=now + call["admission"]["timeout"])
            else:
                collected = terminal(
                    state,
                    call,
                    "failed",
                    error=f"审批{verdict}，工具未执行",
                    error_kind=f"permission_{verdict}",
                )
                if collected is not None:
                    produced.append(collected)
            return True

        result = self.store.transact(user, "permission.decided", apply)
        return bool(result), tuple(produced)
