# GENGHIS POC — llama.cpp RPC substrate

Goal: prove the **memory-pooling-via-layer-sharding** thesis on the real fleet — run one model split across the laptop + LAN donors, streaming activations over the RPC pipeline. This is the "muscle" the GENGHIS coordinator will later plan and heal.

> **This doc is the substrate proof (the raw llama.cpp RPC layer).** The **coordinator now exists and is mature** — `genghis_coordinator.py` plans/heals/serves the fleet, hosts the **model repository**, a **web admin**, **mDNS discovery**, **auth**, and **`/metrics`**. For day-to-day use start with **[INSTALL.md](../INSTALL.md)** + **[USAGE.md](../USAGE.md)**; full reference in **[docs/COMMANDS.md](../docs/COMMANDS.md)**. The donor-setup steps below are still exactly how you bring a donor online.

## Architecture of the POC
```
  [ Laptop (Windows) ]  --RPC-->  [ Donor: Pi 5 ]  --RPC-->  [ Donor: Linux x86 server ]
   llama-cli / server              rpc-server                 rpc-server
   holds tokenizer + sampling      holds a layer range        holds a layer range
```
The client partitions the model's layers across the RPC donors automatically (by their reported free memory). No coordinator yet — that's Phase 2. Here we just confirm the transport works on *this* hardware.

## Roles for the POC
- **Client / orchestrator:** the Windows laptop. Runs `llama-cli` (built here, RPC on).
- **Donors (start with 2):** the **Linux x86 server** (fastest, easiest build) and one **Pi 5 (4GB)**. Add more once 2 work.
- **Skip for now:** Android (needs Termux, doable later), iPad (no practical `rpc-server` — it stays a Phase-3 question).

## Steps

### 1. Client build (laptop) — DONE
Built by the assistant with MSVC (VS 2026 / MSVC 14.51), pinned to llama.cpp commit
`eab8ee41f889ef7823af517e8098fb8a9b3cf601`:
```
cmake -S llama.cpp -B llama.cpp/build-rpc -DGGML_RPC=ON -DLLAMA_CURL=OFF -DGGML_NATIVE=ON
cmake --build llama.cpp/build-rpc --config Release --target llama-cli llama-server ggml-rpc-server -j
```
Binaries in `llama.cpp/build-rpc/bin/Release/`:
`llama-cli.exe`, `llama-server.exe`, and `ggml-rpc-server.exe` (the RPC server target is
**`ggml-rpc-server`**, not `rpc-server`). All donors MUST build the same pinned commit.

### 2. Donor setup (run on each Linux donor)
Copy `donor-setup-linux.sh` to the device and run it:
```bash
chmod +x donor-setup-linux.sh
./donor-setup-linux.sh
```
It installs the toolchain, builds `rpc-server` with the RPC backend, prints the device's
`ip:port`, and starts serving on `0.0.0.0:50052`. **Copy the printed `ip:port` for each donor.**

> Pi 3 B / weak nodes: it builds but is slow — hold it out of the first run, add it later to see the coordinator's value (it's exactly the node a smart partitioner should starve).

### 2b. GPU donor setup (NVIDIA box — run `donor-setup-cuda.sh` instead)
For an NVIDIA GPU donor (the fleet's remote GPU is the **RTX 5060 Ti (Blackwell) on Ubuntu 26.04**; the GTX 1080 Ti was retired — D4. Note: the laptop's own **RTX 5090 is the *compute anchor*** — see the coordinator, [D9](../DECISIONS.md); this script is for the *remote* GPU donors), use the CUDA
script — it builds llama.cpp with `-DGGML_CUDA=ON -DGGML_RPC=ON` so the shard runs **on the GPU**:
```bash
chmod +x donor-setup-cuda.sh
./donor-setup-cuda.sh
```
It installs the NVIDIA driver + CUDA toolkit if missing, checks out the pinned commit, builds
for the GPU's compute arch (`CUDA_ARCH=61` for Pascal/1080 Ti; override for other GPUs), and
serves `ggml-rpc-server -H 0.0.0.0 -p 50052 -c`. It prints a `GPU DONOR READY` block with
**`vram_MB`** — the capacity the planner uses for this donor (VRAM, not system RAM).

- **Fresh driver → reboot:** if no working driver is present, the first run installs it and exits.
  `sudo reboot`, then re-run to build and serve. A box with a working driver goes straight through.
- **A GPU dwarfs the CPU donors** — the smart partitioner loads it heavily and starves the Pis.
  GPU-loaded-vs-naive is a headline demo (and at scale, benching the weak Tegra made a 70B run 3.25× —
  Run #10). The remote GPU here is the RTX 5060 Ti; the *local* 5090 anchors first (`build-cuda`, D9).
- Other GPUs: set `CUDA_ARCH` (Blackwell 120, Ada 89, Ampere 86, Turing 75). Tegra X1 is a *different,
  harder* path (JetPack/CUDA 10.2, CC 5.3) — do the mainstream GPU first.
- **⚠️ Driver/OS for older GPUs (Pascal / GTX 10-series):** use **Ubuntu 24.04 LTS** with the
  **535/550** driver + CUDA 12.x. NVIDIA driver **580 dropped Pascal CUDA**, and Ubuntu 25.10/26.04
  ship *only* 580 — there, `nvidia-smi` shows the card but `cudaGetDeviceCount` fails with error 802.
  The script's CUDA preflight will catch this and refuse to serve rather than silently run on CPU.

### 3. Get a small model (laptop)
Start tiny so the pipeline is easy to verify, then scale up to something that does NOT fit on one donor (the real win). Suggested first model: a ~1.5B instruct model, Q4_K_M GGUF (~1 GB). Place it e.g. at `E:\models\`.

### 4. Run split inference (laptop)
**Use the coordinator** (it plans placement, fills the 5090 anchor first, and heals on drops):
```powershell
cd E:\The_GENGHIS_Protocol\poc
$env:GENGHIS_MODEL = "E:\models\<model>.gguf"     # or a bare name from the Pi model repo
py genghis_coordinator.py decide --goal fastest    # show the plan (which nodes, why)
py genghis_coordinator.py run    --goal biggest     # run it across the fleet
```
`decide`/`run` name the RPC devices for you (`CUDA0,RPC0,…`) and log to `runs.jsonl`. **That's the
substrate proven — now planned + healed.** *(The old `run-client.ps1` manual launcher is superseded; the
coordinator is the supported path.)*

> ⚠️ **Verify placement, don't trust the token count.** `--device none` does NOT mean "RPC only" —
> it excludes RPC too and silently falls back to the laptop CPU (fast, but not what you're testing).
> Always name the RPC devices, and check the log for `assigned to device RPC0`. See `RESULTS.md`.

## What "success" looks like
- Each donor logs an accepted RPC connection and a loaded slice of tensors.
- The laptop generates coherent tokens with `--rpc` pointing at the donors.
- Bonus proof: load a model **larger than any single donor's RAM** and watch it still run — that's memory pooling, the whole point.

## Troubleshooting
- **Donor unreachable:** firewall. Open TCP 50052 on the donor (`sudo ufw allow 50052/tcp`) and confirm laptop and donor share a subnet.
- **Version mismatch:** client and every donor must be built from the **same llama.cpp commit** (RPC has no cross-version compatibility guarantee). Re-pull and rebuild if in doubt.
- **Slow / stalls:** a weak node holding too many layers throttles the pipeline. Exactly the problem Phase 2 (the GENGHIS coordinator) solves.

## Next (Phase 2)
Once 2+ donors run: build the coordinator — probe each donor's `{ram, cores, arch, link latency}`,
compute a throughput-weighted layer assignment, and emit the `--rpc` topology automatically
instead of hand-listing hosts.
