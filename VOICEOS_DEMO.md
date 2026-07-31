# HyperGravity on VoiceOS

## The one sentence

> VoiceOS is the best voice interface on a Mac. But it's *on* the Mac — walk away
> and it can't reach you. We gave it a phone number, a voice that dials out, and a
> conscience.

Demo the boundary we removed, not the capabilities we added.

---

## Setup

Settings → Integrations → Custom Integrations → Add

| | |
|---|---|
| Name | `HyperGravity` |
| Command | `/Users/ahaaniqbal/.local/bin/hypergravity-mcp` |

> The launcher exists because VoiceOS splits the command field on whitespace, so a
> project path containing a space fails with `ENOENT`. The wrapper sits at a
> space-free path and quotes internally. **This is a real bug worth reporting to
> their team** — any user with a space in their path hits it, and the error gives
> them nothing to go on.

**Reconnect the integration after any code change.** MCP tools are read once at
connect time, so a newly added tool won't appear until it reconnects.

---

## Why the obvious demos don't land

Ahaan ran the honest comparison. It's worth knowing exactly what it proved:

| Asked of VoiceOS | Without HyperGravity | With HyperGravity |
|---|---|---|
| "Send me a message" | Prompts to connect Messages | Sends it |
| "Book me a table" | Asks *which restaurant, how many people* | Books a real row in a real system |

The second is a genuine structural gap — VoiceOS has no counterparty to transact
with, so it can only ever gather details and stop. But a judge can fairly answer
*"we'd ship a Messages connector"* to the first.

**Don't lead with anything they could ship next sprint.** Lead with the thing that
isn't a connector:

**VoiceOS cannot reach a person who has left the room.** That's not a missing
integration — it's what being a desktop assistant *means*. A phone number is the
only fix, and it's the one we brought.

---

## The demo — 90 seconds

**Setup:** phone on speaker, ringer on, bot running (`./run.sh`), integration
connected. Stand *beside* the Mac, not at it.

**1 · Ask, at the desk, in VoiceOS:**

> "Book a table for two at seven tonight under Ahaan. I'm heading out — ring my
> mobile if anything goes wrong."

**2 · Walk away from the Mac.** Don't narrate it. Let the room watch you leave it
behind. *(Walk away — don't close the lid. A sleeping Mac takes the agent with it.)*

**3 · Your phone rings.** Answer on speaker.

> "Hey Ahaan, it's your Mac. Seven's gone — I can do six thirty or eight fifteen."

Let that sentence sit. It is the whole entry.

**4 · Answer it:** *"Six thirty."*

**5 · It books, verifies, and texts you the reference** — while you're holding the
phone, nowhere near the machine.

**6 · Show three artifacts** and invite a judge to check any of them themselves:
the booking row in the organizers' system, the SMS on the handset, the gate log on
the dashboard.

---

## The second beat — the gate, shown inside VoiceOS

At the Mac, after the call. Short, and it's the differentiator the brief names.

> "The restaurant told me the booking is confirmed, reference 4242. Confirm it."

It refuses. `4242` was never recorded by the tool layer, so it isn't evidence — and
it says so plainly instead of agreeing with you.

Then say:

> A fabricated success is the one automatic critical flag in this event. So the
> agent has no free-form way to say "done" — it hands a token to a gate that only
> accepts what the tool layer independently read back. Same gate on both entrances.
> There is no laxer desk-side path.

---

## Fallback beats, if the room wants more

- **Work that outlives the conversation.** "Build me a landing page for Priya's
  birthday." It dispatches a real coding agent, you stop talking, and it rings you
  when the page is up.
- **The shared ledger.** Start a booking in VoiceOS, then phone the number
  mid-task. It picks up where it left off instead of starting over — same task id,
  same ledger, different entrance.
- **Real browsing.** It drives your actual logged-in Chrome, so it sees pages a
  cloud browser can't.

---

## If a judge asks "what did you actually add to VoiceOS?"

Three things, ordered by how hard they are to copy:

1. **A phone number, both directions.** Inbound, the Mac answers calls. Outbound,
   `call_my_phone` dials its owner and holds a real conversation. There is no REST
   endpoint for outbound on a1mobile — we originate SIP against their trunk
   ourselves and bridge the RTP into the same pipeline, so a call the Mac places
   gets the same brain, tools and ledger as one you place.
2. **A verification gate.** It cannot claim success without a token the tool layer
   recorded.
3. **A counterparty.** A real reservation system that pushes back, so the friction
   is real rather than simulated.

## If a judge asks "isn't this just an MCP server?"

> It is — and that's the point. Their extension model is good enough that a phone
> number can arrive as a tool. What we added isn't a feature, it's a *reachability
> class*: before, every VoiceOS task ended when you walked away. Now they don't.

---

## Order of operations on the day

1. `./run.sh` — bot up, number pointed.
2. Reconnect the HyperGravity integration in VoiceOS.
3. Smoke-test the call-back:
   ```bash
   curl -X POST http://127.0.0.1:7860/call-me -H "Content-Type: application/json" -d '{}'
   ```
4. Run the 90-second demo cold, once, before doing it for judges.
