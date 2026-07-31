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

from loguru import logger

from .ledger import Ledger

LOG_PATH = Path(__file__).resolve().parent.parent / "verification_log.jsonl"

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
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(asdict(d)) + "\n")
    except OSError:  # never let logging break a live call
        pass


class FabricationBlocked(Exception):
    """Raised when the agent tries to claim something it cannot evidence.

    The message is written to be spoken: it is handed back to the model as the
    tool result so the model's only remaining move is an honest report.
    """


def claim_success(ledger: Ledger, kind: str, token: str) -> dict[str, Any]:
    """The ONLY way the agent may assert a task completed.

    ``kind`` is the evidence channel (``booking`` / ``sms``); ``token`` is the
    identifier the counterparty returned. Both must already be in the ledger,
    put there by a tool handler that saw the counterparty's own response.
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
        reason=f"{kind} {token} verified against counterparty response",
        evidence=evidence,
    )
    _record(d)
    return {"ok": True, "kind": kind, "token": token, "evidence": evidence}


def report(ledger: Ledger) -> str:
    """A truthful status line, derived from evidence rather than from belief."""
    if not ledger.verified:
        pending = ", ".join(ledger.pending) or "nothing started"
        return f"NOT COMPLETED. Outstanding: {pending}."
    parts = [f"{kind}={', '.join(items)}" for kind, items in ledger.verified.items()]
    tail = f" Still outstanding: {', '.join(ledger.pending)}." if ledger.pending else ""
    return "VERIFIED " + "; ".join(parts) + "." + tail
