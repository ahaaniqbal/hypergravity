#!/usr/bin/env bash
# Keep the line up while judges may be calling.
#
# run.sh is the real supervisor, but it owns cloudflared — and cloudflare started
# returning 429 on quick-tunnel creation after a day of restarts, so the tunnel
# here is localtunnel on a PINNED subdomain. Pinned matters more than it sounds:
# a random hostname on every restart means the number ends up pointed at a tunnel
# that no longer exists, the webhook answers nothing, and the caller hears a
# carrier error while every log on this machine looks healthy.
#
# Restarting a dead bot is the easy half. The half that actually bit us is
# re-pointing, so this checks the number's routing every cycle rather than only
# after a restart.
set -uo pipefail
cd "$(dirname "$0")"

SUBDOMAIN="${HG_SUBDOMAIN:-hypergravity}"
HOST="$SUBDOMAIN.loca.lt"
PORT=7860
source_env() { set -a; . ./.env; set +a; }
source_env

say() { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()  { printf "\033[32m✓\033[0m %s\n" "$*"; }
bad() { printf "\033[31m✗\033[0m %s\n" "$*"; }

bot_up()    { curl -sf -o /dev/null -m 5 -X POST "http://127.0.0.1:$PORT/ws" -d "CallSid=hc" 2>/dev/null; }
tunnel_up() { curl -sf -o /dev/null -m 10 -X POST "https://$HOST/ws" -H "User-Agent: keepalive" -d "CallSid=hc" 2>/dev/null; }

start_tunnel() {
  pkill -f "localtunnel --port $PORT" 2>/dev/null
  sleep 1
  nohup npx -y localtunnel --port "$PORT" --subdomain "$SUBDOMAIN" > /tmp/lt.log 2>&1 &
  sleep 12
}

start_bot() {
  pkill -f "bot.py -t telnyx" 2>/dev/null
  sleep 2
  ( cd server && nohup uv run bot.py -t telnyx --proxy "$HOST" >> /tmp/hypergravity.log 2>&1 & )
  sleep 20
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

say "keepalive on https://$HOST — ctrl-c to stop"
bot_up    || { say "bot down, starting";    start_bot; }
tunnel_up || { say "tunnel down, starting"; start_tunnel; }
point_number

while true; do
  sleep 30
  if ! bot_up; then
    bad "bot stopped answering — restarting"
    start_bot
    point_number
    continue
  fi
  if ! tunnel_up; then
    bad "tunnel stopped answering — restarting"
    start_tunnel
    # The subdomain is pinned, so the URL is unchanged — but re-point anyway.
    # Being pointed at the right place is cheap; discovering you weren't costs a
    # judge's phone call, and there is no second impression.
    point_number
  fi
done
