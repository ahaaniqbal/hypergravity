"""Do things on this Mac — the general escape hatch.

Two verbs cover almost everything without needing any macOS permission grant:

``run`` — a shell command. Files, git, running code, ``open -a`` to launch apps,
system queries. This is what makes "write me a script and run it" or "how much
disk have I got left" work at all.

``tell_app`` — AppleScript against an app's scripting dictionary. Mail, Notes,
Messages, Music, Finder, Numbers, Safari, Calendar. These are supported APIs
rather than simulated clicks, so they are far more reliable than driving the UI.

On safety: this is deliberately powerful, and it is reachable from a phone call,
so a small set of irreversible operations is refused outright. The point is not
to build a sandbox — it is the user's own machine and they asked for this — but
a misheard word should not be able to wipe a disk. Everything refused is
reported honestly rather than silently skipped.
"""

from __future__ import annotations

import asyncio
import re
import subprocess

from loguru import logger

TIMEOUT_SECONDS = 45.0
MAX_OUTPUT = 4000

# Irreversible or credential-exposing. Matched loosely on purpose: a false
# refusal costs one spoken sentence, a false allow can cost the machine.
_REFUSE = [
    (re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)*(/|~|\$HOME)\s*$"), "recursive delete of a home or root path"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"), "recursive force delete"),
    (re.compile(r"\b(mkfs|diskutil\s+erase|dd\s+if=.*of=/dev/)"), "disk formatting"),
    (re.compile(r":\(\)\s*\{.*\};\s*:"), "fork bomb"),
    (re.compile(r"\bsudo\b"), "sudo — ask the user to run it themselves"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "shutting the machine down"),
    (re.compile(r"\bsecurity\s+(find|dump)-(generic|internet)-password"), "reading the keychain"),
    (re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(ba)?sh"), "piping a download straight into a shell"),
    (re.compile(r"\bgit\s+push\b.*--force"), "force push"),
]


class MacControlError(RuntimeError):
    pass


def _refusal(command: str) -> str | None:
    for pattern, why in _REFUSE:
        if pattern.search(command):
            return why
    return None


def _trim(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n…[output truncated]"


async def run(command: str, cwd: str | None = None) -> dict[str, str | int | bool]:
    """Run a shell command and return what it printed.

    Returns the exit code as well as the output: a command that fails must not
    look like one that succeeded silently.
    """
    if why := _refusal(command):
        logger.warning(f"refused shell command ({why}): {command}")
        return {
            "ran": False,
            "refused": True,
            "reason": f"I won't run that — it involves {why}.",
            "command": command,
        }

    logger.info(f"run: {command}")
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd or None,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {
            "ran": False,
            "reason": f"still running after {int(TIMEOUT_SECONDS)} seconds — I stopped waiting",
            "command": command,
        }
    except Exception as e:  # noqa: BLE001 — surface anything to the caller honestly
        return {"ran": False, "reason": str(e), "command": command}

    return {
        "ran": True,
        "exit_code": proc.returncode,
        "succeeded": proc.returncode == 0,
        "output": _trim(out.decode(errors="replace")) or "(no output)",
        "command": command,
    }


async def tell_app(app: str, script_body: str) -> dict[str, str | bool]:
    """Run AppleScript against a named app.

    ``launch`` first: scripting a closed app returns -600 "Application isn't
    running", which is the single most common failure here.
    """
    script = f'tell application "{app}"\n  launch\n  {script_body}\nend tell'
    logger.info(f"tell {app}: {script_body[:80]}")
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": f"{app} did not respond in time"}

    if proc.returncode != 0:
        return {"ok": False, "reason": _trim((err or b"").decode()) or f"{app} refused the script"}
    return {"ok": True, "result": _trim(out.decode()) or "(done, no output)"}


async def list_apps() -> list[str]:
    """What is installed, so the agent can answer "can you open X"."""
    result = await run("ls /Applications /System/Applications 2>/dev/null | grep '.app$' | sed 's/.app$//'")
    if not result.get("ran"):
        return []
    return sorted({line.strip() for line in str(result.get("output", "")).splitlines() if line.strip()})
