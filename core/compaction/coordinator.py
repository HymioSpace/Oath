"""压缩的时机与生效：一个 user 一次只备一份产物，固定请求之前决定本次发什么。

异步那条只在后台算，不进任何请求；同步那条会调用模型，因此只能发生在请求保存事务之外。
事务里只做纯数据重组 —— 锁内不跑模型。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from core.compaction.boundary import cut_index
from core.compaction.view import APPLIED, PENDING, compacted, usable_artifact
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.message import MessageEvent
from core.protocol.datatype.user import User
from core.protocol.interface.compaction import Compactor
from core.protocol.interface.event import AgentEventSink
from core.scheduling.leases import renewed
from core.utils.encoding import utc_now_iso
from core.utils.tokens import estimate

_log = logging.getLogger(__name__)


class CompactionCoordinator:
    """按 user 判断阈值、跑压缩、管产物的生效。"""

    def __init__(
        self,
        store: RuntimeStore,
        compactor: Compactor,
        config: AgentConfig,
        *,
        sink: AgentEventSink | None = None,
        workers: int = 2,
        lease_seconds: float = 60,
    ) -> None:
        self._store, self._compactor, self._config = store, compactor, config
        self._sink = sink
        self._lock = threading.Lock()
        self._inflight: set[User] = set()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="oath-compact")
        self._slots = threading.BoundedSemaphore(workers)
        self._lease_seconds = lease_seconds

    def view(self, messages: list[State], state: State) -> list[State]:
        """当前生效的视图。纯函数，可以在事务里调。"""
        return compacted(messages, state)

    def prepare(
        self, user: User, *, overhead: int = 0, system_prompt: str | None = None, fence=None
    ) -> None:
        """固定请求之前调整 context。到同步阈值就生效，到异步阈值就备一份。

        任何失败都只是「没压」：压缩是优化，不该让一个 step 因为它失败。
        """
        if self._stop.is_set():
            return
        try:
            messages, state = self._history(user)
            used = self._usage(messages, state, system_prompt) + overhead
            if used >= self._sync_limit():
                with self._store.fenced(fence):
                    self._apply_pending(user)
                messages, state = self._history(user)
                if self._usage(messages, state, system_prompt) + overhead >= self._sync_limit():
                    self._compress(user, applied=True)
                    with self._store.fenced(fence):
                        self._apply_pending(user)
                messages, state = self._history(user)
                used = self._usage(messages, state, system_prompt) + overhead
            if used >= self._async_limit() and self._reserve(user, messages, state):
                self._pool.submit(self._background, user)
        except Exception:
            _log.exception("上下文压缩判定失败，保留原历史")

    def shutdown(self) -> None:
        """停止备产物。在跑的线程不打断，落盘前会再确认一次。"""
        self._stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)

    # -- 内部 --------------------------------------------------------------

    def _history(self, user: User) -> tuple[list[State], State]:
        state = self._store.read(user)
        return list(state["conversation"].get("messages", [])), state

    def _usage(self, messages: list[State], state: State, system_prompt: str | None = None) -> int:
        view = compacted(messages, state)
        prompt = self._config.system_prompt if system_prompt is None else system_prompt
        if prompt:
            view = [{"role": "system", "content": prompt}, *view]
        return estimate(view)

    def _async_limit(self) -> float:
        return self._config.context_window * self._config.compress_async_at_ratio

    def _sync_limit(self) -> float:
        return self._config.context_window * self._config.compress_sync_at_ratio

    def _reserve(self, user: User, messages: list[State], state: State) -> bool:
        """登记一个在跑的异步压缩。同一个 user 一次只有一个。

        已经有可用的待生效产物就不再起新的 —— 正在备的那份就是为这次准备的。
        """
        current = usable_artifact(state, APPLIED, messages)
        pending = usable_artifact(state, PENDING, messages)
        if pending is not None and (current is None or pending[1] > current[1]):
            return False
        cut = cut_index(messages, self._config.compress_keep_recent)
        if cut is None or (current is not None and cut <= current[1]):
            return False
        with self._lock:
            if user in self._inflight:
                return False
            if not self._slots.acquire(blocking=False):
                return False
            self._inflight.add(user)
        return True

    def _background(self, user: User) -> None:
        try:
            self._compress(user, applied=False)
        except Exception:
            # 备产物失败只是这次没有产物；不能让线程把异常抛到顶上。
            _log.exception("异步压缩失败，保留原历史")
        finally:
            with self._lock:
                self._inflight.discard(user)
            self._slots.release()

    def _apply_pending(self, user: User) -> bool:
        """有可用产物就生效。不调用模型。"""

        def apply(state: State) -> bool:
            messages = state["conversation"].get("messages", [])
            current = usable_artifact(state, APPLIED, messages)
            pending = usable_artifact(state, PENDING, messages)
            if pending is None or (current is not None and pending[1] <= current[1]):
                return False
            state["compactions"][APPLIED] = dict(pending[0])
            return True

        applied = self._store.transact(user, "context.compression_applied", apply)
        if applied:
            saved = self._store.read(user)["compactions"][APPLIED]
            self._emit(user, saved.get("compression_id", ""), "applied", "pending", saved["covers"])
        return applied

    def _compress(self, user: User, *, applied: bool) -> None:
        # Independent of the Brain lease: a prepared summary survives the current step.
        deadline = time.monotonic() + self._config.provider.timeout_seconds + self._lease_seconds
        owner = uuid.uuid4().hex
        while not self._stop.is_set():
            fence = self._store.storage.call(
                "lease.acquire",
                user_id=user.storage_id,
                lane="compaction",
                owner=owner,
                ttl=self._lease_seconds,
            )
            if fence:
                break
            if not applied or time.monotonic() >= deadline:
                return
            self._stop.wait(0.25)
        else:
            return
        try:
            with (
                renewed(self._store.storage, fence, self._lease_seconds),
                self._store.fenced(fence),
            ):
                self._compress_owned(user, applied=applied)
        finally:
            try:
                self._store.storage.call("lease.release", user_id=user.storage_id, fence=fence)
            except Exception:
                _log.warning("Compaction lease released by expiry")

    def _compress_owned(self, user: User, *, applied: bool) -> None:
        messages, snapshot = self._history(user)
        cut = cut_index(messages, self._config.compress_keep_recent)
        if cut is None:
            return
        current = usable_artifact(snapshot, APPLIED, messages)
        covered = current[1] if current else 0
        if cut <= covered:
            return
        segment = ([*current[0]["messages"]] if current else []) + messages[covered:cut]
        budget = self._config.compress_summary_tokens
        # 压出来的比被压掉的还长，这一步没有任何意义。
        if estimate(segment) < budget:
            return
        identity = uuid.uuid4().hex
        mode = "sync" if applied else "async"
        self._emit(user, identity, "started", mode, cut)
        try:
            replacement = self._compactor.compress(segment, budget_tokens=budget)
        except Exception:
            _log.exception("压缩算法失败，保留原历史")
            self._emit(user, identity, "failed", mode, cut, "algorithm_error")
            return
        if not replacement or estimate(replacement) >= estimate(segment):
            self._emit(user, identity, "skipped", mode, cut, "no_smaller_summary")
            return
        if self._stop.is_set():
            return

        def save(state: State) -> bool:
            key = PENDING
            current = usable_artifact(state, APPLIED, state["conversation"].get("messages", []))
            if current is not None and current[1] >= cut:
                return False  # 不退回更少的历史
            state["compactions"][key] = {
                "covers": cut,
                "messages": replacement,
                "created_dt": utc_now_iso(),
                "compression_id": identity,
            }
            return True

        try:
            saved = self._store.transact(user, "context.compressed", save)
        except Exception:
            self._emit(user, identity, "failed", mode, cut, "storage_error")
            raise
        status = "ready" if saved else "skipped"
        self._emit(user, identity, status, mode, cut)

    def _emit(
        self,
        user: User,
        identity: str,
        status: str,
        mode: str,
        covers: int,
        reason: str | None = None,
    ) -> None:
        if self._sink is None or self._stop.is_set():
            return
        try:
            self._sink.emit(
                MessageEvent(
                    type="context.compaction",
                    user=user,
                    payload={
                        "compression_id": identity,
                        "status": status,
                        "mode": mode,
                        "covers": covers,
                        "reason": reason,
                    },
                    idempotency_key=f"compaction:{identity}:{status}",
                    occur_dt=utc_now_iso(),
                )
            )
        except Exception:
            _log.exception("压缩事件推送失败，不改变已保存产物")


__all__ = ["CompactionCoordinator"]
