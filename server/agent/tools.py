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

from .counterparty import Counterparty, CounterpartyError
from .gate import FabricationBlocked, claim_success, report
from .ledger import Ledger, StepState
from .mac_calendar import CalendarError, add_verified_event


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

        ledger.party_name, ledger.requested_slot, ledger.party_size = name, slot, size
        if phone:
            ledger.caller_phone = phone
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
        await params.result_callback({"ledger": ledger.summary(), "truthful_status": report(ledger)})

    # -- schemas --------------------------------------------------------------

    schemas = [
        FunctionSchema(
            name="check_availability",
            description=(
                "List which reservation times the restaurant actually has free. "
                "Call this before offering any time, and again after any refusal."
            ),
            properties={},
            required=[],
            handler=check_availability,
        ),
        FunctionSchema(
            name="book_table",
            description=(
                "Attempt a reservation. This can fail — the slot may be taken. "
                "The result tells you whether it truly landed; believe only that."
            ),
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
            description="Text the caller their confirmation. Only after a verified booking.",
            properties={
                "to": {"type": "string", "description": "Destination number, E.164."},
                "body": {"type": "string", "description": "Short confirmation message."},
            },
            required=["body"],
            handler=send_sms_confirmation,
        ),
        FunctionSchema(
            name="add_to_calendar",
            description=(
                "Put the confirmed booking in the caller's own Mac calendar. "
                "Only after a verified booking."
            ),
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
            description=(
                "The ONLY way to tell the caller the task is done. Call it ONCE, with the "
                "booking id the restaurant issued. The text and the calendar entry are "
                "covered by the same claim — never call this again for those. "
                "If it returns allowed=false, report honestly instead."
            ),
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
            description=(
                "What is done, pending, and verified so far. Call this if you lose track, "
                "are interrupted, or the caller returns on a different device."
            ),
            properties={},
            required=[],
            handler=task_status,
        ),
    ]

    handlers = {s.name: s.handler for s in schemas}
    return ToolsSchema(standard_tools=schemas), handlers
