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


_ensured = False


async def _ensure_calendar_running() -> None:
    """Start Calendar via the shell before scripting it.

    AppleScript's own ``launch`` cannot cold-start it — a closed Calendar returns
    -600 "Application isn't running" no matter what the tell block says. The
    shell opener does start it, and once it is up the scripting dictionary works
    normally. Same trick Safari needs.
    """
    global _ensured
    if _ensured:
        return
    proc = await asyncio.create_subprocess_exec(
        "open", "-a", "Calendar",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await proc.wait()
    await asyncio.sleep(2.0)
    _ensured = True


OSASCRIPT_TIMEOUT = 12.0


async def _osascript(script: str) -> str:
    """Run AppleScript with a hard timeout.

    The first Calendar write raises a macOS Automation consent dialog, and
    osascript blocks on it until someone clicks. Without a timeout that stalls
    the whole call — the caller hears nothing while a dialog they may not even
    be looking at waits for a click.
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=OSASCRIPT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise CalendarError(
            "Calendar did not respond — macOS is probably asking for permission. "
            "Approve it once and this will work from then on."
        ) from None
    if proc.returncode != 0:
        detail = (err or b"").decode().strip().splitlines()
        raise CalendarError(detail[-1][:200] if detail else "osascript failed")
    return out.decode().strip()


async def create_event(
    title: str, hour: int, minute: int, notes: str = "", calendar: str = DEFAULT_CALENDAR
) -> str:
    """Create today's event and return its UID."""
    await _ensure_calendar_running()
    # `launch` starts Calendar without bringing it to the front — without it the
    # script fails with -600 "Application isn't running" whenever the app is
    # closed, which on a demo machine is most of the time.
    script = f'''
    tell application "Calendar"
      launch
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
    await _ensure_calendar_running()
    script = f'''
    tell application "Calendar"
      launch
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
