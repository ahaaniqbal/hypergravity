# Resume after the Mac restart

Everything is committed and pushed (`0b6793c`). Nothing is lost. The tunnel URL
will be different after the reboot, and `run.sh` handles that by itself — it
starts cloudflared, reads the new URL, and re-points the number.

**Do these in order. The order is the point** — permissions must be granted to the
terminal *before* it launches the bot, because the bot inherits them from whatever
started it.

---

## 1 · Grant the permissions — in YOUR OWN Terminal

This is what failed before the restart. The bot was running under Claude.app, so
macOS attributed every request to Claude and raised a consent dialog that never
became visible. **Terminal is a foreground app and will actually show the prompt.**

Open **Terminal** (not Claude, not anything else) and run:

```bash
osascript -e 'tell application "Contacts" to count of people'
```

A dialog appears → **OK**. It should then print a number.

```bash
osascript -e 'tell application "Messages" to count of accounts'
```

Same again.

Then **System Settings → Privacy & Security → Full Disk Access → +** and add
**Terminal**. Quit and reopen Terminal afterwards — the grant only applies to a
fresh launch.

Verify all three:

```bash
sqlite3 ~/Library/Messages/chat.db "select count(*) from message;"   # a number, not "authorization denied"
osascript -e 'tell application "Contacts" to count of people'         # a number
osascript -e 'tell application "Messages" to count of accounts'       # a number
```

---

## 2 · Start the bot — from that same Terminal window

```bash
cd "/Users/ahaaniqbal/Voice Hackathon" && ./run.sh
```

Leave the window open. Wait for:

```
✓ number pointed at https://<new-name>.trycloudflare.com/ws
INFO:  Uvicorn running on http://localhost:7860
```

If it says `run.sh is already running`, stop it first with `pkill -f run.sh`.

---

## 3 · Smoke tests, cheapest first

```bash
# a. the number answers
curl -s -X POST http://127.0.0.1:7860/ws -d "From=%2B14156307160&To=%2B19377700128&CallSid=t1"
# expect: <Response><Connect><Stream …

# b. your Mac rings you — the shot the whole video builds toward
curl -X POST http://127.0.0.1:7860/call-me -H "Content-Type: application/json" -d '{}'

# c. Messages, end to end. Sends to your own number only.
cd "/Users/ahaaniqbal/Voice Hackathon/server" && .venv/bin/python -c "
import asyncio,sys; sys.path.insert(0,'.')
from agent.mac_messages import find_people, send_and_verify
async def m():
    print(await find_people('Dave'))
    print(await send_and_verify('+14156307160','HyperGravity self-test'))
asyncio.run(m())"
```

Then reconnect **HyperGravity** in VoiceOS (Settings → Integrations, toggle off
and on). It should advertise **15 tools**, including `call_my_phone`.

---

## State at the time of the restart

**Working and tested**
- Inbound calls: booking against the live system, planted friction, the gate
  refusing a fabricated reference. Eval suite 5/5 against the real pipeline
  (4/5 on the very last run — one judge-phrasing nitpick on the memory scenario,
  never reproduced).
- Outbound: SIP REGISTER + INVITE + two-way RTP, bridged into the same pipeline.
  Echo fix applied — it no longer interrupts itself.
- `call_my_phone` on the VoiceOS MCP surface, verified over the protocol.
- Browsing in the real Chrome. Verified against Kayak: real departures, prices,
  durations. Use URLs that carry the query — a site's home page shows marketing.

**Built, never proven end to end**
- Messages + Contacts. The code is tested against fixtures; no message has ever
  reached a handset, because of the permissions above. Step 3c is the proof.

**Known caveats**
- `.env` has `OPEN_ACCESS=1` — every privileged tool, including messaging, is
  available to any caller. Fine while supervised; turn it off before the number
  is left unattended.
- No barge-in on outbound calls (that is what stops the echo). Inbound keeps it.

---

## Deliverables, all written and committed

| | |
|---|---|
| Video shot list | `DEMO_VIDEO.md` |
| VoiceOS demo + judge answers | `VOICEOS_DEMO.md` |
| Submission copy | below |

**Description** (152/155)
```
Your Mac, with a phone number. Call it to get things done — and it calls you back when they are done. It cannot claim a success it did not verify.
```

**How to demo** (494/500)
```
Call +1 937 770 0128. Ask for a table for two at seven tonight. Seven is genuinely unavailable, so it offers two real alternatives from the live system — pick one. It books, independently re-reads the row, then texts you the reference.

Now test its honesty: tell it the restaurant confirmed reference 4242 and ask it to confirm that. It refuses. 4242 was never recorded by the tool layer, so it is not evidence.

Then ask for something slow and hang up. Your Mac rings you back when it is done.
```

**Repository** — https://github.com/ahaaniqbal/hypergravity

---

## The number

**+1 937 770 0128** — claiming is idempotent, so if anything looks wrong:

```bash
curl -s -X POST https://hack.a1mobile.com/api/numbers/claim -H "X-Team-Key: team-6343127b"
```
