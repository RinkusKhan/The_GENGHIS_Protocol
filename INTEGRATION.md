# Integrating with GENGHIS

**How to connect your own device, app, or surface to a GENGHIS fleet.**

GENGHIS pools memory + compute across unlike home/LAN devices so people can run private models
they couldn't hold on any one machine. This guide is for anyone extending it — a new **surface**
(a smart TV, a wall panel, a voice puck), a **client app**, or a **compute donor**.

> **Design principle (see [DECISIONS.md](DECISIONS.md) D17): speak the standards the world already
> speaks; invent only our unique layer.** Inference → the **OpenAI API**; monitoring → **Prometheus**;
> discovery → **mDNS/DNS-SD**; auth → **Bearer token**. Our distinctive part — the pool, the fabric,
> effort-routing — is exposed on **native** endpoints behind those standards. So most integrations
> "just work" with tools you already have.

Legend: ✅ **works today** · 🔜 **roadmap** (documented here so the direction is firm, not vaporware).

---

## Errors a client must expect (D28)
`POST /v1/chat/completions` always answers. Two non-200 cases carry an OpenAI-style body your UI should display:
- `503` `{"error": {"type": "model_loading", "message": "GENGHIS is fetching <model> … (37%). Try again in a moment."}}` + `Retry-After: 10` — the model is being pulled from the library in the background. Poll `/registry.json` → `fetch.pct` if you want a progress bar.
- `503` `{"error": {"type": "engine_missing", …}}` — this host has no `llama-cli` build yet (run the installer).

## The one thing to know first: three roles

You integrate as one (or more) of these. They differ a lot in effort:

| Role | You want to… | Effort | Path |
|---|---|---|---|
| **Surface / endpoint** | present the fleet + interact with a human | **low** — pure HTTP | native endpoints (HEARTH is the reference) |
| **Client / app** | *use* the pool to run a model | **low–medium** | OpenAI `/v1` ✅ · the CLI ✅ |
| **Donor** | *contribute* compute/memory | **medium** — build-lockstep | `ggml-rpc-server` + `POST /report` |

The reference implementation of a **surface** already exists and runs on a retail Samsung TV:
**HEARTH** *(coming soon)*. Read its client for a working example of everything below.

---

## Step 0 — Find the coordinator (no hardcoded IP)

The coordinator advertises itself on the LAN over **mDNS/DNS-SD** as **`_genghis._tcp.local`** (✅).
A fresh install should discover it, not ship an IP — every household has a different coordinator.

- **Browse** for `_genghis._tcp` (e.g. Tizen `Tizen.Network.Nsd.DnssdBrowser`, Avahi/`dns-sd`,
  Python `zeroconf`, Android NSD). The SRV/A records give you host + port (default **`:8899`**).
- Python planning clients auto-discover when nothing is pinned; `GENGHIS_COORD` / `GENGHIS_FLEET_URL`
  pin it explicitly. Pure-stdlib responder/querier lives in [`poc/genghis_mdns.py`](poc/genghis_mdns.py).
- Precedence pattern HEARTH uses: **user-set address > last-discovered cache > compiled default.**

Everything else in this guide is plain HTTP against `http://<coordinator>:8899`.

---

## Step 1 — Auth

Auth is **open by default** (LAN-trusting) and lockable with a shared token (see DECISIONS.md D14).
When enabled, send the token any of three ways:

```
X-Genghis-Token: <token>
Authorization: Bearer <token>
?token=<token>            # query param — for surfaces that can't set headers
```

Levels: `off` · `writes` (gates `POST /config` only) · `all` (gates everything but the admin login).
The token is **never served back** (`/config` redacts it). Python clients read `GENGHIS_TOKEN` from env.
🔜 A public, multi-developer deployment will want **scoped/capability keys** rather than one shared secret.

---

## Role A — Build a surface (the HEARTH pattern)  ✅

A surface reads fleet state, renders it, optionally pulls its config, and posts events back. All HTTP,
any language, any platform. This is the **easy, open** path.

**Read fleet / fabric state**
| Endpoint | Returns |
|---|---|
| `GET /fabric` | tab-delimited fabric lines (no JSON parser needed — for constrained clients) |
| `GET /fabric.json` | same, JSON; accepts `?goal=fastest\|balanced\|fit\|biggest` |
| `GET /fleet.json` | the raw authoritative fleet (measured capacity/throughput per node) |
| `GET /hearth` / `GET /hearth.json` | the agent-authored human message (presence) |
| `GET /replies` | HEARTH check-in history (the healthcare longitudinal record — how someone is doing, over time) |

**Pull your config from the coordinator** (so a Store build needs no baked-in settings)
| Endpoint | Returns |
|---|---|
| `GET /config` | tab-text (`GOAL` / `NAME` / `HSET` lines) — zero JSON dependency |
| `GET /config.json` | same, JSON (for richer clients / the web admin) |

Use `names[<your-node-id>]` for this surface's friendly name, `default_goal` for the initial run-type.
Re-pull periodically (HEARTH does ~60 s) so web-admin edits propagate without a restart.

**Report what you are / send events back**
```
POST /report   {"id":"<node-id>", "<field>":<value>, ...}   # capability + telemetry self-report
POST /reply    {...}                                        # a human interaction / answer / event
```
`POST /report` merges only whitelisted fields (`REPORTABLE` in the coordinator) — capacity, version,
role, and probe results. This is also how a **perception node** (e.g. a camera + NPU) raises an event
that can escalate to a bigger model in the pool.

> Worked example: the HEARTH Tizen client (`client/Hearth.cs`) — NSD discovery → `GET /config` →
> render `/fabric` → `POST /report` / `POST /reply`. Copy its shape in your language.

---

## Role B — Use the pool to run a model (submit AI jobs)

We offer the **triad the self-hosted-LLM industry has standardized on** (DECISIONS.md D17):

1. **OpenAI-compatible API — `/v1`** ✅ *live (streaming + non-streaming)* *(the spine; the recommended integration point)*
   - `GET /v1/models` ✅ and `POST /v1/chat/completions` ✅ — both **non-streaming** and **SSE streaming** (`"stream": true` → `chat.completion.chunk` events + `[DONE]`). Verified end-to-end on the pool (`laptop-5090`). Remaining refinements 🔜: real token-usage counts + proper per-model chat templates.
   - **Any** OpenAI client works unchanged — just set `base_url` to the coordinator:
     ```python
     from openai import OpenAI
     client = OpenAI(base_url="http://<coordinator>:8899/v1", api_key="<token-or-blank>")
     client.chat.completions.create(model="genghis-biggest",
         messages=[{"role":"user","content":"hello"}])
     ```
   - **Effort-routing is exposed as model names**: `GET /v1/models` returns `genghis-fastest`,
     `genghis-balanced`, `genghis-fit`, `genghis-biggest`. Picking the "model" picks the routing goal
     across the pool — standard interface, our special sauce, no custom client code.
2. **Web chat for humans** ✅ — **adopted [Open WebUI](https://github.com/open-webui/open-webui)**, not
   built: it runs in a container pointed at `/v1` (`OPENAI_API_BASE_URL=http://<coordinator>:8899/v1`),
   the four `genghis-*` goals auto-list as models, and a chat round-trips on the pool. Optional web search
   (DuckDuckGo, no key) augments answers with live results. A tiny built-in zero-install chat page (same
   featherweight approach as the web admin) is a 🔜 fallback for Pi-only setups.
3. **CLI** ✅ *(today; power users, scripting, headless, CI)*
   ```powershell
   $env:GENGHIS_MODEL = "Llama-3.3-70B-Instruct-Q4_K_M.gguf"   # bare name = fetch-if-missing from the Pi
   py poc/genghis_coordinator.py decide --goal biggest          # show the plan
   py poc/genghis_coordinator.py run    --goal biggest          # run across the fleet
   ```

**Async / batch** 🔜 — for "submit a job, walk away, get results/alerts later" (the unattended
home-watch case), a small native `POST /jobs` → job id → poll/notify layer will live *alongside*
`/v1`, not replace it.

**Models repository** ✅
| Endpoint | Returns |
|---|---|
| `GET /models` | JSON list of staged `.gguf` files (name + size) |
| `GET /models/<name.gguf>` | streamed download, **HTTP Range / resumable** (a dropped 42 GB transfer resumes) |

---

## What a streaming client sees (D31)
`stream: true` returns SSE `chat.completion.chunk`s immediately. Before the answer, GENGHIS sends its status as
`delta.reasoning_content` lines (`GENGHIS · fastest · <model> · solo on <node>`, `loading … into VRAM`, `… (9 s)` ticks
every 3 s, `processing prompt`, `streaming model shards …`), then the model's own reasoning (if it has any) as
`reasoning_content`, then the answer as `delta.content`. Render `reasoning_content` as a collapsible "thinking" area
(Open WebUI does) and your UI never looks frozen; ignore it and you get plain OpenAI behaviour. One chunk carries a
`genghis` object (`nodes`, `goal`, `model_file`, `resident`) — which nodes ran it.

## Role C — Donate compute/memory  ✅ (with one honest caveat)

A donor runs the llama.cpp RPC server so the planner can place a shard on it.

1. Build llama.cpp at **the exact pinned commit** the fleet uses (CPU, or CUDA/Metal/Vulkan if you have
   a GPU backend). See [INSTALL.md](INSTALL.md) and [`poc/donor-serve.sh`](poc/donor-serve.sh).
2. Run `ggml-rpc-server -H 0.0.0.0 -p 50052` (reboot-proof loop in `donor-serve.sh`), reachable on the LAN.
3. Self-report so the planner knows your live capacity:
   ```
   POST /report {"id":"my-box","ram_free_mb":..,"vram_total_mb":..,"app_version":".."}
   ```
   ([`poc/donor-report.sh`](poc/donor-report.sh) does this from `/proc/meminfo` + `nvidia-smi`.)

> ⚠️ **The real constraint, stated plainly:** RPC has **no cross-version compatibility** — every node
> must run the **same pinned llama.cpp commit**. So a donor needs a Linux-style userland and a matching
> build; "any device can donate" is true only within that lockstep. (It's why an armv7 retail TV can
> present a surface but can't donate compute.) Specialized accelerators that *aren't* llama.cpp — e.g. a
> Hailo/Coral vision NPU — don't join the LLM pool; they're **perception nodes** (Role A) that report
> events, not donors.

---

## Monitoring (for businesses using this internally)  ✅

We chose the **industry default on purpose**, so your existing stack integrates with no glue:

- `GET /metrics` ✅ — **Prometheus** text exposition: `build_info`, nodes up/total, per-node
  up/reliability/latency/throughput/free-memory (labeled by id/name/role), repo model count + bytes.
  Scrape it with **Prometheus, Grafana Cloud, Datadog, New Relic** — anything that speaks Prometheus.
- Turnkey bundle in [`monitoring/`](monitoring/): `prometheus.yml`, an importable Grafana dashboard,
  one-command `docker compose up -d`, and a toggleable "donor down" alert.
- 🔜 `GET /healthz` / `GET /readyz` (liveness/readiness), optional structured JSON logs for log
  aggregators, and later **OpenTelemetry traces** (a pooled run naturally spans nodes — ideal for tracing).

---

## Endpoint reference (current)

| Method | Path | Purpose | Auth level gated by |
|---|---|---|---|
| GET | `/` , `/admin` | web admin (manage/name nodes, set goal + PIN) | `all` |
| GET | `/fabric` , `/fabric.json` | fabric state (`?goal=`) | `all` |
| GET | `/hearth` , `/hearth.json` | presence message | `all` |
| GET | `/fleet.json` | authoritative measured fleet | `all` |
| GET | `/config` , `/config.json` | system config (token redacted) | `all` |
| GET | `/replies` | check-in history | `all` |
| GET | `/models` , `/models/<name.gguf>` | model repo (list / Range download) | `all` |
| GET | `/metrics` | Prometheus metrics | `all` |
| GET | `/v1/models` | OpenAI model list (effort-routing goals as model names) | `all` |
| POST | `/v1/chat/completions` | OpenAI chat completion (streaming + non-streaming) | `all` |
| POST | `/report` | node capability/telemetry self-report | `all` |
| POST | `/reply` | human interaction / event | `all` |
| POST | `/config` | update system config | `writes` + `all` |
| GET | `/registry.json` | models on this host (`size_mb`, **`warm_mb`** = what a card must find), goal→model map, the warm pool (`resident.pool`, `resident.loading`) | `all` |
| POST | `/residency` | `{"action":"load"\|"unload","model":…,"node":…,"pooled":true,"why":…}` — warm / unload on any host (forwarded) | `operator` |
| GET | `/plan.json?model=…&node=…` or `&pooled=1` | the planner's dry run for a placement (the Control Room's ghost): per-node MB, `fits`, `why`, `reach`, `hint`, `warn` | `all` |
| GET | `/loading.json?node=…` | live warm-load progress on a host (measured bytes / rate / ETA on Linux; cache loads say so) | `all` |
| GET | `/formations.json` | named warm layouts, which one is current, the live layout, an apply in progress | `all` |
| POST | `/formations` | `{"action":"save"\|"delete"\|"plan"\|"apply","name":…}` — snapshot / remove / preview / switch layouts | `operator` |
| POST | `/fleet` | `{"action":"lend-off"\|"lend-on"\|"retire"\|"restore"\|"remove","id":…}` — **lend-off also reclaims the card** (pooled shards other hosts keep there are unloaded; the note says which) | `admin` |
| GET | `/watchdog.json` | the last 15-min verification pass (`nodes_up`, `down`, `pinned`, `gpu_problems`) | `all` |

Most GETs send `Access-Control-Allow-Origin: *` for browser surfaces. Discovery is mDNS `_genghis._tcp` (UDP 5353).

---

## Versioning & compatibility

- The coordinator stamps `COORD_VERSION`; each node reports its `app_version`. 🔜 We will declare a
  **contract version** on `/config` (or a `GET /info`) with a documented compatibility policy so
  third-party endpoints can check they're talking to a supported coordinator.
- **Surfaces/clients** ride versioned HTTP contracts — safe across coordinator updates.
- **Donors** are bound to the pinned llama.cpp commit — coordinate build upgrades fleet-wide.

---

## TL;DR by audience
- **Fancy AI TV / wall panel / voice puck** → a **surface** (Role A). Use your local AI for light,
  always-on work; escalate to the pool via `/v1` for the heavy models you can't hold. HEARTH is your example.
- **App / script / ComfyUI LLM nodes / LangChain** → the **OpenAI `/v1`** endpoint (Role B).
- **Spare PC / SBC / NAS** → a **donor** (Role C), same pinned build, self-report.
- **Ops team** → scrape `/metrics` with the Prometheus-compatible tool you already run.
