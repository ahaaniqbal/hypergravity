#!/bin/bash
# Keep the phone line up.
#
# Three separate outages cost us live calls today: the bot crashed and stayed
# dead, cloudflared died with its parent process, and a new tunnel URL left the
# number pointed at nothing. Each time the failure was silent — the line rang,
# Telnyx got no valid TeXML, and the caller heard "an application error has
# occurred" with no clue why.
#
# This supervises all three: restarts the bot if it exits, brings the tunnel
# back if it dies, and re-points the number whenever the URL changes.
#
#   ./run.sh          start and supervise
#   ./run.sh status   is the line actually up?
#
# Logs to /tmp/hypergravity.log so a crash can be read after the fact.

set -uo pipefail
cd "$(dirname "$0")"

LOG=/tmp/hypergravity.log
URL_FILE=/tmp/tunnel_url.txt
PORT=7860

set -a; . ./.env; set +a

say() { printf "\033[36m▸\033[0m %s\n" "$1"; }
ok()  { printf "\033[32m✓\033[0m %s\n" "$1"; }
bad() { printf "\033[31m✗\033[0m %s\n" "$1"; }

current_url() { cat "$URL_FILE" 2>/dev/null; }

start_tunnel() {
  pkill -f "cloudflared tunnel --url http://localhost:$PORT" 2>/dev/null
  cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate > /tmp/cf.log 2>&1 &
  for _ in $(seq 1 30); do
    sleep 1
    u=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cf.log | head -1)
    [ -n "$u" ] && { echo "$u" > "$URL_FILE"; ok "tunnel $u"; return 0; }
  done
  bad "tunnel did not come up"; return 1
}

point_number() {
  local u; u=$(current_url)
  local res
  res=$(curl -sS -X POST "$A1_BASE/api/numbers/point" \
        -H "X-Team-Key: $A1_TEAM_KEY" -H "Content-Type: application/json" \
        -d "{\"webhook_url\":\"$u/ws\"}" 2>&1)
  case "$res" in
    *pointed_to*) ok "number pointed at $u/ws" ;;
    # a1mobile creates a Telnyx TeXML application and never updates one, so
    # re-pointing an already-pointed number 422s. Harmless if the URL is the
    # same as before; fatal only if the tunnel changed.
    *) bad "point failed: ${res:0:120}" ;;
  esac
}

line_is_up() {
  local u; u=$(current_url)
  [ -n "$u" ] || return 1
  [ "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$u/ws" 2>/dev/null)" = "200" ]
}

if [ "${1:-}" = "status" ]; then
  pgrep -f "bot.py" >/dev/null && ok "bot running" || bad "bot down"
  pgrep -f cloudflared >/dev/null && ok "tunnel running" || bad "tunnel down"
  line_is_up && ok "line UP — $(current_url)" || bad "line DOWN — calls will fail"
  n=$(curl -sS -X POST "$A1_BASE/api/numbers/claim" -H "X-Team-Key: $A1_TEAM_KEY" 2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('phone_number',''))" 2>/dev/null)
  [ "$n" = "$A1_PHONE_NUMBER" ] && ok "number $n" || bad "NUMBER CHANGED: $n (env says $A1_PHONE_NUMBER)"
  exit 0
fi

say "starting"
[ -n "$(current_url)" ] && pgrep -f cloudflared >/dev/null || start_tunnel
export TUNNEL_HOST="$(current_url | sed 's|https://||')"
point_number

trap 'say "stopping"; pkill -P $$; exit 0' INT TERM

while true; do
  say "bot up — logging to $LOG"
  (cd server && uv run bot.py -t telnyx --proxy "$TUNNEL_HOST" 2>&1 | tee -a "$LOG")
  bad "bot exited — restarting in 3s (see $LOG)"
  sleep 3
  # A dead tunnel means a new URL, which means the number needs re-pointing.
  if ! pgrep -f cloudflared >/dev/null; then
    old=$(current_url)
    start_tunnel && [ "$(current_url)" != "$old" ] && {
      export TUNNEL_HOST="$(current_url | sed 's|https://||')"
      point_number
    }
  fi
done
