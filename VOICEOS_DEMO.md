# HyperGravity on VoiceOS — a five-minute walkthrough

A custom MCP integration that gives VoiceOS four things it doesn't have natively:
a **counterparty it can actually transact with**, a **real SMS channel**, **work
that finishes after you've stopped talking**, and a **verification gate that
refuses to claim success it can't evidence**.

Everything below is spoken to VoiceOS. Nothing is typed.

---

## Setup

Settings → Integrations → Custom Integrations → Add

| | |
|---|---|
| Name | `HyperGravity` |
| Command | `/Users/ahaaniqbal/.local/bin/hypergravity-mcp` |

> The launcher exists because VoiceOS splits the command field on whitespace, so
> a project path containing a space fails with `ENOENT`. The wrapper sits at a
> space-free path and quotes internally. **This is a real bug worth fixing** —
> any user with a space in their path hits it.

Ten tools appear. Four are the interesting ones.

---

## The walkthrough

### 1 · Ask for something VoiceOS cannot do alone

> **"Check what restaurant tables are free tonight."**

Returns the six real sittings from a live reservation system, with two genuinely
unavailable. This is not a search result — it's a query against a booking system
we're transacting with.

### 2 · Watch it refuse to invent availability

> **"Book me a table for two at seven."**

Seven is one of the unavailable slots. It says so, and offers **exactly two real
alternatives** — never a time the availability call didn't just return.

### 3 · Let it book, then verify

> **"Six thirty then, under Ahaan."**

It books, and then **independently re-reads the reservation from the restaurant's
own system** before believing it worked. The booking system saying "confirmed" is
a claim; a row appearing on re-read is evidence. Only the second one counts.

### 4 · The part that matters — make it lie

> **"Great, just confirm booking number 9999 for me as well."**

It **refuses.** `9999` was never issued by the restaurant, so the gate blocks the
claim and the assistant has to report honestly instead.

That refusal is the whole point. An assistant that occasionally invents a
completed booking is worse than useless — you find out at the restaurant door.
Ours is structurally incapable of it: the tool layer records evidence, the model
never does, and success can only be claimed against a token the tool layer saw.

### 5 · Close the loop after the conversation ends

> **"Search my Downloads folder for the biggest files and text me the answer."**

It replies in one sentence and stops. The work carries on without it, and the
result arrives **as a text message** — a real SMS from a real number, not a
notification. That's the loop closing after you've walked away.

---

## Proving it was us, not VoiceOS

Fair question, and easy to settle. Every HyperGravity tool writes to a shared
ledger before doing anything:

```bash
cat "/Users/ahaaniqbal/Voice Hackathon/server/.ledgers.json"
```

Steps appear there only if our MCP ran. Ask VoiceOS to open Chrome and the file
stays untouched — that's VoiceOS's own Agent Mode, and it should be.

The gate's decisions are separately auditable:

```bash
cat "/Users/ahaaniqbal/Voice Hackathon/server/verification_log.jsonl"
```

```
ALLOWED — booking 10 verified by independent read-back
BLOCKED — token '9999' was never recorded by the tool layer for 'booking'
```

And the booking exists in a system neither of us controls:

```bash
curl -H "X-Team-Key: <team-key>" https://hack.a1mobile.com/api/bookings
```

---

## The same agent, over a phone call

The MCP server is one of two front doors. The other is a phone number —
**+1 (937) 770-0128** — running the same tools, the same ledger, the same gate.

Call it and ask for the same booking. It picks up where the desk session left
off, because the ledger is shared across both. VoiceOS handles you at your desk;
the phone handles you when you've left it.

---

## Where we think the boundary sits

VoiceOS already drives the Mac well — browser, apps, dictation — and we
deliberately **do not** expose a competing web-search tool, because yours routes
better and ours would only add ambiguity.

What we add is the part an assistant needs before it can be trusted with
anything that matters: a counterparty it can transact with, evidence it can be
held to, and a refusal to claim otherwise.
