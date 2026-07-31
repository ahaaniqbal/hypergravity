#!/bin/bash
# Is the line actually answerable, right now?
#
# ./run.sh status reports each part separately and every part can look healthy
# while calls still fail. This asks the only question that matters — would a
# call work this second — and says what to do if not.
cd "$(dirname "$0")"
set -a; . ./.env 2>/dev/null; set +a

U=$(cat /tmp/tunnel_url.txt 2>/dev/null)
CODE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$U/ws" 2>/dev/null)
NUM=$(curl -sS -X POST "$A1_BASE/api/numbers/claim" -H "X-Team-Key: $A1_TEAM_KEY" 2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('phone_number',''))" 2>/dev/null)

if [ "$CODE" = "200" ] && [ -n "$NUM" ]; then
  printf "\033[32m✓ READY\033[0m — call %s\n" "$NUM"
  [ "$NUM" != "$A1_PHONE_NUMBER" ] && printf "  \033[33m! number changed from %s — update .env\033[0m\n" "$A1_PHONE_NUMBER"
  exit 0
fi

printf "\033[31m✗ NOT READY\033[0m — a call would fail right now\n"
pgrep -f "bot.py" >/dev/null || printf "  bot is down    →  ./run.sh\n"
pgrep -f cloudflared >/dev/null || printf "  tunnel is down →  ./run.sh\n"
[ "$CODE" != "200" ] && printf "  webhook returns %s (want 200)\n" "$CODE"
exit 1
