"""Browse the real web, in the user's own Chrome.

Backed by ``browser-harness`` over CDP, which attaches to the Chrome the user
already has open. That matters for three reasons: it needs no macOS permission
grant, it inherits their logged-in sessions (a Shopify dashboard works because
they are already signed in), and it happens in the window in front of them — a
demo audience watches the page load rather than being told it did.

The earlier version drove Safari and read the *search results* page, which is
mostly navigation and ads. It honestly reported having nothing usable, which was
correct but useless. This version lands on the real page and reads its text.

Everything here reports what was *read*. Opening a page is not acting on it.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from urllib.parse import quote_plus

from loguru import logger

HARNESS = "browser-harness"
SETTLE_SECONDS = 4
MAX_TEXT = 5000
TIMEOUT_SECONDS = 90.0

# Sites worth going to directly rather than via a search page, because the
# search result is a list of links and the answer is one hop further in.
_DIRECT = [
    (re.compile(r"\bflight", re.I), lambda q: f"https://www.google.com/travel/flights?q={quote_plus(q)}"),
    (re.compile(r"\b(weather|forecast)\b", re.I), lambda q: f"https://www.google.com/search?q={quote_plus(q)}"),
]


class WebError(RuntimeError):
    pass


def _target(query: str) -> str:
    if query.startswith(("http://", "https://")):
        return query
    for pattern, build in _DIRECT:
        if pattern.search(query):
            return build(query)
    return f"https://www.google.com/search?q={quote_plus(query)}"


def _script(url: str) -> str:
    # new_tab rather than goto_url: goto drives whatever tab the user is looking
    # at, which would hijack their work mid-demo.
    return f'''
new_tab({url!r})
wait_for_load()
import time; time.sleep({SETTLE_SECONDS})
try:
    text = js("document.body.innerText")
except Exception as e:
    text = ""
print("===TITLE===")
print(js("document.title"))
print("===TEXT===")
print(text)
'''


async def _harness(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        HARNESS,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(script.encode()), timeout=TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise WebError("the page took too long to load") from None
    return out.decode(errors="replace")


async def look_up(query_or_url: str) -> str:
    """Open a page (or search) in the user's Chrome and read it back.

    One call, because on a live phone call every extra round trip is another
    second of silence the caller has to sit through.
    """
    url = _target(query_or_url)
    logger.info(f"browse → {url}")

    raw = await _harness(_script(url))

    if "===TEXT===" not in raw:
        raise WebError(raw.strip()[-300:] or "the browser returned nothing")

    head, _, body = raw.partition("===TEXT===")
    title = head.partition("===TITLE===")[2].strip().splitlines()
    title = title[0] if title else url

    body = re.sub(r"\n{2,}", "\n", body).strip()
    body = re.sub(r"[ \t]{2,}", " ", body)
    if not body:
        raise WebError(f"'{title}' loaded but had no readable text")
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT] + "\n…[truncated]"

    return f"PAGE: {title}\nURL: {url}\n\n{body}"
