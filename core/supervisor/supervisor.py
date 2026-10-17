"""无用户常驻线程的周期计划；到期与投递在 Storage 一次事务提交。"""


class Supervisor:
    def __init__(self, storage, interval):
        self.storage, self.interval = storage, interval

    def tick(self):
        return self.storage.call("supervisor.tick", interval=self.interval)
