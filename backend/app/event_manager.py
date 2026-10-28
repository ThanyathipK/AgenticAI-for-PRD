"""SYNC 8.0 — In-memory SSE event manager with bounded replay.

Provides a simple pub/sub mechanism where state changes publish SSE notifications
to connected clients. Like the rate limiter, the bus is **in-process** and safe
only for a single OS process (one uvicorn worker). If multiple workers ran, each
would keep its own subscriber set, so events published on one worker would never
reach SSE clients connected to another -- clients would see missing/duplicate
streams. To make that failure impossible to miss, :func:`verify_single_worker_guarantee`
is invoked at startup and **refuses to boot** when more than one worker is
detected (unless the operator explicitly opts into the weaker posture).

Each project also keeps a bounded ring buffer of recent events (length
``SSE_HISTORY_BUFFER_SIZE``). SSE clients can replay events they missed while
disconnected: a browser ``EventSource`` automatically reconnects with a
``Last-Event-ID`` header (the last ``id:`` field it observed), and a subscribing
client with a ``last_event_id`` is seeded with every buffered event written while
it was away. The buffer and sequence counters are process-local, so restart
loses replay history and resets ids. The server emits a ``connected`` handshake,
but SYNC 2.5.1 currently ignores it; restart catch-up is therefore not guaranteed
until another refresh-triggering event or explicit project load.
"""
import asyncio
import json
import logging
from collections import deque
from typing import Any, Deque, Dict, Optional, Set, Tuple

from app.config import settings

logger = logging.getLogger("app.event_manager")


class EventManager:
    """
    Manages SSE subscriptions per project_id.

    Each project has a set of ``asyncio.Queue`` instances (one per connected
    client) plus a bounded ring buffer of recent events that makes reconnecting
    clients recover events they missed. Queue items are ``(seq, payload)`` pairs
    where ``seq`` is a project-monotonic event id and ``payload`` is the JSON
    string ``{"event": <type>, "data": <data>}``.
    """

    def __init__(self, history_size: Optional[int] = None) -> None:
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._history: Dict[str, Deque[Tuple[int, str]]] = {}
        self._next_seq: Dict[str, int] = {}
        self._history_size = history_size if history_size is not None else settings.SSE_HISTORY_BUFFER_SIZE

    # SYNC 8.1 — Allocate an unbounded per-client queue and seed buffered frames
    #            whose process-local sequence is greater than Last-Event-ID.
    def subscribe(
        self, project_id: str, last_event_id: Optional[int] = None
    ) -> asyncio.Queue:
        """Create a new subscription queue for a project.

        When ``last_event_id`` is provided (e.g. from an SSE ``Last-Event-ID``
        reconnect), the queue is pre-seeded with every buffered event whose id is
        strictly greater, so a client that missed events while disconnected
        catches up before live delivery resumes.
        """
        if project_id not in self._subscribers:
            self._subscribers[project_id] = set()
        queue: asyncio.Queue = asyncio.Queue()
        if last_event_id is not None:
            for seq, payload in self._history.get(project_id, ()):
                if seq > last_event_id:
                    queue.put_nowait((seq, payload))
        self._subscribers[project_id].add(queue)
        logger.debug(
            f"SSE client subscribed to project {project_id} "
            f"(replay_after={last_event_id}). "
            f"Total subscribers: {len(self._subscribers[project_id])}"
        )
        return queue

    def unsubscribe(self, project_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscription queue."""
        if project_id in self._subscribers:
            self._subscribers[project_id].discard(queue)
            if not self._subscribers[project_id]:
                del self._subscribers[project_id]
            logger.debug(f"SSE client unsubscribed from project {project_id}.")

    # SYNC 8.2 — Publish one process-local project notification.
    # Trigger: successful/partially successful mutation routes and JOB effects.
    # Input: project id, event name and JSON-serializable data.
    # Writes: monotonic in-memory sequence, bounded replay, subscriber queues.
    # Branches: no subscribers → replay only; subscribers → await fan-out.
    # Failure: no durable/shared broker; unaware of DB commit order, so route
    #          publication before commit can cause a stale reconciliation pull.
    async def publish(self, project_id: str, event_type: str, data: Any) -> None:
        """Publish an event to all subscribers of a project.

        Assigns a monotonic per-project id, records the frame in the bounded ring
        buffer (so a later reconnecting client can replay it), then fans it out to
        every currently-connected subscriber.
        """
        seq = self._next_seq.get(project_id, 0) + 1
        self._next_seq[project_id] = seq
        payload = json.dumps({"event": event_type, "data": data})

        # Bounded per-project replay buffer; holds even when nobody is connected.
        history = self._history.setdefault(
            project_id, deque(maxlen=self._history_size)
        )
        history.append((seq, payload))

        if project_id not in self._subscribers:
            return

        dead_queues: Set[asyncio.Queue] = set()
        for queue in self._subscribers[project_id]:
            try:
                await queue.put((seq, payload))
            except Exception:
                dead_queues.add(queue)
        # Clean up dead queues
        for q in dead_queues:
            self._subscribers[project_id].discard(q)
        if dead_queues:
            logger.debug(
                f"Removed {len(dead_queues)} dead SSE subscriber(s) for project {project_id}."
            )


# SYNC 8.3 — Reject multi-worker use unless the explicit unsafe override is set.
def verify_single_worker_guarantee() -> None:
    """Fail loud instead of letting live updates silently split across processes.

    The SSE bus (subscriber queues + replay buffer) is process-local. With more
    than one uvicorn worker the subscription sets diverge, so events published on
    one worker never reach clients connected to another (missing/duplicate event
    streams) and the in-process replay buffer cannot span workers. The app
    refuses to boot in that case unless the operator explicitly sets
    ``SSE_ALLOW_MULTI_PROCESS_IN_PROCESS=true`` (which still logs a loud warning).
    Unlike the rate limiter's guard, this is active regardless of rate limiting,
    so the single-process contract of the event bus is always enforced at startup.
    """
    # app.rate_limit has no dependency on this module, so there is no import cycle.
    from app.rate_limit import detect_worker_count

    workers = detect_worker_count()
    if workers <= 1:
        return

    message = (
        f"backend started with {workers} uvicorn worker process(es) while the SSE "
        "event bus is in-process (process-local subscriber queues + replay buffer). "
        "Each worker keeps its own subscription set, so events published on one "
        "worker never reach SSE clients connected to another (clients see "
        "missing/duplicate event streams) and the ring-buffer replay cannot span "
        "workers. Run a single uvicorn worker (the default), or set "
        "SSE_ALLOW_MULTI_PROCESS_IN_PROCESS=true to explicitly accept this reduced "
        "posture (not recommended), or move the event bus to a shared store."
    )
    if settings.SSE_ALLOW_MULTI_PROCESS_IN_PROCESS:
        logger.warning("SSE: %s", message)
        return
    logger.error("SSE: %s", message)
    raise RuntimeError("Refusing to start for SSE reliability. " + message)


# Singleton instance
event_manager = EventManager()
