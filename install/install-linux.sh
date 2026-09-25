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
#           --name NAME   this box's name in the fleet (default: its hostname)
#
# It never silently changes your system: apt installs are announced and (without --yes) confirmed. The one
# thing it can't automate -- the NVIDIA driver on a CUDA donor -- is detected and you're told what to do.
set -uo pipefail

ROLE=""; ACCEL="cpu"; COORD=""; YES=0; PREFLIGHT=0; SERVE=0; NAME=""
while [ $# -gt 0 ]; do case "$1" in
  --role)  ROLE="$2"; shift 2;;
  --accel) ACCEL="$2"; shift 2;;
  --coord) COORD="$2"; shift 2;;
  --yes)   YES=1; shift;;
  --serve) SERVE=1; shift;;
  --preflight) PREFLIGHT=1; shift;;
  --name)  NAME="$2"; shift 2;;
  -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown arg: $1 (see --help)"; exit 2;;
esac; done
[ -n "$ROLE" ] || { echo "usage: install-linux.sh --role coordinator|donor|client [--accel cpu|cuda|vulkan] [--coord HOST] [--yes] [--preflight]"; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"; POC="$(cd "$HERE/../poc" && pwd)"
PIN="eab8ee41f889ef7823af517e8098fb8a9b3cf601"
ok(){   echo "  [ ok ] $*"; }
miss(){ echo "  [ -- ] $*"; }
info(){ echo "     $*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
ask(){
  [ "$YES" = 1 ] && return 0
  # No terminal to ask on (an agent, a pipe, cron): `read` gets nothing and the answer used to be a silent "no".
  # Say so, and say how to consent instead, so a skipped step is never mistaken for a done one.
  if [ ! -t 0 ]; then
    echo "  [ ?? ] $1 -- no terminal to ask on, so: NO. To accept, re-run with --yes (after the person has agreed)."
    return 1
  fi
  read -r -p "  $1 [y/N] " a; [ "$a" = y ] || [ "$a" = yes ]
}
apt_need(){ # apt_need <pkg> <label>
  { have "$1" || dpkg -s "$1" >/dev/null 2>&1; } && { ok "$2"; return 0; }   # a command, or an installed package
  miss "$2 -- missing"
  # --preflight promises "detect only, no changes": say what WOULD be installed, never install (not even with --yes --
  # an agent told that preflight changes nothing may well pass both).
  if [ "$PREFLIGHT" = 1 ]; then info "Fix: sudo apt install $1"; return 1; fi
  if have apt-get && ask "apt install $1?"; then sudo apt-get update -qq && sudo apt-get install -y "$1"; ok "installed $2"; return 0; fi
  return 1
}

echo "== GENGHIS Linux installer -- role: $ROLE${ACCEL:+ / accel: $ACCEL} on $(hostname) ($(uname -m)) =="
echo "== 1. Preflight =="
have python3 && ok "python3 ($(python3 --version 2>&1))" || apt_need python3 "python3 (the coordinator IS a Python program)"
apt_need git "git" || true
if [ -n "$COORD" ]; then
  CHOST="${COORD%%:*}"; CPORT="${COORD##*:}"; [ "$CPORT" = "$COORD" ] && CPORT=8899
  if (: > "/dev/tcp/$CHOST/$CPORT") >/dev/null 2>&1; then ok "the authority answers at $CHOST:$CPORT"
  else miss "the authority does not answer at $CHOST:$CPORT -- check the address, that its serve is running, and its firewall (TCP $CPORT)"; fi
  RDEV=$(ip route get "$CHOST" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
  if [ -n "$RDEV" ] && [ -e "/sys/class/net/$RDEV/wireless" ]; then
    info "note: this box reaches the authority over Wi-Fi ($RDEV). It works, but a donor on a cable is faster (a Pi 5: 7.6 -> 9.7 t/s)."
  fi
fi
# A box a person USES (the authority, a host with --serve, a client) reads PDFs attached to a chat and in a role's
# knowledge folder (D50); both need pypdf. Optional: without it every PDF is named as unreadable, never skipped quietly.
# A donor only lends its CPU/GPU and never reads a document, so it is never offered.
if [ "$ROLE" = coordinator ] || [ "$ROLE" = client ] || [ "$SERVE" = 1 ]; then
  if python3 -c "import pypdf" 2>/dev/null; then ok "pypdf (PDFs attached to a chat, or in a role's knowledge folder, can be read)"
  else
    miss "pypdf -- optional: without it, a PDF attached to a chat or in a role's knowledge folder is not read (and says so)"
    if [ "$PREFLIGHT" = 1 ]; then info "Fix: sudo apt install python3-pypdf"
    elif have apt-get && ask "apt install python3-pypdf (so PDFs in chats and role knowledge can be read)?"; then
      if sudo apt-get update -qq && sudo apt-get install -y python3-pypdf; then ok "installed pypdf"
      else miss "python3-pypdf is not available here -- try: python3 -m pip install --user pypdf"; fi
    fi
  fi
fi
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
# The finish line: an install is done when `verify` passes, not when this script reaches its last line. verify is
# read-only; it prints what it checked (with the fix for anything that failed) and the addresses worth bookmarking,
# and saves those to ~/genghis-addresses.txt. Its exit code becomes ours, so a script or an agent can gate on it.
finish_verify(){   # $1 = the authority host to wait for (empty: don't wait)
  if ! have python3 || [ ! -f "$POC/genghis_coordinator.py" ]; then info "verify skipped (python3 or poc/genghis_coordinator.py missing)"; return 0; fi
  if [ -n "$1" ]; then   # a serve that just started needs a few seconds before it answers
    for _ in $(seq 1 20); do (: > "/dev/tcp/$1/8899") >/dev/null 2>&1 && break; sleep 1; done
  fi
  echo; echo "== Verify: is this box really in the fleet? (read-only; re-run any time: python3 $POC/genghis_coordinator.py verify) =="
  ( cd "$POC" && python3 genghis_coordinator.py verify )
}
# Only Vulkan opens /dev/dri for COMPUTE. CUDA opens /dev/nvidia* (world rw), so the DRM groups don't gate it; a CPU
# donor's /dev/dri is at most a display adapter (a VM's virtual SVGA), and re-grouping the user for it was pure noise.
if [ "$ACCEL" = vulkan ]; then gpu_groups; fi

# --- the donor port and the firewall ------------------------------------------------------------------------------
# ggml-rpc-server listens on 0.0.0.0:50052 and is not built for open networks (its own log says so). With a firewall on,
# open it to THIS network only; with none, say so plainly. It used to say nothing: a donor behind an active ufw was
# unreachable with no word as to why (a fresh-box test, 2026-09-25). Mirrors the Windows installer's local-subnet rule.
lan_net(){ local dev cidr
  dev=$(ip route get "${COORD%%:*}" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
  [ -n "$dev" ] || dev=$(ip route show default 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
  cidr=$(ip -o -f inet addr show "$dev" 2>/dev/null | awk '{print $4}' | head -1)
  [ -n "$cidr" ] && python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$cidr" 2>/dev/null
}
fw_kind(){
  if [ -r /etc/ufw/ufw.conf ] && grep -qi '^ENABLED=yes' /etc/ufw/ufw.conf; then echo ufw
  elif have systemctl && systemctl is-active --quiet firewalld 2>/dev/null; then echo firewalld; fi
}
fw_cmd(){ # $1 kind  $2 net
  case "$1" in
    ufw) echo "sudo ufw allow from $2 to any port 50052 proto tcp comment 'GENGHIS donor'";;
    firewalld) echo "sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=$2 port port=50052 protocol=tcp accept' && sudo firewall-cmd --reload";;
  esac
}
donor_firewall(){
  local kind net cmd; kind=$(fw_kind); net=$(lan_net)
  if [ -z "$kind" ]; then
    info "firewall: none active -- :50052 answers anything that can reach this box. Keep it on your home network (the RPC server is not built for open ones)."
    return 0
  fi
  [ -n "$net" ] || { miss "firewall: $kind is active, but this box's network could not be worked out -- allow TCP 50052 from your LAN yourself"; return 0; }
  cmd=$(fw_cmd "$kind" "$net")
  if [ "$PREFLIGHT" = 1 ]; then info "firewall: $kind is active -- the real run offers: $cmd"; return 0; fi
  if ask "open TCP 50052 in $kind for this network only ($net), so the fleet can use this donor?"; then
    if eval "$cmd" >/dev/null; then ok "firewall: TCP 50052 allowed from $net ($kind)"; else miss "the firewall command failed -- run it yourself: $cmd"; fi
  else miss "firewall: the fleet cannot reach this donor until TCP 50052 is allowed: $cmd"; fi
}

if [ "$PREFLIGHT" = 1 ]; then
  # Everything the real run will change, so a person saying "yes" (--yes) has seen it first.
  echo; echo "  The real run will also:"
  case "$ROLE" in
    donor)
      if [ "$ACCEL" = vulkan ]; then info "- fetch llama.cpp at $PIN into $POC/llama.cpp and build it (Vulkan + RPC)"
      else info "- fetch llama.cpp at $PIN into ~/genghis/llama.cpp and build ggml-rpc-server ($ACCEL)"; fi
      info "- start the donor now (donor-serve.sh, one per port) and add an @reboot crontab line for it"
      donor_firewall
      [ -n "$COORD" ] && info "- add 'export GENGHIS_COORD=$COORD' to ~/.profile, register this box with the authority, and start a"
      [ -n "$COORD" ] && info "  self-report of its free memory (another @reboot crontab line)"
      [ "$SERVE" = 1 ] && info "- start the /v1 serve here too, with an @reboot crontab line";;
    coordinator)
      info "- start the serve (Control Room, /v1, the fleet record) and add an @reboot crontab line for it"
      info "- add the watchdog: a crontab line every 15 min";;
  esac
  info "- write this box's poc/fleet.json + poc/config.json (machine-local, git-ignored), then run verify"
  echo; echo "== Preflight only -- no changes made. =="; exit 0
fi

# --- ROLE: donor -> hand off to the existing setup script (pinned build + reboot-proof + self-report) ---
if [ "$ROLE" = donor ]; then
  echo "== 2. Build + serve (pinned commit $PIN) =="
  chmod +x "$POC"/*.sh 2>/dev/null || true         # a ZIP download drops the executable bit -- never let that stop a build
  case "$ACCEL" in
    cpu)  if [ -f "$POC/donor-setup-linux.sh" ]; then ( cd "$POC" && GENGHIS_NO_START=1 bash ./donor-setup-linux.sh ); else miss "poc/donor-setup-linux.sh not found -- is this a complete checkout?"; fi;;
    cuda) if [ -f "$POC/donor-setup-cuda.sh" ];  then ( cd "$POC" && GENGHIS_NO_START=1 bash ./donor-setup-cuda.sh );  else miss "poc/donor-setup-cuda.sh not found -- is this a complete checkout?"; fi;;
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
  # donor-serve.sh is the ONE launcher of ggml-rpc-server (single instance per port, restarts on crash). The setup
  # scripts are told not to start a server of their own (GENGHIS_NO_START=1 above): a fresh-box test found the setup's
  # tmux server holding the port, unsupervised, while this loop failed to bind every 5 s (2026-09-23). The binary is
  # pinned in the @reboot line (D46: with two builds on a box, an unpinned launcher can pick the wrong one), and the
  # log is donor-serve.sh's own: $HOME/genghis/rpc.log.
  RPC_LOG="$HOME/genghis/rpc.log"
  if [ -f "$POC/donor-serve.sh" ]; then
    ( crontab -l 2>/dev/null | grep -v donor-serve.sh; echo "@reboot GENGHIS_RPC_BIN=$RPC_BIN /bin/bash $POC/donor-serve.sh >/dev/null 2>&1" ) | crontab -
    ok "@reboot donor-serve.sh installed (binary pinned: $RPC_BIN)"
    GENGHIS_RPC_BIN="$RPC_BIN" setsid nohup /bin/bash "$POC/donor-serve.sh" >/dev/null 2>&1 < /dev/null &
    for _ in $(seq 1 15); do (: > /dev/tcp/127.0.0.1/50052) >/dev/null 2>&1 && break; sleep 1; done
    if (: > /dev/tcp/127.0.0.1/50052) >/dev/null 2>&1; then ok "ggml-rpc-server is listening on :50052 (log: $RPC_LOG)"
    else miss "ggml-rpc-server is not listening on :50052 yet -- check $RPC_LOG"; fi
  else miss "poc/donor-serve.sh not found -- the donor will not survive a reboot."; fi
  donor_firewall                                    # before registering: the authority dials the port when a box registers

  if [ -n "$COORD" ]; then
    CH="${COORD%%:*}"
    # persist the coordinator for shells + crons (idempotent line in ~/.profile)
    grep -q "GENGHIS_COORD=" "$HOME/.profile" 2>/dev/null || echo "export GENGHIS_COORD=$COORD" >> "$HOME/.profile"
    export GENGHIS_COORD="$COORD"
    echo "== 4. Join the fleet (genghis init --coord + register, D33) =="
    if [ -f "$POC/genghis_coordinator.py" ]; then
      ( cd "$POC" && python3 genghis_coordinator.py init --yes --role compute --coord "$COORD" ${NAME:+--node-name "$NAME"} ) || miss "genghis init failed -- see above"
      ( cd "$POC" && python3 genghis_coordinator.py register --coord "$COORD" --port 50052 ) || miss "registration failed -- run later: python3 $POC/genghis_coordinator.py register --coord $COORD --port 50052"
    fi
    # Report under the name the AUTHORITY registered this box as, which can differ from the local one (a box it
    # already knows keeps its fleet name; a second machine with the same hostname gets a name of its own). A report
    # for a name the authority doesn't know is rejected -- and used to vanish silently (a fresh-box test, 2026-09-23).
    NODE_ID=$(python3 - "$CH" 2>/dev/null <<'PY'
import json, socket, sys, urllib.request
c = sys.argv[1]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect((c, 8899)); me = s.getsockname()[0]
f = json.load(urllib.request.urlopen("http://%s:8899/fleet.json" % c, timeout=5))
print(next(d["id"] for d in f["donors"] if d.get("ip") == me and str(d.get("port")) == "50052"))
PY
)
    [ -n "$NODE_ID" ] || NODE_ID=$(python3 -c "import json;f=json.load(open('$POC/fleet.json'));print([d['id'] for d in f['donors'] if d.get('local')][0])" 2>/dev/null || hostname)
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
    if ! (: > /dev/tcp/127.0.0.1/8899) >/dev/null 2>&1; then
      nohup env GENGHIS_COORD="$COORD" /bin/bash "$POC/serve.sh" >/dev/null 2>&1 &
      sleep 6
    fi
    if (: > /dev/tcp/127.0.0.1/8899) >/dev/null 2>&1; then ok "serve is up: http://$(hostname -I | awk '{print $1}'):8899/  (mode: host of $COORD)"; else miss "serve did not come up on :8899 -- see \$HOME/genghis-serve.log"; fi
  fi
  relogin_note
  echo; echo "== Donor built: $(hostname) serving on :50052$( [ -n "$COORD" ] && echo ", registered with $COORD" )$( [ "$SERVE" = 1 ] && echo ", inference host on :8899" ) =="
  finish_verify "${CH:-}"
  exit $?
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
  # The watchdog: an independent 15-min pass that checks every node, stamps the Control Room's "verified HH:MM", and
  # sets up a chat UI running on this box (Open WebUI's per-model settings: its ~30 built-in tools OFF, which a local
  # model otherwise calls instead of answering). It used to be a manual step, so a newcomer's chat UI never got it.
  if [ -f "$POC/watchdog.py" ]; then
    ( crontab -l 2>/dev/null | grep -v "poc/watchdog.py"; echo "*/15 * * * * /usr/bin/env python3 $POC/watchdog.py >> $POC/watchdog.cron.log 2>&1" ) | crontab -
    ok "watchdog installed (every 15 min): node check + Open WebUI setup"
  else miss "poc/watchdog.py not found -- the Control Room will not show a verified time, and a chat UI here is not set up."; fi
  info "Stage models in poc/models/ (the repo); manage nodes at http://localhost:8899/admin"
fi
relogin_note
echo; echo "== Installed ($ROLE). Full guide: INSTALL.md =="
if [ "$ROLE" = coordinator ]; then finish_verify 127.0.0.1; else finish_verify "${COORD%%:*}"; fi
exit $?
