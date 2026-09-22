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
# Pascal (1080 Ti) = 61. Override for other GPUs (Turing 75, Ampere 86, Ada 89).
CUDA_ARCH="${CUDA_ARCH:-61}"

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

# ---------------------------------------------------------------------------
# 2. Build toolchain + CUDA toolkit (nvcc)
# ---------------------------------------------------------------------------
sudo apt-get update
sudo apt-get install -y build-essential cmake git libcurl4-openssl-dev
if ! command -v nvcc >/dev/null 2>&1; then
  echo "==> Installing CUDA toolkit (distro package)..."
  # Distro package is the most robust 'just works' path on Ubuntu; provides a CUDA 12.x nvcc,
  # which supports Pascal fine. (Alternative: NVIDIA's official CUDA apt repo for the newest toolkit.)
  sudo apt-get install -y nvidia-cuda-toolkit
fi
echo "==> nvcc: $(nvcc --version | grep release || echo 'not found')"

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
SESSION="genghis"
if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" "'$BIN' -d ${RPC_DEVICE} -H 0.0.0.0 -p '${RPC_PORT}' -c"
  echo "==> Serving on GPU device ${RPC_DEVICE} in tmux session '${SESSION}' (survives disconnect)."
  echo "    view logs : tmux attach -t ${SESSION}   (detach: Ctrl-b then d)"
  echo "    stop      : tmux kill-session -t ${SESSION}"
  echo "    check     : pgrep -af ggml-rpc-server ; nvidia-smi"
else
  echo "==> tmux not found; installing and running detached is recommended: sudo apt-get install -y tmux"
  nohup "$BIN" -d "${RPC_DEVICE}" -H 0.0.0.0 -p "${RPC_PORT}" -c > "${WORKDIR}/rpc.log" 2>&1 &
  echo "==> Running with nohup on GPU device ${RPC_DEVICE} (logs: ${WORKDIR}/rpc.log). stop: pkill -f ggml-rpc-server"
fi
