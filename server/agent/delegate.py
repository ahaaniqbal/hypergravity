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
ANY_URL = re.compile(r"https?://[\w.-]+\.[a-z]{2,}[\w/.\-?=&#]*", re.I)
LOCAL_PATH = re.compile(r"(/(?:private/)?tmp/[\w./\-]+\.html?)", re.I)


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    return "-".join(words) or "build"


def _find_result(output: str) -> str | None:
    """A deployed URL, or failing that a file it actually wrote."""
    for pattern in (DEPLOY_URL, ANY_URL, LOCAL_PATH):
        if found := pattern.findall(output or ""):
            return found[-1].rstrip(".,)]\"'")
    return None


async def build_and_report(request: str, deadline_seconds: int = DEADLINE) -> str:
    """Run a coding agent on the brief and return something worth texting."""
    if not shutil.which(CLAUDE):
        return "There's no coding agent installed on this Mac, so I couldn't start it."

    workdir = WORKSPACE / f"{int(time.time())}-{_slug(request)}"
    workdir.mkdir(parents=True, exist_ok=True)

    brief = (
        f"{request}\n\n"
        "Build it as a single self-contained HTML file in this directory. "
        "If a deploy tool is available, deploy it and print the URL. "
        "Finish by printing the deployed URL, or the absolute file path if you "
        "could not deploy. Do not ask questions — nobody is at the keyboard."
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
        return (
            f"The build was still running after {deadline_seconds // 60} minutes, "
            "so I stopped waiting. Nothing was deployed."
        )

    output = out.decode(errors="replace")
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
        return f"It built {', '.join(made[:3])} in {workdir}, but didn't produce a link."
    tail = " ".join(output.split())[-200:]
    return f"The build didn't produce anything I can point you at. It said: {tail}"


def public_link(path: Path) -> str | None:
    """A URL for a file we just built, served over the tunnel we already have.

    The alternative is texting someone an absolute path on a laptop they are
    nowhere near.
    """
    host = os.getenv("TUNNEL_HOST", "").strip()
    if not host or not path.exists():
        return None
    try:
        rel = path.resolve().relative_to(WORKSPACE.resolve())
    except ValueError:
        return None
    return f"https://{host}/build/{rel}"
