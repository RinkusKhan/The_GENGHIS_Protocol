# GPUs: getting a card really used

A GPU that is installed but not really used is the most common silent failure: the box joins, text comes out, and
every layer is running on the CPU at a fraction of the speed. Nothing complains. So for every GPU, the job ends with
`verify` on the box (its `gpu` check) and `verify <node-id> --bench` from the authority (placement and speed).
Read [`AGENTS.md`](../../AGENTS.md) first.

## NVIDIA (CUDA)

**Supported: RTX 20 / GTX 16-series (Turing) and newer** (D52): everything CUDA 13 builds for. For every supported card the toolkit rule is the same:
**CUDA 13.2 or newer, from NVIDIA's repository.** Don't install whatever the package manager offers: Ubuntu's own
`nvidia-cuda-toolkit` is too old for an RTX 50-series card, and CUDA 13.0/13.1 fail against glibc 2.43 (Ubuntu 26.04).

| Generation | Examples | Compute / `CUDA_ARCH` | Supported | Tested here |
|---|---|---|---|---|
| Blackwell | RTX 50-series | 12.0 / `120` | **yes** | **yes:** RTX 5090 Laptop (Windows, CUDA 13.3), RTX 5060 Ti (Ubuntu 26.04, CUDA 13.2) |
| Ada | RTX 40-series | 8.9 / `89` | **yes** | not yet |
| Ampere | RTX 30-series, A100 / A-series | 8.6 / `86` (A100: 8.0 / `80`) | **yes** | not yet |
| Turing | RTX 20, GTX 16 | 7.5 / `75` | **yes** | not yet |
| Pascal | GTX 10-series | 6.1 / `61` | **experimental**: CUDA 13 dropped it, so it needs CUDA 12 and a driver of **570 or older** (580 failed on a GTX 1080 Ti here with CUDA error 802, D4). Not proven yet; running it through Vulkan instead of CUDA is untested | never ran here (retired over D4) |
| Maxwell and older | GTX 9-series … | ≤ 5.x | no: CUDA support ending, most have ≤ 4 GB | no |

The setup script refuses an unsupported card and says why; `GENGHIS_ALLOW_OLD_GPU=1` builds anyway, untested. A
supported card that nobody has measured yet ("not yet") is new evidence: the person's `--bench` number is the first
measurement of it.

**Linux:** `bash install/install-linux.sh --role donor --accel cuda --coord <authority-ip>`. It installs a driver if
none is active (then asks for a reboot and a re-run), reads the card's architecture from the driver, refuses a card
older than Turing (RTX 20 / GTX 16) unless told to try, and builds at the pinned commit. If CUDA 13.2+ isn't installed it **stops and prints NVIDIA's exact
commands** (they add NVIDIA's apt repository, so they are the person's to run), then the script is re-run. With 13.2
nothing else is needed on Ubuntu 26.04: no older host compiler, no patch (the reference 5060 Ti builds this way). **Windows:** the installer detects NVIDIA + the CUDA toolkit and builds the CUDA version. The
driver and the Visual Studio C++ workload are the person's to install; it tells them exactly what to click.

**Is it really on the GPU?**
- `verify` on the box: the `gpu` check reads the card through `nvidia-smi`, and on Linux checks that the running
  `ggml-rpc-server` is the CUDA build, not the CPU one.
- `verify <node-id> --bench` from the authority: a CUDA donor below 20 t/s on the reference model is flagged. The
  RTX 5060 Ti measured 40.1 t/s over RPC. The same card with the CPU build measured **0.58**, which is what "silently
  on the CPU" looks like.

## Intel Arc and AMD (Vulkan)

- **Linux:** `--accel vulkan`. The person's user must be in the `render` and `video` groups, and it only takes
  effect after **logging out and back in**. Before that, a desktop login can make it *look* fine, and then after a
  reboot every model fails with "no devices". The installer's preflight detects this and prints the command.
- **Windows:** building needs the Vulkan SDK; the installer detects an Intel or AMD GPU and says so.
- **Tested here:** the Intel Arc iGPU in an Intel NUC 14 Pro (Ubuntu 26.04). AMD is not tested yet.

## A second card in one box

One box can lend several cards, one `ggml-rpc-server` per card, each on its own port. The reference authority lends
its Arc on `:50052` and an RTX 5060 Ti in an eGPU enclosure on `:50053`. The recipe is in
[`INSTALL.md` §3](../../INSTALL.md#a-second-card-on-a-host--an-egpu-or-two-gpus-in-one-box-d46). What goes wrong:

- **Pin the binary and the device in every `@reboot` line** (`GENGHIS_RPC_BIN`, `GENGHIS_RPC_DEVICE`, `GENGHIS_RPC_PORT`).
  With two builds on the box, an unpinned launcher can start the wrong one after a reboot.
- **Register the second card as its own node, with its own host label:**
  `register --coord <authority-ip> --port 50053 --id <box>-<card> --host <box>-egpu`. If it shares the box's
  hostname, the box's own serve treats it as local instead of dialling it.
- `verify` on the box judges each card by **its own** server's port, and says which node lends the NVIDIA card.

## An eGPU enclosure

A card in a Thunderbolt enclosure on the same box has no network in the way: the reference 5060 Ti went from 38 t/s
(in its own box, over 1 GbE) to **228 t/s** in an enclosure on the host (D46). If the enclosure "does nothing":

1. **Does anything see anything on the cable?** Linux: `boltctl list`, then `lspci | grep -i -E "vga|3d|nvidia"`.
   Windows: Device Manager. If nothing shows at all, it's power, the cable, the port, or the enclosure. It isn't
   software. On the reference enclosure it was a dead contact switch, which looks exactly like a broken card.
2. If `boltctl` lists it but it's not authorized, the person runs `sudo boltctl enroll <uuid>`, which authorizes it
   and remembers it across reboots.
3. Then treat it as a second card (above). The enclosure's own network port isn't needed; the card talks over Thunderbolt.
