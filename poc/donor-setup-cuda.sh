#!/usr/bin/env bash
# GENGHIS POC — CUDA donor setup for an Ubuntu Server box with an NVIDIA GPU.
# Target proven on: GTX 1080 Ti (Pascal, compute capability 6.1), Ubuntu 24.04 LTS Server.
# Builds llama.cpp with the CUDA + RPC backends and serves ggml-rpc-server on the LAN.
#
# Run ON THE GPU DONOR:
#   chmod +x donor-setup-cuda.sh
#   ./donor-setup-cuda.sh
#
# If the NVIDIA driver / CUDA toolkit isn't installed yet, the script installs them.
# A driver install may require a REBOOT — if so, the script tells you and exits; re-run after reboot.
set -euo pipefail

RPC_PORT="${RPC_PORT:-50052}"
WORKDIR="${WORKDIR:-$HOME/genghis}"
# Pin the SAME commit the client was built from — RPC has no cross-version compatibility.
PIN_COMMIT="${PIN_COMMIT:-eab8ee41f889ef7823af517e8098fb8a9b3cf601}"
# The card's architecture: read from the driver below (12.0 -> 120) unless set here. Blackwell 120, Ada 89,
# Ampere 86, Turing 75, Pascal 61. (It used to DEFAULT to 61 -- the first donor's GTX 1080 Ti -- which built a
# Pascal-only binary for every card, and CUDA 13 cannot compile for Pascal at all.)
CUDA_ARCH="${CUDA_ARCH:-}"

echo "==> GENGHIS CUDA donor setup on $(hostname) ($(uname -m))"

# ---------------------------------------------------------------------------
# 1. NVIDIA driver
# ---------------------------------------------------------------------------
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
  echo "==> NVIDIA driver not active. Installing recommended driver..."
  sudo apt-get update
  sudo apt-get install -y ubuntu-drivers-common
  # 'autoinstall' was renamed to 'install' in newer ubuntu-drivers (Ubuntu 25.10/26.04+).
  # Try the new command first, fall back to the old one for older releases.
  sudo ubuntu-drivers install || sudo ubuntu-drivers autoinstall
  echo "======================================================================"
  echo " NVIDIA driver installed. A REBOOT is required before the GPU is usable."
  echo "   sudo reboot   # then re-run this script"
  echo "======================================================================"
  exit 0
fi
echo "==> GPU detected:"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
if [ -z "$CUDA_ARCH" ]; then
  CUDA_ARCH="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' .')"
  if [ -z "$CUDA_ARCH" ]; then
    echo "!! Could not read the GPU's compute capability from the driver. Set it and re-run, e.g.:"
    echo "   CUDA_ARCH=120 $0      # Blackwell 120, Ada 89, Ampere 86, Turing 75, Pascal 61"
    exit 1
  fi
  echo "==> GPU architecture (from the driver): CUDA_ARCH=${CUDA_ARCH}"
fi

# ---------------------------------------------------------------------------
# 2. Build toolchain + CUDA toolkit (nvcc)
# ---------------------------------------------------------------------------
# Build tools: only what is MISSING, announced (the installer's preflight offers these first; normally a no-op here).
NEED=""
for p in build-essential cmake git; do dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"; done
if [ -n "$NEED" ]; then
  echo "==> installing the build tools this donor needs:$NEED  (sudo apt-get install)"
  sudo apt-get update && sudo apt-get install -y $NEED
fi

# Which cards (D52): NVIDIA RTX 20 / GTX 16-series (Turing, compute 7.5) and newer -- everything CUDA 13 still builds
# for, so ONE toolkit rule covers them all. A GTX 10-series (Pascal) card is EXPERIMENTAL: CUDA 13 dropped it, it needs
# CUDA 12 and a driver <= 570 (D4), and that path is not proven here yet. Refused with the reason;
# GENGHIS_ALLOW_OLD_GPU=1 builds anyway, untested.
if [ "${CUDA_ARCH:-0}" -lt 75 ] && [ "${GENGHIS_ALLOW_OLD_GPU:-0}" != 1 ]; then
  echo "======================================================================"
  echo " !!! $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1) is compute ${CUDA_ARCH}: older than GENGHIS supports"
  echo "     (RTX 20 / GTX 16-series, Turing, and newer: D52). A GTX 10-series card is experimental: it needs"
  echo "     CUDA 12 and a driver of 570 or older (D4). Re-run with GENGHIS_ALLOW_OLD_GPU=1 to try anyway."
  echo "======================================================================"
  exit 1
fi

# The toolkit (D52): ONE rule for every supported card -- CUDA 13.2 or newer, from NVIDIA's repository. Ubuntu's own
# nvidia-cuda-toolkit is too old for an RTX 50-series card, and CUDA 13.0/13.1 fail against glibc 2.43 (Ubuntu 26.04)
# on rsqrt/rsqrtf. 13.2 builds clean on 26.04 with the stock compiler (D46). NVIDIA installs nvcc under
# /usr/local/cuda/bin, which is not on PATH by default.
[ -x /usr/local/cuda/bin/nvcc ] && export PATH="/usr/local/cuda/bin:$PATH"
NVCC_VER="$(nvcc --version 2>/dev/null | sed -n 's/.*release \([0-9]*\.[0-9]*\).*/\1/p' | head -1)"
ver_ge(){ [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }   # ver_ge 13.2 13.2 -> true
NEED_CUDA=13.2; [ "${CUDA_ARCH:-0}" -lt 75 ] && NEED_CUDA=12.0   # an opted-in Pascal card: CUDA 13 cannot build for it
if [ -z "$NVCC_VER" ] || ! ver_ge "$NVCC_VER" "$NEED_CUDA" || { [ "$NEED_CUDA" = 12.0 ] && ver_ge "$NVCC_VER" 13.0; }; then
  REPO="ubuntu2404/$( [ "$(uname -m)" = aarch64 ] && echo sbsa || echo x86_64 )"
  echo "======================================================================"
  echo " CUDA toolkit: ${NVCC_VER:-not installed} -- this donor needs CUDA 13.2 or newer from NVIDIA (D52)."
  echo " Install it (the person runs these; they add NVIDIA's apt repository), then re-run this script:"
  echo "   wget https://developer.download.nvidia.com/compute/cuda/repos/${REPO}/cuda-keyring_1.1-1_all.deb"
  echo "   sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt-get update && sudo apt-get install -y cuda-toolkit-13-2"
  echo " (Ubuntu 24.04 and 26.04 both use NVIDIA's ubuntu2404 repository. NVIDIA's own page is authoritative:"
  echo "  https://developer.nvidia.com/cuda-downloads . Do NOT use Ubuntu's nvidia-cuda-toolkit package.)"
  echo "======================================================================"
  exit 1
fi
echo "==> nvcc: CUDA ${NVCC_VER} ($(command -v nvcc))"

# ---------------------------------------------------------------------------
# 3. Source (pinned)
# ---------------------------------------------------------------------------
mkdir -p "$WORKDIR" && cd "$WORKDIR"
if [ ! -d llama.cpp ]; then
  git clone https://github.com/ggml-org/llama.cpp.git
fi
cd llama.cpp
git fetch --depth 1 origin "${PIN_COMMIT}" && git checkout "${PIN_COMMIT}"

# ---------------------------------------------------------------------------
# 4. Build with CUDA + RPC
# ---------------------------------------------------------------------------
echo "==> Building llama.cpp (CUDA arch ${CUDA_ARCH} + RPC)..."
cmake -S . -B build-cuda \
  -DGGML_CUDA=ON \
  -DGGML_RPC=ON \
  -DLLAMA_CURL=OFF \
  -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build-cuda --config Release --target ggml-rpc-server -j "$(nproc)"

BIN="$(find build-cuda -name ggml-rpc-server -type f | head -1)"
echo "==> Built: $BIN"

# ---------------------------------------------------------------------------
# 4b. CUDA PREFLIGHT — verify CUDA actually initializes BEFORE serving.
#     Without this the server silently falls back to the CPU backend (a GPU
#     donor on CPU is pointless). Root cause we hit once: a driver that dropped
#     support for the GPU's arch (NVIDIA driver 580 dropped Pascal / GTX 10xx),
#     where nvidia-smi shows the card but cudaGetDeviceCount returns 802.
# ---------------------------------------------------------------------------
cat > /tmp/genghis_cudacheck.cu <<'EOF'
#include <cstdio>
#include <cuda_runtime.h>
int main(){int n=0; cudaError_t e=cudaGetDeviceCount(&n);
  if(e!=cudaSuccess){fprintf(stderr,"cudaGetDeviceCount: %s\n",cudaGetErrorString(e)); return 1;}
  if(n<1){fprintf(stderr,"no CUDA devices\n"); return 1;} return 0;}
EOF
if nvcc /tmp/genghis_cudacheck.cu -o /tmp/genghis_cudacheck 2>/dev/null && /tmp/genghis_cudacheck; then
  echo "==> CUDA preflight OK — GPU is usable for compute."
else
  echo "======================================================================"
  echo " !!! CUDA PREFLIGHT FAILED — the GPU is NOT usable for CUDA here."
  echo "     cudaGetDeviceCount found no device / failed to initialize."
  echo "     Most common cause: the installed driver dropped support for this"
  echo "     GPU's architecture. PASCAL (GTX 10-series, e.g. 1080 Ti) needs a"
  echo "     <= 570 driver — use Ubuntu 24.04 LTS with nvidia-driver-535/550."
  echo "     (Ubuntu 25.10/26.04 ship only driver 580, which cannot do Pascal CUDA.)"
  echo "     Refusing to serve on CPU-only. Fix the driver/OS, then re-run."
  echo "======================================================================"
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. Report identity (first fleet-registry entry for a GPU donor)
# ---------------------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)"
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
CORES="$(nproc)"
MEM_MB="$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)"
INFO="$WORKDIR/donor-info.txt"
{
  echo "======================================================"
  echo " GPU DONOR READY"
  echo "   host        : $(hostname)"
  echo "   ip          : ${IP}"
  echo "   arch        : $(uname -m)"
  echo "   accelerator : cuda"
  echo "   gpu         : ${GPU_NAME}"
  echo "   vram_MB     : ${VRAM_MB}"
  echo "   cores       : ${CORES}"
  echo "   sys_ram_MB  : ${MEM_MB}"
  echo "   port        : ${RPC_PORT}"
  echo "   -> add to client host list as:  ${IP}:${RPC_PORT}   (device will appear as RPCn)"
  echo "======================================================"
} | tee "$INFO"
echo "==> (also saved to ${INFO} — cat it any time)"

# ---------------------------------------------------------------------------
# 6. Serve — DETACHED so closing your SSH session does NOT kill the donor.
#    -H 0.0.0.0 exposes on the LAN; -c enables the local tensor cache.
# ---------------------------------------------------------------------------
# CRITICAL: ggml-rpc-server does NOT auto-select the GPU — it serves the CPU backend unless you
# name the CUDA device with '-d CUDA0'. Without this the donor advertises system RAM and computes
# on the CPU (catastrophically slow on a GPU box). Override RPC_DEVICE for multi-GPU (CUDA1, ...).
RPC_DEVICE="${RPC_DEVICE:-CUDA0}"
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
  GENGHIS_RPC_BIN="$BIN" GENGHIS_RPC_PORT="$RPC_PORT" GENGHIS_RPC_DEVICE="$RPC_DEVICE" setsid nohup bash "$SERVE_SH" >/dev/null 2>&1 < /dev/null &
  sleep 3
  echo "==> Serving on GPU device ${RPC_DEVICE} on :${RPC_PORT} via $SERVE_SH (restarts on crash; log: $HOME/genghis/rpc.log)"
  echo "    survive reboots: ( crontab -l 2>/dev/null | grep -v donor-serve.sh; echo \"@reboot GENGHIS_RPC_BIN=$BIN GENGHIS_RPC_DEVICE="$RPC_DEVICE" bash $SERVE_SH >/dev/null 2>&1\" ) | crontab -"
  echo "    check: pgrep -af ggml-rpc-server"
else
  echo "==> donor-serve.sh not found next to this script -- starting the server directly (it will NOT restart on a crash)"
  nohup "$BIN" -d "${RPC_DEVICE}" -H 0.0.0.0 -p "${RPC_PORT}" -c > "${WORKDIR}/rpc.log" 2>&1 &
fi
