"""Consumer wake requests are scheduling intents, never direct Brain calls."""

from typing import Protocol

from core.protocol.datatype.user import User


class WakeRequests(Protocol):
    def wake(self, user: User) -> None:
        """Coalesce a pending wake if the user's durable buffers are nonempty."""
        ...

    def resume(self, user: User) -> None:
        """Schedule recovery of an explicitly retried step."""
        ...
