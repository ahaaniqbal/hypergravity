"""HyperGravity — your Mac agent, reachable from anywhere.

One orchestrator, two front doors: an a1mobile phone number (TeXML/Telnyx) when
you are away, and the Mac's own microphone when you are at your desk. The
ledger, tools and verification gate are transport-agnostic, so a task begun at
the desk can be finished on the phone.

Run::

    uv run bot.py                      # telephony (default)
    uv run bot.py -t local             # desk microphone
"""

import os
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
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from agent import events, receipt, ui_server
from agent.counterparty import Counterparty
from agent.fillers import line_for
from agent.gateway_llm import A1GatewayLLMService
from agent.ledger import open_task
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
# enough to think, short enough that the line never feels dead.
IDLE_SECONDS = float(os.getenv("IDLE_SECONDS", "9"))


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Assemble and run one session on whichever transport was handed to us."""
    ledger = open_task(TASK_BASE)
    counterparty = Counterparty()

    # Pill + judges' panel, served alongside the call from this same loop.
    ui_server.start()

    call_data = getattr(runner_args, "call_data", None)
    if call_data and getattr(call_data, "from_number", None):
        ledger.caller_phone = call_data.from_number
        logger.info(f"caller: {ledger.caller_phone}")

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

    llm = A1GatewayLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=GATEWAY_BASE_URL,
        settings=A1GatewayLLMService.Settings(
            model=MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
            # Every turn was spending 35-50 tokens on reasoning for what is
            # essentially "pick a tool and say one sentence". Minimal effort
            # keeps the tool choice while cutting time-to-first-word.
            reasoning=A1GatewayLLMService.ReasoningConfig(effort="minimal"),
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
            # The analyzer is passed through a stop *strategy*, not directly —
            # LLMUserAggregatorParams takes no turn_analyzer argument, and passing
            # one raises only when the pipeline is built, so the bot imports
            # cleanly and then dies the moment a call arrives.
            user_turn_strategies=[
                TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn_analyzer),
            ],
            # Off by default, which leaves both sides sitting in silence when a
            # caller pauses to think. On a phone that reads as a dead line.
            user_idle_timeout=IDLE_SECONDS,
        ),
    )

    # What to say when the line goes quiet, in order. The last one admits defeat
    # rather than pestering someone who has walked away mid-call.
    idle_lines = [
        "Still there?",
        "Take your time — I'm here when you're ready.",
        "I'll leave you to it. Call back whenever.",
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
        if resumed:
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
            await worker.queue_frames(
                [TTSSpeakFrame("Hey, it's HyperGravity. What do you need?")]
            )
            context.add_message(
                {
                    "role": "assistant",
                    "content": "Hey, it's HyperGravity. What do you need?",
                }
            )

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
    from fastapi.responses import HTMLResponse
    from pipecat.runner.run import app

    proxy = os.getenv("TUNNEL_HOST", "")
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

    logger.info(f"TeXML also served on POST /ws → wss://{proxy}/ws")


if __name__ == "__main__":
    from pipecat.runner.run import main

    _install_texml_route()
    main()
