# Replacing the coordinator — the runbook (D26 / D30)

_Moving the **authority** (fleet + config + model library + mDNS + HEARTH backend) from one box to another.
Written for the reference move — **Pi 5 → NUC on Ubuntu** — but every step is generic. Removing or
replacing a *donor* is one click (D26); replacing the coordinator is this page. Budget one session._

> D30 is in place: the new box is authority + home base + donor with one installer run, and every other
> `serve` becomes an inference host of it the moment its `GENGHIS_COORD` points there (no rival `fleet.json`).

## 0 · What actually moves
| Thing | Where it lives on the OLD authority | Size |
|---|---|---|
| `fleet.json` | `~/genghis/fleet.json` | KB |
| `config.json` (names, roles, goals, goal→model map, PIN) | `~/genghis/config.json` | KB |
| Model library | `~/genghis/models/*.gguf` | tens of GB |
| HEARTH state (`hearth_message.txt`, `hearth_reply.txt`, `hearth_replies.log`) | `~/genghis/` | KB |
| Telemetry (`runs.jsonl`, `plans/`) | `~/genghis/` | MB — optional |

Everything else (mDNS name, `/metrics`, the Control Room) is *served*, not stored — it comes up on the new
box by itself.

## 1 · Bring the new box up as a coordinator (nothing points at it yet)
```bash
# on the NEW box (Ubuntu): base OS + GPU driver first (NUC: sudo apt install -y mesa-vulkan-drivers)
git clone https://github.com/RinkusKhan/The_GENGHIS_Protocol ~/genghis-src && cd ~/genghis-src
bash install/install-linux.sh --role coordinator --accel vulkan     # builds llama.cpp @ the pinned commit, runs genghis init, reboot-proof serve
```
Check: `curl http://localhost:8899/fabric.json` answers; the Control Room at `http://<new-ip>:8899/`
shows **this** box only. That is expected — it has its own fresh `fleet.json`.

## 2 · Copy the old authority's state over it
```bash
# on the NEW box — pull from the old one (adjust user/host)
OLD=<old-user>@<old-ip>
systemctl --user stop genghis-serve 2>/dev/null || pkill -f 'genghis_coordinator[.]py serve'   # stop OUR serve while we swap files ([.]: never matches this shell)
scp $OLD:~/genghis/fleet.json  ~/genghis/fleet.json
scp $OLD:~/genghis/config.json ~/genghis/config.json
scp $OLD:~/genghis/hearth_*    ~/genghis/ 2>/dev/null
rsync -avh --progress $OLD:~/genghis/models/ ~/genghis/models/        # the library — this is the slow step (~60 GB over 1 GbE ≈ 10 min)
```
Now edit `~/genghis/fleet.json` **once**, by hand or with the one-liner below:
- `coordinator.host` / `coordinator.ip` → the NEW box.
- The new box's own donor entry: `local: true`, its `device` (`Vulkan0` / `CUDA0`), **and** its `ip`/`port`
  (`50052`) so it is dual-role (D27). If `genghis init` already wrote an entry for this host, merge the two —
  keep the id the fleet already knows.
- The old box's entry: it stays a **donor** — leave its `ip`/`port`; make sure it is *not* `local: true`.
```bash
python3 - <<'EOF'
import json, os, socket, tempfile
p = os.path.expanduser("~/genghis/fleet.json"); f = json.load(open(p))
me = socket.gethostname().lower(); ip = "<NEW-IP>"
f["coordinator"].update(host=me, ip=ip)
for d in f["donors"]:
    if (d.get("host") or "").lower() == me:
        d.update(local=True, device=d.get("device") or "Vulkan0", ip=ip, port=d.get("port") or 50052)
t = tempfile.NamedTemporaryFile("w", dir=os.path.dirname(p), delete=False); json.dump(f, t, indent=2); t.close(); os.replace(t.name, p)
print("coordinator ->", f["coordinator"]["host"], ip)
EOF
```
Start `serve` again (the installer's launcher: `systemctl --user start genghis-serve`, or `~/genghis/serve.sh`).
Check: the Control Room on the new box now shows the **whole fleet**, `GET /models` lists the library.

## 3 · Repoint everything that named the old box
| Who | Where the address lives | Change |
|---|---|---|
| **Old authority (Pi)** | `~/genghis/serve.sh` + its `@reboot` cron | **stop serve for good** (it would keep advertising itself on mDNS): remove the cron line, kill the loop. Keep `donor-serve.sh` + `donor-report.sh` → it is now a plain CPU donor. |
| **Every donor's self-report** | `poc/donor-report.sh` — `GENGHIS_COORD` env in its cron (defaults to the old Pi) | `GENGHIS_COORD=<new-ip>:8899` in each donor's cron line (5060 Ti, Tegra, Pi). |
| **Laptop (client / part-time `/v1` host)** | user env `GENGHIS_COORD` (none set today → it was its own authority) | `setx GENGHIS_COORD <new-ip>` → with D30 its `serve` becomes an inference host of the new authority; until D30, stop the laptop's `serve` or accept a stale copy. |
| **A Windows host (if any)** | user env `GENGHIS_COORD=<old-ip>` | `setx GENGHIS_COORD <new-ip>` and restart its serve (it becomes a host of the new authority, D30). |
| **Prometheus** | `monitoring/prometheus.yml` → `targets: ['<old-ip>:8899']` | new ip; `docker compose restart prometheus` (or move the whole Docker tier here — D29). |
| **HEARTH TV** | discovers by mDNS `_genghis._tcp` | nothing — as long as **only the new box advertises**. Reboot the TV app once and confirm it shows the fabric. |
| **Tailscale bookmarks** | your phone / laptop | the new box's tailnet IP (install Tailscale on it in the same session). |

## 4 · Verify (the whole fleet, from three vantages)
```bash
# on the new authority
python3 genghis_coordinator.py heartbeat            # every node UP that should be
python3 genghis_coordinator.py decide --goal biggest  # the pool is the full fleet
curl -s http://localhost:8899/models | head          # library served from here
# from the laptop
py genghis_coordinator.py discover                  # mDNS finds the NEW box, and only it
py genghis_coordinator.py decide --goal fastest     # plans against the new authority's fleet
# TV: shows the fabric; Control Room: Local Resources cards up (after D29's Docker move)
```

## 5 · The old box becomes the cold spare (Pi 5)
A nightly copy of the authority's state, so a dead NUC is a one-minute recovery, not a rebuild:
```bash
# on the Pi — cron: 0 3 * * *
rsync -a --delete <new-user>@<new-ip>:~/genghis/{fleet.json,config.json,hearth_message.txt,hearth_reply.txt,hearth_replies.log} ~/genghis-spare/
rsync -a <new-user>@<new-ip>:~/genghis/models/ ~/genghis-spare/models/     # optional: keep the library mirrored too (1 TB Pi disk)
```
**If the NUC dies:** on the Pi, `cp ~/genghis-spare/* ~/genghis/`, edit `coordinator` back to the Pi, start
`serve.sh`, repoint `GENGHIS_COORD` on the donors — the fleet is back. (This is deliberately manual; an
automatic failover would need leader election and is not worth it for a home fleet.)

## 6 · Rollback (if step 2–3 go wrong)
Nothing on the old box was deleted. Stop `serve` on the new box, restart the old box's `serve.sh`, revert
any `GENGHIS_COORD` you changed. Donors never cared which box was the authority.

## 7 · What the reference move taught (Pi 5 → NUC, 2026-09-14 — zero fleet downtime)
- **Pull the library FIRST, and keep the old serve up until it lands.** `models pull <name.gguf>` on the new box (with
  `GENGHIS_COORD=<old-ip>`) streams from the old authority's `/models` with HTTP-Range resume — so the old `serve` is
  the *file source* and must outlive the copy. Everything else (state copy, mode flip, repointing) can happen while it
  streams; the `.part` is invisible to `/models` until it completes. Two boxes advertising `_genghis._tcp` for that
  window is harmless (hosts use an explicit `GENGHIS_COORD`; the TV re-discovers only on app restart). A box that was
  already an inference host usually holds most of the library already — check before you copy.
- **Where the state lives depends on how the box was installed.** The Linux installer's checkout is
  `~/genghis-src/poc/` (fleet, config, models, HEARTH files, watchdog.log all next to the coordinator); an older
  hand-deployed box uses `~/genghis/`. Adjust step 2's paths.
- **Merging `config.json`:** the authority's *human* config moves wholesale, but the new box may already carry
  per-box keys (`residency`, `resident_ctx`, `models_dirs`, `auth` — `_LOCAL_HOST_KEYS` in the coordinator); keep
  those from the new box if it set any.
- **Flipping the new box to authority = three things:** delete the `export GENGHIS_COORD=` line from `~/.profile`
  (`serve.sh` reads it for crons), rewrite its `@reboot … serve.sh` cron line without `GENGHIS_COORD=`, and point its
  own `donor-report.sh` cron at *itself*. Then add the **watchdog** cron here (`*/15 * * * * python3 …/watchdog.py`)
  and remove it from the old box — the "verified HH:MM" line follows the authority.
- **Kill precisely over SSH.** `pkill -f "genghis_coordinator.py serve"` matches the SSH shell that runs it and kills
  your own session. Use `pkill -f 'genghis_coordinator[.]py serve'`: `[.]` still matches the serve, but never the
  shell running the command. (Anchored `^/usr/bin/python3 …` patterns break the moment the interpreter path or its
  flags differ, and then match nothing at all.) Also kill an orphaned resident `llama-server` (port 8081) so the new serve starts clean.
- **Windows host:** `[Environment]::SetEnvironmentVariable("GENGHIS_COORD", "<new-ip>", "User")` is not enough — the
  running `serve-laptop.ps1` loop inherited the old env at logon. Stop the loop *and* its python child, then
  `Start-Process` the launcher from a shell that has the new value set.
- **The Docker tier (D29) is a natural part of the same session.** The new authority's Control Room shows Open WebUI /
  Grafana / Prometheus as **NOT ON THIS HOST** until you move them: `sudo apt install -y docker.io docker-compose-v2 &&
  sudo usermod -aG docker $USER` (the one sudo step), stage `docker-compose.yml` + `branding/` + `monitoring/` on the new
  box, set the Prometheus target to the new box, `docker volume create open-webui` (restore the old box's volume into
  it with a tar if you want the chat history), `docker compose up -d`, then `docker compose down` on the old box.
- **Cold spare without a new key.** Step 5's rsync needs an SSH key from the old box to the new one. If you'd rather
  not mint one, pull over HTTP instead (reads are open under `auth.protect: writes`): `/fleet.json`, `/config.json`,
  `/hearth` → `hearth_message.txt`, `/replies` → `hearth_replies.log`. The reference fleet's `~/genghis/spare-pull.sh`
  on the Pi does exactly that at 03:00; the recovery recipe is in its header. The old box already holds the full model
  library, so nothing else needs mirroring.
- **What you should see afterwards:** the new box's `serve` log says `mDNS _genghis._tcp advertised as <new-ip>`; its
  first watchdog pass lists `hosts: <new>(authority), …(host)`; `discover` on the laptop returns the new box; the old
  box's crontab has no `serve.sh` line and its `donor-report.sh` line carries `GENGHIS_COORD=<new-ip>:8899`.

---
_The maintainer's own move (Pi 5 → NUC) is tracked in STATUS.md (internal)._
