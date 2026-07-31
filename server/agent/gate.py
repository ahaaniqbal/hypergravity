"""The verification gate.

The event's one disqualifying failure is a fabricated success. So the agent has
no free-form way to say "done": it must call ``claim_success`` with a token, and
a token is only valid if the *tool layer* recorded it — never if the model
merely produced it in text.

Every decision is logged so the dashboard can show a judge the moment a claim
was refused. That refusal is the strongest thing we can demonstrate.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import os

from loguru import logger

from . import events
from .ledger import Ledger

LOG_PATH = Path(__file__).resolve().parent.parent / "verification_log.jsonl"
REST_BASE = os.getenv("A1_BASE", "https://hack.a1mobile.com")

Verdict = Literal["ALLOWED", "BLOCKED"]


@dataclass
class GateDecision:
    at: float
    task_id: str
    verdict: Verdict
    kind: str
    token: str
    reason: str
    evidence: dict[str, Any] | None = None

    def line(self) -> str:
        head = "success claim ALLOWED" if self.verdict == "ALLOWED" else "success claim BLOCKED"
        return f"{head} — {self.reason}"


_DECISIONS: list[GateDecision] = []


def decisions() -> list[GateDecision]:
    return _DECISIONS


def _record(d: GateDecision) -> None:
    _DECISIONS.append(d)
    logger.bind(gate=True).info(d.line())
    events.gate(d.verdict, d.reason, d.token)
    if d.verdict == "ALLOWED" and d.evidence:
        events.evidence(d.kind, d.token, _how_to_check(d.kind, d.token))
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(asdict(d)) + "\n")
    except OSError:  # never let logging break a live call
        pass


def _how_to_check(kind: str, token: str) -> str:
    """Told to the judge, so they can confirm without taking our word."""
    if kind == "booking":
        return f"curl -H 'X-Team-Key: <key>' {REST_BASE}/api/bookings  → look for id {token}"
    if kind == "sms":
        return f"check the handset at {token} for the confirmation text"
    if kind == "calendar":
        return "open Calendar.app — the event is in the Home calendar, no credentials needed"
    return token


class FabricationBlocked(Exception):
    """Raised when the agent tries to claim something it cannot evidence.

    The message is written to be spoken: it is handed back to the model as the
    tool result so the model's only remaining move is an honest report.
    """


def claim_success(ledger: Ledger, kind: str, token: str) -> dict[str, Any]:
    """The ONLY way the agent may assert a task completed.

    ``token`` is the booking identifier the counterparty returned; it must
    already be in the ledger, put there by a tool handler that saw the
    counterparty's own response.

    The claim is made **once, for the task**, against the booking — not once per
    side effect. Gating each artifact separately invited the model to guess which
    token belonged to which channel, and a wrong guess made it disown work that
    had genuinely landed. Under-reporting is safer than lying, but it is still
    wrong. The extra artifacts ride along as evidence instead.
    """
    token = str(token or "").strip()

    if not token:
        d = GateDecision(
            at=time.time(),
            task_id=ledger.task_id,
            verdict="BLOCKED",
            kind=kind,
            token="",
            reason="no verification token supplied; downgraded to partial-completion report",
        )
        _record(d)
        raise FabricationBlocked(
            "You have no confirmation identifier, so you may not say this was completed. "
            "Tell the caller plainly what did and did not happen, and what you will try next."
        )

    evidence = ledger.evidence(kind, token)
    if evidence is None:
        d = GateDecision(
            at=time.time(),
            task_id=ledger.task_id,
            verdict="BLOCKED",
            kind=kind,
            token=token,
            reason=(
                f"token {token!r} was never recorded by the tool layer for {kind!r} — "
                "the model produced it, the counterparty did not; "
                "downgraded to partial-completion report"
            ),
        )
        _record(d)
        raise FabricationBlocked(
            "That confirmation number is not one the restaurant actually gave us, so you "
            "may not report success. Say honestly that the booking is not confirmed."
        )

    d = GateDecision(
        at=time.time(),
        task_id=ledger.task_id,
        verdict="ALLOWED",
        kind=kind,
        token=token,
        reason=f"{kind} {token} verified by independent read-back",
        evidence=evidence,
    )
    _record(d)

    # Everything else that independently verified, so the agent can mention the
    # text and the calendar entry without making a second claim for each.
    also = {k: list(v) for k, v in ledger.verified.items() if k != kind}
    return {"ok": True, "kind": kind, "token": token, "evidence": evidence, "also_verified": also}


def report(ledger: Ledger) -> str:
    """A truthful status line, derived from evidence rather than from belief."""
    if not ledger.verified:
        pending = ", ".join(ledger.pending) or "nothing started"
        return f"NOT COMPLETED. Outstanding: {pending}."
    parts = [f"{kind}={', '.join(items)}" for kind, items in ledger.verified.items()]
    tail = f" Still outstanding: {', '.join(ledger.pending)}." if ledger.pending else ""
    return "VERIFIED " + "; ".join(parts) + "." + tail
