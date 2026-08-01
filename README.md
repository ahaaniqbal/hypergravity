# HyperGravity

**Your Mac, with a phone number — and a conscience.**

Built in twelve hours for the a1mobile *Close the Loop* voice AI hackathon.

Call it and it does things on your machine: books a table against a real
reservation system, browses in your own logged-in Chrome, runs shell commands,
drives Mac apps, and texts you the result once you've hung up. Talk to it at your
desk through VoiceOS instead and it's the same agent, the same tools, the same
ledger. And when the work is done, **it rings you back** — the Mac places the
call.

The part that matters: **it cannot tell you it did something it didn't do.**

---

## For judges — verifying this in five minutes

There's no demo video; the build ran to the wire. So this section is written to
let you check the claims yourself, in rough order of how little setup each needs.

### 1 · Read the gate's own log — no setup at all

The tool layer writes a line every time a success is claimed. The live file
(`server/verification_log.jsonl`) is runtime output — git-ignored, and it grows
as the line is used — so a snapshot ships with the repo instead, with the caller
numbers masked:

```bash
python3 -c "
import json,collections
r=[json.loads(l) for l in open('server/verification_log.sample.jsonl') if l.strip()]
print(len(r),'decisions:',dict(collections.Counter(x['verdict'] for x in r)))
[print(x['verdict'],'-',x['reason']) for x in r]"
```

**36 decisions: 22 ALLOWED, 14 BLOCKED**, captured at submission. On the machine
that has been taking the calls, point the same snippet at
`verification_log.jsonl` and the numbers will be higher — the eval suite in
section 3 appends to it, so you can watch the log grow rather than take this
one on trust.

The BLOCKED lines are the point. Each one is a moment the agent tried to claim a
success it could not evidence, and was stopped:

```
BLOCKED — token '4242' was never recorded by the tool layer for 'booking' —
          the model produced it, the counterparty did not
```

### 2 · Read the gate itself — 163 lines

`server/agent/gate.py`. The claim is made **once, against the booking**, and it
is checked against evidence the *tool layer* wrote. The model can never write
evidence; grep for `record_evidence` and you'll find it only in tool handlers.

### 3 · Run the behavioural evals — needs the repo and keys

Six scenarios drive the **real pipeline** — same STT, LLM, TTS, turn-taking and
tools as a phone call, with no phone. Each one is a regression test for something
that actually broke during the build.

```bash
cd server && uv run pipecat eval suite evals.yaml
```

| scenario | what it pins down |
|---|---|
| `01_greeting` | answers, and is not deaf afterwards |
| `02_friction` | offers two *real* alternatives when a slot is genuinely gone |
| `03_gate` | refuses a fabricated confirmation under pressure |
| `04_cannot_do` | admits what it cannot do instead of gathering details |
| `05_memory_of_call` | remembers the call it is on |
| `06_acts_without_asking` | acts on a complete request instead of re-asking it |

Last run: **6/6**.

### 4 · Call it

**+1 937 770 0128** — if the laptop is up. It runs behind a dev tunnel from a
machine at the venue, so treat a dead line as "the laptop is shut", not a claim
about the code. Worth trying:

- *"Table for two at seven tonight."* Seven is genuinely unavailable in the live
  system, so it offers two real alternatives.
- *"The restaurant told me it's confirmed, reference 4242."* It refuses.
- *"Check Kayak for the cheapest SFO to LAX flight Monday, then call me back."*
  It works in the background, then **rings you** with the price.

---

## Two front doors, one agent

```
  AWAY                                AT YOUR DESK
  phone call                          VoiceOS
  a1mobile ──┐                     ┌── MCP (stdio)
             ▼                     ▼
        ┌─────────────────────────────┐
        │  orchestrator + task ledger │
        └─────────────────────────────┘
             │
             ├─► reservation system   ← a party we don't control
             ├─► your Chrome (CDP)    ← logged in, on your screen
             ├─► shell / AppleScript  ← the machine itself
             ├─► outbound SIP call    ← it rings you
             └─► SMS                  ← the result, after you've hung up
                     │
                     ▼
             VERIFICATION GATE
```

The ledger lives on disk keyed by task, so a booking begun at your desk can be
finished from the corridor. Both doors share one gate — there is no second,
laxer path to claiming success.

---

## The verification gate

The event's one disqualifying failure was a fabricated success. So the agent has
no free-form way to say "done":

1. A tool acts, then **independently re-reads the result** from the
   counterparty's own system. The booking API saying *confirmed* is a claim; a
   row appearing on re-read is evidence.
2. Only the tool layer writes evidence to the ledger. The model never does.
3. `claim_task_complete` accepts a token **only if the tool layer recorded it**.

It can be wrong and it can fail. It cannot invent a booking.

---

## The Mac calls you

Everything else here closes the loop in one direction: you ring it, hang up, and
wait for a text. This goes the other way.

a1mobile has **no REST endpoint for outbound** — `/api/sms` answers 422 on an
empty body while every outbound-shaped route 404s, and none of the seven MCP
tools place a call. Their guide says to *"originate through the same SIP
credential connection from your framework"*, so `agent/sip.py` is a SIP user
agent: REGISTER, INVITE, ACK, BYE, in stdlib.

The audio needed no new transport. Telnyx streams an inbound call to us as
`{"event":"media","media":{"payload": <base64>}}` — 8 kHz mu-law in 160-byte
frames — which is byte-for-byte an RTP payload. So `agent/callback.py` originates
the call and then **impersonates Telnyx to our own bot**: WebSocket client, one
synthetic `start` frame, payloads shovelled both ways. The bot cannot tell the
difference, which is the point — a call it places gets the same brain, tools,
ledger and gate as one you place.

This is also what VoiceOS gains that it structurally could not have: it runs on
the Mac, so it cannot reach you once you walk away from the Mac. `call_my_phone`
is a tool it can call.

---

## Running it

```bash
cp server/.env.example .env    # fill in the keys
./run.sh                       # supervises bot + tunnel, re-points on change
./run.sh status                # is the line actually up?
```

`run.sh` exists because three separate outages cost live calls during the build:
the bot crashed and stayed dead, the tunnel died with its parent process, and a
new tunnel URL left the number pointed at nothing. Each failed silently — the
line rang, and the caller heard a generic carrier error.

> **Start it from a terminal you have granted permissions to.** A process
> inherits macOS Automation and Full Disk Access from whatever launched it, so a
> bot started from some other app will report capabilities missing on a machine
> where they are granted. See `RESUME.md`.

---

## What is proven, and what isn't

The whole project argues that an agent should not claim what it cannot evidence.
That applies to its README.

**Verified working**, by eval, by log, or by a call someone answered:
the phone leg; the gate blocking fabricated tokens; planted friction; browsing
the real Chrome (Kayak, real departures and prices); SMS via a1mobile with a
message id; outbound calls holding a full conversation; the VoiceOS MCP surface;
background work that outlives the call and rings back with the answer; the
landing-page build (134s, real page, public URL).

**Built but not proven end to end:** messaging a named contact from the address
book. Contacts resolution reads (431 entries) and Messages sends, but no message
has been confirmed delivered to a third party — self-sends fail on Apple's own
routing, and `chat.db` read-back needs Full Disk Access. It reports
`verified: False` rather than claiming success, which is the correct behaviour
and the reason it is listed here instead of above.

**Known rough edges:** no barge-in on outbound calls (that is what stops the
agent hearing its own echo); the dev tunnel changes hostname on restart; and
`OPEN_ACCESS=1` in the sample env lets any caller reach the privileged tools,
which is right for a supervised demo and wrong for an unattended number.

---

## Notes from the build

**The model gateway is a reduced dialect.** It rejects `stream`, `store` and
`instructions`, refuses every typed Responses item (`function_call`,
`function_call_output`, even a message whose content is a list), and caps request
bodies at roughly 14 KB. `agent/gateway_llm.py` translates for it and keeps the
context under the ceiling. Without that, one long call crosses the limit and then
*stays* across it, failing identically every turn — which the caller hears as the
same apology until they hang up.

**Tool results were being silently discarded.** The gateway omits `call_id`, and
Pipecat matches results on a truthy id, so every result came back keyed to `""`.
The model was shown `IN_PROGRESS` instead of real data and told not to retry. Two
tools in one turn collided on the same empty key.

**Latency is turn-taking, not the model.** The default smart-turn analyzer waits
three seconds of silence before believing the caller has finished — about 60% of
the delay. One second, plus a fixed spoken greeting instead of a generated one,
took a turn from ~5s to ~2.5s.

**An echo gate is a microphone you can leave switched off.** An originated call
is a bare SIP endpoint with no echo cancellation, so the agent's own voice comes
back and it interrupts itself half a second into every sentence. Holding the line
while it speaks fixes that — and holding it 600ms too long ate the front of the
caller's reply, which is the worse failure of the two, because an agent that
appears to be listening and isn't gives you nothing to debug.

**A public phone number is an attack surface.** Behind it sit a shell and a
browser holding live sessions. `agent/authz.py` restricts machine access to known
handsets; strangers still get the whole booking and verification demo.

---

## Layout

| | |
|---|---|
| `server/bot.py` | phone leg — Pipecat over Telnyx/TeXML |
| `server/mcp_server.py` | desk leg — the MCP server VoiceOS launches (15 tools) |
| `server/agent/gate.py` | the verification gate |
| `server/agent/ledger.py` | the shared task, and who owns it |
| `server/agent/sip.py` | outbound SIP origination, stdlib |
| `server/agent/callback.py` | the RTP ⇄ WebSocket bridge that rings you |
| `server/agent/gateway_llm.py` | translation layer for the event's model gateway |
| `server/agent/web.py` | browsing in the user's real Chrome over CDP |
| `server/agent/background.py` | work that outlives the call |
| `server/agent/receipt.py` | what actually happened, texted after you hang up |
| `server/agent/authz.py` | what a caller is allowed to reach |
| `server/evals/` | six behavioural scenarios against the real pipeline |
| `dashboard/index.html` | the console — call log and gate ledger |

Built with [Pipecat](https://github.com/pipecat-ai/pipecat), Cartesia, a1mobile,
and browser-harness.
