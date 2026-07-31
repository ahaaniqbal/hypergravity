"""HyperGravity as an MCP server — the desk-side front door.

VoiceOS is an MCP *client*: you point it at a server and every tool becomes
something you can just ask for out loud. So rather than building a second
microphone pipeline, VoiceOS *is* the desk transport. It launches this file
over stdio and speaks to the same orchestrator the phone reaches.

The ledger is shared through ``.ledgers.json``, so a task begun here can be
finished on the phone — and the verification gate is the same gate. There is no
second, laxer path to claiming success.

Connect it in VoiceOS: Settings → Integrations → Custom Integrations → Add

    Name:     HyperGravity
    Command:  <repo>/server/.venv/bin/python <repo>/server/mcp_server.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server import MCPServer

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

from agent.counterparty import Counterparty  # noqa: E402  (needs env first)
from agent.gate import FabricationBlocked, claim_success, report  # noqa: E402
from agent.ledger import StepState, open_task  # noqa: E402
from agent.mac_calendar import CalendarError, add_verified_event  # noqa: E402
from agent.mac_control import run as mac_run, tell_app as mac_tell_app  # noqa: E402
from agent.web import WebError, look_up  # noqa: E402

TASK_ID = os.getenv("TASK_ID", "hypergravity-live")

# mcp 2.x renamed FastMCP -> MCPServer; VoiceOS's guide documents the 1.x name.
mcp = MCPServer("hypergravity")


def _ledger():
    return open_task(TASK_ID)


async def _with_counterparty(fn):
    cp = Counterparty()
    try:
        return await fn(cp)
    finally:
        await cp.aclose()


@mcp.tool()
async def look_up_on_the_web(query: str) -> str:
    """Look anything up on the web in the browser on this Mac, and read the page back.

    Flights, prices, opening hours, a menu, a dashboard. Use whenever asked
    something you do not already know. Reading a page is not acting on it — you
    have not booked, bought or cancelled anything by calling this.

    Args:
        query: What to search for, or a full URL. Write it the way a search box
            wants it, not the way it was spoken: for flights use airport codes and
            drop filler, so "when's the latest flight from San Francisco to LA
            tomorrow" becomes "flights from SFO to LAX tomorrow".
    """
    led = _ledger()
    led.mark(f"look up: {query[:38]}", StepState.PENDING)
    try:
        page = await look_up(query)
    except WebError as e:
        led.mark(f"look up: {query[:38]}", StepState.FAILED, str(e))
        return f"COULD NOT READ THE PAGE — {e}. Do not guess what it said."
    led.mark(f"look up: {query[:38]}", StepState.DONE)
    return page


@mcp.tool()
async def run_on_mac(command: str, cwd: str = "") -> str:
    """Run a shell command on this Mac and read back what it printed.

    How you do anything that is not a web page: inspect or edit files, run or
    write code, use git, open an app with 'open -a', check the system. A command
    that fails is reported as failed — never describe the intended effect as
    though it happened.

    Args:
        command: The shell command.
        cwd: Directory to run it in, optional.
    """
    led = _ledger()
    label = f"run: {command[:34]}"
    led.mark(label, StepState.PENDING)
    r = await mac_run(command, cwd or None)
    if r.get("refused"):
        led.mark(label, StepState.FAILED, "refused")
        return f"REFUSED — {r.get('reason')}"
    if not r.get("ran") or not r.get("succeeded"):
        led.mark(label, StepState.FAILED, str(r.get("reason", "failed")))
        return f"FAILED (exit {r.get('exit_code')}) — {r.get('output') or r.get('reason')}"
    led.mark(label, StepState.DONE)
    return str(r.get("output"))


@mcp.tool()
async def control_app(app: str, applescript: str) -> str:
    """Drive a Mac app through AppleScript — Mail, Notes, Messages, Music, Finder, Numbers.

    Prefer this over the shell when the task belongs to a specific app: the
    scripting dictionary is a real API rather than simulated clicks. Pass only
    the body; the tell block is added for you.

    Args:
        app: App name, e.g. "Notes".
        applescript: Script body, e.g. 'make new note with properties {name:"Ideas"}'.
    """
    led = _ledger()
    label = f"{app}: {applescript[:26]}"
    led.mark(label, StepState.PENDING)
    r = await mac_tell_app(app, applescript)
    led.mark(label, StepState.DONE if r.get("ok") else StepState.FAILED)
    return str(r.get("result")) if r.get("ok") else f"{app} refused — {r.get('reason')}"


@mcp.tool()
async def check_availability() -> str:
    """List which reservation times the restaurant actually has free tonight.

    Call this before offering the caller any time, and again after any refusal.
    """
    async def run(cp: Counterparty):
        slots = await cp.get_availability()
        free = [s["time"] for s in slots if s.get("available")]
        taken = [s["time"] for s in slots if not s.get("available")]
        _ledger().mark("check availability", StepState.DONE)
        return (
            f"Free: {', '.join(free) or 'nothing'}. "
            f"Unavailable: {', '.join(taken) or 'none'}. "
            "Only offer a time from the free list."
        )

    return await _with_counterparty(run)


@mcp.tool()
async def book_table(name: str, party_size: int, time_slot: str, notes: str = "") -> str:
    """Attempt a reservation, then independently verify it landed.

    This can fail — the slot may be taken. Believe only what this returns, not
    what the restaurant claims. Do not tell anyone it is booked unless the reply
    below says it was verified.

    Args:
        name: Name to hold the reservation under.
        party_size: Number of people.
        time_slot: Exact slot, e.g. "18:30".
        notes: Any special request.
    """
    led = _ledger()

    async def run(cp: Counterparty):
        led.party_name, led.requested_slot, led.party_size = name, time_slot, party_size
        led.mark("book table", StepState.PENDING)

        resp = await cp.create_booking(
            name=name,
            party_size=party_size,
            time_slot=time_slot,
            phone=led.caller_phone or os.getenv("A1_PHONE_NUMBER", ""),
            notes=notes,
        )
        booking_id = resp.get("booking_id")
        if not booking_id:
            reason = resp.get("_text") or "the restaurant refused the booking"
            led.mark("book table", StepState.FAILED, reason)
            return (
                f"NOT BOOKED — {reason}. Do not say it is booked. "
                "Check availability and offer two real alternatives."
            )

        row = await cp.confirm_booking_landed(booking_id, time_slot, party_size)
        if row is None:
            led.mark("book table", StepState.FAILED, "read-back failed")
            return (
                "NOT CONFIRMED — the restaurant returned a number but the booking does "
                "not appear in their system on re-check. You may not report success."
            )

        led.record_evidence("booking", booking_id, row)
        led.mark("book table", StepState.DONE, f"booking {booking_id} @ {time_slot}")
        return (
            f"VERIFIED — booking {booking_id} for {party_size} at {time_slot} under {name}, "
            f"re-read from the restaurant's own system. "
            f"Now call claim_task_complete with token {booking_id}."
        )

    return await _with_counterparty(run)


@mcp.tool()
async def send_sms_confirmation(body: str, to: str = "") -> str:
    """Text the confirmation. Only after a verified booking.

    Args:
        body: Short confirmation message.
        to: Destination in E.164; defaults to the number on the task.
    """
    led = _ledger()
    dest = to or led.caller_phone or os.getenv("MY_PHONE", "")
    if not dest:
        return "No destination number on file — ask for one."

    async def run(cp: Counterparty):
        led.mark("send SMS", StepState.PENDING)
        resp = await cp.send_confirmation_sms(to=dest, body=body)
        prose = str(resp.get("_text", ""))
        if "error" in prose.lower() or "not allowed" in prose.lower():
            led.mark("send SMS", StepState.FAILED, prose)
            return (
                f"TEXT NOT SENT — {prose}. Say so plainly; keep it separate from the "
                "booking, which may still be fine."
            )
        led.record_evidence("sms", dest, {"to": dest, "body": body})
        led.mark("send SMS", StepState.DONE, f"to {dest}")
        return f"Text delivered to {dest}."

    return await _with_counterparty(run)


@mcp.tool()
async def add_to_calendar(time_slot: str = "", title: str = "", notes: str = "") -> str:
    """Put the confirmed booking in this Mac's own Calendar, then re-read it.

    Only after a verified booking. A judge can check this one with no
    credentials at all — they just open Calendar.

    Args:
        time_slot: Exact slot, e.g. "18:30". Defaults to the slot on the task.
        title: Event title. Defaults to "Dinner for N".
        notes: Anything worth remembering.
    """
    led = _ledger()
    slot = time_slot or led.requested_slot
    if not slot:
        return "No time slot known yet — book the table first."

    try:
        row = await add_verified_event(
            title or f"Dinner for {led.party_size or 2}", slot, notes
        )
    except CalendarError as e:
        led.mark("add to calendar", StepState.FAILED, str(e))
        return f"NOT ADDED — {e}. The booking itself may still be fine."

    if row is None:
        led.mark("add to calendar", StepState.FAILED, "read-back failed")
        return "NOT CONFIRMED — the event was created but is not in the calendar on re-check."

    led.record_evidence("calendar", row["uid"], row)
    led.mark("add to calendar", StepState.DONE, row["starts"])
    return f"VERIFIED — '{row['summary']}' is in the {row['calendar']} calendar at {row['starts']}."


@mcp.tool()
def claim_task_complete(booking_id: str) -> str:
    """The only way to report the task done. Call it ONCE per task.

    Requires the booking id the restaurant issued and that was independently
    re-read. The text and the calendar entry are covered by this same claim —
    never call this again for those. If it refuses, report honestly instead: a
    fabricated success is worse than an admitted failure.

    Args:
        booking_id: The booking id book_table returned.
    """
    led = _ledger()
    try:
        result = claim_success(led, "booking", booking_id)
    except FabricationBlocked as e:
        return f"REFUSED — {e} Truthful status: {report(led)}"
    also = result.get("also_verified") or {}
    extra = f" Also verified: {also}." if also else ""
    return f"CONFIRMED — booking {result['token']}.{extra}"


@mcp.tool()
def task_status() -> str:
    """What is done, pending and verified on the current task.

    Use this to pick up a task that was started on the phone, or after an
    interruption, instead of asking everything again.
    """
    led = _ledger()
    return f"{led.summary()}\n\n{report(led)}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
