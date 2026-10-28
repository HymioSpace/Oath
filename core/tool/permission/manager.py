"""权限判定与裁定。审批状态和工具提交意图共用 RuntimeStore 的一次原子提交。"""

from __future__ import annotations

import copy
import hashlib
import time
from typing import cast

from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype import (
    PermissionDecision,
    PermissionEventType,
    PermissionRequest,
    PermissionVerdict,
    QueueName,
    User,
)
from core.protocol.datatype.inbound import Inbound
from core.protocol.error import ToolRequestError
from core.protocol.interface.permission import PermissionPolicy
from core.tool.lifecycle import terminal
from core.utils.deadlines import Deadlines
from core.utils.encoding import JsonObject, canonical_json, utc_now_iso

DEFAULT_TTL_SECONDS = 300.0
LATE_RETRY_TTL_SECONDS = 120.0


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
        if isinstance(grant, dict):
            grant_scope = grant.get("scope")
            run_matches = grant_scope != "run" or grant.get("run_id") == run_id
            unexpired = not grant.get("expires_at") or grant["expires_at"] > time.time()
            remaining = grant.get("remaining", 1)
            available = grant_scope != "once" or (isinstance(remaining, int) and remaining > 0)
        else:
            grant_scope, run_matches, unexpired, available = "", False, False, False
        if grant_scope in {"once", "run", "exact_rule"} and run_matches and unexpired and available:
            return PermissionDecision(
                PermissionVerdict.ALLOW,
                reason="Existing permission grant",
                approval=approval if grant_scope == "once" else None,
                grant_key=grant_key,
                policy_version=decision.policy_version,
                retry_of=str(grant.get("retry_of", "")) if grant_scope == "once" else "",
                consume_grant=grant_scope == "once",
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
        """落一条审批裁定。返回 `(是否生效, 本次需要发布的队列事件)`。

        **终态要交给调用方发布。** 拒绝和过期走的是这里，不经过执行器 —— 采集它们要在
        这个事务里做，投递要在这个事务之外做，两件事分开。迟到决定不会恢复旧调用，
        而是产生一条供 Agent 决策的 `permission.late_resolved` 系统事件。
        """
        if decision not in {"approved", "denied", "expired"} or scope not in {
            "once",
            "run",
            "exact_rule",
        }:
            raise ToolRequestError("无效审批结果")
        if decision == "approved":
            call = self.store.entries(user, "calls", [invocation_id]).get(invocation_id)
            late_expired = (
                call is not None
                and call.get("status") == "failed"
                and call.get("permission") == "expired"
                and isinstance(call.get("terminal"), dict)
                and isinstance(call["terminal"].get("error"), dict)
                and call["terminal"]["error"].get("kind") == "permission_expired"
            )
            if call and (call["status"] == "waiting_approval" or late_expired):
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
            if call is None:
                state["controls"][command_id] = {"identity": identity, "applied": False}
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
            waiting = call["status"] == "waiting_approval"
            late_expired = (
                call["status"] == "failed"
                and call.get("permission") == "expired"
                and isinstance(call.get("terminal"), dict)
                and isinstance(call["terminal"].get("error"), dict)
                and call["terminal"]["error"].get("kind") == "permission_expired"
            )
            if not waiting and not (late_expired and decision in {"approved", "denied"}):
                state["controls"][command_id] = {"identity": identity, "applied": False}
                return False
            if late_expired and isinstance(call.get("late_permission"), dict):
                state["controls"][command_id] = {"identity": identity, "applied": False}
                return False
            state["controls"][command_id] = {"identity": identity, "applied": True}
            if state["meta"].get(f"cancelled:{call['run_id']}"):
                collected = terminal(state, call, "cancelled", error="运行已取消，未执行")
                if collected is not None:
                    produced.append(collected)
                state["controls"][command_id]["applied"] = False
                return False
            now = time.time()
            if late_expired:
                late = {
                    "status": decision,
                    "scope": scope,
                    "client_id": client_id[:128],
                    "channel": channel[:32],
                    "received_at": now,
                    "reason": reason[:500],
                    "action_digest": action_digest,
                }
                call["late_permission"] = late
                if decision == "approved":
                    self._grant(state, call, approval, scope, now, late=True)
                produced.append(
                    Inbound(
                        QueueName.SYSTEM,
                        PermissionEventType.LATE_RESOLVED,
                        {
                            "invocation_id": invocation_id,
                            "tool": call["tool"],
                            "decision": decision,
                            "scope": scope,
                            "action_digest": action_digest,
                            "original_status": "permission_expired",
                            "executed": False,
                            "retry_safe": True,
                            "late_permission": late,
                        },
                        source_id=command_id,
                        occur_dt=utc_now_iso(),
                    )
                )
                return True
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
                self._grant(state, call, approval, scope, now)
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

    @staticmethod
    def _grant(
        state: State,
        call: State,
        approval: JsonObject,
        scope: str,
        now: float,
        *,
        late: bool = False,
    ) -> None:
        if scope == "exact_rule" and approval.get("policy_managed_grant") is True:
            return
        grant_key = approval.get("grant_key")
        if not isinstance(grant_key, str) or not grant_key:
            return
        grants = state["meta"].setdefault("permission_grants", {})
        if not isinstance(grants, dict):
            grants = {}
            state["meta"]["permission_grants"] = grants
        existing = grants.get(grant_key)
        if (
            isinstance(existing, dict)
            and existing.get("scope") == "exact_rule"
            and (not existing.get("expires_at") or existing["expires_at"] > now)
        ):
            return
        grants[grant_key] = {
            "scope": scope,
            "run_id": call["run_id"] if scope in {"once", "run"} else "",
            "tool": call["tool"],
            "title": str(approval.get("title", ""))[:160],
            "created_at": now,
            "expires_at": (
                now + LATE_RETRY_TTL_SECONDS
                if scope == "once"
                else None
                if scope == "run"
                else now + 30 * 86400
            ),
            **(
                {"remaining": 1, "retry_of": call["invocation_id"]}
                if late and scope == "once"
                else {}
            ),
        }
