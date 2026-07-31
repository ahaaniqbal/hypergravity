"""Say something while a tool runs.

A tool call is a latency spike. Browsing takes eight seconds, a booking two,
and in that window the caller hears nothing at all — which reads as a dropped
call, not as thinking. Speaking a short line the moment the tool starts doesn't
make anything faster; it moves the caller's first sound from ~3s to ~0.3s, and
that is the difference between a pause and a dead line.

Two rules keep this from becoming noise:

Only fill for tools that are actually slow. A filler in front of a 200ms call
arrives after the answer and sounds unhinged.

Never say anything the tool hasn't earned. "Let me check" is safe; "booking that
now" is a claim about an action that may still fail, and this whole build exists
to avoid narrating outcomes before they happen.
"""

from __future__ import annotations

import random

# Measured on real calls. Anything under ~1s needs no cover.
SLOW_TOOLS = {
    "book_table",
    "book_restaurant_table",
    "browse_the_web",
    "look_up_on_the_web",
    "run_on_mac",
    "control_app",
    "click_in_any_app",
    "add_to_calendar",
    "add_to_mac_calendar",
    "send_sms_confirmation",
    "text_me_an_sms",
    "work_in_background",
}

# Deliberately non-committal: each one is true the instant it is spoken.
_LINES: dict[str, tuple[str, ...]] = {
    "book_table": ("Let me try that.", "One moment."),
    "browse_the_web": ("Let me look.", "Give me a second.", "Looking now."),
    "look_up_on_the_web": ("Let me look that up.", "One sec."),
    "run_on_mac": ("Let me check.", "One moment."),
    "control_app": ("Opening that now.", "One second."),
    "click_in_any_app": ("One moment.", "Let me find it."),
    "add_to_calendar": ("Adding that now.",),
    "send_sms_confirmation": ("Sending that now.",),
    "work_in_background": ("Right, I'll get on that.",),
}
_LINES["book_restaurant_table"] = _LINES["book_table"]
_LINES["add_to_mac_calendar"] = _LINES["add_to_calendar"]
_LINES["text_me_an_sms"] = _LINES["send_sms_confirmation"]

_GENERIC = ("One moment.", "Let me check.", "One second.")

# Repeating the same line twice running is worse than silence — it sounds like a
# recording. Remember the last one and pick something else.
_last: str | None = None


def line_for(tool_names: list[str]) -> str | None:
    """A filler for this batch of calls, or None if none are slow enough."""
    global _last

    slow = [n for n in tool_names if n in SLOW_TOOLS]
    if not slow:
        return None

    options = list(_LINES.get(slow[0], _GENERIC))
    if len(options) > 1 and _last in options:
        options.remove(_last)

    chosen = random.choice(options)
    _last = chosen
    return chosen
