# Landscape — where GENGHIS sits, and what it is not

_Written 2026-09-13 so the question "how is this different from exo?" is answered before it's asked.
Numbers are GitHub as of that date. Update when we go public._

## The one-line answer
Everyone in this table pools devices to run a model. **GENGHIS is the only one that treats the pool as a
home *task-fabric*** — an always-on home base that runs ambient agents (camera → VLM → notify), a TV as the
fabric's face (HEARTH — a client that spans our products, not a GENGHIS feature), effort-routing exposed as model names, and a `/v1` that always answers
with what it is doing. The memory-pooling thesis is shared ground; the second act ([D20](../DECISIONS.md)) is
not.

## The field

| Project | Since | Stars | Engine | Shape | What it is for |
|---|---|---|---|---|---|
| [exo](https://github.com/exo-explore/exo) | Jun 2024 | 47k | own (MLX / tinygrad) | p2p, no coordinator | "Run frontier AI locally" — pool Macs/phones; the famous one; Apple-leaning |
| [distributed-llama](https://github.com/b4rtaz/distributed-llama) | Dec 2023 | 3k | own | root + workers, tensor-parallel | *speed*: more devices ⇒ faster tokens on a model that already fits |
| [GPUStack](https://github.com/gpustack/gpustack) | May 2024 | 5.7k | vLLM / SGLang / llama-box | cluster manager | datacenter-shaped GPU serving, SSH-able GPU instances |
| [Prima.cpp](https://arxiv.org/abs/2504.08791) | Apr 2025 | paper | llama.cpp fork | scheduler ("Halda") | *academic*: 30–70B on 4 heterogeneous home devices, Wi-Fi, low RAM — the closest **idea** to our planner |
| [llama.cpp RPC](https://github.com/ggml-org/llama.cpp/tree/master/tools/rpc) | May 2024 | — | llama.cpp | `--rpc host:port,…` | the **transport** GENGHIS (and CLUSTER, SharedLLM) build on — no planning, no fleet, no healing |
| [CLUSTER](https://github.com/ficusai/CLUSTER) (ficusai) | **Sep 10 2026** | 0 | llama.cpp RPC | root coordinator + workers | a *launcher*: mDNS/UDP discovery, OpenAI API, dashboard, Android via Termux. v0.1, 3 days old |
| SharedLLM (MHASK) | Jun 2026 | 0 | llama.cpp RPC | desktop app, source private (AGPL) | discovery, free-memory tracking, layer split, re-plan on drop, HMAC proxy |
| [Kalavai](https://github.com/kalavai-net/kalavai-client) | Aug 2023 | 221 | k8s-ish | pooled spare GPUs | aggregate spare GPU capacity; more cloud-cooperative than home |
| Petals / Cake / others | 2022–24 | — | own | swarm | internet-scale or mobile-first swarms; different problem |

## What GENGHIS has that none of them do (as of the table date)

| Capability | GENGHIS | Nearest |
|---|---|---|
| **Capability-aware planning with an objective** — `fastest / balanced / fit / biggest`, precise KV from GGUF, true leave-one-out pruning that *drives* selection (D6, M10b) | ✅ | Prima.cpp's scheduler (academic, no objective knob); llama.cpp's default is "split by free memory" |
| **Self-tuning throughput** (EMA from real runs) + self-healing (heartbeat, heal-by-membership, settle-&-retry) | ✅ | SharedLLM re-plans on drop |
| **Local anchor + dual-role nodes** — a box is its own anchor *and* a donor for others; local wins when the model fits (D9/D27) | ✅ | — |
| **Model registry + residency** — every GGUF selectable in `/v1`, goal→model map, warm `llama-server`, context sized from model + GPU and grown on demand (D23/D32) | ✅ | GPUStack (datacenter) |
| **`/v1` that is never silent** — first byte at 0 s, status + the model's reasoning through `reasoning_content`, readable 503s for load / missing engine (D28/D31) | ✅ | — |
| **Fleet authority + model library + first-run `init`** — one `fleet.json`, mDNS, fetch-if-missing models, nothing of ours ships (D10/D22/D30) | ✅ | CLUSTER has discovery |
| **Control Room + Prometheus + Grafana + RBAC + Tailscale** (D24/D25/D29) | ✅ | CLUSTER has a dashboard; GPUStack has auth |
| **Per-role preflight installers** — detect → guide → build at a pinned commit → register → reboot-proof (D18) | ✅ | — |
| **Node lifecycle** — retire/restore/remove, cattle not pets (D26) | ✅ | — |
| **HEARTH** — a Samsung TV as the fabric's *face*: presence, listen-back, remote-answered questions (HEARTH is its own client and will front several of our products; GENGHIS is its first backend) | ✅ | — |
| **The second act (D20)** — always-on home base running placed small task-agents: eye → VLM → notify; voice (PersonaPlex); work/home mode switch | 🚧 (eye node provisioned; pilot next) | — |

## What they have that we don't (be honest)
- **exo:** tens of thousands of users, Apple silicon polish, p2p with no single authority, a brand. If someone
  wants "my three Macs as one model," exo wins today.
- **distributed-llama:** raw *speed* — tensor parallelism makes a model that already fits go faster. GENGHIS
  splits only for *capacity* (D6) — a deliberate choice, and a real difference in goal.
- **GPUStack:** multi-engine (vLLM/SGLang), production serving features, a team.
- **Prima.cpp:** a published scheduler with measured results (674 ms/token for a 70B on four home devices).
  Worth reading against our planner; our LOO pruning and objective knob are different, not obviously better.
- **CLUSTER:** Android via Termux as a *worker*. Our Phase-3 "old phones in a drawer" is still aspirational.

## Positioning, in one paragraph
GENGHIS is not "a faster exo." It is **the home fabric**: the memory you already own, pooled to run models no
single box can hold — *and* an always-on base that keeps small agents working for the household when the big
model isn't needed. Lead with the second act; the pooling is table stakes now, and we should say so.

_Scope note (2026-09-13): health / care use cases are deliberately **not** positioned as GENGHIS features. They are a
separate product with its own backend; HEARTH is the shared household client that will
front GENGHIS, that product, and others. GENGHIS's own second act is the general home task-fabric._

## Still open
- Read Prima.cpp's paper against `plan_v3` and note where we differ (and where they're right).
