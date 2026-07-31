"""Write the confirmed booking into the Mac's own Calendar.

The third verifiable side effect, and the one a judge can check without any
credentials at all — you just open Calendar.app and look.

Driven by AppleScript rather than accessibility-tree automation: Calendar has a
real scripting dictionary, so this is a supported API rather than a simulated
click, and it needs no Accessibility or Screen Recording grant.

As everywhere else in this build, the write is followed by an independent
read-back. Creating an event and reporting success because the command did not
error is exactly the habit the verification gate exists to prevent.
"""

from __future__ import annotations

import asyncio
import subprocess

from loguru import logger

DEFAULT_CALENDAR = "Home"
DURATION_SECONDS = 90 * 60


class CalendarError(RuntimeError):
    pass


async def _osascript(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise CalendarError((err or b"").decode().strip() or "osascript failed")
    return out.decode().strip()


async def create_event(
    title: str, hour: int, minute: int, notes: str = "", calendar: str = DEFAULT_CALENDAR
) -> str:
    """Create today's event and return its UID."""
    script = f'''
    tell application "Calendar"
      tell calendar "{calendar}"
        set theStart to (current date)
        set hours of theStart to {hour}
        set minutes of theStart to {minute}
        set seconds of theStart to 0
        set theEnd to theStart + {DURATION_SECONDS}
        set newEvent to make new event with properties ¬
          {{summary:"{title}", start date:theStart, end date:theEnd, description:"{notes}"}}
        return uid of newEvent
      end tell
    end tell
    '''
    uid = await _osascript(script)
    if not uid:
        raise CalendarError("Calendar returned no event id")
    return uid


async def read_event(uid: str, calendar: str = DEFAULT_CALENDAR) -> dict[str, str] | None:
    """Independently re-read the event. None means it is not really there."""
    script = f'''
    tell application "Calendar"
      tell calendar "{calendar}"
        set matches to (every event whose uid is "{uid}")
        if (count of matches) is 0 then return ""
        set e to item 1 of matches
        return (summary of e) & "|" & (start date of e as string)
      end tell
    end tell
    '''
    try:
        raw = await _osascript(script)
    except CalendarError as e:
        logger.warning(f"calendar read-back failed: {e}")
        return None
    if not raw or "|" not in raw:
        return None
    summary, start = raw.split("|", 1)
    return {"uid": uid, "summary": summary, "starts": start, "calendar": calendar}


async def add_verified_event(
    title: str, time_slot: str, notes: str = "", calendar: str = DEFAULT_CALENDAR
) -> dict[str, str] | None:
    """Create, then re-read. Returns the read-back row, or None if it did not land.

    ``time_slot`` is the counterparty's own format, e.g. "18:30".
    """
    try:
        hour, minute = (int(p) for p in time_slot.split(":", 1))
    except ValueError as e:
        raise CalendarError(f"unparseable time slot {time_slot!r}") from e

    uid = await create_event(title, hour, minute, notes, calendar)
    row = await read_event(uid, calendar)
    if row is None:
        logger.warning(f"calendar event {uid} created but not found on re-read")
    return row
