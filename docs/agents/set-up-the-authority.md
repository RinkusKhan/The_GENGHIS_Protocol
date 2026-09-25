# Set up the authority (the first box)

The **authority** keeps the fleet record, the model library, the Control Room and mDNS discovery. Every other box
points at it, so it has to be the most reliable **always-on** box the person owns. Read [`AGENTS.md`](../../AGENTS.md)
first; its rules apply to every step here.

## 1 · Choose the box (ask; don't assume)

Two questions, from [`INSTALL.md`](../../INSTALL.md#where-things-live--the-two-question-rule-d30):
**is it always on? does it have a GPU?**

- **Always on is required.** A laptop that sleeps or travels is a poor authority; it can be a host instead.
- **A GPU is a bonus:** then the authority is also the home base, answering chats on its own card. That is the
  common setup (the reference fleet's authority is an Intel NUC 14 Pro on Ubuntu with an Arc iGPU and an eGPU).
- **No GPU is fine.** A Raspberry Pi 5 was the reference authority for a week. Chats then run on a GPU box set up
  as a host (`--serve`) that points at the authority.
- **Wired, not Wi-Fi.** Every box talks to the authority. Linux is the tested platform for this role.

If there is a choice, recommend one, say why, and let the person decide.

## 2 · Preflight, then install

```bash
git clone https://github.com/RinkusKhan/The_GENGHIS_Protocol ~/genghis-src && cd ~/genghis-src
bash install/install-linux.sh --role coordinator --preflight   # detects and reports; changes nothing
bash install/install-linux.sh --role coordinator               # fleet record + config + a reboot-proof serve on :8899
```

Fix what the preflight lists before installing. It prints the command for anything it can't do itself. The apt
installs need sudo, which is the person's to run (rule 5).

## 3 · If the authority has a GPU, make it lend it

`--role coordinator` sets up the fleet record and the serve, but **does not build llama.cpp**, even if you pass
`--accel`. To use the authority's own GPU (and to give it `llama-cli` for `verify --bench`), run the installer a
second time as a donor, pointed at the authority's **own LAN address**. This is exactly how the reference
authority runs:

```bash
bash install/install-linux.sh --role donor --accel vulkan --coord <this-box-lan-ip>   # Intel Arc / AMD
bash install/install-linux.sh --role donor --accel cuda   --coord <this-box-lan-ip>   # NVIDIA: read gpus.md first
```

This is safe on the authority: `init --coord` backs up `fleet.json`, keeps every node already in it, and reuses the
entry the fleet already has for this host. The serve still runs as the authority. A second card on the same box
(an eGPU, for example) is its own node on its own port: see [`gpus.md`](gpus.md#a-second-card-in-one-box).

## 4 · Put models in the library

The library is `poc/models/`, one folder with nothing to configure. Every box fetches from it. Start with the
small model the measured references are based on, so `verify --bench` means something:

```bash
curl -L -o poc/models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf   # 1,117,320,736 bytes
```

Larger models are the person's choice. [`docs/MODELS.md`](../MODELS.md) explains how models map to the speed settings.

## 5 · The watchdog

An independent 15-minute pass that notices when something, including the serve itself, has died. It also sets up
Open WebUI on this box for local models (their ~30 built-in tools off), within 15 minutes of the chat UI appearing.
**The installer adds it** (`--role coordinator`), and `verify` has a `watchdog` check. Only if that check warns, add it
by hand:

```bash
( crontab -l 2>/dev/null | grep -v watchdog.py; echo "*/15 * * * * /usr/bin/python3 $HOME/genghis-src/poc/watchdog.py >> $HOME/genghis-src/poc/watchdog.cron.log 2>&1" ) | crontab -
```

## 6 · Optional, only if the person asks

- **Chat in the browser, dashboards:** `docker compose up -d` on this box: Open WebUI on `:3080`, Grafana on `:3000`,
  Prometheus on `:9090` ([`INSTALL.md`](../../INSTALL.md#the-docker-tier-open-webui--grafana--prometheus--one-copy-on-the-home-base-d29)).
- **Reach it from outside the house:** Tailscale or Cloudflare Tunnel ([`INSTALL.md`](../../INSTALL.md#reach-it-from-anywhere-optional--two-front-doors)).
  Never forward a port on the router.

## 7 · Finish

```bash
python3 poc/genghis_coordinator.py verify
```

When it passes, show the person the **Bookmark these** card, and tell them the Control Room address is the one to
keep. Then add their other machines: [`add-a-box.md`](add-a-box.md).
