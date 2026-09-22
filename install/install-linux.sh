#!/usr/bin/env bash
# GENGHIS -- Linux installer (D18: preflight + guided remediation, per role). Idempotent.
# Roles: coordinator (the always-on authority + model repo + /v1), or donor (lends CPU/GPU to runs).
#
#   ./install-linux.sh --role coordinator
#   ./install-linux.sh --role donor --accel cpu            # CPU donor (Pi / x86)
#   ./install-linux.sh --role donor --accel cuda           # NVIDIA GPU donor
#   ./install-linux.sh --role donor --accel vulkan         # Arc/AMD via Vulkan
#   ./install-linux.sh --role client --coord <ip>          # a Linux box you launch models from
#   ./install-linux.sh --role donor --accel vulkan --coord <ip> --serve   # donor + inference host (/v1 + Control Room here, D30)
#   flags:  --coord HOST[:PORT]   --serve (also run serve as a host of --coord)   --yes (accept apt installs)   --preflight (detect only, no changes)
#
# It never silently changes your system: apt installs are announced and (without --yes) confirmed. The one
# thing it can't automate -- the NVIDIA driver on a CUDA donor -- is detected and you're told what to do.
set -uo pipefail

ROLE=""; ACCEL="cpu"; COORD=""; YES=0; PREFLIGHT=0; SERVE=0
while [ $# -gt 0 ]; do case "$1" in
  --role)  ROLE="$2"; shift 2;;
  --accel) ACCEL="$2"; shift 2;;
  --coord) COORD="$2"; shift 2;;
  --yes)   YES=1; shift;;
  --serve) SERVE=1; shift;;
  --preflight) PREFLIGHT=1; shift;;
  *) echo "unknown arg: $1"; exit 2;;
esac; done
[ -n "$ROLE" ] || { echo "usage: install-linux.sh --role coordinator|donor|client [--accel cpu|cuda|vulkan] [--coord HOST] [--yes] [--preflight]"; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"; POC="$(cd "$HERE/../poc" && pwd)"
PIN="eab8ee41f889ef7823af517e8098fb8a9b3cf601"
ok(){   echo "  [ ok ] $*"; }
miss(){ echo "  [ -- ] $*"; }
info(){ echo "     $*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
ask(){ [ "$YES" = 1 ] && return 0; read -r -p "  $1 [y/N] " a; [ "$a" = y ] || [ "$a" = yes ]; }
apt_need(){ # apt_need <pkg> <label>
  have "$1" && { ok "$2"; return 0; }
  miss "$2 -- missing"
  if have apt-get && ask "apt install $1?"; then sudo apt-get update -qq && sudo apt-get install -y "$1"; ok "installed $2"; return 0; fi
  return 1
}

echo "== GENGHIS Linux installer -- role: $ROLE${ACCEL:+ / accel: $ACCEL} on $(hostname) ($(uname -m)) =="
echo "== 1. Preflight =="
have python3 && ok "python3 ($(python3 --version 2>&1))" || apt_need python3 "python3 (the coordinator IS a Python program)"
apt_need git "git" || true
if [ "$ROLE" = donor ]; then
  if have gcc && have g++; then ok "gcc/g++ ($(gcc -dumpversion 2>/dev/null))"; else apt_need build-essential "build-essential (gcc/g++)" || true; fi
  apt_need cmake "cmake" || true
fi

if [ "$ROLE" = donor ] && [ "$ACCEL" = cuda ]; then
  if have nvidia-smi; then ok "NVIDIA driver ($(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1))"; else
    miss "NVIDIA driver -- not found; a CUDA donor needs it"
    info "Install the driver + CUDA for your card/OS (see DECISIONS.md D4 for the arch/driver matrix), then re-run."
  fi
fi
if [ "$ROLE" = donor ] && [ "$ACCEL" = vulkan ]; then
  have vulkaninfo && ok "Vulkan loader present" || { miss "Vulkan loader -- missing"; apt_need vulkan-tools "vulkan-tools" || true; }
  # a GGML_VULKAN build needs: the Vulkan headers, the GLSL->SPIR-V compiler, and the SPIRV-Headers cmake package
  # (Ubuntu 26.04: libvulkan-dev glslc spirv-headers spirv-tools). Missing spirv-headers = cmake "Could not find SPIRV-Headers".
  for pkg in libvulkan-dev glslc spirv-headers spirv-tools; do
    dpkg -s "$pkg" >/dev/null 2>&1 && ok "$pkg" || apt_need "$pkg" "$pkg (Vulkan build prerequisite)" || true
  done
fi

# --- GPU device access: the render/video groups (Vulkan / Arc / AMD; anything that opens /dev/dri) ---
# A fresh Ubuntu box used only over SSH hits this on its FIRST reboot: /dev/dri/renderD* is root:render 0660, and
# the user only ever reached the GPU through the desktop login's TEMPORARY ACL (logind hands the seat's devices to
# whoever is logged in at the console). The moment the box reboots and serve/donor start from cron, every model
# refuses to load ("llama-server exited (code 1)", "no devices"). The durable fix is group membership -- and it
# takes effect only at the next login, which the installer must say out loud (NUC, 2026-09-18).
DRI="${GENGHIS_DRI:-/dev/dri}"                             # overridable so the check can be exercised without a GPU
gpu_groups(){
  local devs g need=() acl_only=0 cur
  devs=$(ls "$DRI"/renderD* "$DRI"/card* 2>/dev/null) || true
  [ -n "$devs" ] || return 0                              # no /dev/dri at all: nothing to check (CPU box, or NVIDIA-only)
  cur=" $(id -nG) "
  for g in $(stat -c %G $devs 2>/dev/null | sort -u); do
    [ "$g" = root ] && continue                           # a root-owned node is a driver/udev matter, not a group one
    case "$cur" in *" $g "*) ok "GPU device group '$g' -- you are a member";; *) need+=("$g");; esac
  done
  [ ${#need[@]} -gt 0 ] || return 0
  local csv; csv=$(IFS=,; echo "${need[*]}")
  # can we open the render node RIGHT NOW anyway? then it's the desktop ACL -- the exact trap: works until reboot
  for d in "$DRI"/renderD*; do [ -r "$d" ] && [ -w "$d" ] && acl_only=1; done
  if [ "$acl_only" = 1 ]; then
    miss "GPU device group(s) ${need[*]} -- NOT a member (the GPU opens now only through your desktop login's temporary ACL)"
    info "After a reboot, serve/donor started by cron will NOT see the GPU: every model load fails with 'llama-server exited (code 1)'."
  else
    miss "GPU device group(s) ${need[*]} -- NOT a member (this user cannot open /dev/dri/renderD*: no GPU for llama.cpp)"
  fi
  if [ "$PREFLIGHT" = 1 ]; then info "Fix: sudo usermod -aG $csv $USER -- then log out and back in (or reboot)."; return 0; fi
  if ask "add $USER to $csv (sudo usermod -aG $csv $USER)?"; then
    if sudo usermod -aG "$csv" "$USER"; then
      ok "added $USER to $csv"
      echo "  [ !! ] Group membership takes effect at your NEXT LOGIN. A serve or donor started from THIS shell still"
      echo "        cannot open the GPU. Finish this installer, then log out and back in (or reboot) and confirm with:"
      echo "        id -nG    (must list: $csv)"
      NEEDS_RELOGIN=1
    else miss "usermod failed -- run it yourself: sudo usermod -aG $csv $USER"; fi
  else info "Skipped. Without it the GPU is unreachable after a reboot: sudo usermod -aG $csv $USER"; fi
}
NEEDS_RELOGIN=0
relogin_note(){ [ "$NEEDS_RELOGIN" = 1 ] && echo "  [ !! ] LOG OUT AND BACK IN (or reboot) now -- the GPU groups you were added to apply only to a new login; the @reboot cron will pick them up on its own." || true; }
if [ "$ACCEL" != cuda ]; then gpu_groups; fi              # CUDA opens /dev/nvidia* (world rw) -- the DRM groups don't gate it

if [ "$PREFLIGHT" = 1 ]; then echo; echo "== Preflight only -- no changes made. =="; exit 0; fi

# --- ROLE: donor -> hand off to the existing setup script (pinned build + reboot-proof + self-report) ---
if [ "$ROLE" = donor ]; then
  echo "== 2. Build + serve (pinned commit $PIN) =="
  chmod +x "$POC"/*.sh 2>/dev/null || true         # a ZIP download drops the executable bit -- never let that stop a build
  case "$ACCEL" in
    cpu)  if [ -f "$POC/donor-setup-linux.sh" ]; then ( cd "$POC" && bash ./donor-setup-linux.sh ); else miss "poc/donor-setup-linux.sh not found -- is this a complete checkout?"; fi;;
    cuda) if [ -f "$POC/donor-setup-cuda.sh" ];  then ( cd "$POC" && bash ./donor-setup-cuda.sh );  else miss "poc/donor-setup-cuda.sh not found -- is this a complete checkout?"; fi;;
    vulkan)
      echo "  building llama.cpp (Vulkan + RPC) at the pin ..."
      [ -d "$POC/llama.cpp/.git" ] || git clone https://github.com/ggerganov/llama.cpp "$POC/llama.cpp"
      if ( cd "$POC/llama.cpp" && git fetch --depth 1 origin "$PIN" && git checkout "$PIN" \
           && cmake -S . -B build-vulkan -DGGML_VULKAN=ON -DGGML_RPC=ON -DLLAMA_CURL=OFF \
           && cmake --build build-vulkan --config Release -j --target ggml-rpc-server llama-cli llama-server ); then
        ok "built build-vulkan (ggml-rpc-server, llama-cli, llama-server)"
      else
        miss "the Vulkan build FAILED -- read the cmake output above (a missing -dev package is the usual cause)"
      fi;;
    *) miss "unknown --accel '$ACCEL' (cpu|cuda|vulkan)";;
  esac
  # Did the build actually land? Say so plainly instead of "Donor ready" over a missing binary.
  # donor-setup-linux.sh / -cuda.sh build under $HOME/genghis/llama.cpp (their own workspace); the vulkan
  # branch above builds under poc/llama.cpp. Accept either -- what matters is that a binary exists.
  RPC_BIN=$(find "$HOME/genghis/llama.cpp" "$POC/llama.cpp" -name ggml-rpc-server -type f 2>/dev/null | head -1)
  if [ -z "$RPC_BIN" ]; then
    miss "no ggml-rpc-server binary under ~/genghis/llama.cpp or poc/llama.cpp -- the build did not complete. Read the output above (toolchain / cmake), fix, re-run."
    exit 1
  fi
  ok "ggml-rpc-server: $RPC_BIN"

  echo "== 3. Reboot-proof serve + self-report =="
  if [ -f "$POC/donor-serve.sh" ]; then
    ( crontab -l 2>/dev/null | grep -v donor-serve.sh; echo "@reboot /bin/bash $POC/donor-serve.sh >> \$HOME/genghis-rpc.log 2>&1" ) | crontab -
    ok "@reboot donor-serve.sh installed"
    # start it now (the setup script's one-shot tmux may or may not be running); the loop is idempotent about the port
    if ! (echo > /dev/tcp/127.0.0.1/50052) >/dev/null 2>&1; then
      nohup /bin/bash "$POC/donor-serve.sh" >> "$HOME/genghis-rpc.log" 2>&1 &
      sleep 3
    fi
    if (echo > /dev/tcp/127.0.0.1/50052) >/dev/null 2>&1; then ok "ggml-rpc-server is listening on :50052"; else miss "ggml-rpc-server is not listening on :50052 yet -- check \$HOME/genghis-rpc.log"; fi
  else miss "poc/donor-serve.sh not found -- the donor will not survive a reboot."; fi

  if [ -n "$COORD" ]; then
    CH="${COORD%%:*}"
    # persist the coordinator for shells + crons (idempotent line in ~/.profile)
    grep -q "GENGHIS_COORD=" "$HOME/.profile" 2>/dev/null || echo "export GENGHIS_COORD=$COORD" >> "$HOME/.profile"
    export GENGHIS_COORD="$COORD"
    echo "== 4. Join the fleet (genghis init --coord + register, D33) =="
    if [ -f "$POC/genghis_coordinator.py" ]; then
      ( cd "$POC" && python3 genghis_coordinator.py init --yes --role compute --coord "$COORD" ) || miss "genghis init failed -- see above"
      ( cd "$POC" && python3 genghis_coordinator.py register --coord "$COORD" --port 50052 ) || miss "registration failed -- run later: python3 $POC/genghis_coordinator.py register --coord $COORD --port 50052"
    fi
    NODE_ID=$(python3 -c "import json;f=json.load(open('$POC/fleet.json'));print([d['id'] for d in f['donors'] if d.get('local')][0])" 2>/dev/null || hostname)
    if [ -f "$POC/donor-report.sh" ]; then
      ( crontab -l 2>/dev/null | grep -v donor-report.sh; echo "@reboot GENGHIS_COORD=${CH}:8899 /bin/bash $POC/donor-report.sh $NODE_ID >> \$HOME/genghis-report.log 2>&1" ) | crontab -
      nohup env GENGHIS_COORD="${CH}:8899" /bin/bash "$POC/donor-report.sh" "$NODE_ID" >> "$HOME/genghis-report.log" 2>&1 &
      ok "self-report (live free memory) running + @reboot, as $NODE_ID -> $CH"
    fi
  else
    info "no --coord given: the box is built and serving but NOT in any fleet. Join later with:"
    info "  python3 $POC/genghis_coordinator.py init --yes --coord <authority-ip>"
  fi
  if [ "$SERVE" = 1 ] && [ -n "$COORD" ] && [ -f "$POC/serve.sh" ]; then
    echo "== 5. Inference host: serve (/v1 + Control Room here, fleet from $COORD) -- reboot-proof (D30) =="
    chmod +x "$POC/serve.sh"
    ( crontab -l 2>/dev/null | grep -v "poc/serve.sh"; echo "@reboot GENGHIS_COORD=$COORD /bin/bash $POC/serve.sh" ) | crontab -
    if ! (echo > /dev/tcp/127.0.0.1/8899) >/dev/null 2>&1; then
      nohup env GENGHIS_COORD="$COORD" /bin/bash "$POC/serve.sh" >/dev/null 2>&1 &
      sleep 6
    fi
    if (echo > /dev/tcp/127.0.0.1/8899) >/dev/null 2>&1; then ok "serve is up: http://$(hostname -I | awk '{print $1}'):8899/  (mode: host of $COORD)"; else miss "serve did not come up on :8899 -- see \$HOME/genghis-serve.log"; fi
  fi
  relogin_note
  echo; echo "== Donor ready: $(hostname) serving on :50052$( [ -n "$COORD" ] && echo ", registered with $COORD" )$( [ "$SERVE" = 1 ] && echo ", inference host on :8899" ). Check the Control Room at http://${CH:-<authority>}:8899/ =="
  exit 0
fi

# --- ROLE: coordinator or client -> python + init (+ serve for coordinator) ---
echo "== 2. Generate THIS machine's fleet (genghis init) =="
if have python3 && [ -f "$POC/genghis_coordinator.py" ]; then
  chmod +x "$POC"/*.sh 2>/dev/null || true
  args=(genghis_coordinator.py init --yes); [ -n "$COORD" ] && args+=(--coord "$COORD")
  if ( cd "$POC" && python3 "${args[@]}" ); then ok "wrote poc/fleet.json + poc/config.json$( [ -n "$COORD" ] && echo ' and registered with the authority (D33)' )"; else miss "genghis init failed -- see above"; fi
  if [ -n "$COORD" ]; then grep -q "GENGHIS_COORD=" "$HOME/.profile" 2>/dev/null || echo "export GENGHIS_COORD=$COORD" >> "$HOME/.profile"; fi
else miss "python3 or poc/genghis_coordinator.py missing -- copy the poc/ folder here."; fi

if [ "$ROLE" = coordinator ]; then
  echo "== 3. Make serve reboot-proof (@reboot cron) =="
  if [ -f "$POC/serve.sh" ]; then
    chmod +x "$POC/serve.sh" 2>/dev/null || true
    ( crontab -l 2>/dev/null | grep -v "poc/serve.sh"; echo "@reboot /bin/bash $POC/serve.sh" ) | crontab -
    nohup bash "$POC/serve.sh" >/dev/null 2>&1 &
    ok "serve started + @reboot cron installed -> /v1 + Control Room on :8899"
  else miss "poc/serve.sh not found -- copy it here (or run: python3 genghis_coordinator.py serve)."; fi
  info "Stage models in poc/models/ (the repo); manage nodes at http://localhost:8899/admin"
fi
relogin_note
echo; echo "== Done ($ROLE). Full guide: INSTALL.md =="
