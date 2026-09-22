# Onboarding an NVIDIA DGX Spark as a GENGHIS node

> **Reference notes from published specs** (no Spark on hand to test against yet). Treat the arch/driver/
> memory numbers as *verify-on-the-unit* — run the checks in §2 first and adjust. GENGHIS itself needs no
> changes; the Spark joins as a standard Linux/CUDA node.

## Why a Spark is a standout node
The **DGX Spark** (GB10 Grace-Blackwell) pairs a 20-core Arm CPU with a Blackwell GPU over **~128 GB of
coherent LPDDR5x unified memory** — the CPU and GPU share one pool. For GENGHIS that means:
- **A single donor that can hold a huge model.** ~120 GB of usable "VRAM" (a slice of the unified pool) makes
  it the best *won't-fit* donor in a home/office fleet — a 70B fits with room to spare; with `biggest` it lets
  the pool reach into 120B+ territory.
- **The ideal always-on inference host** — better than the NUC for the `serve` + **resident** big-model role
  (D23): keep a 70B warm on one box.
- **A fast NIC** (ConnectX, up to 200 Gb/s on the linkable port) — RPC weight-streaming, the usual fleet
  bottleneck, becomes a non-issue. (Two Sparks linked via ConnectX are, to GENGHIS, just **two donors on the
  LAN** — no special handling; NVIDIA's 2-unit scaling is orthogonal.)
- Runs **NVIDIA DGX OS** (Ubuntu-based, **aarch64**) — so it's the standard Linux-CUDA donor path, just on Arm.

## Roles it can play
| Role | Fit |
|---|---|
| **Big compute donor** (RPC) | ⭐ the obvious one — lends ~120 GB to pooled runs |
| **Always-on inference host** (`serve` + a resident big model) | ⭐ great — the D23 residency keystone on real hardware |
| **Coordinator** (the authority + model repo) | possible, but a Pi already does this fine; spend the Spark on compute |

---

## 1 · Verify the unit first
```bash
uname -m                 # expect aarch64
nvidia-smi               # GPU name (Blackwell), driver, and the memory figure
nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv
free -h                  # total system memory (unified — overlaps the GPU pool)
nvcc --version           # CUDA toolkit (DGX OS ships a matching one)
```
Note the **compute capability** (Blackwell is `sm_120`/`sm_121` family — use exactly what `compute_cap`
reports) and the **memory total**. On a **unified-memory** device the GPU "VRAM" and system RAM are the *same
pool* — don't double-count it (same honest-budgeting rule as the laptop's Arc iGPU): leave headroom for the OS
+ KV cache.

## 2 · Onboard as a donor (the standard CUDA path)
```bash
mkdir -p ~/genghis && cd ~/genghis
# copy poc/donor-setup-cuda.sh, poc/donor-serve.sh, poc/donor-report.sh here
chmod +x donor-setup-cuda.sh donor-serve.sh donor-report.sh

CUDA_ARCH=121 ./donor-setup-cuda.sh      # use the compute_cap from §1 (e.g. 120 or 121);
                                         # builds ggml-rpc-server -DGGML_CUDA=ON -DGGML_RPC=ON at the PINNED commit
```
Then make it reboot-proof + self-reporting (same as any CUDA donor — see [INSTALL.md](../INSTALL.md) §3):
```bash
( crontab -l 2>/dev/null | grep -v donor-serve.sh;  echo "@reboot $HOME/genghis/donor-serve.sh  >> $HOME/genghis/rpc.log 2>&1" ) | crontab -
( crontab -l 2>/dev/null | grep -v donor-report.sh; echo "@reboot $HOME/genghis/donor-report.sh dgx-spark >> $HOME/genghis/report.log 2>&1" ) | crontab -
export GENGHIS_COORD=<coordinator-ip>:8899
nohup ~/genghis/donor-serve.sh  >> ~/genghis/rpc.log    2>&1 &
nohup ~/genghis/donor-report.sh dgx-spark >> ~/genghis/report.log 2>&1 &
```
**Golden rule (D-level):** every inference node builds the **same pinned llama.cpp commit** — RPC has zero
cross-version tolerance. `donor-setup-cuda.sh` checks out the pin for you; don't hand-update just this box.

**Verify** from the coordinator/a client: `python3 genghis_coordinator.py heartbeat` shows `dgx-spark` **UP**,
and its self-reported free memory should read ~110–120 GB.

## 3 · (Optional) make it the always-on inference host
Instead of (or as well as) the RPC donor, run `serve` on the Spark and keep a big model **resident** (D23):
```bash
# build llama-cli + llama-server (CUDA) at the pinned commit, then:
export GENGHIS_LLAMA_CLI=~/genghis/llama.cpp/build-cuda/bin/llama-cli
export GENGHIS_SELF_ID=dgx-spark GENGHIS_MODEL=Llama-3.3-70B-Instruct-Q4_K_M.gguf   # bare name -> fetch from the repo
python3 genghis_coordinator.py registry default Llama-3.3-70B-Instruct-Q4_K_M.gguf
python3 genghis_coordinator.py serve --goal fastest        # /v1 + Control Room on :8899
```
Residency keeps the 70B warm in the unified pool — a genuinely pleasant always-on `/v1`.

## 4 · Register / retire (lifecycle)
- If the Spark is also a **client** (you launch from it), `genghis init` writes its own fleet entry.
- As a pure donor, `donor-report.sh` self-registers its live capacity to the authority; give it a friendly
  name + role in the Control Room `/admin`.
- Swapping or removing it later: `genghis fleet retire dgx-spark` (reversible) / `fleet remove dgx-spark`
  ([D26](../DECISIONS.md)).

## Gotchas checklist
- [ ] **aarch64** — use `python3`; build llama.cpp on the box (no cross-arch binaries).
- [ ] **Unified memory** — the ~128 GB is one shared pool; report it honestly, leave OS + KV headroom.
- [ ] **CUDA arch** — set `CUDA_ARCH` to the real `compute_cap` (Blackwell 120/121); DGX OS ships the driver+CUDA.
- [ ] **Pinned commit** — build at the fleet's pinned llama.cpp commit (RPC compat).
- [ ] **Fast NIC bonus** — if it's wired on ConnectX/10GbE, weight-streaming to it is effectively free.
- [ ] **Two Sparks** = two ordinary donors to GENGHIS (NVIDIA's ConnectX 2-unit link is separate).
