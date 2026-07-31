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
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
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
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from agent import events, ui_server
from agent.counterparty import Counterparty
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
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

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
        else:
            context.add_message(
                {"role": "developer", "content": "Greet the caller in one short sentence."}
            )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"disconnected — ledger:\n{ledger.summary()}")
        await counterparty.aclose()
        await worker.cancel()

    # Surface what the agent is hearing, thinking and saying, for the pill and
    # the judges' panel. Transcription frames are the cheapest honest signal.
    @transport.event_handler("on_client_connected")
    async def _ui_ready(transport, client):
        events.state("listening")

    @llm.event_handler("on_function_calls_started")
    async def _on_tools(service, function_calls):
        names = ", ".join(fc.function_name for fc in function_calls)
        events.state("acting", _human_step(names))

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


# Tool names are for logs; the pill says what a person would say.
_HUMAN = {
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
