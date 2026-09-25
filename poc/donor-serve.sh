#!/bin/bash
# GENGHIS donor service — keeps THIS node's ggml-rpc-server alive, reboot-proof and self-restarting.
# The compute-donor mirror of the coordinator's serve.sh. A donor started by hand (tmux) dies on reboot;
# this + a @reboot cron makes the donor role survive reboots like everything else should.
#
# Install (once, on the donor, after building with donor-setup-linux.sh / donor-setup-cuda.sh):
#   chmod +x donor-serve.sh
#   ( crontab -l 2>/dev/null | grep -v donor-serve.sh; \
#     echo "@reboot $HOME/genghis/donor-serve.sh >> $HOME/genghis/rpc.log 2>&1" ) | crontab -
#   nohup ~/genghis/donor-serve.sh >> ~/genghis/rpc.log 2>&1 &   # start now, without waiting for a reboot
#
# Env overrides (optional):
#   GENGHIS_RPC_PORT   port to serve on            (default 50052)
#   GENGHIS_RPC_BIN    explicit path to the binary (default: auto-find under common build dirs)
#   GENGHIS_RPC_LOG    log file                     (default ~/genghis/rpc.log)

PORT="${GENGHIS_RPC_PORT:-50052}"
LOG="${GENGHIS_RPC_LOG:-$HOME/genghis/rpc.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null

# Locate the RPC server binary (env override wins; else search the usual build locations).
BIN="${GENGHIS_RPC_BIN:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$BIN" ]; then
  # the installer's Vulkan/CUDA builds live NEXT TO THIS SCRIPT (poc/llama.cpp/build-*); the older
  # donor-setup-*.sh workspace is $HOME/genghis/llama.cpp. GPU builds first, so a box with both serves its GPU.
  for d in "$HERE/llama.cpp/build-cuda" "$HERE/llama.cpp/build-vulkan" "$HERE/llama.cpp" \
           "$HOME/genghis/llama.cpp/build-cuda" "$HOME/genghis/llama.cpp/build-vulkan" "$HOME/genghis/llama.cpp" \
           "$HOME/llama.cpp" "./llama.cpp" "$HOME/genghis"; do
    BIN=$(find "$d" -name ggml-rpc-server -type f 2>/dev/null | head -1)
    [ -n "$BIN" ] && break
  done
fi
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
  echo "[$(date '+%F %T')] ggml-rpc-server not found — run install/install-linux.sh --role donor (or donor-setup-*.sh), or set GENGHIS_RPC_BIN" | tee -a "$LOG"
  exit 1
fi

# The device: ggml-rpc-server serves the CPU backend unless told otherwise (STATUS lesson #6). Infer it from the
# build dir (build-cuda -> CUDA0, build-vulkan -> Vulkan0); GENGHIS_RPC_DEVICE overrides (e.g. CUDA1, CPU).
DEV="${GENGHIS_RPC_DEVICE:-}"
if [ -z "$DEV" ]; then
  case "$BIN" in *build-cuda*) DEV=CUDA0;; *build-vulkan*) DEV=Vulkan0;; *) DEV=CPU;; esac
fi
DEVARGS=(); [ "$DEV" != CPU ] && DEVARGS=(--device "$DEV")

# ONE loop per port. The installer, INSTALL.md's manual steps and an @reboot cron can all start this script; a second
# copy used to fail to bind every 5 s forever while the first kept the port (a fresh-box test, 2026-09-23). Now the
# second copy says so and leaves. (No flock on the box: carry on without the lock rather than refuse to serve.)
if command -v flock >/dev/null 2>&1; then
  exec 9>"/tmp/genghis-donor-serve-$PORT.lock"
  if ! flock -n 9; then
    echo "[$(date '+%F %T')] donor-serve: already running for port $PORT -- nothing to do" | tee -a "$LOG"
    exit 0
  fi
fi

port_taken(){ (: > "/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1; }

echo "[$(date '+%F %T')] donor-serve: $BIN  -H 0.0.0.0 -p $PORT -c ${DEVARGS[*]}  (device=$DEV)" >> "$LOG"
# -c enables the RPC server's local cache. Restart loop: survives crashes; @reboot cron survives reboots.
warned=0
while true; do
  # Something ELSE already serving this port (e.g. an rpc-server someone started by hand, in tmux): don't fail to
  # bind and respawn every 5 s -- say so once a minute and wait for the port to free up.
  if port_taken; then
    if [ $((warned % 2)) -eq 0 ]; then
      echo "[$(date '+%F %T')] donor-serve: port $PORT is already served by another process; waiting (stop it and this loop takes over: pgrep -af ggml-rpc-server)" >> "$LOG"
    fi
    warned=$((warned + 1)); sleep 30; continue
  fi
  warned=0
  "$BIN" -H 0.0.0.0 -p "$PORT" -c "${DEVARGS[@]}" >> "$LOG" 2>&1
  echo "[$(date '+%F %T')] rpc-server exited — restarting in 5s" >> "$LOG"
  sleep 5
done
