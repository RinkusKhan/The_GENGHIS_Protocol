# GENGHIS — Usage Guide

Day-to-day use, once your fleet is installed ([INSTALL.md](INSTALL.md)). Full command reference:
[`docs/COMMANDS.md`](docs/COMMANDS.md).

> **Command prefix:** run these on your **client**. Windows → `py genghis_coordinator.py …`.
> Linux/macOS → `python3 genghis_coordinator.py …`. (This guide writes `py` for brevity.)

---

## Run a model across the fleet
```powershell
$env:GENGHIS_MODEL = "E:\models\<model>.gguf"   # a full path…
# …or just a NAME that lives in the authority's model library — GENGHIS fetches it if you don't have it:
$env:GENGHIS_MODEL = "Llama-3.3-70B-Instruct-Q4_K_M.gguf"
py genghis_coordinator.py decide                # SEE the plan (which nodes, why) — no inference
py genghis_coordinator.py run                   # actually run it (self-healing)
```
- **`decide`** shows the plan and each node's *why* — safe to run anytime, changes nothing.
- **`run`** executes it: it heartbeats the fleet, plans over the living, streams the model's shards to the
  chosen donors, generates, and **self-heals** — if a donor drops it re-plans; if the fleet is momentarily
  busy it **settles and retries** instead of giving up.
- A model that **fits your anchor GPU** runs 100% local (fast, no network). One that **doesn't** is pooled
  across donors — *that's the whole point:* run what no single box could hold.

## Choose *how* it runs — the goal knob
Add `--goal` to `decide`/`run`:

| goal | use it when |
|---|---|
| `fastest` | you want speed — bench the weak nodes, use the fewest fast ones that fit |
| `balanced` | the sensible default (speed-leaning, small safety margin) |
| `fit` | play it safe on capacity (fatter margin, more nodes) |
| `biggest` | run something huge — pool **every** node for max memory |

*(Measured: on a 70B, `fastest` beat `biggest` **3.25×** by benching the slow Tegra — fewer, faster nodes win when the model fits without the weak one.)*

## Chat with it (Open WebUI / any OpenAI app)
Beyond the CLI, the coordinator speaks the **OpenAI API** at `/v1`, so any OpenAI-compatible client works —
set its `base_url` to `http://<coordinator>:8899/v1` and pick a "model" (`genghis-fastest` … `genghis-biggest`
= the effort-routing goal). The turnkey human GUI is **Open WebUI**:
```bash
docker run -d -p 3080:8080 --name open-webui --restart always \
  -e WEBUI_AUTH=False -e OPENAI_API_BASE_URL=http://host.docker.internal:8899/v1 \
  -e OPENAI_API_KEY=none -v open-webui:/app/backend/data \
  ghcr.io/open-webui/open-webui:v0.11.4
```
Open **http://localhost:3080** and chat (no login). Point it at your **authority** (`http://<authority-ip>:8899/v1`)
for an always-on endpoint, or the **laptop** for the fast 5090 path. Optional web search (DuckDuckGo, no key)
is already enabled — toggle it per message. Full API + integration roles: [`INTEGRATION.md`](INTEGRATION.md).

**One setting to change on first use — the Task Model.** Open WebUI quietly makes *extra* calls per chat
(chat title, tags, follow-up suggestions) and by default sends them to **the same model you are chatting with**.
On GENGHIS that means a `genghis-balanced` question also queues a hidden second 32B run — on a pooled/remote
GPU that is a second 20 GB shard stream in front of your answer. Fix it once:
1. Avatar (top-left) → **Admin Panel** → **Settings** → **Interface**.
2. Set **Local Task Model** *and* **External Task Model** to **`genghis-fastest`** (the small model — a title
   does not need 32B). Save.
Now every chat costs one run of the model you chose, and the housekeeping rides the always-warm small one.
*(Reference fleet: set on the NUC's Open WebUI 2026-09-14; it lives in the `open-webui` Docker volume, so it
survives `compose down/up` and the volume copy when the tier moves box.)*

**One command sets the chat up right: [`integrations/openwebui/provision.py`](integrations/openwebui/provision.py).** Run it once
after the Docker tier is up (and again any time — it only writes what differs): `docker cp` it and `system_prompt.txt` into the
container, `docker exec open-webui python3 /tmp/provision.py`, `docker restart open-webui`. It turns Open WebUI's *Builtin Tools*
off on every GENGHIS model, sets the small default tool belt and native calling, installs the system prompt (diagrams + pages),
switches web search on and makes `genghis-fastest` the task model. Everything below is what it does, for when you want to see
or change it by hand.
**Tools in the chat — use NATIVE mode (D37).** GENGHIS `/v1` speaks OpenAI tool calling: the chat model decides, GENGHIS
relays `tool_calls`, Open WebUI runs the tool and sends the result back. Set it once: **Admin Panel → Settings →
Models → `genghis-balanced` (and `genghis-fit`) → Advanced Params → Function Calling = Native**. Then add tools
(**Workspace → Tools → +**, paste [`integrations/openwebui/genghis_fleet_tool.py`](integrations/openwebui/genghis_fleet_tool.py))
and enable them per model or per chat (**+** by the message box). Ask *"how's the fleet?"* — the Thinking panel shows the
tool call, then the answer. A keyless weather tool ships too ([`integrations/openwebui/weather_tool.py`](integrations/openwebui/weather_tool.py)).
**Keep the tool belt small — and turn OFF Open WebUI's "builtin tools".** With native calling every tool's schema goes to
the model on every turn, and a local model shown many tools *calls them all* instead of answering (Qwen 32B fired
~30 in a row for an org chart — 2026-09-16). Two rules: (1) **Model → Capabilities → Builtin Tools = off** on every
GENGHIS model — that capability (v0.11) hands the model ~30 internal functions (search chats, read/write/**delete**
memories, fetch URLs, run code…) it has no business calling on its own; (2) default-on tools = **Fleet + Weather +
Wikipedia** (~640 tokens), none on `genghis-fastest` (it is also the task model). Chart/UI tools (Visuals Core, OpenUI),
Sub Agent and any *installer* tool stay installed but **off by default** — enable them for one chat with **+**. An
installer tool must never be default-on: it installs code as you, and models take instructions from what they read.
**Charts and pages need no tool.** Paste [`integrations/openwebui/system_prompt.txt`](integrations/openwebui/system_prompt.txt)
into every GENGHIS model (**Admin Panel → Settings → Models → model → System Prompt**). It tells the model: a simple diagram →
a Mermaid block in the answer (flowchart, pie, xy bar/line, timeline, or a markdown table — *not* mindmap/Gantt, which
local models get wrong); a page, dashboard or "better chart" → ONE single-file `html` block, which Open WebUI opens as a
live **Artifact** beside the chat, with an exact Chart.js skeleton to copy (a 32B writing a chart from scratch forgets
the script tag or draws on a `<div>`; given the skeleton it gets it right). The 32B draws an org chart in ~1 s and a
dashboard page with a real log-scale bar chart in ~40 s — no tool. Chart *tools* ask the model to pack a program into a
JSON string — the one thing local models get wrong.
**Web search** is the globe under the **+** menu; if it is missing, enable it in **Admin Panel → Settings → Web Search**
(DuckDuckGo, no key) — Open WebUI keeps that switch in its database, and a migrated database may have it off
regardless of the compose file's defaults. *(The older "Default" mode makes Open WebUI's Task Model pick tools — it asks the model which tool to call, runs the tool's Python in its own
container, and feeds the result back — — it fumbled with the 1.5B and swallowed the 32B's call with a bigger one. Don't use it.)*

**Reading the "Thinking…" panel while a big model loads.** A pooled or remote-GPU run streams the model's weights
to the donor before the first token — GENGHIS narrates it: *streaming Qwen2.5-32B (~20 GB) to laptop-5090 … 6.4 /
20.0 GB (31%) · 27 MB/s · ~8 min left*. That progress line comes from the authority's own network socket, so it is
real, not a spinner. The **first** time a model goes to a node is the slow one; a donor started with the tensor
cache (`-c`, every GENGHIS launcher does) keeps the weights, and the next call loads from the donor's own disk (~1.5 min per
20 GB, *0.4 GB crossed the wire (cache load)*) instead of the air. The panel ends with the measurement — *done · 6.4 t/s generation ·
18.2 t/s prompt · 113 s wall · laptop-5090* — and the same row lands in `runs.jsonl` (`strategy: v1_stream`). If it says
*working (N s)* with no byte count, the authority is not Linux (no `ss`) — it is still loading, just blind.

## Say what you want done — roles (D48–D50)
Besides the speed settings, the model list shows **roles**: `genghis-researcher`, `genghis-coder`, and any you add. Pick
one like any model. A role is a way of working (its own instructions, the tools it may use, a folder of documents it
reads, and a speed setting), and GENGHIS picks a model that can do it. If nothing on your fleet can, it refuses and
says why instead of half-working.
```powershell
py genghis_coordinator.py roles                                  # what each role would run right now
py genghis_coordinator.py roles researcher                       # one role in full: model, tools, knowledge folder
py genghis_coordinator.py roles researcher "your question"       # preview the passages it would read for that question
```
- **Your own roles** live in your home folder (`GENGHIS_HOME`), never in the repo. The fields, and how to write one:
  [`home.example/README.md`](home.example/README.md).
- **Which model a role runs:** list its favourites in `"prefer"`; the first one your library holds runs, and the Thinking
  panel says which and why. A role that drives a program (the Blender role) needs a model that makes real tool calls.
- **Let the Researcher look things up:** switch on `adapters/web.json` in your home. It searches DuckDuckGo and reads
  the pages it cites, public internet only. It needs a model that fits one card (tools don't run on a split).
- **Blender on another machine:** the Blender adapter's `"node"` names the box Blender runs on, and GENGHIS relays to it,
  so Blender's own port never opens to the network. Switch it on in that box's copy of `adapters/blender.json`.
- **Give a role your documents:** add `"knowledge": "~/papers"` to its file. Each question is searched against that
  folder and the best passages go to the model, tagged `[K1]`… with file and page. It's keyword search, so ask with
  the words your documents use. PDFs need `pypdf` in the serve's Python: the installers offer it and `verify` checks it.
  A small model may use a passage without citing it; pick a larger one for a role that must cite.

## Attach a document to a chat
- **In Open WebUI:** drop the PDF into the message box. Open WebUI reads it itself, inside its own container, and
  sends GENGHIS the text.
- **From any other app that attaches the file itself** (an OpenAI `file` part): GENGHIS reads it before the model sees it,
  PDFs page by page and text files as they are, up to about 40,000 characters each. The Thinking panel says what
  happened to each attachment. If it can't read one (no `pypdf`, a scanned page, a photo), the answer says why and how
  to fix it. An attachment never makes the chat fail.

## Is this box OK? — `verify`
```powershell
py genghis_coordinator.py verify                      # the box you're on   (Linux: python3 genghis_coordinator.py verify)
py genghis_coordinator.py verify <node-id> --bench    # from the authority: every layer on that node, at what speed
```
Read-only, safe to run any time. It checks that the box is in the fleet and up, built from the pinned llama.cpp,
lending its GPU for real, on a wired link, and able to read PDFs and its roles' folders. Each problem comes with its
fix. It ends with **the addresses worth bookmarking**, checked rather than guessed, and saved to
`~/genghis-addresses.txt`. What each check means: [`docs/agents/when-verify-fails.md`](docs/agents/when-verify-fails.md).

## The model library (on the authority)
The coordinator **is** the model repository — stage GGUFs once, run them from anywhere by name.
```bash
py genghis_coordinator.py models                       # list the repo + your local cache
py genghis_coordinator.py models pull <name.gguf>      # download one into your local cache (resumable)
```
Stage a new model: `scp your-model.gguf <you>@<AUTHORITY>:~/genghis-src/poc/models/`. Any client can then `run` it by
bare name (fetch-if-missing, resumes if the transfer drops). *(Reference repo: the 1.5B, 32B, and 70B.)*

## What the four goals mean now (reference fleet, 2026-09-23)
| you pick | model | where it runs | feel |
|---|---|---|---|
| `genghis-balanced` | Qwen3.5 9B, thinking off | the NUC's 5060 Ti, resident | a full answer in 3-5 s (~61 t/s); reads pictures |
| `genghis-fastest` | Qwen2.5 1.5B | resident on whichever host you hit | sub-second; Open WebUI's titles and tags use it |
| `genghis-fit` | Qwen2.5 14B | the NUC's 5060 Ti, resident | ~41 t/s once loaded; no pictures |
| `genghis-biggest` | Llama 3.3 70B | pooled across the fleet: no single box could hold it | minutes to load, ~2 t/s; capacity, not speed |

And the roles: `genghis-researcher` (Qwen3.5 9B, thinking on, searches and reads the web itself: 20-45 s), `genghis-coder`
(Qwen2.5-Coder 32B, split across the 5060 Ti and the Arc: ~4 t/s), `genghis-blender` (Qwen3.5 9B, drives Blender on the
laptop). Each goal's model is a choice in config (`goal_models`), set in the Control Room.
The *Thinking…* panel always tells you which of these happened ("handing this to laptop-5090 …", "solo on nuc-155h", "pooled across …").

## Themes for the chat window
Eight looks ship with GENGHIS — six near-black: **Khan** (gold on black), **Blackwell** (graphite + lime), **Rizen' Up** (plum +
signal red), **Arc Light** (teal + cyan), **Steppe Night** (slate + lavender), **Overclock** (crimson + electric yellow) — and two
mid-tone, daylight-studio ones: **Tundra** (slate blue-gray + steel blue, a gold note) and **Foundry** (mid graphite + sky blue,
a warm-red note) — plus **Ordu**, a 60/30/10 palette (cinema's rule: a warm-charcoal canvas, cool blue-slate panels as the
complementary 30 %, one ember accent on the 10 % you act on; built to be easy on the eyes for long sessions). **Ordu is the default** (a first visit gets it); pick another in **Settings → General → Theme** — they sit in a **GENGHIS** group under Open WebUI's own
entries (choosing one puts Open WebUI on Dark underneath; choosing a stock entry turns ours off). Per browser. A preview
gallery lives at `http://<ui>:3080/static/genghis-themes.html`, and a `?genghis_theme=<name>` bookmark applies one
(`none` resets). They re-tint Open WebUI's gray ramp and add one accent, so contrast is preserved; keep Open WebUI's own Theme on
*Dark*. Mechanism: Open WebUI links `/static/custom.css` and runs the shipped-empty `/static/loader.js` on every page —
`integrations/openwebui/themes/` overlays both (compose binds). Add your own in `gen_themes.py` (one line per theme).
After a theme-file update, hard-refresh once (the browser caches the stylesheet).

## Warm models stay warm (D38)
A host keeps **several** models warm at once — as many as its card holds — and only unloads the least-recently-used
one when a new model genuinely doesn't fit beside them. The home base keeps `fit` (14B) and `fastest` (1.5B) both
warm; the laptop keeps the 32B warm and, rather than unloading it for a small request, hands that request to a host
that already has the small model warm. The *Thinking…* panel says which happened: *"… is warm here"*, *"loading …
(first time; it stays warm after this)"*, *"loading … — making room by unloading …"*, or *"… is warm on nuc-155h;
loading it here would evict … — handing this to nuc-155h instead"*. `/registry.json` → `resident.pool` lists them.

## The warm model's context (D32) — and how to get more of it
The *Thinking…* panel says `processing prompt (context N tokens)`. N is chosen per host to fit the card (weights + KV
cache + headroom) and grows on demand when a conversation outruns it. Two per-box keys in that host's `config.json`:
- `"resident_kv": "q8_0"` — quantize the KV cache: **halves its size at negligible quality cost**, so a full card gets
  the next step up (the 32B on a 24 GB card: 8k → 16k). Recommended; the reference fleet runs it on every host.
- `"resident_ctx": 16384` — pin a size outright (it will try even if it doesn't fit; prefer `resident_kv`).
**When a conversation outgrows the card (D43):** the chat is not stopped. The panel says *"this conversation is 19043
tokens; this card alone holds at most 16384 — asking the fabric for a bigger window… re-planning the same chat across
laptop-5090 + nuc-5060ti"*, the model comes back warm across those cards with a window sized for the conversation, and
the chat continues — slower per token (the wire), but it keeps going. The next chat that fits the card alone brings the
model back to it (*"dropping back to this card alone"*). The ceiling is the model's trained context (32k for Qwen2.5);
past that the message is still "start a new chat, or shorten this one".

## Keeping your GPU, and being away (D35)
- **Blender / Resolve day:** on that box, `py genghis_coordinator.py fleet lend off` (or the **Lend off** button on its row in
  the Control Room). The fleet shows it as **HELD**, the planner and delegation leave its GPU alone, and it stays a full
  host for its own chats. **Lend off also reclaims the card:** any pooled model *another* host keeps a shard of there
  is unloaded at once, and the note says which and where to put it instead; the box's *own* warm models stay. Stated
  plainly: a chat mid-answer on a reclaimed shard is not guaranteed — this is the switch for *"taking the laptop for the
  weekend: move things where they need to be, lend off, go"*, not for flipping mid-sentence. `fleet lend on` (or
  **Lend on**) gives it back; nothing moves until the next plan asks.
- **Sleep:** a Windows box that is lending its GPU **on AC power keeps itself awake** (the screen still dims on your
  normal schedule) — the donor launcher holds the same "system required" flag a renderer does, and lets go within a
  minute of `fleet lend off`, on battery, or when the donor stops. Your power plan is never edited; no more flipping it
  by hand before a long night and back in the morning.
- **Away from home:** nothing to do. A box that reaches the authority over Tailscale registers as **AWAY** — a host and
  operator (Control Room, chat, its own GPU), never a donor (no RPC round-trips across the internet). Back on the LAN it
  re-registers as a donor at logon.

## One authority, many hosts (D30)
Your fleet has exactly one **authority** (fleet + config + model library — the NUC in the reference fleet; it was the Pi until the runbook move). Every other box that
runs `serve` is an **inference host**: it reads the fleet from the authority, runs chats on its own GPU, and forwards any
change you make in its Control Room back to the authority. So the Control Room on the laptop, the NUC and the Pi all show
the same fleet, and a node registers once. A host is any box with `GENGHIS_COORD=<authority>` set (the installers set it).

## The Control Room — the main dashboard
Open **`http://<AUTHORITY>:8899/`** in a browser (D24). It is the live face of the fabric: pool summary, per-node
badges, the **goal → model** dropdowns, the **Models grid — one column per host** (each cell **● WARM + Unload**
or **Warm now**; the header line says who holds what), and a **Local Resources** hub (Open WebUI · Grafana · Prometheus · fleet admin · API reference) with
live up/down dots. Each node card has a **Retire** button (see *Nodes come and go*, below).
The original **fleet admin** (friendly names, roles, default run-type, PIN) now lives at **`/admin`**.

## What the chat window shows while it works (D31)
Open WebUI's collapsible **"Thinking… N s"** block is GENGHIS's live status — expand it: `GENGHIS · fastest · <model> · solo
on <node>`, `loading … into VRAM`, `… (9 s)` ticks, `processing prompt`, or `streaming model shards to 3 nodes over RPC` for a
pooled run. A reasoning model's own thinking follows in the same block; then the answer streams. If it says *"reloading with a
16384-token context"* that's D32 growing the warm server to fit a long conversation — one-time.

## The first chat on a new box
If the model isn't on this machine yet, GENGHIS pulls it from the coordinator's library **in the background** and the chat
answers right away with *"GENGHIS is fetching … (37%) — try again in a moment"* (Open WebUI shows that text). The Control
Room lists the download as a progress row. `serve` also pre-fetches the default/goal models when it starts, and the installer
offers to pull the starter model, so you'll rarely see it. (D28)

## Pick the model — the registry + residency
Every GGUF in your models folder(s) is selectable by name in `/v1` (D23), alongside the four goals:
```bash
py genghis_coordinator.py registry                       # list local models (size, text/VLM) + the goal→model map
py genghis_coordinator.py registry map balanced <model>  # which model "balanced" runs (also: fastest/fit/biggest)
py genghis_coordinator.py registry default <model>       # the model plain `run` uses
```
**The Pool (Control Room).** Your fleet's memory drawn as one bar per node, and your models as blocks on a shelf. Drag a
block onto a host's bar to keep it warm there; onto the strip at the bottom to keep it warm **across the fabric** (a model no
single card can hold — its weights are split over the fleet and stay there until you unload); drag a warm block back to the
shelf to unload. While you hover, the ghost is the planner's real answer: green shows exactly how much lands on each node and
what's left, red shows why it won't fit. A node holding a piece of a pooled model shows **PINNED** — it is busy for that
model, not down. No mouse? Click a model, then click where it goes. *Table view* is the same thing as a grid.
The shelf shows each model's **warm** size — weights + its KV cache at a 16k window + the compute floor — because that is
what a card has to find (a "2.6 GB" model with a big KV cache is 7.4 GB warm; hover for the on-disk number). Every piece
of a pooled model shares a colour, a **bracket** in the gutter ties them, and a legend under the bars says what is across
what. While a placement loads, the line under the bars shows what has actually crossed the wire (*6.4 / 21.1 GB · 80 MB/s ·
~3 min left*; a cache load says so instead of inventing an ETA).
**Formations.** Above the bars: your saved layouts of *what is warm where*. Place your models by hand, then **+ save
current as…** (e.g. *Work* = the 32B on the laptop + the 1.5B on the NUC; *Evening* = the 70B across the fabric). ● marks
the one in place. Click another and it lists the exact steps — unload what is not in it, warm what is — asks, then runs
them with live progress per host; a failed step stops the switch and says why. A layout is not a lock: a chat may warm a
small model beside it, and that does not un-mark it.
**Residency** keeps a warm `llama-server` holding the model so a chat answers in seconds instead of
re-loading gigabytes every call (32B: ~15 s cold → ~3 s warm). Each host keeps a **pool** of warm models (D38) —
the oldest goes only when the card is full, and *Warm now* tells you what it unloaded to make room. Warm/unload
**any host's card from the one Control Room** (LAN or Tailscale — the authority does the reaching), or
`POST /residency {"action":"load"|"unload","model":"<id>","node":"<host>"}` (`unload` with a model = that one;
without = the whole pool). `residency:false` in `config.json` turns it off (per-request `llama-cli`, the pooled/70B path).
**Context size** is chosen for you (D32): the largest the model was trained for that fits your GPU next to the weights — 16k
where it fits, and it grows once if a conversation needs more. Pin it with `"resident_ctx": 8192` in `config.json`. If the warm
server won't start, the reason is under the model list in the Control Room and in `poc/resident.log`.

## Nodes come and go (D26 — cattle, not pets)
A node that **breaks** needs nothing from you: the heartbeat marks it DOWN, plans route around it, it rejoins
when it's back. To **permanently** add / remove / replace hardware:
```bash
py genghis_coordinator.py fleet                 # active donors + the retired graveyard + eye nodes
py genghis_coordinator.py fleet retire <id>     # park a node (reversible) — or click Retire in the Control Room
py genghis_coordinator.py fleet restore <id>    # bring it back
py genghis_coordinator.py fleet remove <id>     # hard-delete it (and its names/roles)
```
New hardware joins by running its role installer ([`install/`](install/)) — it registers itself with the coordinator.

## Use it from anywhere
Two ways, both with every port still closed to the world — pick one in [INSTALL.md](INSTALL.md) →
*Reach it from anywhere*:
- **Tailscale** (what we run): put the **home base** on your tailnet and open `http://<tailnet-ip>:8899` from your
  phone. No domain, no DNS change; the visitor needs the Tailscale app.
- **Cloudflare Tunnel + Access**: `https://genghis.yourdomain.com` behind a login at the edge — for people you will
  not put on your network. Needs a domain on Cloudflare.

## Find the coordinator automatically
Clients **auto-discover** the coordinator on the LAN (mDNS) — no IP needed. Check it with
`py genghis_coordinator.py discover`. To pin one explicitly: `GENGHIS_COORD=<host>` (or the full
`GENGHIS_FLEET_URL`).

## See the fleet
```bash
py genghis_coordinator.py heartbeat     # one-shot: who's up, latency, reliability
py genghis_coordinator.py monitor       # live watch — drops & rejoins as they happen
py genghis_coordinator.py view          # the Operator View TUI (colored live fabric)
```

---

## HEARTH — the TV surface
Requires the coordinator's `serve` running (it is, if the Pi is on). The TV app polls it.

- **Leave a message on the TV** — edit the presence message on the coordinator:
  ```bash
  ssh <you>@<AUTHORITY> "cat > ~/genghis/hearth_message.txt" <<'MSG'
  # a headline
  the body of the message the TV will show, in firelight.
  MSG
  ```
- **Ask a question the person answers with the remote** (listen-back) — add `?? …` and `= …` lines:
  ```
  # a gentle check-in
  Whenever you pass by, I'll ask how you're doing.
  ?? How are you feeling today?
  = Good
  = A bit tired
  = Not great
  ```
  They answer on the TV with **UP/DOWN + OK** (misfire-forgiving: it debounces, and "you can still change it").
- **Review the answers over time** (the healthcare record):
  ```bash
  py genghis_coordinator.py checkins      # dated question → answer, newest first
  ```
- On the TV: **LEFT/RIGHT** toggles the HEARTH message ⇄ the live FABRIC; **UP/DOWN** on the fabric switches
  the run-type; **BACK** exits. **Settings** (to point the TV at a coordinator manually) is reached with the
  **MENU/RED** remote key or **▲** on the Hearth view — but the TV normally **auto-detects** the coordinator
  and **pulls its name + default goal** from it, so you rarely need it. See **The Fold** *(coming soon, with HEARTH)* for the guiding concept.

---

## Lock it down (optional)
By default the coordinator is **open on your LAN**. To require a PIN: web admin → **🔒 PIN**, or
`POST /config {"auth":{"token":"…"}}`. Levels (`auth.protect`): `writes` (default once set — guards config
changes) or `all` (guards everything, for off-LAN via a tunnel). Clients then send `GENGHIS_TOKEN`. Full
details + lockout recovery in [`docs/COMMANDS.md`](docs/COMMANDS.md) → *Auth*.

## Watch it in Grafana (optional)
The coordinator exposes Prometheus metrics at `GET /metrics`. One command stands up a dashboard:
```bash
cd monitoring && docker compose up -d      # Grafana → http://localhost:3000 (GENGHIS Fleet)
```
Includes a toggleable **"donor down"** alert. See [`monitoring/README.md`](monitoring/README.md).

---

## Did anything die overnight? (the watchdog)
Look at any Control Room header: **verified HH:MM · 7/7 nodes** — an independent pass on the authority every 15 minutes.
A ⚠ means two passes were missed (the authority itself is down). The history is one line per pass:
```bash
tail -20 ~/genghis/watchdog.log        # on the authority
```

## Keep it healthy
- **Everything auto-starts at boot** (coordinator `serve` + each donor's `donor-serve.sh`/`donor-report.sh`
  via `@reboot` cron). After a power-cut, just wait ~a minute and `curl http://<AUTHORITY>:8899/fabric`.
  On a **Windows** `serve` node (the fast 5090 path), [`poc/serve-laptop.ps1`](poc/serve-laptop.ps1) + a
  per-user **Startup** launcher does the same — crash-restart loop + auto-start at logon, no admin needed.
- **Restart the coordinator** (rarely needed): `ssh <AUTHORITY> "pkill -f 'genghis_coordinator[.]py serve'"` — the
  loop respawns it. The `[.]` matters: a plain `genghis_coordinator.py serve` pattern also matches the SSH shell running
  the command and kills your own session; `[.]` still matches the serve but can never match its own command line.
- **Restart a donor**: `ssh <donor> "pkill -f 'ggml-rpc-serve[r]'"` — its `donor-serve.sh` loop respawns it.
  (If nothing respawns, the service loop isn't running — start `donor-serve.sh` per [INSTALL.md](INSTALL.md) §2.)
- **A node shows DOWN?** Its donor service isn't running, or port `50052` is blocked, or it built a
  different llama.cpp commit — see [INSTALL.md](INSTALL.md) → Troubleshooting.
- **A Linux GPU host lost its GPU after a reboot** (every model: `llama-server exited (code 1)`)? The user isn't
  in the `render`/`video` groups — it only ever worked through the desktop login's temporary ACL. `sudo usermod
  -aG render,video $USER`, log out and back in. `install-linux.sh --preflight` now catches it up front.

## The mental model (so it's never mysterious)
- The **coordinator** (the NUC in the reference fleet) is the always-on **authority** — it holds the one true fleet state and serves it.
- The **client** fetches that, plans, and drives the run from the machine holding the model + `llama-cli`.
- **Donors** just serve `ggml-rpc-server`; they hold shards during a run — and compute those shards on their own GPU,
  every token. What they lack is the *engine* (a host's `serve`), so they can't hold a model warm or answer on their own.
  Head + hands vs hands only — [INSTALL.md → Hosts and donors](INSTALL.md#hosts-and-donors--head-and-hands-read-this-once).
- Everything **self-reports** its live capacity, so what a run *uses* is what you *see*.
