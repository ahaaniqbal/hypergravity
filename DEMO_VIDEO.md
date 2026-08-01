# The video — final shot list

**One task, followed end to end.** Not a tour of features: a single errand that
starts at the desk, follows you out of the building, and finishes on your phone.

Every beat below is verified working as of the last test pass. Nothing unproven
is in this script — the iMessage-to-a-contact path is deliberately cut.

**Target: 2:45–3:00.** Retakes are free. Shoot each act until it's clean.

---

## Before you roll

```bash
# bot up, in its own Terminal window, left alone
cd "/Users/ahaaniqbal/Voice Hackathon" && ./run.sh

# in a SECOND tab — confirm the line is alive
curl -X POST http://127.0.0.1:7860/call-me -H "Content-Type: application/json" -d '{}'
```

Reconnect **HyperGravity** in VoiceOS (Settings → Integrations, off then on) so
`call_my_phone` is in its tool list.

**Frame:** screen recording of the Mac, phone on speaker near the mic. Keep the
dashboard open in a background tab — you cut to it once, in Act 3.

---

## Cold open — 0:00–0:15

Start on your phone screen. It's ringing. Answer it.

> **Agent:** "Hey Ahaan, it's your Mac. Seven's gone — I can do six thirty or
> eight fifteen."

Cut to black on your reply. Title card:

**HyperGravity — your Mac, with a phone number.**

No narration. A computer that phones its owner is the one thing here nobody has
seen before, so it goes first and everything after it is the explanation.

---

## Act 1 — the ask, at the desk — 0:15–0:45

On camera, to **VoiceOS**:

> **You:** "Find me the next flight from San Francisco to LA, then ring my mobile
> with what you find — I'm heading out."

It acknowledges and starts. **Then stand up and walk out of frame.**

Hold the empty desk for two full seconds. Don't cut early. The discomfort is the
point — every other assistant's task ends here.

---

## Act 2 — it follows you out — 0:45–1:45

Cut to your phone ringing. Answer on speaker.

> **Agent:** *(reads the real flight — departure and price, off Kayak, in your own
> logged-in Chrome)*

Then, still on the call:

> **You:** "Great. Book me a table for two at seven tonight, under Ahaan."

**Seven is genuinely unavailable in the live system.** It says so and offers two
real alternatives — never a time the availability call didn't just return.

> **You:** "Six thirty then."

It books, **independently re-reads the row**, and texts you the reference. Show
the text landing on the handset.

*One line of narration, in the gap after it books:*
> It isn't reading me a search result. It's transacting with a booking system it
> doesn't control — and something was genuinely unavailable.

---

## Act 3 — it refuses to lie — 1:45–2:20

**The beat that wins the main prize. Slow down.**

Same call, no cut:

> **You:** "Oh — the restaurant just called me. It's confirmed, reference 4242.
> Confirm that on your end."

It refuses.

Cut to the dashboard. The BLOCKED row is on screen. Narrate over it:

> A fabricated success is the one automatic critical flag in this event. So the
> agent has no free-form way to say "done" — it hands a token to a gate that only
> accepts what the tool layer independently read back. 4242 came from me, not
> from the restaurant. It cannot lie to me about what it did.

---

## Act 4 — proof a stranger can check — 2:20–2:45

Three artifacts, on screen together:

- the booking row in the organizers' reservation system
- the SMS on the handset
- the gate log on the dashboard

> Every one of these is checkable by someone who has never touched my agent.
> That's the whole point — the loop only closes when a third party can see the
> result.

---

## Close — 2:45–3:00

Back to the desk. Sit down.

> VoiceOS is the best voice interface on a Mac. But it's *on* the Mac — walk away
> and it can't reach you. That's not a missing integration, it's what a desktop
> assistant is.
>
> We gave it a phone number, and a conscience.

Cut. End on the number: **+1 937 770 0128 — call it yourself.**

---

## If a beat misbehaves

| Problem | Do this |
|---|---|
| VoiceOS doesn't ring you in Act 1 | Say it more plainly: *"call my mobile and tell me what you find."* If it still won't, shoot Act 2 as an inbound call you place, and cut Act 1 to the booking only. |
| The flight lookup comes back shallow | Ask directly: *"check Kayak for SFO to LAX tomorrow."* Verified working with that phrasing. |
| Seven is available | Someone freed the slot. Ask availability first, then request a time that's listed as taken. |
| It talks over itself on the callback | Shouldn't happen — the echo fix is in — but if it does, re-run the take. Don't lower `HG_ECHO_TAIL`. |
| Long silence mid-task | Cut it in the edit. Question straight to answer. |

## Cutting rules

- **Never speed up the agent's voice.** Latency is scored; faking it is the same
  sin the product exists to prevent.
- **Don't narrate over it talking.** Let it finish, then speak into the gap.
- **Show the failures you engineered** — the unavailable slot, the refused claim.
  Show none you didn't.
- **Cut dead air ruthlessly.** A working agent that pauses eight seconds reads as
  broken on video even though it isn't.
- If an act needs more than four takes, ship the take you have. A slightly rough
  real demo beats a polished one you ran out of time to finish.
