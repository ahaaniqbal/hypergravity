"""The agent calls *you*.

Every other way this product closes the loop still ends with you holding a phone
and wondering. You ring it, hang up, and wait for a text. This is the other
direction: the work finishes, and your Mac rings you to say so — and because the
line is live, you can answer back and keep going.

The trick is that nothing new had to be built to carry the audio. Telnyx streams
an inbound call to us as ``{"event":"media","media":{"payload": <base64>}}`` over
a WebSocket — 8 kHz mu-law in 160-byte, 20 ms frames. That is byte-for-byte what
an RTP payload already is. So instead of writing an RTP transport for Pipecat, we
originate the call over SIP and then *impersonate Telnyx* to our own bot: connect
to its WebSocket as a client, send a synthetic ``start`` frame, and shovel
payloads between the socket and the wire.

The bot needs no changes to serve this. It cannot tell the difference, which is
the point — an outbound call gets the same brain, tools, ledger and gate as an
inbound one, because it *is* the same pipeline.

Two details that matter. RTP is paced off a monotonic schedule rather than a
sleep loop, because drift shows up as audible warble within seconds. And silence
is sent when the queue is empty: a phone line with no packets on it reads as a
dropped call, so the gaps have to be filled with mu-law zero.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
import uuid

import websockets
from loguru import logger

from . import events, sip

BOT_WS = os.getenv("HG_BOT_WS", "ws://127.0.0.1:7860/ws")

FRAME_BYTES = 160          # 20 ms of 8 kHz mu-law
FRAME_SECONDS = 0.02
SILENCE = b"\xff" * FRAME_BYTES  # mu-law zero, not 0x00
MAX_CALL_SECONDS = float(os.getenv("HG_OUTBOUND_MAX_SECONDS", "300"))
# Silence on the RTP socket that means the far end is gone rather than thinking.
HANGUP_AFTER_SILENCE = float(os.getenv("HG_HANGUP_AFTER_SILENCE", "6"))
# Generous on purpose. TTS hands us a whole utterance far faster than the 20 ms
# per frame the wire drains it at, so the queue legitimately holds the entire
# sentence. An earlier 2-second cap silently dropped the tail of anything longer
# — the bot appeared to stop talking mid-thought. This only exists to stop
# unbounded growth if something upstream goes haywire; 160 bytes a frame makes
# a minute of buffer cost nothing.
JITTER_MAX_FRAMES = 3000   # ~60s
# How long after the bot finishes to keep ignoring the line. Covers the round
# trip plus whatever the handset's speaker leaks back into its own microphone.
#
# Kept deliberately short. Every millisecond here is a millisecond of the caller
# being unheard, and on an outbound call they answer the phone and start talking
# immediately — the front of "can you check Kayak for flights to LA" was being
# eaten, leaving a fragment that sounded like someone clearing their throat. Too
# long is a worse failure than too short: self-interruption is obvious and
# recoverable, silently deafening the caller for half a second is neither.
ECHO_TAIL = float(os.getenv("HG_ECHO_TAIL", "0.25"))

# What the agent should open the call with, keyed by the stream id we invent for
# it. bot.py reads this when a session starts and finds an outbound stream id.
PENDING: dict[str, dict[str, str]] = {}

OUTBOUND_PREFIX = "hg-out-"


def pending_opening(stream_id: str | None) -> dict[str, str] | None:
    """Claim the opening for this session, if it is one we dialled."""
    if not stream_id or not stream_id.startswith(OUTBOUND_PREFIX):
        return None
    return PENDING.pop(stream_id, None)


def _rtp_payload(packet: bytes) -> bytes | None:
    """Strip the RTP header. Length is variable — CSRCs and extensions exist."""
    if len(packet) < 12 or (packet[0] >> 6) != 2:
        return None
    csrc = packet[0] & 0x0F
    offset = 12 + 4 * csrc
    if packet[0] & 0x10:  # extension header
        if len(packet) < offset + 4:
            return None
        offset += 4 + 4 * struct.unpack("!H", packet[offset + 2: offset + 4])[0]
    return packet[offset:] or None


async def _pump_to_bot(call: sip.SipCall, ws, stop: asyncio.Event, floor: dict) -> None:
    """Caller's voice: RTP in → base64 → the bot's WebSocket.

    Deaf while the bot is talking, which is not an optimisation — it is the only
    thing that makes an originated call usable. An inbound call arrives through
    Telnyx's own media path; a call we place is a bare SIP endpoint with no echo
    cancellation anywhere in it, so the bot's voice comes back down the line and
    its own STT transcribes it. It then hears "someone" talking, interrupts
    itself half a second into the greeting, and does it again on every turn — the
    caller hears "Hi" and then a line that never says anything else.

    Half-duplex costs barge-in on outbound calls only. That is the right trade:
    a conversation you can have beats one you can interrupt.
    """
    loop = asyncio.get_running_loop()
    last_packet = time.monotonic()
    while not stop.is_set():
        try:
            packet = await asyncio.wait_for(loop.sock_recv(call.rtp, 2048), timeout=1.0)
        except asyncio.TimeoutError:
            # A live call sends RTP continuously, comfort noise included, so a
            # silent socket means the far end is gone. We never see the SIP BYE —
            # nothing reads the signalling socket after the call is answered — so
            # without this every hung-up call stayed alive until the five-minute
            # cap, talking to itself. Three of them were running at once, each
            # asking an empty line whether it was still there.
            if time.monotonic() - last_packet > HANGUP_AFTER_SILENCE:
                logger.info("callback: no RTP for "
                            f"{HANGUP_AFTER_SILENCE:.0f}s — the caller hung up")
                break
            continue
        except (OSError, asyncio.CancelledError):
            break
        last_packet = time.monotonic()
        if time.monotonic() < floor["until"]:
            continue  # our own voice on its way back
        payload = _rtp_payload(packet)
        if not payload:
            continue
        try:
            await ws.send(json.dumps({
                "event": "media",
                "media": {"payload": base64.b64encode(payload).decode()},
            }))
        except Exception:  # noqa: BLE001 — the socket closing is the normal end
            break
    stop.set()


async def _pump_to_caller(call: sip.SipCall, ws, stop: asyncio.Event, floor: dict) -> None:
    """The agent's voice: the bot's WebSocket → base64 → RTP out.

    Reading and sending are deliberately split. The bot emits audio in bursts as
    TTS produces it; the wire needs one frame every 20 ms whatever happens.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def reader() -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("event") != "media":
                    continue
                data = base64.b64decode(msg["media"]["payload"])
                for i in range(0, len(data), FRAME_BYTES):
                    frame = data[i: i + FRAME_BYTES]
                    if len(frame) < FRAME_BYTES:
                        frame += SILENCE[len(frame):]
                    if queue.qsize() < JITTER_MAX_FRAMES:
                        queue.put_nowait(frame)
                # Hold the floor until everything queued has actually played out,
                # plus a tail for the round trip back. Measured off the queue
                # rather than the WebSocket, because TTS arrives in bursts far
                # faster than 20 ms per frame.
                playout = queue.qsize() * FRAME_SECONDS
                floor["until"] = max(floor["until"], time.monotonic() + playout + ECHO_TAIL)
        except Exception:  # noqa: BLE001
            pass
        finally:
            stop.set()

    async def sender() -> None:
        seq, ts, ssrc = 0, 0, int.from_bytes(os.urandom(4), "big")
        started = time.monotonic()
        sent = 0
        while not stop.is_set():
            try:
                frame = queue.get_nowait()
            except asyncio.QueueEmpty:
                frame = SILENCE
            header = struct.pack(
                "!BBHII", 0x80, 0, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc
            )
            try:
                call.rtp.sendto(header + frame, call.remote_media)
            except OSError:
                break
            seq, ts, sent = seq + 1, ts + FRAME_BYTES, sent + 1
            # Pace off elapsed time, not off accumulated sleeps.
            target = started + sent * FRAME_SECONDS
            await asyncio.sleep(max(0.0, target - time.monotonic()))
        logger.debug(f"callback: sent {sent} rtp frames")

    await asyncio.gather(reader(), sender())


async def call_and_say(to: str, opening: str, task_id: str = "") -> bool:
    """Ring ``to`` and have the agent open with ``opening``. True if they answered.

    The whole conversation is a normal session — the caller can reply, ask for
    more, or interrupt, because on the other side of the socket this is
    indistinguishable from a call they placed themselves.
    """
    if not sip.configured():
        logger.warning("callback: SIP is not configured — cannot place the call")
        return False

    try:
        call = await sip.place_call(to)
    except sip.SipError as e:
        logger.warning(f"callback: {to} not reached: {e}")
        return False

    stream_id = OUTBOUND_PREFIX + uuid.uuid4().hex[:12]
    PENDING[stream_id] = {"opening": opening, "task_id": task_id}
    events.step("calling you back", "pending", opening[:60])

    stop = asyncio.Event()
    # Shared between the two pumps: the wall-clock time until which the line
    # belongs to the bot and anything arriving on it is our own echo.
    floor = {"until": 0.0}
    ws = None
    try:
        ws = await websockets.connect(BOT_WS, max_size=None)
        # The runner reads exactly these fields to build call_data. `from` is the
        # human on the line — keeping that orientation means the caller
        # personalisation, memory and ledger lookups all work unchanged.
        await ws.send(json.dumps({
            "event": "start",
            "stream_id": stream_id,
            "start": {
                "call_control_id": stream_id,
                "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1},
                "from": to,
                "to": sip.CALLER_ID,
            },
        }))
        await asyncio.wait_for(
            asyncio.gather(
                _pump_to_bot(call, ws, stop, floor),
                _pump_to_caller(call, ws, stop, floor),
            ),
            timeout=MAX_CALL_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.info(f"callback: {to} hit the {MAX_CALL_SECONDS:.0f}s cap")
    except Exception as e:  # noqa: BLE001 — a failed callback must not lose the result
        logger.error(f"callback: bridge to {to} failed: {e}")
        return False
    finally:
        stop.set()
        PENDING.pop(stream_id, None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        call.hangup()

    logger.info(f"callback: finished call to {to} ({time.time() - call.answered_at:.0f}s)")
    events.step("calling you back", "done", f"spoke to {to}")
    return True
