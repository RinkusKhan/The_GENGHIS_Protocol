<!-- GENERATED — do not edit by hand.
     Source: the project's private DECISIONS.md; generator: poc/dev-tools/make-public-decisions.py
     Regenerate after every decision:  python3 poc/dev-tools/make-public-decisions.py -->

# GENGHIS — Decision Log (public)

Short, dated records of the design decisions behind GENGHIS, so they don't get re-litigated — and so that anyone
running it can see *why* a rule exists before deciding to change it. Every entry states the decision, the reason,
and (where there was one) the measurement that settled it.

This is the maintainer's working journal with the reference lab's own names, addresses and account details
redacted — role words ("the authority", "a donor host") stand in for machine names. Nothing else is removed: the
reasoning and the measured numbers are the point of the document.

Newest at top. See also [PROJECT_CHARTER.md](PROJECT_CHARTER.md), [README.md](README.md) and
[INSTALL.md](INSTALL.md).

---

## D58 — A chat goes to the box that runs its model clearly faster: cards compared in GB/s, loads in seconds (2026-10-03)
**Context:** the planner's own-card rule (D9 addendum, 2026-09-13) runs a model on this box's card whenever it fits. It
was written to stop a box driving a REMOTE card over RPC (the model re-sent, a round trip per token), but it also
stopped the other, network-free option: handing the whole chat to a box that runs the model on ITS own card (D34).
Measured 09-30: Qwen3-Coder-30B, 24.6k-token prompt -- the NUC's Arc 763 s to the first token and 4.7 tokens/s, the
laptop's 5090 13.7 s and ~43 tokens/s. The maintainer chose the rules (10-03): hand over at **2x or faster**; the laptop takes
hand-overs **unless Lend is off**; a model **warm on a slow card still moves**.

**Decision 1 — compare cards in GB/s, not tokens/s.** The stored tokens/s figures came from whatever model ran (the
5060 Ti "228 t/s" on a 1.5B, the 5090 "27 t/s" on a big one): useless for comparison. Generation is bound by memory
speed, so tokens/s x the bytes read per token (from the GGUF's tensor table: every weight but the input embedding, and
only `expert_used_count/expert_count` of a mixture-of-experts model's experts) is roughly the same figure whatever model
ran. Every answer from a warm model on one card teaches that card (llama-server's own `timings`, EMA 0.3; answers under
16 tokens are skipped); `fleet bench` sets every GPU's figure in a few minutes (the model warm there, or Qwen2.5-14B
loaded and unloaded). Measured 10-03: Arc ~50, 5060 Ti ~353, laptop 5090 ~537 GB/s. Approximate: a mixture-of-experts
model uses a card's bandwidth less well than a dense one, and the figure averages both (the 5090 read 384-537 across
the day) -- the ratios (8-11x Arc vs 5090; 1.1-1.5x 5060 Ti vs 5090) stayed on the right side of 2x.

**Decision 2 — the hand-over rule.** When the plan runs a model on this box's own card (or its eGPU) and the request
was not itself handed over, compare with every other host's box (its anchor and any eGPU there) that is up, Lend on, not
held by a home service, has the file, has room, and would not push out a bigger model kept warm there. A card at least
2x faster takes the chat. No figure, no hand-over (never on a guess). A model the operator placed on this card (D57) stays.

**Decision 3 — a load counts.** Handed over cold, the model first loads from the other box's disk; the laptop's models
sat on a USB hard disk (~47 MB/s), and a 17.7 GB model took 370 s -- a short question lost to answering on the Arc. Each
box now learns each model folder's load speed from its own loads (`poc/load_speed.json`) and its registry estimates
`load_s` per model. A cold hand-over happens only within 60 s; slower or unmeasured, the chat is answered here and the
faster box loads the model in the background (not a chosen placement) so the next chat goes there warm. Measured: the
same model from the laptop's internal SSD loads in ~3 s (1,286 MB/s) -- handed over cold, answered in 15 s.

**Decision 4 — models live where they run.** The authority keeps the library; a fast host keeps the few models it
should run on fast local storage (the laptop: `D:\GENGHIS-models`, its NVMe). A host's own settings (`models_dirs`,
`residency`, `resident_ctx`, `resident_kv`) are now set ON the host by `POST /config`; they used to be forwarded to the
authority with everything else, so a host's model folders could not be changed from its Control Room.

---

## D57 — A model placed on a card is planned for THAT card (2026-10-03)
**Context:** the maintainer: *"Can you fix Genghis so that I can put the 14B Coder back on the NUC/5060ti? It no longer works."*
and *"I have plenty of room for it on the NUC's Intel ARC, that doesn't do it either."* Since D46 (when the 5060 Ti
became an eGPU on the NUC), neither of the NUC's columns could take a model: "Qwen2.5-Coder-14B is too big for this host's card
(needs ~11 GB, the authority has ~21 GB usable)". Two bugs: a warm request naming a card was planned for the whole box
(D46 makes the same-box eGPU win on speed, so the plan went to the eGPU over loopback), and `warm_model` accepted only a
plan on the host's own card, so it refused; and a refusal for the named card ("runs no serve of its own") was set and
then overwritten, because the handler fell through to that whole-box warm.

**Decision 1 — the card that was chosen is the card that is planned.** A warm with a `node` (a Control Room column, a
formation step) plans over that card alone. If the model does not fit there it says so with the numbers; it is never
silently put somewhere else.

**Decision 2 — the card's name travels.** Every forwarded warm/unload keeps its `node`, so the host it reaches plans for
that card and not wherever its own planner prefers. A card with no serve of its own (an eGPU) is sent to the host in its
box (same address), which serves it over loopback as D46 already does for chats. A real refusal ends the request.

**Decision 3 — "already warm there" is an answer, not a plan.** The card's RPC endpoint is held by that very model, so a
fresh plan would exclude the card and report a failure.

---

## D56 — Home services: programs the owner starts and stops from the Control Room, and a GPU has one owner at a time (2026-09-26)
**Context:** the maintainer: *"should we have 2 additional cards in the Control center, one that has the load/unload
personaPlex [...] That way, you know it can't be loaded if the 5090 is occupied with a model already & the inverse as
well."* PersonaPlex (a live speech-to-speech model, ~19.4 GB) runs in WSL on the laptop and needs nearly all of the
5090's 24 GB, the same card GENGHIS keeps a chat model warm on and lends to pooled models over RPC. Starting it by
hand meant a terminal command, and nothing stopped GENGHIS from placing a model on the card while it ran (or it from
starting on top of one). An upscaler card was raised too and deliberately left for later: it is a job, not a switch.

**Decision 1 — a service is one file in the home of the box that runs it (`<home>/services/<id>.json`).** `start` and
`stop` commands, a `probe` address that answers while it runs, an `open` link, and the card's words. Like adapters
(D49), only an id and "start"/"stop" ever cross the network; the command is whatever the running box's own file says.
The Control Room on any host lists every host's services (each host answers `/services.json?local=1` for its own) and
relays a click to the box that has it. Browser: admin role; another GENGHIS host: accepted as a relay. The repo carries
the mechanism and a switched-off example; a person's services never enter it (the maintainer: *"this part [...] is just for
our home install"*).

**Decision 2 — `"gpu": true` means the card has ONE owner at a time: the node's `gpu_hold`.** While a GPU service runs,
The authority records `gpu_hold: <service name>` on that node: the planner leaves it out (`live_donors`, even for its
own host), hand-overs skip it, pooled shards other hosts kept there are reclaimed (as Lend off does, D44), warming a
model onto it is refused with the reason, and the Fleet table shows HELD with "GPU in use by ...". The other direction:
Start refuses while GENGHIS holds a model on that box and says what; the card asks, and "free it" unloads those models
first. Lend off (D35) was not enough: it keeps a card for its owner's OWN GENGHIS work, where a service needs it empty.

**Decision 3 — the record follows reality, not the clicks.** A beat (15 s) on every serve holds the card for a service
found running that was started some other way, and gives the card back when a service stops some other way. A service
that does not answer within `start_s` of starting says so on its card.

**Decision 4 — "stopped" means the program is gone, and the stop command's exit code decides.** Found on the first real
stop: PersonaPlex closed its port on SIGTERM and then hung in shutdown with all 19.4 GB still on the card, so "no longer
answering" had reported success while the GPU stayed full. The stop command must not return 0 until the process has
exited (the home file's stop escalates to SIGKILL after 8 s); a non-zero exit keeps the card held and says so.

**Addendum 2026-10-01 — a service's output goes to its `"log"` file, opened by the serve.** On Windows the serve starts a
service DETACHED (it must outlive a serve restart), and a `cmd /c ... > file` redirect inside the command then reaches
the program as nothing: ComfyUI's card logged 0 bytes. The serve opens the file and hands it to the program as its
stdout/stderr. Addendum 2026-10-03: keep a card's command short; a long `bash -lc "..."` with nested quotes was mangled
on its way through Windows' command-line quoting into `wsl.exe` and bash never ran: put the steps in a script.

---

## D55 — Roles that act on a workstation: MCP tool servers and fenced files, each on an allow-list (2026-09-26)
**Context:** the maintainer, after testing the roles: *"The Coder needs to be able to code, not just put examples in prompts.
It'll need access to Visual Studio, hard drives, etc."* The Coder had no tools at all; a role could act only through
the two adapters that existed (Blender, the web). Visual Studio already exposes ~56 tools through an MCP server on the
laptop (build, Error List, documents, Roslyn navigation, the whole debugger), and the codebases live on the laptop's
drives.

**Decision 1 — one generic door for tool servers (`"transport": "mcp"`).** GENGHIS speaks the Model Context Protocol
(streamable HTTP, JSON-RPC; replies as JSON or SSE; a session that is re-opened when the server forgets it) to a server
the adapter file names by `url`, stdlib only. Every MCP server is then one small file, not new code.

**Decision 2 — an MCP adapter offers only what its `allow` list names; none named, none offered.** A server's tool list is
the server's choice; what reaches a local model is the owner's. Two reasons, both measured before: a local model shown
dozens of tools calls them instead of answering (D37, ~30 calls for an org chart), and each tool is something a model
can do on a desktop. This is D49's "the model picks the verb; a human wrote the sentence" in MCP form. The shipped
Visual Studio file allows build, build status, the Error List and navigation; not document writes (files do that, with
backups) and not the debugger.

**Decision 3 — files are a built-in, fenced adapter (`"transport": "files"`).** It touches only the folders its `roots`
name; every path is resolved to its real location before the check, so a symlink or `..` cannot leave; key and
credential files (`.env`, `*.pem`, `id_rsa`, `.ssh/`, …) are refused whatever the roots say. Reading, listing, finding
and searching are the default. Writing is a separate switch (`"write": true`), and even then every write keeps the
previous version in `<root>/.genghis-backup/<time>/` and returns the unified diff, so the chat shows what changed and it
can be put back. There is no delete, and `.git` is never written. A preview that waits for a human was considered and
not built: a chat turn has no place for the person to click "apply" between two tool calls, and a backup plus a visible
diff gives the same safety without pretending otherwise.

**Decision 4 — web and local access belong in different roles, and GENGHIS says so when they meet.** A role that can
both fetch pages and read files can be steered by a page it reads into putting what it read in the address of the next
page it fetches. The adapters do not forbid the combination (it is the owner's home), but the chat warns every time.

**Decision 5 — a role sets its own tool-round budget (`max_rounds`, default 6, at most 24).** A coder reads, edits,
builds, reads the errors and edits again; six rounds is a Blender command, not a build-fix loop.

**Decision 6 — the Coder's fence is a workspace the owner points, not a list the owner edits (the maintainer, same day).**
*"Set it to D:\Coder and keep projects there, separated by project folders; when done archive it and start a different
one"* — and *"if I had a USB in and was doing a small correction, I could switch the folder for that session to
F:\Project."* So the files adapter has a workspace mode: one `root`, changed from the Control Room at any time (checked
on the box that has it; recent roots are one click), and a `project` inside it (or the whole root; or a new one). The
fence is the project alone, and backups live inside it. Some folders are never a root: a system drive's root, the OS and
program folders, the whole user folder and its AppData, any `.ssh`, and any folder that holds GENGHIS's own home — a
model that could write its own adapter file could widen its own fence. Visual Studio is not fenced by this (it builds
whatever solution is open); the card says to open the solution from the workspace.

**Why this order:** it makes the Coder real first (Visual Studio + the codebase), with one generic door that every later
tool server reuses. The Researcher's analysis workspace (a sealed Python sandbox for data and charts) is the next
step and reuses the same belt, relay and allow-list rules.

**Tested:** a fake MCP server with SSE replies, sessions and pagination, which forgets its session mid-run (the client
re-opens it and retries); a tool outside `allow` is refused; an undeclared argument is dropped; `..`, absolute paths
and `.env` are refused; a read-only adapter offers no write tools; a write keeps a backup, preserves CRLF line endings
and returns the diff; an ambiguous replace is refused. Not yet run against the real Visual Studio server (it was not
open); the first run is the check.

**Follow-ups:** the analysis workspace (Researcher: data, charts); a tool-driving model on one card (tools do not
run on a split, so the Coder should answer on the laptop's 5090 or use a model that fits the NUC's 5060 Ti).

---

## D54 — The Researcher looks things up itself: a built-in web adapter that reads only the public internet (2026-09-23)
**Context:** the Researcher could only work from what it was given. Open WebUI's own web search (DuckDuckGo, now 5
results) runs *before* the question and pastes results in; useful, but the model cannot choose what to search, follow
a lead, or read the page behind a snippet. The maintainer: "take the next step".

**Decision:** a `web` adapter, built into GENGHIS (`"transport": "web"`, stdlib only): two named operations, `search`
(DuckDuckGo's no-script results page; Wikipedia's search API when DuckDuckGo gives nothing or refuses) and `read_page`
(one http(s) page to plain text, the page's `<main>`/`<article>` when it marks one, a PDF through pypdf when present,
about 9,000 characters). It rides D49's machinery: switched off in the shipped file, on in the person's home, offered to
any role whose belt names it, at most `ADAPTER_MAX_ROUNDS` (6) rounds per answer.

**The one safety rule it cannot break: public internet only.** Every address the host resolves to must be global; a
private, loopback, link-local or reserved address is refused before the request and again at every redirect (tested:
The NUC's own `/fleet.json`, `localhost`, `169.254.x.x`, `<node-ip>`, `file://`). A page, or a model a page misled,
cannot reach the router, the NUC's admin, or anything else on the LAN. Page text reaches the model marked as material to
weigh and cite, not instructions.

**Measured, first live run:** the Researcher on Qwen3.5-9B (NUC 5060 Ti, thinking on) searched once, read two pages,
and answered with a comparison table, both sources named and dated, and caveats, in **20 s**. It needs a model that
sits whole on one card: split runs have no tool path (D53), which is why Qwen3.5-9B leads the Researcher's list.

— A role names the models it is best at, and reaches a program on another box through that box's serve (2026-09-23)
**Context:** the maintainer: roles "load wrong models and don't work". All true, for several reasons at once. Every role ran
whatever its goal mapped to (Qwen 32B for all three), so the Blender role never used a Blender model. The Blender
adapter looked for Blender on the machine running the chat (the NUC), while Blender runs on the laptop and its add-on
listens on 127.0.0.1 only. A role with no tools told the user it had "web search engines". And underneath, three
engine bugs made role chats come back blank or refused (below).

**Decision 1: `prefer`, softer than a pin.** A role lists the model files it is best at, in order; the first one this
library holds that meets `requires` runs, and otherwise the goal picks as before. The reply's status line says which
happened and why ("its preferred model", or "none of its preferred models can run here: X is not in this library").
A pin (`model`) still means "this or refuse". Handing a chat to another host (D34) now carries the chosen model file
(`X-Genghis-Model`), so the other host runs that model instead of re-resolving the role against its own library, and
a host qualifies for a hand-over only if the file is actually on its disk. (The NUC had handed a Researcher chat to
The laptop "to load Qwen3.8-27B from its local disk"; the laptop does not have that file and ran its own pick.)

**Decision 2: the model is told which tools exist this turn.** A role's system prompt ends with the list of callable
tools, or with "you have no tools in this conversation", so the model cannot fill the gap with invented ones.

**Decision 3: an adapter can run on another fleet host (`"node"`), and GENGHIS relays to it.** Blender's add-on has one
operation, "run this Python", so its port must never be opened to the network. Instead, an adapter file names the node
where the program runs; a role on any host sends each **named operation** (arguments bound as JSON literals, as
before) to that node's serve (`POST /adapter`), which runs it against its own localhost. That node's **own copy** of
the adapter file decides whether it is switched on, and the door accepts only another GENGHIS host in the fleet (a
home LAN runs in local mode, where every visitor is admin). `GET /adapter.json?id=` answers "on, and answering?".
Measured 2026-09-23: the Blender role on the NUC (Qwen3.5-9B on the 5060 Ti) listed the laptop's scene, added a named
cube, and deleted exactly that cube, 7 to 20 s per turn.

**Decision 4: tool-driving roles need models that make real tool calls.** BlenderLLM passes the template check (its
template has a tool branch) but writes its calls as plain text (`<tools>{…}</tools>`), which llama.cpp cannot parse,
so the scene never changes. The stock Blender role prefers Qwen3.5-9B, then Qwen2.5-14B and 32B. A behavioural tool
check in the registry is the follow-up; the template check alone is not enough.

**The engine bugs found on the way (each made a role chat blank or refused):**
- **llama-cli echoes only the first 500 bytes of a prompt**, then `... (truncated)`. The stream reader waited for the
  closing assistant tag that never came and discarded the whole answer, for every prompt over 500 bytes on the
  per-request engine: every role, every multi-turn chat on a split run. It now starts after the truncation marker.
- **Two llama.cpp builds on one box:** binaries were found CUDA-first, so once the NUC gained a CUDA build for its
  eGPU (D46), every plan using its own Arc died with "invalid device: Vulkan0". The binary now follows the box's own
  card (`llama_bin`); the engine's errors go to `poc/engine.log` and reach the chat instead of `/dev/null`.
- **A chat's placement on the box's own eGPU was treated as a chosen fabric placement (D39) and pinned**, so the next
  role's model was refused until someone unloaded it by hand: the "green NVIDIA model that wouldn't move". Only a
  placement made on purpose (Control Room, a formation) or one spanning other boxes pins now; the chosen flag and the
  escalation flag survive a restart.
- **A reply with no answer was a blank bubble.** A stream that ends without content now says why (the model only
  thought; it ran out of tokens; the model is still being fetched). llama-cli's inline `[Start thinking]` block goes to
  the Thinking panel, not the answer.
- **An unknown model name ran the default model under the asked-for label.** It is now a 404 naming the problem.

**Thinking is a setting, not a surprise.** A reasoning model thinks before it answers; for everyday chat that can eat the whole
budget (Qwen3.5-9B: 2,048 tokens of thinking about a heat pump, no answer; with thinking off, 5 s at 61 t/s on the
5060 Ti). Config `thinking` `{model file: bool}` sets the default, a role's `think` overrides it, and a client's own
`chat_template_kwargs.enable_thinking` wins over both. The reference fleet's `balanced` became Qwen3.5-9B, thinking
off: measured against Qwen2.5-14B (41 t/s, no vision) and Qwen2.5-32B (split across the Arc, ~4 t/s).

**Where a person's roles live:** `GENGHIS_HOME` (D36). The NUC's `serve.sh` now reads `export GENGHIS_HOME=` from
`~/.profile`, the way it already read `GENGHIS_COORD`.

## D52 — NVIDIA support: RTX 20 / GTX 16-series (Turing) and newer on one toolkit; GTX 10-series experimental (2026-09-23)
**Context:** the CUDA setup script defaulted to the retired GTX 1080 Ti's architecture, installed Ubuntu's own
`nvidia-cuda-toolkit` whenever `nvcc` was missing (too old for an RTX 50-series card, which AGENTS.md rule 2 forbids),
and INSTALL claimed it applied a GCC-14 host compiler and a glibc patch that it never did. Meanwhile the reference
RTX 5060 Ti proved the simple path: **CUDA 13.2 from NVIDIA's repository builds cleanly on Ubuntu 26.04** with the stock
compiler and no patch (D46).
**Decision (the maintainer's):** support NVIDIA cards from **RTX 20 / GTX 16-series (Turing, compute 7.5)** up: 20, 30, 40 and
50-series and the datacenter cards of those generations. He first set the floor at the 30-series. Adding Turing
costs nothing, because CUDA 13 still builds for it and current drivers support it, so every supported card follows
**one rule: CUDA 13.2 or newer, from NVIDIA's repository**. **GTX 10-series (Pascal) is experimental:** CUDA 13 dropped
it, it needs CUDA 12 and a driver of 570 or older (D4), and that path has never run here. The maintainer still owns the GTX
1080 Ti, so it will be proven or dropped by a test, not a guess. Running it through Vulkan instead of CUDA is the
other path to try. **Maxwell and older: not supported** (CUDA support is ending; most have 4 GB or less).
**What enforces it:** `donor-setup-cuda.sh` reads the card's architecture from the driver, refuses a card older than
Turing with the reason (`GENGHIS_ALLOW_OLD_GPU=1` builds anyway, untested), finds NVIDIA's `nvcc` under
`/usr/local/cuda/bin`, and if CUDA 13.2+ is missing it **stops and prints NVIDIA's exact commands**. It never installs
Ubuntu's toolkit. Adding NVIDIA's repository is the person's to run, just as the driver already was. `verify` WARNs on
a card older than Turing and on a toolkit older than 13.2. **Why the thesis cares:** GENGHIS exists to put hardware
people already own to work, and 8–11 GB cards from a few generations back are exactly what sits in drawers. The
floor is set by what can be supported honestly, not by what is newest. Ties to [D4], [D46].

---

## D51 — The authority keeps its own record current, and a donor's memory is what its device says (2026-09-22)
**Two decisions, found together.**

**1 · The serve owns liveness, and nothing saves a stale copy.** The saved fleet record went stale in three ways, all
found on one evening. Browser polls heartbeat without saving, by design: a read must not rewrite the source of truth.
The watchdog was the only regular saver, every 15 minutes, and it wrote status and "last seen" but not the latency it
had just measured, so a Pi moved to a cable still read 264 ms and the Tegra 1036 ms (really 2.3). And every writer did
load, modify, save of the *whole* file on a threaded server with no lock: a heartbeat or a run held a copy loaded
seconds or minutes earlier, so whichever saved last silently undid the reports, registrations and shard bookings
that landed in between. The watchdog, a separate process doing the same, made it worse every 15 minutes.
**Decision:** the authority's serve re-measures every node every minute on a background beat (never on a request
path) and saves **only** the liveness fields, merged into the file as it is now (`merge_into_fleet_file`). Every
in-process writer takes one lock. The run and calibration paths save only what they measured. The watchdog stays the
independent witness at 15 minutes (it is the one thing that can see the serve itself die) and writes the file **only
when the serve is down**. Shortening the watchdog was considered and rejected: it would have made the race 15× likelier.

**2 · Memory is measured from the device, never from a side channel.** The NUC's Arc (Vulkan) was planned with 16,311 MB:
its eGPU's VRAM, because the box's capacity reporter asked `nvidia-smi` and filed the answer under the box's main node.
Looking closer showed it was worse than one box: `init` never recorded a memory figure for a Vulkan GPU at all, so on
every Intel Arc or AMD install the planner started at a capacity of **zero** until something else wrote a number.
**Decision:** a donor's memory comes from its own `ggml-rpc-server` (llama.cpp's RPC `GET_DEVICE_MEMORY`, i.e.
`ggml_backend_dev_memory()` for the device it actually lends): VRAM for CUDA, the shared heap for a Vulkan iGPU, RAM for
a CPU donor, in the same terms llama.cpp allocates by. It's spoken directly (HELLO with no transport upgrade, then the
query; stdlib only), at most every 10 minutes per node, and never for a donor holding a pooled shard: a
single-client server would make the probe wait out its timeout. A GPU node's `vram_total_mb` is then marked
`vram_source: rpc`, and the authority **refuses** a reported VRAM total for any non-CUDA node and for any node whose
device has answered. So an old reporter on anyone's install cannot bring this back. `init` also reads a Vulkan
device's size from `--list-devices`, and the reporter sends VRAM only for the CUDA device its node lends.
**Measured on the live fleet by the probe:** Arc 23,163 MiB (it had 16,311), Pi 5 7,932, Pi 4 3,796, Tegra 3,956, the
laptop's 5090 24,435. The Arc's planning capacity went from 15,006 MB to 21,309 MB. Ties to [D39] (pooled shards),
[D41] (link class), [D46] (a second card is its own node).

---

## D50 — A role reads its knowledge folder: keyword retrieval first, in the stdlib, and it says what it could not read (2026-09-22)
**Context:** D48 defined a role as `base + prompt + belt + knowledge + goal`, and after D49 knowledge was the only part
that did nothing: a role with a `knowledge` folder answered as if the folder didn't exist, and said so on every
request. It was the part the public stock Researcher needed to be real, and the prerequisite for the maintainer's own
research role (which stays private: D48's wall, the vertical is data, not code).

**Decision 1: the first slice is BM25 keyword retrieval, pure stdlib.** No embedding model, no vector store, no new
dependency, no index file to go stale. The folder is chunked (180-word passages overlapping by 40, so a sentence cut at
a boundary is whole in one of them), indexed in memory, and re-indexed only when a file's size or modification time
changes. Each request is searched with the user's latest message; up to **4 passages, at most ~6,000 characters**,
are handed to the model. Embeddings through the resident server are the upgrade path, not the prerequisite: keyword
search is honest about what it is (it finds shared *words*, not shared *meaning*; a synonym misses), and it works
today on every box, a Pi included.

**Decision 2: the passages go inside the role's own system message, tagged for citation.** Not as a second system
message: several chat templates render a system message only at position 0, and a passage the template silently
drops is the D48 tool-result failure again, with nothing to notice it. Each passage is tagged `[K1]`…`[K4]` with its
file (and page, for a PDF), and the block tells the model to cite by tag and to say so when the passages don't answer.
A delegated chat (D34) carries the block with it, so the folder has to exist only on the host the client talks to.

**Decision 3: never silent about what was not read (D31).** A researcher's papers are mostly PDFs, and "the model read
your folder" must not quietly mean "the model read the three `.md` files in it". Plain text, Markdown, reST, CSV, JSON
and HTML are read by the stdlib; **PDFs need `pypdf`, which is optional**. Without it, every PDF is counted and reported,
as are scanned PDFs with no text layer, unsupported types, oversized files (8 MB) and anything past a 2,000-file cap. The
report appears in the role's note on every request, in `/roles.json` (`knowledge_status`), and in `genghis roles <id>`.
`genghis roles <id> <question>` previews exactly which passages the model would get. A missing folder, or a question
that shares no keyword with anything, adds nothing and says so. Retrieval that fails never takes the chat down.

**Verified end-to-end on the live authority:** a temporary role pinned to the warm Qwen2.5-1.5B, whose folder held
one made-up fact (a lighthouse keeper's name) and one unrelated file, was asked about the fact through `/v1`. The
serve logged `knowledge: 1 passage(s) from lighthouse.md` (the right file), and the model answered correctly in 0.5 s.
The same model **without** the role invented a confident, wrong answer. **Honesty note from the same run:** the 1.5B
used the passage but ignored the instruction to cite it by tag. Citation discipline is a property of the model, so a
role that must cite should pin or require a larger one. Unit-tested before that in a scratch home: the right file
cited, a PDF and a PNG reported as skipped, nothing injected when nothing matches. Ties to [D31], [D34], [D36], [D48].

---

## D49 — Adapters make a role's belt real: the model picks the verb, a human wrote the sentence; everything is off until you arm it (2026-09-22)
**Context:** D48 left a role's tool belt *declared* — GENGHIS told the model nothing about it and the chat client
did any executing. Step 3 of the roles phase is the belt that acts. **The design was forced by the first adapter.**
Blender's MCP add-on exposes exactly one operation over its socket (`127.0.0.1:9876`, one JSON object per
direction, **NUL-delimited**): *run this Python inside Blender*. There is no safe subset to hand over, and a model
holding that primitive holds `bpy`, the filesystem and the network inside a process the user is watching.

**Decision 1 — GENGHIS never lets a model write code.** An adapter declares **named operations**; the Python body of
each is written by whoever wrote the adapter file. The model chooses *which* operation and supplies typed
**arguments**, which GENGHIS injects as **JSON literals bound to variables** above that body — never string-spliced
into source, so an argument cannot become code — and an argument the operation did not declare is dropped rather
than passed to the host. The model picks the verb; a human wrote the sentence. A raw-code escape hatch is
deliberately **not shipped**; a home that wants one adds it and accepts what it means.

**Decision 2 — off until armed, and never armed by us.** An adapter is disabled unless its file says otherwise;
a disabled or unreachable adapter contributes **no tools at all**, rather than tools that go nowhere. GENGHIS states
which of the two is missing (`genghis adapters`, `genghis roles <id>`, `/adapters.json`). Arming one is a deliberate
edit in the **HOME**, on the machine whose Blender it is. GENGHIS will not flip that switch for you.

**Decision 3 — GENGHIS runs the tool loop, but only for tools it owns.** When the model calls a tool from an armed
adapter, the coordinator executes it, appends a `role:"tool"` result and asks the model again, bounded at
`ADAPTER_MAX_ROUNDS` (6) — and when the bound is hit it *says so* instead of truncating in silence. Tools the
**client** supplied are still the client's: they are relayed untouched exactly as D37 did, and a turn mixing ours
with theirs is handed back rather than half-executed. A failed call returns `{"ok": false, "error": …}` as the tool
result, because a tool that fails quietly is precisely how a model starts inventing what it "did" (D31).

**Proven live, 2026-09-22**, against Blender 5.2.0 LTS on the laptop with the add-on running: `genghis-blender`
(role → `balanced` → Qwen2.5-32B) **read the real scene in 25 s** (one `scene_info` round), then **added a torus,
moved a mesh and re-read the scene in 14 s** (three rounds) — verified independently through a separate socket, not
from the model's own account of itself. Asked to delete an object that does not exist, it reported the failure
instead of claiming success. The shipped adapter was disarmed again before commit.

**The streaming path, same day.** A streamed tool call arrives in fragments keyed by `index` (the name once, the JSON
arguments a few characters at a time), so GENGHIS reassembles them and, when a turn ends asking for tools it owns,
runs them and keeps streaming the model's next turn — **narrating each call and its result in the Thinking panel**,
because a user watching a stalled cursor cannot tell work from a hang. With no adapters in play the relay is exactly
D37 (verified: a client's `get_weather` still streams back as `tool_calls` chunks with `finish_reason: tool_calls`,
untouched), and rebuilt calls keep their `index` so a client can still reassemble a mixed turn. One bug fixed in the
same pass: the owned-tool set was held per CONNECTION, and the handler is reused for keep-alive, so a plain-goal chat
arriving after a role chat would have inherited that role's tools — it is reset per request now.

**What this is not:** knowledge retrieval is still declared-and-inert (D48) and the voice binding is still a door.
`stream_resident` still relays verbatim; it is dead code (its caller forces non-stream) and was left alone.
Ties to [D20] (I/O adapters), [D25] (who may arm what, when a UI exposes it), [D31], [D37] (native tool calling),
[D48] (roles), [D36] (adapters are choices; they live in the home).

---

## D48 — A role is not a model: `base + prompt + belt + knowledge + goal`, required capabilities the registry can actually check, and the home/repo wall drawn before a line of it was written (2026-09-22)
**The question that started it (the maintainer, 2026-09-18):** *"the models all say text, but they are composed of different
capabilities. If I want 1 to work on my own research and work in Blender, will that be possible?"* The answer that
landed — *"I wouldn't have known the 'Role' aspect"* — is that what he wanted was never a model. It is a **role**:

> `base model + system prompt + tool belt + knowledge + effort goal`

and **two roles can share one base model**. You say *what you want done*; GENGHIS decides what runs it.

**Decision 1 — the registry learns what a model can be ASKED to do (built: step 1).** A role *requires* capabilities,
so the registry had to stop inferring them from filenames. Almost all of it is inside the GGUF already: the chat
template states whether a tools list is rendered and whether a `tool_call` can be emitted (`native`/`partial`/`none`),
and — the one that bites without a symptom — whether a `role:"tool"` message renders at all. **A model whose template
has no tool branch silently discards every tool result**: the search runs, the answer returns, the template throws it
away, and the model answers from imagination. Nothing else in the system would ever complain. Also read: trained
context, reasoning, architecture, and whether the file is a chat model at all (a `.gguf` may be Stable Diffusion,
Flux, TTS or video — same container, nothing to chat with; that is the D20 "a dropped-in GGUF just sits there"
promise, answered with a reason). Cached in `poc/caps.json` (git-ignored, keyed `path|size|mtime`) because a header
parse reads a 32 MB prefix and the Control Room polls.

**Found by it, immediately:** `balanced` mapped to **Qwen3.8-27B-OBLITERATED**, whose abliterated 506-character
template has no tool machinery at all — the one goal a research role would reach for was the one model in the
library that cannot drive tools. And projector pairing was wrong **in both directions**: the name rule called
Qwen3.8-27B (which *has* a projector) text-only and handed its 5120-wide projector to SmolVLM-500M (960 wide), a
pair that could only ever have failed at load. Pairing now runs **projector → model** on
`clip.vision.projection_dim == <arch>.embedding_length`, ties broken by the publisher's `general.basename`, and an
**unbreakable tie pairs nothing rather than guessing**.

**Decision 2 — roles live in the HOME, and the coordinator never learns what any role IS (built: step 2).** Roles are
`.json` files under `$GENGHIS_HOME/roles/`, exposed as `genghis-<role>` in `/v1` beside the effort goals, listed by
`/roles.json` and `genghis roles`. Resolution order: a pinned `model` is **honoured even if it falls short** (never
substitute a user's explicit choice in silence — report it); otherwise the role's goal picks a model, and if that one
fails the role's requirements GENGHIS substitutes **the qualifying model closest to it in size**, so the *tier* the
role asked for is preserved, and says exactly what it swapped and why. If nothing qualifies, the request is **refused
with the reason** rather than half-working (D31: never silent). A role's declared tool belt and knowledge folder are
**declared, not installed** — GENGHIS cannot reach into someone else's chat client, so it says so instead of
pretending. The response echoes the role the client asked for, not the goal it resolved to.

**Decision 3 — the wall, drawn first (D36 applied).** Three layers: the **mechanism** (public), **stock roles** in
`home.example/` (public — a generic Researcher with an *empty* belt and a Coder; a filled belt would already be a
choice of field), and **the maintainer's own roles** (private, `genghis-home`, never shipped). The maintainer's ruling:
his own roles are his alone and have nothing to do with the distribution. Consequence adopted as a build
rule: **the vertical must be data, not code.** If a personal role ever became a branch inside the coordinator the wall
would be broken invisibly — the leak audit reads private words and shapes, not structure. Building the public
mechanism *first*, proven against a generic Researcher, makes his private role pure configuration by construction.
This is what finally forces `GENGHIS_HOME` to stop being "(planned)" in D36.

**Decision 4 — vocabulary.** "Role" from here on means *a way of working*. The four things a **box** can be
(Authority · Host · Donor · Surface) are **"node capabilities"** — which is what [D35]'s own title called them
(*"Roles are capabilities, not chairs"*). `config.roles{}` keeps its key; nothing migrates.

**Decision 5 — the door for the voice.** `caps.engine` exists from day one (`gguf` / `not-chat`) so a role can later
bind to something that is **not a GGUF**. PersonaPlex is the known case: a speech-to-speech **service** that owns a
whole card (~20 GB of 24) and is up-or-down rather than planner-placed — a **Voice body, not a donor** (D36
`VOICE_PLAN`). A role therefore gets **two bindings**: a `gguf` engine for the mind, optionally a `service` engine
for ears and mouth. That is the maintainer's own VRAM law — *voice on the 5090, mind pooled on the donors* — expressed as
configuration instead of a rule living in a doc. Declared and reported; not wired this phase.

**Verified end-to-end** on an isolated serve: `/roles.json` and `/v1/models` list both stock roles; a role requiring
vision *and* reliable tools (the combination STATUS says is not ready) is refused with all its reasons; a role whose
system prompt says *reply only BRACKISH* answered "Explain quantum tunnelling" with **BRACKISH** on both the
streaming and non-streaming paths, labelled `genghis-parrot`; goals and by-name models unchanged.

**Not built this phase:** knowledge retrieval (declared, inert, and says so), the voice binding, and adapters
(step 3 — Blender first). Ties to [D20] (task/skill catalog), [D23] (registry), [D31] (never silent), [D35], [D36].

---

## D47 — The chat UI is pinned, so GENGHIS owns its upgrade story: no false banner, a documented path, and provisioning that heals an upgrade (2026-09-22)
**Context:** the Docker tier is version-pinned (D18) so a GENGHIS install is reproducible. Open WebUI v0.11.4 arrived
with a **security advisory** and the reference instance showed its *"A new version is available — Update"* banner.
**Three decisions:**
1. **Turn the in-app update banner off** (`ENABLE_VERSION_UPDATE_CHECK=false`). Under a pin the button cannot do
   what it offers — the image is set in `docker-compose.yml`. A control that does nothing is worse than no control
   (D40 rule 1 inverted: the UI must not promise what the deployment cannot deliver). GENGHIS tells the user about
   chat-UI upgrades where it can also *perform* them.
2. **Ship a documented, tested upgrade path**, because a pin is a promise to move it deliberately, not never:
   back up `webui.db` → bump the pin → recreate → re-run provisioning → verify (goals list, a chat round-trips,
   themes, tools on/off) → roll back by restoring the DB and the old pin. A GENGHIS release names the chat-UI
   version it was tested against; a chat-UI **security** release earns a pin bump and a CHANGELOG line.
3. **Provisioning must heal an upgrade without reverting a choice.** The `genghis_provisioned` stamp cannot tell
   *"the user turned this off"* from *"the upgrade reset it"*. So the stamp records the **chat-UI version** it was
   written under; when that version changes, the provisioner re-checks only the settings GENGHIS cares about and
   re-applies **only those now sitting at Open WebUI's own default** — a value the user chose is still never
   touched. Without this, a future release silently restores the thirty-builtin-tools behaviour D40 was written to
   prevent, and nothing tells the user.
**Why it matters beyond this house:** every installation will meet this — an upstream release the user cannot click,
a security fix they must not miss, and settings that can be reset underneath them. It is exactly the class of
problem the project promised to absorb on the user's behalf rather than leave as folklore.

## D46 — A same-box donor is local-class: an eGPU on a host wins on speed and can hold a warm server (2026-09-22)
**Decision:** a donor whose RPC endpoint is on the **same box** as the host planning (same IP as the host's own anchor,
not the host itself — an eGPU, a second card served by its own `ggml-rpc-server`) is **local-class**: D9's "own card
wins whenever the model fits it" does not apply against it (the faster card on the box wins), a solo plan on it is a
**warm-server plan** (`llama-server --rpc <loopback>`, hosted by the box with 0 GB on its own card — over loopback that
is the right placement, not the degenerate one), and D34 never delegates such a plan away to another host. Registered
under its **own host label** (`the-host-egpu`) on purpose: `is_self_node` matches by hostname, and the host's own
serve (a Vulkan build) must dial the card over RPC, never treat it as its local anchor. **Why:** the RTX 5060 Ti moved
from its 1 GbE box into a Sparkle TBX-750FA Thunderbolt 4 enclosure on the NUC. Calibrated from the NUC it went from
**38 t/s** (over the wire) to **228 t/s** (loopback) — the same card, six times faster to the fleet: capacity from the
fleet, speed from placement. Without this rule the planner kept putting the 14B on the Arc (~6 t/s) while the eGPU sat
idle; with it, the 14B answers from the eGPU at ~50 t/s, first token 0.1 s. Ops notes: the enclosure's contact switch
had been the whole "it does nothing" (no Thunderbolt controller powered → nothing on the cable for any host to see);
CUDA 13.0 clashes with Ubuntu 26.04's glibc 2.43 (`rsqrt` exception specs) — 13.2 from NVIDIA's `ubuntu2404` repo
builds clean; `donor-serve.sh` auto-finds a binary, so with two builds on one box **both cron lines must pin
`GENGHIS_RPC_BIN` + `GENGHIS_RPC_DEVICE`** or the Arc's line can pick the CUDA build after a reboot; the enclosure's
1 GbE NIC is not needed (the card talks Thunderbolt).

## D45 — Per-request state is per request; the lock is only where the hardware demands it (2026-09-19)
**Decision:** the planner's per-request state — which model, which goal, the escalation window, the last decision and
refusal, the delegate's "why", the proxy's last message — lives in a **thread-local overlay** (`_REQ`), begun clean at
the top of every HTTP request and inherited by the worker threads a request starts (`with_ticks`, `ticked_iter`).
`serve` is a `ThreadingHTTPServer`: one thread per request, so thread-local *is* per-request; outside a request (the
CLI) the module globals remain the state. **The one-run-at-a-time lock shrinks to what the hardware demands:** the
per-request `llama-cli` engine (exclusive GPU, single-client donors) and pool *mutation* (start / stop / evict, under
`_POOL_LOCK`; a load's health-wait runs outside it, and a request for a model already loading **joins** that load
instead of killing it). Warm-server chats no longer take any lock: different models on one card answer **concurrently**;
the same model queues inside `llama-server` as it always did. A warm server answering a request is **busy**: an eviction
or a reclaim *waits* for the answer (bounded, 30 s) rather than pulling the server out from under the chat. And a small
model is **handed to a host that can hold it rather than evicting a bigger warm model here** (the D38 hand-over, extended
from "warm elsewhere" to "can hold it and it is smaller than what it would evict").
**Why:** the 2026-09-14 finding — a chat and Open WebUI's title call clobbering each other's `GOAL`/`MODEL`, the title
call running the 32B — was patched with `_V1_RUN_LOCK`, which made every `/v1` run on a host wait for the one before it:
correct, and the reason a `fastest` title said *"queued behind a balanced answer"*. Measured after: a `balanced` answer
on the warm 32B and a `fastest` title on the warm 1.5B, one second apart on the laptop — title answered in **0.3 s
while the 32B was mid-answer**; an eviction let the 32B **finish (4.0 s) before** it went; a title call that would have
evicted the laptop's warm 32B (a 3.5-minute reload for the next chat) went to the NUC instead (2 s). **Honesty:** the
engine path still serialises per host — that is the GPU, not the code; `resident.error` / `pinned_by` remain
process-wide (a text race between two failing starts at the same instant, not a correctness one).

## D44 — Every card is the fleet's by default; the owner has one switch, and it means what it says the moment it is clicked (2026-09-19)
**Decision:** the fabric **assumes** every GPU — including the best one, the laptop's 5090 — for planning, escalation
(D43) and formations (D39). What makes that acceptable is that the owner holds **one switch with one meaning** (D35's
lend): *Lend On* = my card is the fleet's; *Lend Off* = my card is mine. **Lend Off now also RECLAIMS the card:** every
pooled model another host keeps a shard of there is unloaded at once (a split cannot run with a piece missing), and the
note says which and where to put it instead (*"Reclaimed: unloaded Qwen2.5-32B on nuc-155h (it was across laptop-5090
+ nuc-155h) — place them again from the Pool without laptop-5090 if you want them back"*). The card's **own** warm
models stay — the owner's serve, the owner's business. *Lend On* moves nothing until the next plan asks. The automatic
forms stay as they were: *away* (registered from the tailnet) = donor off; the keep-awake lets go on battery.
**Stated up front, not hedged:** a conversation mid-answer on a reclaimed shard is **not guaranteed** — the switch is
for *"going to visit family this weekend, taking the laptop: move things where they need to be, turn lending off,
remote in later if I need a model on it"*, not for flipping mid-sentence. The maintainer: *"I wouldn't do that willy-nilly."*
**Why:** before this, Lend Off protected the card from *new* work only; a shard already parked there stayed (the owner
had to clear models by hand first — which the maintainer did). A switch that half-means its word is worse than none for a
stranger. And the boundary the maintainer set for the whole system applies here: *"we must be reasonable — balance reality
with ease of use and effective use, without bending over backwards to the point that we snap in half."* One switch,
one meaning, one honest caveat; no auto-detection of Resolve, no per-app rules, no migration of a running chat.

## D43 — Context escalation: solo while it fits, the fabric when the conversation outgrows the card (2026-09-19)
**Decision:** a warm model is a one-card thing by design (that is what makes it instant), and its window is sized to
that card (D32). When a conversation outgrows the largest window the card alone can give — llama-server's *"request
(19043 tokens) exceeds the available context size (16384)"* — the chat is not stopped. The coordinator **re-plans the
same chat across the fabric with the KV sized for that conversation** (the planner is asked to size the model for the
needed window, not the default 4k), warms it pooled with the bigger window, and continues — slower per token, but it
keeps going — narrating every step in the Thinking panel. That server is marked **escalated**: it is *not* a placement
the user chose, so it is kept only while the conversation coming in still outgrows the card (a cheap size estimate of
the incoming messages — a re-plan every turn would be two loads per message), is never protected from eviction the way
a chosen fabric placement is (D39), and **drops back to the card alone when a chat arrives that fits there again** —
said out loud. Ceiling = the model's trained context (32k for Qwen2.5-32B); past that the honest message stays ("start
a new chat, or shorten this one"). **Why:** the maintainer's question on 2026-09-15 — *"since these can be split among the
fabric, why are we setting it low?"* — the fabric's memory is exactly what a long conversation needs, and the fleet
had it all along. First live run: a 19,043-token chat on the laptop's 32B (16k solo fit) → *"asking the fabric for a
bigger window… re-planning the same chat across laptop-5090 + gpu-5060ti"* → pooled 32k window in 46 s (cache load),
the 19k prompt in 96 s, answered. **Honesty note:** the 5090 sits mostly idle while the pooled run is per-token-bound
over the wire; escalation buys *continuation*, not speed — which is why it drops back.

## D42 — A pinned card still answers: warm elsewhere → the right model on a cold host → (for `fastest`) what fits here; and say so (2026-09-19)
**Decision:** when an effort goal's model cannot load on this host because a **fabric placement holds the memory**
(D39's pin), the request is not refused. In order: **(a)** another host that holds the model **warm** → delegate (D34);
**(b)** for the quality goals (`balanced`, `fit`, `biggest`) a host that **can hold** the right model, even cold (a
one-time load or fetch, told to the user) → delegate; **(c)** for `fastest`, the largest registry text model that **fits
beside the pin** on this card → substitute, and say exactly why in the Thinking panel (*"fastest → Nemotron-4B needs
~7.2 GB, but this card holds Llama-3.3-70B across … (13.2 GB of 16.4); answering with qwen2.5-1.5b instead — it fits in
the 3.2 GB left"*); the answer's `genghis` block carries `substituted: true`. A model asked for **by name** is never
swapped (a cold host may still take it). Only when nothing applies does the D39 409 remain. A delegated non-stream call
now relays the host's own status (a 503 *"fetching the model"* is an answer, not a crash — the older D34 path had the
same hole). **Why:** last night a warm 70B on the NUC silently 409'd every Open WebUI task call (titles, tags, follow-ups)
and every `fastest` question on the home base — the first time the mission (pooled residency) stepped on the product
(an instant daily driver). `fastest` prefers *now* over *right*; the quality goals prefer *right* over *now*; both are
told. Verified live: `fastest` answers beside a pinned 32B in 2.4 s; `fit` is handed to the laptop, whose honest
"fetching the 14B" reply comes back clean.

## D41 — The placement rule: the fewest, fastest nodes with margin; reach for slow-link / CPU nodes only when the wired GPUs cannot cover it; and say so (2026-09-18)
**Decision:** a split is built **tier by tier** — wired-class GPUs first (this host's own card counts), then GPUs on a
slow link, then wired CPUs, then CPUs on a slow link — fastest first within a tier, stopping the moment the goal's
margin is met. So a Wi-Fi Pi or a CPU node is in a plan **only when the wired GPUs could not hold the model**, never
because it happened to be next by score. Three corollaries: **(1) no slice too small to be worth a hop** — a node with
under 3 % of the model (max 1 GB) free is left idle and told why; **(2) the plan says what it reached for and what
would fix it** — *"Reached for wired CPU + slow-link CPU nodes (pi5-8gb, tegra-x1, pi4-donor): the wired GPUs
hold 31.1 GB of the 40.8 GB needed. … laptop-5090 has 0.8 GB of 22.1 GB free — it holds Qwen2.5-32B warm; unload
that and the wired GPUs hold 53.1 GB, enough on their own."* — in the decision log, in each node's WHY (*"~80 % of
every token — reached for (slow-link CPU, 306 ms)"*), and in the Control Room's ghost, which turns **amber** for a
plan that fits only by reaching; **(3) under a SPEED goal (`fastest`, `balanced`) a plan whose per-token cost is
dominated (≥ 50 %) by a slow-link CPU node it reached for is REFUSED with the fix in hand** — *"won't run sensibly …
Or choose `fit` to run it anyway, at that pace"* — the capacity goals (`fit`, `biggest`) still take it, and a fleet
that IS all Pis is never refused for being what it is. The link is judged by the **measured round-trip** (≤ 25 ms =
wired-class), never by the self-described `link_type`, which reads `confirm`/`local` on nodes that ping at 300 ms.
**Why:** tonight the 5090 had 861 MB free (its own 32B warm), so the wired GPUs held 31.8 of the 70B's 41.7 GB — and
the planner greedily added *everything* by score until the +8 % margin cleared: the laptop for a **0.8 GB** slice,
two Wi-Fi Pis (163 / 306 ms) and a **0.1 t/s** node. The LOO prune couldn't drop any of them (each was needed for
the margin, none for the model). It streamed ~40 GB for eight minutes and llama-server died. Replayed with the same
numbers, the new rule refuses in under a second and names the one action that makes the reach unnecessary. The maintainer's
framing: *"prefer the fewest, fastest nodes with margin; reach for Wi-Fi/CPU nodes only when the wired GPUs can't
cover it; say so in the ghost"* — and, for the house, *get the Pis on wired.* **Honesty note:** the per-token shares
are **relative** (share of the layers at that node's pace + its round-trip); the absolute pace of an RPC pipeline is
not derivable from them (a wired three-GPU 70B measures ~2 t/s), so the planner never promises a number, only who
dominates.

## D40 — Two product rules: NOTHING that affects the user's experience is silent, and provisioning is first-time-only (2026-09-16)
**Decision (rule 1 — never silent):** any process that changes what the user is waiting on **tells the user, in the
place they are looking, with a time** — a warm hand-over, a cold load, a network fall-back, a queue, an eviction, a
fetch. The maintainer: *"any process that affects the user experience must let the user know so they don't wrongly think
nothing is happening."* D31 made streaming narrate; this generalises it to every path. The test case: the NUC fell
back from a warm host to streaming 20 GB over RPC and told only its log — the user saw a cursor for minutes and
asked "is it stuck?". Now the fall-back says what it is doing, why the warm host was skipped, how many GB, and
about how many minutes, and how to bail out. Applies to the Control Room too (Warm now says what it evicted).
**Decision (rule 2 — provisioning is first-time-only and never reverts a choice):** `provision.py` stamps a model row
`genghis_provisioned` once and never touches it again; new models are provisioned the first time they are seen (the
authority's watchdog re-runs it every 15 min, so a GGUF dropped in the folder is covered within minutes, not never).
A user who turns a tool off, or writes their own prompt, keeps it. The maintainer: *"this isn't just for me — we want a
flawless user experience delivered in a way that is comfortable for the user."* Defaults are a gift, not a leash.
**Why:** both came from the same afternoon: a stranger's install would have had thirty builtin tools, no chart
prompt and a silent five-minute fall-back — everything the maintainer hit, waiting for the next person. Every fix that was
applied by hand to the reference instance had to become a default that ships (`integrations/openwebui/provision.py`
+ the watchdog hook), and every wait had to speak.

## D39 — The fabric is a warm place too, and control is by FORMATION: what is warm where, switched in one action (2026-09-15) — BUILT 2026-09-18 (steps 1–4); pass 2 (true transfer progress, the bracket, formations) BUILT 2026-09-19 — complete
**Built:** pooled residency (`ensure_resident` with an RPC plan: local share charged here, donors' shards reported to the
authority on their behalf, context sized by the pool's memory; `warm_model(pooled=True)`; `/v1` finds a warm pooled
model BEFORE planning); the **pinning rule** (a pooled entry is never LRU-evicted; a model that fits beside it runs,
one that doesn't gets a 409/[genghis] line with the numbers and the way out; donors holding a shard are **PINNED**,
not DOWN — `ggml-rpc-server` serves one client); and **The Pool** — the maintainer's design: the fleet's memory as one bar per
node, models as blocks on a shelf; drop on a host = warm there, drop on the strip = warm across the fabric, drag a
warm block back = unload; every hover is a **dry run** (`/plan.json`) painted as a ghost, green with the real per-node
split and what's left, red with why not; loading shows as a stripe with a wire estimate; the old grid is a table-view
toggle. Measured: 70B across laptop-5090 + gpu-5060ti + nuc-155h in ~8 min (~80 MB/s), first token 2.2 s, 1.7 t/s.
**Still booked:** formations (named layouts switched in one action) and true transfer progress from the socket counters.

## D39 (original booking) — The fabric is a warm place too, and control is by FORMATION: what is warm where, switched in one action (2026-09-15)
**Decision:** three additions above the per-host warm pools (D38) and the one-page control of every card (built the
same day): **(a) a Fabric column** on the Control Room's Models grid — the pooled model is a first-class place, with
its plan shown (nodes, anchor, measured speed) and an honest state (*runs on demand, cold* / ● WARM); **(b) pooled
residency** — the fabric model can be kept warm (`ensure_resident` already takes `rpc_list/devices/tensor_split`;
only the solo gates in `use_resident` and `warm_model` keep it cold), and while it is warm it **pins** every donor
it spans; **(c) formations** — a named layout of what is warm where, fabric model included, switched in **one
action** (Control Room click · CLI · `POST /formation`), where the switch computes the unloads, *says what it will
unload* (D38's eviction honesty, fleet-wide) and performs them in order. Rule that follows: **per-host pools and a
fabric model are different modes** — a formation chooses one; the fabric never pins a card by accident. Corollary:
**every box is a donor; every box with a card is also a host** (D27's dual role as the default, one installer flag).
**Why:** two scenarios ([docs/SCENARIOS.md](docs/SCENARIOS.md)). *Larry* has one host and many donors — the fabric
column *is* his product, and today it is a cold per-request run hidden behind a "pooled memory" number. *Johnny* runs
a procedure of three formations over Tailscale (solo prime → two remote hosts' checks → drop all, one huge world
model) — the grid alone would make that twelve clicks in the right order, every morning. The maintainer, asking whether the
grid was "the right dashboard and process" for the 5060, whatever comes next, and one model over everything: the
grid is the right *view*; the missing piece is the unit of control above it.
**Measured cautions to carry in:** a warm pooled model removes the ~60 s load, not the per-token network cost
(pooled 32B wired ≈ 21 t/s; a 70B will be a few t/s — warm, not fast); donors must report *live* memory once an RPC
server can hold a resident shard, or the planner double-books them. **Not decided:** phones as donors (Termux) —
plausible, untested, not to be claimed by the Guide until one phone has done it.

## D38 — A pool of warm models per host, not one slot: evict only when the card is actually full, and hand a small request to a host that already holds it warm rather than evicting a big one (2026-09-15)
**Decision:** each host keeps **several** warm `llama-server`s, keyed by model file, each on its own port
(8081–8088), with the active one proxied for `/v1`. A request for a model already in the pool **activates** it —
no load. A new model starts only when it isn't warm; **eviction is least-recently-used and only when the anchor's
budget would be exceeded** (weights + KV at the chosen context + headroom, summed over the pool, against the
card's 92 %). "Free the GPU" for a pooled/RPC run stops the whole pool. Every warm server's footprint is what the
host reports as held VRAM (D34's live report), and `/registry.json` now lists the pool. **The hand-over rule:** if
loading a model here would evict a *bigger* warm one, and another live host has the requested model warm, the
chat is delegated there instead (D34's machinery) — a `fastest` on the laptop goes to the NUC's warm 1.5B and the
5090 keeps its 32B. **Why:** one slot per host meant every `fastest` call evicted `fit` and every `fit` call
reloaded it ("this model was already loaded — why does this keep happening?"); the Arc holds a 14B + a 1.5B
together (12.5 of 16.8 GB), so it should. The 5090 cannot hold the 32B at 16k *and* the 1.5B (23.4 > 22.5 GB) —
hence the hand-over rather than a churn. Verified: NUC `fit/fastest/fit/fastest` → 6.7 s, 2.0 s, 2.1 s, 1.1 s,
both warm; laptop `balanced` then `fastest` → handed to the NUC, 32B untouched. Ties to [D23], [D32], [D34].

---

## D37 — Native tool calling in `/v1`: the model decides, GENGHIS relays, the client runs the tool (2026-09-15)
**Decision:** `/v1/chat/completions` accepts OpenAI `tools` / `tool_choice`; the warm resident (`llama-server --jinja`,
which speaks Hermes-style tool calling for Qwen) returns `tool_calls`; GENGHIS relays them **verbatim** — non-stream
as `message.tool_calls` + `finish_reason: "tool_calls"` (+ real `usage`), stream as `delta.tool_calls` chunks and the
real finish reason — and accepts `role: "tool"` results back. Delegation (D34) carries all of it, since the request
body and the SSE are forwarded whole. The per-request `llama-cli` path renders tool turns in ChatML so a pooled run
doesn't choke on a tool-bearing conversation, but tool *selection* is a resident-path feature. **Why:** Open WebUI's
"Default" function-calling mode delegates tool *selection* to its Task Model — set to the 1.5B (to stop shadow 32B
runs) it fumbled the JSON; set to a bigger model it injected a 7k-token prompt and swallowed the 32B's 16-token
tool-call as a second call — three failed tries on the maintainer's time. Native mode puts the decision on the chat model
and the relay on GENGHIS: deterministic, delegable, no task model in the loop. Verified: direct and via the NUC,
stream and non-stream — `fleet_status` requested, answer composed from its result. **Caller = untrusted input** still
applies (D-voice): tools a persona may call are the client's choice, gated there. Ties to [D23], [D31], [D34].

---

## D36 — Mechanism vs. choice: GENGHIS ships mechanisms; the maintainer's choices live in a private home repo the public project never sees (2026-09-14)
**Decision:** two repositories with a wall between them. **The_GENGHIS_Protocol** (to be public) ships *mechanisms*:
the coordinator, the roles (Authority · Host · Donor · Surface · Voice), placement, adapters (AudioSocket, notify,
eye), installers, and a `home.example/` of placeholders. **genghis-home** (private, forever) holds *choices*: the
maintainer's fleet, personas and cloned voices, phone numbers/DIDs/dialplan, routines (which camera, which car,
which relative to call), M365 hooks, and — the piece that keeps the wall honest — **`NEEDS.md`**, a register of
*my need (private)* ↔ *the mechanism delivered (public)*, one line each. GENGHIS reads the home via `GENGHIS_HOME`
(planned); until then the home's `LEAK_WORDS.txt` is appended to `leak-audit.sh`'s pattern so a private word in a
shipping file fails the audit. **Why:** the voice conversation (2026-09-14) produced a plan that is almost entirely
personal — a specific car, specific phones on specific walls, a business line, a relative to check on — and the
project's charter is the generic capability behind each of those. Writing them in the same file is how a house
leaks into a product. Ties to [D22] (no personal address compiled in), [D34], [D35].

---

## D35 — Roles are capabilities, not chairs: Authority · Host · Donor · Surface (2026-09-14)
**Decision:** the vocabulary is fixed. A box has any combination of four **capabilities**; no box is "the client"
and no box is "in charge of the work". **Authority** — owns the *truth*: fleet, human config, the model library,
mDNS, the watchdog, the HEARTH backend (one per fleet; the NUC). **Host** — a front door: takes a chat, plans it,
then runs it on its own anchor, **hands it to the host that holds the model** (D34), or pools over RPC (the NUC,
The laptop — any box running `serve`). **Donor** — lends memory/compute over RPC (`rpc-server`). **Surface** —
presence and sensing, no compute (the TV; the eye node). The laptop at home = host + donor; the laptop away =
**host over Tailscale, donor off** (an RPC donor over the internet is per-token round trips at 50 ms+ — a box
that registers via the tailnet registers without an RPC endpoint); the laptop rendering in Blender/Resolve =
host with **lend off** (a per-host "lend my GPU" switch the planner and delegation respect). The NUC's agents
(D20) run because it is the home base, not because anyone is or isn't the client. **Why:** the maintainer's
"Remote / Home / Rejoin" chart (2026-09-14) still said *client* and *participant*; walking it through showed the
dichotomy had already dissolved with D30/D34 — work flows to where the model lives, symmetrically — and that
the two remaining gaps are exactly the two switches above, not a redesign. Rejoin is automatic: at logon the
laptop's `serve` + `rpc-server` start, it registers (D33), the watchdog counts it, D34 routes to it.
Ties to [D9], [D27], [D30], [D33], [D34].

---

## D34 — Capacity from the fleet, speed from placement: a chat goes to the host that holds the model on its own GPU; RPC only when nothing fits (2026-09-14)
**Decision:** before an inference host drives a *remote* GPU over RPC, it asks every other live host (a fleet
node with `local: true` that answers `/registry.json`) whether it **holds that model warm on its own anchor**
(resident up, same file) or **could** (its card is big enough; it can swap its resident). If one can, the request
is **handed over whole** — forwarded to that host's `/v1` with an `X-Genghis-Delegated` header (never bounced
back) and its SSE relayed verbatim, narration included: *"handing this to laptop-5090 — it holds Qwen2.5-32B on
its own GPU (warm now; no network per token)"*. Warm beats cold-but-fits; among equals, the faster card. The
local run lock is released the moment a chat is delegated (our GPU is not involved). **Why — measured the same
day:** the NUC's Open WebUI drove the 32B on the laptop's 5090 *over RPC* at **4–7 t/s on Wi-Fi, 21 t/s wired,
plus a ~60 s cache re-upload every call** — while the laptop's *own* serve had that same model resident at ~30 t/s
with zero network per token; and the 1.5B over RPC ran at the *same* 5 t/s as the 32B (per-token round-trip
latency, not compute). After D34: four consecutive `balanced` chats through the NUC → first token 0.2–0.4 s,
whole answers < 1 s. Pooling stays the answer for a model **no single box can hold** (the 70B); it is no longer
the answer for a model *some* box can hold. **Companion rules that made it stick:** (1) a host reports its
anchor's **live free VRAM** (`vram_free_mb` = total minus what its resident holds; on resident change, at serve
start, and every 60 s) so another host never plans an RPC shard onto a card that is already carrying a resident
(that produced 1 tok/s: 20 GB of CUDA spill); (2) that report is for *other* hosts' RPC planning only — a host's
**own** anchor budget stays its total (the resident is reusable/swappable), or it plans around itself and pools
over the network; (3) a delegation target is judged by its card, not by what it holds now; (4) the goal→model map
is a contract: a host that lacks the mapped file **fetches it** rather than silently substituting a smaller
model (`E:\models` had dropped out of the laptop's scan because the search path keyed on the per-request MODEL
global — `balanced` was quietly answering with the 1.5B). Ties to [D9] (anchor), [D23] (residency), [D27]
(dual-role), [D30] (hosts), [D31] (never silent).

---

## D33 — A node registers ITSELF: `init --coord` (and the installers) announce the box to the authority; no hand-edit of `fleet.json` to join (2026-09-13)
**Decision:** joining a fleet is two-way. `genghis init --coord <authority>` already *pulled* the fleet's nodes into
the new box's file (D27); it now also **POSTs the box's own record to the authority** (`/fleet` `{"action":
"register","node":{…}}`), and a standalone `register [--coord A[,B…]] [--port N]` does the same on demand. The
record carries the address other nodes must dial (the local interface that reaches the authority), the RPC port
if an `rpc-server` is listening locally (else none — a pure client is a private anchor), the device, and the build
stamp. The authority **upserts** by id or hostname, keeps what it has learned (throughput EMA, reliability,
names), revives a retired entry, clears a matching `_pending_donors` stub, saves atomically — and the node is in
everyone's fabric on the next heartbeat. Admin-gated like the rest of `/fleet` (a LAN post is admin by default;
with a PIN, the client sends `GENGHIS_TOKEN`). **Why:** the maintainer replaced one box and added another, ran the
installers, and saw nothing — the `-Donor` step ended with "then register this box on the coordinator (Control
Room / fleet.json: ip + port)", a manual step D26 had already promised away. Every customer would have hit it.
**Caveat (until D30):** every `serve` host is its own authority today, so a node must be announced to each one
you want to see it (`--coord pi,laptop`); D30 makes the single authority the rule and retires the list.
Ties to [D18] (installers), [D22] (`init`), [D26] (lifecycle), [D27] (dual-role), [D30] (one authority).

---

## D32 — The warm server's context size is a decision, not a constant: sized from the model + the GPU, grown on demand (2026-09-13)
**Decision:** the resident `llama-server` was started with a fixed `-c 4096`. Any conversation longer than a few
exchanges — 6,217 tokens in the maintainer's Open WebUI chat — came back `HTTP 400: request exceeds the available context
size`. Now `resident_ctx()` chooses: the largest step on a ladder (4k → 8k → 16k → 32k → 64k → 128k) that the model
was **trained for** (GGUF `<arch>.context_length`) and that **fits this GPU's free memory** with weights + precise
KV + the compute floor; default = one comfortable step (16k when it fits: the 1.5B and the 27B on the 5090; the 32B
gets its 8k max). Context is part of the resident's signature. When a request still overflows, the proxy raises a
typed `ContextTooSmall(need, have)`; the stream path **reloads the server once with a context ≥ 1.25× the
conversation** (narrated: *"this conversation is 18037 tokens; the warm server holds 16384 — reloading with a
32768-token context"*) and retries; if the GPU can't hold that, the chat gets a plain sentence — *"the most this model
can hold on this GPU is N tokens; start a new chat or shorten this one"* — instead of a 400. Config `resident_ctx`
pins it. `/registry.json.resident.ctx` shows the live value. **Why:** context is memory, and memory is what GENGHIS
plans — hard-coding it contradicted the whole thesis; and a chat that dies at turn six is a broken product. Measured:
an 18k-token request on the 1.5B → grew 16k→32k once, answered in 7 s. Ties to [D23] (residency), [D28]/[D31]
(explain, don't fail).

---

## D31 — A streaming `/v1` is never silent: status and the model's reasoning flow through `reasoning_content` (2026-09-13)
**Decision:** While a chat is in flight, an OpenAI client can animate exactly two things: streamed `content` and
streamed `reasoning_content` (Open WebUI renders the latter as its live "Thinking… N s" block). GENGHIS was sending
**nothing** until the first answer token: `_proxy_resident` relayed only `delta.content` and **dropped
`reasoning_content`** (where llama-server puts a Qwen3's `<think>` stream), and the warm server's 10–15 s model
load ran *before HTTP headers were even sent*. To the user that was a frozen `|` for the whole think — "it stopped."
Now one streaming entry point (`stream_with_status`): headers go out immediately; GENGHIS **narrates its own steps
through `reasoning_content`** — `GENGHIS · balanced · <model> · solo on laptop-5090`, `loading … into VRAM`, `…
loading (9 s)` ticks every 3 s while a step blocks, `processing prompt`, `streaming model shards to N nodes over
RPC` for pooled runs — then relays the model's own reasoning **as reasoning** and the answer as content. Non-stream
responses carry `message.reasoning_content` too. Clients that don't render reasoning simply ignore it; the
OpenAI wire format is unchanged. Measured: first byte at 0.0 s (was 10–15 s), 47 reasoning chunks from the 27B
relayed (was 0). **Why:** the same rule as D28 — a request always gets an answer, and the answer says what is
happening — applied to the seconds *inside* a request. Ties to [D17] (`/v1` contract), [D23] (residency),
[D28] (first run never looks broken).

---

## D30 — Authority and home base are separate ROLES, co-located by default: one fleet, one `fleet.json`, any number of `/v1` hosts; the NUC takes over from the Pi (2026-09-13)
**Status: BUILT 2026-09-13** — `serve` decides its mode from where `FLEET_URL` points (`GENGHIS_COORD` / `serve --coord HOST`):
at this box → **authority**; elsewhere → **inference host**. A host reads the fleet (`/fleet.json`, 5 s TTL) and the human
config (`/config.json`, 10 s TTL: names, roles, default goal, goal→model map, default model, hearth) from the authority,
keeps only per-box keys local (`residency`, `resident_ctx`, `models_dirs`, `auth`), forwards every write (`/fleet`,
`/config`, `/report`, `/reply`) and HEARTH state reads to the authority, 302s `/models/<file>` to the library, does not
advertise on mDNS, and registers itself with the authority at startup. Its `/v1` + residency + Control Room run from its
own vantage. Verified: the laptop as a host of the Pi — fleet + models from the Pi, a `register` posted to the host
landed on the authority and showed on both, a chat ran resident on the laptop's own 5090. `init` now prints the
two-question placement hint.

**Decision:** The coordinator is really two jobs, and the code will say so. The **authority** owns the
fleet (`fleet.json`, `config.json`), serves the **model library**, advertises on mDNS, and is the HEARTH
backend — it needs to be *always on and reliable*, nothing more. The **home base** (inference host) runs
`/v1`, residency, the Docker tier and the ambient agents — it needs a *GPU and RAM*, always on. Today
`serve` conflates them: since D22 **every** `serve` reads its own local `fleet.json`, so tonight there were
*three* authorities (Pi, laptop, NUC) kept in sync by hand. `serve` gains two modes: **authority** (as now)
and **inference host** (`serve --coord <authority>` / `GENGHIS_COORD` set): fetches fleet + config from the
authority, never owns a copy, runs `/v1` + residency, pushes learned throughput back. One fleet, one file,
any number of `/v1` hosts (the laptop when it is home, the NUC always).
**Placement rule (the product rule for everyone):** the authority goes on the **most reliable always-on
node**; if that node **also has the GPU**, it is the home base too — the roles co-locate by default and
split only when the always-on box has no GPU. `genghis init` asks two questions — *is this box always on?*
*does it have a GPU?* — and proposes the role. This is k3s's stance (the server node runs workloads unless
you deliberately taint it); no home fleet reaches the scale where a dedicated control plane pays.
**Customer shapes this serves:** (1) **one box** — authority = home base = that machine, no fleet concept
(the majority; must stay this simple); (2) **gaming PC + a small always-on box** (Pi/NAS/mini-PC) —
authority on the small box, home base on the PC when it is on (the original design; a fully supported
shape, not a retired one); (3) **always-on GPU box + helpers** (NUC / DGX Spark / homelab server — and every
HEARTH elder-care install, which is "one always-on box + TV + camera") — authority + home base on the GPU
box. **the maintainer's fleet moves from shape 2 to shape 3 with the NUC's Ubuntu install:** the NUC becomes
authority + home base + donor; the **Pi 5 stays as a CPU donor and the coordinator's cold spare** (a nightly
`rsync` of `fleet.json` / `config.json` / the model-library index → if the NUC dies, `serve` on the Pi
brings the fleet back in a minute — insurance, not a second brain, no HA machinery).
**Costs, honestly:** the Pi-authority path stops being dogfooded → it needs a per-release smoke test
(`install-linux.sh --role coordinator` on the Pi in a scratch dir) or it bit-rots; the model library
(~60 GB) migrates Pi → NUC (upside: served over the NUC's 2.5 GbE, a 70B pull drops from ~6 min to ~2.5);
replacing the coordinator is the real migration D26 warned about → [`docs/COORDINATOR_MIGRATION.md`](docs/COORDINATOR_MIGRATION.md)
is the runbook. **Why:** tonight's bugs (self-fetch 404, hand-synced fleet files, FOREIGN anchors) were all
symptoms of "every serve is an authority"; and the fleet's most valuable node for the release is the
always-on GPU box, which should be the thing we dogfood. Ties to [D10] (one source of truth — restored),
[D20] (always-on host), [D22] (`init`), [D26] (coordinator replacement), [D27] (dual-role), [D29].

---

## D29 — The Docker tier lives ONCE, on the home base — never on the Pi, never on a box that is not always on (2026-09-13)
**Decision:** Open WebUI, Grafana and Prometheus (the optional D21 `genghis` compose project) run in **one
place: the home base** — the always-on box that hosts `/v1`. Open WebUI points at `localhost:8899/v1`;
Prometheus scrapes every node's `/metrics` from there; Grafana sits on top; all reachable over Tailscale
once the home base is on the tailnet. **Not the Pi:** it *can* run them (ARM images exist), but Open WebUI
wants 1–2 GB and real CPU, and every GB it eats the Pi cannot lend as a donor or use for the library — the
Pi's value is being small, boring and always right. **Not the laptop:** the day it leaves the house, so do
the chat window and the dashboards (the same failure as Tailscale-on-the-laptop-only). **The Control Room
tells the truth about it:** on any host that is not the home base, the Docker-tier cards read *"not on
this host"* with a pointer to INSTALL.md, instead of a bare red dot into a dead page (the NUC's first
`localhost:3080`). **For the release:** a one-box customer runs the tier on that box; a fleet customer runs
it on their home base; nobody runs it twice. Migration for the maintainer's fleet: Docker Desktop (Windows) — or
native Docker on Ubuntu — on the NUC, `docker compose up -d` there, then retire the laptop's compose
project (chat history starts fresh; not worth migrating the volume). **Why:** the human-facing layer must be
where the humans can always reach it. Ties to [D21] (Docker optional tier), [D24] (Control Room), [D30].

---

## D28 — First run must never look broken: a model is fetched in the background, never inside a request; every failure is a readable answer (2026-09-12)
**Decision:** `/v1` never blocks on a download. If the requested model isn't local, the chat returns
**immediately** with `503` and an OpenAI-style error the client can *show* — `{"error": {"type":
"model_loading", "message": "GENGHIS is fetching <model> from the model library — 412 of 1117 MB (37%).
Try again in a moment."}}` (+ `Retry-After`) — while a **background thread** pulls the model (one at a
time, resumable). `serve` **pre-fetches** the default and goal-mapped models at startup, so the first
chat usually finds them already there; `/registry.json` exposes the fetch (`fetch: {name, pct, active,
error}`) and the **Control Room** shows it as a live progress row. A missing engine (no `llama-cli`
built) is likewise a readable `503 engine_missing`, not a dropped socket. The installer's last step
**offers to pull the starter model** so an install ends ready to chat. Download progress logs one line
per 5 % (a redirected log is not a TTY). **Why:** the NUC's very first chat "closed unexpectedly": the
fetch ran inside the request handler, every HTTP client (Open WebUI, curl, Invoke-RestMethod) gave up
after seconds, and nothing explained the 1 GB download happening behind it. For *anyone* installing
GENGHIS that is the first impression — so the rule is: **a request always gets an answer, and the
answer says what is happening.** Ties to [D17] (the `/v1` contract), [D18] (installers), [D22]
(`init --coord` records the repo the fetch comes from), [D23] (registry/residency).

---

## D27 — Dual-role nodes: a box may be BOTH its own local anchor AND an RPC donor for everyone else; the first non-NVIDIA (Vulkan) client (2026-09-12)
**Decision:** A fleet node may hold two compute roles at once. `local:true` + a `device` (its own GPU, no
RPC hop) makes it the **anchor from its own vantage**; an `ip:port` RPC endpoint makes it a **donor from
every other vantage**. The rule is one predicate — `has_rpc(d)` — applied at the three seams: `live_donors`
keeps a foreign `local:true` node iff it publishes an endpoint (the laptop's 5090 has none → still private to
The laptop; the NUC's Arc does → pooled by everyone); `heartbeat` probes a dual node honestly from other hosts
and marks it UP/0 ms only from itself; `build_devices` was already right (self → `Vulkan0`/`CUDA0`, else
`RPCn`). **Vulkan is a first-class client accelerator:** binary lookup is now `build-cuda` → `build-vulkan` →
`build-rpc` (was CUDA-or-CPU), and the Windows installer detects a non-NVIDIA GPU + the Vulkan SDK (offers
`winget KhronosGroup.VulkanSDK`) and builds `build-vulkan`; it also grew `-Donor` (a reboot-proof
`ggml-rpc-server` Startup launcher, `poc/rpc-serve-windows.ps1`) and `-LlamaDir` (junction an existing
pinned-commit build instead of re-cloning). `serve-laptop.ps1` is now path/user-agnostic (`$PSScriptRoot`,
no hardcoded `E:\…`/Python path) so `-Serve` works on any checkout. **Reference case: the NUC** — from
itself, `decide` plans `Vulkan0` (no RPC) + remote donors; from the laptop it's `RPC0` in the pool; from the
Pi it's a usable donor. **Caveat (honest):** the same VRAM serves both roles — a warm resident model on the
NUC's Arc and a laptop run that pools the NUC compete for the same ~18 GB; the rpc-server's live free-memory
report is what keeps the planner honest, and D20's work/home mode switch is the real answer. **Why:** the
D20 always-on host wants to *run* models (residency, `/v1`) AND *lend* its GPU when idle; before this a node
had to pick one, and an Intel Arc box couldn't be a client at all without a hand-set env var. Closes the
"NUC as a client" gap. Ties to [D9] (anchor), [D18] (installers), [D20] (always-on host), [D22] (`init`).

---

## D26 — Node lifecycle: cattle, not pets — a break is a non-event, add/remove/replace is one command or one click, reversible (2026-09-12)
**Decision:** A heterogeneous fleet is hardware that comes and goes, so GENGHIS treats nodes as **cattle, not
pets**. Two distinct cases: (1) a node **breaking / going offline** needs ZERO action — heartbeat marks it
DOWN, `live_donors` filters it, the planner heals-by-membership and routes around it, and it rejoins on its
own (this was already true). (2) **permanent add / remove / replace** is now first-class and reversible:
`fleet_ops(action,id)` on the authority does **retire** (move a node from `donors`/`_eye_nodes` into the
`_retired_donors` graveyard, dated), **restore** (bring it back), **remove** (hard-delete everywhere incl. its
config `names`/`roles`) — all via **atomic `save_fleet()`**. Surfaced two ways: **CLI** `genghis fleet`
(`list` / `retire <id>` / `restore <id>` / `remove <id>` — POSTs to the coordinator so it persists
fleet-wide) and the **Control Room** (a **Retire** button per node row + a **restore** link for each retired
node). **Admin-gated** (D25): node lifecycle needs the admin role; a viewer/operator gets 403. Replacing
hardware becomes: `retire old-id` → the new box self-registers (`genghis init` / donor-setup). **Caveat
documented:** removing a *donor* is trivial; replacing the *coordinator* (the authority + model repo) is a
real migration with its own runbook. **Why:** for "how anyone may use it," a dead box must be a non-event and
swapping a 5060 Ti for a 4090 must be one gesture, reversible — not a hand-edit of `fleet.json`. Ties to [D22]
(`genghis init` adds THIS node; `fleet` is the symmetric remove) and [D24]/[D25] (Control Room + roles).

---

## D25 — Auth is pluggable via a front proxy; GENGHIS maps forwarded groups → roles (viewer/operator/admin) and gates actions; local default, Azure/LDAP opt-in; remote access via zero-trust edge (2026-09-12)
**Decision:** GENGHIS does **not** hand-roll identity (OIDC/SAML/LDAP is security-critical and a solved problem
— reinventing it is a liability). Instead an **identity proxy sits in front** of every service and injects
trusted headers (`X-Auth-Request-User`, `X-Auth-Request-Groups`); GENGHIS only **reads** them. The coordinator
maps groups → **roles** (`viewer` < `operator` < `admin`) via `config.auth.roles_by_group` and **gates
actions** by role: view = everyone; change model/goal + load/unload = operator; the `/admin` panel, node
names/roles, default goal, and the auth PIN = admin. **Local default preserved:** with no proxy in front (no
groups header), the box behaves exactly as today — single-user LAN trust = full admin — so the home user gets
zero-cloud auth by default and nothing breaks. **Azure Entra ID and LDAP are OPT-IN**, chosen in the proxy's
config, never in GENGHIS code (the coordinator is identical under any provider). Recommended IdP: **Authentik**
(self-hosts local + LDAP + Azure federation behind one group→role UI); `oauth2-proxy` is the lighter
Azure-only path. This also gives **SSO across Control Room + Open WebUI + Grafana** at once.

**Remote access (same seam):** exposing a home box is real attack surface, so only **zero-trust edges** are
recommended — never port-forwarding. All three inject the same verified user+groups GENGHIS already reads, so
the RBAC layer is provider-agnostic: **Azure AD Application Proxy** (native Entra pre-auth + Conditional Access
+ group assignment — the Microsoft-shop path, needs Entra P1), **Cloudflare Tunnel + Access** (outbound-only,
no open ports/home-IP exposure, Access uses Entra as IdP — free), or **Tailscale + Entra SSO** (private mesh,
no public endpoint — "just me remotely"). Opt-in; the deployer wires their own tenant/tunnel.

**Why:** GENGHIS's thesis (D20) is private, owned, idle devices — so local-first is the default and cloud is a
switch, never a requirement (matches the "not a default for everyone" instinct). But the office/lab/multi-user
segment (D17/D20) needs real RBAC + remote access, and an IT shop already living in Entra/AD gets it for free.
Building the **read-the-header + group→role + gate** seam once means local Authentik, Cloudflare Access, and
Azure App Proxy are all just different front doors onto the same lock. **Slices:** (1) API-docs link on the
Coordinator API card; (2) the GENGHIS role layer (`request_role`, `roles_by_group`, `/whoami`, gate
POST /config & /residency, UI reflects role) — works behind ANY proxy, local mode unchanged; (3) a documented
opt-in Entra/proxy overlay; (4) SSO the other services behind the same proxy. Ties to [D18] (installer),
[D21] (proxy = another optional tier), [D22] (privacy default).

**Slice 3 DRAFTED (2026-09-12) — Cloudflare Tunnel + Access (primary) + oauth2-proxy/Caddy (alternative):**
reference blueprints in `deploy/auth/`. Cloudflare path: cloudflared dials out (no open ports, home IP never
exposed), Access authenticates at the edge (Entra/email — the user's admin identity is **<you>@<your-domain>**)
and forwards `Cf-Access-Authenticated-User-Email`, which GENGHIS maps to a role. Hardening added to the code
this slice: **`auth.proxy_secret`** — when set, GENGHIS enters *proxy mode* and trusts identity headers ONLY
when the request also carries `X-Genghis-Proxy == secret` (the edge sets it via a Cloudflare Transform Rule /
Caddy `header_up`); a direct-LAN or spoofed hit then gets `default_role` (viewer), never admin. Also added
**`roles_by_user`** (email→role) as the clean fit for Cloudflare's reliable email (alongside `roles_by_group`
for Entra group GUIDs). Verified end-to-end with the exact README config: Cloudflare-authed <you>@<your-domain>
→ admin, unmapped user → viewer, direct hit → viewer. Files: `deploy/auth/README.md` +
`cloudflared-config.example.yml` + `genghis-auth.example.json` + `oauth2-proxy/` (compose + Caddyfile + env).
**Slice 4 DRAFTED (2026-09-12) — one login for all three:** `deploy/auth/docker-compose.sso.yml`, an
opt-in compose override layered over the base (`-f docker-compose.yml -f deploy/auth/docker-compose.sso.yml`).
Open WebUI trusts the edge email (`WEBUI_AUTH=True` + `WEBUI_AUTH_TRUSTED_EMAIL_HEADER`); Grafana trusts it
via `GF_AUTH_PROXY_ENABLED` (auto-signup Viewer, break-glass admin kept). Header defaults to Cloudflare's;
`TRUSTED_EMAIL_HEADER=X-Auth-Request-Email` for the oauth2-proxy path. Same trusted-header rule: firewall
:3080/:3000 to the tunnel + strip inbound header at the edge. Merge validated (`docker compose config`).
One Cloudflare login now covers Control Room + chat + dashboards. **Remaining:** the deployer runs their own
tunnel/tenant (account actions); optional future hardening = validate the `Cf-Access-Jwt-Assertion`
signature in-code (vs the pragmatic shared-secret model).

---

## D24 — The Control Room is the main dashboard (served by the coordinator, not a static artifact); Open WebUI is the branded chat window; two roles, no fork (2026-09-12)
**Decision:** GENGHIS ships **two complementary UIs with distinct roles**, neither requiring a fork:
1. **Control Room = the cockpit.** The "GENGHIS Control Room" becomes a **live page served by the
   coordinator itself** at the serve port `/` (or `/dashboard`), polling the real `/fabric` endpoint — NOT
   a static artifact. It runs on the pure-Python serve, so seeing your fleet needs **zero Docker** (D21).
   It is the friendly tier of the D20 two-tier UX (normal users live here; power users drop to Grafana for
   deep metrics). It is also **where model management lives** (load/unload, goal→model assignment, residency
   — see [D23]). `genghis doctor` (CLI health check) is the SAME status logic in terminal form — build the
   health/status layer once (fabric up? donors reachable? model resident? containers by label?), surface it
   both as `doctor` and as the Control Room's data feed.
2. **Open WebUI = the chat window.** Kept, not rebuilt. **Branded** cheaply with no fork — `WEBUI_NAME=GENGHIS`,
   a mounted logo/favicon, custom CSS — and it lists whatever `/v1` advertises (goal-routes + resident models)
   in its native model dropdown, so model-switching works for free.
**Why:** Open WebUI is a *window onto `/v1`*; it has no concept of the pool or which node holds which model,
so load/unload/residency/goal-mapping **cannot** live there without forking it (a maintenance trap). Put model
management in GENGHIS's own dashboard (which the user already likes as the Control Room) and let Open WebUI be
the pure chat surface. Ties to [D20] (task-first UX) and [D21] (Docker optional — the cockpit is Docker-free).

**SHIPPED (2026-09-12):** the Control Room is now the coordinator's main page at **`/`** (the old fleet admin
moved to `/admin`), served inline from `poc/control_room.html` (read at import; a minimal fallback keeps the
route from ever 500-ing if the file is absent). It **live-polls** `/fabric.json` + a new **`/registry.json`**
every 4 s — Docker-free, on the pure-Python serve. Shows: pool summary (nodes up, pooled GB, default goal,
**warm** model), the **fleet** table (state badges IN/IDLE/DOWN/STORE/SURF), the **goal→model** map as four
live dropdowns (save via `POST /config` — `goal_models` added to the whitelist), the **local model** list with
size/kind and a **● WARM** badge on the resident, and onward links (Open WebUI/Grafana/Prometheus/admin).
Backend adds `resident_status()` and `/registry.json`; verified end-to-end in-browser (live data, badges,
mapping save/clear). Design carries the artifact's language (IBM Plex, ember/steel, dark+light).

**Open WebUI branded (2026-09-12):** `WEBUI_NAME=GENGHIS` (title/tab/sidebar read "GENGHIS (Open WebUI)" —
the "(Open WebUI)" suffix is the project's built-in attribution, kept deliberately as honest + good OSS
citizenship) plus a GENGHIS **mark** (ember core + steel fabric-ring, `branding/genghis mark`) overlaid onto
the container's static icons (favicon/logo/splash/apple-touch/manifest) via **single-file bind-mounts** in the
`genghis` compose project — so the rest of Open WebUI's static assets are untouched and the branding persists
across `compose up`. The PNGs were rendered from the SVG in-browser (canvas → a throwaway local receiver),
since no host rasterizer was available. Verified in-browser: tab, sidebar header, and model avatar all show
GENGHIS; chat history preserved (data volume intact).

**Load/unload control SHIPPED (2026-09-12):** the Control Room's model rows now carry a **Warm now** button
(pre-load a model into VRAM) and, on the resident, an **Unload** button (free the VRAM on demand) — backed by
`POST /residency` (`{action:"load"|"unload", model}`) + `warm_model()`. A model too big for a single node is
rejected with a plain-language note ("needs pooling — runs on demand"), since only solo-anchor models can be
kept warm. Verified in-browser: Warm now → ● WARM badge + freed-on-Unload (VRAM back to idle). **D24 is now
complete.**

**Front-door hub (2026-09-12):** the Control Room opens with a prominent **Local Resources** card grid —
Open WebUI (:3080), Grafana (:3000), Prometheus (:9090), Fleet Admin (`/admin`), Coordinator API (`/v1` +
`/fabric`) — each with a one-line description and a **LIVE up/down badge** (browser reachability probe every
15s: same-origin via `fetch().ok`, cross-origin via a `no-cors` fetch + timeout — the thing a static web page
hosted elsewhere can't do). This makes **`localhost:8899/` the single memorable front door** to every local
service; the old small footer links were promoted into this first-class menu.

---

## D23 — Model registry + per-model `/v1`: local GGUFs are individually selectable; goal→model mapping is explicit; load/unload = residency (2026-09-12)
**Decision:** GENGHIS gains a **model registry** — the keystone named in [D20]. Each GGUF in the local model
repo becomes a **first-class, selectable model** with metadata (kind: text/VLM, mmproj, chat-template, size,
which node(s) can hold it). Three capabilities: (a) **`/v1` advertises every resident model by name** AND the
four effort **goal-routes** (`genghis-fastest…biggest`); (b) **goal→model mapping is explicit + user-set**
(e.g. `fastest`→a 1.5B, `biggest`→the 70B) instead of today's single global `config.json "model"` behind all
goals; (c) **load/unload = residency control** — a resident `llama-server` per assigned model (warm, no
per-request re-stream), managed from the Control Room ([D24]). A **VLM (e.g. SmolVLM) needs its mmproj + an
image**, so the registry records that and the plain text-chat path won't offer it as a text model.
**Why:** This is the actual product — "load & run models with pooled compute/memory." Today `/v1` exposes only
the 4 goals bound to ONE configured model, so "I dropped a GGUF in the folder and can't select it" is
unsolved; the registry is the real fix and the prerequisite for BOTH the Control Room's model panel and Open
WebUI's dropdown ([D24]). Model-residency (warm llama-server) was already the next-build keystone in [D20]/STATUS.

**ONE-FOLDER model (settled 2026-09-12):** the mental model a user must hold is exactly one thing — **"put
GGUFs in your models folder; GENGHIS lists them, you pick which goal uses which, it runs them pooled."** The
donors NEVER need the file (the orchestrator streams compute/shards to them, not the model). The **local
registry is a read-only scan** of that folder — it does NOT copy, sync, or "fill the store." The separate
**coordinator model store** (`/models` fetch-if-missing) is an **ADVANCED, opt-in feature for people with
MULTIPLE launch machines** who don't want to copy a big file to each — NOT a required step, and never an
automatic push (you don't want a 40 GB model silently shipping itself over WiFi). Docs must present "your
models folder" as the one concept and label the store "Advanced: sharing across multiple launch machines."
No `registry publish`/auto-sync is built; deferred until a real multi-client need appears.

**Slice 1 SHIPPED (2026-09-12):** `build_registry()` scans the model dir(s) → each GGUF is a selectable model
with metadata (name/size/kind text-vs-VLM/mmproj); `/v1/models` advertises the 4 goals PLUS every model;
`resolve_model()` maps a request's "model" (a `genghis-<goal>` OR a concrete model name) to (GGUF, goal);
`config.goal_models` sets which model each goal uses (CLI: `registry`, `registry map <goal> <id>`,
`registry default <id>`). Verified: 5 models listed, VLM detected (SmolVLM only), `genghis-fastest`→mapped
1.5B, by-name selection runs that GGUF.

**Slice 2 SHIPPED (2026-09-12) — residency:** a WARM `llama-server` holds the model in VRAM and GENGHIS
**proxies** to it (`ensure_resident`/`_proxy_resident`), so a repeat call skips the re-load — measured **32B:
~15 s cold -> ~2.8 s warm** (was ~15 s on *every* call), model held resident (~20 GB). Bonus: llama-server
applies the GGUF's **own chat template** natively (`--jinja`), so the fast path drops the manual ChatML/ANSI
scraping and streams cleanly. **Scope:** SOLO-on-the-local-anchor plans (the felt case); a **pooled/split**
run (e.g. `biggest`->70B) **stops the resident first** to free the local GPU's VRAM, then uses the per-request
`llama-cli` path. Swapping models stops the old resident and starts the new one; the resident is torn down on
serve exit (atexit + finally). Toggle: `GENGHIS_RESIDENCY=0` or config `residency:false`. Needs `llama-server`
built (`--target llama-server`). **Root-cause fix bundled:** fleet.json writes are now **atomic**
(`save_fleet()` = temp-file + `os.replace`) — a serve killed mid-write can no longer leave a torn, unparseable
fleet.json (that corruption had crash-looped the launcher 3x). **Slice 3 ideas:** keep >1 model resident when
VRAM allows; idle-unload timeout; a `/unload` control + residency shown in the Control Room ([D24]).

---

## D22 — Nothing of ours ships: no node names, IPs, MACs, or accounts leak; real topology is generated at first run (`genghis init`) (2026-09-12)
**Decision:** The release must contain **zero real fleet data**. Concretely:
1. **Ship examples, not instances.** `fleet.json` and `config.json` become `fleet.example.json` /
   `config.example.json` with placeholders (`coordinator.local`, `node-a`, `0.0.0.0`, no MACs/accounts). The
   real `fleet.json` is **untracked** (git-ignored, like `config.json` already is) — it lives only on the
   user's machine.
2. **No baked-in address.** The compiled default coordinator URL (`http://<node-ip>:8899/fleet.json` — a
   real personal IP) is removed. The coordinator finds its authority via **mDNS discovery** (`genghis_mdns.py`,
   already present) or an explicit `GENGHIS_COORD`/`GENGHIS_FLEET_URL` env — never a hardcoded IP.
3. **`genghis init` generates the real files at first run**, from THIS machine: name this node, choose its
   role + models dir, auto-discover donors over mDNS (or add manually), write a fresh `fleet.json`/`config.json`
   with no trace of the original fleet. Idempotent; safe to re-run.
4. **The public repo starts clean.** Real IPs/MAC/AzureAD username are in **git history** (15+ committed files
   carry `192.168.x`), so git-ignoring now does not un-publish them — the release repo must be a **fresh init
   (squashed, no history)**; the private dev repo keeps full history. History rewriting is a deliberate
   release-time step, never done silently.
**Why:** On a stranger's machine, shipping our topology is both a privacy leak (our IPs, the Pi's MAC, an
internal AzureAD account) and a correctness bug (their install would point at our Pi). The example+first-run
pattern is the standard fix (`.env.example`), and it makes a fresh install **find its own LAN** instead of
ours. Highest-priority release blocker — precedes feature work. Ties to [D18] (installer/first-run standard).

---

## D21 — Docker is an OPTIONAL, self-identifying tier: one labelled `genghis` compose project, never bare `docker run`; the core fabric needs no Docker (2026-09-12)
**Decision:** The whole containerised side of GENGHIS — Open WebUI (chat window) + Prometheus + Grafana
(dashboards) — is now **one Compose project named `genghis`**, defined in the repo-root
[`docker-compose.yml`](docker-compose.yml). Every service carries `com.genghis.*` labels
(`com.genghis.project=genghis`, `com.genghis.tier=ui`, `com.genghis.role=<chat-ui|metrics|dashboards>`,
`com.genghis.managed=true`) plus the Compose-assigned `com.docker.compose.project=genghis`. Three rules:
1. **One project, one teardown.** `docker compose up -d` / `down` from the repo root manages the entire UI
   tier as a unit — no orphans. "What did GENGHIS create?" is always answerable:
   `docker ps --filter label=com.genghis.project=genghis`.
2. **Never `docker run` these by hand.** A bare `docker run` makes an **unlabelled, randomly-named orphan**
   nothing can cleanly reclaim — that is exactly the recurring stray-container mess that prompted this
   (unnamed `ollama`/`prometheus`/`open-webui` containers with `adjective_scientist` names and no
   orchestration labels). Installers and docs use Compose only. The `monitoring/docker-compose.yml` was
   retired to a pointer stub so old `cd monitoring && docker compose up` muscle-memory fails loudly instead
   of starting a second, differently-named project.
3. **Docker is a convenience layer, NOT a dependency.** The **core fabric** (coordinator + `/v1` serve) is
   pure Python + llama.cpp and needs no Docker at all. Docker only powers the *browser UI* tier. If Docker
   Desktop melts down (as it did during setup — orphaned WSL sockets, VM disk-attach failures), the fabric
   keeps serving on `/v1`; you lose the chat window, not the pool. This bounds the blast radius of the
   single flakiest dependency in the stack — critical for a public release where most support pain will be
   Docker-Desktop-shaped, not GENGHIS-shaped.

**Why:** Prompted by the release lens — on a stranger's machine you can't say "the stray is the one with the
random name"; they have their own containers. GENGHIS's own footprint must be **named, labelled, and
reclaimable by one selector**, and the hardest-to-support component must be **optional**, not load-bearing.
The abandoned alternative — a background "stray-container watcher" — was rejected as invasive (it snoops the
whole daemon) and as treating a symptom; the fix is to make strays *impossible for GENGHIS to create*. The
watcher was demoted to a dev-only diagnostic ([`poc/dev-tools/watch-docker.sh`](poc/dev-tools/watch-docker.sh)).
**Follow-ups (not yet built):** a pull-based `genghis doctor` health command that reports GENGHIS's own
containers/ports/serve by label and offers fixes (the right shape vs. a watcher); and a `genghis ui up/down`
wrapper so the optional tier is one obvious command. Ties to [D18](#) (packaging/installer standard) and the
design-for-shipping bar.

---

## D20 — GENGHIS is a home task-fabric: memory-pooling for big models is THE mission; ambient multi-model agents are the second act; the two are user-switchable modes (2026-09-08)
**Decision:** GENGHIS's **primary job is fixed and comes first: pool compute + memory across owned LAN
devices to run models no single box can hold — the direct answer to the memory crisis.** That is the
mission; nothing displaces it, and the leverage is *open release* — we don't end the crisis by running one
fleet, we end it by giving everyone the tool to reclaim the idle memory they already own. **A second
workload** is now an explicit direction: the same fleet also **places and coordinates a swarm of SMALL
always-on models as task-agents** (e.g. watch 2 cameras → a status/summary model → a voice/VoIP model that
calls the person). The two are **user-switchable MODES**, usually **time-multiplexed on the same hardware**,
not forced to run at once: *working from home → spin up the pool for one huge model; done for the evening/
weekend → spin the big model down and let the home's agents run.* Some agents (a doorway watch) run always;
the heavy pooled model and the heavy agent load trade the shared GPU by mode.

Two workload types:
- **Pooled-big (interactive):** one large model **layer-sharded across the pool** — the memory-crisis trick.
  What we've built (`/v1`, effort-routing, self-healing planner).
- **Placed-small (ambient):** many small models, each fitting **one node**, running persistently as
  event-driven agents the coordinator **places and wires**. NOT sharded (they fit); here the coordinator is a
  **scheduler + task runtime**, not a memory-aggregator.

**Interaction model = task-first, two tiers.** Non-technical users pick **tasks/skills** ("watch the front
door and call me about packages"), not models — GENGHIS assigns the model behind each; the default surface is
a **skill catalog + no-code "when-this-do-that" routine builder + a work/home mode switch.** A power-user
**advanced tier** exposes a model registry, per-node placement, and direct wiring. Effort-routing
(goal-as-model-name, D17) is the seed of hiding models behind intent.

**Why:** the memory crisis is why the project exists — pooling to run the otherwise-unrunnable is the
world-problem we mean to fix, and it stays #1. But the same coordinator ("the brain") is naturally a
household scheduler too, and real users will want both — heavy pooled work when they need it, cheap always-on
agents the rest of the time — from hardware they already own. The mode-switch framing (not concurrent) also
**resolves the GPU/VRAM contention honestly**: you rarely run voice + a pooled 70B at once — you switch. This
is exactly what **The Fold** *(coming soon)* described
(senses→mind→face, effort-routed, smallest-thing-that-suffices); D20 just states the two-workload/one-brain
shape and the priority order.

**Consequences / build order (residency is the keystone — already next):**
1. **Model residency** — a resident `llama-server` per active model (not spawn-per-call); everything ambient
   stands on it, and it also fixes the always-on chat cold-load.
2. **Model registry with metadata** — kind (text/vision/speech/embedding), mmproj, chat template, size,
   fits-which-node. Today GENGHIS has one `MODEL` + a folder of nameless files (why a dropped-in SmolVLM
   "just sat there" — a VLM needs its mmproj + image path, not a text-chat slot).
3. **"Place-many-small" scheduler mode** — assign whole small models to nodes, track per-node memory across
   several resident models (new bookkeeping beside "shard-one-big").
4. **Mode switch** — one control to bring the pooled-big model up/down and the agent set up/down (the
   work/home toggle), with VRAM accounting so they don't collide.
5. **Task/automation runtime** — the event→reason→act wiring (the away-owner watch generalized into a small
   rules engine).
6. **I/O adapters** — camera (RTSP/USB), voice (PersonaPlex), VoIP (SIP extension — the scoped FXO/telephony
   work), notify (email/SMS).

**Honest caveats:** (a) small task-models fit one node and must NOT be sharded — pooling is one of *two*
tricks, be precise in the story; (b) VRAM/concurrency is real bookkeeping even with mode-switching
(always-on agents coexist with whatever else runs); (c) the no-code task surface is the biggest "for
everyone" lift and easy to underestimate. **Approach: prototype ONE vertical end-to-end** (2 cameras →
status model → voice call) on the fleet with hardcoded wiring — the **NUC** (32 GB, always-on, iGPU) is the
natural host — then generalize into the catalog/registry/scheduler. Prototype, then template.

## D19 — The name is GENGHIS; "the GENGHIS Protocol" is a formal descriptor, not the everyday name (2026-09-08)
**Decision:** The project's name/brand is **GENGHIS** (bare, one word). "**The GENGHIS Protocol**" survives only
as a **formal descriptor** — the charter title, the `NOTICE` trademark lines, a first-mention definition, or a
tagline ("GENGHIS — distributed AI memory pooling"). Everywhere it functions as *the name* — README H1, the
hero wordmark, doc titles, the CLI, conversation — use bare **GENGHIS™**. The brand was always GENGHIS
underneath (the ™ was reserved on the short name, D3); "…Protocol" was only the descriptive wrapper.

**Why:** one evocative word is how infrastructure gets named — Redis, Kafka, Kubernetes — and GENGHIS reads as
a *presence*: a conqueror uniting many under one banner, which is literally the architecture. "The ___
Protocol" names a *category*, not a product; in running text the "The" and "Protocol" are dead weight around
the part that lands. The maintainer settled on the bare name.

**Consequences:** Front-door docs swept to GENGHIS (README H1 + hero wordmark → gradient "GENGHIS™";
STATUS/DECISIONS titles). Full title **kept** in its formal homes: the `PROJECT_CHARTER` title, `NOTICE` (both
marks claimed — nothing legal changes), the README license note, and `Project.md` (the superseded founding doc
where the name was coined — left as the historical record). **The repo/folder name `The_GENGHIS_Protocol` is
left as-is** — remotes, memory, and muscle-memory point at it; a rename is a deliberate later-if-ever step
(GitHub 301-redirects the old URL, but it ripples into the local path + git remotes), not a blocker.

## D18 — Packaging & dependency strategy: pin exact versions, bundle almost nothing, install per role (2026-09-08)
**BUILT (2026-09-12):** the per-role **preflight installers** now exist in [`install/`](install/) — `install-windows.ps1`
(client/serve) and `install-linux.sh` (`--role coordinator|donor|client`, `--accel cpu|cuda|vulkan`). Each
**detects prereqs and reports in plain language, offers the automatable installs with confirmation (winget/apt),
and GUIDES the two un-automatable steps** (the VS C++ workload; the NVIDIA driver) — never error-dump-and-exit —
clones + builds llama.cpp at the pinned commit, runs `genghis init`, and wires reboot-survival. Idempotent, with
a safe `-PreflightOnly`/`--preflight` "doctor" mode. Windows detection verified on real hardware (found Python/
Git/CMake/MSVC/CUDA and cleared the box to build); the build + Linux paths field-test on a fresh machine. See
[`install/README.md`](install/README.md). Docker tags + llama.cpp commit were already pinned (below).

**Decision:** Public releases ship as **pinned installers per role** (coordinator / donor / surface / client),
**not** one fat bundle — reproducibility comes from **pinning versions, not bundling bytes**. You cannot ship
one binary for a fleet spanning x86+CUDA, x86+Arc/Vulkan, aarch64-CPU, and Mac/Metal, so the release carries
the *recipe and the versions*, and each role installer fetches/builds the right artifact for that box.

Per dependency:
- **Python** — *link + detect* a minimum version; do not bundle. The coordinator is **pure stdlib, zero pip
  deps** (even mDNS is hand-rolled, D13), so there is nothing to vendor. `serve.sh` already fails LOUD
  ("install python3") instead of looping silently.
- **llama.cpp** — the one dependency we pin **hard**, because **RPC has no cross-version wire compat → every
  node must run the same build** (the D17 donor gate). Recommended for release: pin to a llama.cpp **release
  TAG and use their prebuilt binaries** for the common targets (Windows CUDA/Vulkan, Linux, macOS Metal), with
  **build-from-source as the fallback** for exotic nodes — turning the biggest setup friction (install a
  toolchain + compile ~30 min) into download-and-go for the 80% case. (Today: build-from-source at pinned
  commit `eab8ee41f`.)
- **Docker + GUI/monitoring images** (Open WebUI, Grafana, Prometheus) — *link* Docker (don't bundle the
  platform); ship the compose file with **pinned image tags**. This layer is **optional** (GUI/monitoring), not
  core.
- **Models (GGUF)** — **never bundle** (40 GB+). The coordinator IS the repo (D11); donors/clients
  fetch-if-missing with HTTP-range resume (built). Link HuggingFace for first-stage staging, or bring-your-own.

Per role: **surface** = self-contained Tizen `.tpk` (Samsung owns the runtime — no Python/Docker/llama.cpp);
**client** = *nothing to install* (any OpenAI `/v1` client, D17); **coordinator** = Python(stdlib) + the script
+ *optional* Docker for GUI/monitoring; **donor** = llama.cpp for its accelerator + a tiny launcher (the heavy
role).

**Why:** matches the heterogeneous-fleet reality and the project's "smallest thing that suffices" law —
bundling a per-OS Python/Docker runtime is the wrong kind of heavy, while *unpinned* deps are the exact "it
broke because upstream moved" friction we exist to design away (feedback-design-for-shipping). Pinning + a
role installer that detects/fetches the correct artifact is the right shape, and keeps the RPC lockstep D17
depends on.

**Consequences / open TODOs (release-hardening):**
1. **Pin the Docker image tags.** — **DONE (2026-09-08):** `prom/prometheus:v3.14.0`, `grafana/grafana:13.2.1`
   (in `monitoring/docker-compose.yml`), and the Open WebUI container repinned `:main` → `:v0.11.3`.
2. **Choose the llama.cpp distribution.** — **RESOLVED (2026-09-08): donor/RPC nodes build from source at the
   pinned commit.** The official prebuilt Windows/Linux **release binaries OMIT the RPC server** — it's gated
   behind `-DGGML_RPC=ON`, which release CI does not set — so there is *no* prebuilt path for a donor; you
   compile regardless. Prebuilt would help only the *non-RPC* roles, and only after bumping the whole fleet to
   a common release tag (large blast radius). RPC also enforces a protocol-*version* handshake, but same
   version ≠ same wire-encoding across commits, so **identical-commit is the only zero-risk guarantee**
   (research 2026-09-08). Build flags: `-DGGML_VULKAN=ON -DGGML_RPC=ON` (+ Vulkan SDK) for the Arc path.
3. **Declare a minimum Python version** in the installers (mostly handled already by `serve.sh`'s loud-fail).
4. **Installer behavior = preflight + guided remediation, NOT error-dump-and-exit** (the maintainer, 2026-09-08). The
   install/build scripts **detect each prerequisite up front**; a missing one is **reported in plain language**
   and, where the installer can fix it, it **offers to install it with explicit confirmation** (e.g. a
   winget/apt package) — never silent auto-install; where it *can't* (a GPU driver, Docker Desktop, a
   reboot-gated step) it gives a **specific manual instruction**, not a stack trace. **Re-runnable/idempotent**
   so a just-fixed prerequisite resumes rather than restarts. This generalizes `serve.sh`'s loud-fail-and-
   auto-recover to the whole install path — and because everything is pinned (this decision), **everyone hits
   the same preflight**, so the guided experience is as near-identical across machines as possible.
This is the home for the "fold node-setup into installers" work tracked in STATUS next-steps.

## D17 — Standards-first public API: OpenAI `/v1` for inference, Prometheus for monitoring, native endpoints for the pool/surfaces (2026-09-07)
**Decision:** As GENGHIS opens to the public, the API is designed in **two layers**. A **borrowed/standard
layer** so the world integrates for free — **inference = the OpenAI API** (`/v1/chat/completions`,
`/v1/models`, SSE streaming); **monitoring = Prometheus** (`/metrics`, + `/healthz`/`/readyz`); **discovery
= mDNS/DNS-SD** (`_genghis._tcp`); **auth = Bearer token**. And a **native layer** for our actual
differentiation — `/fleet.json`, `/fabric`, `/config`, `/models`, `/hearth`, `/report`, `/reply`, and the
**effort-routing goals**. Third parties integrate as one of **three roles**: **surface** (present/interact —
native endpoints, HEARTH is the reference), **client** (use the pool — `/v1`), or **donor** (contribute
compute — `ggml-rpc-server` at the pinned commit + `POST /report`). **Effort-routing is surfaced as model
names** (`genghis-fastest` … `genghis-biggest`) so any OpenAI client selects a routing goal with zero custom
code. Job submission ships as the industry **triad**: **`/v1` API (spine) + Open WebUI (adopted, not built)
for humans + the CLI (power users)**; an async **`POST /jobs`** layer comes later for unattended/batch.
Captured in [INTEGRATION.md](INTEGRATION.md).

**Why:** the biggest lever is **not inventing an API where a standard exists** — then "do industry monitoring
tools work?" and "can someone use it in ComfyUI?" become free rather than features we build. `/metrics` is
already Prometheus (done, D14); adopting OpenAI `/v1` makes ComfyUI's LLM nodes, LangChain, LM Studio, and
Open WebUI work by just setting a `base_url`. We invent only the pooling/fabric/effort-routing — the part
nobody else has. HEARTH already proves the surface path end-to-end on a retail TV.

**Consequences / honest caveats:** `/v1`, `/healthz`/`/readyz`, `POST /jobs`, and Open WebUI adoption are
**roadmap** — today inference is CLI/client-driven (marked as such in INTEGRATION.md; candor over vaporware).
The **donor** path is gated by the **pinned-llama.cpp-commit lockstep** (RPC has no cross-version compat), so
"any device can donate" holds only within a matching build; non-llama.cpp accelerators (Hailo/Coral vision
NPUs) are **perception nodes** (surfaces that report events), not donors. A public multi-developer deployment
will need **scoped/capability keys** beyond the single shared PIN (D14).

---

## D16 — TV USB re-benchmarked: a USB-2.0-class cold-archive STORE tier, not the hot repo (2026-09-06)
**Decision:** Re-measured the TV's external drive with a real on-device benchmark (HEARTH b27 `BenchStorage`:
write+read a **256 MB** file — larger than the ~190 MB free RAM so the read can't fully cache — in 1 MB blocks,
timed, `fs.Flush(true)` to force the write to disk). Result on the retail set: **write ≈ 19.3 MB/s, read ≈ 27.1
MB/s.** This **confirms D11**: the TV is a legitimate **cold-archive / secondary STORE tier**, but the **Pi stays
the hot repo** (always-on, ~858 GB, already the HTTP authority).

**Why / the limiter:** the CU7000 has **one USB-2.0 port** (Samsung spec: "1 USB-A port … Bluetooth 5.2"), whose
real-world ceiling is ~30–40 MB/s. The **read (27) is already near that port ceiling → the port, not the drive,
is the limiter**; a USB-3 drive in the same port would not go meaningfully faster. Write (19) sits lower — normal
for flash write + the sandboxed-app path. (Open follow-up if it ever matters: benchmark the *same* drive on a
USB-3 host — flies ⇒ port-bound, stays ~20 ⇒ drive-bound. Not worth a build now.)

**Consequence:** if the TV ever serves cold models, it needs a small HTTP file-server *inside* the Tizen app (the
OS gives no always-on LAN read path off the USB) — deferred, low priority. The `diskbench` field is whitelisted in
the coordinator's REPORTABLE and the live result is banked in `fleet.json`.

---

## D15 — The TV is the Fold's FACE + MOUTH; the EARS live off-TV (the mic is a sealed Samsung garden) (2026-09-06)
**Decision:** After an exhaustive on-device investigation (HEARTH probes **b19→b26**), a third-party app on the
retail **UN50CU7000** **cannot obtain the microphone by any route — audio or text.** So the Fold's **ears do not
live on the TV**: they go on any device that shares its mic with apps (a **laptop / phone / small Pi node**, or a
**Bluetooth earbud** paired to one), feeding **PersonaPlex** (real-time full-duplex speech-to-speech on the 5090,
measured **snappy**). The **TV remains the FACE** (HEARTH) and can be the **MOUTH** (speakers).

**The maze, banked so nobody re-runs it:** native `Tizen.Multimedia.AudioCapture` opens + streams but returns
**pure silence** (all-zero PCM), under every stream policy (default / VoiceRecognition / Voip). The BT earbud's
HFP/SCO mic **is visible** via `AudioManager.GetConnectedDevices()` but `AddDeviceForStreamRouting` throws
**`AudioPolicyException`** (that API is output-routing, Voip-only — no app path to bind capture). The Samsung
**Microphone API is WEB-only** (`webapis.microphone`) and the **`.wgt` won't install** on the retail set. Keyboard
voice-to-text transcribes into **Samsung's IME/search overlay, never into our focused field**; this set has **no
Bixby/assistant**, only search. There is **no built-in far-field mic** (the next model up has one; this one needs
the SmartThings app or a Samsung soundbar). Every door is Samsung's.

**Why it matters:** this closes the crux the whole voice layer hinged on, with a clean architecture falling out of
it — **voice on the 5090, mind pooled on the donors** (they can't coexist: PersonaPlex ~20 GB leaves ~5 GB on the
24 GB card). **Do not re-open the TV-mic maze.**

**Corrected on the record (candor over sycophancy):** the CU7000 has **no dedicated NPU/TPU**. Its Crystal
Processor 4K runs Tizen `machine_learning.inference` / Samsung ONE (`nnfw`), but with **no discrete NPU the runtime
falls back to CPU/GPU delegates** (XNNPACK / OpenCL) — CPU/GPU-class, not accelerator-class. An earlier overstatement
of a "64-bit NPU pipeline" (which a web search then sycophantically echoed back) was retracted: two AIs agreeing is
not evidence.

---

## D14 — Optional shared-PIN auth; open by default; the gate for API / monitoring / off-LAN (2026-09-06)
**Decision:** `serve` is **open by default** (LAN-trusting) but lockable with a **shared PIN** held in
`config.json` (`auth.token`), with three `auth.protect` levels: **off** · **writes** (default once a PIN is
set — gates only `POST /config`, so the TV/donors/reads keep working) · **all** (gates every endpoint but
the admin login page — the off-LAN posture). The token is **never served back** (`public_config()` redacts
it; only `{protect, locked}` is exposed); callers pass it as `X-Genghis-Token`, `Authorization: Bearer`, or
`?token=` (TV-friendly). Also added **`GET /metrics`** (Prometheus text) + a `monitoring/` bundle
(Prometheus + Grafana dashboard + "donor down" alert, one-command compose).

**Why:** the fleet was wide-open on the LAN — anyone could `POST /config` and reconfigure it. A *lightweight*
gate (no accounts, no TLS, no framework — a shared secret + a header) fixes the real risk with near-zero
footprint, and is the **single thing that unlocks the brainstorm trio**: a published **API**, standard
**monitoring** (`/metrics` scraped by Prometheus/Grafana), and **Azure/off-LAN** access (PIN + `protect:all`
+ a tunnel). Kept it open-by-default and writes-first so turning it on never breaks the TV or donors.

**Consequence / next:** to run `protect: all` fleet-wide off-LAN, the TV Settings + `donor-report.sh` need to
send the token (small follow-on). Lockout recovery: clear `auth.token` in `config.json` on the Pi + restart.

---

## D13 — Zero-config coordinator discovery via pure-stdlib mDNS (no zeroconf/Avahi) (2026-09-06)
**Decision:** The coordinator advertises itself on the LAN as **`_genghis._tcp.local`** (mDNS/DNS-SD) so a
fresh client or TV finds it with **no hardcoded IP**. Implemented in **pure stdlib** (`poc/genghis_mdns.py`
— a hand-rolled responder + querier on UDP 5353), because the reference fleet has **neither `zeroconf` nor a
running Avahi** and the project ethos is stdlib-only + zero install friction. Python planning clients
**auto-discover** only when the configured/default coordinator is unreachable and nothing is pinned
(`GENGHIS_COORD` / `GENGHIS_FLEET_URL` pin it); the TV uses **Tizen NSD** to browse the same service.

**Why:** a Store-distributed HEARTH and any new client can't ship a fixed IP — every install has a different
Pi. mDNS is the standard answer and, done in stdlib, adds no dependency to install or break. Three real
gotchas were banked in the commit: one-shot querier needs an **ephemeral port + QU unicast reply**;
**`SO_REUSEADDR` only, never `SO_REUSEPORT`** (REUSEPORT load-balances multicast to one socket, breaking
fan-out); and **strip the trailing dot** when matching the service name.

---

## D12 — The coordinator houses human config; a featherweight web admin is the management surface (2026-09-06)
**Decision:** Human-set settings live in **`config.json` on the coordinator** (the counterpart to the
*measured* `fleet.json`): `default_goal`, friendly `names{id}`, `roles{id}`, `hearth{}`, `auth{}`. It's
read/written over HTTP (`GET /config.json` JSON for the admin, `GET /config` **tab-text** for the TV so the
Tizen side needs no JSON parser, `POST /config` whitelist-merge) and edited from a **self-contained
vanilla-JS web admin** served at `/` (name/manage nodes, set roles + default goal, set the PIN). The
coordinator also gained its own **build stamp** (`COORD_VERSION`). "Promoting" was renamed **managing**.

**Why:** device identity/naming and defaults were hardcoded or absent. Centralizing *human* config on the
always-on authority (distinct from measured state) makes it the single source of truth end-to-end. The admin
is deliberately **featherweight** — extends the existing stdlib `http.server` (now `ThreadingHTTPServer`) +
one static page, **no Flask/FastAPI/Node** — so it costs the Pi almost nothing (the brief was "minimal impact
on memory & CPU"). The TV then **pulls the rest of its config** from `/config` (default goal, its friendly
name), so the coordinator configures the surface.

---

## D11 — Model repository lives on the Pi, not the TV; TV reclassified STORE → SURFACE (2026-09-06)
**Decision:** The **coordinator (Pi) is the model repository** — GGUFs staged in `~/genghis/models/`, served
at **`GET /models`** (list) + **`GET /models/<name>`** (streamed, **HTTP Range / resumable**). A client's
`GENGHIS_MODEL` may be a full path **or a bare name**, resolved against a local cache and **fetched-if-missing**
from the Pi. Consequently the **TV's role changes `storage` → `surface`**: it is now purely the **face of the
Fold**, not a store; its USB is kept only as optional cold archive / HEARTH on-device assets.

**Why (reverses the D8 "TV = STORE with a USB" direction):** the TV's USB was *validated* but there is **no
fast, always-on LAN read path** off it — the sandboxed Tizen app or `sdb` in Dev Mode only, at ~18 MB/s on
~190 MB RAM (a 42 GB model ≈ 40 min, and only in Dev Mode). The **Pi is the right home**: always-on, **~858 GB
free**, and *already* the HTTP authority — a `/models` endpoint is a few lines. Range/resume means a dropped
42 GB WiFi transfer picks up where it left off (verified byte-identical). **Done:** all three models (1.5B,
32B, 70B) staged on the Pi; the coordinator can finally size them.

---

## D10 — One fleet, one source of truth: the Pi is the authority; nodes self-report (2026-09-04)
**Decision:** There is **one** authoritative `fleet.json` — on the **always-on Pi coordinator**. Every node
**self-reports its own live capacity** to it (`POST /report`: the TV its USB, compute donors their free RAM,
the anchor its VRAM/throughput). Planning commands (on the laptop) **fetch** the authority (`GET
/fleet.json`), overlay their *own* vantage latency via heartbeat, and plan; the local file is an offline
cache. `serve` reads local — it *is* the authority; only clients fetch.

**Why:** we had drifted into **two** `fleet.json` copies (the laptop planned off one, the Pi's `serve` fed
the fabric off another), so "measure, don't assume" only reached the *display*, not the *plan*. Unifying
makes **what a run uses == what the fabric shows.** And capacity that is *hardcoded* goes stale silently (a
swapped USB, a busy donor) — self-reporting makes it *measured*. The split of concerns: **intrinsic** facts
(capacity, throughput, identity) are authoritative on the Pi; **vantage** facts (latency, up/down) stay
per-observer (they legitimately differ by who's looking).

**Consequence proven (Run #10):** because planning now reads *live* capacity, the coordinator **declined a
back-to-back run that would have OOM'd** a donor still holding a prior shard — then ran clean once it freed.
Measure-don't-assume caught a real fault, unprompted. (Led to D-adjacent `plan_with_settle`: settle & retry
on a *transient* shortfall rather than give up.)

---

## D9 — The client's own 5090 is the ANCHOR node; wire it in and fill it first (2026-09-03)
**Decision:** The laptop (client/orchestrator) has an **RTX 5090 Laptop GPU (24 GB, Blackwell `sm_120`)** —
almost certainly the strongest single node in the fleet. It must become the **anchor**: for any model that
fits in ~20 GB usable VRAM, **run it 100% local (no pool)**; only when a model *exceeds* the 5090 do we
recruit donors, and then the **5090 is filled first, donors carry the overflow.**

**Why this matters (the honest reframe):** pooling is a **capacity** play with a **latency cost** (every
token traverses every shard serially — cf. **D6**). With a 24 GB local GPU, spreading a model that already
fits would only make it *slower*. So the anchor topology (bulk local + no hop, remote overflow only) is
strictly better than the all-remote pipeline we'd been demonstrating.

**The gap this fixes:** the 5090 was **invisible to GENGHIS**. The laptop's llama.cpp build is **CPU+RPC
only** — no `ggml-cuda.dll`, so `-IncludeLaptop` added only the *CPU*, and every run to date pooled *without*
the strongest card. The laptop was modeled as a pure conductor; it wasn't even a compute node in `fleet.json`.

**Plan (Milestone 16):**
- **A — CUDA build:** rebuild llama.cpp `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120` **at the same pinned
  commit** the donors use. RPC has zero cross-version tolerance, so keeping the commit means **donors need no
  rebuild**. (Only a *newer* llama.cpp would force re-pinning the whole fleet — not this milestone.)
- **B — anchor planning:** register the laptop in `fleet.json` (`role:compute`, `accel:cuda`,
  `free_mem_mb≈20000`, `latency:0`); `plan_v3`/`run_decision` fill `CUDA0` to capacity before recruiting RPC
  donors; device list becomes `CUDA0,RPC0,…`.
- **C — validate:** a ~13B model → `CUDA0` only, donors idle; a 70B-class model → 5090 anchored + donor
  overflow, beating the old all-remote tok/s (log to [poc/RESULTS.md](poc/RESULTS.md)).

**Prereq check (2026-09-03):** **PASSED** — CUDA Toolkit **13.3** (≥12.8 required for Blackwell) + cmake
4.3.3 present on the laptop. Phase A is unblocked.

**Scope boundary:** the practical "must pool" threshold for this fleet is ~20 GB. Below it, local-only wins;
GENGHIS's value *for this operator* is the **>24 GB tail** (70B+ at real quant, 100B+). That's a feature of
the honest scoping, not a limitation.


**Addendum (2026-09-13) — the local anchor wins whenever the model FITS it, regardless of a faster remote GPU.** D9 got
"anchor first" for free on the laptop only because the 5090 also had the top throughput. On the NUC (Arc, 17.6 t/s) the
`fastest` planner shipped a 1.5B to the 5060 Ti (38 t/s) over RPC — faster per token, but a remote solo run re-streams the
model on every call while a local run has no network at all and, with residency, stays warm (~1 s to answer). `plan_v3` now
picks THIS host's own anchor for any non-`biggest` goal when the model fits it; a remote node wins only when it does not
fit locally — capacity decides, not comfort. Verified: NUC vantage → `SOLO on nuc-155h (Vulkan0)`; laptop and Pi vantages
unchanged. The fabric now also labels another host's private anchor **FOREIGN — "<host>'s own anchor — not usable from
here"** instead of a misleading IDLE, and "anchor" is the node the *plan* leans on.
---

## D8 — TV/Tizen ACCESS validated; bare TV donates nothing → value pivots to HEARTH; Store role needs a USB (2026-09-02)
**Decision:** A Samsung Tizen TV can be fully accessed and have our code run on it — that part is validated.
Advances **D5** (which parked TVs as "writable but partner-gated → Phase-3"): the partner-gate fear was
**bad info** — sideloading to *your own* TV needs only **Developer Mode + a Samsung author/distributor
certificate bound to the TV's DUID**, over **sdb on the LAN** (or USB). Fully supported.

> **CORRECTION (same night, by measurement — the important part).** The *storage-donor* premise below was
> **wrong**. We deployed a probe app and read the TV's real specs from **inside** (`Tizen.System`):
> **4 cores · 1578 MB RAM / only ~190 MB free · ~0.9 GB internal storage free · NO USB drive attached.**
> The "~1 TB" was a **phantom** (there is no drive). So the **bare TV is neither a compute nor a storage
> donor.** Its **proven value is the HEARTH surface** (the spun-off project — an agent-owned display/comms
> surface on the household screen): our signed Tizen app builds, installs, and runs **full-screen on the set.**
> **To use the TV as a Store (model repository) at all, a USB drive must be plugged in**, plus the
> `http://tizen.org/privilege/externalstorage` privilege in the manifest, then verify a TV app can read/write
> arbitrary files there (worth testing when a drive is available). Until then the TV is recorded as
> `role:"surface"` in fleet.json, **excluded from compute planning**, and shown as a magenta **SURF** node in
> the Operator View. The paragraphs below are kept for the reasoning trail, but their storage/1 TB claims are void.

> **UPDATE — Store role VALIDATED with a USB (2026-09-02, same night).** Plugged a USB drive into the TV,
> added `http://tizen.org/privilege/externalstorage` to `tizen-manifest.xml`, rebuilt/re-signed/redeployed.
> The sandboxed app now sees **External 28.9 GB** *and* passed a **write → read → delete** test at
> **`/opt/media/USBDriveA1`** (arbitrary files, not just media; privilege auto-granted, no user prompt). So
> the TV **is** a real model-repo **`STORE`** node **when a drive is attached** — recorded `role:"storage"`
> in fleet.json, shown cyan **STORE** (~28.9 GB) in the Operator View. Bigger drive ⇒ bigger repo, same
> pipeline. (Bare / no USB it reverts to the HEARTH `SURF` surface.) The storage node was only ever gated on
> *a drive being present* — which the original "1 TB" wrongly assumed, but the mechanism is sound and proven.

**What we proved tonight (live, on a retail set):**
- Connected to **`<tv-model>`** (CU7000, 2023) at **`<node-ip>`** via `sdb connect` after enabling
  Developer Mode (TV whitelists the laptop IP `<node-ip>`; sdbd listens on `:26101` after a reboot).
- Pulled **DUID `<tv-duid>`** and capabilities: **Tizen 9.0**, `cpu_arch armv7 / arm_32`, no root, no
  shell, sockets enabled, `netcoredbg` enabled.

**Binding constraint — 32-bit OS on 64-bit silicon.** The Crystal Processor 4K is 64-bit-capable ARM, but
Samsung ships Tizen **compiled 32-bit** on entry TVs (≤2 GB RAM — 64-bit pointers would waste it). So our
userspace is **armv7**: any native engine must be built **32-bit ARM** (never aarch64), and a 32-bit
process on ~2 GB is a poor layer-compute donor.

**Why storage, not compute:** ~2 GB RAM + 32-bit address space ⇒ a donor process holds only a sliver of a
model → the planner would starve it anyway (cf. Tegra). But its **~1 TB free** is real: a Tizen **.NET
background service** can host the **GGUF library** on that disk and serve models to the fabric, killing the
**~19 GB-per-run streaming** seen in Run #7. This role uses the **.NET tooling already installed** and
**skips the Native SDK entirely**. Native `ggml-rpc-server` (armv7) for actual compute is **deferred/optional**.

**Enabler (this session):** wired an AI coding agent into the IDE via the **VS MCP** (CodingWithCalvin/VS-MCPServer on
`http://localhost:5050`, ~56 tools — built `STVNUIApplication4` clean through it) + **Windows-MCP** desktop
automation for the emulator/TV. See Support/ (gitignored) for Tizen/Samsung reference.

**Done since (2026-09-02):** Samsung `genghis-tv` cert created (DUID-bound; author+distributor) → app
built, signed, **installed and running full-screen on the real TV** (a GENGHIS/HEARTH donor-card readout);
real specs measured from inside (see the correction above).

**Resume here:** (a) **HEARTH** — the real thread — build its first vertical on the TV surface (agent
monitor / ambient / elder-care); (b) **only if a USB drive is added** to the TV, test the Store role
(add `externalstorage` privilege → re-measure storage → serve the GGUF library off the drive).

---

## D7 — Operator view is a terminal TUI by default; web dashboard is an optional install choice (2026-09-02)
**Decision:** The coordinator's operator view is a **terminal (text) TUI**, not a web dashboard, by
default. A **web dashboard is deferred as a future enhancement**. When it's built, the **installer lets
the operator choose: TUI, web, or both** — so each coordinator gets what its box and situation warrant.

**Rationale (the maintainer's "resource hog" instinct — correct, and then some):**
1. **Footprint** — the coordinator is a Pi doing control-plane only (no inference). A web stack
   (server + websockets + browser rendering) is heavy for it; a `rich.Live` TUI is one small process
   reading the fleet state we already keep.
2. **Security surface** — a web dashboard means an open port + auth + TLS + XSS/CSRF. A TUI over SSH
   **inherits SSH's auth/encryption** — zero new exposed surface.
3. **Free when idle** — "login = dashboard": the viewer costs nothing when no one is watching; it
   spins up on login, dies on logout. Opposite of an always-on web server.
4. **Sysadmin-native** — htop/nvtop/k9s energy: SSH in, see the fabric, no browser.

**Design (two pieces):**
- **Always-on heartbeat service** (systemd on the Pi) — maintains fleet status + reliability EMA +
  self-healing whether or not anyone's watching. Cheap (a few TCP probes / 5s). Shared data source.
- **On-demand TUI viewer** (`rich.Live`) — reads & renders that state live; where "login launches it"
  lives, via a **configurable coordinator account** (name/pass set at install) whose login shell runs
  the TUI (`ForceCommand` in sshd, or an `exec` in the account's profile). Local console or SSH, same.

**States (the D-note two-state model, in ANSI color):** green = active (with layer share); **amber =
idle-by-policy** (with the marginal reason, e.g. "excluded: +40% bottleneck"); red = down (with
last-seen). Event-log footer from `runs.jsonl`.

**Trade-off:** the TUI has no shareable URL / browser graphs — a non-issue for the sysadmin/sideload
audience, and covered by the optional web view when an operator chooses it at install. The web view,
when added, is just another **skin over the same heartbeat data** — TUI and web are peers, not a rewrite.

---

## D6 — Split only for capacity, not for speed; the coordinator decides *whether* to split (2026-08-31)
**Decision:** The GENGHIS coordinator must decide **whether** to split a model, not just how. Splitting
is a **cost paid for capacity**, never a free speedup — so pool memory only when a model won't fit on
the single best node; otherwise consolidate onto it.

**Discovered empirically (Run #4, the coordinator's first sweep):** a 1.5B model that **fits entirely
on the fastest node** (RTX 5060 Ti, 16 GB) runs *fastest there alone* — **GPU-solo 40 tok/s > any
split (naive 8.1, genghis 14.2)** — because splitting adds a per-token network round-trip across every
donor. GENGHIS-throughput-weighting still beat naive-memory-weighting **1.75×**, but *neither* split
beat not-splitting. The telemetry taught us the rule on run one.

**Planner rule:**
1. If the model (at chosen quant) fits in the **fastest node's** memory → **run it there, no split.**
2. Else → select the **minimum set of donors**, added in *throughput* order, whose **aggregate memory**
   just holds the model; partition *those* by throughput. Every added node must earn its seat — include
   a slow donor (e.g. the Tegra) **only if its memory is actually needed** for capacity.
3. Weight by throughput *and* network/latency cost, not memory (which is the naive default we beat).

**Why it sharpens the thesis:** GENGHIS's value isn't "split everything" — it's **"run models you
otherwise couldn't, as fast as the fleet allows."** Consolidate when you can; pool only when you must.
The "won't-fit" demo is where splitting stops being *better* and becomes *the only way the model runs.*
Targets: coordinator v0.2 (STATUS milestone 9).

---

## D5 — Tizen / Samsung-TV donors: writable but partner-gated → Phase-3 research (2026-08-31)
> **SUPERSEDED by D8 (2026-09-02).** The TV was validated far sooner than "Phase-3" — sideloaded, signed,
> running on a retail set as a **STORE** node (with a USB) + the **HEARTH** surface. D5's preconditions
> ("don't build until the Lenovo GPU + Phase-2 are done") are moot: the Lenovo was dropped and Phase-2
> shipped. Kept below as the original reasoning trail.

**Decision:** Samsung Tizen TVs are a **Phase-3 research track**, NOT a near-term donor. It is
**not "impossible"** (earlier verdict corrected) — but it needs real porting + either a partner
signing gate or a new coordinator transport. Do not start building it until the Lenovo GPU + the
Phase-2 coordinator are done.

**What we verified (tizen.org / developer.samsung.com — the `samsungtizenos.com` mirror is not
official; treat as unverified):**
- Tizen the platform is writable: **.NET, Native C/C++, Web, Flutter** (official SDK).
- Samsung **TVs** run **Web (`.wgt`)** + **.NET (`.tpk`, 2018+ models)** apps — but **NOT native
  C/C++ `.tpk`**. (.NET apps share the `.tpk` extension — that subtlety caused an earlier wrong
  "web-only" claim.)
- **Background execution is supported:** Tizen **service apps** (no-UI, continuous) + **.NET CPU
  wake-lock** `Power.RequestLock(PowerLock.Cpu,0)` (Tizen 5.0+). (Corrects the earlier "suspends
  when backgrounded" claim.)
- A .NET Tizen app **can P/Invoke a signed native `.so` bundled in the app package** — so a
  native `libllama.so` (llama.cpp built for Tizen ARM) is loadable — **but building/signing native
  `.so` requires Samsung Seller Office partner membership** (a dev cert may cover personal dev-mode
  testing; distribution hits the gate). Docs are 2020-era; current 2026 policy may differ.
- NaCl (native C++ in a web app) is **deprecated** (≤2021 TVs; Chromium killed it) → **WebAssembly**
  is the successor. Both run in the **web-app sandbox → no listening socket.**
- The TV **NPU / NNStreamer** stack is open source but the **wrong shape**: fixed-model inference
  pipelines, not a general layer-shard compute backend; and the HAL isn't app-accessible on retail.

**Two candidate donor architectures + their blockers:**
1. **.NET service app + signed `libllama.so` (P/Invoke) + sockets** — most "donor-shaped" (can
   likely bind a listening socket). Gate: **Seller Office signing** + build ggml for Tizen ARM.
   Built with **TizenFX** (`github.com/Samsung/TizenFX`) — the open .NET API bindings: `Tizen.Network`
   + `System.Net.Sockets` for the link, `Tizen.System` for the CPU wake-lock. NOTE: TizenFX is
   app-level (managed wrapper, sandboxed) — it does NOT give HAL/GPU/NPU access; it equips this
   path, it doesn't remove the two gates above.
2. **Web/WASM compute worker that dials OUT to the coordinator** — sidesteps signing, but sandbox
   blocks a listening socket, so it needs a **new GENGHIS "outbound-worker" transport** (not the
   standard llama.cpp RPC listener) + WASM perf hit.

**Common blockers regardless of path:** weak TV hardware (small CPU donor), **no usable GPU/NPU**
for general compute, and the listening-socket restriction. **Next step if pursued:** a hands-on
**dev-mode sideload attempt on the actual TV** — not more doc reading.

---

## D4 — GPU donors run a driver that still supports their arch; Pascal ⇒ Ubuntu 24.04 LTS (2026-08-30)
**Decision:** A GPU donor must run an NVIDIA driver branch that still supports its GPU architecture.
For **Pascal (GTX 10-series, e.g. the 1080 Ti)** that means a **≤570 driver + CUDA 12.x**, which in
practice means **Ubuntu 24.04 LTS** (driver 535/550). Do **not** use Ubuntu 25.10/26.04 for a Pascal
GPU donor. The `donor-setup-cuda.sh` **CUDA preflight** enforces this — it fails loudly instead of
serving on CPU.

**Rationale (learned the hard way, 2026-08-30):** Set up the 1080 Ti on Ubuntu 26.04 ("resolute").
Everything looked fine — `nvidia-smi` showed the card, driver 580.173.02 bound cleanly (clean dmesg),
all libs matched, UVM loaded — but **CUDA would not initialize** (`cudaGetDeviceCount` → error 802
"system not yet initialized"), even for a trivial 5-line program, so `ggml-rpc-server` silently served
the **CPU** backend (advertised 15 GB system RAM instead of 11 GB VRAM; 0.58 tok/s). Root cause:
**NVIDIA driver 580 dropped Maxwell/Pascal/Volta CUDA support**, and Ubuntu 26.04 ships *only* 580
(every older metapackage — 535/550/570 — is a hollow transitional alias to 580; the "550.107.02"
package is an 11 kB metapackage that installs nothing). Kernel was 7.0 — too new for an old `.run`
driver to build against — so there was no in-place path. Conclusion: reimage to 24.04 LTS.

**Signature to recognize it fast:** `nvidia-smi` works (NVML/display path) but CUDA fails 802 (compute
path); empty kernel log on the CUDA attempt; driver 580 on a pre-Turing GPU. The preflight now prints
this guidance automatically.

---

## D3 — License: Apache-2.0 for the code + protect the "GENGHIS" name (2026-08-29)
**Decision:** The project is licensed under the **Apache License 2.0** (see `LICENSE`), with the
**"GENGHIS" / "The GENGHIS Protocol" name reserved as a mark** (see `NOTICE`, and Apache §6 which
grants no trademark rights). Copyright: **© 2026 Michael B. Rinkus**.

**Rationale:** We were free to choose (llama.cpp is MIT and we invoke it over RPC — no copyleft
entanglement). Apache-2.0 gives maximum adoption reach for a protocol that wins by spreading to
many idle donors, while being "tight" where it counts: an explicit **patent grant + patent-troll
retaliation clause** (which MIT/BSD lack) and a clean basis to keep the code permissive while
controlling the **identity** — anyone may fork/use, nobody may call their fork "GENGHIS" or imply
endorsement. AGPL/dual-licensing remains available *later* if closed-SaaS capture becomes a
concern (you can offer a stricter option down the road; you can't un-copyleft once contributors
pile in). Applied at first commit to the private repo `RinkusKhan/The_GENGHIS_Protocol`.
Not legal advice — formal trademark registration is a separate, optional future step.

---

## D2 — Minimum donor network link: 802.11ac (WiFi 5) on 5 GHz (2026-08-29)
**Decision:** WiFi donors must be **802.11ac (WiFi 5) or better, on the 5 GHz band**. WiFi 6/6E
preferred. 2.4 GHz / 802.11n is below-minimum (edge nodes only). Wired Gigabit always welcome.

**Rationale:** Per-token activation transfers are tiny (KB) and latency-bound, so even weak WiFi
runs — validated live: laptop + Pi 5 on WiFi @ ~3 ms carried Runs #1–#2 with the network never
the bottleneck. The band matters at **cold-start weight streaming** (orchestrator pushes GB of
tensors to each donor at load): 5 GHz ac streams a multi-GB shard in tens of seconds; 2.4 GHz n
makes it painful and adds jitter that compounds across pipeline hops. So 5 GHz is the true floor,
not the standard number. Consequence: Pi 3 B (2.4 GHz n only) is disqualified as a WiFi donor —
reinforces its "starve-me / edge" role. Registry gains a `link_type` field; planner weights the
cold-start cost by measured `link_mbps`.

---

## D1 — Tegra X1 stays a CPU donor; do NOT pursue CUDA on it (2026-08-29)
**Decision:** Keep the Tegra X1 as a **CPU-only** RPC donor. Do not attempt a CUDA/GPU build.

**Rationale:**
1. **Unified slow memory negates the GPU win.** LLM token generation is memory-bandwidth-bound.
   The Tegra X1's Maxwell GPU shares the SoC's ~25 GB/s LPDDR4 with the CPU — there is no fast
   dedicated VRAM. A discrete GPU wins on inference because of its own high-bandwidth memory
   (e.g. 1080 Ti ≈ 484 GB/s); the Tegra has none of that, so decode stays bandwidth-capped at
   roughly the same ceiling as the CPU. (Prompt processing would gain somewhat; generation barely.)
   Maxwell also lacks fast FP16, muting even the compute upside.
2. **Toolchain war, worse than the CPU one.** CUDA on the Tegra means JetPack 4.x → CUDA 10.2,
   while modern llama.cpp's CUDA backend wants CUDA 11/12. Likely won't compile at our pinned
   commit; would force an old llama.cpp fork.
3. **FATAL: RPC version incompatibility.** The cluster requires every node on the SAME pinned
   llama.cpp commit (RPC has no cross-version compatibility). A CUDA build forced onto an old
   fork could not talk to the orchestrator at all — a GPU node that can't join the pipeline.

**Strategic note:** The Tegra is most valuable AS the weak node — the one the planner learns to
starve/exclude (demonstrated in Run #2). GPU effort belongs on the **1080 Ti** (discrete 11 GB,
~484 GB/s, modern CUDA that builds clean). If GPU-accelerated edge SoCs are ever wanted, that's
a separate track (old JetPack forks or MLC-LLM) that fragments the fleet — out of scope.

---

## D0 — Coordinator upgraded Pi 4 → Pi 5 8GB/1TB (2026-08-29)
**Decision:** Coordinator is the Raspberry Pi 5 (8GB, 1TB), not the Pi 4.
**Rationale:** Control plane is light, but the 1TB disk unlocks two extra coordinator roles —
model repository (stage GGUFs once, serve over LAN; keep multiple quants) and telemetry ledger
(log every run/plan; the naive-vs-GENGHIS comparison that proves the thesis). Pi 5 CPU/RAM/IO
headroom keeps it from ever bottlenecking. See charter "coordinator's three pillars."

---

## Open considerations (not yet decided)
- **Donor platform horizon (Phase 3+).** The gate for any donor: run a Linux-style userland +
  `ggml-rpc-server` for its CPU arch, and reach the LAN; GPU accel is a bonus where a backend
  exists (CUDA/Metal/Vulkan). Tiers:
  - *Native today:* Linux (SBCs/servers/desktops), Windows, **macOS** (Metal — high-value, add it).
  - *Feasible via a Linux layer:* **Android** phones/tablets (Termux); **Android TV / Google TV /
    Fire TV** incl. **Nvidia Shield** (Tegra) — the smart-TV & streaming-box family, sideloadable;
    **VR headsets (Meta Quest)** — Snapdragon XR2, 6–12 GB RAM, sideload-friendly, *more capable
    than most phones*; **home-automation hubs** (Home Assistant Yellow/Green — Pi-class, on 24/7);
    **Chromebooks** (Crostini); **Steam Deck / SteamOS** (Vulkan APU); **NAS** (Synology/QNAP/
    TrueNAS via Docker — always-on, underrated).
  - *Long tail (technically-possible novelties):* rooted robot vacuums (Valetudo — embedded ARM
    Linux, ~512MB–1GB, sleeps/roams), smart speakers/displays, set-top boxes/DVRs/NVRs, jailbroken
    Kindles. Weak + intermittent → good for the *story*, not for throughput.
  - *Aspirational / locked:* iOS/iPadOS & tvOS (bespoke Metal app only — no arbitrary binaries);
    Tizen/webOS smart TVs (homebrew only); consoles (jailbreak).
  - **Framing — target under-utilized hardware.** The pitch isn't "buy devices," it's "reclaim
    spare capacity you already own and waste": the gaming PC idle 16h/day, the NAS at 5% CPU, old
    phones in a drawer, the TV off 20h/day. Two practical filters decide real value: **enough RAM
    to hold a shard** and **stays put + awake**. Always-on/idle (NAS, HA hub, idle desktop) beats
    wandering/sleeping (vacuum, phone on the move) — and the self-healing coordinator is what makes
    the come-and-go nodes safe to include at all.
  - Cross-cutting caveats: WiFi/mobile nodes = high-latency leaves (single shard, end of pipeline);
    thermal throttling on phones/TV sticks (live telemetry re-measures and down-weights);
    Android backgrounding/Doze needs `termux-wake-lock`. **Always-on devices (NAS, TV boxes,
    mini-PCs) are the most valuable** — they don't roam or sleep. Same pinned-commit rule applies;
    Termux/modern distros ship modern clang so the Tegra's NEON-intrinsic pain won't recur.
    Revisit after the 1080 Ti and Phase 2. Captured in README "What can be a donor?".

---
_Generated from the project's private decision log on 2026-10-03._
