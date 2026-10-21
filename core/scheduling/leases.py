"""续租失败即失去执行资格；旧 Worker 的提交还会在 Storage 再检查。"""

import logging
import threading
from contextlib import contextmanager

_log = logging.getLogger(__name__)


@contextmanager
def renewed(client, fence, ttl):
    stop = threading.Event()
    lost = threading.Event()

    def renew():
        while not stop.wait(ttl / 3):
            try:
                client.call("lease.renew", user_id=fence["user_id"], fence=fence, ttl=ttl)
            except Exception:
                lost.set()
                _log.exception("Lease renewal failed")
                return

    thread = threading.Thread(target=renew, name="oath-lease", daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stop.set()
        thread.join(timeout=ttl / 3 + 1)


class DurableWake:
    def __init__(self, manager):
        self.manager = manager

    def wake(self, user):
        self.manager.call("wake", user_id=user.storage_id)

    def resume(self, user):
        self.manager.call("wake", user_id=user.storage_id, force=True)
