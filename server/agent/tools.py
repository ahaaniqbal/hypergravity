"""Tools the agent may call, and the only path by which evidence is recorded.

Design rule: the model never writes to ``ledger.verified``. Only the handlers
here do, and only from what the counterparty actually returned. That is what
makes ``gate.claim_success`` meaningful rather than decorative.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

import json
import os

from .background import running_jobs, start as start_background
from .counterparty import Counterparty, CounterpartyError
from .gate import FabricationBlocked, claim_success, report
from .ledger import Ledger, StepState
from .mac_calendar import CalendarError, add_verified_event
from .mac_agent import click_in_app as mac_click
from .mac_control import run as mac_run, tell_app as mac_tell_app
from .web import WebError, browse, look_up


MY_PHONE = os.getenv("MY_PHONE", "")


def build_tools(ledger: Ledger, cp: Counterparty) -> tuple[ToolsSchema, dict[str, Any]]:
    """Return the schema advertised to the LLM plus a name->handler mapping."""

    # -- counterparty: look --------------------------------------------------

    async def check_availability(params: FunctionCallParams) -> None:
        ledger.mark("check availability", StepState.PENDING)
        try:
            slots = await cp.get_availability()
        except CounterpartyError as e:
            ledger.mark("check availability", StepState.FAILED, str(e))
            await params.result_callback({"error": str(e)})
            return
        ledger.mark("check availability", StepState.DONE)
        free = [s["time"] for s in slots if s.get("available")]
        taken = [s["time"] for s in slots if not s.get("available")]
        await params.result_callback(
            {
                "available": free,
                "unavailable": taken,
                "note": "Only offer times in 'available'. Never offer a time in 'unavailable'.",
            }
        )

    # -- counterparty: act, then independently verify -------------------------

    async def book_table(params: FunctionCallParams) -> None:
        a = params.arguments
        name = str(a.get("name", "")).strip()
        slot = str(a.get("time_slot", "")).strip()
        size = int(a.get("party_size") or 0)
        phone = str(a.get("phone", "")).strip()

        ledger.note(name=name, slot=slot, party_size=size, phone=phone or None)
        ledger.mark("book table", StepState.PENDING)

        try:
            resp = await cp.create_booking(
                name=name, party_size=size, time_slot=slot,
                phone=phone or ledger.caller_phone, notes=str(a.get("notes", "")),
            )
        except CounterpartyError as e:
            ledger.mark("book table", StepState.FAILED, str(e))
            await params.result_callback({"booked": False, "reason": str(e)})
            return

        booking_id = resp.get("booking_id")
        if not booking_id:
            # The refusal path — e.g. "time slot 19:00 unavailable".
            reason = resp.get("_text") or "the restaurant refused the booking"
            ledger.mark("book table", StepState.FAILED, reason)
            await params.result_callback(
                {
                    "booked": False,
                    "reason": reason,
                    "instruction": (
                        "This did NOT work. Do not tell the caller it is booked. "
                        "Call check_availability and offer two real alternatives."
                    ),
                }
            )
            return

        # Do not trust the create response. Re-read the bookings list.
        row = await cp.confirm_booking_landed(booking_id, slot, size)
        if row is None:
            ledger.mark("book table", StepState.FAILED, "read-back failed")
            logger.warning(f"create said ok (id={booking_id}) but read-back found no matching row")
            await params.result_callback(
                {
                    "booked": False,
                    "reason": (
                        "The restaurant returned a confirmation number but the booking "
                        "does not appear in their system on re-check."
                    ),
                    "instruction": (
                        "You may NOT report success. Tell the caller honestly that the "
                        "booking could not be confirmed, and offer to try another time."
                    ),
                }
            )
            return

        # Verified. This is the only place booking evidence is written.
        ledger.record_evidence("booking", booking_id, row)
        ledger.mark("book table", StepState.DONE, f"booking {booking_id} @ {slot}")
        await params.result_callback(
            {
                "booked": True,
                "booking_id": booking_id,
                "verified_row": row,
                "instruction": (
                    "Confirmed AND independently re-read from the restaurant's system. "
                    "You may now call claim_success with this booking_id."
                ),
            }
        )

    # -- side effect the judges can see --------------------------------------

    async def send_sms_confirmation(params: FunctionCallParams) -> None:
        a = params.arguments
        to = str(a.get("to") or ledger.caller_phone).strip()
        body = str(a.get("body", "")).strip()
        if not to:
            await params.result_callback({"sent": False, "reason": "no destination number"})
            return
        ledger.mark("send SMS", StepState.PENDING)
        try:
            resp = await cp.send_confirmation_sms(to=to, body=body)
        except CounterpartyError as e:
            ledger.mark("send SMS", StepState.FAILED, str(e))
            await params.result_callback(
                {
                    "sent": False,
                    "reason": str(e),
                    "instruction": (
                        "The text did not go out. Say so plainly — do not imply it was sent. "
                        "The booking itself may still be fine."
                    ),
                }
            )
            return
        # The MCP reports some refusals as prose in a 200 rather than as an
        # error — e.g. "destination not allowed". Treat any such body as a
        # failure, or the ledger records evidence for a text that never went.
        prose = str(resp.get("_text", ""))
        if "error" in prose.lower() or "not allowed" in prose.lower():
            ledger.mark("send SMS", StepState.FAILED, prose)
            await params.result_callback(
                {
                    "sent": False,
                    "reason": prose,
                    "instruction": (
                        "The text did NOT go out. Say so plainly. Keep it separate from "
                        "the booking, which may still be fine."
                    ),
                }
            )
            return

        ledger.record_evidence("sms", to, {"to": to, "body": body, "response": resp})
        ledger.mark("send SMS", StepState.DONE, f"to {to}")
        await params.result_callback({"sent": True, "to": to, "response": resp})

    # -- the general capability: anything on the web --------------------------

    async def look_up_on_the_web(params: FunctionCallParams) -> None:
        query = str(params.arguments.get("query", "")).strip()
        if not query:
            await params.result_callback({"error": "nothing to look up"})
            return

        ledger.mark(f"look up: {query[:38]}", StepState.PENDING)
        try:
            page = await look_up(query)
        except WebError as e:
            ledger.mark(f"look up: {query[:38]}", StepState.FAILED, str(e))
            await params.result_callback(
                {
                    "found": False,
                    "reason": str(e),
                    "instruction": "Say the page would not load. Do not guess what it said.",
                }
            )
            return

        ledger.mark(f"look up: {query[:38]}", StepState.DONE)
        await params.result_callback(
            {
                "found": True,
                "page_text": page,
                "instruction": (
                    "This is what the page actually said. Answer from it in one or two "
                    "spoken sentences, best option first. If it does not contain the "
                    "answer, say so — do not fill the gap from memory. You have only "
                    "READ this; you have not acted on it."
                ),
            }
        )

    # -- do anything on the machine -------------------------------------------

    async def run_on_mac(params: FunctionCallParams) -> None:
        command = str(params.arguments.get("command", "")).strip()
        if not command:
            await params.result_callback({"error": "no command given"})
            return

        label = f"run: {command[:34]}"
        ledger.mark(label, StepState.PENDING)
        result = await mac_run(command, str(params.arguments.get("cwd", "")) or None)

        if result.get("refused"):
            ledger.mark(label, StepState.FAILED, "refused")
            await params.result_callback({**result, "instruction": "Say exactly why you won't."})
            return
        if not result.get("ran") or not result.get("succeeded"):
            ledger.mark(label, StepState.FAILED, str(result.get("reason", "failed")))
            await params.result_callback(
                {
                    **result,
                    "instruction": (
                        "This did NOT succeed. Say so and read the error briefly. "
                        "Do not describe the intended effect as though it happened."
                    ),
                }
            )
            return

        ledger.mark(label, StepState.DONE)
        await params.result_callback(
            {
                **result,
                "instruction": (
                    "This is the real output. Answer from it in one or two spoken "
                    "sentences. Report only what it shows."
                ),
            }
        )

    async def control_app(params: FunctionCallParams) -> None:
        a = params.arguments
        app = str(a.get("app", "")).strip()
        script = str(a.get("applescript", "")).strip()
        if not app or not script:
            await params.result_callback({"error": "need both an app and a script"})
            return

        label = f"{app}: {script[:26]}"
        ledger.mark(label, StepState.PENDING)
        result = await mac_tell_app(app, script)
        ledger.mark(label, StepState.DONE if result.get("ok") else StepState.FAILED)
        await params.result_callback(
            {
                **result,
                "instruction": (
                    "Report only what came back. If ok is false, say the app refused "
                    "and why — do not claim the action happened."
                ),
            }
        )

    async def browse_the_web(params: FunctionCallParams) -> None:
        steps = params.arguments.get("steps") or []
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError:
                await params.result_callback({"error": "steps must be a list"})
                return
        if not steps:
            await params.result_callback({"error": "no steps given"})
            return

        label = f"browse: {str(steps[0])[:34]}"
        ledger.mark(label, StepState.PENDING)
        try:
            page = await browse(steps)
        except WebError as e:
            ledger.mark(label, StepState.FAILED, str(e))
            await params.result_callback(
                {
                    "ok": False,
                    "reason": str(e),
                    "instruction": "Say what went wrong. Do not guess what the page said.",
                }
            )
            return

        ledger.mark(label, StepState.DONE)
        await params.result_callback(
            {
                "ok": True,
                "page": page,
                "instruction": (
                    "'WHAT I DID' is the steps that actually ran — if one says it could "
                    "not find something, that step did not happen, so do not describe it "
                    "as though it did. Answer from the page text in one or two spoken "
                    "sentences. You have only READ this."
                ),
            }
        )


    async def click_in_any_app(params: FunctionCallParams) -> None:
        a = params.arguments
        app = str(a.get("app", "")).strip()
        what = str(a.get("what", "")).strip()
        if not app or not what:
            await params.result_callback({"error": "need an app and what to click"})
            return
        label = f"click {what[:20]} in {app}"
        ledger.mark(label, StepState.PENDING)
        try:
            result = await mac_click(app, what)
        except Exception as e:  # noqa: BLE001
            ledger.mark(label, StepState.FAILED, str(e)[:60])
            await params.result_callback({"ok": False, "reason": str(e)})
            return
        clicked = result.startswith("Clicked")
        ledger.mark(label, StepState.DONE if clicked else StepState.FAILED)
        await params.result_callback(
            {
                "ok": clicked,
                "result": result,
                "instruction": (
                    "If ok is false nothing was clicked — say so rather than describing "
                    "what the click would have done."
                ),
            }
        )

    # -- work that outlives the call ------------------------------------------

    async def work_in_background(params: FunctionCallParams) -> None:
        a = params.arguments
        what = str(a.get("what", "")).strip()
        command = str(a.get("command", "")).strip()
        query = str(a.get("web_query", "")).strip()
        notify = str(a.get("notify") or ledger.caller_phone or MY_PHONE).strip()

        if not what or not (command or query):
            await params.result_callback(
                {"started": False, "reason": "need a description and either a command or a web query"}
            )
            return
        if not notify:
            await params.result_callback(
                {"started": False, "reason": "no number to text the result to — ask for one"}
            )
            return

        async def work():
            if command:
                result = await mac_run(command)
                if result.get("refused"):
                    raise RuntimeError(str(result.get("reason")))
                if not result.get("succeeded"):
                    raise RuntimeError(str(result.get("output") or result.get("reason")))
                return result.get("output")
            return await look_up(query)

        job = start_background(what, ledger.task_id, notify, work)
        await params.result_callback(
            {
                "started": True,
                "job_id": job.job_id,
                "will_text": notify,
                "instruction": (
                    "Tell the caller you're on it and that you'll text them when it's "
                    "done, so they can hang up. Say it in one sentence. Do NOT wait for "
                    "the result and do NOT describe what it will say."
                ),
            }
        )

    # -- the caller's own machine --------------------------------------------

    async def add_to_calendar(params: FunctionCallParams) -> None:
        a = params.arguments
        slot = str(a.get("time_slot") or ledger.requested_slot).strip()
        title = str(a.get("title") or f"Dinner for {ledger.party_size or 2}").strip()
        if not slot:
            await params.result_callback({"added": False, "reason": "no time slot known yet"})
            return

        ledger.mark("add to calendar", StepState.PENDING)
        try:
            row = await add_verified_event(title, slot, str(a.get("notes", "")))
        except CalendarError as e:
            ledger.mark("add to calendar", StepState.FAILED, str(e))
            await params.result_callback(
                {
                    "added": False,
                    "reason": str(e),
                    "instruction": "Say the calendar entry did not go in. The booking may still be fine.",
                }
            )
            return

        if row is None:
            ledger.mark("add to calendar", StepState.FAILED, "read-back failed")
            await params.result_callback(
                {"added": False, "reason": "the event was created but is not in the calendar on re-check"}
            )
            return

        ledger.record_evidence("calendar", row["uid"], row)
        ledger.mark("add to calendar", StepState.DONE, row["starts"])
        await params.result_callback({"added": True, "event": row})

    # -- the gate -------------------------------------------------------------

    async def claim_task_complete(params: FunctionCallParams) -> None:
        a = params.arguments
        try:
            result = claim_success(ledger, "booking", str(a.get("booking_id", "")))
        except FabricationBlocked as e:
            await params.result_callback(
                {"allowed": False, "you_must_say": str(e), "truthful_status": report(ledger)}
            )
            return
        await params.result_callback(
            {
                "allowed": True,
                **result,
                "instruction": (
                    "Call this ONCE per task. Anything under 'also_verified' is already "
                    "confirmed — mention it, do not claim it again."
                ),
            }
        )

    async def task_status(params: FunctionCallParams) -> None:
        """Lets the agent resume honestly after an interruption or a transport
        switch, instead of re-interrogating the caller."""
        # Background jobs are the one thing not in the ledger's step list while
        # they run, and "is that still going?" is the obvious question to ask a
        # minute after starting one.
        still_running = [
            f"{j.what} (started {j.elapsed}s ago)" for j in running_jobs(ledger.task_id)
        ]
        await params.result_callback(
            {
                "ledger": ledger.summary(),
                "truthful_status": report(ledger),
                "still_running": still_running,
                "instruction": (
                    "Anything in still_running has NOT finished — say it's still going "
                    "and that you'll text when it's done. Don't guess at its result."
                ),
            }
        )

    # -- schemas --------------------------------------------------------------

    schemas = [
        FunctionSchema(
            name="browse_the_web",
            description="Browse in the user's Chrome and read the page. Steps run in one go.",
            properties={
                "steps": {
                    "type": "array",
                    "description": (
                        "In order. Each is one of: {\"go\": \"https://…\"}, "
                        "{\"click\": \"visible text on the link or button\"}, "
                        "{\"type\": {\"into\": \"field label\", \"text\": \"…\"}}, "
                        "{\"wait\": seconds}. Start with a go."
                    ),
                    "items": {"type": "object"},
                },
            },
            required=["steps"],
            handler=browse_the_web,
        ),
        FunctionSchema(
            name="work_in_background",
            description='Start slow work and text the result, so the caller can hang up.',
            properties={
                "what": {
                    "type": "string",
                    "description": "Short description in the caller's words, e.g. 'the big files in Downloads'.",
                },
                "command": {"type": "string", "description": "Shell command to run, if it's a machine task."},
                "web_query": {"type": "string", "description": "Search or URL, if it's a web task."},
                "notify": {"type": "string", "description": "Number to text; defaults to the caller."},
            },
            required=["what"],
            handler=work_in_background,
        ),
        FunctionSchema(
            name="run_on_mac",
            description='Run a shell command and read its output. Files, code, git, opening apps.',
            properties={
                "command": {"type": "string", "description": "The shell command."},
                "cwd": {"type": "string", "description": "Directory to run it in, optional."},
            },
            required=["command"],
            handler=run_on_mac,
        ),
        FunctionSchema(
            name="click_in_any_app",
            description='Click something in an app with no AppleScript support.',
            properties={
                "app": {"type": "string", "description": "App name, e.g. 'Comet'."},
                "what": {"type": "string", "description": "The button or menu, in words."},
            },
            required=["app", "what"],
            handler=click_in_any_app,
        ),
        FunctionSchema(
            name="control_app",
            description='Drive a Mac app via AppleScript. Mail, Notes, Messages, Finder, Numbers.',
            properties={
                "app": {"type": "string", "description": "App name, e.g. 'Notes'."},
                "applescript": {
                    "type": "string",
                    "description": "Script body, e.g. 'make new note with properties {name:\"Ideas\"}'.",
                },
            },
            required=["app", "applescript"],
            handler=control_app,
        ),
        FunctionSchema(
            name="check_availability",
            description='Which reservation times are actually free. Call before offering any.',
            properties={},
            required=[],
            handler=check_availability,
        ),
        FunctionSchema(
            name="book_table",
            description='Attempt a reservation. Can fail. Believe only what it returns.',
            properties={
                "name": {"type": "string", "description": "Name the reservation is under."},
                "party_size": {"type": "integer", "description": "Number of people."},
                "time_slot": {"type": "string", "description": "Exact slot, e.g. '18:30'."},
                "phone": {"type": "string", "description": "Caller's number, E.164."},
                "notes": {"type": "string", "description": "Any special request."},
            },
            required=["name", "party_size", "time_slot"],
            handler=book_table,
        ),
        FunctionSchema(
            name="send_sms_confirmation",
            description='Text the caller. Only after a verified booking.',
            properties={
                "to": {"type": "string", "description": "Destination number, E.164."},
                "body": {"type": "string", "description": "Short confirmation message."},
            },
            required=["body"],
            handler=send_sms_confirmation,
        ),
        FunctionSchema(
            name="add_to_calendar",
            description='Add the confirmed booking to the Mac calendar.',
            properties={
                "title": {"type": "string", "description": "Event title, e.g. 'Dinner for 2'."},
                "time_slot": {"type": "string", "description": "Exact slot, e.g. '18:30'."},
                "notes": {"type": "string", "description": "Anything worth remembering."},
            },
            required=[],
            handler=add_to_calendar,
        ),
        FunctionSchema(
            name="claim_task_complete",
            description='The ONLY way to say the task is done. Once, with the booking id.',
            properties={
                "booking_id": {
                    "type": "string",
                    "description": "The booking_id book_table returned.",
                },
            },
            required=["booking_id"],
            handler=claim_task_complete,
        ),
        FunctionSchema(
            name="task_status",
            description="What's done, pending and verified. Use after an interruption.",
            properties={},
            required=[],
            handler=task_status,
        ),
    ]

    handlers = {s.name: s.handler for s in schemas}
    return ToolsSchema(standard_tools=schemas), handlers
