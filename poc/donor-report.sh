#!/bin/bash
# GENGHIS donor capacity reporter — the compute-node mirror of the TV's self-report.
# Pushes LIVE free memory to the coordinator's /report so the fabric reflects reality at
# plan time (measure, don't assume) instead of a one-time hardcoded ram_free_mb in fleet.json.
#
# Usage:  ./donor-report.sh <node-id>            (e.g. ./donor-report.sh pi5-8gb)
#   env:  GENGHIS_COORD=host:port  (required; the installer puts it in the cron line — or it is discovered via mDNS)
#         REPORT_EVERY=<seconds>   (default 30)
#
# Run detached, survives reboots (mirror of serve.sh):
#   tmux new-session -d -s ghreport "~/genghis/donor-report.sh <id>"
#   (crontab -l 2>/dev/null; echo "@reboot ~/genghis/donor-report.sh <id> >> ~/genghis/report.log 2>&1") | crontab -

ID="${1:?usage: donor-report.sh <node-id>}"
COORD="${GENGHIS_COORD:-}"
if [ -z "$COORD" ]; then
  # no address given: ask the LAN (mDNS) once, via the coordinator's own discovery helper if it sits next to us
  HERE="$(cd "$(dirname "$0")" && pwd)"
  if [ -f "$HERE/genghis_mdns.py" ] && command -v python3 >/dev/null 2>&1; then
    COORD=$(python3 "$HERE/genghis_mdns.py" 2>/dev/null | sed -n 's#^http://\([^/]*\)/fleet.json$##p' | head -1)
  fi
  [ -n "$COORD" ] || { echo "donor-report: set GENGHIS_COORD=host:port (no coordinator found on the LAN)"; exit 2; }
fi
EVERY="${REPORT_EVERY:-30}"

while true; do
  # MemAvailable = the honest "free for new work" figure (accounts for reclaimable cache); kB -> MB.
  free_mb=$(awk '/MemAvailable/{printf "%d",$2/1024}' /proc/meminfo)
  total_mb=$(awk '/MemTotal/{printf "%d",$2/1024}' /proc/meminfo)
  payload="{\"id\":\"$ID\",\"ram_free_mb\":$free_mb,\"ram_total_mb\":$total_mb"

  # If this donor has an NVIDIA GPU, also report live VRAM (free_mem_mb uses vram_total_mb for cuda).
  if command -v nvidia-smi >/dev/null 2>&1; then
    vram_total=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
    [ -n "$vram_total" ] && payload="$payload,\"vram_total_mb\":$vram_total"
  fi
  payload="$payload}"

  curl -s --max-time 5 -X POST "http://$COORD/report" \
       -H "Content-Type: application/json" -d "$payload" >/dev/null 2>&1
  sleep "$EVERY"
done
