# AGENTS.md — for an AI agent installing, extending or repairing GENGHIS

You are probably here because a person asked you to set up GENGHIS, add a machine to it, or fix one. GENGHIS pools
the memory and compute of the devices on a home network so they can run AI models together: one **authority**
(always-on; owns the fleet record and the model library), any number of **donors** (lend a CPU or GPU over
llama.cpp RPC), and **hosts** (answer chats on their own GPU). Read this file first; it is short on purpose.

## You are finished when `verify` passes — not when the installer exits

```bash
python3 poc/genghis_coordinator.py verify           # on the box you set up   (Windows: py poc\genghis_coordinator.py verify)
python3 poc/genghis_coordinator.py verify --json    # the same, for you to parse; exit code 1 on any FAIL
python3 poc/genghis_coordinator.py verify <node-id> --bench    # from the authority: every layer on that node, at what speed
```

`verify` changes nothing on the box (the one file it writes is its address card) and is safe to run as often as you like. Every WARN and FAIL comes with a `fix`; follow it, then
run `verify` again. Both installers already end by running it and exit with its result. When it passes, **show the
person the "Bookmark these" card** it prints (also saved to `~/genghis-addresses.txt`): those are the addresses they
will use every day, and they are checked, not guessed.

## Rules that are easy to break and expensive to break

1. **Never change the llama.cpp commit.** Every box builds `eab8ee41f889ef7823af517e8098fb8a9b3cf601`. RPC has no
   compatibility across versions: a box built from anything else fails silently or strangely. Do not "update to
   latest", build from `master`, or use a packaged llama.cpp. The installers pin it for you.
2. **Do not accept the CUDA toolkit or NVIDIA driver a package manager offers by default.** The card decides what
   works: see the table in [`docs/agents/gpus.md`](docs/agents/gpus.md). Ubuntu's own `nvidia-cuda-toolkit` is too
   old for an RTX 50-series card, and a GTX 10-series card needs an older driver.
3. **Check where the layers landed, never the token count.** A GPU box that silently fell back to its CPU still
   produces text. `verify --bench` checks placement (`29/29 layers on '<node>'`) and speed for the node's class.
4. **Never hand-edit `fleet.json` or `config.json`.** The authority owns the fleet record; boxes register themselves
   (the installers do it, or `genghis_coordinator.py register --coord <authority-ip>`). A person's choices (names,
   default goal, which model a goal runs) are made in the Control Room or `/admin`.
5. **Sudo and passwords belong to the person.** Prepare the exact commands, say what each one does, and let them
   run the privileged ones. Never type, store or echo a password or token. Never turn a firewall off: open the one
   port that is needed (`8899` for a serve, `50052` for a donor).
6. **Their choices live in their home, not in this repository.** Roles, adapters and anything personal go in the
   folder `GENGHIS_HOME` points at (see [`home.example/`](home.example/)). Never commit a person's `fleet.json`,
   `config.json`, models or roles into the repo. (The installer writes each box's own `poc/fleet.json` and
   `poc/config.json`: machine-local and git-ignored. That is expected.)
7. **Never silent.** If a step fails or you skip it, say so plainly and do not report the job as done. "It should
   work" is not a result; a passing `verify` is.
8. **One authority per fleet.** Every other box points at it (`--coord <authority-ip>` / `-Coord`, or the
   `GENGHIS_COORD` environment variable). Do not start a second authority to make an error go away.
9. **Recommend a cable for donors.** Wi-Fi works, but every token makes several round-trips to every donor: the same
   Raspberry Pi 5 generated 29 % faster on a cable (7.6 → 9.7 t/s), and the planner keeps slow-link CPU donors out
   of speed plans. `verify`'s `link` check asks the OS whether the box is on Wi-Fi.
10. **Never `pkill -f` a plain pattern over SSH.** `pkill -f "genghis_coordinator.py serve"` matches your own SSH session's
    command line and kills it. Either find the PID (`pgrep -af`) and `kill <pid>`, or bracket one character so the
    pattern can't match itself: `pkill -f 'genghis_coordinator[.]py serve'`, and don't write the unbracketed name anywhere
    else in that same SSH command, or the shell matches after all. The launchers restart the serve on their own.

## Pick the job

| The person wants to… | Read |
|---|---|
| set up their first box (the authority) | [`docs/agents/set-up-the-authority.md`](docs/agents/set-up-the-authority.md) |
| add a box: a donor or a host; Linux, Raspberry Pi, Jetson or Windows | [`docs/agents/add-a-box.md`](docs/agents/add-a-box.md) |
| add or fix a GPU: NVIDIA, Intel Arc / AMD, a second card in one box, an eGPU | [`docs/agents/gpus.md`](docs/agents/gpus.md) |
| understand a WARN or FAIL from `verify` | [`docs/agents/when-verify-fails.md`](docs/agents/when-verify-fails.md) |
| use it: models, speed settings, roles | [`USAGE.md`](USAGE.md) · [`docs/MODELS.md`](docs/MODELS.md) |

## Where things are

- **Installers:** [`install/install-linux.sh`](install/install-linux.sh) and [`install/install-windows.ps1`](install/install-windows.ps1).
  Run them with `--preflight` / `-PreflightOnly` first: it detects and reports, and changes nothing.
- **Everything else is one program:** [`poc/genghis_coordinator.py`](poc/genghis_coordinator.py) (the CLI and the serve).
  Every command: [`docs/COMMANDS.md`](docs/COMMANDS.md). The manual walkthrough the installers automate: [`INSTALL.md`](INSTALL.md).
- **Why a rule exists:** [`DECISIONS.md`](DECISIONS.md) (the D-numbers). **What was measured, on what:** [`poc/RESULTS.md`](poc/RESULTS.md).
- **Python:** `python3` on Linux and macOS, `py` on Windows. **Ports:** `8899` serve (Control Room, `/v1` API, admin),
  `50052` donor (`50053` and up for a second card), `3080` Open WebUI, `3000` Grafana, `9090` Prometheus.

## Ask the person rather than guess

- **Which box is the authority?** The most reliable always-on box (see the two-question rule in
  [`INSTALL.md`](INSTALL.md#where-things-live--the-two-question-rule-d30)). If they aren't sure, recommend and explain; don't decide silently.
- **The authority's address**, if you are adding a box and discovery (mDNS) doesn't find it.
- **Whether a GPU box should also answer chats on its own** (`--serve` / `-Serve`). The default is no: a donor
  lends its card and needs nothing more.

## Be honest about what is proven

Proven in real runs: NVIDIA GPUs (CUDA; supported from RTX 20 / GTX 16-series up, GTX 10-series experimental, see
`docs/agents/gpus.md`), Intel Arc iGPUs (Vulkan), Raspberry Pi 4 and 5, Jetson (Tegra X1), and Windows and Linux hosts.
An x86 CPU donor has been measured only as a virtual machine. **Not yet measured:** x86 servers on real hardware, Macs,
NAS boxes, phones, tablets, Android TV. If the
person's hardware is on the second list, say so before you start. If it works, `verify <node-id> --bench` from the
authority gives the number that moves it onto the first list.
