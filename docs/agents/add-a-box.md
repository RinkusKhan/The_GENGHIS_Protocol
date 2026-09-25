# Add a box: a donor or a host

Every box after the authority is a **donor** (it lends its CPU or GPU; this is most boxes), a **host** (it also
answers chats on its own GPU), or both. Read [`AGENTS.md`](../../AGENTS.md) first. You need the authority's LAN
address; ask the person if discovery doesn't find it.

## 1 · Decide the role

- **Donor, by default.** A donor runs only `ggml-rpc-server`: it holds its share of a model for whichever host is
  running it, and it needs nothing else.
- **Also a host (`--serve` / `-Serve`) only when the person sits at this box** or wants it to answer on its own (a
  second daily driver). One warm model needs exactly one host. More hosts don't make anything faster
  ([`INSTALL.md`, "Hosts and donors"](../../INSTALL.md#hosts-and-donors--head-and-hands-read-this-once)).
- **An always-on box with no GPU that isn't the authority** is still a useful donor: it adds memory, so larger
  models fit.

## 2 · Before you install

- **Tell the person if their hardware is unproven.** Proven in real runs: NVIDIA GPUs (CUDA), Intel Arc (Vulkan),
  Raspberry Pi 4 and 5, Jetson (Tegra X1), and Linux and Windows machines with those GPUs. An x86 **CPU** donor has
  been measured only as a virtual machine (19.0 t/s on 13 cores of a laptop, over a Wi-Fi bridge). Not yet measured:
  x86 servers on real hardware, Macs, NAS boxes, phones, tablets, Android TV. For NVIDIA cards, see the support table
  in [`gpus.md`](gpus.md) (RTX 20 / GTX 16-series and newer; GTX 10-series is experimental).
- **Recommend a cable.** It is the biggest speed change a person can make for free: the same Pi 5 went from 7.6 to
  9.7 t/s. To move a box that is already on Wi-Fi without changing its address, see section 5.
- **Raspberry Pi and Jetson:** a 64-bit OS (`uname -m` must print `aarch64`). The installer upgrades an old compiler
  itself (the Tegra's gcc 7 cannot build llama.cpp's ARM code).
- **Any GPU:** read [`gpus.md`](gpus.md) before installing. The driver and toolkit decide whether the GPU is really used.

## 3 · Install

**Linux, including Raspberry Pi and Jetson:**
```bash
git clone https://github.com/RinkusKhan/The_GENGHIS_Protocol ~/genghis-src && cd ~/genghis-src
#   (downloaded the ZIP instead? unpack it and cd into it: the installer works the same from an unpacked archive)
bash install/install-linux.sh --role donor --accel cpu --coord <authority-ip> --preflight     # changes nothing
bash install/install-linux.sh --role donor --accel cpu --coord <authority-ip> --name <a-name>   # cpu | cuda | vulkan
#   add --serve to make it a host too; --name is the box's name in the fleet (default: its hostname).
#   A chosen --name is kept even if the fleet knew this box under another name. Without one, a box the fleet already
#   knows keeps its fleet name, and a SECOND machine with the same hostname (every fresh Ubuntu Server is "ubuntu")
#   gets a name of its own, e.g. ubuntu-2 -- so pass --name.
```

**Windows** (from the repo folder, in PowerShell). The accelerator is detected automatically:
```powershell
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -PreflightOnly -Donor -Coord <authority-ip>
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Donor -Coord <authority-ip> -Name <a-name>   # add -Serve to host too
```
It installs what is missing through `winget`: Python, Git, CMake and, unattended, the Visual Studio C++ build tools
(about 10 minutes; they are what compiles llama.cpp). It builds llama.cpp at the pinned commit, **starts the donor**,
offers the firewall rule, **registers the donor's port** with the authority (which dials it and says at once if it
cannot), and finishes by running `verify`. The preflight lists everything the real run will change, firewall rule
included: that list is what the person says yes to. After a reboot a **CPU** donor starts at boot (a scheduled task,
created when the installer runs as administrator), a **GPU** donor at logon; `verify`'s `at boot` check says which.
Windows specifics that bite:
- **Run it as administrator** (the C++ tools and the firewall rule need it). Over SSH, an administrator's session already is.
- **The firewall:** the donor port (`50052`) must be open, and a fresh Windows network is usually classed **Public**, so a
  rule for Private networks only does not help. The installer offers a rule for every profile, limited to the local
  network. Opening a port is the person's decision: with `-Yes` they have given it.
- A GPU donor needs a **logged-in session** to reach the GPU. On a headless box, the person enables auto-login
  (Sysinternals Autologon).
- After a `winget` install, open a **new** PowerShell window before re-running, so the new PATH is visible.
- **A brand-new Windows** may queue downloads behind its first round of updates. If an install sits still for many
  minutes, opening **Settings → Windows Update** once gets it moving.

**If you are an agent running it without a terminal:** the installer's questions ("apt install …?") can't be asked,
so it answers **no** and says so on a `[ ?? ]` line. Show the person what the preflight listed; if they agree, re-run
with `--yes` (Windows: `-Yes`). That is their consent, given to you, not a way around it. **How long it takes:** an x86
Linux CPU donor about 2 to 5 minutes; a Pi, a Jetson or a GPU build longer; a bare Windows box 15 to 30 minutes (the
C++ build tools), about 4 once they are there. Run it detached with its output in a log file, and read the log, rather
than holding one command open.
- **Linux:** `nohup bash install/install-linux.sh … --yes > ~/genghis-install.log 2>&1 &`
- **Windows over SSH:** the remote shell is `cmd.exe`, and Windows ends a session's processes when it closes, so start
  the installer through WMI, which outlives the session (from PowerShell in the repo folder):
  ```powershell
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "powershell -NoProfile -ExecutionPolicy Bypass -Command `"& '$PWD\install\install-windows.ps1' -Donor -Coord <authority-ip> -Name <a-name> -Yes *> '$HOME\genghis-install.log'`"" }
  ```
  Then read `%USERPROFILE%\genghis-install.log` until it ends with the `verify` summary.

On Linux the installer builds llama.cpp at the pinned commit, starts the donor through `donor-serve.sh` (one launcher,
restarts on a crash, its log at `~/genghis/rpc.log`), makes it survive reboots, opens `50052` to this network only
when a firewall (ufw or firewalld) is on (and says plainly when none is), registers the box with the authority, and
**finishes by running `verify`**. Its preflight lists all of that before anything changes.

## 4 · Finish: prove it, don't assume it

1. On the box: `python3 poc/genghis_coordinator.py verify` (Windows: `py poc\genghis_coordinator.py verify`).
   Everything that isn't PASS has a `fix`; see [`when-verify-fails.md`](when-verify-fails.md).
2. **From the authority** (or any host), measure it: `python3 poc/genghis_coordinator.py verify <node-id> --bench`.
   A donor can't bench itself (it keeps no models); if you can't reach the authority, give the person this command.
   This puts every layer of the small reference model on the new box and reports where they landed and how fast it
   ran, beside what other boxes of its class measured:

   | Class | Measured on the reference model, every layer on the node |
   |---|---|
   | Raspberry Pi 5 (CPU) | 9.7 t/s wired, 7.6 on Wi-Fi |
   | Raspberry Pi 4 (CPU) | 3.4 t/s wired |
   | Jetson Tegra X1 (CPU) | 1.8 t/s |
   | RTX 5060 Ti (CUDA) | 40.1 t/s over RPC; a CUDA box near **0.6** t/s is running on its CPU (see gpus.md) |

3. Show the person the **Bookmark these** card, and the node's number.

If the box is a kind nobody has measured yet (see section 2), its `--bench` number is new evidence; tell the person so.

**A virtual machine** works as a donor, but `verify` can't see its real network link: a VM's network card always looks
wired, even when the host reaches the LAN over Wi-Fi. `verify` says so (`link` INFO) rather than claiming "wired".

## 5 · Moving a box from Wi-Fi to a cable, keeping its address

GENGHIS finds a box by address, so keeping the address means nothing else changes. **Plug the cable in first.**
Prepare the commands; the person runs them (they need sudo). Switching the network over SSH can cut your own
connection, so run the switch detached, and only disable the Wi-Fi after the cable is confirmed working.

- **Raspberry Pi OS (NetworkManager):** copy the Wi-Fi connection's gateway and DNS
  (`nmcli -g IP4.GATEWAY,IP4.DNS device show wlan0`), then
  `sudo nmcli con add type ethernet ifname eth0 con-name wired-static ipv4.method manual ipv4.addresses <ip>/24 ipv4.gateway <gw> ipv4.dns <dns>`,
  bring it up detached (`sudo nohup sh -c "nmcli con down '<wifi>'; nmcli con up wired-static" &`), check `ip -br addr`,
  then `sudo nmcli con mod '<wifi>' connection.autoconnect no`.
- **Ubuntu (netplan + systemd-networkd, no `nmcli`):** replace the Wi-Fi netplan file with a static `eth0` one and keep the
  old file as a backup. Ubuntu's cloud-init rewrites netplan on the next boot unless
  `/etc/cloud/cloud.cfg.d/…` says `network: {config: disabled}`. Apply it with an automatic rollback if the router
  can't be reached within a minute.
- **Afterwards:** `verify` on the box should report `link … via eth0 (wired)`. Suggest the person reserve the address
  in the router for the box's **Ethernet** MAC address (`ip link show eth0`).
