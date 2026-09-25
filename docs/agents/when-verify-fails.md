# When `verify` doesn't pass

`verify` prints one line per check, named by the second column (`authority`, `registered`, …), and a `fix` under
every WARN and FAIL. Try that fix first. This page is for when it isn't enough: what each check means, the usual
causes in the order to try them, and how to confirm. Fix one thing, then run `verify` again.
**FAIL** means the box isn't doing its job. **WARN** is advice: the box works, but slower or more fragile than it
should. Read [`AGENTS.md`](../../AGENTS.md) first.

## `authority`: no fleet authority answered
1. **The box doesn't know where the authority is.** Set it for this shell: `export GENGHIS_COORD=<authority-ip>`
   (Windows: `$env:GENGHIS_COORD="<authority-ip>"`), then re-run. The installers make it permanent with `--coord` / `-Coord`.
2. **The authority's serve isn't running.** On the authority: `curl -s http://127.0.0.1:8899/fabric.json | head -c 200`.
   If nothing comes back, `tail -30 ~/genghis-serve.log` (or `poc/serve.log`). `serve.sh` restarts it within seconds
   once the cause is fixed.
3. **A firewall is in the way.** From this box: `curl -s -m 3 http://<authority-ip>:8899/fabric.json`. It works on
   the authority but not from here? Open TCP `8899` on the authority. On Windows, open it for **all** profiles: a
   network reclassified as Public stops matching a Private-only rule.

## `registered`: not in the fleet, or DOWN
- **Not in the fleet:** the box never announced itself. Run
  `python3 poc/genghis_coordinator.py register --coord <authority-ip>` (a donor also needs `--port 50052`). A second
  card on this box registers as its own node: see [`gpus.md`](gpus.md#a-second-card-in-one-box).
- **In the fleet but DOWN:** the authority can't reach this box's donor port. Check the `rpc-server` line (below),
  then the firewall: TCP `50052` must be open to the LAN. On Windows, a GPU donor also needs a logged-in session.
- **WARN "marked DOWN … but it answered N s ago":** the donor was busy when the authority's once-a-minute check dialled
  it (right after a `--bench`, or while a pooled model used it). A donor serves one client at a time. Run `verify`
  again in a minute; only if it *stays* down is it the port or the firewall.

## `report`: the self-report uses a name the fleet doesn't know
The box's reporter (its `donor-report.sh` cron line) runs under one name while the fleet knows the box by another, so
the authority rejects every free-memory report and plans with stale numbers. `verify` prints the exact fix: change the
name in `crontab -e`, then restart the reporter with the fleet's name. The installer starts the reporter under the
name the authority confirms, so this should only happen on a box set up before that, or renamed since. The reporter's
log (`~/genghis-report.log`) also says so, once, when its reports start being rejected.

## `rpc-server`: nothing listening
The donor process isn't running. **Linux:** `bash poc/donor-serve.sh` starts it (only one copy per port ever runs:
a second one says so and leaves), and `crontab -l` should show an `@reboot` line for it (the installer adds one). Its
log says why it stopped: **`~/genghis/rpc.log`**. If that log says the port "is already served by another process",
something else started a server by hand: `pgrep -af ggml-rpc-server` finds it; stop it and the loop takes over. **Windows:** the `GENGHIS-rpc` launcher in the Startup folder (`shell:startup`) starts it at logon.

## `llama.cpp`: built from the wrong commit
Never "fix" this by upgrading everything else. Rebuild **this** box at the pinned commit with the installer, which
checks the commit out for you. A box at another commit cannot work with the fleet, and the failures look random.

## `gpu`: the GPU isn't really in use
- **"driver is not working":** after a driver install, reboot. If `nvidia-smi` still fails, the driver doesn't suit
  the card: see the table in [`gpus.md`](gpus.md#nvidia-cuda).
- **"lends as 'cpu'" on a box with a GPU:** the box was installed with the wrong accelerator. Re-run the installer
  with `--accel cuda` (NVIDIA) or `--accel vulkan` (Intel Arc / AMD).
- **"the running rpc-server is … a non-CUDA build":** the launcher started the CPU build. Point the `@reboot` line at
  the CUDA one (`GENGHIS_RPC_BIN=…/build-cuda/bin/ggml-rpc-server`), then restart the donor.
- **Pascal (GTX 10-series) with driver 580 or newer:** the driver no longer suits that card. Use 570 or older.
  (GTX 10-series is experimental, D52; `verify` WARNs for any card older than RTX 20 / GTX 16.)
- **Linux + Vulkan, "no devices" after a reboot:** the user isn't in the `render` and `video` groups. Add them, then
  log out and back in: `sudo usermod -aG render,video $USER`.

## `cuda-toolkit`: too old for the card
Every supported card (RTX 20 / GTX 16-series and newer, D52) uses **CUDA 13.2 or newer from NVIDIA's repository**;
the setup script prints the exact install commands. Install it, then re-run the installer's build step so llama.cpp
is rebuilt against it. (An experimental GTX 10-series card is the exception: it needs CUDA 12.)

## `link`: on Wi-Fi, or not wired-class
The box works, but every token pays for the link. Recommend a cable. **Inside a virtual machine** the check
reports INFO instead: a VM's network card always looks wired, so the real link is the host's. To keep the box's address while moving it
to a cable, see [`add-a-box.md`](add-a-box.md#5--moving-a-box-from-wi-fi-to-a-cable-keeping-its-address). If `verify`
says the box is wired but "the authority's record" is high, the record is older than the change: the authority
re-measures every node every minute, so run `verify` again shortly. A laptop on Wi-Fi is fine as a host; a donor is where the cable pays.

## `pdf`: this box's serve can't read PDFs
Asked of the running serve, which reads every PDF attached to a chat and every PDF in a role's knowledge folder.
Install `pypdf` into the interpreter the check names, using the command it prints. On Windows, the `py` launcher can
point at a different Python from the one the serve runs, so a plain `py -m pip install pypdf` may appear to work and
change nothing.

## `knowledge`: a role can't read (all of) its folder
Checked for every role in this box's home that has a `knowledge` folder. This box is the one that reads it: the box
the person's chat client talks to.
- **"PDF(s) need pypdf":** PDFs are read only if `pypdf` is installed **in the Python the serve runs**. Use the
  command `verify` prints: it names that interpreter. On Windows, `py -m pip install pypdf` can install into a
  *different* Python from the serve's and change nothing. No restart is needed: the next question reads them.
- **"folder does not exist":** the path in the role file is wrong. A relative path is relative to the role file,
  not to where the serve was started.
- **Nothing readable:** only unsupported file types, or scanned PDFs with no text layer (those need OCR first). The
  supported types are listed in [`home.example/README.md`](../../home.example/README.md#knowledge).

## `bench`: placement or speed
- **"not every layer landed on the node":** the node didn't have room, or the run fell back. Run the printed
  command by hand with `-v` and read the `assigned to device` lines.
- **"below N t/s for a cuda donor":** almost always the CPU build (see `gpu`) or a slow link (see `link`). A CUDA donor
  measured 40.1 t/s on the reference model; the CPU-build fallback measured 0.58.
- **Skipped, "holding part of a pooled model":** a host is using that node right now. That is normal; try again later.
- **Skipped, "no llama-cli on this box":** run the bench from the authority or any host:
  `verify <node-id> --bench`.

## `local`: skipped
You checked another node from this box. Run `verify` **on** that node for its build, GPU and rpc-server checks.

## When nothing here fits
Collect `verify --json` from the box and from the authority (`verify <node-id> --json`), plus the last 30 lines of the
serve and rpc-server logs. Before sharing any of it outside the person's own network, **remove addresses, hostnames
and account names**. The troubleshooting table in [`INSTALL.md`](../../INSTALL.md#troubleshooting) covers the rarer cases.
