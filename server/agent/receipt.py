"""Text the caller what actually happened, once the call is over.

A phone call leaves no record. You hang up holding a memory of what the agent
said, which is exactly the thing this build refuses to let you rely on. So the
last act of every call is to send the evidence: what landed, what didn't, and
how to check it yourself.

The failures are the point. Anyone can send a confirmation when things worked;
sending "the text didn't go out" is what makes the successes worth believing.
Built from the ledger rather than the conversation, so it reports what the tool
layer observed, not what the agent thinks it said.
"""

from __future__ import annotations

from loguru import logger

from .counterparty import Counterparty, sms_delivered
from .ledger import Ledger, StepState

MAX_SMS = 320


def compose(ledger: Ledger) -> str | None:
    """The receipt, or None if nothing worth reporting happened."""
    verified = ledger.verified or {}
    failed = [s for s in ledger.steps if s.state is StepState.FAILED]

    if not verified and not failed:
        return None

    lines: list[str] = []

    if booking_ids := list(verified.get("booking", {})):
        slot = ledger.requested_slot or "the time we agreed"
        who = f" under {ledger.party_name}" if ledger.party_name else ""
        size = f" for {ledger.party_size}" if ledger.party_size else ""
        lines.append(f"Booked{size} at {slot}{who}. Ref {booking_ids[0]}.")

    if "calendar" in verified:
        lines.append("It's in your calendar.")

    # Say plainly what didn't work. A receipt that only lists wins is marketing.
    for step in failed:
        detail = (step.detail or "").strip()
        reason = f" — {detail[:70]}" if detail else ""
        lines.append(f"Couldn't {step.name}{reason}.")

    if not lines:
        return None

    body = " ".join(lines)
    if len(body) > MAX_SMS:
        body = body[: MAX_SMS - 1] + "…"
    return body


async def send(ledger: Ledger) -> bool:
    """Send the receipt. Its own counterparty: the call's is closed at hangup."""
    body = compose(ledger)
    if not body:
        return False

    to = ledger.caller_phone
    if not to:
        logger.info("no receipt — no number for this caller")
        return False

    cp = Counterparty()
    try:
        resp = await cp.send_confirmation_sms(to=to, body=body)
        if not sms_delivered(resp):
            logger.warning(f"receipt not delivered to {to}: {resp}")
            return False
        logger.info(f"receipt texted to {to}: {body[:70]}")
        return True
    except Exception as e:  # noqa: BLE001 — a failed receipt must not raise on hangup
        logger.warning(f"receipt failed: {e}")
        return False
    finally:
        await cp.aclose()
