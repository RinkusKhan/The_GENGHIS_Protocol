#!/usr/bin/env bash
# GENGHIS -- Linux `serve` launcher (the authority, or an inference host of one; D30).
# Keeps `genghis_coordinator.py serve` alive across CRASHES (restart loop) and REBOOTS (an @reboot cron
# line, installed by install/install-linux.sh). Path-agnostic: runs the checkout it lives in.
#
#   authority :  ~/genghis-src/poc/serve.sh                       (FLEET_URL = this box)
#   host      :  GENGHIS_COORD=<authority-ip> ~/genghis-src/poc/serve.sh   (reads the fleet from the authority)
#
# env (all optional): GENGHIS_COORD  GENGHIS_SERVE_PORT (8899)  GENGHIS_LLAMA_CLI / GENGHIS_LLAMA_SERVER (auto)
#                     GENGHIS_SELF_ID  GENGHIS_MODELS_DIR  GENGHIS_TOKEN
# Log: $HOME/genghis-serve.log  (the coordinator's own lines land there unbuffered)
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${GENGHIS_SERVE_LOG:-$HOME/genghis-serve.log}"
cd "$HERE" || exit 1
# a cron has no login environment: pick up the coordinator the installer persisted, if the env didn't set it
if [ -z "${GENGHIS_COORD:-}" ] && [ -f "$HOME/.profile" ]; then
  c=$(grep -E '^export GENGHIS_COORD=' "$HOME/.profile" | tail -1 | cut -d= -f2-); [ -n "$c" ] && export GENGHIS_COORD="$c"
fi
export GENGHIS_SERVE_PORT="${GENGHIS_SERVE_PORT:-8899}"

while true; do
  PY=$(command -v python3 || true)
  if [ -z "$PY" ]; then
    echo "[$(date '+%F %T')] python3 NOT FOUND -- the coordinator needs it: sudo apt install -y python3 . Re-checking in 30s." >> "$LOG"
    sleep 30; continue
  fi
  echo "[$(date '+%F %T')] starting: $PY -u genghis_coordinator.py serve  (GENGHIS_COORD=${GENGHIS_COORD:-<none: authority>} port=$GENGHIS_SERVE_PORT)" >> "$LOG"
  "$PY" -u genghis_coordinator.py serve >> "$LOG" 2>&1
  echo "[$(date '+%F %T')] serve exited (code $?) -- restarting in 5s" >> "$LOG"
  sleep 5
done
