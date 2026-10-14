"""压缩的时机与生效：一个 scope 一次只备一份产物，认领之前决定本次发什么。

异步那条只在后台算，不进任何请求；同步那条会调用模型，因此只能发生在认领事务**之前**。
事务里只做纯数据重组 —— 锁内不跑模型。
"""

from __future__ import annotations

import logging
import threading

from core.compaction.boundary import cut_index
from core.compaction.tokens import estimate
from core.compaction.view import APPLIED, PENDING, compacted, usable_artifact
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.scope import Scope
from core.protocol.interface.compaction import Compactor
from core.utils.encoding import utc_now_iso

_log = logging.getLogger(__name__)


class CompactionCoordinator:
    """按 scope 判断阈值、跑压缩、管产物的生效。"""

    def __init__(self, store: RuntimeStore, compactor: Compactor, config: AgentConfig) -> None:
        self._store, self._compactor, self._config = store, compactor, config
        self._lock = threading.Lock()
        self._inflight: set[Scope] = set()
        self._stop = threading.Event()

    def view(self, messages: list[State], state: State) -> list[State]:
        """当前生效的视图。纯函数，可以在事务里调。"""
        return compacted(messages, state)

    def prepare(self, scope: Scope) -> None:
        """认领之前调整 context。到同步阈值就生效，到异步阈值就备一份。

        任何失败都只是「没压」：压缩是优化，不该让一个 step 因为它失败。
        """
        if self._stop.is_set():
            return
        try:
            messages, state = self._history(scope)
            used = self._usage(messages, state)
            if used >= self._sync_limit():
                if not self._apply_pending(scope):
                    self._compress(scope, applied=True)
                messages, state = self._history(scope)
                used = self._usage(messages, state)
            if used >= self._async_limit() and self._reserve(scope, messages, state):
                threading.Thread(
                    target=self._background, args=(scope,), name="oath-compact", daemon=True
                ).start()
        except Exception:
            _log.exception("上下文压缩判定失败，保留原历史")

    def shutdown(self) -> None:
        """停止备产物。在跑的线程不打断，落盘前会再确认一次。"""
        self._stop.set()

    # -- 内部 --------------------------------------------------------------

    def _history(self, scope: Scope) -> tuple[list[State], State]:
        state = self._store.read(scope)
        return list(state["conversation"].get("messages", [])), state

    def _usage(self, messages: list[State], state: State) -> int:
        view = compacted(messages, state)
        if self._config.system_prompt:
            view = [{"role": "system", "content": self._config.system_prompt}, *view]
        return estimate(view)

    def _async_limit(self) -> float:
        return self._config.context_window * self._config.compress_async_at_ratio

    def _sync_limit(self) -> float:
        return self._config.context_window * self._config.compress_sync_at_ratio

    def _reserve(self, scope: Scope, messages: list[State], state: State) -> bool:
        """登记一个在跑的异步压缩。同一个 scope 一次只有一个。

        已经有可用的待生效产物就不再起新的 —— 正在备的那份就是为这次准备的。
        """
        current = usable_artifact(state, APPLIED, messages)
        pending = usable_artifact(state, PENDING, messages)
        if pending is not None and (current is None or pending[1] > current[1]):
            return False
        with self._lock:
            if scope in self._inflight:
                return False
            self._inflight.add(scope)
        return True

    def _background(self, scope: Scope) -> None:
        try:
            self._compress(scope, applied=False)
        except Exception:
            # 备产物失败只是这次没有产物；不能让线程把异常抛到顶上。
            _log.exception("异步压缩失败，保留原历史")
        finally:
            with self._lock:
                self._inflight.discard(scope)

    def _apply_pending(self, scope: Scope) -> bool:
        """有可用产物就生效。不调用模型。"""

        def apply(state: State) -> bool:
            messages = state["conversation"].get("messages", [])
            current = usable_artifact(state, APPLIED, messages)
            pending = usable_artifact(state, PENDING, messages)
            if pending is None or (current is not None and pending[1] <= current[1]):
                return False
            state["compactions"][APPLIED] = dict(pending[0])
            return True

        return self._store.transact(scope, "context.compression_applied", apply)

    def _compress(self, scope: Scope, *, applied: bool) -> None:
        messages, _ = self._history(scope)
        cut = cut_index(messages, self._config.compress_keep_recent)
        if cut is None:
            return
        segment = messages[:cut]
        budget = self._config.compress_summary_tokens
        # 压出来的比被压掉的还长，这一步没有任何意义。
        if estimate(segment) < budget:
            return
        try:
            replacement = self._compactor.compress(segment, budget_tokens=budget)
        except Exception:
            _log.exception("压缩算法失败，保留原历史")
            return
        if not replacement or estimate(replacement) >= estimate(segment):
            return
        if self._stop.is_set():
            return

        def save(state: State) -> None:
            key = APPLIED if applied else PENDING
            current = usable_artifact(state, APPLIED, state["conversation"].get("messages", []))
            if applied and current is not None and current[1] > cut:
                return  # 不退回更少的历史
            state["compactions"][key] = {
                "covers": cut,
                "messages": replacement,
                "created_dt": utc_now_iso(),
            }

        self._store.transact(scope, "context.compressed", save)


__all__ = ["CompactionCoordinator"]
