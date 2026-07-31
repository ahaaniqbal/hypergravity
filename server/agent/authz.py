"""Who is allowed to do what, based on the number they called from.

The number is public — it has to be, judges dial it — and behind it sit a shell,
the filesystem and a browser holding live logged-in sessions. Anyone who can
read the phone number should not thereby get a terminal on someone's laptop.

So capability depends on the caller. A known handset gets everything. A stranger
gets the conversation and the booking flow: enough to see the agent work,
including the verification gate, without touching the machine.

Refusals are spoken plainly rather than silently dropped. An agent that ignores
a request is indistinguishable from one that's broken, and the caller deserves
to know a boundary exists rather than assuming the thing is faulty.
"""

from __future__ import annotations

import os

from loguru import logger

# Anything that touches the machine, the filesystem, or a logged-in browser.
PRIVILEGED = {
    "run_on_mac",
    "control_app",
    "click_in_any_app",
    "browse_the_web",
    "look_up_on_the_web",
    "work_in_background",
    "add_to_calendar",
    "add_to_mac_calendar",
}

# Safe for anyone: the counterparty, the gate, and reading task state. This is
# the whole demo a judge needs — booking, friction, verification, refusal.
PUBLIC = {
    "check_availability",
    "check_restaurant_availability",
    "book_table",
    "book_restaurant_table",
    "send_sms_confirmation",
    "text_me_an_sms",
    "claim_task_complete",
    "verify_before_confirming",
    "task_status",
    "hypergravity_task_status",
}

REFUSAL = (
    "I can't do things on the Mac for a number I don't recognise. "
    "I can still book a table and text you the confirmation."
)


def _normalise(number: str) -> str:
    """Compare on digits: the same handset arrives as +1415…, 1415…, (415)…"""
    return "".join(c for c in (number or "") if c.isdigit())[-10:]


def trusted_numbers() -> set[str]:
    raw = f"{os.getenv('MY_PHONE', '')},{os.getenv('TRUSTED_NUMBERS', '')}"
    return {_normalise(n) for n in raw.split(",") if _normalise(n)}


def open_access() -> bool:
    """Let any caller drive the machine.

    Off by default, which is the right posture for something reachable from a
    public phone number. Worth turning on for a supervised demo, where a judge
    dialling from their own handset should be able to try everything — a refusal
    they weren't expecting reads as a broken agent rather than a deliberate one.
    """
    return os.getenv("OPEN_ACCESS", "").strip().lower() in {"1", "true", "yes"}


def is_trusted(caller: str | None) -> bool:
    """Desk sessions (no caller) are trusted: they're already on the machine."""
    if not caller or open_access():
        return True
    return _normalise(caller) in trusted_numbers()


def may_use(tool: str, caller: str | None) -> bool:
    if tool not in PRIVILEGED:
        return True
    allowed = is_trusted(caller)
    if not allowed:
        logger.warning(f"refused {tool} to untrusted caller {caller}")
    return allowed


def describe(caller: str | None) -> str:
    """One line for the logs and the panel, so the mode is never a surprise."""
    return (
        f"trusted caller ({caller or 'desk'}) — full access"
        if is_trusted(caller)
        else f"unknown caller ({caller}) — booking only, no Mac access"
    )
