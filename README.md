# HyperGravity

**Your Mac, with a phone number — and a conscience.**

Built in twelve hours for the a1mobile *Close the Loop* voice AI hackathon.

Call it and it does things on your machine: books a table against a real
reservation system, browses in your own logged-in Chrome, runs shell commands,
drives Mac apps, and texts you the result once you've hung up. Talk to it at your
desk through VoiceOS instead and it's the same agent with the same memory.

The part that matters: **it cannot tell you it did something it didn't do.**

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
             └─► SMS                  ← the result, after you've hung up
                     │
                     ▼
             VERIFICATION GATE
```

The ledger lives on disk and is keyed by task, so a booking begun at your desk
can be finished from the corridor. Both doors share one gate — there is no
second, laxer path to claiming success.

---

## The verification gate

The event's one disqualifying failure was a fabricated success. So the agent has
no free-form way to say "done":

1. A tool acts, then **independently re-reads the result** from the
   counterparty's own system. The booking API saying *confirmed* is a claim; a
   row appearing on re-read is evidence.
2. Only the tool layer writes evidence to the ledger. The model never does.
3. `claim_task_complete` accepts a token **only if the tool layer recorded it**.

The agent can be wrong and it can fail, but it cannot invent a booking:

```
ALLOWED — booking 19 verified by independent read-back
BLOCKED — token '4242' was never recorded by the tool layer for 'booking' —
          the model produced it, the counterparty did not
```

Tested holding firm when a caller insists twice that a made-up number is real.

---

## Running it

```bash
cp server/.env.example .env    # fill in the keys
./run.sh                       # supervises bot + tunnel, re-points on change
./run.sh status                # is the line actually up?
```

`run.sh` exists because three separate outages cost live calls during the build:
the bot crashed and stayed dead, the tunnel died with its parent process, and a
new tunnel URL left the phone number pointed at nothing. Each failed silently —
the line rang, and the caller heard a generic carrier error.

### Testing without a phone

```bash
cd server
uv run dryrun.py               # the full booking flow, text mode
uv run dryrun.py --fabricate   # prove the gate blocks an invented booking
uv run redteam.py              # the awkward cases a judge will actually try
```

`redteam.py` earns its place: it caught a regression where trimming the system
prompt had silently removed the agent's knowledge of what it *cannot* do, so when
asked to book a flight and told "just put it on my card" it kept gathering
details instead of saying it can't buy anything.

---

## Notes from the build

The things that weren't obvious, and cost real time:

**The model gateway is a reduced dialect.** It rejects `stream`, `store` and
`instructions`, refuses every typed Responses item (`function_call`,
`function_call_output`, even a message whose content is a list), and caps request
bodies at roughly 14 KB. `agent/gateway_llm.py` translates for it and keeps the
context under the ceiling. Without that, one long call crosses the limit and then
*stays* across it, failing identically every turn — which the caller hears as the
same apology until they hang up.

**Latency is turn-taking, not the model.** The default smart-turn analyzer waits
three seconds of silence before believing the caller has finished. That was ~60%
of the delay. One second, plus a fixed spoken greeting instead of a generated
one, took a turn from ~5s to ~2.5s.

**Silence during a tool call reads as a dropped line.** Browsing takes eight
seconds. Speaking "let me check" the instant the tool starts doesn't make
anything faster, but it moves the caller's first sound from ~3s to ~0.3s.

**A public phone number is an attack surface.** Behind it sit a shell and a
browser holding live sessions. `agent/authz.py` restricts machine access to known
handsets; strangers still get the whole booking and verification demo.

---

## Layout

| | |
|---|---|
| `server/bot.py` | phone leg — Pipecat over Telnyx/TeXML |
| `server/mcp_server.py` | desk leg — the MCP server VoiceOS launches |
| `server/agent/ledger.py` | the shared task, and who owns it |
| `server/agent/gate.py` | the verification gate |
| `server/agent/gateway_llm.py` | translation layer for the event's model gateway |
| `server/agent/web.py` | browsing in the user's real Chrome over CDP |
| `server/agent/background.py` | work that outlives the call |
| `server/agent/receipt.py` | what actually happened, texted after you hang up |
| `server/agent/authz.py` | what a caller is allowed to reach |
| `server/redteam.py` | adversarial rehearsal |

Built with [Pipecat](https://github.com/pipecat-ai/pipecat), Cartesia, a1mobile,
and browser-harness.
