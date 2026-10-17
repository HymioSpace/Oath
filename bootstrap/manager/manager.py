"""Manager 只安排执行资格；用户状态与 pending wake 均在持久存储。"""


class Manager:
    def __init__(self, storage):
        self.storage = storage

    def call(self, operation, **params):
        if operation not in {"wake", "brain.claim", "brain.finish", "lease.renew", "lease.release"}:
            raise ValueError("Unknown scheduling operation")
        return self.storage.call(operation, **params)
