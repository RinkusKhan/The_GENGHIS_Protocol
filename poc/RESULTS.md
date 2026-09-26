<!-- GENERATED — do not edit by hand.
     Source: the project's private poc/RESULTS.md; generator: poc/dev-tools/make-public-decisions.py
     Regenerate after every run:  python3 poc/dev-tools/make-public-decisions.py -->

# GENGHIS — Results Ledger (public)

Every logged experiment and its numbers, in the order they were run on the maintainer's reference fleet. This is
the evidence behind the figures in [README.md](../README.md); the machine-readable version is
[`runs.jsonl`](runs.jsonl), with the plans in [`plans/`](plans/).

The reference lab's addresses and private names are redacted; device-class node ids (`pi5-8gb`, `gpu-5060ti`,
`tegra-x1`) stay, because they are what the numbers were measured on. Nothing else is removed, **including the
failures**: the OOMs, the estimator bugs and the runs that went slower are in here on purpose.

---

## Run #1 — First remote split inference ✅ (2026-08-29)
**Milestone: the substrate is proven.** One model, all compute on a remote donor over RPC.

- **Orchestrator:** Windows laptop (`llama-cli`, commit `eab8ee4`, MSVC 14.51)
- **Donor:** Raspberry Pi 5 8GB @ `<node-ip>:50052` (`ggml-rpc-server`), 3 ms LAN latency
- **Model:** Qwen2.5-1.5B-Instruct Q4_K_M (~1.07 GB), 28 layers
- **Placement (verified, verbose log):** **28/28 layers → RPC0 (Pi 5), 0 → CPU**
- **Donor reports:** `RPC0 : <node-ip>:50052 (8059 MiB, 8059 MiB free)`
- **Result:** coherent generation returned to the laptop.

### Perf snapshot (single donor, all layers remote)
| Placement | Prompt t/s | Gen t/s | Meaning |
|---|---|---|---|
| Laptop CPU (accidental fallback) | 525 | 60.5 | 24-core x86 — **not** what we want to measure |
| **Pi 5 via RPC (all layers)** | 14.3 | **7.8** | genuine remote compute over the network |

The 60 → 7.8 t/s drop is the *proof it's actually remote* — the Pi is doing the work, not the laptop.

### ⚠️ Key gotcha (cost us two runs — remember this)
`--device none` does **NOT** mean "use only RPC." It excludes the RPC device too, so llama.cpp
silently falls back to the local CPU. To force compute onto a donor you MUST name it:
```
--device RPC0 -ngl 99
```
Always verify placement with `-v` and grep `assigned to device RPC0` — never trust the token count alone.

### What this validates
- llama.cpp RPC transport works across our LAN on real hardware.
- The laptop→donor activation streaming path is live.
- Memory pooling is real: the laptop offloaded the entire model into the Pi's RAM.

---

## Run #2 — First two-donor pipeline split ✅ (2026-08-29)
**Milestone: multi-donor layer distribution works — AND it produced our naive-baseline proof.**

- **Donors:** Pi 5 `<node-ip>:50052` (RPC0) + Tegra X1 `<node-ip>:50052` (RPC1, ~3-4 ms)
- **Model:** Qwen2.5-1.5B-Instruct Q4_K_M, 28 layers
- **Placement (verified):** layers split across BOTH donors, **0 on laptop CPU**.
  Split ratio ≈ **2:1 (Pi5:Tegra)** — proportional to free RAM (8059 vs 3964 MiB). This IS the
  stock `naive_memory` strategy: divide by available memory, blind to throughput.
- **Answer:** coherent ("Distributed computing involves the sharing of computational resources across multiple computers…").

### The keystone finding
| Config | Gen tok/s |
|---|---|
| Pi 5 alone (Run #1) | 7.8 |
| **Pi 5 + Tegra, naive split (Run #2)** | **3.3** |

**Adding a second donor made it SLOWER (7.8 → 3.3 tok/s).** The weak Tegra A57 became the
pipeline bottleneck, plus we paid extra network hops and pipeline bubbles. This is the entire
GENGHIS thesis on real hardware: **naive memory-split can hurt.** A throughput-aware planner
should have starved or excluded the Tegra. Run #2 is our documented "naive is bad" baseline —
the number the GENGHIS coordinator must beat.

### Tegra X1 build saga (banked in donor-setup-linux.sh)
The Tegra's frozen JetPack/Ubuntu-18.04 toolchain fought us: cmake 3.10 (too old, need ≥3.14)
AND gcc 7.5 → gcc 8.4 both MISSING the ARM NEON `_x4` load intrinsics (`vld1q_s8_x4`). Fix:
pip-upgrade cmake + **gcc-10 from ppa:ubuntu-toolchain-r/test**. The script now auto-does both
on any aarch64 donor with gcc < 10.

### Next
1. **A "won't fit" model** — run something larger than any single donor's RAM to demonstrate pooling as *capacity* (needs the 1080 Ti's 11 GB to be interesting).
2. **1080 Ti donor** (tomorrow) — first GPU-accelerated split; the anchor node.
3. Begin Phase 2: the GENGHIS planner that BEATS the Run #2 naive baseline.

---

### Fleet data captured so far
| id | host | ip:port | arch | ram_free | latency | role |
|---|---|---|---|---|---|---|
| pi5-8gb | the Pi | <node-ip>:50052 | aarch64 | 8059 MiB | 3 ms | coordinator (donor for this test) |

---

## Run #3 — First GPU donor VERIFIED ✅ (2026-08-31)
**Milestone: real GPU compute over the network — after a two-session driver/toolkit saga.**
- **Donor:** `gpu-5060ti` @ <node-ip> — NVIDIA **RTX 5060 Ti (Blackwell, 16 GB VRAM)**, Ubuntu 26.04.
- **The saga (see DECISIONS D4 + fleet.json toolchain_note):** the box's original **1080 Ti was Pascal** → driver 580 dropped it (CUDA 802). Swapped in the **RTX 5060 Ti**; then 26.04 needed *three* fixes for Blackwell: **CUDA 13 toolkit** (apt's 12.4 too old for `sm_120`), **GCC-14 host** (26.04's GCC-15 too new for CUDA 13), and a **`crt/math_functions.h` patch** (add `noexcept(true)` to `rsqrt`/`rsqrtf` for glibc 2.41 — llama.cpp issue #19100, NVIDIA "wontfix").
- **Verified real GPU:** all layers on GPU, RPC0 advertised **~15848 MiB VRAM** (vs the 15477 system-RAM CPU-fallback tell), **gen 41.4 tok/s over RPC** vs **0.58 tok/s** CPU-fallback (~**70×**). Network/RPC-limited, not the card's ceiling.

## Run #4 — PHASE 2: the coordinator BEATS naive ✅ (2026-08-31)
**Milestone: the GENGHIS coordinator (`genghis_coordinator.py`) autonomously profiled the fleet,
planned a throughput-weighted split, and beat the memory-weighted (naive) split — the thesis, measured.**

Coordinator-measured **solo throughput** (calibration): GPU 5060 Ti **40.1** · Pi 5 **7.2** · Tegra **1.9** tok/s.

| Strategy | GPU share | Pi 5 | Tegra | Generation |
|---|---|---|---|---|
| naive (weight by memory) | 58% | 28% | 14% | 8.1 tok/s |
| **GENGHIS (weight by throughput)** | 82% | 15% | 4% | **14.2 tok/s** |

**→ GENGHIS = 1.75× the naive split. WIN.** The naive plan wastes 42% of the model on slow CPUs;
GENGHIS loads the GPU and starves the Tegra. Logged to `runs.jsonl`; plans in `plans/`.

### The telemetry's first lesson (a gift, not a flaw)
Both splits (8.1 / 14.2) are *below* the GPU **solo** (40.1) — because this 1.5B model **fits on the
GPU alone**, so splitting only adds per-token network round-trips. **Takeaway for the coordinator:
also decide *whether* to split** — splitting only wins when a model **won't fit** on the fastest node.
That's the next demo (won't-fit model) and the next planner smarts (split-vs-don't-split + exclude-slow-nodes).

### Next
1. **Won't-fit demo** — a model larger than 16 GB VRAM, so pooling is *required* → splitting genuinely wins.
2. **Planner v0.2** — decide whether to split; drop donors that don't help (the Tegra); model network cost, not just raw throughput.
3. **Self-healing** — donor drop/rejoin mid-run.

---

### Coordinator-measured throughput (written back to fleet.json by the coordinator itself)
| id | accel | solo tok/s |
|---|---|---|
| gpu-5060ti | cuda (Blackwell 16GB) | 40.1 |
| pi5-8gb | cpu (aarch64 ×4) | 7.2 |
| tegra-x1 | cpu (aarch64 ×4, weak) | 1.9 |

---

## Run #5 — THE WON'T-FIT DEMO ✅ (2026-09-02) — *the headline*
**A 32B model that fits on NO single device ran by pooling the whole fleet.**

- **Model:** Qwen2.5-32B-Instruct Q4_K_M — **18.9 GB weights** (won't fit the 16 GB GPU, or anything else alone).
- **v0.2 correctly decided to SPLIT** (model ~21.8 GB > fastest node) → picked GPU + Pi 5.
- **First attempt FAILED (OOM) after 344 s** — and that failure *proved the thesis twice*:
  1. **The capacity pain-point asserted itself** (the maintainer's prediction): the 85 MB margin was illusory.
  2. **Estimator bug found:** `model_mem = file × 1.15` ignored the **KV cache**, which at the default
     (huge) context is GB-scale. File size ≠ footprint.
- **Corrective run: all 3 donors (GPU + Pi 5 + Tegra) + bounded context (`-c 2048`) → RAN.** Output:
  > *"A distributed AI cluster is a network of interconnected computing nodes designed to collaboratively perform complex artificial intelligence tasks by dividing the workload."*
  (The 32B, running across the cluster, defined a distributed AI cluster. 🤯)
- **~0.4 tok/s — and that's the point:** this was never about speed. The model is **unrunnable on any
  single node**; GENGHIS made it run **at all**. Capacity, not velocity. *"The reason it runs."*

### Lessons banked → fixes applied
1. **KV cache must be in the capacity estimate** and it scales with context. Fixed `model_mem_mb()` to add
   a KV term (`~6% of weights per 4k ctx`) + a compute-buffer floor, and the coordinator now bounds the
   run to `N_CTX=4096` (`-c`). (Precise KV needs GGUF `n_layers`/`kv_dim` → v0.3.)
2. **The pain-point/objective axis is real** (D-note in coordinator-notes): tight fit ⇒ capacity dominates
   (include the Tegra); comfortable fit ⇒ speed dominates (exclude it). Same fleet, opposite plans.

### Next
1. v0.3 — context-aware capacity (couple N_CTX to fleet memory; precise KV from GGUF metadata) + the goal knob.
2. Self-healing (donor drop/rejoin mid-run).

---

## Run #6 — SELF-HEALING demonstrated ✅ (2026-09-02)
**A donor dropped and rejoined; the coordinator healed membership autonomously.**

- **Tegra disconnected** (`tmux kill-session`) → next `heartbeat`: `tegra-x1 [DOWN]`, reliability EMA
  **1.0 → 0.8**, `last_seen` frozen. `decide` immediately **re-planned over survivors** (GPU + Pi 5 only;
  Tegra gone from the scoring/plan) — heal-by-membership, no crash, no intervention.
- **Tegra restored** → `heartbeat`: `tegra-x1 [UP]`, reliability **0.8 → 0.84 (recovering)**, `last_seen`
  fresh; `decide` shows it **folded back into the pool.**
- Reliability is a rolling **reputation** (EMA), not a flag: trust decays on failure, recovers over time.

Honest scope (v1): healing is at **membership/run granularity** (detect drop → re-plan → retry over
survivors + rejoin), *not* per-token — llama.cpp RPC can't hot-swap a donor mid-generation (a custom
transport would be needed; future). New commands: `heartbeat` (one-shot), `monitor` (live watch).
This heartbeat feed is also the data source for the future dashboard (green/amber/red) and the v0.3
donor-score reliability dimension.

---

## Run #7 — v0.3 LOO-prune VALIDATED ✅ (2026-09-02) — *dropping the bottleneck = 3.5× faster*
**The 32B ran on GPU+Pi5 (Tegra benched) at 1.4 t/s — vs Run #5's 0.4 t/s on all 3 nodes.**

| Run | plan | gen t/s |
|---|---|---|
| Run #5 (pre-prune) | GPU + Pi5 + **Tegra** (all 3) | **0.4** |
| **Run #7 `fastest`/`balanced`** | **GPU + Pi5** (Tegra benched) | **1.4** — **3.5×** |
| Run #7 `biggest` (all-3 regression check) | GPU + Pi5 + Tegra (all 3) | **0.4** |

The **same 32B on the same fleet** at both poles of the objective knob — `fastest` prunes to the min fast
set (1.4 t/s); `biggest` deliberately pools all 3 for max capacity headroom (0.4 t/s, 289 s wall). The knob
makes the speed↔capacity trade-off explicit: Run #5 paid the all-3 cost *silently*; v0.3 makes it a choice.

The leave-one-out analysis proved GPU+Pi5 alone (~21,856 MB) fits the 32B (~20,468 MB @ 4k), so
`fastest`/`balanced` **auto-pruned the Tegra** — the 1.8 t/s node whose per-token drag tanked the Run #5
pipeline. Clearing it from the pipeline is the whole D6 "starve the slow node" thesis, now automatic
and measured. (The prune held even when Pi5's 146 ms wifi latency dropped its score into a tie with the
Tegra: it kept the higher-**capacity** Pi5 and dropped the Tegra — capacity is what a won't-fit split needs.)

### The bug this run caught (and the fix) — capacity-split, not throughput-split
First attempt **OOM'd in 6 s**: `failed to allocate RPC0[gpu] buffer of size 18144354304` — **18.1 GB
asked of the 16 GB card.** Cause: `run_decision` was tensor-splitting **by throughput**, which hands the
fast GPU 94.5% of the model. But `run_decision` only ever splits when the model **won't fit solo** (D6),
so every shard is **capacity-constrained** — throughput-weighting is only right for the *sweep benchmark*
on a model that fits. Fix: capacity-driven splits now weight by **free_mem_mb** (GPU 68.7% → 14.1 GB,
Pi5 31.3% → 6.4 GB; each fills to the same fraction of its headroom-derated budget, both with margin).
This is also *why Run #5 needed all 3 nodes* — spreading the load thinner masked the mis-weighting.

### Also confirmed this run
- **Precise KV (GGUF):** header reads `weights 18932 + KV 1024 @ 4096ctx (GGUF) + 512 = ~20,468 MB`.
- **Self-tuning EMA:** `calibrate` reseeded solo t/s (pi5 7.6, tegra 1.8, gpu 38.2) and donor scores now
  read from `ema`, not the one-shot `solo`.
- **Stale latency healed:** `pi5-8gb` 212 ms → **146 ms** on the live heartbeat (still wifi-high; a wired
  Pi would lift its score clear of the Tegra, but the LOO-prune gets the right plan regardless).
- Wall: 176 s including the ~19 GB stream to both donors + 32-token gen.

---

## Run #8 — the laptop 5090 as the ANCHOR node (M16/D9, 2026-09-03)

**First run with the client's own GPU wired into the fabric.** After the CUDA+RPC rebuild (Phase A),
the coordinator now registers the laptop as a **local anchor** (`laptop-5090`, `local:true`,
device `CUDA0`, latency 0) and maps it to `CUDA0` while donors map to `RPC0…`.

**Plan (`balanced`, Qwen2.5-1.5B ~1690 MB):** `RUN SOLO on laptop-5090` — the anchor holds the whole
model; all donors benched ("would bottleneck"). Exactly the intended behavior: a model that fits the
5090 pools **nobody** (fewer hops = faster).

**Result:** `gen 303.4 t/s | prompt 1745.3 t/s | 12 s` — a pure-local CUDA0 run (no `--rpc` passed at
all). ~7× the 5060ti's 41 t/s over RPC, with zero network tax. Self-tune folded the real number in:
`tps_ema 120 (placeholder) → 175` (converges toward ~300 with more runs).

**Capacity ceiling lifted:** `biggest` now pools all 4 compute nodes = **~47.7 GB** (laptop 22.5 +
5060ti 15.0 + pi5 6.9 + tegra 3.4), up from ~31 GB. A 70B-class model at q4 (~40 GB) is now in reach —
the anchor-plus-overflow case (Phase C's won't-fit half) is the next thing to validate with a big GGUF.

**What this proves:** the anchor topology is live — fits-model → 100% local on the 5090; the planner,
device mapping (`CUDA0` + `RPC0..`), local-always-up heartbeat, and self-tuning all work end to end.

---

## Run #9 — THE WON'T-FIT WIN: a 70B across the whole fleet (M16 Phase C, 2026-09-04)

**The thesis, proven.** `Llama-3.3-70B-Instruct-Q4_K_M` (~42,343 MB need = 40,551 weights + 1,280 KV
@4k + 512) — a model that **fits on no single device in the fleet** — ran by pooling **all four compute
nodes** with the laptop 5090 as the anchor.

**Plan (`biggest`):** `SPLIT across laptop-5090, gpu-5060ti, pi5-8gb, tegra-x1` (~47,706 MB pooled,
model ~42,343 MB fits). Device map `CUDA0, RPC0, RPC1, RPC2`; capacity-weighted split (~89% of each
node's derated budget → 5090 ~20 GB, 5060ti ~13 GB, Pi5 ~6 GB, Tegra ~3 GB).

**Result:** `-> RAN: gen 0.4 t/s | prompt 0.7 t/s | 917 s`. It generated. Slow — as expected: ~25 GB of
donor shards streamed over WiFi first, and every token then pipelines serially through the Tegra (the
weakest node, in the plan only because `biggest` pools everything). Speed was never the point here —
**the point is that a 70B ran at all on hardware where the single biggest piece (a 24 GB 5090) can't
hold even 60% of it.**

**What this closes:** M16 is complete end to end. The 5090 anchors (Run #8: fits-model 100% local @
303 t/s); the pool absorbs the overflow for a model no node could hold alone (this run). GENGHIS is now
what it set out to be — *run the model you couldn't, on the hardware you already own.*

**Natural next comparison (optional):** `fastest` benches the Tegra (3-node split: 5090+5060ti+Pi5, all
load-bearing) — should beat this `biggest` run on tok/s by dropping the serial Tegra hop. The knob's two
poles on the same 70B, same fleet.

---

## Run #10 — the goal-knob showdown: `fastest` vs `biggest` on the 70B (2026-09-04)

Same model (`Llama-3.3-70B-Q4_K_M`, need ~42,343 MB), same fleet, run **back-to-back** to
quantify what the run-type knob buys now that **planning uses live capacity** (fleet unification).

**`fastest` → SPLIT across laptop-5090 + gpu-5060ti + pi5-8gb (Tegra BENCHED).** All three
load-bearing; the Tegra excluded as "would bottleneck" (24% of the slowest in-plan node).
**Result: `gen 1.3 t/s | 745 s`.**

**vs Run #9 `biggest` (all 4 nodes, Tegra IN) = 0.4 t/s** → **`fastest` is ~3.25× faster.** The
knob's value, measured: for a model that fits *without* the weakest node, benching the Tegra's
**serial pipeline hop** more than triples throughput. Starving the slow node (D6) pays off big.

**The better finding — live capacity has teeth.** `biggest` ran *immediately* after `fastest`, and
**declined**: `WON'T FIT (even pooled)`. Why? The `fastest` run's ~6 GB shard on the **pi5 hadn't been
freed yet**, so its live-reported free RAM had collapsed **6327 → 1029 MB**; pooled capacity read
**41,885 MB < 42,343 need**. Pre-unification (hardcoded pi5 = 8059 MB) the planner would have assumed
the RAM was free, planned the split, and **OOM'd the pi5 mid-load**. Instead it *saw* the depleted
fleet and refused. **Measure-don't-assume prevented a real OOM in the wild** — the morning's fleet
unification doing exactly its job, unprompted.

**Operational nuance discovered:** donor free-RAM **lags run completion** (the rpc-server/OS holds the
shard briefly), so back-to-back runs see a still-busy fleet. **→ FIXED (2026-09-04): `plan_with_settle`.**
When a plan is capacity-short but the fleet's *best-case* pool (TOTAL RAM) could hold the model, the
coordinator now recognizes the shortfall as **transient** — waits ~20s for donors to free up, re-fetches
The authority, and re-measures (up to 2 settles) — instead of giving up. Only declares "won't fit" when
the model exceeds even full-free capacity. Unit-tested (busy→settle→recover→fits). This is exactly the
Run #10 back-to-back scenario, now self-healed.

**Clean `biggest` rerun (fleet recovered, pi5 back to 7.4 GB):** `SPLIT across all 4` →
`gen 0.4 t/s | 423 s`. Confirms the same-session comparison — **`fastest` 1.3 vs `biggest` 0.4 t/s
= 3.25× on generation** — and proves the earlier `CANNOT RUN` was the fleet being transiently busy,
not a bug (once the pi5 freed its shard, `biggest` planned + ran fine). *(Note: wall time isn't a clean
throughput measure here — the 2nd run streams faster because the donors have the model shards cached
from the 1st, which is why `biggest`'s wall was shorter despite slower generation. Gen t/s is the honest
metric.)*

---

## After Run #10 — where later measurements live
The numbered runs stop here because the questions changed: after 2026-09-04 the measurements were taken to
**settle a decision**, and each one is recorded in the decision it settled rather than as a run. The ones the
README quotes:

| Measurement | Where |
|---|---|
| Warm residency: a 32B from ~15 s cold to ~2.8 s warm | D23 |
| Wi-Fi donors at 163 / 306 ms round-trip; a 70B plan that reached for them streamed ~40 GB and died, and the rule that now refuses it | D41 |
| The same RTX 5060 Ti: **38 t/s over 1 GbE → 228 t/s** on its host over Thunderbolt; a 14B on it at ~50 t/s, first token 0.1 s | D46 |

## Calibration snapshot (2026-09-22)
Each node's most recent **solo** calibration (`calibrate`: all layers on that node alone), as recorded in
`fleet.json` on this date.

| Node | Class | Solo gen t/s | Model |
|---|---|---|---|
| Pi 5 8 GB | CPU, aarch64 ×4 | 7.6 on Wi-Fi · **9.7 wired** | Qwen2.5-1.5B Q4_K_M (Run #7; wired: see below) |
| Tegra X1 | CPU, aarch64 ×4 | 1.8 | Qwen2.5-1.5B Q4_K_M (Run #7) |
| Pi 4 4 GB | CPU, aarch64 ×4 | 3.4 | Qwen2.5-1.5B Q4_K_M, **wired** (see below) |

**Honesty note:** `calibrate` measures with whatever model the calibrating host has configured, and
`fleet.json` does not record which model that was. (Follow-up: `calibrate` should store the model beside the
number.) The Pi 4's earlier `fleet.json` value, 2.9 t/s, had no model attached, so it was re-measured below
under Run #7's exact conditions rather than quoted.

### Pi 4 on wired, measured (2026-09-22)
The Pi 4 moved from Wi-Fi to Ethernet (same address, now on `eth0`). Measured from the authority exactly as
`calibrate` does it: Qwen2.5-1.5B Q4_K_M, `--device RPC0 -ngl 99 -c 4096 -n 64`, all layers on the Pi.
Placement verified with `-v`: **29/29 layers assigned to RPC0** (the Pi, 3795 MiB), none on the authority.

| | |
|---|---|
| Round-trip from the authority | **0.42 ms** avg (20 pings, 0 lost) |
| Generation | **3.4 t/s** — 3 passes, identical |
| Prompt | 4.7–4.8 t/s |
| Wall | 37 s first pass (model streamed over the wire), 25–27 s after |

No Wi-Fi number exists for this Pi under the same conditions, so this run alone says nothing about what the
cable is worth. (A first reading of it — "for a single CPU node the cable barely moves generation", drawn by
comparing it with the Pi 5 on Wi-Fi — was **wrong**, and the Pi 5 before/after below is what showed it.)

### x86 CPU donor, as a virtual machine: the fresh-box install test (2026-09-23)
A fresh AI agent, given only the public repository and AGENTS.md, turned a clean Ubuntu 26.04.1 Server VM into a donor
in about 7.5 minutes (the build: 70 s on 13 cores), then benched it from the authority exactly as `calibrate` does.

| Node | What it is | Solo gen t/s | Prompt t/s | Placement |
|---|---|---|---|---|
| `ubuntu` (VM) | VirtualBox VM: 13 vCPUs of a laptop CPU, 23 GB, bridged over the laptop's Wi-Fi | **19.0** | 72.7 | 29/29 layers on the VM |

**Read it for what it is:** proof that an x86 **CPU** donor works from a clean install, and a first number for the
class. It is **not** a real x86 server (a VM sharing a laptop's CPU, reaching the LAN over Wi-Fi), so the "x86 server"
class stays unmeasured. It is twice a wired Pi 5 on the same model. The run's real output was the list of friction
points the agent hit; they were fixed before this entry was written (see CHANGELOG, 2026-09-23). The VM was retired
from the fleet afterwards, so the planner never counts it.

### Pi 5: the same node on Wi-Fi, then wired (2026-09-22)
The clean comparison: one Pi, one model, one method, measured minutes apart from the authority (Qwen2.5-1.5B
Q4_K_M, `--device RPC0 -ngl 99 -c 4096 -n 64`, three passes each). Wired = the Pi's 1 GbE port, confirmed by
the answering MAC being the Ethernet one.

| Pi 5 8 GB | Round-trip | Prompt | Generation | Wall per pass |
|---|---|---|---|---|
| Wi-Fi | 6.6 ms avg (4.5–24.7) | 16.2 t/s | 7.4–7.6 t/s | 30–31 s |
| **Wired, 1 GbE** | **0.5 ms** | **17.5 t/s** | **9.7–9.8 t/s** | **14–16 s** |
| change | 13× lower | +8 % | **+29 %** | about half |

The Wi-Fi row reproduces Run #7's 7.6 t/s three weeks later, so the baseline is stable. **What it shows:**
even one CPU donor on its own pays for Wi-Fi on every token. An RPC token is several round-trips, not one, so
~6 ms per trip is ~30 ms per token, about the whole gap between 7.5 and 9.7 t/s (133 → 103 ms per token). In a
multi-node pipeline that cost is paid at every node. On a *good* Wi-Fi moment; the Wi-Fi Pis have also
measured 146–306 ms, where it stops being a percentage and becomes the whole story (D41).

---
_Generated from the project's private results ledger on 2026-09-25._
