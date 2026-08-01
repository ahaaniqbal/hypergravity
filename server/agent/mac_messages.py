"""Find someone in Contacts, message them, and then read the message back.

The flow this exists for: on a call, "look that up and text Dave the answer".
Three things have to be true before that is safe to do, and each is a separate
failure mode this module treats separately.

**The right Dave.** Contacts is searched, and if more than one person matches we
stop and return the list. Guessing which Dave was meant and messaging the wrong
one is the single worst thing this feature could do — worse than not sending —
so ambiguity is never resolved silently. Nor is "I could not read Contacts"
allowed to look like "nobody by that name": the caller must not hear "you have
no Dave" because macOS was withholding the address book.

**Actually sending.** AppleScript against Messages, not a simulated click. The
composer is a web view with no accessible text field, so driving it through the
accessibility tree types into nothing and reports success — the same trap
``delegate.py`` documents. ``send`` is a real scripting command.

**Proof.** AppleScript ``send`` returns without error whether or not anything
went anywhere, so it is not evidence of anything. Messages' own dictionary can
create messages but cannot read them, so the read-back has to come from outside
the app: ``chat.db``, the database Messages itself writes. That gives the
recipient handle, the text, and the send state of the row that actually exists.
Where that is unreadable we fall back to the on-screen conversation, and where
neither works we say we could not confirm it. We never upgrade "the command did
not error" into "it sent".
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

# Contacts is behind a one-time Automation consent dialog, and osascript blocks
# on that dialog until somebody clicks it. On a live call that is dead air, so
# every script here is killed rather than waited on.
LOOKUP_TIMEOUT = 12.0
SEND_TIMEOUT = 25.0

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
# chat.db timestamps are nanoseconds since 2001-01-01.
APPLE_EPOCH = 978_307_200

# Contacts stores numbers however they were typed. Messages wants a handle it
# can route. Digits-only comparison settles the difference; the country code is
# only guessed when the stored number has none.
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "+1")

# How long to keep looking for the sent row before giving up on proving it.
VERIFY_ATTEMPTS = 6
VERIFY_PAUSE = 1.0


class MessagesError(RuntimeError):
    pass


# -- osascript ------------------------------------------------------------


async def _osa(script: str, *args: str, timeout: float = LOOKUP_TIMEOUT) -> str:
    """Run AppleScript with its arguments passed as argv, and a hard timeout.

    Arguments go through ``on run argv`` rather than being interpolated into the
    source. A message dictated down a phone routinely contains quotes and
    apostrophes, and string-building the script means one of them eventually
    ends the literal early and either breaks the script or — far worse — sends a
    truncated message.
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-", *args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(script.encode()), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise MessagesError(
            "macOS is asking permission for this and nobody has clicked it — "
            "it needs allowing once on the Mac, then this works from then on."
        ) from None

    if proc.returncode != 0:
        text = (err or b"").decode().strip()
        low = text.lower()
        if "-1743" in text or "not allowed" in low or "not authorised" in low or "not authorized" in low:
            raise MessagesError(
                "I'm not allowed to use that app on this Mac — it needs allowing "
                "once in System Settings, under Privacy, Automation."
            )
        lines = [ln for ln in text.splitlines() if ln.strip()]
        raise MessagesError(lines[-1][:200] if lines else "the script failed")
    return out.decode().strip()


# -- handles --------------------------------------------------------------


def normalise(handle: str) -> str:
    """A phone number as Messages likes it, or an email left alone."""
    handle = (handle or "").strip()
    if "@" in handle:
        return handle.lower()
    digits = re.sub(r"[^\d+]", "", handle)
    if digits.startswith("+"):
        return "+" + re.sub(r"\D", "", digits[1:])
    digits = re.sub(r"\D", "", digits)
    if not digits:
        return ""
    if len(digits) == 10:  # a local number, no country code stored
        return f"{DEFAULT_COUNTRY_CODE}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def same_handle(a: str, b: str) -> bool:
    """Whether two handles are the same person.

    Compared on the last ten digits: Contacts may hold "(415) 630-7160" where
    chat.db holds "+14156307160", and a string comparison would call a correctly
    delivered message unverified.
    """
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if "@" in a or "@" in b:
        return a == b
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    if not da or not db:
        return False
    return da[-10:] == db[-10:] if min(len(da), len(db)) >= 10 else da == db


# -- who they meant -------------------------------------------------------

_FILLER = re.compile(
    r"^(?:my\s+)?(?:friend|mate|wife|husband|partner|boss|mum|mom|dad|brother|"
    r"sister|colleague|neighbour|neighbor)\s+",
    re.I,
)

_FIND_PEOPLE = '''
on run argv
  set needle to item 1 of argv
  tell application "Contacts"
    set out to ""
    repeat with p in (every person whose name contains needle)
      -- not `line`: that is AppleScript's own text-chunk keyword, and the
      -- script then fails to compile rather than at runtime.
      set thisLine to (name of p)
      repeat with ph in (phones of p)
        set thisLine to thisLine & tab & (value of ph)
      end repeat
      repeat with em in (emails of p)
        set thisLine to thisLine & tab & (value of em)
      end repeat
      set out to out & thisLine & linefeed
    end repeat
    return out
  end tell
end run
'''


def _clean_name(who: str) -> str:
    """"my friend Dave" -> "Dave". Searching for the whole phrase finds nobody."""
    return _FILLER.sub("", (who or "").strip()).strip(" .,'\"")


async def find_people(who: str) -> dict[str, Any]:
    """Search Contacts. Returns matches, and whether the search happened at all.

    ``readable`` is the important field. An empty match list from a Contacts we
    could not open means nothing, and reporting it as "no such person" is a
    confident false statement about someone's own address book.
    """
    name = _clean_name(who)
    if not name:
        return {"readable": False, "reason": "no name given", "matches": []}

    try:
        raw = await _osa(_FIND_PEOPLE, name)
    except MessagesError as e:
        logger.info(f"contacts lookup failed for {name!r}: {e}")
        return {"readable": False, "reason": str(e), "matches": [], "searched": name}

    matches = _parse_people(raw)
    # "Dave Chen" with nothing stored under that exact spelling still has a Dave
    # behind it. One retry on the first word costs a second and finds him.
    if not matches and " " in name:
        try:
            matches = _parse_people(await _osa(_FIND_PEOPLE, name.split()[0]))
        except MessagesError:
            pass
    return {"readable": True, "matches": matches, "searched": name}


def _parse_people(raw: str) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for line in (raw or "").splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if not parts or not parts[0]:
            continue
        seen: list[str] = []
        for h in (normalise(p) for p in parts[1:] if p.strip()):
            if h and h not in seen:
                seen.append(h)
        # Kept even with no number at all. Dropping them turns "Dave is in your
        # contacts but you have no number for him" into "you have no Dave",
        # which is a different and false thing to tell someone.
        people.append({"name": parts[0], "handles": seen})
    return people


# -- sending --------------------------------------------------------------

# Try iMessage, then SMS. A number that is not registered with iMessage raises
# rather than silently going nowhere, and on this Mac SMS is the fallback that
# reaches an Android phone.
_SEND = '''
on run argv
  set theHandle to item 1 of argv
  set theBody to item 2 of argv
  tell application "Messages"
    try
      set svc to 1st account whose service type = iMessage
      send theBody to participant theHandle of svc
      return "iMessage"
    on error firstError
      try
        set svc to 1st account whose service type = SMS
        send theBody to participant theHandle of svc
        return "SMS"
      on error secondError
        -- Both reasons. The SMS one alone is misleading: the usual cause is
        -- that the number is not on iMessage and this Mac has no SMS
        -- forwarding, and only the pair of errors says that.
        error "iMessage said: " & firstError & " — SMS said: " & secondError
      end try
    end try
  end tell
end run
'''


async def send(handle: str, body: str) -> str:
    """Hand the message to Messages. Returns the service it claims to have used.

    This is the app's claim, not proof. Nothing here should be reported to a
    caller without ``read_back`` agreeing.
    """
    handle, body = normalise(handle), (body or "").strip()
    if not handle:
        raise MessagesError("no number or address to send to")
    if not body:
        raise MessagesError("no message to send")
    await _ensure_messages_running()
    return await _osa(_SEND, handle, body, timeout=SEND_TIMEOUT) or "unknown"


_started = False


async def _ensure_messages_running() -> None:
    """Start Messages before scripting it.

    AppleScript's own ``launch`` cannot cold-start it — a closed Messages answers
    -600 "Application isn't running" whatever the tell block says. ``-g -j``
    starts it hidden and in the background, so answering the phone never yanks a
    chat window over whatever is on screen.
    """
    global _started
    if _started:
        return
    proc = await asyncio.create_subprocess_exec(
        "open", "-g", "-j", "-a", "Messages",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await proc.wait()
    await asyncio.sleep(1.5)
    _started = True


# -- proof ----------------------------------------------------------------


def _rows_from_chat_db(limit: int = 40) -> list[dict[str, Any]]:
    """The most recent messages we sent, straight out of Messages' own database.

    Deliberately not the return value of the ``send`` command: that is the app
    agreeing with itself. This is the row Messages wrote.
    """
    if not CHAT_DB.exists():
        raise MessagesError("there is no Messages database on this Mac")

    def sql(body_column: str) -> str:
        return (
            f"SELECT m.ROWID, m.text, {body_column}, m.is_sent, m.error, m.date, "
            "       COALESCE(h.id, c.chat_identifier) AS dest "
            "FROM message m "
            "LEFT JOIN handle h ON h.ROWID = m.handle_id "
            "LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "LEFT JOIN chat c ON c.ROWID = cmj.chat_id "
            "WHERE m.is_from_me = 1 "
            "ORDER BY m.ROWID DESC LIMIT ?"
        )

    for conn in _connections():
        try:
            # Older stores have no attributedBody column. Losing the whole
            # read-back to that would surface as "can't read the database",
            # which is a different and misleading thing to tell someone.
            for column in ("m.attributedBody", "NULL"):
                try:
                    cur = conn.execute(sql(column), (limit,))
                except sqlite3.OperationalError as e:
                    if "no such column" not in str(e).lower():
                        raise
                    continue
                return [
                    {
                        "rowid": r[0], "text": r[1], "blob": r[2], "is_sent": r[3],
                        "error": r[4], "date": r[5], "dest": r[6],
                    }
                    for r in cur.fetchall()
                ]
        except sqlite3.Error as e:
            logger.info(f"chat.db read failed on one path: {e}")
        finally:
            conn.close()
    raise MessagesError(
        "I can't read the Messages database — this Mac needs to give the "
        "terminal Full Disk Access once, in System Settings under Privacy."
    )


def _connections():
    """Read-only first; a copy second.

    chat.db is in WAL mode and Messages holds it open. A read-only handle cannot
    always create the shared-memory file it needs, and an ``immutable`` handle
    ignores the write-ahead log — which is exactly where a message sent two
    seconds ago still lives. Copying all three files sidesteps both.
    """
    try:
        yield sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        logger.info(f"chat.db read-only open failed: {e}")

    tmp = Path(tempfile.mkdtemp(prefix="hg-chatdb-"))
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(CHAT_DB) + suffix)
            if src.exists():
                shutil.copy2(src, tmp / src.name)
        yield sqlite3.connect(f"file:{tmp / CHAT_DB.name}?mode=ro", uri=True, timeout=5)
    except (OSError, sqlite3.Error) as e:
        logger.info(f"chat.db copy failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _row_says(row: dict[str, Any], body: str) -> bool:
    """Whether this row carries the message we meant to send.

    Modern Messages leaves ``text`` null and keeps the words in an archived
    ``attributedBody`` blob. We already know what we are looking for, so we look
    for it rather than trying to decode the archive.
    """
    body = (body or "").strip()
    if (row.get("text") or "").strip() == body:
        return True
    blob = row.get("blob")
    return bool(blob and body and body.encode("utf-8") in bytes(blob))


def _sent_at(row: dict[str, Any]) -> float | None:
    """When the row says it was sent, or None if it does not say.

    None rather than 0: a row whose timestamp has not been written yet must not
    look like a message from 2001 and get discarded as stale.
    """
    raw = row.get("date") or 0
    if not raw:
        return None
    seconds = raw / 1_000_000_000 if raw > 1e12 else raw
    return seconds + APPLE_EPOCH


async def read_back(handle: str, body: str, since: float) -> dict[str, Any]:
    """Find the row for the message we just sent, and say what it shows.

    Matched on recipient as well as text. A row saying the right words to the
    wrong person is not a success — it is the specific failure this whole path
    is built to avoid.
    """
    try:
        rows = await asyncio.to_thread(_rows_from_chat_db)
    except MessagesError as e:
        return {"verified": False, "reason": str(e), "checkable": False}

    for row in rows:
        if not _row_says(row, body):
            continue
        if not same_handle(str(row.get("dest") or ""), handle):
            continue
        at = _sent_at(row)
        if at is not None and at < since - 120:  # an older identical message isn't ours
            continue
        if row.get("error"):
            return {
                "verified": False,
                "checkable": True,
                "reason": f"Messages recorded a delivery error (code {row['error']}) on it",
                "to": row.get("dest"),
            }
        if not row.get("is_sent"):
            return {
                "verified": False,
                "checkable": True,
                "reason": "it is in the conversation but Messages has not marked it sent yet",
                "to": row.get("dest"),
            }
        return {
            "verified": True,
            "checkable": True,
            "how": "read back from the Messages database",
            "to": row.get("dest"),
            "text": body,
        }

    return {
        "verified": False,
        "checkable": True,
        "reason": "no such message appears in the conversation on re-check",
    }


async def _read_back_on_screen(handle: str, body: str) -> dict[str, Any]:
    """Weaker fallback: look for the words in the conversation on screen.

    Enough to say "it is in the conversation with Dave", not enough to say it
    was delivered — an undelivered message is drawn in the transcript too. Kept
    because it needs only the Accessibility grant this build already has, and a
    weaker true statement beats a strong false one.
    """
    try:
        from .mac_agent import read_from_mac

        seen = await asyncio.wait_for(read_from_mac("Messages"), timeout=20)
    except Exception as e:  # noqa: BLE001 — a fallback that fails just isn't proof
        logger.info(f"on-screen read-back unavailable: {e}")
        return {"verified": False, "checkable": False, "reason": str(e)[:120]}

    if (body or "").strip() and body.strip()[:60] in seen:
        return {
            "verified": True,
            "checkable": True,
            "partial": True,
            "how": "read off the conversation on screen (in the chat, delivery not confirmed)",
            "to": handle,
            "text": body,
        }
    return {
        "verified": False,
        "checkable": True,
        "reason": "the message is not visible in the conversation on screen",
    }


async def send_and_verify(handle: str, body: str, name: str = "") -> dict[str, Any]:
    """Send, then prove it independently. The only send path anything else uses."""
    handle = normalise(handle)
    body = (body or "").strip()
    started = time.time()

    try:
        service = await send(handle, body)
    except MessagesError as e:
        return {"sent": False, "verified": False, "to": handle, "name": name, "reason": str(e)}

    # The row is written asynchronously; looking once, immediately, finds
    # nothing and would report a message that went as one that did not.
    result: dict[str, Any] = {}
    for _ in range(VERIFY_ATTEMPTS):
        result = await read_back(handle, body, started)
        if result.get("verified") or not result.get("checkable"):
            break
        await asyncio.sleep(VERIFY_PAUSE)

    if not result.get("verified"):
        fallback = await _read_back_on_screen(handle, body)
        if fallback.get("verified"):
            result = fallback

    return {
        "sent": True,
        "service": service,
        "to": handle,
        "name": name,
        "body": body,
        **result,
    }


# -- the whole errand -----------------------------------------------------


async def message_person(who: str, body: str, handle: str = "") -> dict[str, Any]:
    """Resolve a name to one person, message them, and verify it landed.

    Stops at the first thing that is not certain. More than one match is
    returned as a question, never resolved by picking the first.
    """
    body = (body or "").strip()

    if handle.strip():
        if not body:
            return {"sent": False, "verified": False, "reason": "no message to send"}
        return await send_and_verify(handle, body, who.strip())

    found = await find_people(who)
    if not found.get("readable"):
        return {
            "sent": False, "verified": False, "found": False, "contacts_readable": False,
            "reason": found.get("reason") or "I could not open Contacts on this Mac",
        }

    matches = found.get("matches") or []
    if not matches:
        return {
            "sent": False, "verified": False, "found": False, "contacts_readable": True,
            "reason": f"nobody in Contacts matches {found.get('searched') or who!r}",
        }
    if len(matches) > 1:
        return {
            "sent": False, "verified": False, "found": True, "ambiguous": True,
            "choices": [m["name"] for m in matches[:6]],
            "reason": f"{len(matches)} people in Contacts match that name",
        }

    person = matches[0]
    if not person["handles"]:
        return {
            "sent": False, "verified": False, "found": True, "contacts_readable": True,
            "name": person["name"],
            "reason": f"{person['name']} is in Contacts but has no number or email saved",
        }
    if not body:  # a lookup, not a send
        return {
            "sent": False, "verified": False, "found": True,
            "name": person["name"], "handles": person["handles"],
        }
    return await send_and_verify(person["handles"][0], body, person["name"])
