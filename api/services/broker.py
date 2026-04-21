import asyncio
from contextlib import asynccontextmanager
from typing import Any


class BrokerFull(Exception):
    """Raised when subscriber count would exceed ``max_subscribers``."""


class SnapshotBroker:
    """Fan-out broker for live snapshot events. Subscribers each get their
    own bounded queue so a slow reader can't back up the poller."""

    def __init__(self, queue_size: int = 256, max_subscribers: int = 256):
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest to keep the stream moving for other subscribers.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    @asynccontextmanager
    async def subscribe(self):
        if len(self._subscribers) >= self._max_subscribers:
            raise BrokerFull(
                f"Too many SSE subscribers (max {self._max_subscribers})"
            )
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
