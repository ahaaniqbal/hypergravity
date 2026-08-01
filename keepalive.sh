#!/usr/bin/env bash
# Keep the line answerable while judges may be calling.
#
# Tunnel history, because the choice is not arbitrary: cloudflared is the good
# option and run.sh uses it, but cloudflare began returning 429 on quick-tunnel
# creation after a day of restarts. localtunnel was the fallback and measured
# 3 requests in 10 — a judge would have failed seven calls out of ten and
# concluded the project was broken. localhost.run measured 10 in 10, twice.
#
# The hostname is not stable across SSH sessions, so this does not just restart
# things: when the tunnel comes back with a different name it restarts the bot on
# the new one and re-points the number. That sequence — new URL, stale routing —
# is the exact failure that ate calls all day, and it is silent from the outside.
# The line rings and the caller hears a carrier error while every log here looks
# perfectly healthy.
set -uo pipefail
cd "$(dirname "$0")"

PORT=7860
SSH_LOG=/tmp/lhr.log
set -a; . ./.env; set +a

say() { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()  { printf "\033[32m✓\033[0m %s\n" "$*"; }
bad() { printf "\033[31m✗\033[0m %s\n" "$*"; }

HOST=""

bot_up() { curl -sf -o /dev/null -m 6 -X POST "http://127.0.0.1:$PORT/ws" -d "CallSid=hc" 2>/dev/null; }

tunnel_url() { grep -oE 'https://[a-z0-9-]+\.lhr\.life' "$SSH_LOG" 2>/dev/null | tail -1; }

tunnel_up() {
  [ -n "$HOST" ] || return 1
  curl -sf -o /dev/null -m 12 -X POST "https://$HOST/ws" -H "User-Agent: keepalive" -d "CallSid=hc" 2>/dev/null
}

start_tunnel() {
  pkill -f "ssh.*localhost.run" 2>/dev/null
  sleep 1
  : > "$SSH_LOG"
  nohup ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 \
            -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
            -R 80:localhost:$PORT nokey@localhost.run >> "$SSH_LOG" 2>&1 &
  for _ in $(seq 1 15); do
    sleep 2
    local u; u=$(tunnel_url)
    if [ -n "$u" ]; then HOST="${u#https://}"; ok "tunnel up on $HOST"; return 0; fi
  done
  bad "tunnel did not come up"
  return 1
}

start_bot() {
  pkill -f "bot.py -t telnyx" 2>/dev/null
  sleep 2
  ( cd server && nohup uv run bot.py -t telnyx --proxy "$HOST" >> /tmp/hypergravity.log 2>&1 & )
  for _ in $(seq 1 20); do sleep 2; bot_up && { ok "bot up"; return 0; }; done
  bad "bot did not come up"
  return 1
}

point_number() {
  local res
  res=$(curl -sS -m 15 -X POST "$A1_BASE/api/numbers/point" \
    -H "X-Team-Key: $A1_TEAM_KEY" -H "Content-Type: application/json" \
    -d "{\"webhook_url\":\"https://$HOST/ws\"}" 2>&1)
  case "$res" in
    *pointed_to*) ok "number pointed at https://$HOST/ws" ;;
    *) bad "point failed: ${res:0:120}" ;;
  esac
}

# The bot bakes its hostname in at startup (it serves it inside the TeXML), so a
# changed tunnel means the bot is now advertising a URL that no longer resolves.
# Restart it before re-pointing, or the number is correct and the audio socket
# still goes nowhere.
adopt_host() {
  local previous="$1"
  if [ "$HOST" != "$previous" ]; then
    say "tunnel hostname changed: ${previous:-none} → $HOST"
    start_bot
  fi
  point_number
}

say "keepalive starting"
HOST=$(tunnel_url); HOST="${HOST#https://}"
tunnel_up || start_tunnel
bot_up    || start_bot
point_number
say "watching — ctrl-c to stop"

while true; do
  sleep 30
  if ! tunnel_up; then
    bad "tunnel stopped answering — restarting"
    prev="$HOST"
    start_tunnel && adopt_host "$prev"
    continue
  fi
  if ! bot_up; then
    bad "bot stopped answering — restarting"
    start_bot && point_number
  fi
done
