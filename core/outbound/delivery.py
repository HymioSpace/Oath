"""每客户端的 Outbox 读取协议；连接不拥有任何执行状态。"""

import asyncio


class Delivery:
    def __init__(self, storage, user_id, client_id, send, poll_seconds=0.25):
        self.storage = storage
        self.user_id, self.client_id = user_id, client_id
        self.send = send
        self.poll_seconds = poll_seconds
        self.generation = 0
        self.highest_sent = 0
        self.lock = asyncio.Lock()

    async def call(self, operation, **params):
        return await asyncio.to_thread(self.storage.call, operation, user_id=self.user_id, **params)

    async def attach(self, history_limit=None):
        params = {} if history_limit is None else {"history_limit": history_limit}
        attached = await self.call("client.attach", client_id=self.client_id, **params)
        self.generation = attached["generation"]
        self.highest_sent = attached.get("delivery_cursor", attached["after_cursor"])
        return attached

    async def confirm(self, cursor):
        async with self.lock:
            if (
                not isinstance(cursor, int)
                or isinstance(cursor, bool)
                or not 0 <= cursor <= self.highest_sent
            ):
                raise ValueError("ACK exceeds this connection delivery")
            await self.call(
                "client.ack", client_id=self.client_id, generation=self.generation, seq=cursor
            )

    async def transmit(self, event):
        async with self.lock:
            # Holding the lock prevents ACK processing until this send completes.
            await self.call(
                "client.delivered",
                client_id=self.client_id,
                generation=self.generation,
                seq=event["event_seq"],
            )
            await self.send(event)
            self.highest_sent = event["event_seq"]

    async def run(self, attached):
        after = attached["after_cursor"]
        high = attached["through_cursor"]
        if "snapshot" in attached:
            async with self.lock:
                await self.call(
                    "client.delivered",
                    client_id=self.client_id,
                    generation=self.generation,
                    seq=high,
                )
                await self.send({"v": 2, "type": "conversation.snapshot", **attached["snapshot"]})
                self.highest_sent = high
        await self.send(
            {"v": 2, "type": "offline.start", "after_cursor": after, "through_cursor": high}
        )
        while after < high:
            events = await self.call("outbox.read", after=after, through=high)
            if not events:
                raise ValueError("resync_required")
            for event in events:
                await self.transmit(event)
                after = event["event_seq"]
        await self.send({"v": 2, "type": "offline.end", "through_cursor": high})
        while True:
            events = await self.call("outbox.read", after=after)
            for event in events:
                await self.transmit(event)
                after = event["event_seq"]
            if not events:
                await asyncio.sleep(self.poll_seconds)
