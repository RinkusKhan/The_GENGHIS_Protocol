# The GENGHIS Protocol™ — Project Charter (Restated)

> Supersedes the framing in `Project.md`. Kept as the working definition of what we are building and why.

## One-line
A dedicated coordinator that turns a heterogeneous fleet of everyday LAN devices into a single, self-balancing AI compute-and-memory pool for a client device — by planning *where each slice of a model runs* from measured device capability and link latency, and healing that plan when devices come and go.

## Why now — the memory crisis
AI demand is pulling DRAM, HBM, and VRAM into datacenters; memory is scarce and prices are up.
The conventional answer to "I need more memory to run a bigger model" is **buy more** — into a
shortage. GENGHIS answers differently: **you already own the memory — it's scattered across
idle devices in your home.** Instead of purchasing scarce new RAM, pool the RAM you already have
(a Pi, an old GPU box, a mini-PC — and, not yet measured, phones and tablets) so a modest client can run a model it could never hold
alone. Reclaiming idle, already-owned memory is the near-opposite of the buy-more arms race — and
it is the sharpest statement of what this project brings to the sector.

## What changed from the original idea
| Original framing | Restated framing |
|---|---|
| Pool a fungible quantity `P(D)` of "processing" per device | Pool **memory** (the real prize) via layer sharding; treat compute as latency-bound, not fungible |
| Coordinator reserves and accounts resources | Coordinator is a **control plane**: capability probe → partition planner → health & rebalance |
| Build the whole stack | **Stand on a proven transport** (llama.cpp RPC / exo); build the *brain* that's missing |
| "Extra CPU + memory" generic offload | **Capability-aware, latency-aware model partitioning** for a home/edge fleet |

## Thesis (why it's viable)
Splitting one model across mixed devices — pooling their RAM, streaming tiny activations down a pipeline — is already proven (exo, llama.cpp RPC, Petals). What nobody ships cleanly is a **dedicated, centralized coordinator** that:
1. **Measures** each donor's real throughput, free memory, accelerator, and link speed/latency — not just "does it fit."
2. **Plans** a throughput-weighted, latency-aware partition: starve the weak nodes, keep chatty adjacent shards on the fastest-linked boxes, push WiFi leaves to single shards.
3. **Heals** — when a donor joins or dies mid-inference, reassign its layer range without dropping the request.
4. **Governs membership** — add/remove donors through one authority (the coordinator), datacenter-scheduler-style, shrunk to a LAN.

## The new, useful thing we bring to the sector
**A capability-aware control plane for edge AI clustering.** Existing tools are peer-to-peer and partition naïvely by memory. GENGHIS is the *scheduler*: it profiles a fleet, computes an optimal placement, and keeps that placement healthy — the "brain" on top of a commodity "muscle" (RPC pipeline transport). It lets a modest client device run models larger and faster than it could alone, using hardware people already own.

## The coordinator's three pillars
The coordinator is not just a scheduler — with real local storage it becomes the fleet's **system of record**:

1. **Control plane** — device registry & discovery, capability/latency probing, the partition planner (bin-packing / pipeline-balance solver), heartbeat, and rebalance logic. Emits the `--rpc` topology.
2. **Artifact store** — one canonical model repository on the LAN. Stage each GGUF **once**; the orchestrator pulls from here over Gigabit instead of every device re-downloading multi-GB files. Keeps **multiple quantizations** per model (Q4_K_M / Q5 / Q8) so the planner can pick the quant that fits the *current* fleet's aggregate memory.
3. **Telemetry store** — every run logs `{model, quant, fleet composition, plan used, TTFT, tokens/sec, per-stage timings, failures}`. This ledger **is** the thesis: naive-vs-GENGHIS placement becomes a measured claim over many runs, not an opinion — and the measured throughput feeds back into the next partition.

> **Mechanics note (accurate to llama.cpp RPC):** the orchestrator loads the GGUF and streams each donor its layer tensors; the donor's `ggml-rpc-server -c` caches its shard to *its own* disk. So the coordinator's disk holds the **model repo + telemetry**, not the tensor cache (that lives per-donor). A Phase-3 optimization: pre-split models on the coordinator and have donors pull only their shard directly, cutting cold-start.

## Scope boundaries
- **We build:** the coordinator's three pillars above — registry/discovery, capability & latency probing, the partition planner, heartbeat/rebalance, plus the model repository and run-telemetry ledger.
- **We reuse:** the pipeline transport (llama.cpp RPC or exo) as the layer-sharding substrate.
- **Out of scope (for now):** generic non-AI distributed compute (MPI's domain); tensor parallelism (needs datacenter interconnect).

## Win conditions
- Run a model that **does not fit** on the client, using only LAN devices.
- **Faster tokens** than the client alone, via a smarter partition than default tools produce.
- **Graceful** donor add/remove without killing an in-flight request.
- Coordinator runs comfortably on a **Pi 5 (8GB)** as control plane + model repo + telemetry ledger (no heavy inference).

## Reference fleet
| Device | Role | Notes |
|---|---|---|
| Windows 11 laptop | Client / orchestrator | RPC-enabled llama.cpp built (MSVC, commit `eab8ee4`) |
| Raspberry Pi 5 (8GB, 1TB) | **Coordinator** | control plane + model repository + run-telemetry ledger |
| Linux headless server (x86) | Donor | fastest CPU donor; easiest build |
| Raspberry Pi 5 (4GB) | Donor | CPU |
| Raspberry Pi 5 (2GB) | Donor | CPU |
| Raspberry Pi 3 B | Donor | CPU, weak — held out of round 1 (ideal "starve me" test node) |
| Nvidia Tegra X1 (Jetson) | Donor | **only CUDA-capable node** — GPU backend; CC 5.3 / CUDA 10.2 caveats |
| Android phone | Donor | Phase 3 (Termux) |
| iPad | Donor | Phase 3 (no practical rpc-server) |

## Why the name fits
Genghis Khan let conquered peoples keep their own cultures and languages. GENGHIS lets each donor keep its own architecture, OS, and accelerator — ARM, x86, Metal, whatever — unified under one coordinating authority without forcing them to become the same.

## Build sequence
1. **POC — prove the substrate.** llama.cpp RPC across a subset of the fleet: one model split laptop + donors, streaming activations. Confirms memory pooling works on *this* hardware. (See `poc/README.md`.)
2. **GENGHIS coordinator — build the brain.** Capability probe → latency matrix → throughput-weighted partition plan → emit the RPC launch topology. This is the actual contribution.
3. **Healing & membership.** Heartbeat, donor add/remove, mid-inference rebalance.
