"""HyperGravity — your Mac agent, reachable from anywhere.

One orchestrator, two front doors: an a1mobile phone number (TeXML/Telnyx) when
you are away, and the Mac's own microphone when you are at your desk. The
ledger, tools and verification gate are transport-agnostic, so a task begun at
the desk can be finished on the phone.

Run::

    uv run bot.py                      # telephony (default)
    uv run bot.py -t local             # desk microphone
"""

import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.stt import CartesiaSTTService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import (
    UserTurnStrategies,
    default_user_turn_start_strategies,
)
from pipecat.evals.transport import EvalTransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from agent import events, receipt, ui_server
from agent.counterparty import Counterparty
from agent.callback import pending_opening
from agent.fillers import line_for
from agent.gateway_llm import A1GatewayLLMService
from agent.ledger import StepState, open_task
from agent.memory import greeting_for, start_call
from agent.prompt import SYSTEM_INSTRUCTION
from agent.tools import build_tools

load_dotenv(override=True)

# The model gateway is an OpenAI *Responses* endpoint, not chat/completions.
GATEWAY_BASE_URL = os.getenv(
    "OPENAI_BASE_URL",
    "https://h3zqfzovcybu5annkciuqf47mu0cbczd.lambda-url.us-east-2.on.aws/openai/v1",
)
MODEL = os.getenv("OPENAI_MODEL", "openai.gpt-5.6-sol")

# Which task a caller lands on is decided per call by ledger.open_task(): it
# resumes genuinely unfinished work (the desk-to-phone handoff) and otherwise
# starts fresh, so a completed booking is never replayed at a new caller.
TASK_BASE = os.getenv("TASK_ID", "hypergravity-live")

# How long a caller can be silent before we check they are still there. Long
# enough to think, short enough that the line never feels dead. Nine seconds was
# too eager once the agent could act on the Mac: the caller turns to watch
# something happen on screen, and gets asked three times whether they're still
# there before they've finished looking.
IDLE_SECONDS = float(os.getenv("IDLE_SECONDS", "15"))


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Assemble and run one session on whichever transport was handed to us."""
    # Who is calling has to be known *before* the task is opened: it decides
    # whether this call resumes existing work or starts clean. Opening first and
    # attaching the number afterwards let whoever rang next drop into a stranger's
    # half-finished booking.
    call_data = getattr(runner_args, "call_data", None)
    caller = getattr(call_data, "from_number", None) if call_data else None

    # A call *we* placed carries the line it should open with. Same pipeline,
    # same tools, same ledger — the only difference is who spoke first, so this
    # is a lookup rather than a separate code path.
    outbound = pending_opening(getattr(call_data, "stream_id", None) if call_data else None)

    ledger = open_task(TASK_BASE, caller=caller)
    profile = start_call(caller or "")
    if caller:
        ledger.note(phone=caller)
        logger.info(f"caller: {caller} → task {ledger.task_id}")

    counterparty = Counterparty()
    events.reset()  # don't replay the previous caller onto the panel

    # Process-wide, not per-call: run_bot runs for every incoming call, and
    # starting this here meant the second caller tried to rebind a held port and
    # killed the process.
    ui_server.start()

    # No calendar warm-up. Reading it at call start launched Calendar and raised
    # an Automation prompt on *every* call, for a clash check the caller might
    # never need — and a denied prompt derailed the conversation. Nothing should
    # touch the calendar until someone asks for something calendar-related.

    # Cartesia for both legs: Deepgram signup was down on the day, and Cartesia's
    # Live STT runs on the key we already had. One vendor, one failure domain.
    stt = CartesiaSTTService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaSTTService.Settings(
            model=os.getenv("CARTESIA_STT_MODEL", "ink-whisper"),
            language="en",
        ),
    )
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
        ),
    )

    tools_schema, handlers = build_tools(ledger, counterparty)

    def _task_state() -> str:
        """What has actually happened, in a form that survives trimming."""
        done = [s.name for s in ledger.steps if s.state is StepState.DONE]
        failed = [s.name for s in ledger.steps if s.state is StepState.FAILED]
        if not done and not failed:
            return ""
        bits = []
        if done:
            bits.append("already done: " + ", ".join(done[-5:]))
        if failed:
            bits.append("failed: " + ", ".join(failed[-3:]))
        if ledger.verified:
            bits.append("verified: " + ", ".join(ledger.verified))
        return (
            "SO FAR THIS CALL — " + "; ".join(bits) +
            ". Don't greet them again or ask what they need; carry on from here."
        )

    llm = A1GatewayLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=GATEWAY_BASE_URL,
        state=_task_state,
        settings=A1GatewayLLMService.Settings(
            model=MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
            # No reasoning config: this gateway rejects the field outright.
            # The adapter strips it anyway, but asking for it was pointless.
        ),
    )

    for name, handler in handlers.items():
        # Look-ups are cheap to abandon mid-sentence; the gate and the booking
        # attempt must finish even if the caller talks over us, or we would lose
        # track of a side effect that really happened.
        llm.register_function(
            name,
            handler,
            cancel_on_interruption=name in ("check_availability", "task_status"),
        )

    context = LLMContext(tools=tools_schema)

    # Turn-taking is the single biggest cost on this call. The default smart-turn
    # analyzer waits 3s of silence before believing the caller has finished, and
    # measured on a real call that was roughly 60% of the delay — far more than
    # the model itself. One second still lets people pause mid-sentence without
    # being cut off, and takes two seconds off every single turn.
    turn_analyzer = LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=1.0))

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            # A UserTurnStrategies pair, not a bare list. The controller reads
            # .start and .stop off it, so a list raised "'list' object has no
            # attribute 'start'" on every single utterance: the greeting played
            # (straight to TTS) and then the agent could not process a word the
            # caller said. Keeping the default start strategies matters too —
            # replacing them would break barge-in detection entirely.
            user_turn_strategies=UserTurnStrategies(
                start=default_user_turn_start_strategies(),
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn_analyzer)],
            ),
            # Off by default, which leaves both sides sitting in silence when a
            # caller pauses to think. On a phone that reads as a dead line.
            user_idle_timeout=IDLE_SECONDS,
        ),
    )

    # What to say when the line goes quiet, in order. None of these may sound like
    # a goodbye: the line stays open after the last one, so "I'll leave you to it"
    # read as the agent hanging up and then left the caller holding a silent
    # handset — worse than saying nothing, because they stop trying to talk.
    idle_lines = [
        "Still there?",
        "Take your time — I'm here when you're ready.",
        "I'm still on the line whenever you want to carry on.",
    ]
    idle_count = 0

    @user_aggregator.event_handler("on_user_turn_idle")
    async def _on_idle(aggregator):
        nonlocal idle_count
        if idle_count >= len(idle_lines):
            return
        line = idle_lines[idle_count]
        idle_count += 1
        logger.info(f"caller idle ({idle_count}) — prompting")
        await worker.queue_frames([TTSSpeakFrame(line)])

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    is_phone = "local" not in type(transport).__name__.lower()
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            # Telephony is narrowband; the desk mic is not.
            audio_in_sample_rate=8000 if is_phone else 16000,
            audio_out_sample_rate=8000 if is_phone else 24000,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("connected")
        resumed = bool(ledger.done or ledger.verified)
        if outbound:
            # We rang them. They have no idea why their phone is buzzing, so the
            # first sentence has to carry the reason — spoken straight to TTS,
            # because making the model compose it costs two seconds of a stranger
            # holding a silent handset they didn't ask to be holding.
            opener = outbound["opening"]
            await worker.queue_frames([TTSSpeakFrame(opener)])
            context.add_message({"role": "assistant", "content": opener})
            context.add_message(
                {
                    "role": "developer",
                    "content": (
                        "You called them, they did not call you. You have just "
                        "said the line above. Do not greet them again or ask what "
                        "they need.\n"
                        "They answered the phone mid-sentence, so their first "
                        "words may arrive clipped. If you can tell what they're "
                        "asking for, DO IT — never answer with 'go ahead', 'of "
                        "course', or anything else that hands the turn back "
                        "without doing anything. If you genuinely caught nothing, "
                        "ask them to say it again in those words.\n"
                        f"{ledger.summary()}"
                    ),
                }
            )
        elif resumed:
            # The handoff: pick the task up mid-flight rather than starting over.
            context.add_message(
                {
                    "role": "developer",
                    "content": (
                        "This caller is resuming a task already in progress. "
                        "Do not start over or re-ask what you already know.\n"
                        f"{ledger.summary()}\n"
                        "Greet them in one short sentence and say where things stand."
                    ),
                }
            )
            await worker.queue_frames([LLMRunFrame()])
        else:
            # Speak a fixed opener straight to TTS. Asking the model to compose
            # "Hi, it's HyperGravity" cost 2.4s of dead air on every answered
            # call — the caller's first impression was silence. The line is the
            # same every time, so there is nothing to generate.
            opener = greeting_for(caller or "")
            await worker.queue_frames([TTSSpeakFrame(opener)])
            context.add_message({"role": "assistant", "content": opener})
            # What we know about this caller from previous calls. Offered, not
            # assumed: a returning caller shouldn't be re-interrogated, but
            # people do change their minds.
            if brief := profile.brief():
                context.add_message({"role": "system", "content": brief})

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"disconnected — ledger:\n{ledger.summary()}")

        # Text the caller what actually happened — including what didn't. A call
        # leaves no record, and a remembered assurance is exactly what this build
        # refuses to let anyone rely on. Sent before teardown so the worker isn't
        # cancelled out from under it.
        try:
            await receipt.send(ledger)
        except Exception as e:  # noqa: BLE001 — never block hangup
            logger.warning(f"receipt failed: {e}")

        await counterparty.aclose()
        await worker.cancel()

    # Surface what the agent is hearing, thinking and saying, for the pill and
    # the judges' panel. Transcription frames are the cheapest honest signal.
    @transport.event_handler("on_client_connected")
    async def _ui_ready(transport, client):
        events.state("listening")

    @llm.event_handler("on_function_calls_started")
    async def _on_tools(service, function_calls):
        names = [fc.function_name for fc in function_calls]
        events.state("acting", _human_step(", ".join(names)))

        # Cover the tool's latency with speech. Browsing takes ~8s; without this
        # the caller hears nothing at all and assumes the line dropped.
        if filler := line_for(names):
            await worker.queue_frames([TTSSpeakFrame(filler)])

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


# Tool names are for logs; the pill says what a person would say.
_HUMAN = {
    "look_up_on_the_web": "looking that up…",
    "work_in_background": "on it — I'll text you…",
    "run_on_mac": "working on your Mac…",
    "control_app": "opening that app…",
    "check_availability": "checking what's free…",
    "book_table": "booking the table…",
    "send_sms_confirmation": "texting the confirmation…",
    "claim_task_complete": "verifying before I say it's done…",
    "task_status": "picking up where we left off…",
}


def _human_step(names: str) -> str:
    return " ".join(_HUMAN.get(n.strip(), n.strip()) for n in names.split(","))


async def bot(runner_args: RunnerArguments):
    """Entry point. ``create_transport`` wires the Telnyx serializer.

    Note on ``TELNYX_API_KEY``: the serializer defaults ``auto_hang_up=True`` and
    refuses to construct without a key, which killed the transport before any
    audio moved — an answered line with silence. a1mobile fronts Telnyx and
    issues only SIP credentials, so we have no key.

    We set a placeholder rather than building the transport by hand, because the
    runner parses ``call_data`` (stream_id, call_control_id, encoding) out of the
    websocket handshake *inside* ``create_transport``; bypassing it leaves that
    None. The only cost is one doomed REST call at hangup, which the serializer
    already wraps in try/except — by then the call is over anyway.
    """
    os.environ.setdefault("TELNYX_API_KEY", "unused-a1mobile-fronts-telnyx")

    transport_params = {
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
        # Drives the REAL pipeline headless — same STT, LLM, TTS, turn-taking
        # and tools, no phone. Every bug that broke calls today (the turn
        # strategies, the UI server exiting, a rejected gateway param, the
        # context wipe) was invisible to the HTTP-level tests and would have
        # been caught here on the first run.
        "eval": lambda: EvalTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


def _install_texml_route() -> None:
    """Answer the TeXML voice webhook on ``/ws`` as well as ``/``.

    a1mobile's ``point_number`` creates a Telnyx TeXML application, and creating
    it a second time returns 422 — so whatever URL landed on the first
    successful call is the one we are stuck with, and ours is ``…/ws``.
    Pipecat serves TeXML on ``/`` and the media stream on ``/ws``.

    FastAPI routes websocket and HTTP separately, so a ``POST /ws`` can coexist
    with the existing ``websocket /ws`` upgrade. Telnyx POSTs here for the XML,
    then connects to the very same path for audio.
    """
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from pipecat.runner.run import app

    # The --proxy argument wins over the environment, because it is the only one
    # of the two that is necessarily current. run.sh discovers the tunnel at
    # startup and passes it here, but load_dotenv(override=True) above then
    # replaces TUNNEL_HOST with whatever .env last recorded — which after a
    # reboot is a hostname that no longer resolves. Reading the env meant serving
    # TeXML that pointed Telnyx at a dead tunnel: the webhook answered, the call
    # connected, and the audio socket went nowhere. The line rings and then
    # silence, with nothing in the logs that looks wrong.
    from agent.delegate import tunnel_host

    proxy = tunnel_host()
    if not proxy:
        return

    @app.post("/ws")
    async def texml_on_ws():
        return HTMLResponse(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response>\n"
                "  <Connect>\n"
                f'    <Stream url="wss://{proxy}/ws" bidirectionalMode="rtp"></Stream>\n'
                "  </Connect>\n"
                '  <Pause length="40"/>\n'
                "</Response>"
            ),
            media_type="application/xml",
        )

    # Ring the owner on demand. This has to live inside the bot's own process:
    # the opening line is handed to the session through a module-level table, so
    # a caller in another process would place the call and then lose the reason
    # for it. It also gives the call-back a trigger that doesn't require waiting
    # out a real background job.
    @app.post("/call-me")
    async def call_me(request: Request):
        from agent.callback import call_and_say
        from agent.sip import CALLER_ID, configured

        if not configured():
            return JSONResponse({"error": "SIP is not configured"}, status_code=503)
        body = await request.json() if await request.body() else {}

        # Answer "can this place a call" without placing one. Checking SIP by
        # dialling means every health check rings a real handset, and someone
        # picks up to hear a test phrase and nothing else. Ask this instead.
        if body.get("check"):
            return JSONResponse({
                "sip_configured": True,
                "from": CALLER_ID,
                "would_call": str(body.get("to") or os.getenv("MY_PHONE", "")),
                "dialled": False,
            })

        to = str(body.get("to") or os.getenv("MY_PHONE", "")).strip()
        if not to:
            return JSONResponse({"error": "no number to call"}, status_code=400)
        opening = str(body.get("opening") or "").strip() or (
            f"Hey {os.getenv('OWNER_NAME', 'there')}, it's your Mac. "
            "You asked me to ring you — what do you need?"
        )
        # Don't block the HTTP response on a call that rings for 45 seconds.
        asyncio.create_task(call_and_say(to, opening))
        return JSONResponse({"calling": to, "from": CALLER_ID, "opening": opening})

    # Serve anything a build produced, so a text can carry a real link instead
    # of a path on a laptop the recipient is nowhere near.
    from fastapi.responses import FileResponse, PlainTextResponse
    from agent.delegate import WORKSPACE

    @app.get("/build/{path:path}")
    async def serve_build(path: str):
        target = (WORKSPACE / path).resolve()
        # Never serve outside the build directory, whatever the path says.
        if not str(target).startswith(str(WORKSPACE.resolve())) or not target.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target)

    logger.info(f"TeXML on POST /ws, builds on GET /build/… → https://{proxy}/build/")


if __name__ == "__main__":
    from pipecat.runner.run import main

    _install_texml_route()
    main()
