"""Hand a build job to a real coding agent, and text back what it produced.

The shape this exists for: you're out, you ring your Mac, you ask for a landing
page. A coding agent runs headless on the machine, writes the files, deploys if
it can, and the URL arrives as a text a few minutes later.

The first version typed the brief into Claude Desktop and watched the screen.
That failed for a reason worth recording: the composer is a web view, so the
accessibility tree exposes no editable field at all. The keystrokes went
nowhere, the previous message stayed in the box, and — because delivering a
keystroke is not the same as sending a message — it reported success and waited
for a URL that could never arrive.

``claude -p`` needs none of that. No focus, no window, no pixels: a process with
real output we can read. What gets texted is taken from that output, never
composed, so there is no path by which this invents a link.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

CLAUDE = shutil.which("claude") or "claude"
WORKSPACE = Path("/tmp/hypergravity-builds")
DEADLINE = 12 * 60

# Deployment hosts worth reporting. A bare http:// match picks up documentation
# links and package registries, which is worse than finding nothing.
DEPLOY_URL = re.compile(
    r"https?://[\w.-]*(?:vercel\.app|netlify\.app|pages\.dev|github\.io|onrender\.com|railway\.app|surge\.sh)[\w/.\-?=&#]*",
    re.I,
)
# Deliberately NOT a general URL match. A failed run prints its own
# troubleshooting links — "Invalid API key, see docs.anthropic.com/…" — and a
# general matcher happily texted the caller an Anthropic billing page as their
# finished landing page.
ANY_URL = re.compile(r"https?://[\w.-]+\.[a-z]{2,}[\w/.\-?=&#]*", re.I)
_NOT_A_DELIVERABLE = re.compile(
    r"(anthropic|console\.|docs\.|github\.com|npmjs|stackoverflow|/troubleshooting|/billing|/rate-limits)",
    re.I,
)
LOCAL_PATH = re.compile(r"(/(?:private/)?tmp/[\w./\-]+\.html?)", re.I)


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    return "-".join(words) or "build"


def _find_result(output: str) -> str | None:
    """A deployed URL, or failing that a file that really exists."""
    if found := DEPLOY_URL.findall(output or ""):
        return found[-1].rstrip(".,)]\"'")

    for candidate in reversed(ANY_URL.findall(output or "")):
        url = candidate.rstrip(".,)]\"'")
        if not _NOT_A_DELIVERABLE.search(url):
            return url

    for candidate in reversed(LOCAL_PATH.findall(output or "")):
        path = candidate.rstrip(".,)]\"'")
        if Path(path).exists():   # a path it merely mentioned is not a result
            return path
    return None


async def build_and_report(request: str, deadline_seconds: int = DEADLINE) -> str:
    """Run a coding agent on the brief and return something worth texting."""
    # Raise rather than return on failure: the caller marks a job DONE and
    # texts "Done — <this text>" for anything returned, so a returned excuse
    # arrives as a success message.
    if not shutil.which(CLAUDE):
        raise RuntimeError("there's no coding agent installed on this Mac")

    workdir = WORKSPACE / f"{int(time.time())}-{_slug(request)}"
    workdir.mkdir(parents=True, exist_ok=True)

    brief = (
        f"{request}\n\n"
        "Build it as a single self-contained index.html in this directory — "
        "inline CSS, no build step, no external assets.\n\n"
        "Then DEPLOY it. You have the Vercel MCP connected: use it. Deploying is "
        "the point of the job, not an optional extra — the person who asked for "
        "this is not at a computer and can only be sent a link.\n\n"
        "Print the deployed URL on its own line as the very last thing you say. "
        "If the deploy genuinely fails, print the absolute file path instead and "
        "say deployment failed. Never invent a URL.\n\n"
        "Do not ask questions — nobody is at the keyboard to answer them."
    )

    logger.info(f"dispatching coding agent in {workdir}: {request[:70]}")
    proc = await asyncio.create_subprocess_exec(
        CLAUDE, "-p", brief, "--permission-mode", "acceptEdits",
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=deadline_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"the build was still running after {deadline_seconds // 60} minutes, "
            "so I stopped waiting — nothing was deployed"
        )

    output = out.decode(errors="replace")
    if proc.returncode != 0:
        tail = " ".join(output.split())[-200:]
        raise RuntimeError(f"the coding agent exited with an error: {tail}")

    result = _find_result(output)

    if result:
        logger.info(f"build produced {result}")
        # A local path is useless in a text message. We already have a public
        # tunnel pointing at this machine, so anything built here can be served
        # over it — a real link they can open from the pavement, without
        # depending on a deploy service being wired up.
        if not result.startswith("http"):
            if link := public_link(Path(result)):
                return f"Done — {link}"
        return f"Done — {result}"

    # Nothing to point at. Say what it said rather than claiming a result.
    made = sorted(p.name for p in workdir.iterdir() if p.is_file())
    if made:
        raise RuntimeError(
            f"it built {', '.join(made[:3])} but produced no link I can send you"
        )
    tail = " ".join(output.split())[-200:]
    raise RuntimeError(f"the build produced nothing. It said: {tail}")


def tunnel_host() -> str:
    """The tunnel this process is actually serving on.

    The --proxy argument wins over the environment because it is the only one of
    the two that is necessarily current: run.sh discovers the tunnel at startup
    and passes it, whereas TUNNEL_HOST in .env is whatever was true the last time
    anyone wrote it down. A quick tunnel gets a new name on every restart, so the
    recorded value is stale by default — and a stale value here means texting
    somebody a link to a hostname that stopped resolving two restarts ago.
    """
    if "--proxy" in sys.argv:
        i = sys.argv.index("--proxy")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip()
    return os.getenv("TUNNEL_HOST", "").strip()


def public_link(path: Path) -> str | None:
    """A URL for a file we just built, served over the tunnel we already have.

    The alternative is texting someone an absolute path on a laptop they are
    nowhere near.
    """
    host = tunnel_host()
    if not host or not path.exists():
        return None
    try:
        rel = path.resolve().relative_to(WORKSPACE.resolve())
    except ValueError:
        return None
    return f"https://{host}/build/{rel}"
