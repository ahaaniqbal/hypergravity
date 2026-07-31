"""In-process event bus feeding the pill and the verification panel.

Both surfaces are the same page at different sizes, so both drink from here.
A short ring buffer means a client that connects mid-call still renders the
conversation so far rather than an empty box — which matters when a judge opens
the panel after the call has already started.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Literal

EventKind = Literal["state", "user", "agent", "step", "gate", "evidence"]

_HISTORY_LIMIT = 200
_history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_LIMIT)
_subscribers: set[asyncio.Queue] = set()


def history() -> list[dict[str, Any]]:
    return list(_history)


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def emit(kind: EventKind, **payload: Any) -> None:
    """Fire and forget. Never blocks the audio pipeline, never raises."""
    event = {"kind": kind, "at": time.time(), **payload}
    _history.append(event)
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # A stalled browser tab must not back up a live call.
            _subscribers.discard(q)


# -- convenience wrappers, so callers read declaratively ------------------

def state(name: str, detail: str = "") -> None:
    """idle | listening | thinking | acting | speaking | blocked"""
    emit("state", name=name, detail=detail)


def heard(text: str, final: bool = True) -> None:
    emit("user", text=text, final=final)


def said(text: str) -> None:
    emit("agent", text=text)


def step(name: str, status: str, detail: str = "") -> None:
    emit("step", name=name, status=status, detail=detail)


def gate(verdict: str, reason: str, token: str = "") -> None:
    emit("gate", verdict=verdict, reason=reason, token=token)


def evidence(kind: str, token: str, how_to_check: str) -> None:
    emit("evidence", evidence_kind=kind, token=token, how_to_check=how_to_check)
