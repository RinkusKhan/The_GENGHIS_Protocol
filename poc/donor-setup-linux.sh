#!/usr/bin/env bash
# GENGHIS POC — donor setup for Linux donors (Raspberry Pi OS 64-bit, or x86 Linux server).
# Builds llama.cpp with the RPC backend and starts rpc-server bound to the LAN.
#
# Run ON EACH DONOR:
#   chmod +x donor-setup-linux.sh
#   ./donor-setup-linux.sh
#
# Notes:
#  - Pi 3 B and any 32-bit OS: this still works but is slow; use a tiny model.
#  - Requires the device be on the same LAN/subnet as the client laptop.
set -euo pipefail

RPC_PORT="${RPC_PORT:-50052}"
JOBS="$(nproc)"
WORKDIR="${WORKDIR:-$HOME/genghis}"

echo "==> GENGHIS donor setup on $(hostname) ($(uname -m)), ${JOBS} cores"

# 1. Toolchain
# Only what is MISSING, and say what and why: a fresh-box test found this installing 39 packages with sudo, unannounced,
# two of them never used (libcurl dev: the build sets -DLLAMA_CURL=OFF; pip: only the old-cmake fallback below needs
# it, and installs it there). The installer's preflight offers the same packages first, so normally this is a no-op.
if command -v apt-get >/dev/null 2>&1; then
  NEED=""
  for p in build-essential cmake git; do dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"; done
  if [ -n "$NEED" ]; then
    echo "==> installing the build tools this donor needs:$NEED  (sudo apt-get install)"
    sudo apt-get update
    sudo apt-get install -y $NEED
  else
    echo "==> build tools present (build-essential, cmake, git) -- nothing to install"
  fi
fi

# 1b. cmake version guard — llama.cpp needs cmake >= 3.14. Old distros (e.g. JetPack/Ubuntu 18.04
#     on the Tegra X1) ship 3.10, which fails at configure. Self-heal via pip's prebuilt cmake.
CMAKE_VER="$(cmake --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)"
CMAKE_MAJ="${CMAKE_VER%%.*}"; CMAKE_MIN="${CMAKE_VER##*.}"
if [ -z "$CMAKE_VER" ] || [ "$CMAKE_MAJ" -lt 3 ] || { [ "$CMAKE_MAJ" -eq 3 ] && [ "$CMAKE_MIN" -lt 14 ]; }; then
  echo "==> cmake ${CMAKE_VER:-none} is too old (need >= 3.14). Installing a newer cmake via pip…"
  sudo pip3 install --upgrade "cmake>=3.22" || pip3 install --user --upgrade "cmake>=3.22"
  hash -r
  # pip user installs land in ~/.local/bin — make sure it's on PATH for this run.
  export PATH="$HOME/.local/bin:$PATH"
  echo "==> cmake now: $(cmake --version | head -1)"
fi

# 1c. gcc version guard (aarch64) — old GCC lacks the ARM NEON _x4 load intrinsics
#     (vld1q_s8_x4 / vld1q_u8_x4) that ggml-cpu needs. EMPIRICAL: on JetPack/Ubuntu 18.04
#     (Tegra X1), even gcc-8.4's arm_neon.h is MISSING them — gcc-10 is the first that works.
#     So on aarch64 with gcc < 10 we pull gcc-10 from the toolchain PPA and build with it.
GENGHIS_CC=""; GENGHIS_CXX=""
GCC_MAJ="$(gcc -dumpversion 2>/dev/null | cut -d. -f1)"
if [ "$(uname -m)" = "aarch64" ] && [ -n "$GCC_MAJ" ] && [ "$GCC_MAJ" -lt 10 ]; then
  echo "==> gcc ${GCC_MAJ}.x lacks the ARM NEON _x4 intrinsics (need >= 10). Installing gcc-10…"
  sudo apt-get install -y software-properties-common || true
  sudo add-apt-repository -y ppa:ubuntu-toolchain-r/test || true
  sudo apt-get update
  sudo apt-get install -y gcc-10 g++-10
  GENGHIS_CC=gcc-10; GENGHIS_CXX=g++-10
  echo "==> will build with CC=$GENGHIS_CC CXX=$GENGHIS_CXX"
fi

# 2. Source
mkdir -p "$WORKDIR" && cd "$WORKDIR"
# Pin the SAME commit the client was built from — RPC has no cross-version compatibility.
PIN_COMMIT="${PIN_COMMIT:-eab8ee41f889ef7823af517e8098fb8a9b3cf601}"
if [ ! -d llama.cpp ]; then
  git clone https://github.com/ggml-org/llama.cpp.git
fi
cd llama.cpp
git fetch --depth 1 origin "${PIN_COMMIT}" && git checkout "${PIN_COMMIT}"

# 3. Build with RPC backend
# NOTE: the RPC server target is named 'ggml-rpc-server' (produces the 'ggml-rpc-server' binary).
CC_ARG=""; CXX_ARG=""
if [ -n "$GENGHIS_CC" ]; then CC_ARG="-DCMAKE_C_COMPILER=$GENGHIS_CC"; CXX_ARG="-DCMAKE_CXX_COMPILER=$GENGHIS_CXX"; fi
cmake -S . -B build-rpc $CC_ARG $CXX_ARG -DGGML_RPC=ON -DLLAMA_CURL=OFF -DGGML_NATIVE=ON
cmake --build build-rpc --config Release --target ggml-rpc-server -j "${JOBS}"

BIN="$(find build-rpc -name ggml-rpc-server -type f | head -1)"
echo "==> Built: $BIN"

# 4. Report identity — ALSO saved to a file so it survives an SSH disconnect.
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
MEM_MB="$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)"
INFO="$WORKDIR/donor-info.txt"
{
  echo "======================================================"
  echo " DONOR READY"
  echo "   host   : $(hostname)"
  echo "   ip     : ${IP}"
  echo "   arch   : $(uname -m)"
  echo "   cores  : ${JOBS}"
  echo "   ram_MB : ${MEM_MB}"
  echo "   port   : ${RPC_PORT}"
  echo "   -> add to client host list as:  ${IP}:${RPC_PORT}"
  echo "======================================================"
} | tee "$INFO"
echo "==> (also saved to ${INFO} — cat it any time)"

# 5. Serve — DETACHED so closing your SSH session does NOT kill the donor.
#    -H 0.0.0.0 exposes on the LAN; -c enables the on-disk tensor cache.
# ONE launcher serves ggml-rpc-server: donor-serve.sh (one instance per port, restarts on crash, the same thing the
# @reboot cron runs). This script used to start its own copy in tmux as well, and a fresh-box test found the two
# fighting over the port, the one actually serving unsupervised (2026-09-23). GENGHIS_NO_START=1 (the installer sets it)
# means: build only; the caller starts donor-serve.sh.
SERVE_SH=""
for s in "$(cd "$(dirname "$0")" && pwd)/donor-serve.sh" "${WORKDIR}/donor-serve.sh" "$HOME/genghis-src/poc/donor-serve.sh"; do
  [ -f "$s" ] && { SERVE_SH="$s"; break; }
done
if [ "${GENGHIS_NO_START:-0}" = 1 ]; then
  echo "==> Built. Not starting a server here: the installer starts donor-serve.sh."
elif [ -n "$SERVE_SH" ]; then
  GENGHIS_RPC_BIN="$BIN" GENGHIS_RPC_PORT="$RPC_PORT" setsid nohup bash "$SERVE_SH" >/dev/null 2>&1 < /dev/null &
  sleep 3
  echo "==> Serving on :${RPC_PORT} via $SERVE_SH (restarts on crash; log: $HOME/genghis/rpc.log)"
  echo "    survive reboots: ( crontab -l 2>/dev/null | grep -v donor-serve.sh; echo \"@reboot GENGHIS_RPC_BIN=$BIN bash $SERVE_SH >/dev/null 2>&1\" ) | crontab -"
  echo "    check: pgrep -af ggml-rpc-server"
else
  echo "==> donor-serve.sh not found next to this script -- starting the server directly (it will NOT restart on a crash)"
  nohup "$BIN" -H 0.0.0.0 -p "${RPC_PORT}" -c > "${WORKDIR}/rpc.log" 2>&1 &
fi
