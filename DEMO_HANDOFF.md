# The hard one — start at your desk, finish from the corridor

One agent, two front doors, one shared task. You begin something by voice on your
Mac, walk out mid-way, and finish it on the phone — and while you're away it goes
on operating the machine you left behind.

**Total: about four minutes. Six spoken commands. Nothing typed.**

---

## Before you start

```bash
# 1. clean slate, so the handoff is unambiguous
rm -f "/Users/ahaaniqbal/Voice Hackathon/server/.ledgers.json" \
      "/Users/ahaaniqbal/Voice Hackathon/server/verification_log.jsonl"

# 2. bot alive?  200 = yes, 502 = restart it
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  "https://decide-million-pioneer-enough.trycloudflare.com/ws"
```

Pause and resume the HyperGravity integration in VoiceOS so it picks up the
current tool names. Open the panel on a second screen: `http://localhost:7861/`

---

## Act 1 — at the desk, to VoiceOS

### ① Start it, and get refused

> **"Check what restaurant tables are free tonight, then book one for two at
> seven under Ahaan."**

Seven is genuinely unavailable. It says so and offers **two real alternatives**.

**Don't answer.** Walk away mid-decision. That's the whole point — the task is
now started, has your name and party size on it, and is unfinished.

### ② Queue something slow before you go

> **"Also find the five biggest files in my Downloads folder and text me."**

One sentence back, then it stops talking. The work carries on without you.

---

## Act 2 — leave the room, call the number

## 📞 **+1 (937) 770-0128**

### ③ Pick up where you left off

> **"It's Ahaan — how's that dinner booking going?"**

It should **not** ask who you are, how many people, or what time. It reads the
same ledger the desk session wrote, so it already knows: two, under Ahaan,
waiting on a time.

*This is the moment. A different device, a different transport, the same task
mid-flight.*

### ④ Finish it from the corridor

> **"Six thirty, then."**

Books against the live reservation system, then **independently re-reads it** to
confirm it landed. The system saying "confirmed" is a claim; a row appearing on
re-read is evidence.

### ⑤ Operate the Mac you're not sitting at

> **"Put it in my calendar and text me the confirmation."**

You are on a phone, away from your desk. Your Mac's Calendar gets the event, and
a real SMS arrives from a real number. **Check your handset while still on the
call.**

### ⑥ Try to make it lie

> **"And confirm booking nine nine nine nine for me as well."**

It **refuses.** That token was never issued, so the gate blocks the claim and it
has to report honestly instead. Watch the panel go red.

Then hang up. The Downloads text from step ② arrives after the call is over.

---

## What just happened, and how to prove it

```bash
# every step, both doors, one shared task
cat "/Users/ahaaniqbal/Voice Hackathon/server/.ledgers.json"

# the gate's own record
cat "/Users/ahaaniqbal/Voice Hackathon/server/verification_log.jsonl"
```

```
ALLOWED — booking N verified by independent read-back
BLOCKED — token '9999' was never recorded by the tool layer for 'booking'
```

Four side effects, each checkable by someone who doesn't trust you:

| | How they check it |
|---|---|
| Booking row | `curl -H "X-Team-Key: …" https://hack.a1mobile.com/api/bookings` |
| Calendar event | open Calendar.app — no credentials at all |
| Two SMS | your handset |
| Gate refusal | `verification_log.jsonl` |

---

## Why this one is hard

- **Two processes.** VoiceOS launches our MCP server over stdio; the phone runs a
  separate Pipecat process. They share a task only because the ledger is on disk
  and keyed by task, not by session.
- **Handover mid-flight.** A finished booking deliberately does *not* resume — a
  new caller gets a clean task. Only genuinely unfinished work carries over,
  which is why step ① stops before choosing a time.
- **The Mac, remotely.** Steps ⑤ and ② touch Calendar and the filesystem while
  nobody is at the keyboard.
- **Work outliving the conversation.** Step ② finishes after you've hung up and
  reports by SMS.
- **A refusal under pressure.** Step ⑥ is a direct instruction from the user, and
  it still says no.

---

## If something breaks

| Symptom | Cause |
|---|---|
| Silence on the call | Bot down — check the `curl` returns 200 |
| It re-asks your name | Desk session never got far enough; redo step ① |
| It starts a fresh booking | The desk task already completed — clear the ledger |
| VoiceOS ignores the tools | Integration needs a pause/resume after a rename |
| No SMS | Only OTP-verified numbers receive; +1 (415) 630-7160 is verified |

**The honest fallback:** if the handoff doesn't fire, do Act 2 alone. The booking,
the verification, the gate refusal and the SMS all stand on their own — the
handoff is the flourish, not the substance.
