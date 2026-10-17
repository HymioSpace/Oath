"""Durable producer contract. Delivery and ACK belong to the broker consumer."""

from typing import Protocol

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.user import User


class EventQueue(Protocol):
    def add(self, user: User, item: Inbound) -> None:
        """Return after durable acceptance; retries must keep the input identity."""
        ...
