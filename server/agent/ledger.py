"""The task ledger — the spine of the agent.

Everything hangs off this: interruption-resume, honest partial reporting, and
the verification gate all read from here. It is deliberately dumb data plus a
handful of accessors; the intelligence lives in the gate and the prompt.

Keyed by *task id*, not by session or transport, so a task begun at the desk
mic can be finished over the phone.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from . import events


class StepState(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Step:
    name: str
    state: StepState = StepState.PENDING
    detail: str = ""


@dataclass
class Ledger:
    """One user task, tracked across however many transports touch it."""

    task_id: str
    goal: str = ""
    caller_phone: str = ""

    # What the user asked for, as we currently understand it.
    party_name: str = ""
    party_size: int = 0
    requested_slot: str = ""

    steps: list[Step] = field(default_factory=list)

    # Facts we have INDEPENDENT evidence for. Only the tool layer writes here —
    # never the model. This is what the gate checks against.
    verified: dict[str, Any] = field(default_factory=dict)

    created_at: float = field(default_factory=time.time)

    # -- steps ------------------------------------------------------------

    def step(self, name: str) -> Step:
        for s in self.steps:
            if s.name == name:
                return s
        s = Step(name)
        self.steps.append(s)
        return s

    def mark(self, name: str, state: StepState, detail: str = "") -> None:
        s = self.step(name)
        s.state = state
        if detail:
            s.detail = detail
        events.step(name, state.value, s.detail)
        save()

    @property
    def pending(self) -> list[str]:
        return [s.name for s in self.steps if s.state is StepState.PENDING]

    @property
    def done(self) -> list[str]:
        return [s.name for s in self.steps if s.state is StepState.DONE]

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.state is StepState.FAILED]

    # -- evidence ---------------------------------------------------------

    def note(
        self,
        *,
        name: str | None = None,
        party_size: int | None = None,
        slot: str | None = None,
        phone: str | None = None,
    ) -> None:
        """Record what we've learned about the request, and persist it.

        Details used to be assigned as plain attributes, which never hit disk:
        only ``mark`` and ``record_evidence`` save. A call that took the caller's
        name and then handed over mid-flight lost it, and the next leg asked for
        it again — exactly the re-interrogation the handoff exists to avoid.
        """
        if name:
            self.party_name = name
        if party_size:
            self.party_size = party_size
        if slot:
            self.requested_slot = slot
        if phone:
            self.caller_phone = phone
        save()

    def record_evidence(self, kind: str, token: str, payload: dict[str, Any]) -> None:
        """Called ONLY from tool handlers, with what the counterparty returned."""
        self.verified.setdefault(kind, {})[str(token)] = payload
        save()

    def has_evidence(self, kind: str, token: str) -> bool:
        return str(token) in self.verified.get(kind, {})

    def evidence(self, kind: str, token: str) -> dict[str, Any] | None:
        return self.verified.get(kind, {}).get(str(token))

    # -- what the model is allowed to see ---------------------------------

    def summary(self) -> str:
        """Injected into context so the agent can resume mid-task after a
        transport switch or an interruption, without re-asking everything."""
        lines = [f"TASK {self.task_id}: {self.goal or '(not yet stated)'}"]
        if self.party_name or self.party_size or self.requested_slot:
            lines.append(
                f"  details: name={self.party_name or '?'} "
                f"party_size={self.party_size or '?'} slot={self.requested_slot or '?'}"
            )
        for s in self.steps:
            flag = {"done": "x", "pending": " ", "failed": "!"}[s.state.value]
            lines.append(f"  [{flag}] {s.name}{f' — {s.detail}' if s.detail else ''}")
        if self.verified:
            for kind, items in self.verified.items():
                lines.append(f"  VERIFIED {kind}: {', '.join(items)}")
        else:
            lines.append("  VERIFIED: nothing yet — you may not claim success")
        return "\n".join(lines)


_LEDGERS: dict[str, Ledger] = {}

# The phone bot and the VoiceOS MCP server are separate processes — VoiceOS
# launches ours over stdio — so the ledger has to live on disk for a task begun
# at the desk to be finished on the phone.
STORE = Path(__file__).resolve().parent.parent / ".ledgers.json"


def _serialise(led: Ledger) -> dict[str, Any]:
    return {
        "task_id": led.task_id,
        "goal": led.goal,
        "caller_phone": led.caller_phone,
        "party_name": led.party_name,
        "party_size": led.party_size,
        "requested_slot": led.requested_slot,
        "steps": [{"name": s.name, "state": s.state.value, "detail": s.detail} for s in led.steps],
        "verified": led.verified,
        "created_at": led.created_at,
    }


def _deserialise(d: dict[str, Any]) -> Ledger:
    led = Ledger(
        task_id=d["task_id"],
        goal=d.get("goal", ""),
        caller_phone=d.get("caller_phone", ""),
        party_name=d.get("party_name", ""),
        party_size=d.get("party_size", 0),
        requested_slot=d.get("requested_slot", ""),
        verified=d.get("verified", {}),
        created_at=d.get("created_at", time.time()),
    )
    led.steps = [Step(s["name"], StepState(s["state"]), s.get("detail", "")) for s in d.get("steps", [])]
    return led


def save() -> None:
    """Best effort. A failed write must never break a live call."""
    try:
        STORE.write_text(json.dumps({k: _serialise(v) for k, v in _LEDGERS.items()}, indent=2))
    except OSError:
        pass


def _load() -> None:
    try:
        raw = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for task_id, d in raw.items():
        if task_id not in _LEDGERS:
            _LEDGERS[task_id] = _deserialise(d)


def get_ledger(task_id: str) -> Ledger:
    """Fetch or create. Shared across transports *and processes* — this is what
    makes the desk-to-phone handoff work."""
    _load()
    if task_id not in _LEDGERS:
        _LEDGERS[task_id] = Ledger(task_id=task_id)
    return _LEDGERS[task_id]


def all_ledgers() -> dict[str, Ledger]:
    _load()
    return _LEDGERS


# A task counts as still open only while there is real work outstanding. A
# finished one must never be resumed: a caller who says "hello" on a fresh call
# should not have last hour's booking driven to completion around them, which is
# exactly what happened when any non-empty ledger was treated as resumable.
RESUME_WINDOW_SECONDS = 30 * 60


def open_task(base: str = "hypergravity-live", now: float | None = None) -> Ledger:
    """The task a new caller should land on.

    Resumes an existing one only when it is recent, has been started, and has
    no verified booking yet — the genuine mid-flight case the desk-to-phone
    handoff is for. Otherwise starts a fresh task so the call begins clean.
    """
    _load()
    now = now or time.time()

    for led in _LEDGERS.values():
        if not led.task_id.startswith(base):
            continue
        if led.verified.get("booking"):
            continue  # done — nothing to resume
        if not led.steps:
            continue  # never started
        if now - led.created_at > RESUME_WINDOW_SECONDS:
            continue  # stale
        return led

    # Second-resolution ids collide when two calls land in the same second, and
    # the second one silently overwrites the first task's ledger.
    fresh = f"{base}-{int(now)}-{uuid.uuid4().hex[:6]}"
    _LEDGERS[fresh] = Ledger(task_id=fresh, created_at=now)
    return _LEDGERS[fresh]


def reset() -> None:
    """Clear everything. For rehearsing the demo from a known-clean state."""
    _LEDGERS.clear()
    try:
        STORE.unlink()
    except OSError:
        pass
