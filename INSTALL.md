# GENGHIS — Install Guide

Set up a GENGHIS fleet on your own hardware. It has four kinds of node — install only what you have:

| Role | What it is | Where |
|---|---|---|
| **Authority** (the coordinator) | the always-on source of truth — fleet + config + model library + mDNS + watchdog (+ the HEARTH backend) | your most reliable always-on box: a mini-PC, a server, a Raspberry Pi |
| **Host** | a front door: runs `serve` (`/v1`, warm models, a Control Room) on its own GPU | the authority, plus any box you sit at (Windows / Linux) |
| **Donor** | lends CPU / GPU / RAM to a run (`ggml-rpc-server`) | any Linux box, Raspberry Pi, Jetson, NVIDIA or Intel Arc box, or Windows PC |
| **Surface** (TV / HEARTH) | the optional household display | a Samsung Tizen TV — see [`docs/TV_CLIENT_INSTALL.md`](docs/TV_CLIENT_INSTALL.md) |

One machine can hold several roles (the reference fleet's authority, an Intel NUC, is authority, host **and** a donor of two GPUs; the laptop is a host **and** lends its GPU). Where this guide says **client** (a box you launch models from), read *host*.
Macs are designed-for but not yet measured (§4b).

---

## ⚡ Fast path — the installers (D18)
Since 2026-09-12 there is **one installer per role** in [`install/`](install/) that does everything below for you —
it **detects** what's present, **tells you plainly** what's missing, **offers** to install what it can (with
confirmation), **guides** the two things it can't (the VS C++ workload on Windows; the NVIDIA driver on Linux),
builds llama.cpp **at the pinned commit**, runs `genghis init`, and makes the role **reboot-proof**. Idempotent — re-run any time.

```powershell
# Windows client (add -Serve -Coord <authority-ip> to also host /v1 + chat; -Donor to also lend this GPU; -PreflightOnly = check only)
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1
```
The Windows installer picks the accelerator itself: NVIDIA + CUDA toolkit → `build-cuda`; **any other GPU (Intel Arc / AMD) +
the Vulkan SDK → `build-vulkan`** (it offers to install the SDK); else CPU. A box can be **client + donor at once** (D27) —
e.g. the NUC: `-Serve -Donor -Coord <authority-ip>` makes its Arc the local anchor for its own runs *and* an RPC donor for everyone else.
```bash
# Linux — coordinator / donor / client
bash install/install-linux.sh --role coordinator
bash install/install-linux.sh --role donor --accel cpu|cuda|vulkan
bash install/install-linux.sh --role client --coord <authority-ip>
```
An **NVIDIA DGX Spark** is a Linux CUDA donor (`--role donor --accel cuda`) — see [`docs/DGX_SPARK.md`](docs/DGX_SPARK.md).
Details + what each installer checks: [`install/README.md`](install/README.md). The sections below are the
**manual path** (what the installers automate) — still useful to understand a node or to fix one by hand.

---

## ⚠️ Three golden rules (read first — these cause 90% of problems)

1. **Every inference node builds the SAME pinned llama.cpp commit.** llama.cpp RPC has **zero** cross-version tolerance — a mismatched build fails silently or weirdly. The `donor-setup-*.sh` scripts check out the pinned commit automatically; don't hand-update one node.
2. **The Python command is platform-specific.** **Linux / macOS → `python3`**. **Windows → `py`**. `python`/`py` usually don't exist on Linux — that's the "command not found."
3. **Ports:** donors listen on **`50052`**, the coordinator/HEARTH on **`8899`**. Open them on the LAN. (Pi OS has no firewall by default; Windows blocks inbound on a *Public* network — set it to Private or add a rule, which is why the always-on `serve` lives on the always-on authority, not the laptop.)

---

## 1 · Coordinator (the authority: a mini-PC, a server, a Raspberry Pi — any always-on Linux box)

The brain: the authority + model repo + HEARTH backend. It does **not** need a GPU.

**Prereqs:** `git`, and **`python3`** — the coordinator *is* a Python program, so this is non-negotiable.
On Pi OS / Debian / Ubuntu:
```bash
sudo apt update && sudo apt install -y python3 git
python3 --version        # confirm it's there (any 3.x)
```
*(If `python3` ever goes missing after an OS change, `serve.sh` now logs a clear "install python3" message
and auto-recovers the moment you install it — no silent looping.)*

**Steps:**
```bash
# on the authority, as a normal user
mkdir -p ~/genghis && cd ~/genghis
# copy poc/genghis_coordinator.py, poc/genghis_mdns.py, poc/serve.sh here (git clone or scp)
# GENERATE YOUR OWN fleet.json + config.json for THIS machine (D22 — ships nothing of ours):
python3 genghis_coordinator.py init --yes        # detects this box; add donors later via `discover` or by hand
#   (interactive `init` — no --yes — prompts for node name, models dir, coordinator; re-run anytime, it backs up)
chmod +x serve.sh
# start it now, and make it auto-start at boot (no sudo needed):
nohup bash ~/genghis/serve.sh >/dev/null 2>&1 &
( crontab -l 2>/dev/null | grep -v serve.sh; echo "@reboot /bin/bash $HOME/genghis/serve.sh" ) | crontab -
```
**Verify** (from any machine on the LAN):
```bash
curl -s http://<AUTHORITY_IP>:8899/fabric        # should print a FAB / NODE… snapshot
```
**Zero-config discovery:** `serve` advertises itself on the LAN via mDNS (`_genghis._tcp`, UDP 5353, pure
stdlib — no zeroconf/Avahi). Clients **find it automatically** — no need to know the IP:
```bash
python3 genghis_coordinator.py discover   # prints the coordinator URL it found
```
Planning commands auto-discover when the default/configured coordinator is unreachable. To pin one instead,
set `GENGHIS_COORD=<host>` (or the full `GENGHIS_FLEET_URL`).
**Manage it from a browser:** open **`http://<AUTHORITY_IP>:8899/`** — the **Control Room** (the fleet, which model
each speed setting runs, what is warm where). The original **web admin** (friendly names, roles, default run-type, PIN)
is at **`/admin`**. Changes save to the authority (`config.json`) and take effect everywhere the fabric shows
(including the TV).

> **Where do models go?** Into the authority's models folder (the `models/` folder next to the coordinator — here
> `~/genghis/models/`; with the installer, `~/genghis-src/poc/models/`). That folder is the fleet's **model library**,
> served at `/models`. Every host runs a model by bare name and **fetches it if missing** (resumable), and a host's
> `serve` pre-fetches its speed settings' models when it starts. Donors never need the file. More:
> **[Models: the one-folder rule](docs/MODELS.md)**.
```bash
scp your-model.gguf <USER>@<AUTHORITY_IP>:~/genghis/models/     # stage a model in the library
python3 genghis_coordinator.py models                       # list the library + this box's local cache
python3 genghis_coordinator.py models pull your-model.gguf  # pull one into this box's cache ahead of time
```

---

## 2 · Compute donor — Linux / Raspberry Pi (CPU)

**Prereqs:** a Linux userland + LAN. **Steps:**
```bash
mkdir -p ~/genghis && cd ~/genghis
# copy poc/donor-setup-linux.sh, poc/donor-serve.sh, poc/donor-report.sh here
chmod +x donor-setup-linux.sh donor-serve.sh donor-report.sh

./donor-setup-linux.sh          # builds ggml-rpc-server at the PINNED commit (self-heals old gcc/cmake) + serves once

# make the donor role reboot-proof (this is what a hand-run tmux session does NOT do):
( crontab -l 2>/dev/null | grep -v donor-serve.sh;  echo "@reboot $HOME/genghis/donor-serve.sh  >> $HOME/genghis/rpc.log 2>&1" ) | crontab -
( crontab -l 2>/dev/null | grep -v donor-report.sh; echo "@reboot $HOME/genghis/donor-report.sh <node-id> >> $HOME/genghis/report.log 2>&1" ) | crontab -
export GENGHIS_COORD=<AUTHORITY_IP>:8899    # point at YOUR coordinator
nohup ~/genghis/donor-serve.sh  >> ~/genghis/rpc.log    2>&1 &   # start serving now
nohup ~/genghis/donor-report.sh <node-id> >> ~/genghis/report.log 2>&1 &   # start self-reporting RAM
```
`<node-id>` is this donor's id in `fleet.json` (e.g. `pi5-8gb`). **Verify:** on the coordinator/client,
`python3 genghis_coordinator.py heartbeat` shows this node **UP**.

> **Known-hard cases** (the setup script handles them, but so you know): very old ARM (Tegra X1 on
> Ubuntu 18.04) needs **gcc-10** for NEON `_x4` intrinsics; the script installs it.

---

## 3 · Compute donor — NVIDIA GPU (CUDA)

Same as §2 but with the CUDA script — the shard runs **on the GPU**:
```bash
./donor-setup-cuda.sh           # installs the driver if missing, checks CUDA 13.2+, builds -DGGML_CUDA=ON at the pinned commit
# then the SAME donor-serve.sh + donor-report.sh crons as §2 (report picks up VRAM via nvidia-smi)
```
The card's architecture is read from the driver (`nvidia-smi` compute capability 12.0 → `CUDA_ARCH=120`); set
`CUDA_ARCH` yourself only to override it (**Blackwell 120**, Ada 89, Ampere 86, Turing 75, Pascal 61).
> **Supported cards: RTX 20 / GTX 16-series (Turing) and newer (D52); GTX 10-series is experimental**, all on **CUDA 13.2 or newer from NVIDIA's repository**;
> the script refuses older cards (`GENGHIS_ALLOW_OLD_GPU=1` to try anyway) and, if CUDA 13.2+ is missing, stops and
> prints NVIDIA's install commands. With 13.2 no GCC-14 host compiler or glibc patch is needed. (Older notes: Pascal (GTX
> 10-series) needs Ubuntu 24.04 + driver ≤570 — see [`DECISIONS.md`](DECISIONS.md) D4 and [`docs/agents/gpus.md`](docs/agents/gpus.md).)
> **CUDA 13.0 vs glibc 2.43** (Ubuntu 26.04): `nvcc` fails on `rsqrt`/`rsqrtf` exception specs before it compiles
> anything of ours. Install **13.2 or newer** from NVIDIA's repo (`cuda-keyring` for `ubuntu2404`, then
> `cuda-toolkit-13-2`); Ubuntu's own `nvidia-cuda-toolkit` is 12.4 and too old for a 50-series card anyway.

### A second card on a host — an eGPU, or two GPUs in one box (D46)
A box can lend **more than one card**: run a second `ggml-rpc-server` on another port, one per device. The reference
fleet does exactly this — the NUC serves its Arc on `:50052` (Vulkan build) and an **RTX 5060 Ti in a Thunderbolt 4
enclosure** on `:50053` (CUDA build):
```bash
# second donor: its own port, its own binary, its own device
GENGHIS_RPC_BIN=~/genghis-src/poc/llama.cpp/build-cuda/bin/ggml-rpc-server GENGHIS_RPC_DEVICE=CUDA0 GENGHIS_RPC_PORT=50053 GENGHIS_RPC_LOG=$HOME/genghis/rpc-cuda.log   ./donor-serve.sh
# register it as its own node, under its OWN host label (see the warning below)
python3 genghis_coordinator.py register --coord <authority-ip> --port 50053 --id <box>-<card> --host <box>-egpu
```
Three things that will bite you, all learned the hard way:
- **Pin the binary and the device in every `@reboot` line.** `donor-serve.sh` auto-finds a `ggml-rpc-server` when
  `GENGHIS_RPC_BIN` is unset — with two builds present it can pick the wrong one after a reboot and serve the wrong card.
- **Give the second card its own host label** (`<box>-egpu`). A node is judged "this box" by *hostname*; if the second
  card shares the host's name, the host's own serve will treat it as its local anchor instead of dialling it over RPC.
- **It is local-class, and that is the point.** GENGHIS knows a donor on the same box has no wire (D46): it can be the
  faster card the planner prefers over the host's own, and it can hold a **warm model** over loopback. Moving the
  reference fleet's 5060 Ti from a 1 GbE box into the NUC's enclosure took it from **38 t/s to 228 t/s** — same card.
  Thunderbolt 4 (~32 Gb/s) is far more than a donor needs; a shard streams in once and stays.
- **If the enclosure "does nothing":** check that *any* host sees anything on the cable at all (`boltctl list`,
  `lspci`, Windows Device Manager). Nothing on the cable = power, cable or port — not software. A dead contact switch
  in the enclosure looks exactly like a broken card.

---

## 4 · Host by hand (Windows — the machine you run models from)

**Prereqs:** `git`, CMake, a C++ toolchain (MSVC/VS Build Tools); optional NVIDIA CUDA toolkit to use a
**local GPU as the anchor**. **Steps:**
```powershell
cd poc\llama.cpp
# CPU + RPC only:
cmake -S . -B build-rpc -DGGML_RPC=ON -DLLAMA_CURL=OFF
cmake --build build-rpc --config Release --target llama-cli ggml-rpc-server
# …OR with a local NVIDIA GPU as the anchor (adds ggml-cuda; keep RPC on):
cmake -S . -B build-cuda -G "Visual Studio 18 2026" -A x64 -DGGML_CUDA=ON -DGGML_RPC=ON -DCMAKE_CUDA_ARCHITECTURES=<arch>
cmake --build build-cuda --config Release --target llama-cli llama-server ggml-rpc-server
# …OR with an Intel Arc / AMD GPU as the anchor (needs the Vulkan SDK; D27):
cmake -S . -B build-vulkan -A x64 -DGGML_VULKAN=ON -DGGML_RPC=ON -DLLAMA_CURL=OFF
cmake --build build-vulkan --config Release --target llama-cli llama-server ggml-rpc-server
```
The coordinator looks for `build-cuda`, then `build-vulkan`, then `build-rpc`. Point it at your fleet + a model:
```powershell
$env:GENGHIS_MODEL = "E:\models\<model>.gguf"
$env:GENGHIS_FLEET_URL = "http://<AUTHORITY_IP>:8899/fleet.json"   # point at YOUR coordinator
py genghis_coordinator.py decide --goal balanced
```

**Host `/v1` here (the fast 5090 path) + make it reboot-proof.** To serve the OpenAI API with the local GPU
as anchor, run `serve` on this box — and use the Windows reboot-survival launcher so a reboot doesn't kill it
(the analogue of the Pi's `@reboot` cron):
```powershell
# start it now
Start-Process -WindowStyle Hidden powershell -ArgumentList '-ExecutionPolicy','Bypass','-File','<repo>\poc\serve-laptop.ps1'
# auto-start at every logon (NO admin): a hidden launcher in the per-user Startup folder
$s = [Environment]::GetFolderPath('Startup')
Set-Content (Join-Path $s 'GENGHIS-serve.vbs') 'CreateObject("WScript.Shell").Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File ""<repo>\poc\serve-laptop.ps1""", 0, False' -Encoding ASCII
```
**Reachability:** open TCP `8899` for *all* profiles (see Troubleshooting — the auto-created per-app rule is Private-only). If you
just ran the installer with `-Coord`, start serve from a **new** window so the `GENGHIS_COORD` user variable is visible.

[`poc/serve-laptop.ps1`](poc/serve-laptop.ps1) is a crash-restart loop (mirrors `serve.sh`); the Startup
entry restarts it at logon. (The always-on authority in §1 hosts `/v1` too, on its own GPU if it has one; a laptop serve is for the box you sit at.)

**Chat GUI (optional) — Open WebUI.** Point the adopted human GUI at the coordinator's `/v1`:
```powershell
docker run -d -p 3080:8080 --name open-webui --restart always -e WEBUI_AUTH=False `
  -e OPENAI_API_BASE_URL=http://host.docker.internal:8899/v1 -e OPENAI_API_KEY=none `
  -v open-webui:/app/backend/data ghcr.io/open-webui/open-webui:v0.11.4
```
Open `http://localhost:3080` (no login). Image tag is **pinned** (D18) — bump deliberately, not via `:latest`.

## 4b · Client — macOS *(designed-for; not yet verified on real hardware)*
Same shape: build llama.cpp with `-DGGML_METAL=ON -DGGML_RPC=ON` (Macs get the Metal GPU backend), then
run with `python3`. **Honest status:** the reference fleet has no Mac yet, so treat these steps as the
*pattern* and verify before relying on them.

---

## 5 · TV / HEARTH (optional)
The household surface is its own guide: [`docs/TV_CLIENT_INSTALL.md`](docs/TV_CLIENT_INSTALL.md) (Tizen dev
sideload → prosumer installer → Samsung store) and **HEARTH** *(coming soon)*. HEARTH
consumes the coordinator's `serve` endpoints; it needs a working coordinator (§1) first.

---

## Where things live — the two-question rule (D30)
GENGHIS has two "coordinator" jobs: the **authority** (fleet + config + model library + mDNS — needs to be *always on*)
and the **home base** (`/v1`, warm models, the Docker tier, ambient agents — needs a *GPU*). Ask of each box: **is it always
on? does it have a GPU?** The authority goes on the most reliable always-on box; if that box also has the GPU it is the home
base too (one box, one role — the common case). Split them only when your always-on box has no GPU (a Pi/NAS + a gaming PC).
One fleet has **one** authority; every other `/v1` host runs `serve --coord <authority>` (or has `GENGHIS_COORD` set — the
installers do that for you with `-Coord`/`--coord`). A host's Control Room shows the whole fleet from its own vantage, its
`/v1` runs on its own GPU, and anything you change there (names, goals, retire/register) is forwarded to the authority.
Moving the authority to a new box:
[docs/COORDINATOR_MIGRATION.md](docs/COORDINATOR_MIGRATION.md).

### Hosts and donors — head and hands (read this once)
Running a model is two jobs. The **engine** — tokenizing your prompt, placing the layers, sampling each next token,
keeping the conversation, speaking OpenAI `/v1` — runs in *one* place: a **host** (`serve`). The **layers** — the matrix
math — can be split across any GPUs that engine can reach. A **donor** runs only `ggml-rpc-server`, which is deliberately
simple: *"send me tensors and ops; I keep them in my memory and compute what you ask."* It does real compute — a donor
holding a third of a 70B runs those layers on its own GPU, every token — but it has no engine, so it cannot hold a model
warm or answer a chat **on its own**; it holds a *shard* for some host. Host = head + hands; donor = hands only. A fleet
can have many hands; each warm model needs exactly one head.

**Which boxes should run `serve`?** For most people (and the reference fleet): **the authority, plus any box you actually
sit at** (your laptop). Every other GPU is a donor and needs nothing more — that is the setup this project is built and
presented around. Add a serve to a donor (`install-linux.sh --role donor … --coord <authority> --serve`, or the Windows
installer's `-Serve`) **only when you want that box to hold models warm and answer on its own** — a second daily driver,
or a large shop where several people each want a card that answers instantly.

> **More serves is not more speed.** A serve adds a *head*, not horsepower: it does not make a card faster, does not lend
> more memory, and does not make a pooled model answer sooner — a warm model has exactly one engine wherever it lives, and
> the wire between shards costs the same however many hosts you run. What extra serves *do* add is more boxes that can
> each hold their own warm models and answer on their own — which also means more warm models competing for the same
> cards. The default layout (one authority, a serve where you sit, everything else a donor) is the design, not a
> limitation: one head per model, every card free to be lent. Add heads for a reason you can name, not for speed.

Two words to remember: *donor* = my card is the fleet's to use; *host* = my box answers for itself (and may still lend).

## The Docker tier (Open WebUI · Grafana · Prometheus) — one copy, on the home base (D29)
Optional, and it runs in exactly one place: the always-on box that hosts `/v1`. On that box:
```bash
docker compose up -d          # from the repo root; the labelled `genghis` project (D21) — never bare `docker run`
```
Open WebUI → `http://<home-base>:3080`, Grafana `:3000`, Prometheus `:9090` (scrape target in `monitoring/prometheus.yml`).
On any other host the Control Room's cards read *"not on this host"* — that is correct, not broken. Don't run it on a Pi (it
starves the donor/library) or on a laptop that leaves the house.

## Reach it from anywhere (optional) — two front doors

The fabric is **LAN-only by design**: nothing listens on the internet, and the installers open nothing. To use it
from a phone, an office, or another house, pick **one** of these. Both keep every port closed to the world.

| | **1 · Tailscale** *(what the reference fleet runs)* | **2 · Cloudflare Tunnel + Access** |
|---|---|---|
| How it reaches you | a private WireGuard network between *your* devices | an outbound tunnel to Cloudflare's edge |
| Visitor needs | the Tailscale app, signed into your tailnet | nothing — any browser, with a login |
| Address | `http://100.x.y.z:8899`, or a MagicDNS name | `https://genghis.yourdomain.com` |
| Your domain / DNS | not needed | needed (a domain on Cloudflare) |
| Good for | you and your own devices; a household; a small team you can invite | people you will not put on your network; a public-facing demo |
| Identity → GENGHIS roles | optional (`tailscale serve`, below) | built in (Access → email/group) |

Whichever you choose, **put it on the box that hosts the Control Room** (the home base), not only on the laptop you
carry — otherwise the address you bookmarked leaves the house with you.

### Option 1 — Tailscale
```bash
# on the home base (and on each device you want to reach it from)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                      # sign in; the box gets a 100.x.y.z address
tailscale ip -4                        # the address to bookmark
```
Then browse `http://100.x.y.z:8899` (Control Room), `:3080` (chat), `:3000` (Grafana). With **MagicDNS** on (the
Tailscale admin console, default for new tailnets) the box's own name works too: `http://home-base:8899`.

If a remote device cannot connect, allow the ports **from the tailnet range only** — Windows, admin PowerShell:
```powershell
New-NetFirewallRule -DisplayName "GENGHIS over Tailscale" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8899,3080,3000 -RemoteAddress 100.64.0.0/10 -Profile Any
```
Linux with ufw: `sudo ufw allow in on tailscale0 to any port 8899,3080,3000 proto tcp`.

**Nicer address, real certificate (optional).** `tailscale serve` puts HTTPS and the box's own name in front of the
Control Room, so you browse `https://home-base.your-tailnet.ts.net` instead of an IP and port:
```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8899
```
(Serve must be enabled once for the tailnet — the CLI prints the link. `--operator=$USER` lets you skip `sudo`
afterwards; it is a standing permission, so grant it only if you want it.)

**A single-user tailnet needs nothing else.** Every device on it is yours, so GENGHIS stays in local/admin mode —
which is the correct answer, not a compromise.

**Identity → roles on a SHARED tailnet needs one more hop.** Measured here 2026-09-22, so you don't have to guess:
`tailscale serve` authenticates the visitor and injects `Tailscale-User-Login: you@example.com` (plus
`Tailscale-User-Name` and an `X-Forwarded-For` of their tailnet address) — GENGHIS reads it, and `/whoami` over the
tailnet correctly named the caller. **But** Serve cannot add a header of your own and proxies from `127.0.0.1`, so
GENGHIS cannot tell a tailnet visitor from any local process: with `proxy_secret` empty it stays in local mode and
*everyone* gets `local_role` (we forged the header on the LAN and got admin — as designed, but not enforcement). To
actually enforce viewer/operator/admin, run a one-hop local proxy that adds the shared secret:
**Serve → Caddy (adds `X-Genghis-Proxy`) → GENGHIS**, using
[`deploy/auth/tailscale-serve-caddy.example.Caddyfile`](deploy/auth/tailscale-serve-caddy.example.Caddyfile)
and [`genghis-auth.tailscale.example.json`](deploy/auth/genghis-auth.tailscale.example.json).

> **`tailscale funnel` (public internet) injects no identity header at all** — anyone who finds the URL is
> anonymous. Use Option 2 for a public front door.

### Option 2 — Cloudflare Tunnel + Access
For people who should reach GENGHIS **without joining your network** — a colleague, a client, a demo — on your own
domain, with a login at the edge and no port open at home (`cloudflared` dials *out*).
```bash
# on the home base
curl -fsSL https://pkg.cloudflare.com/cloudflared-linux-amd64.deb -o /tmp/cf.deb && sudo dpkg -i /tmp/cf.deb
cloudflared tunnel login                       # opens a browser; pick the domain
cloudflared tunnel create genghis
cloudflared tunnel route dns genghis genghis.yourdomain.com
```
Then point the tunnel at the Control Room and add an Access policy that lists who may in. The config, the policy,
the role mapping and the anti-spoof `proxy_secret` are all in
[`deploy/auth/`](deploy/auth/) — start with its [README](deploy/auth/README.md) and
[`cloudflared-config.example.yml`](deploy/auth/cloudflared-config.example.yml). *Status:* a tested-in-pattern
blueprint — it needs your Cloudflare account and domain, so verify it in your environment.

> **Either way, `proxy_secret` is what makes it safe.** Once a proxy is in front, set it (D25): GENGHIS then trusts
> identity headers **only** on requests that also carry `X-Genghis-Proxy: <secret>`, which only your proxy sets. A
> direct hit on `:8899` that forges a header gets `default_role`, never admin. Do not set it before the front door
> is actually live, or you will demote your own LAN access.

## Joining the fleet = automatic (D33)
Every installer runs `genghis init --coord <authority>`, which **pulls** the fleet's nodes into the new box *and*
**registers the box with the authority** — it appears in the Control Room on the next heartbeat. If you skipped
`--coord`, or started a donor later on a box that was a pure client, announce it by hand:
```bash
python3 genghis_coordinator.py register --coord <authority-ip>       # py … on Windows
```
A box registers with **the** authority; every host reads from it, so one `--coord <authority>` is enough (D30).

## The watchdog (on the authority)
An independent 15-minute pass that probes every node's RPC port and every `serve`, logs one line, and feeds the
Control Room's *verified HH:MM* badge. It is the witness that notices when the serve itself has died. Keeping the saved
fleet record current is the serve's own job, every minute (D51); the watchdog writes that record only while the serve
is down. It also sets up a chat UI on this box for local models (Open WebUI's ~30 built-in tools off; they make a
local model call tools instead of answering). **`install-linux.sh --role coordinator` adds it**, and `verify` checks it;
by hand, once, on the authority:
```bash
( crontab -l 2>/dev/null | grep -v watchdog.py; echo "*/15 * * * * /usr/bin/python3 $HOME/genghis-src/poc/watchdog.py >> $HOME/genghis-src/poc/watchdog.cron.log 2>&1" ) | crontab -
```
`tail -20 poc/watchdog.log` is the morning check.

## Verify the whole fleet
```bash
python3 genghis_coordinator.py verify                  # THIS box: really in the fleet, doing what its class should? (+ bookmarks)
python3 genghis_coordinator.py verify <node-id> --bench   # from the authority: every layer on that node, at what speed
python3 genghis_coordinator.py heartbeat               # every node UP? (py … on Windows)
python3 genghis_coordinator.py decide                  # a plan over the live fleet, no run
```
`verify` is read-only and prints the fix for anything that isn't right; the installers run it as their last step.
What each check means: [`docs/agents/when-verify-fails.md`](docs/agents/when-verify-fails.md).

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `python: command not found` (Linux/Pi) | use **`python3`** (not `python`/`py`). |
| coordinator won't start after a reboot / `serve.log` says "python3 NOT FOUND" | `python3` isn't installed: `sudo apt install -y python3` — `serve.sh` re-checks every 30s and recovers on its own. |
| I ran the installer on a new box and it doesn't appear anywhere | Update the repo on that box (`git pull`, or download the ZIP again) and run `python3 genghis_coordinator.py register --coord <authority-ip>` (older builds never announced the node — D33). It must also be *serving*: a donor needs `ggml-rpc-server` listening on `:50052` (`donor-serve.sh` / the Windows `-Donor` launcher), else it registers as a client without a port. |
| a node shows **DOWN** after a reboot | its **`donor-serve.sh`** isn't running — start it + add the `@reboot` cron (§2). Confirm port `50052` is reachable. |
| **(Linux, Vulkan/Arc/AMD)** everything worked, then after a **reboot** every model fails: `llama-server exited (code 1)`, `no devices`, `--list-devices` is empty, the Control Room shows the host with no GPU | Your user is **not in the `render`/`video` groups** (`/dev/dri/renderD*` is `root:render 0660`). It worked before only through the desktop login's *temporary* ACL (the `+` in `ls -l /dev/dri`); a `serve`/`donor-serve.sh` started from cron has no such ACL. Fix once: `sudo usermod -aG render,video $USER`, then **log out and back in** (or reboot) — membership applies only to a new login. Check: `id -nG`. Current installers detect this in preflight (`install-linux.sh --preflight`). |
| a run fails / weird output on a donor | that donor built a **different llama.cpp commit** — re-run `donor-setup-*.sh` (golden rule #1). |
| GPU donor runs slow / on CPU | wrong `CUDA_ARCH`, or the CUDA preflight failed — check `rpc.log`; the script refuses to serve on CPU fallback rather than lie. |
| coordinator unreachable | `serve` down (`curl :8899/fabric`), wrong `GENGHIS_FLEET_URL`, or a firewall blocking `:8899`. |
| **Windows:** `:8899` answers on `localhost` but times out from other devices (RPC `:50052` still works) | Windows made a *per-app* `python.exe` firewall rule that only applies to the **Private** profile; the moment the NIC is reclassified **Public** (seen after an RDP reconnect) it stops matching. Fix once, in an **admin** PowerShell: `New-NetFirewallRule -DisplayName 'GENGHIS serve 8899' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8899 -Profile Any` (the installer's `-Serve` offers this when run as admin). Check the profile with `Get-NetConnectionProfile`. |
| **Windows:** installer dies at preflight with a `py.exe … NativeCommandError` / "Python install manager" message | The new Windows *Python install manager* (`py.exe` 26.x) can exist with **no interpreter** behind it and prints notices to stderr. Update the repo (`git pull`, or download the ZIP again) — the installer now looks for a real interpreter and offers `winget install Python.Python.3.12`. After any winget install, **open a new PowerShell window** before re-running. |
| **Windows:** the first chat fetches the model **from itself** / `404` from `http://127.0.0.1:8899/models` | The serve was started from a shell opened *before* the installer set `GENGHIS_COORD`, so it never saw it. Current builds resolve the repo from `fleet.json`'s `coordinator` regardless; otherwise open a new window (or reboot) so the user env var is visible, then restart serve. |
| chat says *"loading … into VRAM"* then *"llama-server exited (code 1)"* and falls back to the slow path | The warm server couldn't start. Its own output is in **`poc/resident.log`** and the reason is shown under the Control Room's model list (`/registry.json` → `resident.error`). Usual causes: the model file isn't where it says (a stale path from another machine), not enough VRAM, a Vulkan driver missing an extension. |
| chat says *"the most this model can hold on this GPU is N tokens"* | The conversation outgrew the largest context the model + GPU allow (D32). Start a new chat, or shorten this one. To force a size: `config.json` → `"resident_ctx": 8192` (it must still fit). |
| a model in the list is the wrong size / `failed to load model` on a file you know is good | A truncated copy (an interrupted download that was renamed into place by an older build). Delete the small `.gguf` in `poc/models/` and re-pull (`models pull <name>`); current builds keep an incomplete transfer as `.part` and resume it. |
| `serve.log` / `rpc-serve.log` is unreadable (CJK-looking mojibake) | An older launcher wrote the first line as UTF-16. Update the repo (`git pull` or a fresh ZIP), delete the log, restart the launcher — every line is UTF-8 now. |
| `pkill -f "genghis_coordinator.py serve"` over SSH kills **your own SSH session** | its command line matches the pattern. Use `pkill -f 'genghis_coordinator[.]py serve'`: `[.]` still matches the serve but not the shell running the command (as long as the unbracketed name appears nowhere else in that same command). (An anchored `^/usr/bin/python3 genghis_…` pattern is fragile: the serve runs as `python3 -u …`, so it matches nothing and the serve is never restarted.) The `serve.sh` loop restarts it in 5 s. |

See [`docs/COMMANDS.md`](docs/COMMANDS.md) for every command and [`USAGE.md`](USAGE.md) for day-to-day use.
