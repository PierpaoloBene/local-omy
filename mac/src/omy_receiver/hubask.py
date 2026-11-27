"""The live side of "Chiedi" on the hub: the Mac streams its events to the receiver, which relays them to the
iPhone's open SSE request (docs/hub-migration.md, 1.3 and decision 2). If nobody is listening, or the Mac is off,
the question is queued in omy.asks and the answer is kept for later."""

from __future__ import annotations

import asyncio

START_WAIT = 12.0     # the Mac is online: wait this long for the first event (the poll answers at once)
IDLE_WAIT = 90.0      # then this long between two events (the app's own limit)
QUEUED_TEXT = "Il Mac non risponde: la domanda resta in coda e la risposta arriva appena si riaccende."


class Live:
    """ask id -> the queue of events of the iPhone request waiting for it. One event loop: no locks."""

    def __init__(self) -> None:
        self.queues: dict[str, asyncio.Queue] = {}

    def open(self, ask_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.queues[ask_id] = q
        return q

    def close(self, ask_id: str) -> None:
        self.queues.pop(ask_id, None)

    def publish(self, ask_id: str, event: dict) -> bool:
        q = self.queues.get(ask_id)
        if q is None:
            return False
        q.put_nowait(event)
        return True


def queued_events(ask_id: str) -> list[dict]:
    """What the iPhone is told when the question goes in the queue. The `queued:<id>` stage is how the app learns
    the id (old apps show the delta as the answer)."""
    return [{"type": "status", "stage": f"queued:{ask_id}"}, {"type": "citations", "items": []},
            {"type": "delta", "text": QUEUED_TEXT},
            {"type": "done", "not_found": False, "used": [], "timings": {}, "queued": True, "ask_id": ask_id}]


async def relay(live: Live, ask_id: str, online: bool):
    """Async generator of the events for the iPhone: the Mac's, or the queued notice."""
    q = live.open(ask_id)
    try:
        yield {"type": "status", "stage": "searching"}
        first = True
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), (START_WAIT if online else 0.0) if first else IDLE_WAIT)
            except (TimeoutError, asyncio.TimeoutError):
                for e in queued_events(ask_id):
                    yield e
                return
            first = False
            yield ev
            if ev.get("type") in ("done", "error"):
                return
    finally:
        live.close(ask_id)
