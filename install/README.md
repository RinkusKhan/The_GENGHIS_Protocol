# GENGHIS installers (D18)

**One installer per role, preflight-first.** The design principle: an installer **detects** what's present,
**reports in plain language**, **offers** to install what it can (with confirmation), and **guides** the two
things it can't automate (the VS C++ workload on Windows; the NVIDIA driver on Linux) — it never dumps an
error and quits, and it's **idempotent** (re-run any time; it fills only the gaps).

## Which one
| You have | Run |
|---|---|
| **Windows** box (launch models / host `/v1` / lend its GPU) | `install-windows.ps1` (`-Serve`, `-Donor`; CUDA **or Vulkan** auto-detected) |
| **Linux** coordinator (the always-on authority) | `install-linux.sh --role coordinator` |
| **Linux** donor (lends CPU/GPU) | `install-linux.sh --role donor --accel cpu\|cuda\|vulkan` |
| **Linux** client | `install-linux.sh --role client --coord <ip>` |
| **NVIDIA DGX Spark** | it's a Linux CUDA donor — `--role donor --accel cuda`; see [`docs/DGX_SPARK.md`](../docs/DGX_SPARK.md) |
| **Samsung TV (HEARTH surface)** | separate path — [`docs/TV_CLIENT_INSTALL.md`](../docs/TV_CLIENT_INSTALL.md) |

**Donor or host?** A donor lends its card and needs nothing else — the engine that runs a model lives on a *host* (`serve`),
and a donor computes the shard a host gives it. Run `serve` on the authority and on any box you sit at; add `--serve` /
`-Serve` to a donor **only** if you want that box to hold models warm and answer on its own (a second daily driver, a large
shop). **More serves is not more speed** — a serve adds a head, not horsepower; the default layout is the design. Full note: [INSTALL.md → Hosts and donors](../INSTALL.md#hosts-and-donors--head-and-hands-read-this-once).

## Windows
```powershell
# from the repo root:
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -PreflightOnly   # just check (no changes)
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1                  # client
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Serve -Coord <authority-ip>
# client + /v1 host + RPC donor on one box (the NUC), reusing its existing pinned-commit build:
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Serve -Donor -Coord <authority-ip> -LlamaDir C:\path\to\llama.cpp
```
It preflights Python / Git / CMake / **MSVC C++ tools** / the accelerator, offers the winget installs, **guides the VS
C++ workload** (the #1 snag — the exact box to tick), clones + builds llama.cpp at the pinned commit, runs
`genghis init`, and installs the no-admin reboot-proof launchers: `-Serve` → `/v1` + Control Room
(`poc/serve-laptop.ps1`), `-Donor` → `ggml-rpc-server` on `:50052` (`poc/rpc-serve-windows.ps1`). `-Yes` accepts
winget installs non-interactively.

**Accelerator (auto):** NVIDIA GPU + CUDA toolkit → `build-cuda`. **Any other GPU (Intel Arc / AMD) + the Vulkan SDK →
`build-vulkan`** — if the SDK is missing it offers `winget install KhronosGroup.VulkanSDK` (re-run from a new window
afterwards so `VULKAN_SDK` is visible). Neither → CPU + RPC (`build-rpc`). `-LlamaDir <path>` junctions an existing
llama.cpp checkout under `poc\llama.cpp` instead of cloning (it warns if that checkout isn't at the pinned commit).

**After it finishes (Windows):** open a **new** PowerShell window before starting serve (the `GENGHIS_COORD` user variable set
by `-Coord` isn't visible to windows that were already open). `-Serve` offers to open TCP `8899` in the firewall when run as
admin; otherwise run the one-liner it prints — the per-app `python.exe` rule Windows creates is Private-profile only and stops
matching if the network flips to Public. Logs: `poc\serve.log` (coordinator), `poc\rpc-serve.log` (donor), `poc\resident.log`
(the warm model server) — all UTF-8, readable with `Get-Content -Tail 40`.

**Dual-role (D27):** `-Serve -Donor` on one box makes its GPU the *local anchor* for its own runs **and** an RPC donor
for every other node. The installer registers it with the authority (its device and its port) once the donor listens,
and the launcher re-registers on every start — nothing to add by hand.

**A donor that lends for real:** `-Donor` builds `ggml-rpc-server`, starts it, checks the port, offers the firewall rule
for `50052` (every network profile, **local subnet only** — a fresh network is *Public*), and registers the port. A CPU
donor starts at boot as a scheduled task (no login needed); a GPU donor starts at logon. If any step fails it stops and
says why rather than ending "FINISHED".

## Linux
```bash
chmod +x install/install-linux.sh
install/install-linux.sh --role donor --accel cuda            # e.g. a CUDA donor
install/install-linux.sh --role coordinator                   # the always-on authority
```
It preflights python3 / git / build tools / the accelerator, offers `apt` installs, then hands the donor
build to the pinned `donor-setup-*.sh` (reboot-proof + self-report wired in), or for a coordinator runs
`genghis init`, starts `serve` with a `@reboot` cron and adds the 15-minute watchdog. A donor needs
`--coord <authority-ip>` to register; `--serve` also makes it a host (only if you want that box to answer on its own).
If `ufw` or `firewalld` is active it opens the donor port (`50052`) to this network only, and says so when no firewall
is on. `--preflight` checks without changing anything and lists everything the real run would change;
`--yes` accepts apt installs (run without a terminal, the installer can't ask, so it answers **no** and says so:
re-run with `--yes` once the person agrees); `--name NAME` sets the box's name in the fleet (default: its hostname).

**GPU device groups (Vulkan / Arc / AMD — anything that opens `/dev/dri`):** the preflight checks that you are in
the groups that own `/dev/dri/renderD*` / `card*` (`render`, `video` on Ubuntu) and offers `sudo usermod -aG`. It
also catches the trap that bit the NUC: the GPU *opens now* only through your desktop login's temporary ACL, so
everything works until the first reboot, when `serve`/`donor-serve.sh` start from cron with no GPU. Group
membership applies at your **next login** — the installer says so, and reminds you at the end. CUDA boxes are
exempt (`/dev/nvidia*` is world-rw).

## Notes
- **Everything is announced and confirmed** — no silent system changes. The two un-automatable steps (VS C++
  workload / NVIDIA driver) are detected and explained, not assumed.
- **The pinned llama.cpp commit is load-bearing** — every inference node must build the *same* one (RPC has
  zero cross-version tolerance). The installers check out the pin for you.
- **Both end with `verify`** and exit with its result: the install is finished when it passes.
- **Status:** both installers have been run end-to-end on clean machines by fresh AI agents with only this repo and
  [`AGENTS.md`](../AGENTS.md): a Windows 11 Home VM and an Ubuntu Server VM each became a donor and passed `verify`
  (29/29 layers on the new node). They are also the installers the reference fleet's own boxes run. A machine unlike
  those may still surface something new; they report and guide rather than assume (D18).
