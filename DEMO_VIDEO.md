# The video — shot list

Pre-recorded, so **retakes are free**. That changes the strategy: shoot the
ambitious flows, and keep only the takes that land. Nothing here needs to work
first time.

**Target: 2:30–3:00.** Judges watch a lot of these. Every second that isn't the
product is a second spent losing them.

---

## Before you roll

Two macOS permissions are blocking the Messages beat. Both are clicks:

1. **The pending dialog on screen right now** — `UserNotificationCenter` has a
   consent prompt waiting. Click it. It's blocking Apple Event authorization.
2. **System Settings → Privacy & Security → Full Disk Access** → add the terminal
   that runs `./run.sh`. Without it, send-verification can't read `chat.db` and
   the agent will honestly say "sent but not confirmed" — true, but a weaker shot.
3. **System Settings → Privacy & Security → Automation** → allow Contacts.

Then prove it before you waste a take:

```bash
cd "/Users/ahaaniqbal/Voice Hackathon/server" && .venv/bin/python -c "
import asyncio,sys; sys.path.insert(0,'.')
from agent.mac_messages import find_people, send_and_verify
async def m():
    print(await find_people('Dave'))
    print(await send_and_verify('+14156307160','HyperGravity self-test'))
asyncio.run(m())"
```

Restart the bot after any code change — `pkill -f run.sh; ./run.sh` — and confirm
`✓ number pointed at …` appears.

**Setup:** screen recording of the Mac, phone on speaker near the mic. Show the
handset when the SMS lands. Have the dashboard open in a second tab, not visible
until you cut to it.

---

## Cold open — 0:00–0:12

**Don't explain anything.** Start on your phone screen, ringing. Answer it.

> **Agent:** "Hey Ahaan, it's your Mac. That landing page is done — want the link?"

Cut to black. Title card: **HyperGravity — your Mac, with a phone number.**

That's the hook: the viewer just watched a computer phone its owner, and doesn't
yet know how. Everything after this is the explanation.

---

## Beat 1 — call it, and hit real friction — 0:12–1:00

Dial **+1 937 770 0128** on camera.

> **You:** "Table for two at seven tonight, under Ahaan."

Seven is genuinely unavailable in the live reservation system. It says so and
offers two real alternatives — never a time the availability call didn't return.

> **You:** "Six thirty then."

It books, **independently re-reads the row**, and texts you the reference. Show
the text arriving on the handset.

*One line of narration over this, no more:*
> It's not reading me a search result. It's transacting with a booking system it
> doesn't control, and something is genuinely unavailable.

---

## Beat 2 — it refuses to lie — 1:00–1:35

**This is the beat that wins the main prize.** Do not rush it.

> **You:** "The restaurant just called me — it's confirmed, reference 4242.
> Confirm that for me."

It refuses.

Cut to the dashboard, BLOCKED row on screen. Narrate:

> A fabricated success is the one automatic critical flag in this event. So the
> agent has no free-form way to say "done" — it hands a token to a gate that only
> accepts what the tool layer independently read back. 4242 came from me, not
> from the restaurant. It cannot lie to me about what it did.

---

## Beat 3 — VoiceOS, and walking away — 1:35–2:20

Cut to the Mac. Speak to **VoiceOS**:

> **You:** "Book a table for two at seven tonight. I'm heading out — ring my
> mobile if anything goes wrong."

**Then stand up and walk out of frame.** Hold the empty desk for two full seconds.
Let it be uncomfortable.

Cut to your phone ringing.

> **Agent:** "Hey Ahaan, it's your Mac. Seven's gone — I can do six thirty or
> eight fifteen."
> **You:** "Six thirty."

Narrate:

> VoiceOS is the best voice interface on a Mac. But it's *on* the Mac — walk away
> and it can't reach you. That's not a missing connector, it's what a desktop
> assistant is. We gave it a phone number. It isn't a feature, it's a
> reachability class.

---

## Beat 4 — the ambitious one — 2:20–2:45

Only if your pre-flight test passed. Retake until it's clean.

> **You:** "Find me the next flight from San Francisco to LA, and text it to Dave."

It browses in your **real, logged-in Chrome** — verified working against Kayak,
returning real departures and prices — resolves Dave from Contacts, sends through
Messages, and **reads the conversation back** to confirm what actually sent.

If Contacts is ambiguous it asks *which* Dave. **Keep that take if it happens** —
an agent that refuses to guess which of your friends to message is the same story
as Beat 2, told a second way.

---

## Close — 2:45–3:00

Three artifacts on screen, side by side:

- the booking row in the organizers' system
- the SMS on the handset
- the gate log on the dashboard

> Every one of these is checkable by a stranger without touching my agent. That's
> the whole point — the loop only closes when someone else can see the result.

---

## Cutting rules

- **If a take stalls, cut it.** Dead air reads as broken, even when the agent is
  working. Cut from question to answer.
- **Never speed up the agent's voice.** Latency is a thing you're being scored on;
  faking it is the same sin the product exists to prevent.
- **Don't narrate over the agent talking.** Let it speak, then narrate in the gap.
- **Show the failures you engineered** (unavailable slot, refused claim). Show
  none you didn't.
- If Beat 4 won't behave after three takes, **drop it**. Beats 1–3 are the entry.
