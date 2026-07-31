"""The task ledger — the spine of the agent.

Everything hangs off this: interruption-resume, honest partial reporting, and
the verification gate all read from here. It is deliberately dumb data plus a
handful of accessors; the intelligence lives in the gate and the prompt.

Keyed by *task id*, not by session or transport, so a task begun at the desk
mic can be finished over the phone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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

    def record_evidence(self, kind: str, token: str, payload: dict[str, Any]) -> None:
        """Called ONLY from tool handlers, with what the counterparty returned."""
        self.verified.setdefault(kind, {})[str(token)] = payload

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


def get_ledger(task_id: str) -> Ledger:
    """Fetch or create. Shared across transports — this is what makes the
    desk-to-phone handoff work."""
    if task_id not in _LEDGERS:
        _LEDGERS[task_id] = Ledger(task_id=task_id)
    return _LEDGERS[task_id]


def all_ledgers() -> dict[str, Ledger]:
    return _LEDGERS
