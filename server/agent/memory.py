"""What the agent remembers about you between calls.

A one-shot agent forgets you the moment you hang up, so every call starts with
the same interrogation: who are you, how many people, what time. This keeps a
small profile per phone number — your name, what you usually book, anything you
have told it to remember — so the second call can open with "same as last time?"

Deliberately small and legible. It is a handful of facts in a JSON file, not a
transcript archive: the agent should recall what you *told* it, not everything it
overheard. Anything remembered can be read back on request and forgotten on
request, because a memory you cannot inspect is a liability rather than a feature.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loguru import logger

import os

STORE = Path(__file__).resolve().parent.parent / ".memory.json"

# Who owns this Mac. Known from the first call, so the agent doesn't have to
# learn its owner's name by watching them book a table.
OWNER_NAME = os.getenv("OWNER_NAME", "")
OWNER_PHONE = os.getenv("MY_PHONE", "")
MAX_BOOKINGS = 5
MAX_NOTES = 8


@dataclass
class Profile:
    """What we know about one caller."""

    phone: str
    name: str = ""
    usual_party_size: int = 0
    usual_slot: str = ""
    # Free-form things they asked to be remembered, newest last.
    notes: list[str] = field(default_factory=list)
    past_bookings: list[str] = field(default_factory=list)
    calls: int = 0
    last_seen: float = 0.0

    def brief(self) -> str:
        """The line handed to the model at the start of a call."""
        if not self.calls:
            return ""
        bits: list[str] = []
        if self.name:
            bits.append(f"This is {self.name}")
        if self.usual_party_size and self.usual_slot:
            bits.append(
                f"usually books for {self.usual_party_size} at {self.usual_slot}"
            )
        elif self.usual_party_size:
            bits.append(f"usually books for {self.usual_party_size}")
        if self.past_bookings:
            bits.append(f"last time: {self.past_bookings[-1]}")
        for note in self.notes[-3:]:
            bits.append(note)
        if not bits:
            return ""
        return (
            "WHAT YOU KNOW ABOUT THIS CALLER: "
            + "; ".join(bits)
            + ". Offer it back rather than asking again — but confirm before "
            "acting on it, since people change their minds."
        )


def _key(phone: str) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


def _load() -> dict[str, dict]:
    try:
        return json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(all_profiles: dict[str, dict]) -> None:
    try:
        STORE.write_text(json.dumps(all_profiles, indent=2))
    except OSError:  # never let a memory write break a call
        pass


def recall(phone: str) -> Profile:
    """Load a profile, ignoring anything we no longer understand.

    A stored file outlives the code that wrote it. Passing it straight into the
    dataclass means the day a field is renamed, every returning caller's memory
    raises on load — and a crash reading memory is far worse than forgetting one
    preference.
    """
    key = _key(phone)
    if not key:
        return Profile(phone="")
    raw = _load().get(key)
    if not raw:
        return Profile(phone=phone)

    known = {f for f in Profile.__dataclass_fields__}
    dropped = set(raw) - known
    if dropped:
        logger.info(f"memory: ignoring unknown field(s) {', '.join(sorted(dropped))}")
    return Profile(**{k: v for k, v in raw.items() if k in known})


def remember(profile: Profile) -> None:
    key = _key(profile.phone)
    if not key:
        return
    profile.last_seen = time.time()
    profile.notes = profile.notes[-MAX_NOTES:]
    profile.past_bookings = profile.past_bookings[-MAX_BOOKINGS:]
    all_profiles = _load()
    all_profiles[key] = asdict(profile)
    _save(all_profiles)


def start_call(phone: str) -> Profile:
    """Bump the call count and hand back what we know."""
    p = recall(phone)
    p.phone = phone or p.phone
    p.calls += 1
    remember(p)
    logger.info(f"caller {phone}: visit {p.calls}" + (f", known as {p.name}" if p.name else ""))
    return p


def learn_booking(phone: str, name: str, party_size: int, slot: str) -> None:
    """Record a booking that actually landed — never one that was merely asked for."""
    p = recall(phone)
    p.phone = phone or p.phone
    if name:
        p.name = name
    if party_size:
        p.usual_party_size = party_size
    if slot:
        p.usual_slot = slot
    entry = f"{party_size or '?'} at {slot or '?'}"
    if entry not in p.past_bookings:
        p.past_bookings.append(entry)
    remember(p)


def learn_note(phone: str, note: str) -> None:
    p = recall(phone)
    p.phone = phone or p.phone
    note = note.strip()
    if note and note not in p.notes:
        p.notes.append(note)
    remember(p)


DEFAULT_GREETING = "Hey, it's HyperGravity. What do you need?"


def greeting_for(phone: str) -> str:
    """The opening line, given who is calling.

    Spoken straight to TTS rather than generated, so it costs nothing and never
    varies in latency. Personal for someone we know, neutral for anyone else —
    a stranger being greeted by name is unsettling rather than warm, and a judge
    dialling in should hear the product introduce itself.
    """
    profile = recall(phone)
    name = profile.name or (OWNER_NAME if _key(phone) == _key(OWNER_PHONE) else "")
    if not name:
        return DEFAULT_GREETING

    # Vary it a little. The same words every time is how a returning caller
    # notices they're talking to a recording.
    first = name.split()[0]
    openers = [
        f"Hey {first}, what do you need?",
        f"{first} — what can I do?",
        f"Hi {first}, what's up?",
    ]
    return openers[profile.calls % len(openers)]


def forget(phone: str) -> bool:
    """Drop everything for a caller. Asked for, and honoured immediately."""
    key = _key(phone)
    all_profiles = _load()
    if key in all_profiles:
        del all_profiles[key]
        _save(all_profiles)
        return True
    return False
