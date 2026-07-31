"""Hand a job to another agent on the Mac, and text back what it produced.

The shape this was built for: you're out, you ring your Mac, you ask for a
landing page. It types the request into Claude Desktop — which has its own
tools and its own Vercel connection — you hang up, and a few minutes later the
deployed URL arrives as a text.

What makes this more than "type and hope" is the watching. The job polls the
app's accessibility tree for a URL that wasn't there when it started, and only
texts once it has one. If nothing appears before the deadline it says so. There
is no version of this that invents a link, because the link is only ever read
off the screen — never composed.
"""

from __future__ import annotations

import asyncio
import re
import time

from loguru import logger

from .mac_agent import MacError, session, use_app

# Deployment hosts worth reporting. A bare http:// match picks up documentation
# links and analytics pixels, which is worse than finding nothing.
URL_PATTERN = re.compile(
    r"https?://[\w.-]*(?:vercel\.app|netlify\.app|pages\.dev|github\.io|onrender\.com|railway\.app)[\w/.\-?=&#]*",
    re.I,
)
ANY_URL = re.compile(r"https?://[\w.-]+\.[a-z]{2,}[\w/.\-?=&#]*", re.I)

POLL_SECONDS = 20
DEFAULT_DEADLINE = 12 * 60


async def _was_submitted(app: str, request: str) -> bool:
    """Did the message actually go, or is it still sitting in the input box?

    Checked by looking for a distinctive chunk of our own text in an editable
    field. Once sent, the box empties and the words move into the transcript, so
    finding them still in an input means nothing was submitted.
    """
    await asyncio.sleep(2)
    state = await _visible_text(app)
    if not state:
        return True  # can't tell; don't block on a reading we didn't get

    probe = " ".join(request.split()[:6]).lower()
    for line in state.splitlines():
        lowered = line.lower()
        editable = any(k in line for k in ("AXTextArea", "AXTextField", "AXComboBox"))
        if editable and probe and probe in lowered:
            return False
    return True


async def _visible_text(app: str) -> str:
    try:
        return await session().call(
            "get_app_state", app=app, include_screenshot=False, max_elements=400
        )
    except MacError as e:
        logger.info(f"couldn't read {app}: {e}")
        return ""


def _find_url(text: str, ignore: set[str]) -> str | None:
    for pattern in (URL_PATTERN, ANY_URL):
        for match in pattern.findall(text or ""):
            url = match.rstrip(".,)]\"'")
            if url not in ignore:
                return url
    return None


async def ask_app_and_watch(
    app: str,
    request: str,
    deadline_seconds: int = DEFAULT_DEADLINE,
) -> str:
    """Type a request into an app, then wait for a URL to appear.

    Returns something worth texting. Everything it says is read off the screen.
    """
    before = await _visible_text(app)
    # Anything already on screen is not this job's output. Without this the very
    # first poll returns a URL from whatever was open before, and the caller gets
    # a confident text pointing at last week's work.
    already = set(ANY_URL.findall(before))

    # New conversation first. Typing into whatever happened to be open would
    # land the request in the middle of someone else's thread — including,
    # cheerfully, the one they're using to build this.
    result = await use_app(
        app,
        [
            {"key": "cmd+n"},
            {"type": request},
            {"key": "return"},
        ],
    )
    if not result.startswith("Did ["):
        return f"Couldn't hand that to {app} — {result[:150]}"

    # Delivering the keystroke is not the same as sending the message. The first
    # version reported success on the strength of "we pressed Return", then sat
    # watching for a URL from a request still sitting unsent in the box. If our
    # text is still on screen, it did not go.
    if not await _was_submitted(app, request):
        logger.warning(f"{app}: request typed but not sent — retrying submit")
        await session().call("press_key", app=app, key="enter", include_state=False)
        await asyncio.sleep(2)
        if not await _was_submitted(app, request):
            return (
                f"I typed it into {app} but couldn't get it to send — the message "
                "is sitting in the box. Press return on it and I'll pick it up."
            )

    logger.info(f"delegated to {app}, watching for a link: {request[:60]}")
    started = time.time()

    while time.time() - started < deadline_seconds:
        await asyncio.sleep(POLL_SECONDS)
        text = await _visible_text(app)
        if url := _find_url(text, already):
            mins = int((time.time() - started) / 60)
            logger.info(f"{app} produced {url} after {mins}m")
            return f"{app} finished: {url}"

    mins = int(deadline_seconds / 60)
    return (
        f"{app} was still working after {mins} minutes and hadn't produced a link. "
        f"It may still finish — have a look when you're back."
    )
