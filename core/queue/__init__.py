"""两条 `EventQueue` 实现。选型换掉了就换掉其中一个。"""

from core.queue.in_memory_queue import InMemoryEventQueue
from core.queue.redis_queue import RedisEventQueue

__all__ = ["InMemoryEventQueue", "RedisEventQueue"]
