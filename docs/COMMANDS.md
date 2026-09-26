# GENGHIS Coordinator — Command Reference

The coordinator is one script: [`poc/genghis_coordinator.py`](../poc/genghis_coordinator.py). It runs the
loop **probe → calibrate → plan → run → log → compare**, plus the live views and the HEARTH backend.

**Single source of truth:** the always-on **authority** (in the reference fleet, an Intel NUC) runs `serve` and owns
the authoritative `fleet.json` (it receives every node's self-report, and re-measures every node each minute, D51).
Planning commands on any other box **fetch** the live fleet from it
(`GET /fleet.json`) so *what a run uses == what the fabric shows*, then overlay their own vantage latency;
the local `poc/fleet.json` is the offline cache/fallback.

```bash
# Windows (the laptop/client):
py genghis_coordinator.py <command> [--goal fastest|balanced|fit|biggest]
# Linux (the authority, Linux hosts, the donors) — use python3, NOT `py` or `python`:
python3 genghis_coordinator.py <command> [--goal ...]
```
> **Platform note:** `py` is the **Windows** Python launcher. On **Linux / a Pi** the interpreter is
> **`python3`** (`python` and `py` are usually absent — that's the "command not found"). The authority's `serve`
> auto-starts at boot via a `@reboot` cron the installer adds (`poc/serve.sh`, a restart loop) — you rarely restart it by hand.

Every command accepts **`--goal`** (the run-type / objective knob; default `balanced`):

| goal | meaning |
|---|---|
| `fastest` | minimum highest-throughput set (tightest margin) — prunes drop-safe bottleneck nodes |
| `balanced` | fastest-leaning default; min fast set that fits, with a modest safety margin |
| `fit` | capacity-safe; fatter margin (pulls in more nodes) |
| `biggest` | pool **all** nodes for max capacity — "run a model no single node could" |

## Commands

| Command | What it does | Needs model/llama-cli? |
|---|---|---|
| `calibrate` | Measure each live donor's **solo throughput** (all layers on it alone) → writes `tokens_per_s_solo` and seeds the self-tuning `tps_ema` in `fleet.json`. | yes |
| `decide` | Print **donor scores** + the **plan** for the model at the chosen goal (run solo / split across N / won't-fit) with the per-node *why* (leave-one-out marginal). Calibrates first if needed. Does **not** run inference. | yes |
| `run` | Execute the decision **with self-healing**: heartbeat first → plan over the living → run → on a donor drop, re-plan over survivors and retry. **Settles & retries** on a *transient* capacity shortfall (a busy donor hasn't freed a prior run's shard — waits for free RAM to recover before declaring "won't fit"). Splits are **capacity-weighted** (won't-fit shards fill by free memory). The laptop's own **RTX 5090 is the local anchor** (device `CUDA0`, no RPC hop) via the CUDA+RPC build (`llama.cpp/build-cuda`); donors carry overflow. Self-tunes `tps_ema` from single-node runs (pushed to the authority). | yes |
| `sweep` | Benchmark **naive (memory-weighted) vs GENGHIS (throughput-weighted)** splits on the same fleet+model; runs both, logs to `runs.jsonl`, prints the ratio. (The "coordinator beats naive" proof.) | yes |
| `heartbeat` | One-shot **liveness probe** of every donor → `status` / `last_seen` / `latency` / rolling **reliability EMA**; prints a status table and persists to `fleet.json`. | no |
| `monitor` | Live **heartbeat watch** loop (every ~5 s) — prints drops (`DOWN`) and rejoins as they happen. The self-healing watch. | no |
| `view` | **Operator View** TUI over the heartbeat feed. On a TTY: live colored refresh; piped: one plain snapshot. States: green **IN** / amber **IDLE** / cyan **STORE** / magenta **SURF** / red **DOWN**, with throughput, free, latency, reliability bar, share %, and *why*. Pure reader (no persist). | no |
| `serve --coord HOST` | **Inference host mode (D30).** Serve `/v1` + residency + a Control Room on THIS box while reading the fleet and human config from the authority at HOST (also selected by `GENGHIS_COORD`); writes are forwarded there; no mDNS advertising; registers itself on start. | for chats |
| `serve` | **The fleet authority + HEARTH backend + web admin.** Always-on on the authority; reads the local `fleet.json` (it IS the source of truth) and serves HTTP on `:8899` (threaded, so a browser client never blocks the TV's poll). Endpoints: **`GET /`** (the **Control Room** — fleet, speed-setting → model, the Pool / warm models, formations, local resources); **`GET /admin`** (the original **web admin** — name nodes, set roles, pick the default run-type, PIN; writes via `POST /config`); **`GET /registry.json`**, **`/roles`**, **`/adapters`**, **`/formations.json`**, **`/watchdog.json`**, **`/whoami`**, **`POST /residency`** (the Control Room's own data and actions); **`GET /metrics`** (Prometheus text-exposition — fleet + per-node liveness/throughput/free/latency/reliability + repo model sizes; scrape with Prometheus/Grafana or any HTTP monitor; open at `protect: writes`, token-gated at `protect: all`); **`GET /v1/models`** + **`POST /v1/chat/completions`** (the **OpenAI-compatible API** — non-streaming + SSE streaming; the effort-routing goals appear as model names `genghis-fastest`…`genghis-biggest`, beside every local GGUF and every role (`genghis-researcher`, …), so any OpenAI client or **Open WebUI** works by pointing `base_url` here); **`GET /models`** (repo file list) / **`GET /models/<name.gguf>`** (streamed model download — the model repository; supports **HTTP Range**, so an interrupted big-model transfer **resumes** instead of restarting); **`GET /fabric`** (tab-text) / **`/fabric.json`** (live fleet; `?goal=` selectable so the TV remote can switch run-types); **`GET /hearth`** / **`/hearth.json`** (the agent-authored presence message + optional `?? question` / `= choice` for listen-back); **`GET /fleet.json`** (raw authoritative fleet — planning clients fetch this); **`GET /replies`** (the check-in history); **`POST /report`** (a node self-reports live capacity — disk/RAM/VRAM/app_version/tps_ema); **`POST /reply`** (a human's TV answer → the check-in log); **`GET /config.json`** (the human-set settings — `default_goal`, friendly `names{id}`, `roles{id}`, `hearth{}` — JSON, for the web admin) / **`GET /config`** (the same as tab-text — `GOAL` / `NAME` / `HSET` lines — for the TV, which pulls its default goal + friendly name with no JSON parser); **`POST /config`** (managing/naming nodes — whitelist merge into `config.json`, the foundation of the web admin). | no |
| `models` | **The model repository** (the authority is the always-on model library). `models` lists what the repo holds + the local cache; `models pull <name.gguf>` fetches one into the local cache (**resumable** — a dropped transfer picks up from the partial `.part`). A run **resolves the model by name and fetches-if-missing** from the repo, so a client without the file doesn't re-download 40 GB from HuggingFace. `GENGHIS_MODEL` may be a full path (the laptop keeps its own `E:\models`) **or** a bare name resolved against the repo. | no |
| `init` | **First-run setup (D22).** Generates THIS machine's own `fleet.json` + `config.json` from the local host only (hostname, cores, RAM, GPU via `nvidia-smi` / Vulkan via `llama-cli --list-devices`) — ships nothing of anyone else's topology. Idempotent: re-run to refresh this node (backs up first) while keeping donors you added. Interactive, or `--yes --node-name NAME --models-dir DIR --coord HOST --dir DIR`. | no |
| `registry` | **The model registry (D23).** `registry` lists the GGUFs in your models folder(s) with size + kind (text/VLM) and the current goal→model map; `registry map <goal> <model>` sets which model a speed setting uses; `registry default <model>` sets the fallback; `registry unmap <goal>` clears one. Names are fuzzy (a unique substring). This is the **local one-folder** model list — read-only, it does **not** touch the shared store. See [MODELS.md](MODELS.md). | no |
| `roles` | **Roles (D48, D50).** `roles` lists the roles your home offers (`genghis-<id>` in any OpenAI client) and what each would really run; `roles <id>` explains one: its model and any substitution, unmet requirements, its tool belt and which adapters are live, and its **knowledge folder**: how many files and passages are indexed and everything it could not read. `roles <id> "a question"` previews the passages the model would be given, best first. | no |
| `adapters` | **Adapters (D49).** The adapters your home declares, whether each is armed (`"enabled": true`), and whether it answers right now. | no |
| `verify` | **The finish line: is this box really in the fleet, doing what its class should?** Read-only, runs on any box (donors included). Checks: the authority answers · the box is registered and UP · its rpc-server listens · it was built from the **pinned llama.cpp commit** · its NVIDIA GPU is really lent (not the CPU build) and the driver/CUDA suit the card · its link is **wired-class** (asks the OS whether it's Wi-Fi rather than guessing from the round-trip) · this box's **serve** can read PDFs (attached to a chat, or in a role's knowledge folder; asked of the running serve, since its Python can differ from yours) · every role with a `knowledge` folder can read it. Each WARN/FAIL prints its fix. Ends with **"Bookmark these"**: this box's real LAN/Tailscale addresses that answered (Control Room, `/v1`, admin, Open WebUI, Grafana), saved to `~/genghis-addresses.txt`. `verify <node-id>` checks another node from here; `--bench` also measures placement + speed on the 1.5B the ledger uses (needs llama-cli here); `--json` for scripts and agents. **Exit 1 on any FAIL.** The installers run it as their last step. | only for `--bench` |
| `register` | **Announce THIS box to the fleet authority (D33).** `register` (uses `GENGHIS_COORD` / the coordinator in your `fleet.json`), `register --coord <host>[,<host2>]`, `register --port 50052` to force the donor port. **An additional card on this box** (an eGPU, a second GPU; D46): `register --port 50053 --id <box>-<card> --host <box>-egpu --accel cuda [--device CUDA0]` registers it as its own node, beside the box's main one (it refuses without its own `--port`, or with the box's own hostname as `--host`). `init --coord` runs it for you; use it after starting an rpc-server on a box that was a pure client, or to re-announce after a re-image. | no |
| `fleet` | **Node lifecycle (D26).** `fleet` lists active donors + the retired graveyard + eye nodes; `fleet retire <id>` moves a node to `_retired_donors` (reversible), `fleet restore <id>` brings it back, `fleet remove <id>` hard-deletes it (and its config names/roles). Posts to the coordinator (the authority) so it persists fleet-wide; **admin role** required (D25). A node merely *offline* needs none of this — the planner heals around it automatically. Also on the Control Room (a Retire button per node). | no |
| `discover` | **Zero-config coordinator discovery** (mDNS / `_genghis._tcp.local`). Finds the coordinator on the LAN and prints its URL — no hardcoded IP. `serve` advertises the service automatically; planning clients **auto-discover** it when the configured/default coordinator is unreachable and nothing is pinned. Pure stdlib (UDP 5353), no `zeroconf`/Avahi needed. | no |
| `checkins` | Review the **HEARTH check-in history**: fetches `/replies` from the authority and prints the dated question→answer record, newest first. | no |

("Needs model/llama-cli" = the command loads the GGUF and runs `llama-cli`; the others are control-plane only.)

## Environment

> Binary lookup (Windows): `poc/llama.cpp/build-cuda` → `build-vulkan` → `build-rpc` (`bin/Release/llama-cli.exe`, `llama-server.exe`). Override with `GENGHIS_LLAMA_CLI` / `GENGHIS_LLAMA_SERVER` (required on Linux hosts).

| Var | Default | Purpose |
|---|---|---|
| `GENGHIS_MODEL` | `E:\models\qwen2.5-1.5b-instruct-q4_k_m.gguf` | model GGUF to plan/run/size |
| `GENGHIS_SERVE_PORT` | `8899` | port for `serve` |
| `GENGHIS_FLEET_URL` | `http://<authority-ip>:8899/fleet.json` | the authority planning clients fetch the live fleet from (also the base for `/report`, `/replies`, `/models`). Setting it **pins** the coordinator (disables auto-discovery) |
| `GENGHIS_COORD` | *(unset)* | shorthand coordinator `host` or `host:port` (builds the fleet URL); also pins it. Leave both unset for **mDNS auto-discovery** |
| `GENGHIS_MODELS_DIR` | `<poc>/models` | local model cache; on the coordinator this dir **is** the repository it serves at `/models` |
| `GENGHIS_TOKEN` | *(unset)* | the shared PIN a client sends (as `X-Genghis-Token`) when the coordinator is locked with `protect: all`. Harmless when the coordinator is open |
| `GENGHIS_LLAMA_CLI` | *(auto)* | override the `llama-cli` binary path — a **Linux-hosted `serve`** (an always-on Linux authority hosting `/v1`) points at its own build; the laptop auto-detects `build-cuda`/`build-rpc` |
| `GENGHIS_SELF_ID` | *(hostname)* | which fleet node **is this orchestrator** — so a `local:true` anchor (e.g. the laptop's 5090) is honored only on its own host and excluded elsewhere (the self-vantage fix that lets the authority host `/v1`) |

### Config keys (`config.json`, editable live via the Control Room / `POST /config`)
| Key | Purpose |
|---|---|
| `model` | default GGUF (path or repo name) |
| `goal_models` | `{goal: model-id}` — which model each speed setting runs (D23) |
| `models_dirs` | extra folders to scan for GGUFs |
| `residency` | `false` turns the warm server off (per-request engine only) |
| `resident_ctx` | pin the warm server's context (tokens); default = chosen from model + GPU, grown on demand (D32) — per box |
| `resident_kv` | `"q8_0"` quantizes the warm server's KV cache (half the size, a bigger window on a full card) — per box |
| `formations` | named warm layouts, `{name: [{model, node \| pooled}]}` (D39) — saved from the Control Room |
| `thinking` | `{model file: false}` = that model answers without thinking unless asked |
| `default_goal`, `names`, `roles`, `hearth`, `auth` | as before (D12/D25) |

### Logs (where to look)
| File | What |
|---|---|
| `poc/serve.log` (Windows) / `~/genghis-serve.log` (Linux, `serve.sh`) | the coordinator (`serve`) — plans, model fetches (`[model] …`), residency events (`[resident] …`), tracebacks |
| `poc/resident.log` | the warm `llama-server`'s own output — the reason when it won't start |
| `poc/rpc-serve.log` (Windows) / `~/genghis/rpc.log` (Linux donors) | the donor's `ggml-rpc-server` |
| `poc/runs.jsonl`, `poc/plans/` | every run's outcome + the plan it used |
| `poc/watchdog.log` / `poc/watchdog.json` (authority) | the **watchdog**: one line per 15-min pass — donors up/down, which serves answered (and their mode/build); the latest pass is served at `/watchdog.json` and shown in the Control Room as *verified HH:MM · N/M donors* (⚠ if two passes were missed). `tail -20 poc/watchdog.log` answers "did anything die overnight?" |

### Auth (optional shared PIN)
The coordinator is **open by default** (LAN-trusting). Set a PIN — from the web admin's **🔒 PIN** button, or `POST /config {"auth":{"token":"…"}}` — to lock it. The token is held in `config.json` and **never served back** (`/config` only reports whether a PIN is set). Levels (`auth.protect`): **`off`** (never) · **`writes`** (default once a PIN is set — gates only `POST /config`, so the TV/donors/reads keep working) · **`all`** (gates every endpoint except the admin login page — the off-LAN posture). Callers pass the token as the `X-Genghis-Token` header, `Authorization: Bearer …`, or `?token=…`. To remove it: PIN button → blank, or `POST /config {"auth":{"token":""}}` with the current token. Locked yourself out? Clear `auth.token` in `config.json` on the authority and restart `serve`.

## Typical flows
```bash
py genghis_coordinator.py verify                    # after any install or change: is this box really in? + addresses
py genghis_coordinator.py verify pi5-8gb --bench    # from the authority: every layer on that node, at what speed
py genghis_coordinator.py calibrate                 # refresh solo t/s + seed EMA
py genghis_coordinator.py decide --goal fastest     # see the plan, no run
py genghis_coordinator.py run --goal fastest        # run it (self-healing)
py genghis_coordinator.py sweep                     # naive vs genghis benchmark
py genghis_coordinator.py view --goal fastest       # live operator TUI
py genghis_coordinator.py serve --goal fastest      # fleet authority + HEARTH endpoints (:8899)
py genghis_coordinator.py checkins                  # review the HEARTH check-in history
```
See [`RESULTS.md`](../poc/RESULTS.md) for the run ledger and `../STATUS.md` for the milestone hub.
