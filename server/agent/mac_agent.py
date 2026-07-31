"""Drive this Mac — the general capability behind "ask it anything".

Wraps ``computer-use-mcp`` (a notarized Swift MCP server, MIT) over stdio. It
drives apps through the accessibility tree *without stealing the cursor or
focus*, which is what makes it usable while someone is watching a demo.

Deliberately a small set of primitives rather than a nested agent loop. The
orchestrator already has tool-calling; giving it Mac verbs directly costs one
round trip per step instead of two, and every step shows up in the pill as
something a person would recognise.

The honesty rule carries over unchanged: these tools report what was *observed*.
They never conclude that an action worked because the command returned without
an error — where the tool layer can re-read, it does.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import Any

from loguru import logger

BINARY = os.getenv("COMPUTER_USE_MCP", shutil.which("computer-use-mcp") or "computer-use-mcp")
_TIMEOUT = 45.0


class MacError(RuntimeError):
    pass


class MacSession:
    """One stdio MCP connection to computer-use-mcp, reused across calls."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def _start(self) -> None:
        if self._proc and self._proc.returncode is None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            BINARY, "serve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "hypergravity", "version": "0.1"},
            },
        })
        await self._read()
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        logger.info("computer-use-mcp session up")

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read(self) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=_TIMEOUT)
            if not line:
                raise MacError("computer-use-mcp closed the connection")
            text = line.decode().strip()
            if text.startswith("{"):
                return json.loads(text)

    async def call(self, tool: str, **args: Any) -> str:
        """Call one tool and return its text output."""
        async with self._lock:
            await self._start()
            self._id += 1
            await self._send({
                "jsonrpc": "2.0", "id": self._id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            })
            while True:
                msg = await self._read()
                if msg.get("id") != self._id:
                    continue  # skip notifications
                if err := msg.get("error"):
                    raise MacError(f"{tool}: {err}")
                result = msg.get("result", {})
                parts = [
                    c.get("text", "")
                    for c in result.get("content", [])
                    if c.get("type") == "text"
                ]
                return "\n".join(p for p in parts if p).strip()

    async def aclose(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()


_SESSION: MacSession | None = None


def session() -> MacSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = MacSession()
    return _SESSION


async def available() -> tuple[bool, str]:
    """Whether the Mac can actually be driven, and why not if it can't.

    Only Accessibility is required. It carries reading the UI tree, clicking and
    typing — everything we actually do. Screen Recording adds screenshots, which
    we never take, so demanding it turned a working setup into a refusal.
    """
    if not shutil.which(BINARY) and not os.path.exists(BINARY):
        return False, "computer-use-mcp is not installed"
    proc = await asyncio.create_subprocess_exec(
        BINARY, "doctor",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    report = out.decode()
    for line in report.splitlines():
        if line.strip().startswith("Accessibility:"):
            if "NOT GRANTED" in line:
                return False, "Accessibility is not granted in System Settings"
            return True, "ready"
    return False, "could not read permission status"


# -- the verbs the orchestrator gets --------------------------------------

async def open_on_mac(target: str) -> str:
    """Open an app by name, or a URL in the default browser."""
    s = session()
    if target.startswith(("http://", "https://")):
        await s.call("open_url", url=target)
        return f"Opened {target}"
    await s.call("open_app", name=target)
    return f"Opened {target}"


async def read_from_mac(app: str = "") -> str:
    """Read the visible text of an app (or the frontmost window)."""
    s = session()
    try:
        return await s.call("read_text", app=app) if app else await s.call("read_text")
    except MacError:
        # read_text is picky about which app is addressable; the app state dump
        # is coarser but almost always available.
        return await s.call("get_app_state", app=app) if app else await s.call("get_app_state")


async def click_in_app(app: str, what: str) -> str:
    """Click something in an app that has no AppleScript dictionary.

    Resolves ``what`` against the accessibility tree, so no coordinates and no
    screenshots — it survives window moves and needs only the Accessibility
    grant. Reports honestly when nothing matched rather than clicking at random.
    """
    s = session()
    found = await s.call("find", app=app, query=what)
    if "No elements match" in found or not found.strip():
        return f"Nothing in {app} matches '{what}' — I did not click anything."
    try:
        await s.call("click", app=app, query=what)
    except MacError as e:
        return f"Found it in {app} but the click failed: {e}"
    return f"Clicked '{what}' in {app}. What I found: {found[:200]}"


async def use_app(app: str, actions: list[dict[str, Any]]) -> str:
    """Drive any Mac app: a short plan of menu picks, clicks, typing and keys.

    Written around *labels and menu paths* rather than element ids, because the
    caller is speaking. Resolving "the Send button" to an accessibility id is our
    job, not something to make a model guess at over the phone.

    Menu paths are the workhorse. Most real commands live in a menu, and
    ``click_menu_item`` reaches them by name without opening anything visually —
    no element lookup, no coordinates, and it works in apps whose accessibility
    tree is otherwise sparse.

    Batched into one call: each round trip is a second of silence on a live call,
    and a four-step task done one step at a time is four seconds of nothing.
    """
    s = session()
    if not actions:
        return "No actions given."

    # open_app only activates something already running — a closed app stays
    # closed and every later step quietly does nothing. The shell opener starts
    # it for real, the same way Calendar and Safari need.
    proc = await asyncio.create_subprocess_exec(
        "open", "-a", app,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        return f"Couldn't open {app}: {(err or b'').decode().strip()[:120]}. Nothing was done."
    await asyncio.sleep(1.5)

    try:
        await s.call("open_app", app=app, activate=True)
    except MacError as e:
        return f"{app} wouldn't come to the front: {e}. Nothing was done."

    steps: list[dict[str, Any]] = []
    described: list[str] = []

    for act in actions[:10]:  # batch caps at 10
        if path := act.get("menu"):
            steps.append({"tool": "click_menu_item", "path": path, "include_state": False})
            described.append(f"menu {path}")
        elif label := act.get("click"):
            hit = await s.call("find", app=app, query=label, max_results=1)
            eid = _first_element_id(hit)
            if not eid:
                return f"Couldn't find '{label}' in {app}. Nothing was done."
            steps.append({"tool": "click", "element_id": eid, "include_state": False})
            described.append(f"click {label}")
        elif (text := act.get("type")) is not None:
            steps.append({"tool": "type_text", "text": str(text), "include_state": False})
            described.append("typed")
        elif key := act.get("key"):
            steps.append({"tool": "press_key", "key": key, "include_state": False})
            described.append(key)

    if steps:
        try:
            result = await s.call("batch", app=app, actions=steps, include_screenshot=False)
        except MacError as e:
            done = ", ".join(described)
            return f"Got as far as [{done}] then {app} refused: {e}"

        # The batch can return without raising and still have done nothing —
        # an app that isn't really there answers APP_NOT_FOUND in the body.
        # Reporting the plan as though it ran is precisely the fabrication this
        # build exists to prevent, so treat any such reply as a failure.
        if _looks_like_failure(result):
            return f"NOTHING HAPPENED in {app} — {result.strip()[:200]}"

    # Read back the app's actual state, so we report what happened rather than
    # what we intended.
    try:
        after = await s.call("get_app_state", app=app, include_screenshot=False, max_elements=60)
    except MacError:
        after = ""
    if _looks_like_failure(after):
        return f"NOTHING HAPPENED in {app} — {after.strip()[:200]}"

    return f"Did [{', '.join(described)}] in {app}.\n\n{after[:1200]}"


_FAILURE_MARKERS = ("APP_NOT_FOUND", "is not running", "NOT_FOUND", "no windows")


def _looks_like_failure(text: str) -> bool:
    return any(marker in (text or "") for marker in _FAILURE_MARKERS)


def _first_element_id(found: str) -> str | None:
    """Pull an element id out of find()'s text output."""
    match = re.search(r"\b(?:id|element_id)[=:\s]+([A-Za-z0-9_\-]+)", found)
    return match.group(1) if match else None


async def act_on_mac(instruction: str, app: str = "") -> str:
    """Click or type, described in plain language.

    ``find`` resolves the description against the accessibility tree, so the
    caller does not need coordinates and the action survives layout changes.
    """
    s = session()
    found = await s.call("find", query=instruction, **({"app": app} if app else {}))
    if not found:
        return f"Could not find anything matching: {instruction}"
    return found
