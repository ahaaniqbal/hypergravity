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

import asyncio
import json
import os

from .authz import REFUSAL, may_use
from .background import running_jobs, start as start_background
from . import watch as watcher
from .delegate import build_and_report
from .memory import learn_booking, learn_note
from .counterparty import Counterparty, CounterpartyError, sms_delivered as _sms_delivered
from .gate import FabricationBlocked, claim_success, report
from .ledger import Ledger, StepState
from .mac_calendar import CalendarError, add_verified_event, busy_if_known, clashes_in
from .mac_agent import use_app as mac_use_app
from .mac_control import run as mac_run, tell_app as mac_tell_app
from .mac_messages import message_person
from .web import WebError, browse, look_up, peek


MY_PHONE = os.getenv("MY_PHONE", "")


def build_tools(ledger: Ledger, cp: Counterparty) -> tuple[ToolsSchema, dict[str, Any]]:
    """Return the schema advertised to the LLM plus a name->handler mapping."""

    def _guard(name: str, handler):
        """Wrap a handler so an untrusted caller can't reach the machine.

        Enforced here rather than by hiding tools from the model: the schema is
        the same for everyone, so the agent can explain the boundary instead of
        behaving as though the capability never existed.
        """

        async def guarded(params: FunctionCallParams) -> None:
            if not may_use(name, ledger.caller_phone):
                ledger.mark(f"{name} (refused)", StepState.FAILED, "caller not recognised")
                await params.result_callback(
                    {"allowed": False, "reason": REFUSAL, "you_must_say": REFUSAL}
                )
                return
            await handler(params)

        return guarded

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

        # Check the caller's own calendar at the same time. Folded in here rather
        # than bolted onto the booking because this is the step they already
        # expect a pause for — one "let me check" covers both, instead of adding
        # six seconds of silence after they've chosen. Run concurrently so the
        # cost is one lookup, not one per slot.
        clashes: dict[str, list[str]] = {}
        checked = False
        try:
            busy = await busy_if_known()
            # An empty list means "nothing read", not "nothing on". Reporting
            # those identically let the agent say "you're free then" about a
            # calendar it had never opened.
            checked = bool(busy)
            clashes = clashes_in(busy, free)
        except Exception as e:  # noqa: BLE001 — a calendar we can't read never blocks a booking
            logger.info(f"clash check skipped: {e}")

        await params.result_callback(
            {
                "available": free,
                "unavailable": taken,
                "already_busy_then": clashes,
                "calendar_checked": checked,
                "note": (
                    "Only offer times in 'available'. Never offer one in 'unavailable'. "
                    "If calendar_checked is false you have NOT seen their diary — say "
                    "nothing about whether they're free. If it's true, a time listed in "
                    "'already_busy_then' clashes with something: mention it in passing "
                    "and lead with a time that's genuinely clear."
                ),
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
        # Only bookings that verified are worth remembering — a preference
        # learned from something that failed is a lie told slowly.
        learn_booking(ledger.caller_phone, name, size, slot)
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
        # Delivery must be positively evidenced. "No known error word in a
        # field that is usually absent" is not evidence of anything.
        if not _sms_delivered(resp):
            # `prose` used to be read here without ever being assigned, so the
            # one branch that reports a failed text raised NameError instead —
            # the caller heard nothing at all about a text that never went.
            prose = str(resp.get("_text", "")).strip() or "the network did not confirm it"
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


    async def use_mac_app(params: FunctionCallParams) -> None:
        a = params.arguments
        app = str(a.get("app", "")).strip()
        actions = a.get("actions") or []
        if isinstance(actions, str):
            try:
                actions = json.loads(actions)
            except json.JSONDecodeError:
                await params.result_callback({"error": "actions must be a list"})
                return
        if not app or not actions:
            await params.result_callback({"error": "need an app and at least one action"})
            return

        label = f"{app}: {str(actions[0])[:26]}"
        ledger.mark(label, StepState.PENDING)
        try:
            result = await mac_use_app(app, actions)
        except Exception as e:  # noqa: BLE001
            ledger.mark(label, StepState.FAILED, str(e)[:60])
            await params.result_callback({"ok": False, "reason": str(e)})
            return

        worked = result.startswith("Did [")
        ledger.mark(label, StepState.DONE if worked else StepState.FAILED)
        await params.result_callback(
            {
                "ok": worked,
                "result": result,
                "instruction": (
                    "If ok is false NOTHING happened — say so rather than describing "
                    "what the steps would have done."
                ),
            }
        )

    async def build_it_and_text_me(params: FunctionCallParams) -> None:
        a = params.arguments
        request = str(a.get("request", "")).strip()
        app = str(a.get("app") or "Claude").strip()
        notify = str(a.get("notify") or ledger.caller_phone or MY_PHONE).strip()

        if not request:
            await params.result_callback({"started": False, "reason": "nothing to build"})
            return
        if not notify:
            await params.result_callback(
                {"started": False, "reason": "no number to text the link to"}
            )
            return

        async def work():
            return await build_and_report(request)

        job = start_background(f"{app}: {request[:40]}", ledger.task_id, notify, work)
        await params.result_callback(
            {
                "started": True,
                "job_id": job.job_id,
                "will_text": notify,
                "instruction": (
                    f"Say you've put it to {app} and you'll text the link when it's "
                    "ready, in ONE sentence, so they can hang up. Do not promise what "
                    "the result will be — you haven't seen it yet."
                ),
            }
        )

    async def tell_me_when(params: FunctionCallParams) -> None:
        a = params.arguments
        what = str(a.get("what", "")).strip()
        target = str(a.get("url_or_command", "")).strip()
        contains = str(a.get("until_contains", "")).strip()
        notify = str(a.get("notify") or ledger.caller_phone or MY_PHONE).strip()
        every = int(a.get("every_minutes") or 2) * 60
        for_secs = int(a.get("for_minutes") or 60) * 60

        if not what or not target:
            await params.result_callback(
                {"started": False, "reason": "need something to watch and where to look"}
            )
            return
        if not notify:
            await params.result_callback({"started": False, "reason": "no number to text"})
            return

        is_web = target.startswith(("http://", "https://"))

        async def look() -> str:
            if is_web:
                return await peek(target)
            result = await mac_run(target)
            return str(result.get("output") or result.get("reason") or "")

        try:
            baseline = await look()
        except Exception as e:  # noqa: BLE001
            await params.result_callback(
                {"started": False, "reason": f"couldn't check it even once: {e}"}
            )
            return

        w = watcher.start(what, ledger.task_id, notify, look, baseline,
                          contains=contains, every=every, for_seconds=for_secs)
        await params.result_callback(
            {
                "started": True,
                "watch_id": w.watch_id,
                "checking_every_minutes": w.every // 60,
                "instruction": (
                    "Say in ONE sentence that you'll keep an eye on it and text them, "
                    "so they can hang up. Don't guess when it'll happen."
                ),
            }
        )

    async def remember_this(params: FunctionCallParams) -> None:
        note = str(params.arguments.get("note", "")).strip()
        if not note:
            await params.result_callback({"saved": False})
            return
        learn_note(ledger.caller_phone, note)
        await params.result_callback(
            {"saved": True, "instruction": "Acknowledge in four words. Don't recite it back."}
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
                    "Tell the caller you're on it and that you'll CALL THEM BACK and "
                    "text when it's done, so they can hang up now. Say it in one "
                    "sentence. Do NOT wait for the result and do NOT describe what it "
                    "will say."
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

    async def message_someone(params: FunctionCallParams) -> None:
        """Find them in Contacts, message them, and read it back.

        Three outcomes are deliberately kept apart, because collapsing any two
        of them is how someone gets told a message went to a person it never
        reached: nobody by that name, more than one person by that name, and a
        message sent but not provably delivered.
        """
        a = params.arguments
        who = str(a.get("to", "")).strip()
        body = str(a.get("body", "")).strip()
        handle = str(a.get("handle", "")).strip()

        # "text me" needs no address book — we already know who is on the line.
        if who.lower() in ("me", "myself", "my phone") and not handle:
            handle = ledger.caller_phone or MY_PHONE
            who = "you"
        if not who and not handle:
            await params.result_callback({"sent": False, "reason": "no-one to message"})
            return

        label = f"message {who or handle}"[:40]
        ledger.mark(label, StepState.PENDING)
        try:
            r = await message_person(who, body, handle)
        except Exception as e:  # noqa: BLE001 — never let this raise into the call
            ledger.mark(label, StepState.FAILED, str(e)[:60])
            await params.result_callback({"sent": False, "verified": False, "reason": str(e)})
            return

        if r.get("ambiguous"):
            ledger.mark(label, StepState.FAILED, "more than one match")
            await params.result_callback(
                {
                    **r,
                    "instruction": (
                        "You do NOT know which one they meant. Read the names out and "
                        "ask which. Do not pick one, and do not send anything yet."
                    ),
                }
            )
            return

        if r.get("contacts_readable") is False:
            ledger.mark(label, StepState.FAILED, "contacts unreadable")
            await params.result_callback(
                {
                    **r,
                    "instruction": (
                        "You could not open Contacts, so you do NOT know whether they "
                        "have that person saved. Say you couldn't get at the address "
                        "book and offer to send it if they read you the number."
                    ),
                }
            )
            return

        if not r.get("sent"):
            ledger.mark(label, StepState.FAILED, str(r.get("reason", ""))[:60])
            await params.result_callback(
                {
                    **r,
                    "instruction": (
                        "Nothing was sent. Say so plainly and say why in a few words."
                    ),
                }
            )
            return

        if not r.get("verified"):
            # Handed to Messages, but not found on re-read. The command not
            # erroring is the app agreeing with itself; it is not delivery.
            ledger.mark(label, StepState.FAILED, "unverified")
            logger.warning(f"message to {r.get('to')} unverified: {r.get('reason')}")
            await params.result_callback(
                {
                    **r,
                    "instruction": (
                        "You may NOT say it sent. Tell them you passed it to Messages "
                        "but could not confirm it went, and let them check."
                    ),
                }
            )
            return

        ledger.record_evidence("message", str(r.get("to")), r)
        ledger.mark(label, StepState.DONE, str(r.get("to")))
        await params.result_callback(
            {
                **r,
                "instruction": (
                    "Sent AND read back from Messages itself. Say who it went to by "
                    "name in one short sentence. Don't read the message back."
                ),
            }
        )

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
            description=(
                "Browse in the user's Chrome and read the page. Plan the WHOLE "
                "task: loading a site answers nothing. Call again to go deeper."
            ),
            properties={
                "steps": {
                    "type": "array",
                    "description": (
                        "In order. Each is one of: {\"go\": \"https://…\"}, "
                        "{\"click\": \"visible text on the link or button\"}, "
                        "{\"type\": {\"into\": \"field label\", \"text\": \"…\"}}, "
                        "{\"wait\": seconds}. Start with a go, then fill the "
                        "search fields and click through to the actual result. "
                        "Best is a URL that already carries the query, e.g. "
                        "kayak.com/flights/SFO-LAX/2026-08-01 — a site's home "
                        "page shows marketing, not answers. Add a wait of 6-8 "
                        "after a search so results render."
                    ),
                    "items": {"type": "object"},
                },
            },
            required=["steps"],
            handler=browse_the_web,
        ),
        FunctionSchema(
            name="tell_me_when",
            description=(
                "Keep watching something and text them when it changes — a page, or a "
                "command's output. 'Tell me when the build finishes', 'text me if the "
                "price drops'. Returns at once so they can hang up."
            ),
            properties={
                "what": {"type": "string", "description": "In their words, e.g. 'the build'."},
                "url_or_command": {"type": "string", "description": "A URL to watch, or a shell command to re-run."},
                "until_contains": {"type": "string", "description": "Word to wait for. Omit to notify on any change."},
                "every_minutes": {"type": "integer", "description": "How often. Default 2."},
                "for_minutes": {"type": "integer", "description": "Give up after. Default 60."},
                "notify": {"type": "string", "description": "Number; defaults to the caller."},
            },
            required=["what", "url_or_command"],
            handler=tell_me_when,
        ),
        FunctionSchema(
            name="remember_this",
            description=(
                "Store something about the caller for next time — a preference, a "
                "detail. Only when they ask you to remember it."
            ),
            properties={"note": {"type": "string", "description": "Short, in their words."}},
            required=["note"],
            handler=remember_this,
        ),
        FunctionSchema(
            name="build_it_and_text_me",
            description=(
                "Hand a build or research job to another agent on the Mac — Claude "
                "Desktop by default, which has its own tools and can deploy — then "
                "text the caller the link when it appears. Use for 'make me a landing "
                "page', 'write me a script', anything that takes minutes. Returns "
                "immediately so they can hang up."
            ),
            properties={
                "request": {
                    "type": "string",
                    "description": (
                        "The WHOLE brief, written out as you'd type it to another "
                        "engineer — what to build, for whom, every detail the caller "
                        "gave you (names, dates, times, style). Several sentences. "
                        "The other agent cannot hear the call and cannot ask you "
                        "anything, so a vague line produces a vague result."
                    ),
                },
                "app": {"type": "string", "description": "Which app. Defaults to Claude."},
                "notify": {"type": "string", "description": "Number to text; defaults to the caller."},
            },
            required=["request"],
            handler=build_it_and_text_me,
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
            description=(
                "Run a shell command and read its output. Files, folders, git, code; "
                "`open` for apps and Finder; `osascript -e` for AppleScript."
            ),
            properties={
                "command": {"type": "string", "description": "The shell command."},
                "cwd": {"type": "string", "description": "Directory to run it in, optional."},
            },
            required=["command"],
            handler=run_on_mac,
        ),
        FunctionSchema(
            name="use_mac_app",
            description=(
                "Do something in any Mac app — Notes, Mail, Slack, Figma, anything. "
                "Give a short plan; it runs in one go. Menu paths are the most "
                "reliable: most commands live in a menu and need no hunting."
            ),
            properties={
                "app": {"type": "string", "description": "App name, e.g. 'Notes'."},
                "actions": {
                    "type": "array",
                    "description": (
                        'In order, each one of: {"menu": "File > New Note"}, '
                        '{"click": "visible label"}, {"type": "text"}, {"key": "cmd+s"}.'
                    ),
                    "items": {"type": "object"},
                },
            },
            required=["app", "actions"],
            handler=use_mac_app,
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
            name="message_someone",
            description=(
                "Text someone from Contacts, then read it back to prove it went. "
                "'me' is the caller. Empty body just looks up their number."
            ),
            properties={
                "to": {"type": "string", "description": "Their name, e.g. 'Dave'."},
                "body": {"type": "string", "description": "The whole message."},
                "handle": {
                    "type": "string",
                    "description": "Exact number/email, once they've picked a name.",
                },
            },
            required=["to"],
            handler=message_someone,
        ),
        FunctionSchema(
            name="task_status",
            description="What's done, pending and verified. Use after an interruption.",
            properties={},
            required=[],
            handler=task_status,
        ),
    ]

    handlers = {s.name: _guard(s.name, s.handler) for s in schemas}
    return ToolsSchema(standard_tools=schemas), handlers
