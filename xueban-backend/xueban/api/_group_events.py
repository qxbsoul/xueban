"""Group chat SSE event bus — per-group in-process pub/sub.

Mirrors the approval event bus (`xueban.runtime.approval.service`) but
buckets subscribers by ``group_id`` so a chat room only receives its own
events. Used to push asynchronously-produced agent replies (from the
``_chat_bridge`` background task) to the browser in real time, instead of
relying on the client to poll ``GET /api/groups/{id}/messages``.

Design notes
------------
- **In-process only**: a module-level ``Dict[group_id, List[asyncio.Queue]]``.
  Under a multi-worker deployment, events published in one process won't reach
  SSE subscribers in another — the same limitation the approval / workflow SSE
  buses already have. Acceptable for the single-uvicorn-process MVP.
- **Best-effort delivery**: full queues are skipped (``QueueFull`` swallowed)
  rather than blocking the publisher (a background task that must not stall).
- **Self-cleaning**: when a group's last subscriber unsubscribes, its bucket is
  removed so the dict doesn't grow unbounded.

Event shape: ``{"id": <seq>, "type": "agent.message" | "system.notice" | "agent.typing",
"data": {...}}``. The SSE endpoint serialises ``data`` as the SSE ``data:``
line, ``type`` as the ``event:`` line and ``id`` as the ``id:`` line.

History
-------
2026-05-29 — Created for the real-chat (DM + group) work: closes the gap where
agent replies landed in the group JSON file but the UI never saw them without a
manual refresh.
2026-11-07 — Disconnect replay (mirror of Node groups-event-bus.ts): every
event gets a per-group monotonic ``id`` and replayable events land in a bounded
ring buffer so a client that lost its SSE connection can catch up via the
``Last-Event-ID`` cursor. ``agent.typing`` frames are transient (they get an
id but are never buffered). Publishing with zero subscribers no longer drops
the event — it stays in the buffer for the reconnecting client.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# group_id -> list of subscriber queues
_subscribers: Dict[str, List[asyncio.Queue]] = {}

# Bounded so a slow/dead client can't grow memory without limit.
_QUEUE_MAXSIZE = 100

# ── Disconnect replay: per-group seq + ring buffer (2026-11-07) ──────────────
_REPLAY_BUFFER_MAX = 100  # keep the newest 100 replayable events per group
_REPLAY_SKIP_TYPES = {"agent.typing"}  # transient hint frames: id but no replay
_MAX_TRACKED_GROUPS = 200  # safety valve: evict whole buffers FIFO beyond this

_seq_counters: Dict[str, int] = {}
_replay_buffers: Dict[str, Deque[Dict[str, Any]]] = {}


def subscribe(group_id: str) -> asyncio.Queue:
    """Register a new subscriber queue for ``group_id`` and return it."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _subscribers.setdefault(group_id, []).append(q)
    return q


def unsubscribe(group_id: str, q: asyncio.Queue) -> None:
    """Remove a subscriber queue; drop the bucket when it becomes empty.

    The replay buffer is intentionally kept (it is the recovery source for a
    reconnecting client); only the subscriber bucket is cleaned up.
    """
    queues = _subscribers.get(group_id)
    if not queues:
        return
    try:
        queues.remove(q)
    except ValueError:
        pass
    if not queues:
        _subscribers.pop(group_id, None)


def publish_group_event(group_id: str, event: Dict[str, Any]) -> int:
    """Push an event to all subscribers of ``group_id``.

    Best-effort: full queues are skipped so a stuck client never blocks the
    background dispatch task.

    Returns the assigned per-group monotonic ``id`` (0-based, resets when the
    process restarts). The event is delivered as a copy carrying that ``id``.

    Since 2026-11-07 the event is also appended to a bounded replay buffer
    (except transient ``agent.typing`` frames) even when nobody is listening,
    so frames published during a client disconnect can be replayed on
    reconnect via :func:`get_group_events_after`.
    """
    seq = _seq_counters.get(group_id, 0)
    _seq_counters[group_id] = seq + 1
    payload = {**event, "id": seq}

    if event.get("type") not in _REPLAY_SKIP_TYPES:
        buf = _replay_buffers.get(group_id)
        if buf is None:
            if len(_replay_buffers) >= _MAX_TRACKED_GROUPS:
                # Map/dict insertion order: the first key is the least recently
                # published group — evict its buffer wholesale.
                stale = next(iter(_replay_buffers), None)
                if stale is not None:
                    _replay_buffers.pop(stale, None)
            buf = deque(maxlen=_REPLAY_BUFFER_MAX)
            _replay_buffers[group_id] = buf
        buf.append(payload)

    for q in list(_subscribers.get(group_id, [])):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
    return seq


def get_group_events_after(
    group_id: str, cursor: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Replay set for a reconnecting client: buffered events with ``id > cursor``.

    - ``cursor=None`` (fresh connection without a cursor) → replay the whole
      buffer. The client dedupes by ``message_id`` so duplicates are harmless,
      and this also covers reconnects whose ``Last-Event-ID`` never got set
      (connection dropped while the group was idle).
    - ``cursor`` greater than the current high-water mark (a stale cursor from
      before a server restart reset the seqs) → treated as ``None``.
    - Evicted frames cannot be recovered; only what remains is replayed.
    """
    buf = _replay_buffers.get(group_id, ())
    latest = _seq_counters.get(group_id, 0) - 1
    effective = cursor if (cursor is not None and cursor <= latest) else -1
    return [e for e in buf if e["id"] > effective]


def latest_seq(group_id: str) -> int:
    """High-water mark (id of the last assigned event; -1 when none). Tests."""
    return _seq_counters.get(group_id, 0) - 1


def subscriber_count(group_id: str) -> int:
    """Number of live subscribers for a group (used by tests)."""
    return len(_subscribers.get(group_id, []))


def reset_for_tests() -> None:
    """Drop all bus state (subscribers, seqs, replay buffers). Tests only."""
    _subscribers.clear()
    _seq_counters.clear()
    _replay_buffers.clear()
