"""In-process async event bus.

The agent publishes "thoughts", trades, and lifecycle events here. The WebSocket
layer subscribes and streams them to the dashboard. Keeping this in-process is fine
for the MVP; it can be swapped for Redis/NATS later without touching publishers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator


class EventType(str, Enum):
    THOUGHT = "thought"          # free-form reasoning from the agent
    TOKEN_SEEN = "token_seen"    # a new/updated token entered the pipeline
    DECISION = "decision"        # buy / skip decision with rationale
    TRADE = "trade"              # a fill (open or close)
    POSITION = "position"        # position update (mark-to-market)
    REFLECTION = "reflection"    # periodic learning output
    ADVICE = "advice"            # user advice + the agent's own verdict on it
    TWEET = "tweet"              # something the agent posted (or would post) to X
    STATUS = "status"            # portfolio / system status
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, "ts": self.ts, "data": self.data}


class EventBus:
    def __init__(self, history_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: list[Event] = []
        self._history_size = history_size

    async def publish(self, event: Event) -> None:
        self._history.append(event)
        if len(self._history) > self._history_size:
            self._history = self._history[-self._history_size :]
        for q in list(self._subscribers):
            # Drop for slow consumers rather than blocking the agent loop.
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await q.put(event)

    async def emit(self, type: EventType, **data: Any) -> None:
        await self.publish(Event(type=type, data=data))

    def history(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._history]

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)


# Global bus shared across the app.
bus = EventBus()
