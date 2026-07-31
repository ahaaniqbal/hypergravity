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
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from agent.counterparty import Counterparty
from agent.gateway_llm import A1GatewayLLMService
from agent.ledger import get_ledger
from agent.prompt import SYSTEM_INSTRUCTION
from agent.tools import build_tools

load_dotenv(override=True)

# The model gateway is an OpenAI *Responses* endpoint, not chat/completions.
GATEWAY_BASE_URL = os.getenv(
    "OPENAI_BASE_URL",
    "https://h3zqfzovcybu5annkciuqf47mu0cbczd.lambda-url.us-east-2.on.aws/openai/v1",
)
MODEL = os.getenv("OPENAI_MODEL", "openai.gpt-5.6-sol")

# One shared task id so a call and a desk session land on the same ledger.
# In a fuller build this would key off the caller's number.
ACTIVE_TASK_ID = os.getenv("TASK_ID", "hypergravity-live")


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Assemble and run one session on whichever transport was handed to us."""
    ledger = get_ledger(ACTIVE_TASK_ID)
    counterparty = Counterparty()

    call_data = getattr(runner_args, "call_data", None)
    if call_data and getattr(call_data, "from_number", None):
        ledger.caller_phone = call_data.from_number
        logger.info(f"caller: {ledger.caller_phone}")

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
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

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point. ``create_transport`` picks the front door."""
    transport_params = {
        # create_transport wires the Telnyx serializer (a1mobile speaks TeXML).
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
