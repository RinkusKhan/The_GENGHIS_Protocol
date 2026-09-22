# GENGHIS — Fleet monitoring (Prometheus + Grafana)

The coordinator serves `GET /metrics` in Prometheus format. This folder turns that into a live dashboard.

![panels: nodes up · fleet throughput · repo size · per-node throughput/free/latency · up-down timeline · model sizes]

## Quick start (one command)

Monitoring is part of the **one unified `genghis` Compose project** at the repo root (D21) — it starts
together with the Open WebUI chat window, sharing `com.genghis.*` labels so the whole UI tier tears down as
a unit. Run it from the **repository root**, not this folder:

```bash
docker compose up -d          # from the repo root — prometheus + grafana + open-webui
```
- **Grafana** → http://localhost:3000 — the **"GENGHIS Fleet"** dashboard is pre-loaded (anonymous view is on; log in as `admin`/`admin` to edit).
- **Prometheus** → http://localhost:9090 — *Status → Targets* should show the `genghis` job **UP**.
- **Open WebUI** → http://localhost:3080 — the chat window onto GENGHIS's `/v1`.

Stop it with `docker compose down` (from the repo root). See everything GENGHIS runs with
`docker ps --filter label=com.genghis.project=genghis`. The `docker-compose.yml` still *in this folder* is a
retired pointer stub — the assets here (`prometheus.yml`, `grafana/`) are referenced by the root project.

> Docker is the **optional UI tier** (D21). The core fabric — coordinator + `/v1` serve — is pure Python +
> llama.cpp and needs none of this; if Docker is down, the pool keeps serving, you just lose the browser UI.

> The scrape target is a placeholder (**`CHANGE-ME:8899`**). If your coordinator is elsewhere, edit [`prometheus.yml`](prometheus.yml) and `docker compose restart prometheus`.

## What's on the dashboard
| Panel | Metric |
|---|---|
| Nodes up · Fleet throughput · Repo models · Repo size | `genghis_nodes_up`, `sum(genghis_node_throughput_tps)`, `genghis_repo_models_count`, `sum(genghis_repo_model_bytes)` |
| Throughput / Free memory / Latency per node | `genghis_node_throughput_tps`, `genghis_node_free_mb`, `genghis_node_latency_ms` (labeled by friendly name) |
| Node up / down timeline | `genghis_node_up` |
| Repo model sizes | `genghis_repo_model_bytes` |

## Manual import (existing Grafana)
1. Add a Prometheus datasource pointing at your Prometheus.
2. **Dashboards → New → Import → Upload** [`grafana-dashboard.json`](grafana-dashboard.json); pick your Prometheus datasource when prompted.

## If the coordinator is locked (`auth.protect: all`)
Prometheus must send the PIN. In [`prometheus.yml`](prometheus.yml) under the `genghis` job, uncomment **one** of:
```yaml
    authorization:
      type: Bearer
      credentials: "YOUR_PIN"
# or:
    params:
      token: ["YOUR_PIN"]
```
(At the default `auth.protect: writes`, `/metrics` is open on the LAN — no token needed.)

## Alerting — "a donor dropped"
A provisioned alert rule **GENGHIS donor down** ([`grafana/provisioning/alerting/donor-down.yaml`](grafana/provisioning/alerting/donor-down.yaml)) fires when any **compute** donor reports `genghis_node_up == 0` for **2 minutes** (rides out a transient blip). See it at **Grafana → Alerting → Alert rules** (folder *GENGHIS*); firing rules turn red there.

**Turn it off / on:**
- **Quick (UI, no restart):** *Alerting → Silences → New silence*, matcher `alertname = GENGHIS donor down`. Delete the silence to re-enable.
- **Declarative:** set `isPaused: true` in the rule file and `docker compose restart grafana`.

**Get it pushed to you** (email / Slack / Telegram / webhook): *Alerting → Contact points → add one*, then point the default notification policy (or a policy matching `severity=warning`) at it. Without a contact point the alert still fires and is visible in the UI — it just isn't delivered anywhere.

*Test it:* stop a donor's `ggml-rpc-server` (or unplug it); within ~2–3 min the rule goes **Firing** and the dashboard's "Node up/down" panel shows red. Bring it back and it clears.

## Notes
- `scrape_interval` is 30s; each scrape refreshes fleet liveness (a heartbeat to each donor). Lower it if you want snappier graphs.
- All metric names, labels, and help are defined in `metrics_text()` in `poc/genghis_coordinator.py`.
