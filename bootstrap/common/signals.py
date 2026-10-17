"""后台服务的退出信号，不参与执行状态。"""

import signal
import threading


def shutdown_event():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop
