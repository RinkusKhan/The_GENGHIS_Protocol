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

  # VRAM describes ONE card, so report it only for the NVIDIA device THIS node lends: GENGHIS_RPC_DEVICE=CUDAn picks
  # it (unset = card 0, the single-card box); a Vulkan or CPU device reports none. Asking nvidia-smi regardless is how
  # the NUC's Arc node was credited with its eGPU's 16 GB. (The authority also refuses a VRAM total for a non-CUDA
  # node, and trusts the device's own answer over any report -- D51 -- so an old copy of this script is harmless.)
  case "${GENGHIS_RPC_DEVICE:-}" in
    CUDA*) gpu_idx="${GENGHIS_RPC_DEVICE#CUDA}";;
    "")    gpu_idx=0;;
    *)     gpu_idx="";;
  esac
  if [ -n "$gpu_idx" ] && command -v nvidia-smi >/dev/null 2>&1; then
    vram_total=$(nvidia-smi -i "$gpu_idx" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
    [ -n "$vram_total" ] && payload="$payload,\"vram_total_mb\":$vram_total"
  fi
  payload="$payload}"

  # Say when the authority REJECTS the report (HTTP 400 = it knows no node called "$ID"): the output used to go to
  # /dev/null, so a box reporting under the wrong name looked fine forever while every report was thrown away
  # (a fresh-box test, 2026-09-23). Logged when the state CHANGES, not every 30 s.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "http://$COORD/report" \
       -H "Content-Type: application/json" -d "$payload" 2>/dev/null)
  if [ "$code" != "${last_code:-}" ]; then
    case "$code" in
      200) echo "[$(date '+%F %T')] donor-report: reporting as '$ID' to $COORD";;
      400) echo "[$(date '+%F %T')] donor-report: the authority at $COORD knows no node '$ID' -- reports are rejected. Run 'genghis_coordinator.py verify' on this box: it shows the name the fleet uses, and restart this reporter with it.";;
      *)   echo "[$(date '+%F %T')] donor-report: could not reach $COORD (HTTP ${code:-none}) -- retrying every ${EVERY}s";;
    esac
    last_code="$code"
  fi
  sleep "$EVERY"
done
