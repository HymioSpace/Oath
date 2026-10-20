"""Bounded workers execute one durable step per user lease."""

import logging
import threading
import uuid

from core.agent import AgentBrain
from core.protocol.datatype.user import User
from core.scheduling.leases import renewed

_log = logging.getLogger(__name__)


def run_workers(stop, manager, context_factory, provider, compaction, *, ttl, poll, workers):
    def work(index):
        owner = uuid.uuid4().hex
        while not stop.is_set():
            fence = None
            retry = False
            brain = None
            try:
                fence = manager.call(
                    "brain.claim",
                    owner=owner,
                    ttl=ttl,
                    allow_subagents=workers == 1 or index < workers - 1,
                )
                if not fence:
                    stop.wait(poll)
                    continue
                with renewed(manager, fence, ttl) as lost:
                    user = User.from_storage_id(fence["user_id"])
                    ctx = context_factory(fence)
                    brain = AgentBrain(ctx, provider, compaction)
                    step = brain.intake.claim(user)
                    if step and step["status"] == "prepared" and not lost.is_set():
                        brain.execute(user, step)
                    state = ctx.store.read(user)
                    active = state["meta"].get("active_step")
                    retry = bool(active and state["steps"][active]["status"] == "prepared")
            except Exception:
                retry = True
                _log.exception("Worker failed; durable step remains recoverable")
            finally:
                if brain:
                    brain.close()
                if fence:
                    try:
                        manager.call(
                            "brain.finish", user_id=fence["user_id"], fence=fence, retry=retry
                        )
                    except Exception:
                        _log.warning("Execution lease will expire; pending revision retained")
            stop.wait(poll)

    threads = [
        threading.Thread(target=work, args=(i,), name=f"oath-brain-{i}") for i in range(workers)
    ]
    for thread in threads:
        thread.start()
    stop.wait()
    for thread in threads:
        thread.join()
