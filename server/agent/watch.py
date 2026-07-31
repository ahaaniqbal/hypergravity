"""Standing briefs — watch something, and say so when it changes.

Every other voice agent is one-shot: you ask, it does, it forgets you. This is
the opposite shape. "Tell me when that build finishes." "Text me if the price
drops." You hang up and the agent keeps the brief, checks periodically, and
reaches out when the thing you cared about actually happens.

Two rules keep it honest.

It reports the *observed* change, quoting what it saw. A watcher that texts
"it's ready!" without evidence is the same failure as a fabricated booking,
wearing a stopwatch.

And it gives up out loud. A watch that expires silently is worse than one that
never started, because you spend the evening assuming it's still looking.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from loguru import logger

from . import events
from .counterparty import Counterparty, sms_delivered as _sms_delivered
from .ledger import get_ledger

DEFAULT_EVERY = 120
DEFAULT_FOR = 60 * 60
# Floor, not a default. 30s was too coarse for "tell me when the build finishes";
# anything under this just burns CPU re-reading a page nobody has touched.
MIN_EVERY = 5


@dataclass
class Watch:
    watch_id: str
    what: str
    notify: str
    task_id: str
    every: int = DEFAULT_EVERY
    expires_at: float = 0.0
    started_at: float = field(default_factory=time.time)
    checks: int = 0
    fired: bool = False
    outcome: str = ""

    @property
    def live(self) -> bool:
        return not self.fired and time.time() < self.expires_at


_WATCHES: dict[str, Watch] = {}
_TASKS: set[asyncio.Task] = set()


def watches(task_id: str = "") -> list[Watch]:
    return [
        w for w in _WATCHES.values()
        if w.live and (not task_id or w.task_id == task_id)
    ]


def _normalise(text: str) -> str:
    """Strip the noise that changes on every page load — clocks, counters, ids."""
    text = re.sub(r"\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)?", " ", text, flags=re.I)
    text = re.sub(r"\b\d{4,}\b", " ", text)
    return " ".join(text.split()).lower()


async def _notify(watch: Watch, body: str) -> None:
    cp = Counterparty()
    try:
        resp = await cp.send_confirmation_sms(to=watch.notify, body=body[:300])
        # Delivery must be positively evidenced. "No known error word in a
        # field that is usually absent" is not evidence of anything.
        if not _sms_delivered(resp):
            logger.error(f"{watch.watch_id} couldn't text {watch.notify}: {prose}")
        else:
            logger.info(f"{watch.watch_id} texted: {body[:70]}")
            get_ledger(watch.task_id).record_evidence(
                "sms", watch.notify, {"to": watch.notify, "body": body}
            )
    except Exception as e:  # noqa: BLE001
        logger.error(f"{watch.watch_id} notify failed: {e}")
    finally:
        await cp.aclose()


async def _run(
    watch: Watch,
    look: Callable[[], Awaitable[str]],
    contains: str,
    baseline: str,
) -> None:
    """Poll until the condition is met, or the brief expires."""
    want = contains.strip().lower()
    base = _normalise(baseline)

    while watch.live:
        await asyncio.sleep(watch.every)
        if watch.fired:
            return

        try:
            seen = await look()
        except Exception as e:  # noqa: BLE001 — a failed check is not a result
            logger.info(f"{watch.watch_id} check failed: {e}")
            continue

        watch.checks += 1
        events.step(f"watching: {watch.what[:30]}", "pending", f"check {watch.checks}")

        hit: str | None = None
        if want:
            if want in (seen or "").lower():
                # Quote the surrounding text: the caller should see what we saw,
                # not just be told the word appeared.
                idx = seen.lower().find(want)
                hit = seen[max(0, idx - 90) : idx + 110].strip()
        elif base and _normalise(seen) != base:
            hit = (seen or "")[:180].strip()

        if hit:
            watch.fired = True
            watch.outcome = hit
            mins = int((time.time() - watch.started_at) / 60)
            await _notify(
                watch,
                f"{watch.what} — after {mins} min:\n\n{hit}",
            )
            events.step(f"watching: {watch.what[:30]}", "done", "fired")
            return

    # Expired. Say so: a watch that dies quietly leaves someone still waiting.
    if not watch.fired:
        hours = max(1, int((time.time() - watch.started_at) / 3600))
        watch.outcome = "expired"
        await _notify(
            watch,
            f"Gave up watching {watch.what} after {hours}h and {watch.checks} checks — "
            f"nothing changed. Ask again if you still want it.",
        )
        events.step(f"watching: {watch.what[:30]}", "failed", "expired")


def start(
    what: str,
    task_id: str,
    notify: str,
    look: Callable[[], Awaitable[str]],
    baseline: str,
    contains: str = "",
    every: int = DEFAULT_EVERY,
    for_seconds: int = DEFAULT_FOR,
) -> Watch:
    """Begin a standing brief and return at once, so the caller can hang up."""
    watch_id = f"watch-{len(_WATCHES) + 1}"
    watch = Watch(
        watch_id=watch_id,
        what=what,
        notify=notify,
        task_id=task_id,
        every=max(MIN_EVERY, every),
        expires_at=time.time() + for_seconds,
    )
    _WATCHES[watch_id] = watch

    task = asyncio.create_task(_run(watch, look, contains, baseline))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    logger.info(f"{watch_id}: watching {what} every {watch.every}s for {for_seconds // 60}min")
    events.step(f"watching: {what[:30]}", "pending", "standing by")
    return watch
