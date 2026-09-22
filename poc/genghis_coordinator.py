#!/usr/bin/env python3
"""
GENGHIS Coordinator — v0.3 (Phase 2, the "brain").

The contribution: instead of splitting a model across donors by *memory* (what
llama.cpp/exo do by default), profile each donor's real *throughput* and split by
that — loading fast nodes, starving slow ones — then prove it beats the naive split.

Loop:  probe -> calibrate -> plan -> run -> log -> compare.

v0.3 refinements (this file):
  (1) precise KV cache from GGUF metadata (n_layers, kv_dim) — replaces a weights*6% guess;
  (2) true leave-one-out marginal analysis that DRIVES selection (auto-prunes drop-safe
      bottleneck nodes, slowest-first) and reports per-node load-bearing / marginal WHY;
  (3) self-tuning throughput: an EMA of REAL single-node runs feeds future plans.

Runs on the orchestrator (the laptop): it has llama-cli, the model, and reaches the
donors over RPC. Reads live donors from fleet.json.

Usage (Windows):
  py genghis_coordinator.py calibrate      # measure solo tok/s per donor -> fleet.json
  py genghis_coordinator.py sweep          # calibrate + run naive vs genghis, log + compare
"""
import argparse, json, os, re, socket, subprocess, sys, time, datetime, urllib.parse, urllib.request, urllib.error
import glob, shutil, platform   # genghis init: local model scan, fleet.json backup, arch detection
import threading                 # background model fetch (D28): never download inside an HTTP request

HERE      = os.path.dirname(os.path.abspath(__file__))
FLEET     = os.path.join(HERE, "fleet.json")
RUNS      = os.path.join(HERE, "runs.jsonl")
PLANS_DIR = os.path.join(HERE, "plans")
# Local llama.cpp builds, in preference order: CUDA (M16/D9 — the laptop's 5090 as a local CUDA0 anchor),
# then Vulkan (D27 — an Intel Arc / AMD box such as the NUC as a local Vulkan0 anchor), then the CPU+RPC
# build. Every build also speaks RPC to the donors. GENGHIS_LLAMA_CLI / GENGHIS_LLAMA_SERVER override
# (host-portable): a Linux-hosted `serve` (the always-on Pi) must point at its own POSIX binaries, not a
# Windows .exe — set them in that host's serve.sh.
_BUILD_DIRS = ("build-cuda", "build-vulkan", "build-rpc")
def _find_build_bin(name):
    """First existing llama.cpp build binary in _BUILD_DIRS order, in either layout: MSVC multi-config
    (<build>/bin/Release/<name>.exe) or Linux/macOS single-config (<build>/bin/<name>). Else the CPU+RPC
    path for this OS, so error messages name a sensible expected location."""
    cands = []
    for b in _BUILD_DIRS:
        if os.name == "nt":
            cands.append(os.path.join(HERE, "llama.cpp", b, "bin", "Release", name + ".exe"))
        else:
            cands.append(os.path.join(HERE, "llama.cpp", b, "bin", name))
            cands.append(os.path.join(os.path.expanduser("~"), "genghis", "llama.cpp", b, "bin", name))   # donor-setup-*.sh workspace
    return next((c for c in cands if os.path.exists(c)), cands[-1])
LLAMA_CLI = os.environ.get("GENGHIS_LLAMA_CLI") or _find_build_bin("llama-cli")
# D23 slice 2 (residency): a WARM llama-server holds the model in VRAM so a switch doesn't re-load 16-40 GB
# from disk each call. Same binary family as llama-cli, in the same build dir. Env override for other hosts.
LLAMA_SERVER = os.environ.get("GENGHIS_LLAMA_SERVER") or _find_build_bin("llama-server")
RESIDENT_HOST = "127.0.0.1"
RESIDENT_PORT = int(os.environ.get("GENGHIS_RESIDENT_PORT", "8081"))   # internal port for the warm server
MODEL     = os.environ.get("GENGHIS_MODEL", r"E:\models\qwen2.5-1.5b-instruct-q4_k_m.gguf")
_MODEL_DEFAULT = MODEL   # the import-time default (env or built-in), before any config override — the fallback for refresh_model()
# Model repository: the coordinator (Pi) is the always-on model library. MODEL may be a full path
# (the laptop keeps its own E:\models) OR a bare name resolved against this local cache, fetched from
# the Pi's /models endpoint if missing. On the Pi itself, HERE/models IS the repository it serves.
MODELS_DIR = os.environ.get("GENGHIS_MODELS_DIR", os.path.join(HERE, "models"))
# Self-identity: which fleet node IS this orchestrator. A `local:true` anchor (e.g. the laptop's own
# 5090, device CUDA0, NO RPC endpoint) is genuinely local ONLY to the host it lives on. From any OTHER
# orchestrator (the always-on Pi) that same node is unreachable — it has no RPC server to dial — so it
# must be excluded from planning, not treated as an up, zero-latency anchor. Match by explicit id
# (GENGHIS_SELF_ID) or hostname vs the node's `host` field.
SELF_ID   = os.environ.get("GENGHIS_SELF_ID", "")
SELF_HOST = (os.environ.get("GENGHIS_SELF_HOST") or socket.gethostname()).lower()


def is_self_node(d):
    """True if fleet node `d` is THIS orchestrator host — so its local:true anchor is really local here."""
    if SELF_ID and d.get("id") == SELF_ID:
        return True
    h = (d.get("host") or "").lower()
    return bool(h) and h == SELF_HOST


def has_rpc(d):
    """True if node `d` publishes an RPC endpoint (ip:port) — i.e. OTHER hosts can dial it.
    D27 dual-role: a `local:true` anchor that ALSO runs ggml-rpc-server (the NUC: its own Arc is the local
    Vulkan0 anchor when it is the client, and the same Arc is `RPCn` for everyone else) is usable from
    every vantage — local from itself (build_devices), over RPC from anyone else. A `local:true` node
    WITHOUT an endpoint (the laptop's 5090) stays private to its own host."""
    return bool(d.get("ip")) and bool(d.get("port"))
# Optional shared PIN for a locked coordinator (protect=all). Harmless when the coordinator is open.
GENGHIS_TOKEN = os.environ.get("GENGHIS_TOKEN", "")
def _auth_headers():
    return {"X-Genghis-Token": GENGHIS_TOKEN} if GENGHIS_TOKEN else {}
PROMPT    = "In two sentences, describe what a coordinator does in a distributed system."
N_PREDICT = 64     # the CLI benchmark budget (run/calibrate) -- NOT the chat default, see V1_MAX_TOKENS
# /v1 chat budget when the client sends no max_tokens (Open WebUI doesn't): a real answer, not a benchmark
# snippet. Found the day the always-on chat first got a "tell me about X" -- it stopped mid-sentence at 64.
V1_MAX_TOKENS = int(os.environ.get("GENGHIS_V1_MAX_TOKENS", "2048"))
TOOL_LOOP_LIMIT = int(os.environ.get("GENGHIS_TOOL_LOOP_LIMIT", "3"))   # the same tool call this many times in a row = a loop
V1_HARD_MAX_TOKENS = int(os.environ.get("GENGHIS_V1_HARD_MAX_TOKENS", "4096"))   # the ceiling even when a client asks for more
N_CTX     = 4096   # bounded context. KV cache scales with this; the DEFAULT (huge) context
                   # caused the 32B OOM in Run #5. Bounding it shrinks the KV footprint.
HB_TIMEOUT   = 2.0 # heartbeat TCP-connect timeout (s)
MONITOR_EVERY = 5  # heartbeat monitor interval (s)
GOAL         = "balanced"  # run-type / objective (D6, coordinator-notes v0.3). Spectrum by capacity margin:
                           #   fastest (x1.02, aggressive speed) -> balanced (x1.08, "Balanced/Fastest" DEFAULT)
                           #   -> fit (x1.20, capacity-safe) -> biggest (pool ALL nodes, max capacity).
GOAL_MARGIN  = {"fastest": 1.02, "balanced": 1.08, "fit": 1.20}
GOALS        = ("fastest", "balanced", "fit", "biggest")  # ordered run-type spectrum; HEARTH TV up/down cycles this
def _build_stamp():
    """The build this node runs, for the fabric's 'is it stale?' glance. In order: the git commit of this
    checkout (short hash · date), else poc/BUILD (written by deploy / git archive), else a fixed fallback.
    A hand-edited constant went ~15 builds without changing and defeated its own purpose."""
    try:
        out = subprocess.run(["git", "-C", HERE, "log", "-1", "--format=%h %cs"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            h, d = out.stdout.split()[:2]
            return f"{d}·{h}"
    except Exception:
        pass
    stamp = ""
    try:
        with open(os.path.join(HERE, "BUILD"), encoding="utf-8") as f:
            v = f.read().strip()
            if v and "$Format" not in v:
                stamp = v
    except OSError:
        pass
    # A host deployed by copying files (no git here) keeps whatever BUILD said when it was written -- the NUC
    # reported 2026-09-14 while running code from the 22nd, which is exactly the question the stamp exists to
    # answer. The code ON DISK is what runs: if this file is newer than the stamp, say so (2026-09-22·deployed).
    try:
        fdate = datetime.date.fromtimestamp(os.path.getmtime(os.path.abspath(__file__))).isoformat()
        if not stamp or fdate > stamp.split("·")[0]:
            return f"{fdate}·deployed"
    except OSError:
        pass
    return stamp or "2026-09-13·unknown"
COORD_VERSION = _build_stamp()  # coordinator build stamp — shown in the fabric + web admin, and reported by nodes
                                 # the coordinator-side analog of the TV client's AppVersion. Bump on each deploy.

GEN_RE = re.compile(r"Generation:\s*([\d.]+)\s*t/s")
PP_RE  = re.compile(r"Prompt:\s*([\d.]+)\s*t/s")


# Single source of truth: the always-on Pi holds the authoritative fleet — it receives every node's
# self-report (/report) and serves the fabric. Planning commands on the laptop FETCH it so that what a
# run USES is exactly what the fabric SHOWS; `serve` (the authority itself) reads the local file.
# The local file is the offline cache / fallback. Vantage-specific facts (latency, up/down) are NOT
# authoritative — each observer overlays its own via heartbeat.
def _default_fleet_url():
    """Coordinator URL precedence: GENGHIS_FLEET_URL (full) > GENGHIS_COORD (host[:port]) > localhost, then
    mDNS. NO personal address is ever compiled in (D22): with neither env set we default to localhost (correct
    when serve runs on this box) and, if that's unreachable, load_fleet falls back to mDNS discovery — so a
    fresh install on any LAN finds ITS OWN coordinator, never someone else's. `genghis init`/`GENGHIS_COORD`
    pin it explicitly."""
    if os.environ.get("GENGHIS_FLEET_URL"):
        return os.environ["GENGHIS_FLEET_URL"]
    c = os.environ.get("GENGHIS_COORD")
    if c:
        return f"http://{c if ':' in c else c + ':8899'}/fleet.json"
    return "http://127.0.0.1:8899/fleet.json"


FLEET_URL = _default_fleet_url()
_EXPLICIT_COORD = bool(os.environ.get("GENGHIS_FLEET_URL") or os.environ.get("GENGHIS_COORD"))

# --- D30: authority vs inference host ---------------------------------------------------------------------
# One fleet has ONE authority (owns fleet.json/config.json, serves the model library, advertises on mDNS, is
# the HEARTH backend). Any other `serve` is an INFERENCE HOST: it runs /v1 + residency + the Control Room
# from its own vantage, but reads the fleet and the human config from the authority, forwards every write
# there, and never owns a rival fleet.json. Mode is decided by where FLEET_URL points: at this box ->
# authority; elsewhere -> host. (`serve --coord HOST` or GENGHIS_COORD selects the authority.)
SERVE_MODE = "authority"          # "authority" | "host"; set by decide_serve_mode() before serve() starts
AUTHORITY_BASE = None             # "http://<authority>:8899" in host mode
_REMOTE_CACHE = {}                # url -> (fetched_at, data): a host asks the authority at most every few seconds
_LOCAL_HOST_KEYS = ("residency", "resident_ctx", "resident_kv", "models_dirs", "auth")   # per-box config; never taken from the authority
# ONE /v1 run at a time per serve process. The planner works off process-globals (GOAL, MODEL) that each request
# sets for itself; two concurrent requests (a chat + Open WebUI's title call) clobbered each other and the title
# call ran the 32B. Until per-request state is threaded through the planner, requests plan-and-run under this lock
# (a queued stream says so in its status). The donors are single-client anyway, so nothing real is lost.
# D45: the planner's per-request state -- which model, which goal, the escalation window, the last decision / refusal,
# the delegate's "why", the proxy's last message -- lives in a THREAD-LOCAL overlay. `serve` is a ThreadingHTTPServer:
# one thread per request, so thread-local IS per-request. Outside a request (the CLI) the module globals are the state.
# Before this the same names were process globals and two concurrent requests clobbered each other (a chat and Open
# WebUI's title call: the title call ran the 32B, 2026-09-14); _V1_RUN_LOCK serialised every /v1 run as a patch.
_REQ = threading.local()

def _req_begin():
    """Called at the top of every HTTP request: a clean overlay for this thread."""
    _REQ.in_request = True; _REQ.model = None; _REQ.goal = None; _REQ.plan_ctx = None
    _REQ.last_dec = None; _REQ.last_refusal = ""; _REQ.last_why = []; _REQ.proxy = {}

def _in_request():
    return bool(getattr(_REQ, "in_request", False))

def _req_get(key, default=None):
    if not _in_request():
        return default
    v = getattr(_REQ, key, default)
    return default if v is None else v

def _req_set(key, value):
    if _in_request():
        setattr(_REQ, key, value)

def _req_snapshot():
    """The overlay, to hand to a worker thread started by this request (with_ticks / ticked_iter)."""
    return dict(vars(_REQ)) if _in_request() else None

def _req_restore(snap):
    if snap:
        for k, v in snap.items():
            setattr(_REQ, k, v)

def active_model():
    """The model THIS request is working on (its overlay), else the process default."""
    return _req_get("model") or MODEL

def active_goal():
    return _req_get("goal") or GOAL

def set_active_goal(goal):
    global GOAL
    if goal not in GOALS:
        return
    if _in_request():
        _REQ.goal = goal
    else:
        GOAL = goal

_V1_RUN_LOCK = threading.Lock()
_V1_RUN_INFO = {}          # what holds the lock right now: {"goal","model","since"} -- so a queued user is told WHAT is ahead


def _my_addresses():
    """Every IPv4 this box answers to (so 'GENGHIS_COORD=<my own ip>' still means: I am the authority)."""
    ips = {"127.0.0.1", "localhost", SELF_HOST}
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return ips


def decide_serve_mode():
    """Set SERVE_MODE / AUTHORITY_BASE from FLEET_URL. Returns the mode."""
    global SERVE_MODE, AUTHORITY_BASE
    host = urllib.parse.urlparse(FLEET_URL).hostname or "127.0.0.1"
    if host.lower() in {a.lower() for a in _my_addresses()}:
        SERVE_MODE, AUTHORITY_BASE = "authority", None
    else:
        SERVE_MODE, AUTHORITY_BASE = "host", FLEET_URL.rsplit("/", 1)[0]
    return SERVE_MODE


def _remote_json(url, ttl=5.0, timeout=4):
    """GET a JSON document from the authority with a small TTL cache. Raises on failure (callers decide
    whether a stale copy is acceptable)."""
    now = time.time()
    hit = _REMOTE_CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with urllib.request.urlopen(urllib.request.Request(url, headers=_auth_headers()), timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    _REMOTE_CACHE[url] = (now, data)
    return data
_DISCOVERY_TRIED = False


def discover_coordinator(timeout=2.0, announce=True):
    """Find the coordinator on the LAN via mDNS; update FLEET_URL if found. Returns the URL or None."""
    global FLEET_URL, _DISCOVERY_TRIED
    _DISCOVERY_TRIED = True
    try:
        import genghis_mdns
        found = genghis_mdns.discover(timeout=timeout)
    except Exception:
        found = None
    if found:
        FLEET_URL = found
        if announce:
            print(f"  (discovered coordinator via mDNS: {found})", file=sys.stderr)
    return found


def load_fleet(remote=False):
    if SERVE_MODE == "host":
        # D30: an inference host never owns a fleet. Ask the authority (TTL-cached so a busy Control Room and
        # /v1 don't hammer it); the local file is only the offline copy, refreshed on every successful fetch.
        try:
            data = _remote_json(FLEET_URL, ttl=5.0)
            try:
                save_fleet(data)
            except Exception:
                pass
            return data
        except Exception as e:
            print(f"  (authority {FLEET_URL} unreachable — using the local copy: {e})", file=sys.stderr)
            return _load_json_resilient(FLEET, what="fleet.json")
    if remote:
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(urllib.request.Request(FLEET_URL, headers=_auth_headers()), timeout=4) as r:
                    data = json.loads(r.read().decode("utf-8"))
                try:                                # cache locally so the laptop still works if the Pi is down
                    save_fleet(data)
                except Exception:
                    pass
                return data
            except Exception as e:
                # First failure with no explicit config: try to DISCOVER the coordinator, then retry once.
                if attempt == 1 and not _EXPLICIT_COORD and not _DISCOVERY_TRIED:
                    if discover_coordinator():
                        continue
                print(f"  (fleet authority {FLEET_URL} unreachable — using local cache: {e})", file=sys.stderr)
                break
    return _load_json_resilient(FLEET, what="fleet.json")


def _load_json_resilient(path, what="state file"):
    """Read a JSON state file; if it is torn/unparseable, recover from <path>.bak, say so loudly, and re-save
    the good copy over the bad one -- a serve must never crash-loop on a file it can repair itself."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        bak = path + ".bak"
        try:
            with open(bak, encoding="utf-8") as f:
                good = json.load(f)
        except Exception:
            raise SystemExit(f"[state] {what} at {path} is unreadable ({e}) and no usable {os.path.basename(bak)} exists. "
                             f"Restore it (a copy from the authority: GET /fleet.json) and restart.")
        try:
            shutil.copyfile(path, path + f".corrupt-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}")
        except OSError:
            pass
        print(f"[state] {what} was torn ({e}); recovered from {os.path.basename(bak)} and re-saved (the bad copy is kept as .corrupt-*)", flush=True)
        atomic_json_write(path, good, keep_backup=False)
        return good


def atomic_json_write(path, obj, keep_backup=True):
    """The ONLY way a JSON state file gets written. Temp file in the same dir + os.replace() (atomic on
    Windows and POSIX), fsync'd; the previous good file is kept as <path>.bak so a reader that finds a torn
    file can recover instead of dying. On Windows os.replace() can fail transiently while another process
    holds the target open for reading -- retry briefly rather than leaving the temp file behind.
    (Two writers each using open(path, "w") on their own handle interleave into a file that is valid JSON
    plus a stray byte -- "Extra data: line 241" -- which crash-looped the laptop's serve on 2026-09-13.)"""
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush(); os.fsync(f.fileno())
    if keep_backup and os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    last = None
    for attempt in range(20):
        try:
            os.replace(tmp, path); return
        except PermissionError as e:
            last = e; time.sleep(0.05 * (attempt + 1))
    try: os.remove(tmp)
    except OSError: pass
    raise last


def save_fleet(fleet, path=None):
    """ATOMIC write of fleet.json (see atomic_json_write)."""
    atomic_json_write(path or FLEET, fleet)


def reclaim_card(node_id, fleet):
    """D44: unload every pooled warm model that some OTHER host keeps a shard of on `node_id` (its owner turned
    lending off). The node's own warm models are its owner's business and stay. Returns one note per model."""
    notes = []
    for h in fleet.get("donors", []):
        if h.get("id") == node_id or not h.get("local"):
            continue                                              # only hosts (they run the pooled servers)
        pooled = {m: nodes for m, nodes in (h.get("pooled") or {}).items() if node_id in (nodes or [])}
        if is_self_node(h):                                       # this serve holds it: unload here, directly
            pooled.update({os.path.basename(e["model"]): list(e.get("nodes") or []) for e in _pool_alive().values()
                           if node_id in (e.get("shards") or {})})
        for m, nodes in pooled.items():
            why = f"reclaimed: the owner of {node_id} turned lending off (it was across {' + '.join(nodes)})"
            try:
                if is_self_node(h):
                    ok, _ = unload_model(m, why=why)
                else:
                    body = json.dumps({"action": "unload", "model": m, "why": why}).encode("utf-8")
                    req = urllib.request.Request(f"http://{h['ip']}:{int(h.get('serve_port') or 8899)}/residency", data=body,
                                                 headers={"Content-Type": "application/json", **_auth_headers()})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        ok = bool(json.loads(r.read().decode("utf-8")).get("ok"))
            except Exception as e:
                ok = False; print(f"[lend-off] could not reclaim {m} from {h.get('id')}: {e}", flush=True)
            notes.append(f"unloaded {_tiny_model(m)} on {h.get('id')} (it was across {' + '.join(nodes)})" if ok
                         else f"could NOT unload {_tiny_model(m)} on {h.get('id')} -- unload it there by hand")
            print(f"[lend-off] {notes[-1]}", flush=True)
    return notes

def fleet_ops(action, node_id):
    """D26 node lifecycle — mutate the AUTHORITY fleet.json. Nodes are cattle, not pets:
      retire  -> move a node out of `donors`/`_eye_nodes` into `_retired_donors` (reversible graveyard)
      restore -> move it back from `_retired_donors` to `donors`
      remove  -> hard-delete it everywhere, incl. its config `names`/`roles`
    A broken node needs NONE of this (heartbeat + heal-by-membership route around it automatically); this is
    for permanent add/remove/replace. Runs on the serve host so `load_fleet()` is the authority copy."""
    f = load_fleet()
    donors = f.setdefault("donors", []); retired = f.setdefault("_retired_donors", []); eyes = f.setdefault("_eye_nodes", [])
    def _pop(lst):
        for i, d in enumerate(lst):
            if d.get("id") == node_id:
                return lst.pop(i)
        return None
    if action == "register":
        # D33: a node announces ITSELF to the authority (installer / `init --coord` / `register`). Upsert by
        # id, else by host. Keeps what the fleet has learned about it (throughput EMA, reliability, names);
        # revives it from the graveyard if it had been retired; clears a matching _pending_donors stub.
        node = node_id if isinstance(node_id, dict) else None
        if not node or not node.get("id"):
            return False, "register needs a node dict with an id"
        nid = node["id"]; host = (node.get("host") or "").lower()
        keep = ("tps_ema", "tokens_per_s_solo", "reliability", "last_seen", "notes", "added")
        existing = None
        for i, x in enumerate(donors):
            if x.get("id") == nid or (host and (x.get("host") or "").lower() == host):
                existing = donors.pop(i); break
        if existing is None:
            for i, x in enumerate(retired):
                if x.get("id") == nid or (host and (x.get("host") or "").lower() == host):
                    existing = retired.pop(i); existing.pop("retired_at", None); break
        merged = dict(existing or {})
        merged.update({k: v for k, v in node.items() if v is not None})
        # Presence-keyed fields: the node's CURRENT announcement is the truth. A box that registers without an
        # RPC endpoint (away, or a pure client) is not a donor -- an old `port` must not survive the merge; and
        # `away` clears the moment it registers from the LAN again (D35).
        for k in ("port", "away"):
            if node.get(k) is None:
                merged.pop(k, None)
        for k in keep:
            if existing and existing.get(k) is not None and node.get(k) is None:
                merged[k] = existing[k]
        merged.setdefault("added", datetime.date.today().isoformat())
        merged["status"] = "up"; merged["last_seen"] = datetime.datetime.now().isoformat(timespec="seconds")
        if existing and existing.get("id") and existing["id"] != nid:
            merged["id"] = existing["id"]            # the fleet's name for this host wins over a fresh hostname id
        donors.append(merged)
        pend = f.setdefault("_pending_donors", [])
        pend[:] = [x for x in pend if x.get("id") != merged["id"]]
        note = f"registered {merged['id']} ({merged.get('host')} {merged.get('ip')}:{merged.get('port')})" + (" — updated" if existing else " — NEW")
        save_fleet(f)
        return True, note
    if action == "retire":
        d = _pop(donors) or _pop(eyes)
        if not d: return False, f"no active node '{node_id}'"
        d["retired_at"] = datetime.date.today().isoformat(); retired.append(d); note = f"retired {node_id}"
    elif action == "restore":
        d = None
        for i, x in enumerate(retired):
            if x.get("id") == node_id: d = retired.pop(i); break
        if not d: return False, f"'{node_id}' is not retired"
        d.pop("retired_at", None); donors.append(d); note = f"restored {node_id}"
    elif action == "remove":
        hit = _pop(donors) or _pop(eyes)
        before = len(retired)
        retired[:] = [x for x in retired if x.get("id") != node_id]   # also purge from the graveyard
        if not hit and len(retired) == before:
            return False, f"no node '{node_id}'"
        note = f"removed {node_id}"
    elif action in ("lend-off", "lend-on"):
        # D35: "lend my GPU" -- off = the owner keeps this box's GPU (Blender/Resolve day): the planner and
        # delegation leave it alone; it stays a full host for its own work. On = back in the pool.
        # D44: OFF also RECLAIMS the card -- every pooled model another host keeps a shard of here is unloaded, and the
        # note says which and where to put it instead. The switch means what it says the moment it is clicked; a
        # conversation mid-answer on that shard is not guaranteed (Michael: "I wouldn't do that willy-nilly").
        d = next((x for x in donors if x.get("id") == node_id), None)
        if not d: return False, f"no active node '{node_id}'"
        if action == "lend-off": d["lend"] = False
        else: d.pop("lend", None)
        note = f"{node_id}: lending {'OFF -- the owner keeps this GPU' if action == 'lend-off' else 'on'}"
        save_fleet(f)                                     # the switch is on disk BEFORE the reclaim: the hosts' unload reports
        if action == "lend-off":                          # rewrite the fleet (bookings cleared) and must not be overwritten
            got = reclaim_card(node_id, f)                #   by a stale copy saved afterwards (seen 2026-09-19)
            if got:
                note += ". Reclaimed: " + "; ".join(got) + f" -- place them again from the Pool without {node_id} if you want them back"
        return True, note
    else:
        return False, "action must be retire | remove | restore | lend-off | lend-on"
    save_fleet(f)
    if action == "remove":                                  # tidy human config so a re-added id starts clean
        c = load_config(); changed = False
        for k in ("names", "roles"):
            if isinstance(c.get(k), dict) and node_id in c[k]:
                c[k].pop(node_id, None); changed = True
        if changed: _save_config(c)
    return True, note


def push_report(nid, fields):
    """Push node-intrinsic values (throughput, capacity) to the always-on authority (Pi /report) so
    measurements/learning persist to the single source of truth. Best-effort; silent on failure."""
    if not nid:
        return
    try:
        import urllib.request
        url = FLEET_URL.rsplit("/", 1)[0] + "/report"
        body = json.dumps({"id": nid, **fields}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **_auth_headers()})
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass


def live_donors(fleet):
    """COMPUTE donors currently serving (status 'up'), in a stable order.
    Excludes non-compute roles (e.g. role='storage' — the TV model-repo node, D8): those are real
    fabric members but can't take model layers, so they must never enter compute planning/calibration."""
    return [d for d in fleet.get("donors", [])
            if d.get("status") == "up" and d.get("role", "compute") in (None, "compute")
            and (not d.get("local") or is_self_node(d) or has_rpc(d))    # a foreign local anchor is usable only if it publishes an RPC endpoint (D27)
            and (d.get("lend", True) or is_self_node(d))]                # lend off (D35): only its owner may use it


# ---------------------------------------------------------------------------
# Heartbeat + self-healing (see coordinator-notes "Dashboard" + STATUS milestone 11)
# ---------------------------------------------------------------------------
def probe(ip, port, timeout=HB_TIMEOUT):
    """Liveness check: can we TCP-connect to the donor's rpc-server? Returns (up, latency_ms)."""
    t0 = time.time()
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True, round((time.time() - t0) * 1000, 1)
    except OSError:
        return False, None


def heartbeat(fleet, persist=True):
    """Probe every donor; update status/last_seen/latency/reliability. Return list of transitions."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    transitions = []
    for d in fleet.get("donors", []):
        if d.get("local") and (is_self_node(d) or not has_rpc(d)):
            # A local anchor (e.g. the laptop's 5090) has no RPC endpoint to probe, so no foreign observer can
            # judge its liveness — mark it UP as a fabric member and let the PLANNER decide usability via
            # is_self_node (below). This stops the Pi from falsely writing another host's anchor DOWN into the
            # shared fleet (the vantage-vs-shared-state seam). Its own host validates real placement.
            # (From its OWN host any local anchor is up at latency 0 — no point dialing our own rpc-server.)
            up, lat = True, 0.0
        elif _shard_held_mb(d) > 0:
            # D39: ggml-rpc-server serves ONE client at a time. A donor holding a shard of some host's pooled
            # resident cannot answer a probe -- that is "busy for host X", not "down" -- so don't dial it at all:
            # each dial was a full 2 s timeout, and two pinned donors made /fabric.json take 4 s on every Control
            # Room refresh and the watchdog call the authority DOWN (2026-09-18). The shard booking is refreshed
            # by the host that holds the pooled model (every beat, 15-min freshness): if that host or this donor
            # dies, the booking goes stale and the probe resumes. Keep it up, don't bleed its reliability.
            up, lat = True, None
        else:
            # remote donors — and a D27 dual-role anchor seen from another host — are probed honestly
            up, lat = probe(d["ip"], d["port"])
        pinned = up and lat is None and _shard_held_mb(d) > 0
        d["pinned"] = pinned
        new = "up" if up else "down"
        prev = d.get("status")
        if prev and prev != new:
            transitions.append((d["id"], prev, new))
        d["status"] = new
        if up:
            d["last_seen"] = now
            if lat is not None:
                d["latency_ms_to_orchestrator"] = lat
        # rolling reliability (EMA): 1.0 = always up. Also feeds the v0.3 donor score.
        d["reliability"] = round(0.8 * d.get("reliability", 1.0) + 0.2 * (1.0 if up else 0.0), 3)
    # A host that is DOWN runs no pooled server: the shards it booked on other cards are gone with it. Drop those
    # bookings now -- otherwise the donors read PINNED (and the planner avoids them) for up to 15 min of freshness
    # (the "pinned laptop" family, 2026-09-19). A host that merely restarted keeps its bookings via D38.1 adoption.
    down_hosts = {d.get("id") for d in fleet.get("donors", []) if d.get("local") and d.get("status") == "down"}
    if down_hosts:
        for d in fleet.get("donors", []):
            held = d.get("shard_held") if isinstance(d.get("shard_held"), dict) else None
            if held and any(h in down_hosts for h in held):
                for h in list(held):
                    if h in down_hosts:
                        held.pop(h); print(f"[heartbeat] dropped {h}'s booking on {d.get('id')} -- that host is down", flush=True)
                if held: d["shard_held"] = held
                else: d.pop("shard_held", None)
    if persist:
        save_fleet(fleet)
    return transitions


def monitor(fleet):
    """Live heartbeat console — the self-healing watch (and the dashboard's data feed)."""
    print(f"== GENGHIS heartbeat monitor — every {MONITOR_EVERY}s (Ctrl-C to stop) ==")
    heartbeat(fleet)  # prime statuses
    try:
        while True:
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            for did, prev, new in heartbeat(fleet):
                if new == "down":
                    print(f"  [{stamp}] [DOWN] {did} -- no communication (self-heal: excluded from next plan)")
                else:
                    print(f"  [{stamp}] [REJOIN] {did} -- folding back into the fabric")
            up   = [d["id"] for d in fleet["donors"] if d.get("status") == "up"]
            down = [d["id"] for d in fleet["donors"] if d.get("status") == "down"]
            line = f"  [{stamp}] up: {', '.join(up) or '(none)'}"
            if down:
                line += f"   | DOWN: {', '.join(down)}"
            print(line)
            time.sleep(MONITOR_EVERY)
    except KeyboardInterrupt:
        print("\n  monitor stopped.")


def mem_weight(d):
    """Memory capacity for the NAIVE plan: VRAM for GPUs, RAM for CPUs."""
    return d.get("vram_total_mb") or d.get("ram_total_mb") or d.get("ram_free_mb") or 1


# ---------------------------------------------------------------------------
# v0.2 — the "whether to split" brain (see DECISIONS D6 + coordinator-notes v0.2)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# v0.3 (1/3) — precise KV cache from GGUF metadata (was a weights*0.06 heuristic)
# ---------------------------------------------------------------------------
# GGUF value-type enum -> (struct fmt, byte size) for the SCALAR types. 8=STRING, 9=ARRAY handled separately.
_GGUF_SCALAR = {0:("B",1), 1:("b",1), 2:("H",2), 3:("h",2), 4:("I",4), 5:("i",4),
                6:("f",4), 7:("?",1), 10:("Q",8), 11:("q",8), 12:("d",8)}
_GGUF_CACHE = {}   # path -> (n_layers, kv_dim) | None  (parse once per model)


def _gguf_scalar_meta(path, prefix_mb=32):
    """Parse a GGUF header's metadata and return {key: value} for all SCALAR (non-array) keys.
    Reads only a prefix of the file — metadata lives at the top; the big tokenizer ARRAYs are
    walked (skipped) in-memory. Returns {} on any parse failure (caller falls back to heuristic)."""
    import struct
    with open(path, "rb") as f:
        buf = f.read(prefix_mb * 1024 * 1024)
    if buf[:4] != b"GGUF":
        raise ValueError("not a GGUF file")
    off = 4
    (ver,) = struct.unpack_from("<I", buf, off); off += 4
    if ver < 2:                                   # v1 used uint32 counts; we only support v2/v3
        raise ValueError(f"unsupported GGUF version {ver}")
    off += 8                                       # n_tensors (uint64) — skip
    (n_kv,) = struct.unpack_from("<Q", buf, off); off += 8

    def rd_str():
        nonlocal off
        (ln,) = struct.unpack_from("<Q", buf, off); off += 8
        s = buf[off:off + ln].decode("utf-8", "replace"); off += ln
        return s

    def skip(vtype):                               # advance past one value (recurses for arrays)
        nonlocal off
        if vtype == 8:                             # STRING
            (ln,) = struct.unpack_from("<Q", buf, off); off += 8 + ln
        elif vtype == 9:                           # ARRAY: elem-type, count, then elems
            (et,) = struct.unpack_from("<I", buf, off); off += 4
            (aln,) = struct.unpack_from("<Q", buf, off); off += 8
            if et in _GGUF_SCALAR:                  # fixed-width elems -> seek past in one jump
                off += aln * _GGUF_SCALAR[et][1]
            else:
                for _ in range(aln):
                    skip(et)
        else:
            off += _GGUF_SCALAR[vtype][1]

    meta = {}
    for _ in range(n_kv):
        key = rd_str()
        (vtype,) = struct.unpack_from("<I", buf, off); off += 4
        if vtype in _GGUF_SCALAR:
            fmt, sz = _GGUF_SCALAR[vtype]
            (meta[key],) = struct.unpack_from("<" + fmt, buf, off); off += sz
        elif vtype == 8:
            meta[key] = rd_str()
        else:
            skip(vtype)                            # arrays (tokenizer vocab, etc.) — not needed
    return meta


def gguf_arch_params(path=None):
    """(n_layers, kv_dim) from GGUF metadata for precise KV sizing, or None if unavailable. Cached."""
    path = path or model_path()
    if path in _GGUF_CACHE:
        return _GGUF_CACHE[path]
    res = None
    try:
        m = _gguf_scalar_meta(path)
        arch = m["general.architecture"]           # e.g. "qwen2", "llama"
        n_layers = m[f"{arch}.block_count"]
        n_embd   = m[f"{arch}.embedding_length"]
        n_head   = m[f"{arch}.attention.head_count"]
        n_kv     = m.get(f"{arch}.attention.head_count_kv", n_head)   # == n_head for non-GQA (MHA)
        kv_dim   = n_kv * (n_embd // n_head)        # kv_dim = n_kv_heads * head_dim
        res = (n_layers, kv_dim)
    except Exception:
        res = None
    _GGUF_CACHE[path] = res
    return res


_KV_BYTES = {"f16": 2.0, "bf16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}   # bytes per KV element by cache type

def resident_kv_type():
    """Per-box config `resident_kv`: the warm server's KV-cache type (f16 default; q8_0 halves the cache at
    negligible quality cost -- the lever that turns an 8k context into 16k on a full card). Validated."""
    t = (load_config().get("resident_kv") or "f16").strip().lower()
    return t if t in _KV_BYTES else "f16"

def kv_cache_mb(n_ctx=N_CTX, path=None, kv_type=None):
    """KV cache footprint in MB. Precise from GGUF (2 [K+V] * n_layers * n_ctx * kv_dim * bytes-per-element for
    the cache type -- f16 by default, q8_0 when the resident is configured so); falls back to the old weights*0.06
    heuristic if the GGUF can't be parsed."""
    ap = gguf_arch_params(path)
    if ap:
        n_layers, kv_dim = ap
        b = _KV_BYTES.get(kv_type or resident_kv_type(), 2.0)
        return 2 * n_layers * n_ctx * kv_dim * b / (1024 * 1024)
    try:
        return (os.path.getsize(path or model_path()) / (1024*1024)) * 0.06 * (n_ctx / 4096.0)
    except OSError:
        return 0.0


def gguf_ctx_train(path=None):
    """The model's trained context length (<arch>.context_length) or None."""
    try:
        m = _gguf_scalar_meta(path or model_path())
        return int(m[f"{m['general.architecture']}.context_length"])
    except Exception:
        return None


_CTX_LADDER = (4096, 8192, 16384, 32768, 65536, 131072)

def resident_ctx(path, free_mb, need=0):
    """D32 — the warm server's context size is a DECISION, not a constant. Pick the largest step on the
    ladder that (a) the model was trained for, (b) fits in this GPU's free memory with weights + KV +
    compute floor, and (c) is at least `need` tokens (a conversation that just outgrew the current one).
    Config `resident_ctx` pins it. Floor is N_CTX (4096). Returns (ctx, max_fit)."""
    pinned = (load_config().get("resident_ctx") or 0)
    try:
        weights = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        weights = 0
    train = gguf_ctx_train(path) or 32768
    fit = N_CTX
    for c in _CTX_LADDER:
        if c > train:
            break
        if weights + kv_cache_mb(c, path) + 512 <= free_mb:
            fit = c
    if pinned:
        return int(pinned), fit
    want = N_CTX
    for c in _CTX_LADDER:
        if c >= need * 1.25 and c <= fit:
            want = c; break
    else:
        want = fit if need > N_CTX else N_CTX
    # comfortable default even for a short chat: one step above the floor when it fits (8k), so an
    # ordinary conversation never trips the limit; grow further on demand
    if need == 0 and fit >= 8192:
        want = min(16384, fit)
    return want, fit


def _local_anchor_free_mb():
    """Free memory of THIS host's own anchor (for sizing the warm server's context)."""
    try:
        with open(FLEET, encoding="utf-8") as f:
            for d in json.load(f).get("donors", []):
                if d.get("local") and is_self_node(d):
                    return free_mem_mb(d)
    except Exception:
        pass
    return 0


def model_mem_mb():
    """Footprint = weights (file) + KV cache (precise, scales with N_CTX) + compute-buffer floor.
    Run #5 lesson: file size ALONE under-counts — a big model's KV at a large context is GB-scale
    and OOM'd a plan that 'fit' on paper. v0.3: KV is now read precisely from GGUF metadata
    (n_layers, kv_dim) instead of a weights*6% guess — the guess under-counted the 32B by ~2x,
    which is exactly what caused the Run #5 OOM."""
    try:
        weights = os.path.getsize(model_path()) / (1024 * 1024)
    except OSError:
        return 0.0
    return weights + kv_cache_mb(_req_get("plan_ctx") or N_CTX) + 512   # +512 MB compute-buffer floor; plan_ctx = an escalation's window (D43)


# ---- model repository (the Pi is the model library; clients fetch-if-missing) ----
def refresh_model():
    """Re-read config.json "model" and rebind the global MODEL LIVE, so changing the model (by editing
    config.json or via the web admin's POST /config) takes effect on the next request — no serve restart.
    An explicit GENGHIS_MODEL env still wins (host pin). Empty/unset config -> the import-time default.
    Returns True if MODEL changed (lets a caller log the switch)."""
    global MODEL
    if "GENGHIS_MODEL" in os.environ:
        return False
    m = (load_config().get("model") or "").strip() or _MODEL_DEFAULT
    if m != MODEL:
        MODEL = m
        return True
    return False


# --- D23: model registry ---------------------------------------------------------------------------------
# The registry turns "GGUFs sitting in a folder" into named, selectable models with metadata, and lets each
# effort-GOAL map to its OWN model. /v1 advertises the goals AND the models; a request may name either.
_MMPROJ_HINT = ("mmproj", "clip", "projector", "vision")   # a companion file, not a chat model itself
_VLM_HINT    = ("vl", "vlm", "llava", "smolvlm", "-vision", "qwen2-vl", "minicpm-v")


# --- ROLES step 1: capability metadata (2026-09-22) ------------------------------------------------------
# A role REQUIRES capabilities ("this one has to drive the PubMed tools"), so the registry has to know more
# about a GGUF than its size and a guess from its filename. Nearly all of it is already inside the file:
# the chat template says whether the model can be OFFERED tools at all, and -- the one that bites silently --
# whether a tool RESULT survives the round trip; <arch>.context_length says how much room a research role
# really has; a projector file says which model it pairs with. Read the GGUF first, fall back to the name
# only where the file is silent. Names lie, and ours did: on this very library the name heuristic called
# Qwen3.8-27B (which HAS a projector) text-only, and handed that projector to SmolVLM-500M, whose embedding
# width doesn't match it -- a pairing that could only ever have failed at load.
_CAPS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "caps.json")
_CAPS_CACHE  = None      # "abspath|size|mtime" -> caps dict; keyed so a re-quantized file re-reads itself
_CAPS_LOCK   = threading.Lock()

# Jinja chat-template probes. A template is the model's own statement of what it can be handed.
_RE_TOOL_ROLE = re.compile(r"""role\s*==\s*['"]tool['"]""")          # does a role:"tool" message render?
_RE_TOOL_CALL = re.compile(r"tool_call", re.I)                        # can it emit one?
_RE_TOOLS_VAR = re.compile(r"(^|[^\w.])tools([^\w]|$)")               # is a tools list rendered at all?
_RE_THINK     = re.compile(r"<think|</think|reasoning_content", re.I)


def _caps_key(path):
    st = os.stat(path)
    return f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"


def _caps_cache():
    """Load the on-disk caps cache once. Parsing a GGUF header means reading a 32 MB prefix per model;
    /registry.json is polled by every Control Room, so without this a serve restart would re-read a few
    hundred MB to answer the first refresh. Torn/missing file just means a cold read."""
    global _CAPS_CACHE
    if _CAPS_CACHE is None:
        try:
            _CAPS_CACHE = _load_json_resilient(_CAPS_CACHE_FILE, what="caps.json") or {}
        except Exception:
            _CAPS_CACHE = {}                        # no cache yet (first run) is the normal case, not an error
        if not isinstance(_CAPS_CACHE, dict):
            _CAPS_CACHE = {}
    return _CAPS_CACHE


def _read_caps(path):
    """Parse ONE GGUF into a capability dict. Every field is evidence from the file, never a vibe:

      arch/name/params  general.* -- what the publisher called it
      ctx_train         <arch>.context_length -- the ceiling a long-context role plans against
      tools             'native'  = the template renders a tools list AND can emit a tool_call
                        'partial' = it can emit a call but never sees the tool list
                        'none'    = the template has no tool machinery at all
      tool_results      does the template render a role:"tool" message? A False here is the nasty one:
                        the call goes out, the answer comes back, and the template DROPS it -- the model
                        never sees its own tool's reply and confabulates. Silent by construction.
      reasoning         emits <think>/reasoning content
      projector         (mmproj only) the vision width it projects into, for honest pairing
    """
    # `engine` is here from day one so a role can bind to something that is NOT a GGUF. The voice layer is
    # the known case: PersonaPlex is a speech-to-speech SERVICE that owns a whole card (~20 GB of 24) and is
    # up or down rather than placed by the planner -- a Voice body, not a donor (D36 VOICE_PLAN). A role
    # will name a `gguf` engine for its mind and may name a `service` engine for its ears and mouth.
    c = {"engine": "gguf",
         "arch": None, "name": None, "params": None, "ctx_train": None,
         "tools": "unknown", "tool_results": None, "reasoning": False,
         "chatml": False, "is_mmproj": False, "projection_dim": None,
         "basename": None, "embedding_length": None, "template_chars": 0}
    try:
        m = _gguf_scalar_meta(path)
    except Exception as e:
        c["error"] = str(e)[:120]
        return c
    arch = m.get("general.architecture")
    c["arch"]     = arch
    c["name"]     = m.get("general.name")
    c["params"]   = m.get("general.size_label")
    c["basename"] = (m.get("general.basename") or "").strip().lower() or None
    if m.get("general.type") == "mmproj" or m.get("clip.has_vision_encoder") or arch == "clip":
        c["is_mmproj"] = True
        c["projection_dim"] = m.get("clip.vision.projection_dim")
        return c
    if arch:
        c["ctx_train"]        = m.get(f"{arch}.context_length")
        c["embedding_length"] = m.get(f"{arch}.embedding_length")
    # Is this a CHAT model at all? A .gguf in a models folder may be Stable Diffusion, Flux, a video or a
    # TTS model -- same container, nothing llama-server can chat with. They carry no chat template and no
    # <arch>.context_length, while every instruct model has both. Answering "why does this one just sit
    # there?" with a reason is the D20 registry-metadata promise; Michael's library has a dozen of them.
    t = m.get("tokenizer.chat_template")
    c["chat"] = bool(isinstance(t, str) and c["ctx_train"])
    if not c["chat"]:
        c["engine"] = "not-chat"
        c["why_not_chat"] = (f"a '{arch}' model — GENGHIS runs text chat models; this one has "
                             f"{'no chat template' if not isinstance(t, str) else 'no context length'}")
        c["tools"] = "none"; c["tool_results"] = False
        return c
    c["template_chars"] = len(t)
    c["chatml"]         = "<|im_start|>" in t
    c["reasoning"]      = bool(_RE_THINK.search(t))
    can_call  = bool(_RE_TOOL_CALL.search(t))
    sees_list = bool(_RE_TOOLS_VAR.search(t))
    c["tools"] = "native" if (can_call and sees_list) else ("partial" if can_call else "none")
    c["tool_results"] = bool(_RE_TOOL_ROLE.search(t))
    return c


def model_caps(path):
    """Cached _read_caps. Safe to call per request."""
    try:
        key = _caps_key(path)
    except OSError:
        return {}
    cache = _caps_cache()
    hit = cache.get(key)
    if hit is not None:
        return hit
    caps = _read_caps(path)
    with _CAPS_LOCK:
        cache[key] = caps
        try:
            atomic_json_write(_CAPS_CACHE_FILE, cache, keep_backup=False)
        except Exception:
            pass                                    # a cache we can't persist is still a cache in memory
    return caps


def caps_summary(caps):
    """One plain line for a human: what this model can actually be asked to do. The UI and the CLI say the
    same sentence, and a role's refusal quotes it (D31 -- never silent)."""
    if not caps or caps.get("error"):
        return "couldn't read this file's metadata"
    if caps.get("chat") is False:
        return caps.get("why_not_chat") or "not a chat model"
    bits = []
    ctx = caps.get("ctx_train")
    if ctx:
        bits.append(f"{ctx // 1024}k context")
    tools = caps.get("tools")
    if tools == "native" and caps.get("tool_results"):
        bits.append("drives tools")
    elif tools == "native":
        bits.append("calls tools but DROPS their results")
    elif tools == "partial":
        bits.append("emits tool calls but is never shown the tool list")
    elif tools == "none":
        bits.append("no tool support")
    if caps.get("vision"):
        bits.append("vision")
    if caps.get("reasoning"):
        bits.append("reasoning")
    return " · ".join(bits) if bits else "text only"

def _model_dirs():
    """Directories scanned for local GGUFs: MODELS_DIR, the configured model's own dir, and any
    config 'models_dirs' — deduped, existing-only. This is why a model in a second models folder is found even though
    MODELS_DIR is poc/models."""
    dirs = [MODELS_DIR]
    try:                                                   # the LOCAL file only: this scan runs inside /registry.json,
        local_cfg = _load_json_resilient(CONFIG, what="config.json") or {}   # which another serve may be waiting on
    except Exception:
        local_cfg = {}
    cfg_model = (local_cfg.get("model") or "").strip()
    if cfg_model:
        dirs.append(os.path.dirname(cfg_model))
    # The import-time default (GENGHIS_MODEL env / built-in) is a STABLE anchor for the scan. The live MODEL
    # global is per-request (set_active_model) -- keying the scan on it made E:\models vanish the moment a
    # request pointed MODEL at poc/models/..., and every later `balanced` silently degraded to the 1.5B.
    for m in (_MODEL_DEFAULT, MODEL):
        if m and os.path.dirname(m):
            dirs.append(os.path.dirname(m))
    for d in (local_cfg.get("models_dirs") or []):
        dirs.append(d)
    seen, out = set(), []
    for d in dirs:
        d = os.path.abspath(d) if d else d
        if d and d not in seen and os.path.isdir(d):
            seen.add(d); out.append(d)
    return out

def build_registry():
    """Scan the model dirs -> a list of {id, path, size_mb, kind, mmproj, template}. `id` is the filename
    (stable, unique, what shows in the repo). mmproj/vision companion files are detected and attached to
    their sibling VLM, never listed as their own chat model."""
    files = {}     # id -> full path. Same name in two dirs: the LARGER file wins (a truncated copy is always
                   # smaller than the real one) — never a file we can't stat, never a .part.
    for d in _model_dirs():
        try:
            for n in sorted(os.listdir(d)):
                if not n.lower().endswith(".gguf"):
                    continue
                fp = os.path.join(d, n)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                if n in files:
                    try:
                        if sz <= os.path.getsize(files[n]):
                            continue
                    except OSError:
                        pass
                    print(f"[registry] {n}: two copies — keeping the larger ({fp}); the smaller is likely a truncated download", flush=True)
                files[n] = fp
        except OSError:
            pass
    # A projector is identified by its OWN metadata (general.type == mmproj / clip.has_vision_encoder), not
    # by hoping its filename says so -- and the name check stays as the cheap first pass for files we can't
    # parse. Everything here is cached, so this costs one read per file per change.
    caps_of  = {i: model_caps(p) for i, p in files.items()}
    mmprojs  = {i: p for i, p in files.items()
                if caps_of[i].get("is_mmproj") or any(h in i.lower() for h in _MMPROJ_HINT)}
    # Pair PROJECTOR -> model, not the other way round: a projector belongs to exactly one model, while a
    # model may sit next to none. Matching clip.vision.projection_dim against embedding_length is NECESSARY
    # but not sufficient -- in this library three models are 5120 wide -- so a tie is broken by the
    # publisher's own general.basename, and an unbreakable tie pairs NOTHING rather than guessing (a wrong
    # projector doesn't degrade, it fails at load).
    paired = {}                                             # model id -> projector path
    for mi, mp in mmprojs.items():
        dim = caps_of[mi].get("projection_dim")
        if not dim:
            continue
        cand = [i for i in files
                if i not in mmprojs and caps_of[i].get("embedding_length") == dim]
        if len(cand) > 1:
            base = caps_of[mi].get("basename")
            narrowed = [i for i in cand if base and caps_of[i].get("basename") == base]
            if not narrowed:
                nm = (caps_of[mi].get("name") or "").strip().lower()
                narrowed = [i for i in cand if (caps_of[i].get("name") or "").strip().lower() == nm and nm]
            cand = narrowed
        if len(cand) == 1 and cand[0] not in paired:
            paired[cand[0]] = mp
    reg = []
    for i, p in files.items():
        if i in mmprojs:
            continue                                        # companion file, not a model
        low  = i.lower()
        caps = dict(caps_of[i])
        # VLM detection is EVIDENCE-driven: a model is a VLM when a projector in the library actually fits
        # it -- clip.vision.projection_dim must equal this model's embedding_length (that is the tensor the
        # projector writes into), with general.basename breaking a tie between two that fit. The old
        # name-driven rule got this library exactly backwards: it called Qwen3.8-27B text-only and handed
        # its 5120-wide projector to SmolVLM-500M (960 wide), a pair that could never have loaded.
        sib = paired.get(i)
        if sib is None and any(h in low for h in _VLM_HINT) and not caps.get("embedding_length"):
            # unreadable file that at least CLAIMS to be a VLM by name — keep the old same-dir guess
            same_dir = [mp for mp in mmprojs.values() if os.path.dirname(mp) == os.path.dirname(p)]
            stem = low.split("-")[0]
            pref = [mp for mp in same_dir if stem in os.path.basename(mp).lower()]
            sib = (pref or same_dir or [None])[0]
        caps["vision"] = bool(sib)
        try: size_mb = int(os.path.getsize(p) / (1024 * 1024))
        except OSError: size_mb = 0
        kind = "other" if caps.get("chat") is False else ("vlm" if sib else "text")
        reg.append({"id": i, "path": p, "size_mb": size_mb,
                    "kind": kind,
                    "mmproj": sib, "template": "chatml",
                    "caps": caps, "caps_why": caps_summary(caps)})
    reg.sort(key=lambda m: m["id"].lower())
    return reg

def registry_index():
    """id -> model dict, for O(1) lookup by name."""
    return {m["id"]: m for m in build_registry()}

def goal_model_map():
    """config 'goal_models' {goal: model-id}. Unset goals fall back to the default model (config 'model')."""
    gm = load_config().get("goal_models")
    return gm if isinstance(gm, dict) else {}

# --- D48 ROLES: say what you want done, not which GGUF does it -------------------------------------------
# A role is NOT a model. It is `base model + system prompt + tool belt + knowledge + effort goal`, and two
# roles can share one base model. GENGHIS ships the MECHANISM and a few generic stock roles; WHICH roles a
# house runs -- and what they research -- is a choice and lives in the HOME (D36), never in this repo.
# The coordinator therefore never learns what any particular role IS. It loads role files, checks their
# stated requirements against the registry's capability metadata (roles step 1), and refuses out loud.
#
# Vocabulary (settled 2026-09-22): "role" from here on means THIS -- a way of working. The four things a
# BOX can be (Authority / Host / Donor / Surface) are "node capabilities", which is what D35's own title
# called them all along. config.roles{} keeps its key for compatibility; nothing migrates.
_ROLE_CACHE = {"stamp": None, "roles": {}}
_ROLE_LOCK  = threading.Lock()
_ROLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def home_dir():
    """The HOME (D36): where CHOICES live -- roles, personas, routines -- as opposed to the mechanisms in
    this repo. `GENGHIS_HOME` wins, then config `home_dir`, then the shipped `home.example/` so a fresh
    install has working stock roles instead of an empty feature with nothing in it."""
    h = (os.environ.get("GENGHIS_HOME") or "").strip()
    if not h:
        try:
            h = ((_load_json_resilient(CONFIG, what="config.json") or {}).get("home_dir") or "").strip()
        except Exception:
            h = ""
    if not h:
        h = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home.example")
    return os.path.abspath(h)


def roles_dir():
    return os.path.join(home_dir(), "roles")


def _role_defaults(rid, raw, path):
    """One role file -> the shape the rest of the code can rely on. Unknown keys are KEPT (a home may carry
    fields a newer GENGHIS understands); missing ones get honest defaults."""
    r = dict(raw)
    r["id"]          = rid
    r["name"]        = (raw.get("name") or rid.replace("-", " ").replace("_", " ").title())
    r["description"] = raw.get("description") or ""
    r["goal"]        = raw.get("goal") if raw.get("goal") in GOALS else "balanced"
    r["model"]       = (raw.get("model") or "").strip() or None      # optional hard pin
    r["requires"]    = raw.get("requires") if isinstance(raw.get("requires"), dict) else {}
    r["tools"]       = [t for t in (raw.get("tools") or []) if isinstance(t, str)]
    r["knowledge"]   = (raw.get("knowledge") or "").strip() or None
    r["voice"]       = (raw.get("voice") or "").strip() or None      # a service engine (PersonaPlex); not wired yet
    r["source"]      = path
    sysp = raw.get("system")
    if not sysp and raw.get("system_file"):                          # a long prompt belongs in its own file
        fp = os.path.join(os.path.dirname(path), raw["system_file"])
        try:
            with open(fp, encoding="utf-8") as f:
                sysp = f.read()
        except OSError as e:
            sysp = ""
            r["warning"] = f"system_file '{raw['system_file']}' could not be read ({e.__class__.__name__})"
    r["system"] = (sysp or "").strip()
    return r


def load_roles(force=False):
    """<home>/roles/*.json -> {id: role}. One file per role; the filename stem is the id unless the file
    names one. Re-read when any file's mtime changes, so editing a role takes effect without a restart."""
    d = roles_dir()
    try:
        files = sorted(n for n in os.listdir(d) if n.lower().endswith(".json"))
        stamp = (d, tuple((n, int(os.path.getmtime(os.path.join(d, n)))) for n in files))
    except OSError:
        files, stamp = [], (d, ())
    with _ROLE_LOCK:
        if not force and _ROLE_CACHE["stamp"] == stamp:
            return _ROLE_CACHE["roles"]
        roles = {}
        for n in files:
            p = os.path.join(d, n)
            try:
                with open(p, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print(f"[roles] {n}: not loaded — {e.__class__.__name__}: {e}", flush=True)
                continue
            if not isinstance(raw, dict):
                print(f"[roles] {n}: not loaded — a role file must be one JSON object", flush=True)
                continue
            rid = (raw.get("id") or os.path.splitext(n)[0]).strip().lower()
            if not _ROLE_ID_RE.match(rid):
                print(f"[roles] {n}: not loaded — id '{rid}' must be lowercase letters/digits/-/_", flush=True)
                continue
            if rid in GOALS:
                # `genghis-fastest` is already an effort goal; a role of the same name would shadow it.
                print(f"[roles] {n}: not loaded — '{rid}' is an effort goal name; pick another id", flush=True)
                continue
            roles[rid] = _role_defaults(rid, raw, p)
        _ROLE_CACHE["stamp"], _ROLE_CACHE["roles"] = stamp, roles
        return roles


def role_unmet(role, caps):
    """Which of a role's stated requirements this model FAILS, in the words a human needs. Empty list = fit.
    Only keys the role actually states are checked -- a role that says nothing requires nothing."""
    req, bad = role.get("requires") or {}, []
    if not caps or caps.get("error"):
        return ["its capabilities could not be read from the file"]
    if caps.get("chat") is False:
        # Not a requirement a role has to state: nothing can run a chat on a diffusion model.
        return [caps.get("why_not_chat") or "not a chat model"]
    want_tools = req.get("tools")
    if want_tools:
        have = caps.get("tools")
        rank = {"none": 0, "partial": 1, "native": 2}
        if rank.get(have, 0) < rank.get(want_tools, 2):
            bad.append(f"needs tool support '{want_tools}', this model is '{have}'")
    if req.get("tool_results") and not caps.get("tool_results"):
        # The quiet one. Worth its own sentence because nothing else in the system will ever complain.
        bad.append("its chat template has no branch for a tool's reply, so every tool result would be "
                   "silently dropped and the model would answer from imagination")
    if req.get("vision") and not caps.get("vision"):
        bad.append("needs vision; no matching projector is paired with this model")
    if req.get("reasoning") and not caps.get("reasoning"):
        bad.append("needs a reasoning model")
    need_ctx = req.get("ctx_train")
    if need_ctx:
        have_ctx = caps.get("ctx_train") or 0
        if have_ctx < int(need_ctx):
            bad.append(f"needs {int(need_ctx)//1024}k trained context, this model has {have_ctx//1024}k")
    return bad


def resolve_role(rid):
    """A role id -> what it will ACTUALLY run, and why.

    Returns a dict: {role, model_id, path, goal, substituted, unmet, problem}.
    Order: a pinned `model` is honoured even if it falls short (never substitute a user's explicit choice in
    silence -- we report it instead); otherwise the role's effort goal picks a model, and if THAT model does
    not meet the role's requirements we substitute the qualifying model closest in size to it, so the tier
    the role asked for is preserved. If nothing in the library qualifies, `problem` says so and the caller
    refuses -- a role that cannot work must not half-work."""
    roles = load_roles()
    role = roles.get(rid)
    if not role:
        return {"role": None, "problem": f"no role '{rid}' in {roles_dir()}"}
    idx  = registry_index()
    out  = {"role": role, "goal": role["goal"], "substituted": None, "unmet": [], "problem": None,
            "model_id": None, "path": None}

    if role["model"]:
        m = idx.get(role["model"])
        if not m:
            out["problem"] = (f"role '{rid}' pins model '{role['model']}', which is not in this host's "
                              f"library — add it, or drop the pin to let the '{role['goal']}' goal choose")
            return out
        out["model_id"], out["path"] = m["id"], m["path"]
        out["unmet"] = role_unmet(role, m.get("caps"))
        return out

    want = goal_model_map().get(role["goal"])
    base = idx.get(want) if want else None
    if base is None:
        dflt = os.path.basename((load_config().get("model") or "").strip())
        base = idx.get(dflt)
    if base is not None and not role_unmet(role, base.get("caps")):
        out["model_id"], out["path"] = base["id"], base["path"]
        return out

    # The goal's model falls short. Pick the qualifying model nearest it in size: the role still gets the
    # tier it asked for, and we say plainly what happened and why.
    ok = [m for m in idx.values() if not role_unmet(role, m.get("caps"))]
    if not ok:
        out["unmet"] = role_unmet(role, (base or {}).get("caps")) or ["no model in this library qualifies"]
        out["problem"] = (f"no model on this host meets role '{rid}': "
                          + "; ".join(out["unmet"]))
        return out
    target = base["size_mb"] if base else max(m["size_mb"] for m in ok)
    pick   = min(ok, key=lambda m: (abs(m["size_mb"] - target), m["size_mb"]))
    out["model_id"], out["path"] = pick["id"], pick["path"]
    if base is not None:
        out["substituted"] = {"from": base["id"], "to": pick["id"],
                              "why": "; ".join(role_unmet(role, base.get("caps")))}
    return out


def apply_role_to_request(res, data):
    """Put the role INTO the OpenAI request: its system prompt in front of the conversation (never replacing
    a system message the caller wrote -- the role's goes first, the caller's still applies), and a note when
    a role with a declared tool belt was called with no tools attached.

    GENGHIS cannot install tools into someone else's chat client, so the belt is DECLARED here and surfaced
    in /roles.json and /v1/models for the client (or `genghis ui provision`, §4b) to act on. Saying that
    plainly beats pretending the belt is live."""
    role = res["role"]
    msgs = data.get("messages")
    if role.get("system") and isinstance(msgs, list):
        data["messages"] = [{"role": "system", "content": role["system"]}] + msgs
    notes = []
    # D49: the belt is no longer only a declaration. Any adapter the role names that is present, enabled and
    # reachable contributes its operations as tools GENGHIS itself will execute; the client's own tools (if
    # any) are left exactly as they are and still belong to the client.
    atools, anotes, owned = adapter_tools(role)
    res["owned_tools"] = owned
    notes.extend(anotes)
    if atools:
        data["tools"] = (data.get("tools") or []) + atools
        data.setdefault("tool_choice", "auto")
    if role.get("tools") and not atools and not data.get("tools"):
        notes.append(f"role '{role['id']}' expects a tool belt ({', '.join(role['tools'])}) but none of it "
                     f"is live and this request arrived with no tools attached — the model has no tools")
    if role.get("knowledge"):
        notes.append(f"role '{role['id']}' declares knowledge at {role['knowledge']} — retrieval is not "
                     f"built yet, so the model is NOT reading it")
    if role.get("voice"):
        notes.append(f"role '{role['id']}' declares a voice service ({role['voice']}) — not wired yet")
    if role.get("warning"):
        notes.append(role["warning"])
    res["notes"] = notes
    return notes


def role_why(res):
    """One line a human (or the Thinking panel) can read about a role resolution."""
    if not res or not res.get("role"):
        return (res or {}).get("problem") or "unknown role"
    r = res["role"]
    if res.get("problem"):
        return f"{r['name']}: {res['problem']}"
    line = f"{r['name']} → {res['model_id']} ({r['goal']})"
    if res.get("substituted"):
        s = res["substituted"]
        line += f" — {s['from']} was the '{r['goal']}' model but {s['why']}"
    elif res.get("unmet"):
        line += " — WARNING: " + "; ".join(res["unmet"])
    return line


# --- D49 ADAPTERS: the tool belt becomes real, without handing the model the keys ------------------------
# A role's belt was DECLARED (D48). An adapter is what makes it act. The design is forced by what the first
# one actually offers: Blender's MCP add-on exposes a single operation -- run arbitrary Python inside
# Blender (`{"type":"execute","code":...}` null-delimited on :9876). There is no safe subset to hand over.
#
# So GENGHIS does NOT let a model write code. An adapter declares NAMED OPERATIONS whose body the adapter's
# author wrote; the model chooses which operation to call and supplies typed ARGUMENTS, which are
# substituted as JSON literals, never string-spliced into source. The model picks the verb; a human wrote
# the sentence. A raw-code escape hatch can exist, but only if the adapter file turns it on by name.
#
# Everything is off until someone says otherwise: adapters ship disabled, a disabled or unreachable adapter
# contributes no tools, and a refused call comes back as a tool RESULT that says why (D31 -- never silent;
# a tool that fails quietly is how a model starts inventing what it "did").
_ADAPTER_CACHE = {"stamp": None, "adapters": {}}
_ADAPTER_LOCK  = threading.Lock()
ADAPTER_MAX_ROUNDS = 6            # model -> tool -> model round trips per request, then we stop and say so


def adapters_dir():
    return os.path.join(home_dir(), "adapters")


def merge_tool_call_deltas(acc, tc):
    """Rebuild whole tool calls from an OpenAI STREAM. A streamed call arrives in fragments keyed by
    `index`: the name usually once, the JSON arguments a few characters at a time. Accumulate into
    {index: call}; `tool_calls_from(acc)` hands back the finished list. (The non-stream path gets whole
    calls and needs none of this.)"""
    for frag in tc or []:
        i = frag.get("index", 0)
        # `index` is kept: if these turn out NOT to be ours, they are relayed to the client, and an OpenAI
        # streaming client needs the index to reassemble them. Execution ignores it.
        cur = acc.setdefault(i, {"index": i, "id": None, "type": "function",
                                 "function": {"name": "", "arguments": ""}})
        if frag.get("id"):
            cur["id"] = frag["id"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            cur["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            cur["function"]["arguments"] += fn["arguments"]
    return acc


def tool_calls_from(acc):
    return [acc[k] for k in sorted(acc)]


def load_adapters(force=False):
    """<home>/adapters/*.json -> {id: adapter}. Re-read on mtime change, like roles."""
    d = adapters_dir()
    try:
        files = sorted(n for n in os.listdir(d) if n.lower().endswith(".json"))
        stamp = (d, tuple((n, int(os.path.getmtime(os.path.join(d, n)))) for n in files))
    except OSError:
        files, stamp = [], (d, ())
    with _ADAPTER_LOCK:
        if not force and _ADAPTER_CACHE["stamp"] == stamp:
            return _ADAPTER_CACHE["adapters"]
        out = {}
        for n in files:
            p = os.path.join(d, n)
            try:
                with open(p, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print(f"[adapters] {n}: not loaded — {e.__class__.__name__}: {e}", flush=True)
                continue
            if not isinstance(raw, dict):
                continue
            aid = (raw.get("id") or os.path.splitext(n)[0]).strip().lower()
            if not _ROLE_ID_RE.match(aid):
                print(f"[adapters] {n}: not loaded — bad id '{aid}'", flush=True)
                continue
            a = dict(raw)
            a["id"]        = aid
            a["name"]      = raw.get("name") or aid.title()
            a["enabled"]   = bool(raw.get("enabled"))          # OFF unless the file says otherwise
            a["transport"] = (raw.get("transport") or "").strip().lower()
            a["host"]      = raw.get("host") or "localhost"
            a["port"]      = int(raw.get("port") or 0)
            a["timeout"]   = float(raw.get("timeout") or 30)
            a["operations"] = raw.get("operations") if isinstance(raw.get("operations"), dict) else {}
            a["source"]    = p
            out[aid] = a
        _ADAPTER_CACHE["stamp"], _ADAPTER_CACHE["adapters"] = stamp, out
        return out


def adapter_reachable(a):
    """(ok, detail). A cheap TCP dial -- we do not run anything to find out if a thing is up."""
    if a["transport"] != "blender-socket":
        return False, f"unknown transport '{a['transport']}'"
    try:
        s = socket.create_connection((a["host"], a["port"]), timeout=2)
        s.close()
        return True, f"{a['host']}:{a['port']} answers"
    except Exception as e:
        return False, f"{a['host']}:{a['port']} — {e.__class__.__name__}"


def _blender_send(a, code, strict_json=False):
    """The Blender add-on's wire format: one JSON object, NUL-delimited, both directions."""
    req = json.dumps({"type": "execute", "code": code, "strict_json": strict_json}) + "\0"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(a["timeout"])
        s.connect((a["host"], a["port"]))
        s.sendall(req.encode("utf-8"))
        buf = bytearray()
        while True:
            ch = s.recv(65536)
            if not ch:
                break
            buf.extend(ch)
            if b"\0" in buf:
                break
    if not buf:
        raise RuntimeError("empty response from the adapter")
    line, _, _ = bytes(buf).partition(b"\0")
    return json.loads(line.decode("utf-8", "replace"))


def adapter_tool_name(aid, op):
    return f"{aid}_{op}"


def adapter_tools(role):
    """The OpenAI tool schemas a ROLE actually gets: every operation of every adapter its belt names, when
    that adapter is enabled AND reachable. A belt entry that is none of those contributes nothing -- and the
    caller reports why, rather than leaving the model to call a tool that was never there."""
    tools, notes, owned = [], [], {}
    known = load_adapters()
    for aid in role.get("tools") or []:
        a = known.get(aid)
        if not a:
            notes.append(f"'{aid}' is not an adapter in {adapters_dir()}"); continue
        if not a["enabled"]:
            notes.append(f"adapter '{aid}' is present but DISABLED (set \"enabled\": true in {os.path.basename(a['source'])} to arm it)")
            continue
        ok, detail = adapter_reachable(a)
        if not ok:
            notes.append(f"adapter '{aid}' is enabled but unreachable — {detail}")
            continue
        for op, spec in (a["operations"] or {}).items():
            if not isinstance(spec, dict) or not spec.get("code"):
                continue
            name = adapter_tool_name(aid, op)
            tools.append({"type": "function", "function": {
                "name": name,
                "description": spec.get("description") or f"{a['name']}: {op}",
                "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
            }})
            owned[name] = (aid, op)
        notes.append(f"adapter '{aid}' armed — {detail}")
    return tools, notes, owned


def run_adapter_tool(owned, name, args):
    """Execute ONE adapter tool call and return the string a `role:\"tool\"` message will carry.

    The model never supplies code. It named an operation and gave arguments; the operation's Python was
    written by whoever wrote the adapter file, and each argument is injected as a JSON LITERAL bound to a
    variable -- no string splicing into source, so an argument cannot become code."""
    aid, op = owned[name]
    a = load_adapters().get(aid)
    if not a or not a["enabled"]:
        return json.dumps({"ok": False, "error": f"adapter '{aid}' is not enabled"})
    spec = (a["operations"] or {}).get(op) or {}
    code = spec.get("code")
    if not code:
        return json.dumps({"ok": False, "error": f"operation '{op}' has no code"})
    if not isinstance(args, dict):
        args = {}
    allowed = ((spec.get("parameters") or {}).get("properties") or {})
    if allowed:                                        # an argument the operation never declared is dropped,
        args = {k: v for k, v in args.items() if k in allowed}   # not passed through to the host
    preamble = "".join(f"{k} = {json.dumps(v)}\n" for k, v in args.items())
    try:
        r = _blender_send(a, preamble + code)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{e.__class__.__name__}: {e}"})
    if r.get("status") != "ok":
        return json.dumps({"ok": False, "error": r.get("message") or "the adapter reported an error",
                           "stderr": (r.get("stderr") or "")[:800]})
    return json.dumps({"ok": True, "result": r.get("result"),
                       "stdout": (r.get("stdout") or "")[:4000]})


def resolve_model(model_field):
    """Map an OpenAI 'model' request field -> (gguf_path, goal). Accepts:
      - 'genghis-<goal>'  -> that effort goal; GGUF = goal_models[goal] or the default model
      - a registry id     -> THAT model, placed with the server default goal
      - blank/unknown     -> default model + default goal
    GENGHIS_MODEL env still hard-overrides the path (host pin)."""
    field = (model_field or "").strip()
    idx = registry_index()
    default_path = (load_config().get("model") or "").strip() or _MODEL_DEFAULT
    if "GENGHIS_MODEL" in os.environ:
        default_path = os.environ["GENGHIS_MODEL"]

    # D48: `genghis-<role>` resolves through the role (its pin or its goal, checked against the registry's
    # capability metadata). Goals are matched first, so a stock goal name can never be shadowed by a home.
    if field.startswith("genghis-") and field[len("genghis-"):] not in GOALS:
        res = resolve_role(field[len("genghis-"):])
        if res.get("role") and res.get("path"):
            return res["path"], res["goal"]

    if field.startswith("genghis-") and field[len("genghis-"):] in GOALS:
        goal = field[len("genghis-"):]
        mid = goal_model_map().get(goal)
        if mid and mid not in idx:
            # The goal maps to a model this host doesn't have locally: honour the map and let fetch-if-missing
            # pull it from the repository -- never substitute a different (smaller) model in silence.
            return os.path.join(MODELS_DIR, mid), goal
        path = idx[mid]["path"] if mid in idx else default_path
        return path, goal
    if field in idx:                                        # user picked a concrete model by name
        return idx[field]["path"], active_goal()
    if field in GOALS:                                      # bare goal name (no genghis- prefix)
        mid = goal_model_map().get(field)
        if mid and mid not in idx:
            return os.path.join(MODELS_DIR, mid), field
        return (idx[mid]["path"] if mid in idx else default_path), field
    return default_path, active_goal()

def set_active_model(path):
    """Point THIS request's model at `path` (its thread-local overlay); outside a request, the process default."""
    global MODEL
    if not path:
        return
    if _in_request():
        _REQ.model = path
    elif path != MODEL:
        MODEL = path


def model_path():
    """Best LOCAL path for the configured MODEL: the path itself if it exists, else the repo-cache
    copy under MODELS_DIR, else the configured value unchanged (may not exist yet)."""
    m = active_model()
    if os.path.exists(m):
        return m
    cand = os.path.join(MODELS_DIR, os.path.basename(m))
    return cand if os.path.exists(cand) else m


def model_repo_base():
    """The coordinator's model endpoint, derived from the fleet authority URL (…/fleet.json → …/models).
    When FLEET_URL is the localhost default (a `serve` host reads its OWN fleet.json — D22) but this box is
    NOT the fleet's coordinator, the repo is the coordinator that `genghis init --coord` recorded in
    fleet.json — not ourselves. (The NUC's first chat 404'd fetching the model from its own empty /models:
    its serve was launched from a shell that predated the installer's GENGHIS_COORD env var.)"""
    base = FLEET_URL.rsplit("/", 1)[0]
    if base.startswith(("http://127.0.0.1", "http://localhost")):
        try:
            with open(FLEET, encoding="utf-8") as f:
                c = json.load(f).get("coordinator") or {}
            ip = (c.get("ip") or "").strip()
            if ip and ip not in ("127.0.0.1", "localhost", "0.0.0.0") and ip.lower() != SELF_HOST:
                base = f"http://{ip}:{int(c.get('serve_port') or 8899)}"
        except Exception:
            pass
    return base + "/models"


def ensure_model():
    """Return a local path to the model, fetching it from the Pi repo if it isn't here. Used by run/decide.
    The laptop (full path exists) never touches the network; a bare-name client pulls once, then caches."""
    p = model_path()
    if os.path.exists(p):
        return p
    name = os.path.basename(active_model())
    url = model_repo_base() + "/" + urllib.parse.quote(name)
    os.makedirs(MODELS_DIR, exist_ok=True)
    dest = os.path.join(MODELS_DIR, name)
    print(f"[model] {name} not local — fetching from repo {url} …")
    _download(url, dest)
    print(f"[model] cached at {dest}")
    return dest


# --- D28: background model fetch -------------------------------------------------------------------------
# A model that isn't local yet is fetched by a background thread, NEVER inside an HTTP request: a 1-40 GB
# pull takes minutes and every HTTP client (Open WebUI, curl, Invoke-RestMethod) gives up long before that,
# so the first chat on a fresh box "closed unexpectedly" with no explanation. Now the request gets an
# immediate, explicit answer (503 model_loading + progress) and `serve` pre-fetches at startup.
_FETCH = {"name": None, "done": 0, "total": 0, "error": None, "active": False}
_FETCH_LOCK = threading.Lock()

def fetch_status():
    """Snapshot of the background fetch (for /registry.json + the Control Room + 503 messages)."""
    with _FETCH_LOCK:
        st = dict(_FETCH)
    st["pct"] = (st["done"] * 100 // st["total"]) if st["total"] else 0
    return st

def start_fetch(model_ref):
    """Fetch `model_ref` (a bare name or a path whose basename is in the repo) into MODELS_DIR in the
    background. Returns immediately: True if a fetch is now running for it (new or already in flight),
    False if it could not be started (another model is mid-fetch — one at a time keeps a small box sane)."""
    name = os.path.basename(model_ref)
    with _FETCH_LOCK:
        if _FETCH["active"]:
            return _FETCH["name"] == name
        _FETCH.update(name=name, done=0, total=0, error=None, active=True)

    def _run():
        url = model_repo_base() + "/" + urllib.parse.quote(name)
        os.makedirs(MODELS_DIR, exist_ok=True)
        dest = os.path.join(MODELS_DIR, name)
        def prog(done, total):
            with _FETCH_LOCK:
                _FETCH["done"], _FETCH["total"] = done, total
        print(f"[model] {name} not local — fetching from repo {url} …", flush=True)
        try:
            _download(url, dest, prog)
            print(f"[model] cached at {dest}", flush=True)
            err = None
        except Exception as e:
            err = f"{e}"
            print(f"[model] fetch of {name} FAILED: {e}", flush=True)
        with _FETCH_LOCK:
            _FETCH["active"] = False; _FETCH["error"] = err
    threading.Thread(target=_run, name=f"fetch:{name}", daemon=True).start()
    return True

def ensure_model_async():
    """serve-side counterpart of ensure_model(): the local path if the model is here, else kick off (or
    join) a background fetch and return None — the caller answers the request with a model_loading
    status instead of blocking."""
    p = model_path()
    if os.path.exists(p):
        return p
    start_fetch(active_model())
    return None

def prefetch_models():
    """At `serve` start: make sure the default model and every goal-mapped model are local, fetching the
    missing ones one after another in the background — so the first chat usually finds them already here."""
    cfg = load_config()
    wanted = []
    # Only the DEFAULT model and the `fastest` one. Pulling every goal's model meant a fresh host started
    # downloading 60+ GB (32B + 27B + 70B) over Wi-Fi the moment it came up; the bigger models fetch on
    # first use instead, with the D28 progress message. Set config "prefetch": "all" to opt in to everything.
    gm = cfg.get("goal_models") or {}
    refs = [cfg.get("model") or _MODEL_DEFAULT, gm.get("fastest")]
    if cfg.get("prefetch") == "all":
        refs += list(gm.values())
    for ref in refs:
        ref = (ref or "").strip()
        if ref and ref not in wanted:
            wanted.append(ref)
    def _run():
        for ref in wanted:
            name = os.path.basename(ref)
            try:
                local_ids = set(registry_index())              # every GGUF in ALL configured models_dirs (D23)
            except Exception:
                local_ids = set()
            if os.path.exists(ref) or os.path.exists(os.path.join(MODELS_DIR, name)) or name in local_ids:
                continue
            try:
                with urllib.request.urlopen(urllib.request.Request(model_repo_base(), headers=_auth_headers()), timeout=6) as r:
                    names = {m["name"] for m in json.load(r).get("models", [])}
            except Exception as e:
                print(f"[model] prefetch: repo unreachable ({e}) — will fetch on first use", flush=True); return
            if name not in names:
                print(f"[model] prefetch: {name} is not in the repo — skipping", flush=True); continue
            start_fetch(name)
            while fetch_status()["active"]:
                time.sleep(2)
    threading.Thread(target=_run, name="prefetch", daemon=True).start()


def _download(url, dest, progress=None):
    """Stream a URL to `dest` with a progress line, RESUMING a partial `.part` if one exists.
    A dropped transfer (a 42 GB model over flaky WiFi) picks up where it left off via an HTTP Range
    request; the server answers 206 (append) or, if it ignores Range, 200 (restart from zero).
    `progress(done_bytes, total_bytes)` is called as data lands (the serve-side fetch state, D28)."""
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url, headers=_auth_headers())
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        _r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:                    # leftover .part >= server file: discard + restart clean
            try: os.remove(tmp)
            except OSError: pass
            return _download(url, dest, progress)
        raise
    with _r as r:
        resuming = (r.status == 206)                  # server honored Range -> append; else start fresh
        clen = int(r.headers.get("Content-Length", 0))
        total = (have + clen) if resuming else clen
        done = have if resuming else 0
        if have and not resuming:
            have = 0                                  # server ignored Range: overwrite from the top
        mode = "ab" if resuming else "wb"
        if resuming:
            print(f"  resuming at {have/1e6:.0f} MB")
        last_pct = -1
        with open(tmp, mode) as f:
            while True:
                chunk = r.read(1024 * 512)
                if not chunk:
                    break
                f.write(chunk); done += len(chunk)
                if progress:
                    progress(done, total)
                if total:
                    pct = done * 100 // total
                    if pct // 5 != last_pct // 5:            # one line per 5% — a redirected log is not a TTY
                        last_pct = pct
                        print(f"  {done/1e6:.0f}/{total/1e6:.0f} MB ({pct}%)", flush=True)
    if total and done < total:
        # A dropped connection ends the read loop exactly like EOF. Renaming a short file into place made a
        # 397 MB "70B" that the registry then trusted (sized it as fits-solo; llama-server choked). Keep the
        # .part for the Range-resume and say so.
        raise IOError(f"transfer ended early: {done/1e6:.0f} of {total/1e6:.0f} MB — kept {os.path.basename(tmp)} for resume")
    os.replace(tmp, dest)


def list_local_models():
    """(name, size_mb) for every .gguf in the local models dir."""
    out = []
    try:
        for n in sorted(os.listdir(MODELS_DIR)):
            if n.lower().endswith(".gguf"):
                out.append((n, os.path.getsize(os.path.join(MODELS_DIR, n)) / (1024 * 1024)))
    except OSError:
        pass
    return out


def discover_cmd(fleet):
    """Find the coordinator on the LAN via mDNS (zero-config) and print it."""
    print("discovering the GENGHIS coordinator on the LAN (mDNS, 3s)…")
    url = None
    try:
        import genghis_mdns
        url = genghis_mdns.discover(timeout=3.0)
    except Exception as e:
        print(f"  mDNS unavailable: {e}")
    if url:
        print(f"  -> {url}")
        print(f"     (set GENGHIS_FLEET_URL={url} to pin it, or rely on auto-discovery)")
    else:
        print("  no coordinator found — is `serve` running on the same LAN? (mDNS uses UDP 5353)")


def _detect_ram_mb():
    """Total physical RAM in MB, cross-platform, no third-party deps."""
    try:
        if os.name == "nt":
            import ctypes
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                            ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]
            m = MS(); m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullTotalPhys / (1024 * 1024))
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    return int(int(ln.split()[1]) / 1024)
    except Exception:
        pass
    return 0


def _detect_local_node(node_id, models_dir):
    """Best-effort probe of THIS machine -> a local-anchor donor dict for a fresh fleet. Never leaks anyone
    else's data — everything comes from the running host. Falls back to a CPU node if no GPU is found."""
    node = {"id": node_id, "host": socket.gethostname().lower(), "local": True, "arch": platform.machine().lower(),
            "cores": os.cpu_count() or 1, "ram_total_mb": _detect_ram_mb(), "role": "compute",
            "link_type": "local", "latency_ms_to_orchestrator": 0.0, "tokens_per_s_solo": 0.0,
            "status": "up", "reliability": 1.0, "accelerator": "cpu", "device": "CPU",
            "notes": "This machine (local anchor — no RPC hop). Written by `genghis init`."}
    # NVIDIA CUDA?
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8)
        if out.returncode == 0 and out.stdout.strip():
            name, mem = (out.stdout.strip().splitlines()[0].split(",", 1) + [""])[:2]
            node.update(accelerator="cuda", device="CUDA0", gpu=name.strip(),
                        vram_total_mb=int(float(mem.strip() or 0)))
            return node
    except Exception:
        pass
    # Vulkan (Arc/AMD/etc.) via the local llama-cli, if built
    try:
        if os.path.exists(LLAMA_CLI):
            out = subprocess.run([LLAMA_CLI, "--list-devices"], capture_output=True, text=True, timeout=15)
            for ln in (out.stdout or "").splitlines():
                if "Vulkan" in ln or "vulkan" in ln:
                    node.update(accelerator="vulkan", device="Vulkan0",
                                gpu=ln.strip(), notes=node["notes"] + " (Vulkan GPU detected)")
                    break
    except Exception:
        pass
    return node


def _init_opt(rest, key, default=None):
    """Tiny `--key value` / `--flag` parser over argparse's leftover `rest` (init-only options)."""
    if f"--{key}" not in rest:
        return default
    i = rest.index(f"--{key}")
    if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
        return rest[i + 1]
    return True   # bare flag


def _is_tailnet_ip(ip):
    """True for a Tailscale CGNAT address (100.64.0.0/10)."""
    try:
        a, b = (int(x) for x in str(ip or "").split(".")[:2])
        return a == 100 and 64 <= b <= 127
    except Exception:
        return False


def _lan_ip_towards(host, port=8899):
    """The local interface address that reaches `host` — what OTHER nodes must dial to reach us."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect((host, port)); return sk.getsockname()[0]
    except Exception:
        return None


def register_self(coord=None, port=None, quiet=False):
    """D33 — announce THIS box to the fleet authority so it appears in everyone's fabric without a hand edit.
    Reads our own fleet.json local entry (written by `init`), adds the address other nodes must dial, and
    POSTs {"action":"register","node":{…}} to the authority's /fleet. Idempotent (upsert). Returns (ok, note)."""
    url_base = None
    if coord:
        url_base = f"http://{coord if ':' in coord else coord + ':8899'}"
    else:
        url_base = FLEET_URL.rsplit("/", 1)[0]
        if url_base.startswith(("http://127.0.0.1", "http://localhost")):
            try:
                with open(FLEET, encoding="utf-8") as fh:
                    c = json.load(fh).get("coordinator") or {}
                if c.get("ip") and c["ip"] not in ("127.0.0.1", "localhost") and (c.get("host") or "").lower() != SELF_HOST:
                    url_base = f"http://{c['ip']}:8899"
            except Exception:
                pass
    if url_base.startswith(("http://127.0.0.1", "http://localhost")):
        return False, "this box IS the authority (localhost) — nothing to register; other nodes register to it"
    try:
        with open(FLEET, encoding="utf-8") as fh:
            mine = [d for d in json.load(fh).get("donors", []) if d.get("local") and is_self_node(d)]
    except Exception:
        mine = []
    node = dict(mine[0]) if mine else _detect_local_node(SELF_HOST, MODELS_DIR)
    host = url_base.split("//", 1)[1].split(":")[0]
    ip = _lan_ip_towards(host) or node.get("ip")
    node["ip"] = ip
    # D35: AWAY = host, never donor. Reaching the authority over the tailnet (CGNAT 100.64/10) means every
    # RPC token would be an internet round trip -- worse than the Wi-Fi we just escaped. Register with no
    # RPC endpoint; the box stays a full host/operator (Control Room, /v1, its own GPU) over Tailscale.
    away = _is_tailnet_ip(ip) or _is_tailnet_ip(host)
    if away:
        node["away"] = True
    else:
        node.pop("away", None)
    # a donor publishes its RPC endpoint; a pure client has none (and then is a private anchor, D27)
    rpc_port = None if away else (port if port is not None else node.get("port"))
    if rpc_port is None and not away:
        try:
            with socket.create_connection(("127.0.0.1", 50052), timeout=0.5):
                rpc_port = 50052                       # an rpc-server is running here -> we are a donor too
        except OSError:
            rpc_port = None
    if rpc_port: node["port"] = int(rpc_port)
    else: node.pop("port", None)
    node.pop("status", None); node.pop("last_seen", None)
    node["app_version"] = COORD_VERSION
    body = json.dumps({"action": "register", "node": node}).encode("utf-8")
    req = urllib.request.Request(url_base + "/fleet", data=body,
                                 headers={"Content-Type": "application/json", **_auth_headers()})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return False, f"the authority at {url_base} refused (403): registration needs the admin role / the fleet PIN (GENGHIS_TOKEN)"
        return False, f"the authority at {url_base} answered HTTP {e.code}"
    except Exception as e:
        return False, f"could not reach the authority at {url_base}: {e}"
    ok = bool(res.get("ok")); note = res.get("note", "")
    if not quiet:
        print(("  registered with the fleet: " if ok else "  registration FAILED: ") + note)
    return ok, note


def register_cmd(args):
    """`register [--coord HOST[:PORT][,HOST2…]] [--port N]` — announce this box to the fleet authority (D33).
    A comma-separated --coord registers with several serve hosts (until D30 makes one authority the rule)."""
    rest = args.rest or []
    coord = _init_opt(rest, "coord"); port = _init_opt(rest, "port")
    coords = [c.strip() for c in coord.split(",")] if isinstance(coord, str) else [None]
    bad = 0
    for c in coords:
        ok, note = register_self(c, int(port) if isinstance(port, str) else None)
        bad += (not ok)
    if bad:
        sys.exit(1)


def init_cmd(args):
    """`genghis init` — generate THIS machine's own fleet.json + config.json (D22). Ships nothing of ours:
    every value is detected from the local host or discovered on the local LAN. Idempotent: an existing
    fleet.json is updated in place (this node refreshed, other donors kept) after a .bak backup.
    Options (after `init`):  --node-name NAME  --coord HOST[:PORT]  --models-dir DIR  --role ROLE
                             --dir DIR (write target, default alongside the coordinator)  --yes (no prompts)"""
    rest = args.rest or []
    out_dir = _init_opt(rest, "dir") or HERE
    fleet_path = os.path.join(out_dir, "fleet.json")
    config_path = os.path.join(out_dir, "config.json")
    interactive = sys.stdin.isatty() and not _init_opt(rest, "yes")

    def ask(prompt, default):
        if not interactive:
            return default
        try:
            v = input(f"  {prompt} [{default}]: ").strip()
        except EOFError:
            return default
        return v or default

    print("== genghis init — generating YOUR fleet (nothing of ours ships; D22) ==")
    node_id   = _init_opt(rest, "node-name") or ask("name for THIS node", socket.gethostname().lower())
    models_dir = _init_opt(rest, "models-dir") or ask("models directory", os.environ.get("GENGHIS_MODELS_DIR", os.path.join(out_dir, "models")))
    role      = _init_opt(rest, "role") or "compute"
    coord     = _init_opt(rest, "coord") or ask("coordinator host (blank = this machine / localhost)", "")

    local = _detect_local_node(node_id, models_dir); local["role"] = role
    acc = local["accelerator"]; vram = local.get("vram_total_mb")
    print(f"  detected: {local['host']}  {local['arch']}  {local['cores']} cores  "
          f"{local['ram_total_mb']} MB RAM  accel={acc}" + (f" ({vram} MB VRAM)" if vram else ""))
    # D30 placement hint — the two questions: is this box always on? does it have a GPU?
    gpu = acc in ("cuda", "vulkan")
    if coord:
        print(f"  role: {'donor + inference host' if gpu else 'donor'} of the authority at {coord}"
              f"  (run `serve` here for a /v1 + Control Room of your own; it will read the fleet from {coord})")
    else:
        print("  role: this box will be the AUTHORITY (fleet + model library" + (" + home base: it has a GPU)" if gpu else ") — with no GPU, run /v1 on the GPU box with `serve --coord <this ip>`"))
        print("  (if a MORE reliable always-on box exists, make THAT the authority and re-run here with --coord <its ip>)")

    coord_host = (coord or "127.0.0.1").split(":")[0]
    coord_entry = {"id": "coordinator", "host": coord_host,
                   "ip": "127.0.0.1" if not coord else coord_host, "role": "coordinator",
                   "arch": local["arch"], "ram_total_mb": local["ram_total_mb"], "disk_gb": 0,
                   "notes": "control plane + model repository + telemetry ledger (set by genghis init)"}

    # Idempotent merge: keep any donors already present; refresh THIS node.
    if os.path.exists(fleet_path):
        try:
            existing = json.load(open(fleet_path, encoding="utf-8"))
            shutil.copyfile(fleet_path, fleet_path + ".bak")
            print("  existing fleet.json found -> backed up to fleet.json.bak")
        except Exception:
            existing = {}
    else:
        existing = {}
    donors = [d for d in existing.get("donors", []) if d.get("id") != node_id and not d.get("local")]

    # JOIN an existing fleet (D27): with --coord, seed this file from the authority's fleet.json so a fresh
    # client can pool with the donors the coordinator already knows (its own `serve` reads THIS local file).
    # If the authority already lists THIS host (matched by hostname), adopt that entry's id and keep its
    # RPC endpoint — that is what makes the box dual-role (local anchor here, RPC donor for everyone else).
    if coord:
        try:
            url = f"http://{coord if ':' in coord else coord + ':8899'}/fleet.json"
            with urllib.request.urlopen(urllib.request.Request(url, headers=_auth_headers()), timeout=6) as r:
                remote = json.loads(r.read().decode("utf-8"))
            me = [d for d in remote.get("donors", []) if (d.get("host") or "").lower() == local["host"]]
            if me:
                known = me[0]
                if known.get("id") != node_id and _init_opt(rest, "node-name") is None:
                    print(f"  the coordinator already knows this host as '{known['id']}' -> adopting that id")
                    node_id = known["id"]; local["id"] = node_id
                for k in ("ip", "port", "tokens_per_s_solo", "tps_ema", "reliability", "vram_total_mb"):
                    if known.get(k) is not None and not local.get(k):
                        local[k] = known[k]            # keep the endpoint + learned throughput the fleet has
            seeded = [d for d in remote.get("donors", [])
                      if (d.get("host") or "").lower() != local["host"] and d.get("id") not in {x.get("id") for x in donors}]
            donors += seeded
            for k in ("_retired_donors", "_pending_donors", "_eye_nodes"):
                if not existing.get(k) and remote.get(k):
                    existing[k] = remote[k]
            print(f"  joined fleet at {url}: {len(seeded)} other node(s) seeded")
        except Exception as e:
            print(f"  (could not fetch the coordinator's fleet from {coord}: {e} — writing this node only; re-run init later to join)")
    donors.insert(0, local)
    fleet = {
        "_schema": "GENGHIS fleet registry — generated by `genghis init`; git-ignored (never commit real topology).",
        "updated": datetime.date.today().isoformat(),
        "coordinator": existing.get("coordinator") if coord == "" and existing.get("coordinator") else coord_entry,
        "donors": donors,
        "_retired_donors": existing.get("_retired_donors", []),
        "_pending_donors": existing.get("_pending_donors", []),
        "_eye_nodes": existing.get("_eye_nodes", []),
    }

    # config.json — keep an existing one's human settings; only fill what's missing.
    cfg = {}
    if os.path.exists(config_path):
        try: cfg = json.load(open(config_path, encoding="utf-8"))
        except Exception: cfg = {}
    cfg.setdefault("_schema", "GENGHIS system config — HUMAN-set settings (genghis init / POST /config).")
    cfg.setdefault("default_goal", "fastest")
    if "model" not in cfg:
        found = sorted(glob.glob(os.path.join(models_dir, "*.gguf"))) if os.path.isdir(models_dir) else []
        cfg["model"] = found[0] if found else None
        if found:
            print(f"  found {len(found)} model(s) in {models_dir}; default -> {os.path.basename(found[0])}")
    for k in ("names", "roles", "hearth"):
        cfg.setdefault(k, {})

    if interactive:
        print(f"\n  about to write:\n    {fleet_path}\n    {config_path}")
        if ask("write these? (y/n)", "y").lower() not in ("y", "yes"):
            print("  aborted — nothing written."); return

    os.makedirs(out_dir, exist_ok=True)
    atomic_json_write(fleet_path, fleet)
    json.dump(cfg, open(config_path, "w", encoding="utf-8"), indent=2)
    print(f"\n  wrote {fleet_path}")
    print(f"  wrote {config_path}")
    if coord and out_dir == HERE:
        # D33: joining a fleet is two-way — we pulled its nodes above; now tell it about us.
        for c in [x.strip() for x in str(coord).split(",") if x.strip()]:
            register_self(c)
    print("  next: build/point llama.cpp, then `serve` on the coordinator. Add remote donors with")
    print("        `genghis discover` (mDNS) or by editing fleet.json. Nothing of ours is in these files.")


def _save_config(cfg):
    atomic_json_write(CONFIG, cfg)

def registry_cmd(args):
    """`registry`                      — list local models + the goal->model map
       `registry map <goal> <id>`      — set which model an effort goal uses
       `registry default <id>`         — set the fallback model (config 'model')
       `registry unmap <goal>`         — clear a goal's mapping (falls back to default)"""
    rest = args.rest or []
    sub = rest[0] if rest else "list"
    reg = build_registry()
    idx = {m["id"]: m for m in reg}
    cfg = load_config()

    def _resolve_id(token):
        if token in idx: return token
        hits = [i for i in idx if token.lower() in i.lower()]
        return hits[0] if len(hits) == 1 else (None if not hits else False)  # None=miss, False=ambiguous

    if sub in ("map", "unmap", "default") and len(rest) >= (2 if sub in ("unmap",) else (2 if sub == "default" else 3)):
        if sub == "map":
            goal, token = rest[1], rest[2]
            if goal not in GOALS: sys.exit(f"unknown goal '{goal}' (choose: {', '.join(GOALS)})")
            mid = _resolve_id(token)
            if not mid: sys.exit(f"no single model matches '{token}'")
            cfg.setdefault("goal_models", {})[goal] = mid
            _save_config(cfg); print(f"  {goal}  ->  {mid}")
        elif sub == "unmap":
            goal = rest[1]
            (cfg.get("goal_models") or {}).pop(goal, None); _save_config(cfg)
            print(f"  {goal}  ->  (default)")
        elif sub == "default":
            mid = _resolve_id(rest[1])
            if not mid: sys.exit(f"no single model matches '{rest[1]}'")
            cfg["model"] = idx[mid]["path"]; _save_config(cfg); print(f"  default model  ->  {mid}")
        return

    # list view
    gm = cfg.get("goal_models") or {}
    default_model = os.path.basename((cfg.get("model") or "").strip()) or "(none)"
    print(f"== model registry ({len(reg)} model(s) across {len(_model_dirs())} dir(s)) ==")
    if not reg:
        print("  no .gguf found. Put models in one of:"); [print("   ", d) for d in _model_dirs()]
    for m in reg:
        used = [g for g in GOALS if gm.get(g) == m["id"]]
        tag = f"  <- {', '.join(used)}" if used else ""
        vlm = "  [VLM +mmproj]" if m["kind"] == "vlm" else ""
        print(f"  {m['id']:44.44} {m['size_mb']:>7} MB{vlm}{tag}")
        # What this model can be ASKED to do (roles step 1) — read from the GGUF, printed next to it so a
        # goal mapped to a model that can't drive tools is visible at a glance instead of at 2 a.m.
        print(f"      {caps_summary(m.get('caps'))}")
    print("\n== goal -> model ==")
    for g in GOALS:
        mid = gm.get(g)
        where = mid if mid in idx else f"(default: {default_model})"
        print(f"  {g:9} -> {where}")
    print("\n  set with:  registry map <goal> <model>   |   registry default <model>")


def roles_cmd(args):
    """`roles`            — list the roles this host offers and what each would actually run
       `roles <id>`       — one role in full: its requirements, its belt, and every reason it can't run"""
    rest  = args.rest or []
    roles = load_roles(force=True)
    print(f"== roles ==  home: {home_dir()}")
    if not roles:
        print(f"  none found in {roles_dir()}")
        print("  A role is one .json file there. Set GENGHIS_HOME to keep your own roles outside this repo.")
        return
    if rest:
        rid = rest[0].strip().lower()
        if rid not in roles:
            sys.exit(f"no role '{rid}' (have: {', '.join(sorted(roles))})")
        r, res = roles[rid], resolve_role(rid)
        print(f"\n  {r['name']}  ({rid})")
        if r["description"]: print(f"    {r['description']}")
        print(f"    goal        {r['goal']}")
        print(f"    runs on     {res.get('model_id') or '— nothing on this host'}")
        if res.get("substituted"):
            s = res["substituted"]
            print(f"    substituted {s['from']} -> {s['to']}")
            print(f"                because {s['why']}")
        print(f"    requires    {json.dumps(r['requires']) if r['requires'] else '(nothing stated)'}")
        print(f"    tool belt   {', '.join(r['tools']) if r['tools'] else '(empty — you fill it)'}")
        if r["tools"]:
            _t, _notes, _owned = adapter_tools(r)
            for n in _notes:
                print(f"                {n}")
            print(f"                {len(_owned)} live tool(s): {', '.join(sorted(_owned)) or '(none)'}")
        if r["knowledge"]: print(f"    knowledge   {r['knowledge']}  (declared; retrieval not built yet)")
        if r["voice"]:     print(f"    voice       {r['voice']}  (declared; not wired yet)")
        for u in res.get("unmet") or []:  print(f"    ! {u}")
        if res.get("problem"):            print(f"    PROBLEM: {res['problem']}")
        print(f"    file        {r['source']}")
        return
    for rid in sorted(roles):
        res = resolve_role(rid)
        mark = "  " if not res.get("problem") else "! "
        print(f"  {mark}genghis-{rid:22.22} {role_why(res)}")
    print("\n  detail with:  roles <id>        call from any OpenAI client as:  genghis-<id>")


def adapters_cmd(args):
    """`adapters` — the adapters this home declares, whether each is armed, and whether it answers."""
    ads = load_adapters(force=True)
    print(f"== adapters ==  {adapters_dir()}")
    if not ads:
        print("  none declared. An adapter is one .json file there; see home.example/adapters/.")
        return
    for aid, a in sorted(ads.items()):
        if a["enabled"]:
            ok, detail = adapter_reachable(a)
            state = "ARMED  " if ok else "ENABLED"
            tail = detail if ok else f"but UNREACHABLE — {detail}"
        else:
            state, tail = "off    ", "disabled (set \"enabled\": true to arm it)"
        print(f"  {state} {aid:12.12} {a['transport']:16.16} {a['host']}:{a['port']}  {tail}")
        print(f"            operations: {', '.join(sorted((a['operations'] or {}).keys())) or '(none)'}")
    print()
    print("  An adapter is OFF until you arm it. Arming one lets a local model act on this machine.")


def fleet_cmd(args):
    """`fleet`                      — list active donors + retired + eye nodes
       `fleet retire <id>`          — move a node to the retired graveyard (reversible)
       `fleet restore <id>`         — bring a retired node back
       `fleet remove <id>`          — hard-delete a node (and its config names/roles)
       `fleet lend off|on [id]`     — keep this box's GPU for yourself / give it back (D35; default id = this box)
       `register [--coord HOST]`    — announce THIS box to the authority (D33; `init --coord` does it for you)
    Sends the change to the coordinator (the authority) so it persists fleet-wide (D26)."""
    rest = args.rest or []
    sub = rest[0] if rest else "list"
    if sub == "list":
        f = load_fleet()
        print("== active donors ==")
        for d in f.get("donors", []):
            print(f"  {d.get('id',''):16} {d.get('accelerator','?'):8} {d.get('status','?')}")
        ret = f.get("_retired_donors", [])
        if ret:
            print("== retired (restore with `fleet restore <id>`) ==")
            for d in ret: print(f"  {d.get('id',''):16} retired {d.get('retired_at','?')}")
        eyes = f.get("_eye_nodes", [])
        if eyes:
            print("== eye nodes ==")
            for d in eyes: print(f"  {d.get('id',''):16} {d.get('status','?')}")
        return
    if sub == "lend" and len(rest) >= 2 and rest[1] in ("on", "off"):
        # D35: `fleet lend off [id]` -- keep this box's GPU for yourself (Blender/Resolve); `lend on` gives it back.
        # Default id = THIS box's fleet entry.
        nid = rest[2] if len(rest) >= 3 else next((d.get("id") for d in load_fleet().get("donors", [])
                                                   if d.get("local") and is_self_node(d)), None)
        if not nid:
            sys.exit("  fleet lend: this box has no fleet entry yet -- run `init --coord <authority>` or give an id")
        sub, rest = f"lend-{rest[1]}", [None, nid]
    if sub in ("retire", "remove", "restore", "lend-off", "lend-on") and len(rest) >= 2:
        nid = rest[1]
        url = FLEET_URL.rsplit("/", 1)[0] + "/fleet"
        req = urllib.request.Request(url, data=json.dumps({"action": sub, "id": nid}).encode("utf-8"),
                                     headers={**_auth_headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                res = json.loads(r.read().decode("utf-8"))
            print("  " + ("done: " if res.get("ok") else "no-op: ") + str(res.get("note", "")))
        except urllib.error.HTTPError as e:
            sys.exit(f"  refused ({e.code}) — node lifecycle needs the admin role (D25)")
        except Exception as e:
            sys.exit(f"  failed to reach the coordinator at {url}: {e}")
        return
    sys.exit("usage: fleet [list | retire <id> | restore <id> | remove <id> | lend on|off [id]]")


def models_cmd(fleet):
    """`models` = list what the coordinator repo holds + what's cached locally;
    `models pull <name>` = fetch one from the repo into the local cache."""
    argv = sys.argv[2:]
    if argv and argv[0] == "pull":
        if len(argv) < 2:
            sys.exit("usage: models pull <name.gguf>")
        name = argv[1]
        url = model_repo_base() + "/" + urllib.parse.quote(name)
        os.makedirs(MODELS_DIR, exist_ok=True)
        dest = os.path.join(MODELS_DIR, name)
        print(f"pulling {name} from {url}")
        try:
            _download(url, dest)
        except Exception as e:
            sys.exit(f"pull failed: {e}")
        print(f"cached at {dest} ({os.path.getsize(dest)/1e6:.0f} MB)")
        return
    # list
    base = model_repo_base()
    print(f"== model repository: {base} ==")
    try:
        with urllib.request.urlopen(urllib.request.Request(base, headers=_auth_headers()), timeout=6) as r:
            repo = json.load(r)
        for m in repo.get("models", []):
            print(f"  {m['name']:<48} {m['size_mb']:>8.0f} MB")
        if not repo.get("models"):
            print("  (repository is empty — stage a .gguf in the coordinator's models/ dir)")
    except Exception as e:
        print(f"  (repo unreachable: {e})")
    local = list_local_models()
    if local:
        print(f"\n== local cache: {MODELS_DIR} ==")
        for n, mb in local:
            print(f"  {n:<48} {mb:>8.0f} MB")


def free_mem_mb(d):
    """Usable memory budget for a donor's shard, from LIVE free memory (headroom-adjusted).
    A GPU node that also runs its own serve REPORTS `vram_free_mb` (total minus what its warm resident holds);
    when that report is fresh it wins over the static total -- the 5090 holding a resident 32B is NOT 22 GB
    free, and planning as if it were put a second 32B on it over RPC at 1 tok/s (2026-09-14)."""
    held = _shard_held_mb(d) if not (d.get("local") and is_self_node(d)) else 0.0   # D39: shards parked here by a pooled resident
    if d.get("accelerator") in ("cuda", "vulkan"):
        total = (d.get("vram_total_mb") or 0) * 0.92 - held  # leave VRAM for CUDA context
        vf = d.get("vram_free_mb")
        # The live report describes what an RPC client would find on that card. For THIS host's OWN anchor
        # the resident is reusable or swappable (ensure_resident), so its budget stays the total -- otherwise a
        # host reads its own resident as "card full", plans around itself and pools over the network (seen 2026-09-14).
        if vf is not None and _report_fresh(d) and not (d.get("local") and is_self_node(d)):
            return max(0.0, min(total, float(vf) - held))
        return max(0.0, total)
    return max(0.0, (d.get("ram_free_mb") or d.get("ram_total_mb") or 0) * 0.85 - held)


def _shard_held_mb(d, max_age_s=900):
    """MB of this node's memory currently holding shards of other hosts' POOLED residents (D39), fresh only."""
    out = 0.0
    for who, h in (d.get("shard_held") or {}).items() if isinstance(d.get("shard_held"), dict) else []:
        try:
            if (datetime.datetime.now() - datetime.datetime.fromisoformat(h.get("ts") or "")).total_seconds() < max_age_s:
                out += float(h.get("mb") or 0)
        except Exception:
            pass
    return out


def _report_fresh(d, max_age_s=900):
    """True if the node's last self-report is recent enough to trust over static capacity."""
    try:
        t = datetime.datetime.fromisoformat(d.get("last_reported") or "")
        return (datetime.datetime.now() - t).total_seconds() < max_age_s
    except Exception:
        return False


def best_case_mem_mb(d):
    """Best-case budget if the donor were fully idle (uses TOTAL, not live-free RAM). Used only to tell
    a *transient* capacity shortfall (a busy donor / a prior run's shard not yet freed) from a model that
    genuinely won't fit even when the fleet is idle."""
    if d.get("accelerator") in ("cuda", "vulkan"):
        return (d.get("vram_total_mb") or 0) * 0.92
    return (d.get("ram_total_mb") or d.get("ram_free_mb") or 0) * 0.85


def net_discount(d):
    """Network penalty on a donor's usable throughput (round-trip tax). LAN => ~1.0."""
    lat = d.get("latency_ms_to_orchestrator") or 1
    return 1.0 / (1.0 + lat / 50.0)


# ---------------------------------------------------------------------------
# v0.3 (3/3) — self-tuning throughput: EMA of REAL observed t/s, not just one calibration
# ---------------------------------------------------------------------------
TPS_ALPHA = 0.3   # EMA weight for a fresh observation (heavier than reliability's 0.2: t/s obs are informative & rarer)


def donor_tps(d):
    """Best throughput estimate for planning: the self-tuned live EMA if we have one,
    else the one-shot calibration baseline. (Both are SOLO/single-node numbers — honest to compare.)"""
    return d.get("tps_ema") or d.get("tokens_per_s_solo") or 0.1


def observe_tps(d, measured, fleet, persist=True):
    """Fold a REAL single-node throughput measurement into the donor's EMA (self-tuning).
    Only single-node runs give an honest per-node signal — a pipeline (split) run yields ONE
    aggregate ~= its slowest stage, which can't be attributed to individual nodes, so we don't."""
    if not measured or measured <= 0:
        return
    prev = d.get("tps_ema") or d.get("tokens_per_s_solo") or measured
    d["tps_ema"] = round((1 - TPS_ALPHA) * prev + TPS_ALPHA * measured, 2)
    if persist:
        save_fleet(fleet)
    push_report(d.get("id"), {"tps_ema": d["tps_ema"]})   # persist the learning to the single source of truth


def eff_throughput(d):
    """The single weighted 'donor score' for allocation: throughput discounted by network cost.
    Throughput = self-tuned EMA if available (v0.3), else the calibration solo."""
    return donor_tps(d) * net_discount(d)


# --- D41: the placement rule (2026-09-18) --------------------------------------------------------------------
# Tonight the 5090 had 861 MB free (its own 32B warm), so the wired GPUs held 31.8 of the 70B's 41.7 GB -- and the
# planner greedily added EVERYTHING by score until the +8 % margin cleared: the laptop for a 0.8 GB slice, two
# Wi-Fi Pis (163 / 306 ms) and a 0.1 t/s node. It streamed for eight minutes and died. The rule now: prefer the
# fewest, fastest nodes with margin; reach for slow-link / CPU nodes only when the wired GPUs cannot cover it;
# never take a slice too small to be worth a hop; and SAY what was reached for and what would fix it.
FAST_LINK_MS   = 25.0    # measured round-trip at or under this = wired-class; the Wi-Fi Pis measure 150-300 ms
MIN_SLICE_FRAC = 0.03    # a share under 3 % of the model (and under 1 GB) is a pipeline hop for nothing

def same_box(d):
    """A donor that lives on THIS box (an eGPU, a second card) but is dialled over RPC: same IP as our own anchor and not
    ourselves. Over loopback there is no wire, so it is local-class for planning and can hold a warm server (D46)."""
    if not d or is_self_node(d):
        return False
    try:
        me = next((x for x in load_fleet().get("donors", []) if x.get("local") and is_self_node(x)), None)
    except Exception:
        me = None
    return bool(me and d.get("ip") and d.get("ip") == me.get("ip"))

def node_lat_ms(d):
    return 0.0 if (d.get("local") and is_self_node(d)) else float(d.get("latency_ms_to_orchestrator") or 0.0)

def node_tier(d):
    """0 = GPU on a wired-class link (or this host's own card) · 1 = GPU on a slow link · 2 = CPU, wired-class ·
    3 = CPU on a slow link. The link is judged by the MEASURED round-trip, never by the self-described
    `link_type` (which reads 'confirm' / 'local' on nodes that ping at 300 ms)."""
    gpu = d.get("accelerator") in ("cuda", "vulkan")
    return (0 if gpu else 2) + (1 if node_lat_ms(d) > FAST_LINK_MS else 0)

def tier_word(d):
    return ("wired GPU", "slow-link GPU", "wired CPU", "slow-link CPU")[node_tier(d)]

def min_slice_mb(need):
    return min(1024.0, need * MIN_SLICE_FRAC)

def token_costs(chosen):
    """Relative per-token cost of each node in a split: its share of the layers at its own pace, plus its
    round-trip (a pooled run crosses every boundary on every token). Relative only -- the absolute pace of an
    RPC pipeline is not derivable from these numbers (measured: a wired 3-GPU 70B does ~2 t/s), but WHICH node
    dominates is, and that is what the user needs to hear."""
    cap = sum(free_mem_mb(d) for d in chosen) or 1.0
    costs = {}
    for d in chosen:
        share = free_mem_mb(d) / cap
        costs[d["id"]] = share / max(0.1, donor_tps(d)) + node_lat_ms(d) / 1000.0
    total = sum(costs.values()) or 1.0
    return {k: v / total for k, v in costs.items()}

def reach_report(chosen, ranked, need):
    """The D41 narrative for a split: what slow-link/CPU nodes were reached for, what the wired GPUs hold, and --
    when a wired GPU is nearly full because it holds a warm model -- the one action that would make the reach
    unnecessary. Returns (reach_ids, text, hint). A fleet with no wired GPU at all reaches for nothing: it is
    simply what it is."""
    reach = [d for d in chosen if node_tier(d) > 0]
    fast = [d for d in ranked if node_tier(d) == 0]
    if not reach or not fast:
        return [], "", ""
    fast_cap = sum(free_mem_mb(d) for d in fast)
    kinds = sorted({tier_word(d) for d in reach}, key=lambda w: ("slow-link GPU", "wired CPU", "slow-link CPU").index(w))
    text = (f"Reached for {' + '.join(kinds)} nodes ({', '.join(d['id'] for d in reach)}): "
            f"the wired GPUs hold {fast_cap/1024:.1f} GB of the {need/1024:.1f} GB needed.")
    hint = ""
    for d in sorted(fast, key=lambda d: -best_case_mem_mb(d)):
        card, free = best_case_mem_mb(d), free_mem_mb(d)
        if card - free > 0.5 * card and d.get("warm"):
            would = fast_cap - free + card
            hint = (f"{d['id']} has {free/1024:.1f} GB of {card/1024:.1f} GB free -- it holds "
                    f"{', '.join(_tiny_model(m) for m in d['warm'])} warm; unload that and the wired GPUs hold "
                    f"{would/1024:.1f} GB" + (", enough on their own." if would >= need else "."))
            break
    return [d["id"] for d in reach], text, hint

def _why(ranked, chosen, need, goal, split, fits, tiny=None, costs=None, reach=None):
    """v0.3 (2/3) — true leave-one-out marginal analysis. For each node, compute the marginal
    contribution to the ACTIVE objective (fit) by comparing the plan WITH vs WITHOUT it:
      IN  + removing it breaks the fit -> load-bearing (its capacity is essential)
      IN  + plan still fits without it -> marginal / drop-safe
      idle-> what adding it WOULD cost: unneeded capacity + a throughput bottleneck ratio.
    This replaces v0.3's earlier fixed WHY labels with computed, per-node reasoning."""
    per = {}
    chosen_ids = {d["id"] for d in chosen}
    cap = sum(free_mem_mb(d) for d in chosen)
    min_in_tps = min((donor_tps(d) for d in chosen), default=0.1)   # slowest in-plan node = the pipeline pace
    for d in ranked:
        f = free_mem_mb(d)
        if d["id"] in chosen_ids:
            if not fits:
                per[d["id"]] = f"IN (+{f:.0f}MB pooled -- still short of {need:.0f}MB)"
            elif goal == "biggest":
                per[d["id"]] = f"IN (+{f:.0f}MB to the max-capacity pool)"
            elif not split:
                per[d["id"]] = f"IN -- holds the whole model ({f:.0f}MB free >= {need:.0f}MB needed)"
            else:
                without = cap - f                                   # leave-one-out capacity
                if without < need:
                    per[d["id"]] = f"IN -- load-bearing: drop it -> pool {without:.0f}<{need:.0f}MB, won't fit"
                else:
                    per[d["id"]] = f"IN -- marginal: +{f:.0f}MB, drop-safe (pool still {without:.0f}>={need:.0f}MB)"
                if costs and d["id"] in costs:                      # D41: who the pipeline waits on
                    per[d["id"]] += f"; ~{costs[d['id']]:.0%} of every token"
                if d.get("local") and is_self_node(d) and split:
                    per[d["id"]] += " -- this host's own card: no network for its share"
                if reach and d["id"] in reach:
                    per[d["id"]] += f" -- reached for ({tier_word(d)}, {node_lat_ms(d):.0f} ms)"
        elif tiny and d["id"] in tiny:
            per[d["id"]] = f"idle -- only {f:.0f}MB free: under {tiny[d['id']]:.0f}MB is too small a slice to be worth a hop"
        else:
            ratio = donor_tps(d) / min_in_tps if min_in_tps else 0
            bott = f", {ratio:.0%} of slowest in-plan -> would bottleneck" if ratio < 1 else ""
            per[d["id"]] = f"idle -- capacity already met; +{f:.0f}MB unneeded{bott}"
    return per


def plan_v3(donors, goal=None):
    """Objective-driven selection (v0.3). Returns split / nodes / per-node WHY / fits / pooled_mb.
    Run-types differ by how they trade speed vs capacity when picking WHO is in the plan:
      fastest/balanced/fit -> minimum highest-throughput set that fits (margin grows fastest<balanced<fit)
      biggest              -> pool ALL nodes (max capacity; the 'run a model no single node could' mode)."""
    goal = goal or active_goal()
    if not donors:
        return {"goal": goal, "split": None, "nodes": [], "per_node": {}, "pooled_mb": 0, "fits": False,
                "reason": "no live donors."}
    need = model_mem_mb()
    ranked = sorted(donors, key=eff_throughput, reverse=True)
    pooled = sum(free_mem_mb(d) for d in ranked)

    if goal == "biggest":
        chosen = [d for d in ranked if free_mem_mb(d) > 0]
        cap = sum(free_mem_mb(d) for d in chosen)
        fits = need <= cap
        return {"goal": goal, "split": (len(chosen) > 1) if fits else None,
                "nodes": [d["id"] for d in chosen], "pooled_mb": cap, "fits": fits,
                "per_node": _why(ranked, chosen, need, goal, len(chosen) > 1, fits),
                "reason": f"biggest: pool ALL {len(chosen)} nodes (~{cap:.0f}MB max capacity); "
                          f"this model ~{need:.0f}MB {'fits' if fits else 'does NOT fit even pooled'}."}

    margin = GOAL_MARGIN.get(goal, 1.08)
    target = need * margin
    # D9 addendum (2026-09-13): THIS host's own anchor wins whenever the model fits it — regardless of a
    # faster remote GPU. A local run has no network at all and, with residency, stays WARM (~1 s to
    # answer); a remote solo run re-streams the model over RPC on every call. D9 got this for free on the
    # laptop only because the 5090 also had the top throughput; the NUC's Arc does not, so `fastest` was
    # shipping a 1.5B to the 5060 Ti across the LAN instead of holding it resident. A remote node wins
    # only when the model does NOT fit locally — then capacity, not comfort, decides.
    mine = [d for d in ranked if d.get("local") and is_self_node(d)]
    # D46: a same-box donor (an eGPU on this host) is local-class -- no wire, and a warm server can hold the model on
    # it -- so the D9 "own card wins" rule does not apply against it: the faster card on this box wins.
    if mine and free_mem_mb(mine[0]) >= target and mine[0] is not ranked[0] and not (same_box(ranked[0]) and free_mem_mb(ranked[0]) >= target):
        a = mine[0]
        return {"goal": goal, "split": False, "nodes": [a["id"]], "pooled_mb": free_mem_mb(a), "fits": True,
                "per_node": _why(ranked, [a], need, goal, False, True),
                "reason": f"{goal}: model ~{need:.0f}MB fits THIS host's own {a.get('device', 'GPU')} — "
                          f"no network hop, stays warm (residency); {ranked[0]['id']} is faster per token but "
                          f"would re-stream the model on every call (D9)."}
    best = ranked[0]
    if free_mem_mb(best) >= target:
        return {"goal": goal, "split": False, "nodes": [best["id"]], "pooled_mb": free_mem_mb(best), "fits": True,
                "per_node": _why(ranked, [best], need, goal, False, True),
                "reason": f"{goal}: model ~{need:.0f}MB fits {best['id']} alone — no split (fewer hops = faster)."}

    # D41: a split takes nodes tier by tier -- wired GPUs first, then slow-link GPUs, wired CPUs, slow-link CPUs --
    # fastest first within a tier, and never a slice too small to be worth a hop. It stops the moment the
    # margin is met, so a slow-link/CPU node is in the plan ONLY when the wired GPUs could not cover the model.
    slice_min = min_slice_mb(need)
    tiny = {d["id"]: slice_min for d in ranked if 0 < free_mem_mb(d) < slice_min}
    usable = [d for d in ranked if d["id"] not in tiny and free_mem_mb(d) > 0]
    # This host's OWN card leads any split it hosts: its share crosses no network at all, and a host that proxies a
    # model living entirely on other cards while its own 16 GB sits empty is wrong (seen 2026-09-19: the NUC gave
    # itself 0.0 GB of a 32B it was warming). It is added first and never pruned.
    own = lambda d: bool(d.get("local")) and is_self_node(d)
    order = sorted(usable, key=lambda d: (0 if own(d) else 1, node_tier(d), -eff_throughput(d)))
    chosen, cap = [], 0.0
    for d in order:                        # add nodes, this card first, then best tier first, until capacity (×margin) is met
        chosen.append(d); cap += free_mem_mb(d)
        if cap >= target:
            break
    # LOO prune (v0.3): greedy can overshoot — the marginal analysis DRIVES the decision, not just reports it.
    # Drop drop-safe nodes worst-tier-first, then slowest: a node the pool doesn't NEED for capacity only adds
    # pipeline bottleneck (the D6 "starve the slow node" thesis, applied automatically).
    if cap >= target:
        for d in sorted(chosen, key=lambda d: (-node_tier(d), eff_throughput(d))):
            if own(d):
                continue                                     # never drop this host's own card from a split it hosts
            if len(chosen) > 1 and cap - free_mem_mb(d) >= target:
                chosen.remove(d); cap -= free_mem_mb(d)
    chosen = [d for d in ranked if d in chosen]               # keep the throughput order for devices / tensor-split
    fits = cap >= need
    costs = token_costs(chosen) if fits and len(chosen) > 1 else {}
    reach, reach_text, hint = reach_report(chosen, ranked, need) if fits and len(chosen) > 1 else ([], "", "")
    per = _why(ranked, chosen if fits else ranked, need, goal, len(chosen) > 1, fits, tiny=tiny, costs=costs, reach=reach)
    dom = max(costs.items(), key=lambda kv: kv[1]) if costs else None
    reason = (f"{goal}: pool min fast set {[d['id'] for d in chosen]} (~{cap:.0f}MB) to fit "
              f"~{need:.0f}MB{' (+'+str(round((margin-1)*100))+'% margin)' if margin>1.03 else ''}." if fits
              else f"{goal}: model ~{need:.0f}MB EXCEEDS pooled capacity (~{pooled:.0f}MB) — won't fit.")
    if reach_text:
        reason += " " + reach_text
    if dom and dom[1] >= 0.5:
        dd = next(d for d in chosen if d["id"] == dom[0])
        reason += (f" {dom[0]} alone accounts for ~{dom[1]:.0%} of every token "
                   f"({free_mem_mb(dd)/1024:.1f} GB at {donor_tps(dd):.1f} t/s, {node_lat_ms(dd):.0f} ms away).")
    if hint:
        reason += " " + hint
    # Under a SPEED goal, a plan that fits only by handing most of every token to a slow-link CPU node is not a
    # plan -- it is an eight-minute stream that answers at reading pace or dies. Refuse it with the fix in hand;
    # the capacity goals (`fit`, `biggest`) still take it. Only when wired GPUs are in the plan: a fleet that IS
    # all Pis is never refused for being what it is.
    refused = ""
    if (fits and goal in ("fastest", "balanced") and dom and dom[1] >= 0.5 and reach
            and any(node_tier(d) == 0 for d in chosen)
            and node_tier(next(d for d in chosen if d["id"] == dom[0])) == 3):
        fits = False
        refused = reason = (f"{goal}: won't run sensibly -- it fits only by reaching for {', '.join(reach)}, and {dom[0]} would carry "
                  f"~{dom[1]:.0%} of every token ({node_lat_ms(next(d for d in chosen if d['id'] == dom[0])):.0f} ms away). "
                  + (hint + " " if hint else "") + "Or choose `fit` to run it anyway, at that pace.")
    return {"goal": goal, "split": (len(chosen) > 1) if fits else None,
            "nodes": [d["id"] for d in chosen] if fits else [d["id"] for d in ranked],
            "pooled_mb": cap, "fits": fits, "reach": reach, "hint": hint, "refused": refused,
            "dominant": {"id": dom[0], "share": round(dom[1], 2)} if dom else None,
            "per_node": per, "reason": reason}


def plan_v2(donors):
    """Compat shim → the v0.3 planner using the active GOAL run-type."""
    return plan_v3(donors, active_goal())


def decide(fleet):
    donors = live_donors(fleet)
    if any(d.get("tokens_per_s_solo") is None for d in donors):
        donors = calibrate(fleet)
    ranked = sorted(donors, key=eff_throughput, reverse=True)
    print(f"== Donor scores (goal={active_goal()}; score = tok/s x network discount) ==")
    for d in ranked:
        tsrc = "ema" if d.get("tps_ema") else "solo"          # self-tuned live EMA vs one-shot calibration
        print(f"  {d['id']:<12} score={eff_throughput(d):6.1f}  "
              f"({tsrc} {donor_tps(d):.1f} t/s, free ~{free_mem_mb(d):.0f} MB)")
    dec = plan_v3(donors, active_goal())
    print(f"\n== DECISION [{active_goal()}] for {os.path.basename(active_model())} (~{model_mem_mb():.0f} MB) ==")
    if not dec["fits"] and dec.get("reach") and dec.get("pooled_mb", 0) >= model_mem_mb():
        verdict = "WON'T RUN SENSIBLY under this goal (fits only by reaching)"     # D41
    elif not dec["fits"]:
        verdict = "WON'T FIT (even pooled)"
    elif dec["split"]:
        verdict = "SPLIT across " + ", ".join(dec["nodes"])
    else:
        verdict = "RUN SOLO on " + dec["nodes"][0]
    print(f"  -> {verdict}")
    print(f"     {dec['reason']}")
    print("  per-node (why in / out):")
    for d in ranked:
        print(f"     {d['id']:<12} {dec['per_node'].get(d['id'], '')}")
    return dec


def build_devices(chosen):
    """Map an ordered node list -> (rpc_list, devices) for llama-cli (M16/D9).
    A LOCAL node (the orchestrator's own GPU) uses its CUDA device directly with NO RPC endpoint
    (latency 0); remote donors are dialed over RPC and named RPC0,RPC1,... in the order they appear
    in --rpc. The device list mirrors `chosen`'s order, so a per-node tensor-split lines up 1:1."""
    rpc_list, devices, k = [], [], 0
    for d in chosen:
        if d.get("local") and is_self_node(d):
            devices.append(d.get("device", "CUDA0"))   # only THIS host's anchor is a real local device (CUDA0 / Vulkan0)
        else:
            rpc_list.append(f"{d['ip']}:{d['port']}")   # everything else is dialed over RPC (heartbeat already excluded foreign anchors)
            devices.append(f"RPC{k}"); k += 1
    return rpc_list, devices


def run_llama(rpc_list, devices, tensor_split=None, n=N_PREDICT, prompt=PROMPT):
    """Invoke llama-cli across the given devices; return (gen_tps, prompt_tps, ok).
    rpc_list may be empty — a pure-local run on the anchor GPU passes no --rpc at all."""
    args = [LLAMA_CLI, "-m", ensure_model()]   # laptop: local path; bare-name client: fetch-if-missing from the Pi repo
    if rpc_list:
        args += ["--rpc", ",".join(rpc_list)]
    args += ["--device", ",".join(devices),
             "-ngl", "99", "-c", str(N_CTX), "-n", str(n), "--single-turn", "-p", prompt]
    if tensor_split:
        args += ["--tensor-split", ",".join(f"{w:.4f}" for w in tensor_split)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=1800)  # big models stream ~tens of GB to donors — allow 30 min
    blob = (proc.stdout or "") + (proc.stderr or "")
    gen = GEN_RE.search(blob)
    pp  = PP_RE.search(blob)
    return (float(gen.group(1)) if gen else None,
            float(pp.group(1)) if pp else None,
            gen is not None)


# --- OpenAI /v1 inference path (D17 / INTEGRATION.md) --------------------------------------------
# llama-cli (this build) prints a startup banner and ECHOES the conversation prompt to stdout, then
# the generation, then an ANSI-colored perf/exit epilogue. So the robust delimiter is the prompt's
# trailing "assistant:" marker: the completion is everything AFTER it, up to the epilogue. We strip
# ANSI codes and stop at the first escape/marker that follows the answer.
_V1_ANCHOR = "<|im_start|>assistant"                        # our ChatML prompt ends with this tag
# Epilogue/leak markers: the CLI banner epilogue PLUS any ChatML/role token — if the model ever
# runs past its turn, cutting at these kills the "user:/assistant:" scaffold leak (D17 templating).
_V1_STOP   = ("<|im_end|>", "<|im_start|>", "[ Prompt:", "Exiting...", "> EOF by user", "[end of text]")

_SYS_DEFAULT = "You are GENGHIS, a helpful AI assistant running on a pooled compute fabric."

def _chatml(messages):
    """Render OpenAI messages as Qwen2.5 ChatML so the model behaves as a chat model and STOPS at
    <|im_end|> (fixes the raw 'user:/assistant:' scaffold leaking + compounding into replies). A system
    message is guaranteed (Open WebUI may or may not send one). Proper per-model templates come later;
    ChatML covers the Qwen/Llama-Instruct GGUFs currently in the repo."""
    rendered, has_sys = [], False
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):                        # OpenAI content-parts -> join text pieces
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role == "tool":                                   # D37: a tool result on the per-request path -- Qwen ChatML's tool turn
            content = f"<tool_response>\n{content}\n</tool_response>"
        elif role == "assistant" and m.get("tool_calls") and not content:
            content = "\n".join("<tool_call>\n" + json.dumps({"name": (tc.get("function") or {}).get("name"),
                                 "arguments": (tc.get("function") or {}).get("arguments")}) + "\n</tool_call>" for tc in m["tool_calls"])
        if role == "system":
            has_sys = True
        rendered.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")
    if not has_sys:
        rendered.insert(0, f"<|im_start|>system\n{_SYS_DEFAULT}<|im_end|>\n")
    return "".join(rendered) + "<|im_start|>assistant\n"
_ANSI      = re.compile(r"\x1b\[[0-9;]*m")                  # ANSI color codes (stripped everywhere)


def _v1_args(rpc_list, devices, tensor_split, n, prompt):
    args = [LLAMA_CLI, "-m", ensure_model()]
    if rpc_list:
        args += ["--rpc", ",".join(rpc_list)]
    args += ["--device", ",".join(devices), "-ngl", "99", "-c", str(N_CTX), "-n", str(n),
             "--single-turn", "--simple-io", "--log-disable", "-p", prompt]
    if tensor_split:
        args += ["--tensor-split", ",".join(f"{w:.4f}" for w in tensor_split)]
    return args


def _plan_or_pooled(goal):
    """D39: a model kept warm ACROSS the fabric is found BEFORE planning. Its donors are busy holding its shards
    (ggml-rpc-server serves one client), so a fresh plan would see them as unavailable and give up on the very
    model that is ready to answer. Returns a pseudo-plan (no RPC, no devices: ensure_resident reuses the warm
    pooled entry by model) or the ordinary plan."""
    try:
        e = _pool_alive().get(model_path())
        if residency_enabled() and e and e.get("shards") and not e.get("escalated"):
            return [], [], None, list(e.get("nodes") or [])    # an ESCALATION (D43) is not a chosen placement: plan normally;
                                                               # ensure_resident keeps or drops it by the conversation's size
    except Exception:
        pass
    return _plan_for(load_fleet(), goal)


def no_plan_reason(default="the fleet had no live donors or the model exceeds capacity"):
    """What to tell the user when _plan_for returned None: the planner's own refusal if it made one, else the default."""
    r = _req_get("last_refusal", "")
    return f"[genghis] {r}" if r else f"[genghis] no completion — {default}."
def _plan_for(fleet, goal):
    """Shared planner for the /v1 path: heartbeat -> plan_with_settle -> build_devices -> capacity
    split, exactly as run_decision. Returns (rpc_list, devices, weights, node_ids) or None if the
    fleet has no live donors / the model exceeds capacity."""
    set_active_goal(goal)
    heartbeat(fleet)                                     # plan over the living
    _req_set("last_refusal", "")
    if not live_donors(fleet):
        return None
    fleet, dec = plan_with_settle(fleet)
    _req_set("last_dec", dec)                            # D41: the ghost (dry_run_plan) reads the reach narrative from it
    if dec is None:                                      # model exceeds fleet capacity
        return None
    by_id = {d["id"]: d for d in live_donors(fleet)}
    chosen = [by_id[i] for i in dec["nodes"] if i in by_id]
    rpc_list, devices = build_devices(chosen)
    weights = None
    if dec["split"]:
        w = [free_mem_mb(d) for d in chosen]; s = sum(w) or 1
        weights = [x / s for x in w]                     # capacity-weighted (D6), same as run_decision
    return rpc_list, devices, weights, [d["id"] for d in chosen]


def _v1_trim(txt):
    """Strip ANSI + cut at the first epilogue marker; return the clean completion."""
    txt = _ANSI.sub("", txt)
    cut = -1
    for m in _V1_STOP:
        j = txt.find(m)
        if j != -1:
            cut = j if cut == -1 else min(cut, j)
    if cut != -1:
        txt = txt[:cut]
    return txt.strip()


def run_llama_capture(rpc_list, devices, tensor_split=None, n=N_PREDICT, prompt=PROMPT):
    """Return (text, ok): the model's completion (non-streaming /v1). Anchor on the echoed prompt."""
    try:
        proc = subprocess.run(_v1_args(rpc_list, devices, tensor_split, n, prompt),
                              capture_output=True, text=True, timeout=1800,
                              stdin=subprocess.DEVNULL)   # ChatML -> conversation mode; EOF + --single-turn = exit after one turn
    except subprocess.TimeoutExpired:
        return ("", False)
    out = _ANSI.sub("", proc.stdout or "")
    k = out.find(prompt)                                 # everything after the echoed prompt is the answer
    if k != -1:
        out = out[k + len(prompt):]
    else:                                                # fallback: after the last "assistant:" marker
        a = out.rfind(_V1_ANCHOR)
        if a != -1:
            out = out[a + len(_V1_ANCHOR):]
    txt = _v1_trim(out)
    return (txt, bool(txt) and proc.returncode == 0)


def run_llama_stream(rpc_list, devices, tensor_split=None, n=N_PREDICT, prompt=PROMPT, on_late_perf=None):
    """Yield the completion incrementally as llama-cli produces it (streaming /v1, slice 2).
    First swallow the banner + echoed prompt up to the "assistant:" anchor, then stream the answer
    (ANSI-stripped), holding back a short tail and stopping at the epilogue's first ANSI/marker."""
    proc = subprocess.Popen(_v1_args(rpc_list, devices, tensor_split, n, prompt),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            stdin=subprocess.DEVNULL)   # ChatML -> conversation mode; EOF + --single-turn = exit after one turn
    run_llama_stream.last_perf = None                   # set from the epilogue when the answer completes
    run_llama_stream.current_proc = proc                # so an abandoned stream can kill the engine at once
    late_owner = False                                  # True -> a background thread owns the process's end
    started = False
    need = max(1, prompt.count(_V1_ANCHOR))   # assistant tags in the prompt echo to skip before the answer
    head = ""            # pre-answer buffer (banner + prompt echo) — discarded
    pending = ""         # confirmed answer text, awaiting emit
    HOLD = 24            # tail held back so a split marker/escape is caught
    try:
        while True:
            chunk = proc.stdout.read(8)
            if not chunk:
                break
            if not started:
                head += chunk
                # Anchor on the END of the echoed prompt, not the first assistant tag: in a multi-turn
                # chat the prompt itself contains earlier <|im_start|>assistant turns, and anchoring on
                # the first one streamed the OLD assistant message back as the answer.
                # Deterministic: the prompt echo contains exactly `need` assistant tags (one per prior
                # assistant turn + the final open tag); the answer begins right after the need-th one.
                # (rfind on a still-growing buffer is racy — it fires on an OLD tag before the last arrives.)
                clean = _ANSI.sub("", head)
                pos, idx = 0, -1
                for _ in range(need):
                    idx = clean.find(_V1_ANCHOR, pos)
                    if idx == -1:
                        break
                    pos = idx + len(_V1_ANCHOR)
                if idx == -1:
                    continue
                started = True
                pending = clean[pos:].lstrip("\r\n")
                head = ""
            else:
                pending += chunk
            cut = -1                                      # stop at the epilogue (first ANSI escape / marker)
            for m in _V1_STOP:
                j = pending.find(m)
                if j != -1:
                    cut = j if cut == -1 else min(cut, j)
            if cut != -1:
                out = _ANSI.sub("", pending[:cut])
                if out:
                    yield out
                # The epilogue after the answer IS the measurement ("[ Prompt: X t/s | Generation: Y t/s ]").
                # Drain it (bounded) before terminating, so a streamed chat is not the one run we never time.
                epilogue = [pending[cut:]]
                def _drain():                                 # read in small pieces; over RPC the process
                    try:                                      # lingers on teardown long after the line is out
                        while True:
                            c = proc.stdout.read(64)
                            if not c: break
                            epilogue.append(c)
                    except Exception: pass
                t = threading.Thread(target=_drain, daemon=True); t.start()
                t_end = time.time() + 15.0
                while time.time() < t_end and not GEN_RE.search(_ANSI.sub("", "".join(epilogue))):
                    time.sleep(0.1)
                blob = _ANSI.sub("", "".join(epilogue))
                g, pp = GEN_RE.search(blob), PP_RE.search(blob)
                run_llama_stream.last_perf = {"gen_tok_s": float(g.group(1)) if g else None,
                                              "prompt_tok_s": float(pp.group(1)) if pp else None}
                if g is None:
                    # A big pooled run frees GBs of remote buffers BEFORE it prints its timing -- longer than the
                    # client should wait. Let the engine finish on its own (bounded) and hand the numbers to
                    # whoever asked via on_late_perf; only then terminate. Killing it here lost every 32B timing.
                    late_owner = True
                    def _late():
                        t.join(180.0)
                        b2 = _ANSI.sub("", "".join(epilogue))
                        g2, p2 = GEN_RE.search(b2), PP_RE.search(b2)
                        perf = {"gen_tok_s": float(g2.group(1)) if g2 else None,
                                "prompt_tok_s": float(p2.group(1)) if p2 else None}
                        run_llama_stream.last_perf = perf
                        try:
                            if on_late_perf: on_late_perf(perf)
                        except Exception: pass
                        try: proc.terminate()
                        except Exception: pass
                    threading.Thread(target=_late, daemon=True).start()
                pending = ""
                break
            if len(pending) > HOLD:
                emit, keep = pending[:-HOLD], pending[-HOLD:]
                k = emit.rfind("\x1b")
                if k != -1 and not _ANSI.match(emit, k):      # incomplete escape cut at the boundary —
                    keep, emit = emit[k:] + keep, emit[:k]    # hold it back so '\x1b[' never leaks
                out = _ANSI.sub("", emit)
                if out:
                    yield out
                pending = keep
    finally:
        if not late_owner:
            try: proc.stdout.close()
            except Exception: pass
            try: proc.terminate()
            except Exception: pass
        run_llama_stream.current_proc = None
    tail = _v1_trim(pending)
    if tail:
        yield tail


def generate(fleet, goal, prompt, n=N_PREDICT):
    """Plan + run ONE completion; return (text, ok, node_ids). Non-streaming /v1 core."""
    plan = _plan_for(fleet, goal)
    if plan is None:
        return ("", False, [])
    rpc_list, devices, weights, nodes = plan
    text, ok = run_llama_capture(rpc_list, devices, tensor_split=weights, n=n, prompt=prompt)
    return (text, ok, nodes)


# --- D23 slice 2: model residency (warm llama-server) --------------------------------------------------
# A resident llama-server keeps ONE model warm in VRAM and speaks OpenAI /v1 natively (it applies the GGUF's
# own chat template via --jinja — so proxying to it is both faster AND more correct than re-scraping llama-cli
# each call). GENGHIS stays the front door (registry, goal routing, planning); the resident does inference.
# Scope: SOLO-on-the-local-anchor runs (the fast path where the re-load cost is felt). Pooled/split runs keep
# the per-request llama-cli path. Swapping models stops the old resident and starts the new one.
_resident = {"proc": None, "model": None, "port": RESIDENT_PORT, "sig": None, "error": None}
# D38: a POOL of warm servers per host, keyed by model file. `_resident` is the ACTIVE one (every older
# touchpoint keeps reading it); the pool is what stays warm behind it. Each entry: proc, model, port, sig, ctx,
# mb (its VRAM footprint), last_used. Eviction is LRU and only when the card's budget is actually exceeded --
# the Arc holds a 14B and a 1.5B together (11 GB of 17), the 5090 a 32B and a 1.5B (22 of 24). Before this,
# one slot per host meant every `fastest` call evicted `fit` and every `fit` call reloaded it (2026-09-15).
_POOL = {}
_POOL_LOCK = threading.RLock()   # D45: pool MUTATION (start / stop / evict) is serialised; a load's health-wait is not
RESIDENT_LOG = os.path.join(HERE, "resident.log")

def _entry_for_base(base_url):
    """The pool entry a proxied request is talking to (by port), or None."""
    try:
        port = int(str(base_url).rsplit(":", 1)[1])
    except Exception:
        return None
    return next((e for e in _POOL.values() if e.get("port") == port), None)

class _inflight:
    """`with _inflight(base):` -- while a request is being answered by a warm server, that server is BUSY: it is
    neither evicted (LRU) nor stopped from under the chat (D45; the old one-run-at-a-time lock did this by accident)."""
    def __init__(self, base_url):
        self.e = _entry_for_base(base_url)
    def __enter__(self):
        if self.e is not None:
            self.e["inflight"] = self.e.get("inflight", 0) + 1
        return self
    def __exit__(self, *a):
        if self.e is not None:
            self.e["inflight"] = max(0, self.e.get("inflight", 0) - 1)
        return False   # llama-server's own stderr — the WHY when it won't come up
_RESIDENT_PORTS = list(range(RESIDENT_PORT, RESIDENT_PORT + 8))
POOL_LEDGER = os.path.join(HERE, "pool.json")      # the warm pool, written on every change: a serve restart ADOPTS it (D38.1)


class _Adopted:
    """A warm llama-server this serve did not start (a previous serve did) but now owns. Quacks like Popen for the
    pool: poll() is None while the server answers /health (and, on Linux, the pid is alive); terminate/kill end it."""
    def __init__(self, pid, port):
        self.pid, self.port, self.returncode = int(pid or 0), int(port), None
        self._t, self._ok = 0.0, True
    def poll(self):
        now = time.time()
        if now - self._t > 2.0:                       # health is an HTTP call: cache it briefly, the pool asks often
            ok = _resident_health(self.port)
            if ok and self.pid and platform.system() != "Windows":
                try: os.kill(self.pid, 0)
                except OSError: ok = False
            self._ok, self._t = ok, now
        if not self._ok and self.returncode is None:
            self.returncode = 1
        return None if self._ok else self.returncode
    def terminate(self):
        try:
            if platform.system() == "Windows":
                subprocess.run(["taskkill", "/PID", str(self.pid), "/F"], capture_output=True, timeout=10)
            else:
                os.kill(self.pid, 15)
        except Exception:
            pass
        self._t = 0.0
    kill = terminate
    def wait(self, timeout=None):
        end = time.time() + (timeout or 10)
        while time.time() < end and self.poll() is None:
            time.sleep(0.2)
        return self.returncode


def _pool_save():
    """Write the ledger: enough to re-adopt every warm server after a restart (model, port, pid, plan, shares)."""
    try:
        rows = []
        for e in _POOL.values():
            pr = e.get("proc")
            if pr is None or pr.poll() is not None:
                continue
            rows.append({"model": e["model"], "port": e["port"], "pid": getattr(pr, "pid", 0), "ctx": e.get("ctx"),
                         "mb": e.get("mb", 0), "total_mb": e.get("total_mb", e.get("mb", 0)), "shards": e.get("shards") or {},
                         "nodes": e.get("nodes") or [], "sig": e.get("sig"), "last_used": e.get("last_used", 0)})
        tmp = POOL_LEDGER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"written": datetime.datetime.now().isoformat(timespec="seconds"), "pool": rows}, f, indent=1)
        os.replace(tmp, POOL_LEDGER)
    except Exception as e:
        print(f"[resident] could not write {POOL_LEDGER}: {e}", flush=True)


def _server_model(port):
    """Which model file a llama-server on `port` is serving (its /props), or None."""
    try:
        with urllib.request.urlopen(f"http://{RESIDENT_HOST}:{port}/props", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d.get("model_path") or (d.get("default_generation_settings") or {}).get("model") or None
    except Exception:
        return None


def adopt_pool():
    """At serve start: re-attach to the warm servers a previous serve left running (the ledger says which ports,
    models, plans and shares). Until now every restart -- every deploy -- emptied the pool: three 8-minute 70B warm-ups
    in one afternoon (2026-09-18). Anything the ledger lists that no longer answers, or answers with a different
    model, is dropped; anything on a resident port that is NOT adopted is reaped by kill_stale_residents() next."""
    if os.environ.get("GENGHIS_POOL_KEEP", "1") in ("0", "false", "no"):
        return 0
    try:
        with open(POOL_LEDGER, encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("pool") or []
    except Exception:
        return 0
    n = 0
    for r in rows:
        port, model = int(r.get("port") or 0), r.get("model") or ""
        if not port or not model or not _resident_health(port):
            continue
        served = _server_model(port)
        if served and os.path.basename(served) != os.path.basename(model):
            print(f"[resident] port {port} serves {os.path.basename(served)}, not {os.path.basename(model)} -- not adopting", flush=True)
            continue
        entry = {"proc": _Adopted(r.get("pid"), port), "model": model, "sig": r.get("sig"), "port": port, "error": None,
                 "ctx": r.get("ctx"), "mb": r.get("mb", 0), "total_mb": r.get("total_mb", r.get("mb", 0)),
                 "shards": r.get("shards") or {}, "nodes": r.get("nodes") or [], "last_used": r.get("last_used", 0), "ready": True, "inflight": 0}
        _POOL[model] = entry; n += 1
        print(f"[resident] adopted warm {os.path.basename(model)} on :{port} (pid {r.get('pid')})"
              + (f" across {' + '.join(entry['nodes'])}" if entry["shards"] else ""), flush=True)
    if n:
        newest = max(_POOL.values(), key=lambda e: e.get("last_used", 0))
        _activate(newest)
        try: report_anchor_vram(_pool_held_mb())        # the authority's books (incl. donors' shards) come back at once
        except Exception: pass
    return n

# D39 pass 2: TRUE progress for a warm load. While ensure_resident waits for llama-server, this holds what the kernel
# has actually pushed to the donors (`ss -tin bytes_acked`, Linux) against what the plan says must cross the wire --
# the same measurement the per-request path narrates. Read by resident_status() (/registry.json, /loading.json),
# the /v1 Thinking panel ticks and the Control Room's loading stripe. Empty when nothing is loading.
_LOADING = {}
_LAST_LOAD = {}          # the measurement of the last finished load (the Control Room's "warmed" note quotes it)

def last_load_text():
    """'29.5 GB over the wire in 6 min 40 s (78 MB/s)' for the last measured load, else ''."""
    L = _LAST_LOAD
    if not L or not L.get("measured") or not L.get("sent_mb"):
        return ""
    el = int(L.get("elapsed_s") or 0)
    if L.get("cached"):
        return f"cache load from the donors' own disks, {L['sent_mb']/1024:.1f} GB of slivers over the wire, {el//60} min {el%60} s"
    return f"{L['sent_mb']/1024:.1f} GB over the wire in {el//60} min {el%60} s ({L.get('rate_mbs', 0):.0f} MB/s)"

def loading_status():
    """A copy of the live load, or None."""
    return dict(_LOADING) if _LOADING else None

def loading_text():
    """One line for a status tick: '6.4 / 29.5 GB (22%) · 80 MB/s · ~5 min left', or the honest fallback."""
    L = _LOADING
    if not L:
        return ""
    el = int(time.time() - L.get("started", time.time()))
    if L.get("phase") == "computing":
        return f"model delivered, computing the prompt ({el} s)"
    if L.get("phase") == "disk":
        return f"loading {L.get('model', '')} from disk ({el} s)"
    if L.get("cached"):
        crossed = f"{L.get('sent_mb', 0)/1024:.1f} GB crossed the wire" if L.get("measured") else "only the uncached slivers cross the wire"
        how = "the byte counter says the donors already hold" if L.get("cache_inferred") else "the donors already hold"
        return f"cache load: {how} {L.get('model', '')} -- they re-hash it from their own disk (about a minute per 20 GB); {crossed} ({el} s)"
    if not L.get("measured"):
        mins = max(1, int(L.get("remote_mb", 0) / 1024 / 4 + 0.5))
        return f"streaming ~{L.get('remote_mb', 0)/1024:.1f} GB to {', '.join(L.get('nodes') or [])} -- about {mins} min (estimate; a byte counter needs a Linux host) ({el} s)"
    eta = L.get("eta_s")
    eta_t = (f" · ~{int(eta/60)} min left" if eta and eta >= 90 else (f" · ~{int(eta)} s left" if eta else ""))
    return f"{L.get('sent_mb', 0)/1024:.1f} / {L.get('remote_mb', 0)/1024:.1f} GB ({L.get('pct', 0)}%) · {L.get('rate_mbs', 0):.0f} MB/s{eta_t}"

def _pool_alive():
    """Live pool entries (drop the dead ones as a side effect)."""
    for k in list(_POOL):
        e = _POOL[k]
        if e.get("proc") is None or e["proc"].poll() is not None:
            _POOL.pop(k, None)
    return _POOL

def _pool_held_mb():
    return sum(e.get("mb", 0) for e in _pool_alive().values())

def _activate(entry):
    """Make a pool entry the active resident (what /v1 proxies to)."""
    entry["last_used"] = time.time()
    _resident.clear(); _resident.update(entry)

def _free_port():
    used = {e["port"] for e in _pool_alive().values()}
    for pt in _RESIDENT_PORTS:
        if pt in used: continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
                sk.settimeout(0.3); sk.connect((RESIDENT_HOST, pt)); continue   # something answers -> taken
        except OSError:
            return pt
    return None

def _pool_evictions_for(model_file, n_ctx=None):
    """Which warm entries would have to go to fit `model_file` (LRU order)? Empty if it fits beside them."""
    if model_file in _pool_alive():
        return []
    try:
        if n_ctx is None:
            n_ctx, _ = resident_ctx(model_file, _local_anchor_free_mb())
        need_mb = os.path.getsize(model_file) / (1024 * 1024) + kv_cache_mb(n_ctx, model_file) + 512
    except Exception:
        return []
    budget = _local_anchor_free_mb(); held = _pool_held_mb(); out = []
    for e in sorted(_pool_alive().values(), key=lambda e: e.get("last_used", 0)):
        if held + need_mb <= budget:
            break
        out.append(e["model"]); held -= e.get("mb", 0)
    return out


def _stop_entry(entry, why=""):
    if entry.get("inflight", 0) > 0:                          # a chat is being answered by it: let it finish (bounded) --
        t0 = time.time()                                       # an eviction or a reclaim WAITS for the answer, never pulls the
        print(f"[resident] {os.path.basename(entry.get('model') or '?')} is answering {entry['inflight']} request(s) -- waiting for it before: {why}", flush=True)
        while entry.get("inflight", 0) > 0 and time.time() - t0 < 30:   # server out from under it (D45)
            time.sleep(0.5)
        if entry.get("inflight", 0) > 0:
            print(f"[resident] stopping {os.path.basename(entry.get('model') or '?')} with {entry['inflight']} answer(s) in flight ({why})", flush=True)
    p_ = entry.get("proc")
    if p_ and p_.poll() is None:
        try: p_.terminate(); p_.wait(timeout=10)
        except Exception:
            try: p_.kill()
            except Exception: pass
    _POOL.pop(entry.get("model"), None)
    _pool_save()
    if why: print(f"[resident] stopped {os.path.basename(entry.get('model') or '?')} ({why})", flush=True)

def unload_model(model_id, why="unloaded by the operator"):
    """Stop one warm server (a pool entry) by model id; the rest of the pool stays. Returns (ok, note)."""
    with _POOL_LOCK:
        entry = next((e for k, e in _pool_alive().items() if os.path.basename(k) == model_id or k == model_id), None)
        if entry is None:
            return False, f"{model_id} is not warm here"
        _stop_entry(entry, why)
    if _resident.get("model") == entry.get("model"):        # it was the active one: no active resident now
        _resident.update(proc=None, model=None, sig=None)
    try: report_anchor_vram(_pool_held_mb())
    except Exception: pass
    return True, f"unloaded {model_id}"

def gpu_access_problem(accel=None):
    """Linux: can THIS user open the GPU's device node? "" when yes (or when there is nothing to check: CPU host,
    Windows/macOS). The render-group bug (2026-09-18): a fresh Ubuntu box used only over SSH works until its first
    reboot, because /dev/dri/renderD* is root:render 0660 and the user only ever reached it through the desktop
    login's temporary ACL. After a reboot llama-server dies with `exited (code 1)` and nothing says why. This says
    why -- before the launch, in the Control Room, and in the watchdog's line."""
    if platform.system() != "Linux":
        return ""
    if accel is None:
        try:
            with open(FLEET, encoding="utf-8") as f:
                me = next((d for d in json.load(f).get("donors", []) if d.get("local") and is_self_node(d)), None)
            accel = (me or {}).get("accelerator")
        except Exception:
            accel = None
    if accel == "cuda":
        nodes = ["/dev/nvidiactl"] if os.path.exists("/dev/nvidiactl") else []
        what = "/dev/nvidiactl"
    elif accel == "vulkan":
        nodes = sorted(glob.glob("/dev/dri/renderD*"))
        what = "/dev/dri/renderD*"
    else:
        return ""
    if not nodes:
        return f"no {what} on this box -- is the GPU driver loaded?"
    if any(os.access(n, os.R_OK | os.W_OK) for n in nodes):
        return ""
    n = nodes[0]
    try:
        import grp, pwd, stat
        st = os.stat(n)
        group = grp.getgrgid(st.st_gid).gr_name
        owner = pwd.getpwuid(st.st_uid).pw_name
        mode = stat.filemode(st.st_mode)
        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        group, owner, mode, user = "render", "root", "?", os.environ.get("USER", "$USER")
    return (f"can't open the GPU: {n} is {owner}:{group} {mode} and {user} is not in '{group}' -- "
            f"sudo usermod -aG {group}{',video' if group == 'render' else ''} {user}, then log out and back in (or reboot)")

def residency_enabled():
    """On unless disabled, and only if the server binary exists. `GENGHIS_RESIDENCY=0` or config
    residency:false turns it off (falls back to the per-request llama-cli path)."""
    env = os.environ.get("GENGHIS_RESIDENCY")
    if env is not None:
        return env not in ("0", "false", "no", "")
    if load_config().get("residency") is False:
        return False
    return os.path.exists(LLAMA_SERVER)

def _resident_alive():
    p = _resident["proc"]
    return p is not None and p.poll() is None

def resident_status():
    """What model (if any) is warm right now — for the Control Room / registry view. `pool` lists EVERY warm
    server on this host (D38); `model` is the active one."""
    pool = [{"model": os.path.basename(e["model"]), "ctx": e.get("ctx"), "mb": int(e.get("mb", 0)), "port": e["port"], "escalated": bool(e.get("escalated")),
             "total_mb": int(e.get("total_mb") or e.get("mb", 0)), "nodes": list(e.get("nodes") or []),
             "shards": {k: int(v) for k, v in (e.get("shards") or {}).items()}}
            for e in sorted(_pool_alive().values(), key=lambda e: -e.get("last_used", 0))]
    return {"model": os.path.basename(_resident["model"]) if _resident.get("model") else None,
            "up": _resident_alive(), "enabled": residency_enabled(), "ctx": _resident.get("ctx"),
            "error": _resident.get("error"),          # last start failure, readable (Control Room shows it)
            "loading": loading_status(),              # D39 pass 2: the live load (measured bytes, rate, ETA) or None
            "pool": pool, "pool_mb": int(_pool_held_mb())}

def dry_run_plan(model_id, node=None, pooled=False):
    """D39 pass 1 -- the Control Room's drag preview: WHERE would `model_id` go, and does it fit? Read-only: the
    planner's globals are borrowed under the run lock and restored. `node` = a single host's card (drop on one bar);
    `pooled` = across the fabric (drop on the pool). Returns per-node MB so the picture IS the plan."""
    idx = registry_index()
    if model_id not in idx:
        return {"ok": False, "why": f"no model named {model_id}"}
    path = idx[model_id]["path"]
    fleet = load_fleet()
    donors = {d["id"]: d for d in fleet.get("donors", [])}
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        size_mb = idx[model_id].get("size_mb") or 0
    out = {"ok": True, "model": model_id, "size_mb": int(size_mb), "nodes": [], "fits": False, "why": ""}
    if node:                                                    # --- solo on one host's card ---
        d = donors.get(node)
        if not d:
            out["why"] = f"no node {node}"; return out
        card = best_case_mem_mb(d)
        held = 0.0
        if is_self_node(d):
            held = _pool_held_mb()
        else:
            try:
                reg = _remote_json(f"http://{d['ip']}:{int(d.get('serve_port') or 8899)}/registry.json", ttl=3.0, timeout=3)
                held = float(sum(e.get("mb", 0) for e in (reg.get("resident") or {}).get("pool") or []))
            except Exception:
                held = 0.0
        held += _shard_held_mb(d)
        ctx, _ = resident_ctx(path, max(0.0, card - held))
        need = size_mb + kv_cache_mb(ctx, path) + 512
        free = max(0.0, card - held)
        out.update(ctx=ctx, need_mb=int(need), nodes=[{"id": node, "mb": int(need), "free_mb": int(free), "card_mb": int(card)}],
                   fits=need <= free)
        out["why"] = (f"{_tiny_model(model_id)} fits on {node}: {need/1024:.1f} GB needed, {free/1024:.1f} GB free ({(free-need)/1024:.1f} GB left)"
                      if out["fits"] else
                      f"{_tiny_model(model_id)} does not fit on {node}: needs {need/1024:.1f} GB, {free/1024:.1f} GB free"
                      + (" (a pooled model is holding memory there)" if _shard_held_mb(d) > 0 else ""))
        return out
    # --- across the fabric: borrow the planner, then put everything back ---
    if not _V1_RUN_LOCK.acquire(timeout=2.0):
        out["why"] = "busy: a run is in progress -- try again in a moment"; out["busy"] = True; return out
    prev_model, prev_goal = active_model(), active_goal()
    try:
        set_active_model(path)
        plan = _plan_for(fleet, load_config().get("default_goal") or active_goal())
        if not plan:
            out["why"] = (_req_get("last_refusal", "") or f"{_tiny_model(model_id)} exceeds what the whole fleet can hold right now"); return out
        rpc_list, devices, weights, nodes = plan
        chosen = [donors[n] for n in nodes if n in donors]
        budget = sum(free_mem_mb(d) for d in chosen)
        ctx, _ = resident_ctx(path, budget)
        need = size_mb + kv_cache_mb(ctx, path) + 512
        w = list(weights) if weights else [1.0 / max(1, len(nodes))] * len(nodes)
        rows = []
        for d, wi in zip(chosen, w):
            rows.append({"id": d["id"], "mb": int(need * wi), "free_mb": int(free_mem_mb(d)), "card_mb": int(best_case_mem_mb(d))})
        out.update(ctx=ctx, need_mb=int(need), nodes=rows, pooled=bool(rpc_list), fits=True)
        dec = _req_get("last_dec") if isinstance(_req_get("last_dec"), dict) else {}
        out["reach"] = list(dec.get("reach") or []); out["hint"] = dec.get("hint") or ""
        out["warn"] = bool(out["reach"])                                    # D41: fits, but by reaching -- the ghost turns amber
        why = f"{_tiny_model(model_id)} across {' + '.join(nodes)}: {need/1024:.1f} GB in {len(nodes)} shares"
        if rpc_list and not out["reach"]:
            why += " -- warm, not fast: a pooled model does a few tokens/s"
        if out["reach"]:
            fast_gb = sum(free_mem_mb(d) for d in chosen if node_tier(d) == 0) / 1024
            kinds = sorted({tier_word(donors[i]) for i in out["reach"] if i in donors})
            why += (f" -- reaching for {', '.join(out['reach'])} ({' / '.join(kinds)}):"
                    f" the wired GPUs hold {fast_gb:.1f} GB of {need/1024:.1f}")
            dom = dec.get("dominant") or {}
            if dom.get("share", 0) >= 0.5:
                why += f"; {dom['id']} alone would be ~{dom['share']:.0%} of every token"
            if out["hint"]:
                why += ". " + out["hint"]
        out["why"] = why
        return out
    finally:
        set_active_model(prev_model)
        set_active_goal(prev_goal)
        _V1_RUN_LOCK.release()


def warm_model(model_id, pooled=False):
    """Explicitly pre-load a model into VRAM (Control Room 'Warm now'). Only SOLO-on-the-local-anchor models
    can be kept warm; one too big for a single node (needs pooling) can't. Returns (ok, note)."""
    if not residency_enabled():
        return False, "residency is off (GENGHIS_RESIDENCY=0 / config residency:false)"
    idx = registry_index()
    if model_id not in idx:
        return False, f"no model named {model_id}"
    set_active_model(idx[model_id]["path"])
    plan = _plan_for(load_fleet(), load_config().get("default_goal") or active_goal())
    if not plan:
        return False, (_req_get("last_refusal", "") or "no live donors / model exceeds capacity")
    rpc_list, devices, weights, nodes = plan
    if pooled and rpc_list and len(nodes) == 1 and all(str(x).startswith("RPC") for x in (devices or [])):
        # D44 follow-through: "across the fabric" but ONE remote card can hold it whole -- warming it from here would make
        # this host proxy a model living entirely on that card over RPC (9.7 GB of slivers, 2.5 min, every token on the
        # wire -- seen 2026-09-19). Warm it on that card's OWN serve instead: solo there, no network per token.
        tgt = next((d for d in load_fleet().get("donors", []) if d.get("id") == nodes[0]), None)
        if tgt and tgt.get("local") and tgt.get("ip"):
            try:
                body = json.dumps({"action": "load", "model": model_id}).encode("utf-8")
                req = urllib.request.Request(f"http://{tgt['ip']}:{int(tgt.get('serve_port') or 8899)}/residency", data=body,
                                             headers={"Content-Type": "application/json", **_auth_headers()})
                with urllib.request.urlopen(req, timeout=2400) as r:
                    res = json.loads(r.read().decode("utf-8"))
                return bool(res.get("ok")), f"{_tiny_model(model_id)} fits {nodes[0]} alone -- warmed there on its own card (no network per token): {res.get('note') or ''}"
            except Exception as e:
                return False, f"{_tiny_model(model_id)} fits {nodes[0]} alone, but its serve did not take the warm: {e}"
    if (rpc_list or len(nodes) != 1) and pooled:
        # D39: keep it warm ACROSS the fabric -- the shards stream to the donors once and stay there.
        base = ensure_resident(idx[model_id]["path"], rpc_list, devices, weights, plan_nodes=nodes)
        if base is None:
            return False, f"failed to start the pooled warm server for {_tiny_model(model_id)} -- see poc/resident.log"
        return True, (f"warmed {_tiny_model(model_id)} across {', '.join(nodes)} -- it stays there until you unload it"
                      + (f" ({last_load_text()})" if last_load_text() else ""))
    if rpc_list or len(nodes) != 1:
        # Say the REAL reason (D40): too big for THIS host's own card (name the numbers, and where it would fit), or
        # genuinely too big for any single card (pooled on demand). "needs pooling (1 nodes)" told nobody anything.
        need = model_mem_mb() + kv_cache_mb(16384, idx[model_id]["path"]) + 512
        me = next((d for d in load_fleet().get("donors", []) if is_self_node(d)), None)
        have = best_case_mem_mb(me) if me else 0
        fits_elsewhere = [d["id"] for d in load_fleet().get("donors", []) if d.get("local") and not is_self_node(d)
                          and d.get("status") == "up" and best_case_mem_mb(d) >= need]
        if len(nodes) == 1:
            where = f" -- it fits on {', '.join(fits_elsewhere)}: use that column" if fits_elsewhere else ""
            return False, (f"{_tiny_model(model_id)} is too big for this host's card (needs ~{need/1024:.0f} GB, "
                           f"{socket.gethostname().lower()} has ~{have/1024:.0f} GB usable){where}")
        return False, (f"{_tiny_model(model_id)} is too big for any single card here (needs ~{need/1024:.0f} GB) -- "
                       f"use 'Warm across the fabric' to keep it warm over {len(nodes)} nodes, or it runs pooled on demand")
    victims = [os.path.basename(v) for v in _pool_evictions_for(idx[model_id]["path"])]   # D38: say what has to go
    base = ensure_resident(idx[model_id]["path"], rpc_list, devices, weights, plan_nodes=nodes)
    if base is None:
        return False, "failed to start the warm server"
    return True, ("warmed" if not victims else f"warmed -- unloaded {', '.join(victims)} to make room on this card")

def _resident_health(port, timeout=1.0):
    try:
        with urllib.request.urlopen(f"http://{RESIDENT_HOST}:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def report_anchor_vram(held_mb=0):
    """Tell the authority how much of THIS host's own GPU is actually free (total*0.92 minus what the warm
    resident holds) -- the planner must not treat a card carrying a resident model as empty. Best-effort."""
    try:
        with open(FLEET, encoding="utf-8") as f:
            me = next((d for d in json.load(f).get("donors", []) if d.get("local") and is_self_node(d)), None)
        if not me or me.get("accelerator") not in ("cuda", "vulkan"):
            return
        total = (me.get("vram_total_mb") or 0) * 0.92
        alive = sorted(_pool_alive().values(), key=lambda e: -e.get("last_used", 0))
        warm = [os.path.basename(e["model"]) for e in alive]
        shards = {}                                                    # donor -> MB this host's pooled residents hold there
        pooled = {}                                                    # model -> [nodes] for pooled residents (D39)
        escalated = []                                                 # of those, the ones a long chat asked for (D43) -- not chosen
        for e in alive:
            for nid, mb in (e.get("shards") or {}).items():
                shards[nid] = shards.get(nid, 0) + int(mb)
            if e.get("shards"):
                pooled[os.path.basename(e["model"])] = list(e.get("nodes") or [])
                if e.get("escalated"):
                    escalated.append(os.path.basename(e["model"]))
        push_report(me.get("id"), {"vram_free_mb": int(max(0, total - held_mb)), "warm": warm,
                                   "shards": shards, "pooled": pooled, "escalated": escalated})
    except Exception:
        pass


def kill_stale_residents(port=None):
    """Kill any llama-server that is NOT ours but sits on our resident port. A serve that is restarted (or dies)
    leaves its warm server behind as an orphan; the next serve's fresh server then cannot bind, the orphan keeps
    answering with yesterday's settings, and the orphans pile up on the GPU (four on the 5090, 24/24 GB, models
    paging through system RAM -- 2026-09-15). Called at serve start and before every resident start. Returns the
    number killed."""
    ports = [int(port)] if port else _RESIDENT_PORTS
    mine_pids = {e["proc"].pid for e in _pool_alive().values()}
    if _resident.get("proc") and _resident["proc"].poll() is None: mine_pids.add(_resident["proc"].pid)
    killed = 0
    for port in ports:
      mine = None
      try:
        if platform.system() == "Windows":
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'llama-server.exe' -and $_.CommandLine -match '--port %d( |$)' } "
                  "| Select-Object -ExpandProperty ProcessId") % port
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20).stdout
            pids = [int(x) for x in out.split() if x.strip().isdigit()]
            for pid in pids:
                if pid in mine_pids: continue
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10); killed += 1
        else:
            out = subprocess.run(["pgrep", "-f", f"llama-server.*--port {port}( |$)"], capture_output=True, text=True, timeout=10).stdout
            for x in out.split():
                pid = int(x)
                if pid in mine_pids or pid == os.getpid(): continue
                try: os.kill(pid, 9); killed += 1
                except Exception: pass
      except Exception as e:
        print(f"[resident] stale-server sweep failed: {e}", flush=True)
    if killed:
        print(f"[resident] killed {killed} stale llama-server(s) on ports {ports[0]}..{ports[-1]}", flush=True)
        time.sleep(1.0)
    return killed


def stop_resident():
    """Free the GPU: terminate EVERY warm server on this host (a pooled run needs the VRAM, or serve shutdown).
    A model swap no longer comes here -- ensure_resident keeps the pool and evicts LRU only when it must."""
    with _POOL_LOCK:
        for e in list(_pool_alive().values()):
            _stop_entry(e, "freeing the GPU")
    p = _resident.get("proc")
    if p and p.poll() is None:
        try: p.terminate(); p.wait(timeout=10)
        except Exception:
            try: p.kill()
            except Exception: pass
    _resident.update(proc=None, model=None, sig=None)
    report_anchor_vram(0)

def ensure_resident(model_file, rpc_list, devices, tensor_split, n_ctx=None, need_ctx=0, plan_nodes=None,
                    escalated=False, conv_tokens=0):
    """D45 wrapper: pool mutation under _POOL_LOCK; the (long) health wait outside it, so a request that only needs
    an already-warm server never queues behind someone else's load."""
    with _POOL_LOCK:
        r = _ensure_resident_locked(model_file, rpc_list, devices, tensor_split, n_ctx, need_ctx, plan_nodes, escalated, conv_tokens)
    if isinstance(r, tuple):                                   # ("wait", entry, logf, budget): a start we must now wait for
        return _await_resident(*r[1:])
    return r

def _await_resident(entry, logf, budget):
    """Wait for a just-started (or someone else's in-progress) warm server to answer /health, measuring the shard
    stream meanwhile (D39 pass 2). Returns its base URL, or None (with resident.error set)."""
    proc, port, model_file = entry["proc"], entry["port"], entry["model"]
    shards, local_mb, rpc_list = entry.get("shards") or {}, entry.get("mb", 0.0), entry.get("_rpc") or []
    if shards and not _LOADING:
        print(f"[resident] pooled load: {os.path.basename(model_file)} -> "
              + ", ".join(f"{k} {v/1024:.1f} GB" for k, v in shards.items()) + f" over RPC, {local_mb/1024:.1f} GB here", flush=True)
    deadline = time.time() + (2400 if shards else 900)
    remote_mb = float(sum(shards.values())) if shards else 0.0
    b0 = _rpc_bytes_sent(rpc_list) if rpc_list else None
    cached = bool(shards) and all(nid in _delivered_nodes(os.path.basename(model_file)) for nid in shards)
    owner = not _LOADING or _LOADING.get("model") != os.path.basename(model_file)   # the first waiter narrates; joiners just wait
    if owner:
        _LOADING.clear()
        _LOADING.update(model=os.path.basename(model_file), nodes=list(entry.get("nodes") or []), started=time.time(),
                        remote_mb=remote_mb, local_mb=float(local_mb), sent_mb=0.0, rate_mbs=0.0, pct=0, eta_s=None, cached=cached,
                        measured=b0 is not None and remote_mb > 0, phase="disk" if not shards else "streaming")
    last_meas = 0.0
    while time.time() < deadline:
        if owner and _LOADING.get("measured") and time.time() - last_meas >= 2.0:
            last_meas = time.time()
            now_b = _rpc_bytes_sent(rpc_list)
            if now_b is not None:
                sent = max(0.0, (now_b - b0) / 1e6)
                el = max(1e-6, time.time() - _LOADING["started"]); rate = sent / el
                left = (remote_mb - sent) / rate if rate > 0.05 else None
                _LOADING.update(sent_mb=sent, rate_mbs=rate, pct=min(99, int(100 * sent / max(1.0, remote_mb))),
                                eta_s=left, phase="computing" if sent >= remote_mb * 0.98 else "streaming")
                # The ledger is not the only truth about a donor's tensor cache (it holds what earlier runs sent, from
                # before the ledger existed). If, 20 s in, almost nothing has crossed at a rate far below the wire, the
                # donors already hold it: say "cache load" instead of an ETA extrapolated from slivers (2026-09-19).
                if not _LOADING.get("cached") and el > 20 and rate < 8.0 and sent < 0.10 * remote_mb:
                    _LOADING["cached"] = True; _LOADING["cache_inferred"] = True
                    print(f"[resident] {_LOADING.get('model')}: {sent/1024:.1f} GB in {int(el)} s at {rate:.0f} MB/s -- the donors already hold it (cache), only slivers are crossing", flush=True)
        if proc.poll() is not None:
            tail = ""
            try:
                if logf: logf.close()
                with open(RESIDENT_LOG, encoding="utf-8", errors="replace") as f:
                    tail = " | ".join(l.strip() for l in f.readlines()[-4:] if l.strip())
            except Exception:
                pass
            with _POOL_LOCK:
                _POOL.pop(model_file, None); _pool_save()
            if owner: _LOADING.clear()
            _resident.update(proc=None, model=None, sig=None,
                             error=f"llama-server exited (code {proc.returncode}) while loading {os.path.basename(model_file)}: {tail[-400:]} — see poc/resident.log")
            print(f"[resident] {_resident['error']}", flush=True)
            return None
        if _resident_health(port):
            if owner:
                _LAST_LOAD.clear(); _LAST_LOAD.update(_LOADING, elapsed_s=time.time() - _LOADING.get("started", time.time()))
                if _LOADING.get("measured"):
                    print(f"[resident] delivered {last_load_text()}", flush=True)
                if shards:
                    _mark_delivered(os.path.basename(model_file), list(shards))
                _LOADING.clear()
                with _POOL_LOCK:
                    _pool_save()
                try: report_anchor_vram(_pool_held_mb())
                except Exception: pass
                print(f"[resident] warm: {', '.join(os.path.basename(k) for k in _pool_alive())} ({_pool_held_mb():.0f} MB of {budget:.0f})", flush=True)
            entry["ready"] = True
            return f"http://{RESIDENT_HOST}:{port}"
        time.sleep(0.5)
    if owner: _LOADING.clear()
    _resident["error"] = f"llama-server did not become healthy within 15 min loading {os.path.basename(model_file)} — see poc/resident.log"
    print(f"[resident] {_resident['error']}", flush=True)
    with _POOL_LOCK:
        _stop_entry(entry, "never became healthy")
    return None

def _ensure_resident_locked(model_file, rpc_list, devices, tensor_split, n_ctx=None, need_ctx=0, plan_nodes=None,
                            escalated=False, conv_tokens=0):
    """Guarantee a warm llama-server for `model_file` with this plan is up; return its base URL, or None to
    fall back. Reuses the current resident when the (model + plan + context) signature matches and it's
    healthy; otherwise swaps it. First start of a big model blocks while it loads (that's the ONE slow call).
    n_ctx None -> chosen by resident_ctx() from the model + this GPU's free memory (D32); `need_ctx` asks
    for at least that many tokens (a conversation that outgrew the current server)."""
    _resident.pop("pinned_by", None)                     # a stale "pinned" verdict from an earlier start must not shape this one
    have = _pool_alive().get(model_file)
    if n_ctx is None:
        cur = 0 if (have or {}).get("escalated") else ((have or {}).get("ctx") or 0)   # a drop-back sizes fresh, not to the fabric window
        ctx_budget = _local_anchor_free_mb()
        if rpc_list and plan_nodes:                      # D39: weights AND KV are split across the plan -- size the
            try:                                         # context by the whole pool's memory, not this card alone
                ctx_budget = sum(free_mem_mb(d) for d in load_fleet().get("donors", []) if d.get("id") in plan_nodes)
            except Exception:
                pass
        n_ctx, fit = resident_ctx(model_file, ctx_budget, need=need_ctx)
        if need_ctx == 0 and have and cur >= n_ctx:
            n_ctx = cur                                   # never shrink a healthy warm server for nothing
    sig = "|".join([model_file, ",".join(rpc_list or []), ",".join(devices or []),
                    ",".join(f"{w:.3f}" for w in (tensor_split or [])), f"ctx={n_ctx}"])
    # already warm in the pool (same plan + context)? activate it -- no load at all (D38)
    if have and have.get("sig") == sig and have["proc"].poll() is None and _resident_health(have["port"]):
        _activate(have)
        return f"http://{RESIDENT_HOST}:{have['port']}"
    if have and have["proc"].poll() is None and not have.get("ready"):
        _activate(have)                                       # D45: another request is loading this model right now -- join its
        return ("wait", have, None, _local_anchor_free_mb())  # wait (whatever its exact plan) rather than kill it and start over
    # D39: a POOLED resident (shards on other nodes) is a placement the user chose -- a later request must not
    # re-plan it into a different split just because donors' free memory drifted. Same model, warm, pooled: reuse.
    # D43: unless it is an ESCALATION (the fabric window a long chat needed): keep it while the conversation that
    # comes in still outgrows this card alone (a re-plan every turn would be two loads per message); drop back to
    # solo -- said out loud by the caller -- when a chat arrives that fits here again.
    if have and have.get("shards") and have["proc"].poll() is None and _resident_health(have["port"]):
        if not have.get("escalated"):
            _activate(have)
            return f"http://{RESIDENT_HOST}:{have['port']}"
        if rpc_list or conv_tokens * 1.25 > _solo_fit_ctx(model_file) or need_ctx > _solo_fit_ctx(model_file):
            _activate(have)                                 # still needs the bigger window: keep going on the fabric
            return f"http://{RESIDENT_HOST}:{have['port']}"
        _stop_entry(have, f"dropping back to this card alone: the conversation ({conv_tokens} tokens) fits here again")
        have = None
    if have:
        _stop_entry(have, "same model, new context/plan")  # it has to be restarted with the new signature
    # make room: this model's footprint vs what the pool already holds, within the anchor's budget
    try:
        need_mb = os.path.getsize(model_file) / (1024 * 1024) + kv_cache_mb(n_ctx, model_file) + 512
    except OSError:
        need_mb = model_mem_mb()
    # D39: with an RPC plan the footprint is SPLIT -- only this host's share lives on this card; the rest sits in
    # the donors' memory and is reported to the authority on their behalf (they run only ggml-rpc-server and never
    # self-report). `plan_nodes` mirrors devices/tensor_split 1:1 (build_devices).
    shards, local_mb = {}, need_mb
    if rpc_list:
        w = list(tensor_split) if tensor_split else None
        if not w or len(w) != len(devices or []):
            w = [1.0 / max(1, len(devices or [1]))] * max(1, len(devices or [1]))
        local_mb = 0.0
        for i, dev in enumerate(devices or []):
            nid = (plan_nodes or [None] * len(devices))[i] if plan_nodes else None
            if dev.startswith("RPC"):
                if nid: shards[nid] = int(need_mb * w[i])
            else:
                local_mb = need_mb * w[i]
    budget = _local_anchor_free_mb()
    for e in sorted(_pool_alive().values(), key=lambda e: e.get("last_used", 0)):
        if _pool_held_mb() + local_mb <= budget:
            break
        if e.get("shards") and not e.get("escalated"):
            continue                                     # D39: a fabric placement is CHOSEN -- never evicted for a passer-by (an escalation is not chosen)
        _stop_entry(e, f"evicted (LRU) to fit {os.path.basename(model_file)}: pool {_pool_held_mb():.0f} + {local_mb:.0f} > budget {budget:.0f} MB")
    if _pool_held_mb() + local_mb > budget:
        pinned = [e for e in _pool_alive().values() if e.get("shards") and not e.get("escalated")]
        if pinned:
            pin = pinned[0]
            _resident["pinned_by"] = os.path.basename(pin["model"])
            _resident["error"] = (f"{socket.gethostname().lower()} is holding {_tiny_model(os.path.basename(pin['model']))} across "
                                  f"{' + '.join(pin.get('nodes') or [])} ({pin.get('mb', 0)/1024:.1f} GB of this card); "
                                  f"{_tiny_model(os.path.basename(model_file))} needs ~{local_mb/1024:.1f} GB and only "
                                  f"{max(0.0, budget - _pool_held_mb())/1024:.1f} GB is free. Unload the pooled model (Control Room) or pick a smaller one.")
            print(f"[resident] {_resident['error']}", flush=True)
            return None
    _resident.pop("pinned_by", None)
    if not _POOL:
        kill_stale_residents()                           # nothing of ours is up: sweep orphans off every resident port
    port = _free_port()
    if port is None:
        _resident["error"] = "no free resident port (8081-8088)"; print(f"[resident] {_resident['error']}", flush=True); return None
    # -np 1: ONE slot. llama-server defaults to 4 parallel slots that carve the context up between them, so a
    # single conversation got a fraction of `-c` (the wall at "2 or 3 questions", 2026-09-15). The resident serves
    # one chat at a time anyway (the run lock) -- give that chat the whole window.
    args = [LLAMA_SERVER, "-m", model_file, "--host", RESIDENT_HOST, "--port", str(port),
            "-ngl", "99", "-c", str(n_ctx), "-np", "1", "--jinja", "--no-webui"]
    kvt = resident_kv_type()
    if kvt != "f16":                                   # quantized KV needs flash attention in llama.cpp
        args += ["-fa", "on", "--cache-type-k", kvt, "--cache-type-v", kvt]
    if rpc_list:
        args += ["--rpc", ",".join(rpc_list)]
    if devices:
        args += ["--device", ",".join(devices)]
    if tensor_split:
        args += ["--tensor-split", ",".join(f"{w:.4f}" for w in tensor_split)]
    # llama-server's stderr goes to resident.log, NOT /dev/null: when it dies on startup (missing binary,
    # bad --device, OOM, a Vulkan driver without the needed extension) the reason must be readable —
    # silently falling back to llama-cli on every call looked like "residency doesn't work" on the NUC.
    if not os.path.exists(LLAMA_SERVER):
        _resident["error"] = f"llama-server not found at {LLAMA_SERVER} — build it (cmake --target llama-server) or set GENGHIS_LLAMA_SERVER"
        print(f"[resident] {_resident['error']}", flush=True)
        return None
    if any(not dev.startswith("RPC") for dev in (devices or [])):       # the plan uses THIS host's own GPU
        prob = gpu_access_problem()
        if prob:
            _resident["error"] = prob                                    # readable in the Control Room, not `exited (code 1)`
            print(f"[resident] {_resident['error']}", flush=True)
            return None
    try:
        logf = open(RESIDENT_LOG, "w", encoding="utf-8", errors="replace")
        logf.write("$ " + " ".join(args) + "\n"); logf.flush()
        proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except Exception as e:
        _resident["error"] = f"could not start llama-server: {e}"
        print(f"[resident] {_resident['error']}", flush=True)
        return None
    entry = {"proc": proc, "model": model_file, "sig": sig, "port": port, "error": None, "ctx": n_ctx,
             "mb": local_mb, "total_mb": need_mb, "shards": shards, "nodes": list(plan_nodes or []), "escalated": bool(escalated),
             "last_used": time.time(), "_rpc": list(rpc_list or []), "ready": False, "inflight": 0}
    _POOL[model_file] = entry
    _activate(entry)
    _pool_save()
    if shards:
        try: report_anchor_vram(_pool_held_mb())                # book the donors' shares NOW, not on the next 60-s beat: for up to a
        except Exception: pass                                  # minute of a 3-minute load the page showed them free (36 vs 21 GB, 2026-09-19)
    return ("wait", entry, logf, budget)                        # the caller waits OUTSIDE the pool lock (_await_resident)

def _solo_fit_ctx(model_file):
    """The largest context this card alone can give `model_file` (D32's ladder)."""
    try:
        return resident_ctx(model_file, _local_anchor_free_mb())[1]
    except Exception:
        return N_CTX

def approx_tokens(messages):
    """A cheap size for an incoming conversation (chars/4 + a little per message) -- enough to decide whether an
    escalated fabric window is still needed, never used to size a server."""
    n = 0
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
        n += len(str(c or "")) // 4 + 4
        for tc in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            n += len(json.dumps(tc)) // 4
    return n

def escalation_plan(fleet, goal, need_tok):
    """D43: the same chat outgrew this card -- ask the planner for a placement sized for `need_tok` of context
    (weights + that KV), which will be a split across the fabric (or a card elsewhere). Returns
    (rpc_list, devices, weights, nodes) or None. The planner's context override is restored afterwards."""
    _req_set("plan_ctx", int(need_tok * 1.25))
    try:
        plan = _plan_for(fleet, goal)
    finally:
        _req_set("plan_ctx", None)
    if not plan or not plan[0]:                          # no plan, or a solo plan on this card (which just said no)
        return None
    return plan

def _repair_json_text(txt):
    """A JSON document a model wrote with RAW newlines/tabs inside its strings (Qwen does this for multi-line
    code arguments) -> the same document with them escaped. Returns (fixed_text, changed). If it still
    doesn't parse, returns the original and False."""
    try:
        json.loads(txt); return txt, False
    except Exception:
        pass
    out, in_str, esc = [], False, False
    for ch in txt:
        if in_str:
            if esc:            out.append(ch); esc = False
            elif ch == "\\":  out.append(ch); esc = True
            elif ch == '"':    out.append(ch); in_str = False
            elif ch == "\n":   out.append("\\n")
            elif ch == "\r":   out.append("\\r")
            elif ch == "\t":   out.append("\\t")
            else:              out.append(ch)
        else:
            out.append(ch)
            if ch == '"': in_str = True
    fixed = "".join(out)
    try:
        json.loads(fixed); return fixed, True
    except Exception:
        return txt, False

def tool_loop_guard(data):
    """A model that asks for the SAME tool with the SAME arguments over and over is not working, it is looping --
    the client (Open WebUI's native function-calling loop) runs the tool, appends the result, calls again, and the
    model asks again: 13 tokens a round, +438 tokens of context a round, forever, on the warm 70B (2026-09-18,
    the fleet tool, ~20 rounds before Michael stopped it). The client has no repetition check, so this is where
    one belongs: when the trailing rounds of the history are TOOL_LOOP_LIMIT identical calls, forward THIS request
    with `tool_choice: "none"` -- the model must now answer in prose from the results it already has. Returns the
    note to show the user ("" = no loop). The next user turn starts clean."""
    msgs = data.get("messages") or []
    if not data.get("tools") or data.get("tool_choice") == "none":
        return ""
    def key(m):
        out = []
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: pass
            out.append((str(fn.get("name") or "?"), json.dumps(args, sort_keys=True, default=str)))
        return tuple(sorted(out))
    rounds, i = [], len(msgs) - 1
    while i >= 0:                                  # walk back over the trailing tool rounds only
        m = msgs[i] if isinstance(msgs[i], dict) else {}
        if m.get("role") == "tool":
            i -= 1; continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            rounds.append(key(m)); i -= 1; continue
        break                                      # a user turn or a prose answer ends the run
    if len(rounds) >= TOOL_LOOP_LIMIT and rounds[0] and all(r == rounds[0] for r in rounds[:TOOL_LOOP_LIMIT]):
        data["tool_choice"] = "none"
        names = ", ".join(sorted({n for n, _ in rounds[0]}))
        note = (f"the model asked for `{names}` {len(rounds)} times in a row with the same arguments -- "
                f"that is a loop, not progress; answering from what it already has (tools off for this turn)")
        print(f"[v1] tool loop guard: {note}", flush=True)
        return note
    return ""

def sanitize_tool_args(messages):
    """D37 follow-through: llama-server json-parses every `tool_calls[].function.arguments` in the HISTORY when it
    renders the chat template -- one argument string with a raw newline in it (streamed straight through on an
    earlier turn, then echoed back by the client) poisons the whole chat: HTTP 500 on every request from then on,
    'try again' included (2026-09-16, Qwen2.5-32B + a render tool). Repair such strings in place; if one can't be
    repaired, wrap it as {"_raw": ...} so the template can render and the model still sees what it once wrote.
    Returns the number of arguments fixed."""
    fixed = 0
    for m in messages or []:
        for tc in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                fn["arguments"] = json.dumps(args); continue          # some clients send the object itself
            if not isinstance(args, str):
                continue
            new, changed = _repair_json_text(args)
            if changed:
                fn["arguments"] = new; fixed += 1
            else:
                try: json.loads(args)
                except Exception:
                    fn["arguments"] = json.dumps({"_raw": args}); fixed += 1
    if fixed:
        print(f"[v1] repaired {fixed} malformed tool-call argument string(s) in the chat history", flush=True)
    return fixed

class FenceGuard:
    """A whole HTML document a model writes WITHOUT the ```html fence renders as a wall of text instead of opening as
    an Artifact (Open WebUI) -- one dropped line, and the user sees garbage (2026-09-16, right after a tool call).
    Streaming filter: feed() every content delta, emit what it returns; finish() at the end. If a <!DOCTYPE html> /
    <html appears outside any code fence, a ```html line is inserted before it and ``` after </html> (or at the end).
    Documents the model fenced itself pass through untouched. Holds back at most a few characters while a marker may
    be forming, so streaming stays live."""
    OPEN = ("<!doctype html", "<html")
    CLOSE = ("</html>",)

    def __init__(self):
        self.buf = ""; self.in_fence = False; self.in_doc = False; self.ticks = 0; self.last = ""; self.bt = 0
        self.need_nl = False                     # a closing fence was just written: the next text must start on a new line

    def _emit(self, text, out):
        if not text:
            return out
        if self.need_nl:
            self.need_nl = False
            if not text.startswith("\n"): text = "\n" + text
        for ch in text:                          # fence parity by consecutive backticks -- a ``` split across chunks still counts
            if ch == "`":
                self.bt += 1
                if self.bt == 3: self.ticks += 1; self.bt = 0
            else:
                self.bt = 0
        self.in_fence = (self.ticks % 2 == 1); self.last = text[-1]
        return out + text

    @staticmethod
    def _partial(low, markers):
        """Longest suffix of `low` that is a proper prefix of a marker (held back until the next chunk decides)."""
        best = 0
        for m in markers:
            for n in range(min(len(low), len(m) - 1), 0, -1):
                if m.startswith(low[-n:]):
                    best = max(best, n); break
        return best

    def feed(self, text):
        self.buf += text
        out = ""
        while self.buf:
            low = self.buf.lower()
            if self.in_doc:
                i = low.find("</html>")
                if i >= 0:
                    j = i + len("</html>")
                    out += self.buf[:j] + "\n```"; self.last = "`"; self.buf = self.buf[j:]
                    self.in_doc = False; self.ticks += 1; self.bt = 0; self.in_fence = False; self.need_nl = True
                    continue
                hold = self._partial(low, self.CLOSE)
                emit = self.buf[:len(self.buf) - hold]
                if emit: self.last = emit[-1]
                out += emit; self.buf = self.buf[len(emit):]
                break
            hits = [k for k in (low.find(m) for m in self.OPEN) if k >= 0]
            if hits:
                hit = min(hits)
                out = self._emit(self.buf[:hit], out); self.buf = self.buf[hit:]
                if self.in_fence:                          # the model fenced it itself: pass through, keep scanning
                    out = self._emit(self.buf[0], out); self.buf = self.buf[1:]
                    continue
                sep = "" if (not self.last or self.last == "\n") else "\n"
                out += sep + "```html\n"; self.last = "\n"; self.ticks += 1; self.bt = 0; self.in_fence = True; self.in_doc = True
                continue
            hold = self._partial(low, self.OPEN)
            emit = self.buf[:len(self.buf) - hold]
            out = self._emit(emit, out); self.buf = self.buf[len(emit):]
            break
        return out

    def finish(self):
        out = self.buf; self.buf = ""
        if self.in_doc:
            out += "\n```\n"; self.in_doc = False
        elif self.need_nl:
            out += "\n"; self.need_nl = False
        return out


def fence_html(text):
    """Non-streaming form of FenceGuard for a complete message."""
    g = FenceGuard(); return g.feed(text or "") + g.finish()


class ContextTooSmall(RuntimeError):
    """llama-server: the request is longer than the warm server's context. `.need` = tokens in the request."""
    def __init__(self, need, have):
        super().__init__(f"request is {need} tokens; the warm server holds {have}")
        self.need, self.have = need, have


# --- D39 formations: what is warm where, as a named layout, switched in one action ------------------------
# A formation is the fleet's warm layout by name -- "Work" = the 32B on the laptop + the 1.5B on the NUC; "Evening" =
# the 70B across the fabric. Stored in the authority's config (`formations`), so every host sees the same list.
# Applying one = unload every warm model that is not in it, then warm each item in order, each step said out loud
# (D40) and its transfer measured (pass 2). One apply at a time, fleet-wide, from whichever host you are on.
_FORMATION = {}          # the apply in progress: name, step, of, note, log, started, done, ok

def live_layout():
    """What is warm where RIGHT NOW, from the fabric view: [{model, node}] for a solo warm model, [{model, pooled:true,
    nodes:[...]}] for one kept warm across the fabric (listed once, under its host)."""
    out = []
    try:
        fab = fabric_state(load_fleet())
    except Exception:
        return out
    for n in fab.get("nodes", []):
        if not n.get("up") or not n.get("host"):
            continue
        pooled = n.get("pooled") or {}
        for m in n.get("warm") or []:
            if m in (n.get("escalated") or []):
                continue                                          # D43: a window a long chat asked for is not a layout
            if m in pooled and len(pooled[m]) > 1:
                out.append({"model": m, "pooled": True, "host": n["id"], "nodes": list(pooled[m])})
            else:
                out.append({"model": m, "node": n["id"]})
    return out

def formations_list():
    f = load_config().get("formations")
    return f if isinstance(f, dict) else {}

def _item_matches(item, live):
    if item.get("pooled"):                                 # "across the fabric" is satisfied by wherever the fabric put it --
        return any(l["model"] == item["model"] for l in live)   # a split, or one card that could hold it whole
    return any((not l.get("pooled")) and l["model"] == item["model"] and l.get("node") == item.get("node") for l in live)

def current_formation(live=None):
    """The name of the formation whose every item is warm right now (a chat may have warmed extra models beside
    it -- that does not break the match), or None."""
    live = live if live is not None else live_layout()
    for name, items in formations_list().items():
        if items and all(_item_matches(it, live) for it in items):
            return name
    return None

def formation_plan(name):
    """The steps applying `name` would take, as [{action, model, node|pooled, note}] -- unloads first (everything warm
    that the formation does not include), then loads in the formation's order, skipping what is already right."""
    items = formations_list().get(name)
    if items is None:
        return None
    live = live_layout()
    keep = {(it["model"], "pooled" if it.get("pooled") else it.get("node")) for it in items}
    keep_any = {it["model"] for it in items if it.get("pooled")}     # a fabric item keeps that model wherever the fabric put it
    steps = []
    for l in live:
        key = (l["model"], "pooled" if l.get("pooled") else l.get("node"))
        if key not in keep and l["model"] not in keep_any:
            host = l.get("host") or l.get("node")
            steps.append({"action": "unload", "model": l["model"], "node": host,
                          "note": f"unload {_tiny_model(l['model'])} " + (f"(across {' + '.join(l.get('nodes') or [])})" if l.get("pooled") else f"on {host}")})
    for it in items:
        if _item_matches(it, live):
            continue
        if it.get("pooled"):
            steps.append({"action": "load", "model": it["model"], "pooled": True, "note": f"warm {_tiny_model(it['model'])} across the fabric"})
        else:
            steps.append({"action": "load", "model": it["model"], "node": it.get("node"), "note": f"warm {_tiny_model(it['model'])} on {it.get('node')}"})
    return steps

def formation_status():
    return dict(_FORMATION) if _FORMATION else None

def apply_formation(name, port):
    """Run formation_plan(name) step by step in a background thread, through THIS serve's /residency (which forwards
    to the right host). Returns (ok, note). One at a time."""
    if _FORMATION and not _FORMATION.get("done"):
        return False, f"already switching to {_FORMATION.get('name')} (step {_FORMATION.get('step')}/{_FORMATION.get('of')})"
    steps = formation_plan(name)
    if steps is None:
        return False, f"no formation named {name}"
    _FORMATION.clear()
    _FORMATION.update(name=name, step=0, of=len(steps), note="starting", log=[], started=time.time(), done=False, ok=None,
                      steps=[st["note"] for st in steps])
    if not steps:
        _FORMATION.update(done=True, ok=True, note=f"{name} is already in place", finished=time.time())
        return True, f"{name} is already in place -- nothing to do"
    def _run():
        ok_all = True
        for i, st in enumerate(steps, 1):
            _FORMATION.update(step=i, note=st["note"], target=st.get("node") or "")
            body = {k: v for k, v in st.items() if k in ("action", "model", "node", "pooled")}
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/residency", data=json.dumps(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json", **_auth_headers()})
                with urllib.request.urlopen(req, timeout=2400) as r:
                    res = json.loads(r.read().decode("utf-8"))
                ok = bool(res.get("ok")); note = res.get("note") or ""
            except urllib.error.HTTPError as e:
                ok = False
                try: note = (json.loads(e.read().decode("utf-8")).get("note") or f"HTTP {e.code}")
                except Exception: note = f"HTTP {e.code}"
            except Exception as e:
                ok, note = False, str(e)
            _FORMATION["log"].append(("ok " if ok else "FAILED ") + st["note"] + (f" -- {note}" if note else ""))
            print(f"[formation] {name} {i}/{len(steps)}: {_FORMATION['log'][-1]}", flush=True)
            if not ok:
                ok_all = False
                break                                    # a failed step stops the switch; the log says which and why
        _FORMATION.update(done=True, ok=ok_all, finished=time.time(),
                          note=(f"{name} is in place" if ok_all else f"stopped: {_FORMATION['log'][-1]}"))
    threading.Thread(target=_run, name="formation", daemon=True).start()
    return True, f"switching to {name}: {len(steps)} step(s) -- " + "; ".join(st["note"] for st in steps)

# --- D34: delegate to the warm host ---------------------------------------------------------------------
# A chat that would drive a REMOTE GPU over RPC (per-token round trips, a cache re-upload every call) is
# handed instead to the host whose OWN anchor already holds that model warm -- zero network per token, no
# load. Measured 2026-09-14: 32B over wired RPC 21 t/s + ~60 s load; the same 32B resident on its host ~30 t/s
# and instant. Capacity from the fleet, speed from placement.
DELEGATED_HDR = "X-Genghis-Delegated"

def _delegate_target(name, fleet, model_mb):
    """(node_id, base_url, warm) for the best OTHER host that can serve `name` on its own local anchor:
    warm (resident up with this model) beats cold-but-fits (local disk load, still no per-token network).
    None when no such host answers. Only nodes with `local: true` (a box that runs its own serve) qualify."""
    cands = []; why = []
    for d in fleet.get("donors", []):
        if not d.get("local") or is_self_node(d) or not d.get("ip"):
            continue
        if d.get("status") == "down":
            why.append(f"{d['id']}: down"); continue
        if d.get("lend") is False:
            why.append(f"{d['id']}: lend off (owner keeps the GPU)"); continue
        base = f"http://{d['ip']}:{int(d.get('serve_port') or 8899)}"
        if name in (d.get("warm") or []) and d.get("status") == "up":     # the beat says it's warm there (D38): no probe
            cands.append((0, -(d.get("tps_ema") or d.get("tokens_per_s_solo") or 0), d["id"], base, True)); continue
        reg = None
        for tmo in (2.0, 6.0):                              # a host mid-answer can be slow to reply: one retry
            try:
                reg = _remote_json(base + "/registry.json", ttl=2.0, timeout=tmo); break
            except Exception as e:
                err = e
        if reg is None:
            why.append(f"{d['id']}: no serve ({err})"); continue
        res = reg.get("resident") or {}
        warm = (bool(res.get("up")) and res.get("model") == name) or any(e.get("model") == name for e in res.get("pool") or [])
        fits = best_case_mem_mb(d) >= model_mb                 # a host can SWAP its resident: judge by the card, not by what it holds now
        if warm or fits:
            cands.append((0 if warm else 1, -(d.get("tps_ema") or d.get("tokens_per_s_solo") or 0), d["id"], base, warm))
        else:
            why.append(f"{d['id']}: resident={res.get('model')}/{res.get('up')} free={free_mem_mb(d):.0f}<{model_mb:.0f}")
    if not cands:
        print(f"  [delegate] no host for {name}: {'; '.join(why) or 'no local hosts in the fleet'}", flush=True)
        _req_set("last_why", why)                          # the user gets told too (never a silent slow path)
        return None
    cands.sort()
    _, _, nid, base, warm = cands[0]
    return nid, base, warm


def pinned_alternative(name, requested_model, fleet):
    """D42: the goal's model is pinned out of this card (a fabric placement holds the memory, D39). A small answer must
    not die for that -- last night a warm 70B on the NUC 409'd every Open WebUI task call (titles, tags, follow-ups)
    and every `fastest` question on the home base, silently. In order: (a) another host that holds `name` warm or
    can hold it -> delegate; (b) if the request named an effort GOAL (not a concrete model), the largest registry
    text model that fits in what this card has left beside the pinned placement -> substitute, and say so.
    Returns ("delegate", nid, base, warm) | ("substitute", path, note) | None."""
    tgt = _delegate_target(name, fleet, model_mem_mb())
    if tgt and tgt[2]:                                     # warm on another host: the best answer, no load anywhere
        return ("delegate",) + tuple(tgt)
    field = str(requested_model or "")
    goal = field[len("genghis-"):] if field.startswith("genghis-") else ""
    if goal not in GOALS:
        return ("delegate",) + tuple(tgt) if tgt else None  # a model asked for by NAME: never swapped; a cold host may still take it
    sub = _substitute_for(name, goal)
    # `fastest` wants an answer NOW: a small model that fits here beats a host that must load (or fetch) the right one.
    # The quality goals want the RIGHT model: a cold host (one-time load, told to the user) beats a downgrade.
    if goal == "fastest" and sub:
        return sub
    if tgt:
        return ("delegate",) + tuple(tgt)
    return sub

def _substitute_for(name, goal):
    """The largest registry text model that fits beside the pinned placement on this card, as a
    ("substitute", path, note) -- or None. Only for an effort goal: a model asked for by name is never swapped."""
    if not goal:
        return None
    pins = [e for e in _pool_alive().values() if e.get("shards")]
    if not pins:
        return None
    left = max(0.0, _local_anchor_free_mb() - sum(e.get("mb", 0) for e in pins))
    best = None
    for mid, m in registry_index().items():
        if mid == name or m.get("kind") == "vlm":
            continue
        try:
            need = os.path.getsize(m["path"]) / (1024 * 1024) + kv_cache_mb(N_CTX, m["path"]) + 512
        except Exception:
            continue
        if need <= left and (best is None or need > best[1]):
            best = (m["path"], need)
    if not best:
        return None
    pin = pins[0]
    note = (f"{goal} -> {_tiny_model(name)} needs ~{model_mem_mb()/1024:.1f} GB, but this card holds "
            f"{_tiny_model(os.path.basename(pin['model']))} across {' + '.join(pin.get('nodes') or [])} "
            f"({pin.get('mb', 0)/1024:.1f} GB of {_local_anchor_free_mb()/1024:.1f}); answering with "
            f"{_tiny_model(os.path.basename(best[0]))} instead -- it fits in the {left/1024:.1f} GB left")
    return ("substitute", best[0], note)

def _delegate_stream(base, data, sse_raw):
    """Forward the chat to another host's /v1 and relay its SSE lines verbatim (its narration included)."""
    body = json.dumps({**data, "stream": True}).encode("utf-8")
    req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", DELEGATED_HDR: SELF_HOST or "1", **_auth_headers()})
    with urllib.request.urlopen(req, timeout=1800) as r:
        for line in r:
            if line.strip():
                sse_raw(line)


DELIVERED = os.path.join(HERE, "delivered.json")   # model file -> [node ids that hold it in their tensor cache]

def _delivered_nodes(name):
    try:
        with open(DELIVERED, encoding="utf-8") as f:
            return set(json.load(f).get(name) or [])
    except Exception:
        return set()

def _mark_delivered(name, nodes):
    """After a pooled run completes, remember that these donors now hold `name` in their tensor cache (-c),
    so the NEXT run's narration says "loading from cache" instead of counting a 20 GB stream that never comes."""
    try:
        try:
            with open(DELIVERED, encoding="utf-8") as f: d = json.load(f)
        except Exception:
            d = {}
        d[name] = sorted(set(d.get(name) or []) | set(nodes))
        tmp = DELIVERED + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f, indent=1)
        os.replace(tmp, DELIVERED)
    except Exception:
        pass


def _rpc_bytes_sent(rpc_list):
    """How many bytes THIS box has pushed to the given RPC donors so far (sum of bytes_acked over every TCP
    socket to ip:port), from the kernel via `ss -tin` -- Linux only; None where that isn't available.
    The one number that turns "working (102 s)" into "6.4 / 20.5 GB (31%) ~15 min left" while a model
    streams to a remote GPU: the shard stream has no progress output of its own, but the socket knows."""
    if not rpc_list or platform.system() != "Linux" or not shutil.which("ss"):
        return None
    try:
        out = subprocess.run(["ss", "-tin"], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return None
    total, want = 0, {ep.strip() for ep in rpc_list}
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if not any(ep in ln for ep in want):
            continue
        detail = lines[i + 1] if i + 1 < len(lines) else ""
        m = re.search(r"bytes_acked:(\d+)", detail)
        if m:
            total += int(m.group(1))
    return total


def _proxy_resident(base_url, data, stream):
    """Forward an OpenAI chat request to the resident server. Returns (text, ok) for non-stream, or yields
    SSE-content deltas for stream — llama-server emits valid OpenAI chunks, so streaming is a near-passthrough."""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(f"{base_url}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    def _open():
        """urlopen, but a 4xx/5xx from llama-server becomes an error that SAYS WHAT IT REJECTED (its JSON
        error.message) plus the request's shape in the log — a bare 'HTTP Error 400' helped nobody."""
        try:
            return urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = (json.loads(e.read().decode("utf-8", "replace")).get("error") or {}).get("message") or ""
            except Exception:
                pass
            shape = {k: (type(v).__name__ if k != "messages" else
                         [(m.get("role"), type(m.get("content")).__name__, len(str(m.get("content") or ""))) for m in v])
                     for k, v in data.items()}
            print(f"[resident] llama-server rejected the request: HTTP {e.code} {msg!r}  request shape: {shape}", flush=True)
            m = re.search(r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)", msg or "")
            if m:
                raise ContextTooSmall(int(m.group(1)), int(m.group(2))) from None
            raise RuntimeError(f"the model server rejected the request (HTTP {e.code}): {msg or 'no detail'}") from None
    if not stream:
        with _open() as r:
            resp = json.loads(r.read().decode("utf-8"))
        ch = (resp.get("choices") or [{}])[0]
        m = ch.get("message", {}) or {}
        msg = m.get("content", "") or ""
        if m.get("reasoning_content"):                       # keep the model's thinking for clients that show it
            _req_set("proxy_reasoning", m["reasoning_content"])
        # D37: native tool calling -- the warm server (llama-server --jinja) emits OpenAI `tool_calls`; hand the
        # whole message + finish_reason to the caller so nothing the model decided is dropped on the floor.
        _req_set("proxy_message", m)
        _req_set("proxy_finish", ch.get("finish_reason") or ("tool_calls" if m.get("tool_calls") else "stop"))
        _req_set("proxy_usage", resp.get("usage"))
        return msg, True
    def _gen():
        with _open() as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                p = line[5:].strip()
                if p == "[DONE]":
                    break
                try:
                    d = json.loads(p)
                    ch = d["choices"][0]
                    delta = dict(ch.get("delta", {}) or {})      # content, reasoning_content AND tool_calls (D31/D37)
                    if ch.get("finish_reason"):
                        delta["_finish"] = ch["finish_reason"]
                    if d.get("usage"):
                        delta["_usage"] = d["usage"]
                    yield delta
                except Exception:
                    continue
    return _gen()


def calibrate(fleet):
    """Measure each donor's solo throughput (all layers on it alone)."""
    donors = live_donors(fleet)
    print(f"== Calibrating {len(donors)} live donor(s) — solo throughput ==")
    for i, d in enumerate(donors):
        rpc_list, devices = build_devices([d])       # local node -> CUDA0 (no --rpc); donor -> RPC0 (M16/D9)
        print(f"  [{d['id']}] {rpc_list[0] if rpc_list else devices[0]} ... ", end="", flush=True)
        gen, pp, ok = run_llama(rpc_list, devices)
        if ok:
            d["tokens_per_s_solo"] = round(gen, 2)
            observe_tps(d, gen, fleet, persist=False)    # seed/refresh the self-tuning EMA (v0.3)
            ema = f" | ema {d['tps_ema']:.1f}" if d.get("tps_ema") else ""
            print(f"gen {gen:.1f} t/s (prompt {pp:.0f} t/s){ema}")
        else:
            print("FAILED (no timing parsed) — skipping")
    save_fleet(fleet)
    print("  -> solo throughput written to fleet.json")
    return donors


def make_plan(donors, strategy):
    """Return (weights, rpc_list, devices) for a strategy: 'naive' or 'genghis'."""
    if strategy == "naive":
        raw = [mem_weight(d) for d in donors]           # split by memory
    elif strategy == "genghis":
        raw = [donor_tps(d) for d in donors]            # split by throughput (self-tuned EMA if we have it)
    else:
        raise ValueError(strategy)
    total = sum(raw) or 1
    weights = [r / total for r in raw]
    rpc_list, devices = build_devices(donors)        # local anchor -> CUDA0, donors -> RPC0.. (M16/D9)
    return weights, rpc_list, devices


def save_plan(run_id, strategy, donors, weights):
    os.makedirs(PLANS_DIR, exist_ok=True)
    plan = {
        "run_id": run_id, "strategy": strategy,
        "assignments": [
            {"donor": d["id"], "share": round(w, 3),
             "weighted_by": ("throughput/tok_s" if strategy == "genghis" else "memory/MB")}
            for d, w in zip(donors, weights)
        ],
    }
    with open(os.path.join(PLANS_DIR, f"{run_id}_{strategy}.json"), "w") as f:
        json.dump(plan, f, indent=2)


def log_run(rec):
    with open(RUNS, "a") as f:
        f.write(json.dumps(rec) + "\n")


def sweep(fleet):
    donors = live_donors(fleet)
    if any(d.get("tokens_per_s_solo") is None for d in donors):
        donors = calibrate(fleet)

    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    results = {}
    print(f"\n== Sweep: naive (by memory) vs genghis (by throughput) across {len(donors)} donors ==")
    for strategy in ("naive", "genghis"):
        run_id = f"{ts}_{strategy}"
        weights, rpc_list, devices = make_plan(donors, strategy)
        save_plan(run_id, strategy, donors, weights)
        shares = ", ".join(f"{d['id']}={w*100:.0f}%" for d, w in zip(donors, weights))
        print(f"\n  [{strategy}] plan: {shares}")
        print(f"    running split across {devices} ...", flush=True)
        t0 = time.time()
        gen, pp, ok = run_llama(rpc_list, devices, tensor_split=weights)
        wall = time.time() - t0
        results[strategy] = gen
        print(f"    -> generation {gen} t/s | prompt {pp} t/s | {wall:.0f}s wall")
        log_run({"run_id": run_id, "ts": ts, "strategy": strategy,
                 "fleet": [d["id"] for d in donors],
                 "shares": {d["id"]: round(w, 3) for d, w in zip(donors, weights)},
                 "gen_tok_s": gen, "prompt_tok_s": pp})

    print("\n================= RESULT =================")
    n = results.get("naive"); g = results.get("genghis")
    print(f"  naive  (memory-weighted):   {n} tok/s")
    print(f"  genghis(throughput-weighted): {g} tok/s")
    if n and g:
        print(f"  --> GENGHIS is {g/n:.2f}x the naive split "
              f"({'WIN' if g > n else 'no win yet'})")
    print("==========================================")


def hb_show(fleet):
    """One-shot heartbeat with a status table."""
    heartbeat(fleet)
    print("== Heartbeat ==")
    for d in fleet.get("donors", []):
        s = "UP  " if d.get("status") == "up" else "DOWN"
        print(f"  {d['id']:<12} [{s}]  lat={d.get('latency_ms_to_orchestrator')}ms  "
              f"reliab={d.get('reliability')}  seen={d.get('last_seen','-')}")


def _fmt_dur(secs):
    """Compact duration: 3d 4h / 5h 2m / 12m / 8s."""
    secs = max(0, int(secs)); m, s = divmod(secs, 60); h, m = divmod(m, 60); d, h = divmod(h, 24)
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    if m: return f"{m}m"
    return f"{s}s"


def host_uptime():
    """Local host uptime string (this box = the coordinator in deployment), or None if unavailable."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/uptime") as f:
                return _fmt_dur(float(f.read().split()[0]))
        if sys.platform.startswith("win"):
            import ctypes
            return _fmt_dur(ctypes.windll.kernel32.GetTickCount64() / 1000.0)
    except Exception:
        pass
    return None


def recent_runs(n=3):
    """Last n run events from runs.jsonl as 'HH:MM run Xt/s' strings (newest last)."""
    try:
        with open(RUNS) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln); ts = r.get("ts", "")
            hhmm = ts[11:16].replace("-", ":") if len(ts) >= 16 else "--:--"
            g = r.get("gen_tok_s")
            out.append(f"{hhmm} run {g}t/s" if g else f"{hhmm} run")
        except Exception:
            pass
    return out


def _model_short(path):
    """'Qwen2.5-32B-Instruct-Q4_K_M.gguf' -> 'Qwen2.5-32B' (family + param size)."""
    base = os.path.basename(path).replace(".gguf", "")
    fam = base.split("-")[0]
    m = re.search(r"(\d+\.?\d*)\s*[bB]\b", base)
    return f"{fam}-{m.group(1).upper()}B" if m else base[:18]


def operator_view(fleet):
    """Terminal Operator View (D7): live fabric — green=in-plan, amber=idle-by-policy, red=down.
    TTY -> live colored refresh loop; piped/captured -> one plain snapshot (scriptable)."""
    tty = sys.stdout.isatty()
    uni = (getattr(sys.stdout, "encoding", "") or "").lower().startswith("utf")
    DOT, FULL, HOLLOW, MT, RULE = ("●", "█", "□", "·", "─") if uni else ("*", "#", "o", ".", "-")
    C = (lambda c: c) if tty else (lambda c: "")
    G, A, R, DIM, B, CY, M, X = (C("\033[32m"), C("\033[33m"), C("\033[31m"), C("\033[2m"),
                                 C("\033[1m"), C("\033[36m"), C("\033[35m"), C("\033[0m"))
    W = 78
    events = []   # persists across frames: [(sortkey, text)] newest-first at render

    def age_str(last_seen):
        try:
            secs = (datetime.datetime.now() - datetime.datetime.fromisoformat(last_seen)).total_seconds()
            return _fmt_dur(secs)
        except Exception:
            return "?"

    def frame():
        # D7: the view is a pure READER — persist=False. (Persisting every frame made concurrent
        # viewers clobber fleet.json and stomp external edits; the heartbeat/monitor/run cmds own writes.)
        for did, prev, new in heartbeat(fleet, persist=False):   # capture transitions -> events log
            events.insert(0, (datetime.datetime.now().strftime("%H:%M"),
                              f"{did} {'rejoined' if new == 'up' else 'dropped'}"))
        live = live_donors(fleet)
        chosen, per, fits, ranked_ids = [], {}, True, []
        try:
            dec = plan_v3(live, active_goal()) if live else {"nodes": [], "per_node": {}, "fits": False}
            chosen = dec.get("nodes") or []
            per = dec.get("per_node", {}); fits = dec.get("fits", True)
            ranked_ids = [d["id"] for d in sorted(live, key=eff_throughput, reverse=True)]
        except Exception:
            pass
        chosen_set = set(chosen)
        cap_tot = sum(free_mem_mb(d) for d in fleet["donors"] if d["id"] in chosen_set) or 1
        anchor = ranked_ids[0] if ranked_ids else None
        now = datetime.datetime.now().strftime("%H:%M:%S")
        up_n = sum(1 for d in fleet["donors"] if d.get("status") == "up")

        upt = host_uptime(); chost = fleet.get("coordinator", {}).get("host", "?")
        lastrun = recent_runs(1); tail = f" · {lastrun[-1].split('run ')[-1]}" if lastrun else ""
        hdr = (f"{B}{CY}GENGHIS{X}{B} · coordinator {chost}{X}"
               + (f"{DIM} · up {upt}{X}" if upt else "")
               + f"{DIM} · {X}{_model_short(active_model())} {B}[{active_goal()}]{X}"
               + ("" if fits else f" {R}[WON'T FIT]{X}") + f"{DIM}{tail} · {now}{X}")
        head = (f"{DIM} {'NODE':<12}{'STATE':<8}{'THRPUT':>7}  {'FREE':>8}  {'LAT':>6}  "
                f"{'RELIAB':<7}{'SHARE':>6}  WHY{X}")
        out = [hdr, DIM + RULE * W + X, head]

        for d in fleet.get("donors", []):
            up = d.get("status") == "up"
            role = d.get("role", "compute")
            if role in ("surface", "storage"):        # non-compute fabric members (D8: TV = HEARTH surface)
                disk = d.get("disk_free_gb")
                if not up:
                    col, word, why = R, "DOWN", f"no comms {age_str(d.get('last_seen',''))}"
                elif role == "surface":
                    col, word = M, "SURF"
                    why = "display / comms surface (HEARTH) — not a compute/storage donor"
                elif d.get("usb_attached") is False:
                    col, word = CY, "STORE"
                    why = "storage role (D8) needs a USB drive — none attached"
                else:
                    col, word = CY, "STORE"
                    why = "model repository (D8) — serves the GGUF library"
                thr = (f"{d.get('serve_mb_s'):.0f} MB/s" if up and role == "storage" and d.get("serve_mb_s") else "—")
                if not up or role == "surface":
                    mem = "—"
                elif disk and disk >= 1000:
                    mem = f"~{disk/1000:.1f} TB"
                elif disk:
                    mem = f"{disk:.1f} GB"
                else:
                    mem = "—"
                lat = (f"{d.get('latency_ms_to_orchestrator'):.0f}ms"
                       if up and d.get("latency_ms_to_orchestrator") is not None else "—")
                rel = d.get("reliability", 1.0); filled = int(round(rel * 6))
                barcol = (M if role == "surface" else CY) if up else R
                bar = barcol + FULL * filled + X + DIM + MT * (6 - filled) + X
                state = f"{col}{DOT}{X} {col}{word:<5}{X}"
                out.append(f" {B}{d['id']:<12}{X}{state} {thr:>7}  {mem:>8}  {lat:>6}  "
                           f"{bar} {'—':>5}  {DIM}{why}{X}")
                continue
            in_plan = up and d["id"] in chosen_set and fits
            if not up:
                col, word = R, "DOWN"
                why = f"no comms {age_str(d.get('last_seen',''))}"
            elif in_plan:
                col, word = G, "IN"
                why = ("anchor" if d["id"] == anchor
                       else "margin (drop-safe)" if "marginal" in per.get(d["id"], "")
                       else "capacity")
            else:
                col, word = A, "IDLE"
                why = "excluded: bottleneck" if up else "up"
            thr = f"{donor_tps(d):.0f} t/s" if up else "—"
            mem = f"{free_mem_mb(d)/1024:.1f} GB" if up else "—"
            lat = (f"{d.get('latency_ms_to_orchestrator'):.0f}ms"
                   if up and d.get("latency_ms_to_orchestrator") is not None else "—")
            share = f"{free_mem_mb(d)/cap_tot*100:.0f}%" if in_plan else "—"
            rel = d.get("reliability", 1.0); filled = int(round(rel * 6))
            rcol = (G if rel >= 0.9 else A if rel >= 0.6 else R) if up else R
            glyph = FULL if in_plan else HOLLOW      # solid = in operation; hollow = healthy but benched
            bar = rcol + glyph * filled + X + DIM + MT * (6 - filled) + X
            state = f"{col}{DOT}{X} {col}{word:<5}{X}"
            out.append(f" {B}{d['id']:<12}{X}{state} {thr:>7}  {mem:>8}  {lat:>6}  "
                       f"{bar} {share:>5}  {DIM}{why}{X}")

        out.append(DIM + RULE * W + X)
        feed = [f"{t} {txt}" for t, txt in events[:3]] + list(reversed(recent_runs(3)))
        foot = "  ·  ".join(feed[:4]) or "no events yet"
        tail2 = (f"q/Ctrl-C to exit · refresh {MONITOR_EVERY}s" if tty else "snapshot")
        out.append(f"{DIM} fabric {up_n}/{len(fleet['donors'])} up  ·  events: {foot}  ·  {tail2}{X}")
        return "\n".join(out)

    if not tty:
        print(frame()); return
    try:
        while True:
            sys.stdout.write("\033[2J\033[H" + frame() + "\n")
            sys.stdout.flush()
            time.sleep(MONITOR_EVERY)
    except KeyboardInterrupt:
        print("\n  Operator View closed.")


def fabric_state(fleet, goal=None):
    """Structured live snapshot of the fabric for external surfaces (HEARTH TV monitor).
    Mirrors the Operator View data as plain JSON-able dicts. Heartbeats (no persist) + plans fresh."""
    cfg = load_config()
    coord_id = fleet.get("coordinator", {}).get("id")   # this node runs the coordinator program itself
    goal = goal or cfg.get("default_goal") or active_goal()   # web-set default goal, else launch GOAL
    heartbeat(fleet, persist=False)
    live = live_donors(fleet)
    try:
        dec = plan_v3(live, goal) if live else {"nodes": [], "fits": False}
    except Exception:
        dec = {"nodes": [], "fits": False}
    chosen = set(dec.get("nodes") or [])
    fits = dec.get("fits", True)
    ranked = [d["id"] for d in sorted(live, key=eff_throughput, reverse=True)]
    # the anchor is the node the PLAN leans on: the solo node, else the highest-throughput in-plan node
    # (a D9-addendum local solo run makes THIS host the anchor even when a remote GPU ranks higher)
    anchor = next((i for i in ranked if i in chosen), None) if chosen else (ranked[0] if ranked else None)
    if dec.get("split") is False and len(chosen) == 1:
        anchor = next(iter(chosen))
    cap_tot = sum(free_mem_mb(d) for d in fleet["donors"] if d["id"] in chosen) or 1
    gpu_problem = gpu_access_problem()                 # "" unless this host cannot open its own GPU
    nodes = []
    for d in fleet.get("donors", []):
        up = d.get("status") == "up"
        role = cfg["roles"].get(d["id"]) or d.get("role", "compute")   # web-admin role override wins
        n = {"id": d["id"], "name": cfg["names"].get(d["id"]) or d["id"], "role": role, "up": up,
             "reliab": round(d.get("reliability", 1.0), 3),
             "lat": d.get("latency_ms_to_orchestrator"), "thrput": "", "free": "", "share": "", "why": ""}
        if role in ("surface", "storage"):
            n["state"] = "DOWN" if not up else ("STORE" if role == "storage" else "SURF")
            if up and role == "storage":
                if d.get("disk_free_gb"):
                    n["free"] = f"~{d['disk_free_gb']/1000:.1f} TB" if d["disk_free_gb"] >= 1000 else f"{d['disk_free_gb']:.1f} GB"
                if d.get("serve_mb_s"):
                    n["thrput"] = f"{d['serve_mb_s']:.0f} MB/s"
                n["why"] = "model repository"
            elif up:
                n["why"] = "display / comms surface"
            else:
                n["why"] = "no comms"
        else:
            in_plan = up and d["id"] in chosen and fits
            foreign = bool(d.get("local")) and not is_self_node(d) and not has_rpc(d)   # another host's private GPU
            held = d.get("lend") is False and not is_self_node(d)                       # D35: owner keeps the GPU
            n["lend"] = d.get("lend", True); n["away"] = bool(d.get("away"))
            n["warm"] = ([os.path.basename(e["model"]) for e in sorted(_pool_alive().values(), key=lambda e: -e.get("last_used", 0))]
                         if is_self_node(d) else list(d.get("warm") or [])) if up else []
            n["host"] = is_self_node(d) or (bool(d.get("local")) and "warm" in d)      # runs a serve (reports its pool): warm/unload there
            n["escalated"] = ([os.path.basename(e["model"]) for e in _pool_alive().values() if e.get("escalated")]
                              if is_self_node(d) else list(d.get("escalated") or [])) if up else []   # D43: pooled for a long chat, not chosen
            n["card_mb"] = int(best_case_mem_mb(d))                                    # what its own card can hold when idle
            n["pooled"] = ({os.path.basename(e["model"]): list(e.get("nodes") or []) for e in _pool_alive().values() if e.get("shards")}
                           if is_self_node(d) else dict(d.get("pooled") or {})) if up else {}
            n["shard_held_mb"] = int(_shard_held_mb(d)) if up else 0                  # memory parked here by another host's pooled resident
            n["held_mb"] = (int(_pool_held_mb()) if is_self_node(d) else max(0, int(best_case_mem_mb(d) - free_mem_mb(d)))) if up else 0   # what its own warm models take (the Pool bars)
            n["state"] = "DOWN" if not up else ("PINNED" if d.get("pinned") or _shard_held_mb(d) > 0 else ("IN" if in_plan else ("HELD" if held else ("AWAY" if d.get("away") else ("FOREIGN" if foreign else "IDLE")))))
            if up:
                n["thrput"] = f"{donor_tps(d):.0f} t/s"
                n["free"] = f"{free_mem_mb(d)/1024:.1f} GB"
            if in_plan:
                n["share"] = f"{free_mem_mb(d)/cap_tot*100:.0f}%"
                n["why"] = "anchor" if d["id"] == anchor else "in plan"
            elif up and _shard_held_mb(d) > 0:
                who = ", ".join(f"{k} ({v.get('mb',0)/1024:.1f} GB)" for k, v in (d.get("shard_held") or {}).items())
                n["why"] = f"holding a shard of a pooled warm model for {who}"
            elif held and up:
                n["why"] = "lend off — the owner keeps this GPU (still a host for its own work)"
            elif d.get("away") and up:
                n["why"] = "away — a host over Tailscale, not a donor (no RPC across the internet)"
            elif foreign and up:
                n["why"] = f"{d.get('host', 'another host')}'s own anchor — not usable from here (no RPC endpoint)"
            elif up:
                n["why"] = "idle (benched)"
            else:
                n["why"] = "no comms"
        # Deploy observability: a node's self-reported build stamp, surfaced in its WHY so a stale
        # client is visible at a glance everywhere the fabric renders (measure-don't-assume for code).
        # The COORDINATOR node runs THIS program, so stamp its build directly (it never self-reports).
        if n.get("warm"):                                   # D38: what this host holds warm, visible in every fabric view
            n["why"] = (n["why"] + " · " if n["why"] else "") + "warm: " + ", ".join(_tiny_model(m) for m in n["warm"])
        if is_self_node(d) and gpu_problem:                 # render-group bug: say it where the eye lands, not in a log
            n["why"] = "⚠ " + gpu_problem + (" · " + n["why"] if n["why"] else "")
        n["ver"] = COORD_VERSION if d["id"] == coord_id else d.get("app_version", "")
        if n["ver"] and up:
            n["why"] = (n["why"] + " · " if n["why"] else "") + f"app {n['ver']}"
        nodes.append(n)
    up_n = sum(1 for d in fleet["donors"] if d.get("status") == "up")
    return {"coordinator": fleet.get("coordinator", {}).get("host", "?"),
            "mode": SERVE_MODE, "authority": AUTHORITY_BASE,          # D30: who owns the fleet this view came from
            "coord_version": COORD_VERSION,
            "gpu_access": gpu_problem,                               # "" = fine; else why this host has no GPU (watchdog reads it)
            "goal": goal, "model": os.path.basename(active_model()), "model_mb": round(model_mem_mb()),
            "fits": fits, "up": up_n, "total": len(fleet["donors"]), "nodes": nodes,
            "generated": datetime.datetime.now().strftime("%H:%M:%S")}


def _tiny_model(name):
    """'Qwen2.5-32B-Instruct-Q4_K_M.gguf' -> 'Qwen2.5-32B' — the family + size is what a glance needs."""
    import re
    return re.sub(r"[-_](instruct|it|chat|q\d.*|\.gguf).*$", "", str(name), flags=re.I) or str(name)


def fabric_text(st):
    """Tab-delimited, line-based rendering of the fabric snapshot — trivial for the TV app to parse
    (split on newlines then tabs; no JSON dependency on the Tizen side)."""
    lines = ["FAB\t{coordinator}\t{goal}\t{model}\t{up}/{total}\t{fits}\t{generated}".format(**st)]
    for n in st["nodes"]:
        lines.append("NODE\t{id}\t{state}\t{thrput}\t{free}\t{share}\t{reliab}\t{why}".format(**n))
    return "\n".join(lines)


def metrics_text(fleet):
    """Prometheus text-exposition metrics for the fleet — scrape with Prometheus, graph in Grafana,
    or watch with any HTTP monitor. Refreshes liveness (heartbeat) on each scrape."""
    heartbeat(fleet, persist=False)
    cfg = load_config()
    donors = fleet.get("donors", [])

    def esc(s):
        return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    def lbls(d):
        role = cfg["roles"].get(d["id"]) or d.get("role", "compute")
        name = cfg["names"].get(d["id"]) or d["id"]
        return f'id="{esc(d["id"])}",name="{esc(name)}",role="{esc(role)}"'

    def is_compute(d):
        return (cfg["roles"].get(d["id"]) or d.get("role", "compute")) not in ("surface", "storage")

    out = []
    def block(name, help_, typ="gauge"):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {typ}")

    block("genghis_build_info", "Coordinator build/version (always 1; read the label).")
    out.append(f'genghis_build_info{{version="{esc(COORD_VERSION)}"}} 1')
    block("genghis_nodes_up", "Fleet nodes currently up.")
    out.append(f'genghis_nodes_up {sum(1 for d in donors if d.get("status") == "up")}')
    block("genghis_nodes_total", "Total nodes registered in the fleet.")
    out.append(f"genghis_nodes_total {len(donors)}")

    block("genghis_node_up", "Node liveness (1=up, 0=down).")
    for d in donors:
        out.append(f'genghis_node_up{{{lbls(d)}}} {1 if d.get("status") == "up" else 0}')
    block("genghis_node_reliability", "Rolling reliability EMA (0..1).")
    for d in donors:
        out.append(f'genghis_node_reliability{{{lbls(d)}}} {d.get("reliability", 1.0):.4f}')
    block("genghis_node_latency_ms", "Observed latency to the orchestrator (ms).")
    for d in donors:
        lat = d.get("latency_ms_to_orchestrator")
        if isinstance(lat, (int, float)):
            out.append(f'genghis_node_latency_ms{{{lbls(d)}}} {lat:.1f}')
    block("genghis_node_throughput_tps", "Per-node throughput EMA (tokens/sec); compute nodes.")
    for d in donors:
        if is_compute(d) and d.get("status") == "up":
            out.append(f'genghis_node_throughput_tps{{{lbls(d)}}} {donor_tps(d):.2f}')
    block("genghis_node_free_mb", "Usable memory for a shard (MB); compute nodes.")
    for d in donors:
        if is_compute(d) and d.get("status") == "up":
            out.append(f'genghis_node_free_mb{{{lbls(d)}}} {free_mem_mb(d):.0f}')

    local = list_local_models()
    block("genghis_repo_models_count", "Models staged in the coordinator repo.")
    out.append(f"genghis_repo_models_count {len(local)}")
    block("genghis_repo_model_bytes", "Size of each staged repo model (bytes).")
    for name, mb in local:
        out.append(f'genghis_repo_model_bytes{{name="{esc(name)}"}} {int(mb * 1024 * 1024)}')
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HEARTH presence — the agent-authored human message for the TV's HEARTH view.
# Not fleet telemetry: a warm word on the household's screen. Backed by a plain-text
# file (`hearth_message.txt` next to the coordinator) the agent writes over SSH.
# Format: optional first line "# <headline>" = the big title; "?? <question>" = a prompt to the
# viewer; "= <choice>" lines = answer buttons (listen-back); everything else = the body.
# ---------------------------------------------------------------------------
HEARTH_MSG       = os.path.join(HERE, "hearth_message.txt")
HEARTH_REPLY     = os.path.join(HERE, "hearth_reply.txt")     # latest human reply — the agent reads this
HEARTH_REPLY_LOG = os.path.join(HERE, "hearth_replies.log")   # append-only reply history


def hearth_message():
    d = {"title": "", "body": "", "updated": "", "question": "", "choices": []}
    try:
        with open(HEARTH_MSG, encoding="utf-8") as f:
            raw = f.read().replace("\r\n", "\n").rstrip("\n")
        rows = raw.split("\n")
        if rows and rows[0].startswith("# "):
            d["title"] = rows[0][2:].strip(); rows = rows[1:]
        body_lines = []
        for row in rows:
            if row.startswith("?? "):
                d["question"] = row[3:].strip()
            elif row.startswith("= "):
                d["choices"].append(row[2:].strip())
            else:
                body_lines.append(row)
        d["body"] = "\n".join(body_lines).strip("\n")
        d["updated"] = datetime.datetime.fromtimestamp(os.path.getmtime(HEARTH_MSG)).strftime("%H:%M")
    except FileNotFoundError:
        d["title"], d["body"] = "the hearth is lit", "No word yet — but the fire is warm and the screen is listening."
    return d


def hearth_text(m):
    """Wire format for the TV (no JSON dep on the Tizen side):
      line 0 : HEARTH\\t<title>\\t<updated>\\t<question>
      next   : zero+ 'CHOICE\\t<text>' lines (the answer buttons)
      then   : a blank line, then the body (multi-line)."""
    lines = ["HEARTH\t{title}\t{updated}\t{question}".format(**m)]
    for c in m.get("choices", []):
        lines.append("CHOICE\t" + c)
    lines.append("")
    lines.append(m["body"])
    return "\n".join(lines)


def record_reply(data):
    """Listen-back: record a human's answer from the HEARTH TV. Writes the latest to
    hearth_reply.txt (the agent reads this) and appends to hearth_replies.log."""
    choice = (data.get("choice") or "").strip()
    if not choice:
        return False
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    line = "{}\t{}\t{}\t{}".format(ts, (data.get("from") or "tv"),
                                   (data.get("question") or "").strip(), choice)
    try:
        with open(HEARTH_REPLY, "w", encoding="utf-8") as f:
            f.write(line + "\n")
        with open(HEARTH_REPLY_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        return False


def read_replies(limit=100):
    """The HEARTH check-in history — raw tab lines 'ts\\t from\\t question\\t choice', oldest→newest.
    A check-in is a record over time, not a one-off: this is the longitudinal trail a caregiver reviews."""
    try:
        with open(HEARTH_REPLY_LOG, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        return "\n".join(lines[-limit:])
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# System config — HUMAN-set settings, held on the coordinator (the authority), served + edited over HTTP.
# The counterpart to fleet.json: fleet.json is *measured* node state (self-reported); config.json is what a
# person chose (friendly names, roles, default goal). The (coming) web admin reads/writes this via /config.
# ---------------------------------------------------------------------------
CONFIG      = os.path.join(HERE, "config.json")
CONFIG_KEYS = ("default_goal", "model", "names", "roles", "hearth", "auth",
               "goal_models", "residency", "models_dirs",   # editable via POST /config (D23/D24 add the last three)
               "formations")                                # D39: named warm layouts {name: [{model, node|pooled}]}


def load_config():
    try:
        c = _load_json_resilient(CONFIG, what="config.json")   # torn -> .bak, never a silent {} that drops your settings
    except (FileNotFoundError, SystemExit):
        c = {}
    except ValueError:
        c = {}
    c.setdefault("default_goal", "fastest")
    for k in ("names", "roles", "hearth"):
        if not isinstance(c.get(k), dict):
            c[k] = {}
    # auth: an OPTIONAL shared PIN. token="" => open (default, LAN-trusting). protect levels:
    #   off    – no auth ever · writes – gate POST /config only (default once a PIN is set; TV/donors/reads
    #   stay open) · all – gate every endpoint except the admin login page (the off-LAN posture).
    a = c.get("auth")
    if not isinstance(a, dict):
        a = {}
    a.setdefault("token", "")
    a.setdefault("protect", "writes")
    # D25 role layer: a front proxy (Authentik / oauth2-proxy / Cloudflare Access / Azure App Proxy) injects
    # the signed-in identity + groups; we only READ them and map groups -> a role. No proxy in front (no
    # groups header) => local_role (single-user LAN trust = admin), so the home box is unchanged.
    a.setdefault("user_header", "X-Auth-Request-User")
    a.setdefault("groups_header", "X-Auth-Request-Groups")
    a.setdefault("roles_by_group", {})     # {"<azure group GUID / ldap group>": "viewer"|"operator"|"admin"}
    a.setdefault("roles_by_user", {})      # {"<email>": role} — the clean fit for Cloudflare Access (reliable email)
    a.setdefault("default_role", "viewer")  # authenticated but no matching group/user
    a.setdefault("local_role", "admin")     # no proxy at all (direct LAN) — unchanged home behavior
    a.setdefault("proxy_secret", "")        # D25 slice 3: set => "proxy mode": trust identity headers ONLY when
                                            # the request also carries X-Genghis-Proxy==secret (the proxy sets it).
    c["auth"] = a
    if SERVE_MODE == "host" and AUTHORITY_BASE:
        # D30: names, roles, default goal, goal->model map, default model, hearth: the AUTHORITY's, so every
        # Control Room shows the same fleet and every /v1 host routes goals the same way. Per-box keys
        # (_LOCAL_HOST_KEYS: residency, context, model dirs, auth) stay local. Unreachable -> local copy.
        try:
            r = _remote_json(AUTHORITY_BASE + "/config.json", ttl=10.0)
            for k, v in r.items():
                if k in _LOCAL_HOST_KEYS or k.startswith("_"):
                    continue
                c[k] = v
        except Exception:
            pass
    return c


def public_config(c=None):
    """Config safe to SERVE — the auth token is NEVER exposed (only whether one is set)."""
    c = c or load_config()
    a = c.get("auth", {})
    pub = {k: v for k, v in c.items() if k != "auth"}
    pub["auth"] = {"protect": a.get("protect", "writes"), "locked": bool(a.get("token"))}
    return pub


def _req_token(headers, qs):
    """Extract a caller's token: `X-Genghis-Token` header, `Authorization: Bearer`, or `?token=`."""
    t = headers.get("X-Genghis-Token")
    if not t:
        auth = headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            t = auth[7:].strip()
    if not t and qs:
        t = qs.get("token", [""])[0] or ""
    return t or ""


def _auth_denied(path, method, headers, qs):
    """None if the request is allowed; True if it must be rejected (401). Honors the config protect level:
    off -> never; writes -> only POST /config; all -> everything but the admin HTML shell."""
    a = load_config().get("auth", {})
    token = a.get("token") or ""
    if not token or a.get("protect") == "off":
        return None
    if a.get("protect") == "all":
        need = path not in ("/", "/admin")     # the login page loads; its data/actions need the token
    else:                                       # "writes": guard only the dangerous mutation
        need = (method == "POST" and path == "/config")
    if not need:
        return None
    return _req_token(headers, qs) != token


# --- D25: group -> role (viewer < operator < admin) ---------------------------------------------------------
ROLES = ("viewer", "operator", "admin")
def _role_rank(r): return ROLES.index(r) if r in ROLES else -1

def request_identity(headers):
    """(user, groups[], role) for a request, from proxy-injected headers (D25). Two modes:
      • PROXY MODE  (auth.proxy_secret set): identity headers are trusted ONLY when the request also carries
        X-Genghis-Proxy == proxy_secret (the front proxy sets it). A direct-LAN hit that lacks it — a spoof or
        a bypass — gets default_role, never admin. Role = highest of roles_by_group[group] / roles_by_user[email].
      • LOCAL MODE  (no proxy_secret, the home default): no proxy header => local_role (admin), unchanged."""
    a = load_config().get("auth", {})
    uh = a.get("user_header") or "X-Auth-Request-User"
    gh = a.get("groups_header") or "X-Auth-Request-Groups"
    secret = a.get("proxy_secret") or ""
    if secret and headers.get("X-Genghis-Proxy") != secret:
        return (None, [], a.get("default_role", "viewer"))   # not through the trusted proxy -> untrusted
    user = headers.get(uh)
    raw = headers.get(gh)
    if not secret and raw is None:
        return (user, [], a.get("local_role", "admin"))      # direct LAN, no proxy at all -> home default
    groups = [g.strip() for g in re.split(r"[,;]", raw or "") if g.strip()]
    role = None
    def _bump(r):
        nonlocal role
        if r and (role is None or _role_rank(r) > _role_rank(role)): role = r
    for g in groups:                                          # highest role any of the user's groups grants
        _bump((a.get("roles_by_group") or {}).get(g))
    if user:                                                  # or a direct email->role (Cloudflare Access)
        _bump((a.get("roles_by_user") or {}).get(user))
    return (user, groups, role or a.get("default_role", "viewer"))

def request_role(headers):
    return request_identity(headers)[2]

def has_role(headers, need):
    return _role_rank(request_role(headers)) >= _role_rank(need)


def update_config(data):
    """Merge whitelisted keys into config.json — dicts (names/roles/hearth) merge by sub-key; scalars replace."""
    c = load_config()
    for k in CONFIG_KEYS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(c.get(k), dict) and isinstance(v, dict):
            for kk, vv in v.items():
                if vv in ("", None):
                    c[k].pop(kk, None)      # "" = clear this override (keeps config.json tidy)
                else:
                    c[k][kk] = vv
        else:
            c[k] = v
    c["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    atomic_json_write(CONFIG, c)
    return c


def config_text(c):
    """Tab-delimited config for surfaces with no JSON parser (the HEARTH TV). One record per line:
       GOAL\t<default_goal>            — the fleet default run-type
       NAME\t<node_id>\t<friendly>     — one per named node (a surface picks out its own id)
       HSET\t<key>\t<value>            — hearth settings (e.g. start_view, refresh_ms)"""
    lines = ["GOAL\t" + str(c.get("default_goal", "fastest"))]
    for nid, nm in (c.get("names") or {}).items():
        if nm:
            lines.append(f"NAME\t{nid}\t{nm}")
    for k, v in (c.get("hearth") or {}).items():
        lines.append(f"HSET\t{k}\t{v}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-reported capacity (M16 backlog "measure, don't assume"). A node POSTs its own
# live capacity to /report; the coordinator merges the whitelisted fields into fleet.json
# for that id. First user: the TV STORE node reporting its USB free/total (the retail TV's
# shell + dlog are muted, so on-device code reporting over the network is the only way to
# measure that drive). LAN-only, no auth — a home fabric; only known fields on a known id.
# ---------------------------------------------------------------------------
REPORTABLE = ("disk_free_gb", "disk_total_gb", "ram_free_mb", "ram_total_mb", "vram_free_mb",
              "warm",                             # models this host holds warm (D38 pool), most recent first
              "pooled",                           # model -> [nodes] for residents whose shards sit on other nodes (D39)
              "escalated",                        # of those, the ones a long chat asked for (D43): not a chosen layout
              "usb_mount", "usb_attached", "vram_total_mb", "app_version",
              "tps_ema", "tokens_per_s_solo",   # throughput learning persists to the authority too
              "caps", "micprobe", "audiodevs", "voicetext",   # + text Samsung's voice-to-text fed our field
              "diskbench")   # measured USB write/read throughput (the TV-as-STORE re-benchmark)


def update_donor_report(nid, data):
    """Merge a node's self-reported capacity into fleet.json. Returns True if the id was found."""
    if not nid:
        return False
    fleet = load_fleet()
    found = False
    now_iso = datetime.datetime.now().isoformat(timespec="seconds")
    shards = data.get("shards")
    for d in fleet.get("donors", []):
        if d.get("id") == nid:
            found = True
            for k in REPORTABLE:
                v = data.get(k)
                if v is not None:
                    d[k] = v
            d["last_reported"] = now_iso
            d.pop("disk_free_stale", None)   # measured now — the stale flag no longer applies
        if isinstance(shards, dict):
            # D39: the reporter's pooled residents hold `shards[nid]` MB on other donors. Rewrite this reporter's
            # entry on every donor (absent = released) so free_mem_mb() stops double-booking a card holding a shard.
            held = d.get("shard_held") if isinstance(d.get("shard_held"), dict) else {}
            held.pop(nid, None)
            if d.get("id") in shards and shards[d["id"]]:
                held[nid] = {"mb": int(shards[d["id"]]), "ts": now_iso}
            if held: d["shard_held"] = held
            else: d.pop("shard_held", None)
    if found:
        save_fleet(fleet)
    return found


# The web admin — a single self-contained vanilla-JS page (no framework, no CDN, no build step) served
# by the same stdlib HTTP server. Featherweight by design: the Pi ships ~9 KB of static text once; the
# BROWSER does all the work (fetch /fabric.json + /config.json, POST /config). Keeps the coordinator's
# memory/CPU footprint essentially unchanged — the whole point of not adding Flask/Node.
ADMIN_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GENGHIS · Fleet Admin</title>
<style>
  :root{ --bg:#0a0b0f; --panel:#12141b; --line:#232634; --ink:#e7ebf2; --dim:#8b93a3;
         --ember:#ff8c34; --gold:#ffcf76; --cyan:#4dccff; --green:#67e58c; --amber:#ffcc4d;
         --red:#ff6b6b; --magenta:#d982ff; }
  *{box-sizing:border-box} html,body{margin:0}
  body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
       -webkit-font-smoothing:antialiased}
  .wrap{max-width:1000px;margin:0 auto;padding:24px 18px 60px}
  header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
  h1{font-size:22px;margin:0;color:var(--gold);font-weight:650;letter-spacing:.2px}
  h1 .mark{color:var(--ember)}
  .sub{color:var(--dim);font-size:13px}
  .bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:18px 0;padding:14px 16px;
       background:var(--panel);border:1px solid var(--line);border-radius:12px}
  .bar label{color:var(--dim);font-size:13px;margin-right:6px}
  select,input{background:#0e1017;color:var(--ink);border:1px solid var(--line);border-radius:8px;
       padding:8px 10px;font:inherit}
  input:focus,select:focus{outline:none;border-color:var(--ember)}
  button{background:var(--ember);color:#1a1206;border:0;border-radius:8px;padding:9px 16px;font:inherit;
       font-weight:650;cursor:pointer}
  button:disabled{background:#3a3730;color:#8a8577;cursor:default}
  button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line);font-weight:500}
  .spacer{flex:1}
  #saved{color:var(--green);font-size:13px;opacity:0;transition:opacity .3s}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
       border-radius:12px;overflow:hidden}
  th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}
  th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
  tr:last-child td{border-bottom:0}
  td .id{color:var(--dim);font-size:12px;font-family:ui-monospace,monospace}
  td input.name{width:100%;min-width:120px}
  .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:650}
  .st-IN{background:rgba(103,229,140,.15);color:var(--green)}
  .st-IDLE{background:rgba(255,204,77,.15);color:var(--amber)}
  .st-DOWN{background:rgba(255,107,107,.15);color:var(--red)}
  .st-STORE{background:rgba(77,204,255,.15);color:var(--cyan)}
  .st-SURF{background:rgba(217,130,255,.15);color:var(--magenta)}
  .why{color:var(--dim);font-size:12px}
  .num{font-variant-numeric:tabular-nums;color:var(--ink)}
  .foot{color:var(--dim);font-size:12px;margin-top:16px;text-align:center}
  .err{color:var(--red)}
  @media(max-width:640px){ .hide-sm{display:none} .wrap{padding:16px 10px 50px} }
</style></head>
<body><div class="wrap">
  <header>
    <h1><span class="mark">&#9672;</span> GENGHIS &middot; Fleet Admin</h1>
    <span class="sub" id="head">connecting&hellip;</span>
  </header>

  <div class="bar">
    <label for="goal">Default run-type</label>
    <select id="goal">
      <option value="fastest">fastest</option>
      <option value="balanced">balanced</option>
      <option value="fit">fit</option>
      <option value="biggest">biggest</option>
    </select>
    <span class="sub hide-sm">&mdash; what a run picks when no goal is given (the TV can still override live)</span>
    <span class="spacer"></span>
    <button class="ghost" id="pin" title="Set or change the admin PIN">🔒 PIN</button>
    <span id="saved">saved &#10003;</span>
    <button id="save">Save changes</button>
    <button class="ghost" id="revert">Revert</button>
  </div>

  <table>
    <thead><tr>
      <th>Name</th><th>State</th><th>Role</th>
      <th class="hide-sm">Throughput</th><th class="hide-sm">Free</th><th class="hide-sm">Notes</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="6" class="sub">loading the fleet&hellip;</td></tr></tbody>
  </table>

  <div class="foot" id="foot"></div>
</div>
<script>
const $ = s => document.querySelector(s);
let CFG = {names:{}, roles:{}, default_goal:"fastest"};
let dirty = false;
let ROWIDS = "";                 // the node-id set currently rendered; a full re-render only when it changes
const ROLES = ["compute","storage","surface"];

// Optional admin PIN (for a locked coordinator). Stored per-browser; sent as X-Genghis-Token.
let TOKEN = ""; try{ TOKEN = localStorage.getItem("genghis_token") || ""; }catch(e){}
function setToken(t){ TOKEN = t||""; try{ t ? localStorage.setItem("genghis_token",t) : localStorage.removeItem("genghis_token"); }catch(e){} }
function hdrs(extra){ const h = Object.assign({}, extra||{}); if(TOKEN) h["X-Genghis-Token"]=TOKEN; return h; }
// fetch that, on 401, asks for the PIN once and retries — so a locked coordinator prompts to unlock.
async function authFetch(path, opts){
  opts = opts||{}; opts.headers = hdrs(opts.headers);
  let r = await fetch(path, opts);
  if(r.status===401){
    const pin = prompt("This coordinator is locked. Enter the admin PIN:");
    if(pin===null) return r;
    setToken(pin); opts.headers = hdrs(opts.headers); r = await fetch(path, opts);
    if(r.status===401) setToken("");     // wrong PIN — forget it
  }
  return r;
}

function markDirty(){ dirty = true; $("#save").disabled = false; $("#revert").disabled = false; }
// True while the user is interacting with any field — NEVER re-render the rows under them (fixes the
// focus-vs-refresh race: recreating inputs every 5s would drop focus / interrupt typing).
function editing(){ const a=document.activeElement; return a && (a===$("#goal") || $("#rows").contains(a)); }

async function loadAll(){
  try{
    const [fbr, cfr] = await Promise.all([
      authFetch("/fabric.json",{cache:"no-store"}),
      authFetch("/config.json",{cache:"no-store"})
    ]);
    if(fbr.status===401 || cfr.status===401){ $("#head").innerHTML='<span class="err">🔒 locked — enter the admin PIN (🔒 PIN button)</span>'; return; }
    const fb = await fbr.json(), cf = await cfr.json();
    CFG = {names: cf.names||{}, roles: cf.roles||{}, default_goal: cf.default_goal||"fastest"};
    const auth = cf.auth||{}; $("#pin").textContent = auth.locked ? "🔒 PIN" : "🔓 PIN";
    $("#pin").title = auth.locked ? ("locked — protect: "+(auth.protect||"writes")) : "no PIN set — click to add one";
    // Full re-render ONLY when the node set changes AND nothing is being edited; otherwise just refresh
    // the live status cells in place — so existing inputs (and focus, and unsaved edits) are never clobbered.
    const key = fb.nodes.map(n=>n.id).join(",");
    if(!dirty && !editing() && key !== ROWIDS){ ROWIDS = key; $("#goal").value = CFG.default_goal; renderRows(fb); }
    else { updateStatus(fb); }
    const ver = fb.coord_version ? ` &middot; <span class="sub">${fb.coord_version}</span>` : "";
    $("#head").innerHTML = `on <b>${fb.coordinator}</b>${ver} &middot; ${fb.up}/${fb.total} up &middot; model ${fb.model} (${fb.model_mb} MB) &middot; ${fb.fits?'<span style="color:var(--green)">fits</span>':'<span class="err">won\'t fit</span>'}`;
    $("#foot").textContent = "live · updated "+fb.generated+" · auto-refreshes every 5s";
  }catch(e){ $("#head").innerHTML = '<span class="err">coordinator unreachable &mdash; '+e+'</span>'; }
}

function renderRows(fb){
  const tb = $("#rows"); tb.innerHTML = "";
  for(const n of fb.nodes){
    const tr = document.createElement("tr"); tr.dataset.id = n.id; tr.dataset.role0 = n.role;
    const roleOpts = ROLES.map(r=>`<option value="${r}"${r===n.role?" selected":""}>${r}</option>`).join("");
    tr.innerHTML =
      `<td><input class="name" value="${(n.name||n.id).replace(/"/g,'&quot;')}"><div class="id">${n.id}</div></td>
       <td><span class="badge st-${n.state}" data-cell="state">${n.state}</span></td>
       <td><select class="role">${roleOpts}</select></td>
       <td class="num hide-sm" data-cell="thrput">${n.thrput||""}</td>
       <td class="num hide-sm" data-cell="free">${n.free||""}</td>
       <td class="why hide-sm" data-cell="why">${n.why||""}</td>`;
    tr.querySelector(".name").addEventListener("input", markDirty);
    tr.querySelector(".role").addEventListener("change", markDirty);
    tb.appendChild(tr);
  }
}

// Refresh only the live-status cells so we never clobber a name/role the user is editing.
function updateStatus(fb){
  const byId = {}; fb.nodes.forEach(n=>byId[n.id]=n);
  for(const tr of $("#rows").children){
    const n = byId[tr.dataset.id]; if(!n) continue;
    const b = tr.querySelector('[data-cell="state"]'); b.className="badge st-"+n.state; b.textContent=n.state;
    tr.querySelector('[data-cell="thrput"]').textContent = n.thrput||"";
    tr.querySelector('[data-cell="free"]').textContent = n.free||"";
    tr.querySelector('[data-cell="why"]').textContent = n.why||"";
  }
}

$("#goal").addEventListener("change", markDirty);

$("#save").addEventListener("click", async ()=>{
  const names={}, roles={};
  for(const tr of $("#rows").children){
    const id = tr.dataset.id;
    const nm = tr.querySelector(".name").value.trim();
    if(nm && nm!==id) names[id]=nm; else names[id]="";   // "" clears an override back to the raw id
    const rv = tr.querySelector(".role").value;
    if(rv !== tr.dataset.role0) roles[id]=rv;            // persist only roles the user actually changed
  }
  const payload = {default_goal: $("#goal").value, names, roles};
  $("#save").disabled = true;
  try{
    const r = await authFetch("/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    if(r.status===401){ $("#save").disabled=false; alert("locked — the admin PIN is required to save"); return; }
    const j = await r.json();
    if(j.ok){ dirty=false; $("#revert").disabled=true; ROWIDS="";   // force one clean re-render to canonical state
      const s=$("#saved"); s.style.opacity=1; setTimeout(()=>s.style.opacity=0,1600); loadAll(); }
    else { $("#save").disabled=false; alert("save failed"); }
  }catch(e){ $("#save").disabled=false; alert("save failed: "+e); }
});

// Set / change / clear the admin PIN. Writing auth requires the CURRENT PIN if one is set (authFetch prompts).
$("#pin").addEventListener("click", async ()=>{
  const pin = prompt("Set an admin PIN to lock config changes.\n(Leave blank and OK to REMOVE the PIN.)", "");
  if(pin===null) return;
  const r = await authFetch("/config",{method:"POST",headers:{"Content-Type":"application/json"},
                                        body:JSON.stringify({auth:{token:pin}})});
  if(r.status===401){ alert("wrong current PIN — not changed"); return; }
  const j = await r.json();
  if(j.ok){ setToken(pin);   // remember the new PIN in this browser (blank clears it)
    alert(pin ? "PIN set — config changes now require it." : "PIN removed — config is open again.");
    loadAll(); }
  else alert("could not update the PIN");
});

$("#revert").addEventListener("click", ()=>{ dirty=false; $("#save").disabled=true; $("#revert").disabled=true; ROWIDS=""; loadAll(); });

$("#save").disabled = true; $("#revert").disabled = true;
loadAll();
setInterval(loadAll, 5000);
</script>
</body></html>
"""


def _load_control_html():
    """D24: the Control Room dashboard (served at `/`). Kept in poc/control_room.html for easy iteration;
    read on EVERY request (a cosmetic edit used to need a serve restart, which empties the warm pool -- 2026-09-17). If it's missing, fall back to a minimal page that still links onward — the coordinator
    must never 500 just because the template file didn't ship."""
    try:
        with open(os.path.join(HERE, "control_room.html"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><title>GENGHIS</title>"
                "<body style='font-family:system-ui;max-width:640px;margin:60px auto;padding:0 20px'>"
                "<h1>GENGHIS</h1><p>Control Room template not found. The fleet admin still works: "
                "<a href='/admin'>/admin</a>. Live fleet JSON: <a href='/fabric.json'>/fabric.json</a>.</p>")

CONTROL_HTML = _load_control_html()

# D25 slice 1: a concise, served reference for the coordinator's HTTP API (the Control Room "Coordinator API"
# card links here). Self-contained; same palette as the Control Room.
API_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>GENGHIS API</title>
<style>
:root{--bg:#EEF1F5;--panel:#FFF;--line:#D7DEE6;--text:#18202A;--muted:#586878;--faint:#8494A4;--ember:#B96E10;--steel:#2A6FB0;--ok:#2E9E58;--mono:"IBM Plex Mono",ui-monospace,Consolas,monospace;--sans:"IBM Plex Sans",system-ui,Segoe UI,sans-serif}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0C1016;--panel:#141A22;--line:#242E3A;--text:#E6EDF3;--muted:#93A1B0;--faint:#657686;--ember:#E8A33D;--steel:#6BB4EE;--ok:#56D07E}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.55}
.wrap{max-width:900px;margin:0 auto;padding:30px 20px 60px}a{color:var(--steel);text-decoration:none}
h1{font-size:24px;margin:0 0 4px}.lede{color:var(--muted);margin:0 0 8px}.back{font-family:var(--mono);font-size:12px}
.sect{font-family:var(--mono);font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--faint);margin:26px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.ep{border:1px solid var(--line);border-radius:10px;background:var(--panel);padding:13px 15px;margin:9px 0}
.ep .m{font-family:var(--mono);font-size:11px;font-weight:700;padding:2px 7px;border-radius:5px;margin-right:8px}
.get{color:var(--ok);border:1px solid color-mix(in srgb,var(--ok) 45%,var(--line))}
.post{color:var(--ember);border:1px solid color-mix(in srgb,var(--ember) 45%,var(--line))}
.ep code{font-family:var(--mono);font-size:13.5px;color:var(--text)}.ep p{margin:8px 0 0;color:var(--muted);font-size:13px}
pre{font-family:var(--mono);font-size:12.5px;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px 14px;overflow-x:auto}
</style></head><body><div class=wrap>
<p class=back><a href="/">&larr; Control Room</a></p>
<h1>GENGHIS Coordinator API</h1>
<p class=lede>OpenAI-compatible inference + a small native control plane. Point any OpenAI client's <code>base_url</code> at <code>/v1</code>.</p>

<div class=sect>Inference (OpenAI-compatible)</div>
<div class=ep><span class="m get">GET</span><code>/v1/models</code><p>Lists the four effort goals (<code>genghis-fastest|balanced|fit|biggest</code>) plus every model in your folder (D23). Point Open WebUI / the openai SDK here.</p></div>
<div class=ep><span class="m post">POST</span><code>/v1/chat/completions</code><p>Standard chat completion (streaming + non-streaming). <code>"model"</code> may be a goal or a concrete model name. Warm models answer instantly (D23 residency).</p></div>
<pre>curl http://HOST:8899/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"genghis-fastest","messages":[{"role":"user","content":"hi"}]}'</pre>

<div class=sect>Fleet &amp; models</div>
<div class=ep><span class="m get">GET</span><code>/fabric.json</code><p>Live fleet: nodes, state, throughput, free memory, the active goal + model. (<code>/fabric</code> = tab-text.)</p></div>
<div class=ep><span class="m get">GET</span><code>/registry.json</code><p>Local models + the goal&rarr;model map + which model is warm right now.</p></div>
<div class=ep><span class="m post">POST</span><code>/residency</code> <span style="color:var(--faint);font-family:var(--mono);font-size:11px">operator</span><p><code>{"action":"load","model":"&lt;id&gt;"}</code> pre-warms a model; <code>{"action":"unload"}</code> frees the VRAM.</p></div>
<div class=ep><span class="m get">GET</span><code>/metrics</code><p>Prometheus exposition — scrape with Prometheus/Grafana.</p></div>
<div class=ep><span class="m get">GET</span><code>/fleet.json</code> &middot; <span class="m get">GET</span><code>/models</code><p>Raw authoritative fleet; the model repository (with HTTP-Range resume).</p></div>

<div class=sect>Config &amp; identity</div>
<div class=ep><span class="m get">GET</span><code>/config.json</code> &middot; <span class="m post">POST</span><code>/config</code> <span style="color:var(--faint);font-family:var(--mono);font-size:11px">operator/admin</span><p>Read/set human config: friendly names, roles, default goal, the goal&rarr;model map. Node/role/goal/auth changes need <b>admin</b>; the goal&rarr;model map needs <b>operator</b> (D25).</p></div>
<div class=ep><span class="m get">GET</span><code>/whoami</code><p>Your forwarded identity + the role it maps to (<code>viewer</code>/<code>operator</code>/<code>admin</code>). Behind a proxy it reflects your Azure/LDAP groups; direct on the LAN it is the local role.</p></div>
<p style="color:var(--faint);font-size:12.5px;margin-top:24px">Full integration notes: <code>INTEGRATION.md</code> in the repo. Auth &amp; roles: <code>DECISIONS.md</code> D25.</p>
</div></body></html>"""


def serve(fleet):
    """HEARTH v1 backend: serve the live fabric snapshot for the TV monitor to poll.
    `/fabric` -> tab-delimited text (default, for the TV); `/fabric.json` -> JSON.
    `/` and `/admin` -> the web admin page (manage/name nodes, set the default goal)."""
    import http.server
    port = int(os.environ.get("GENGHIS_SERVE_PORT", "8899"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def deny(self):
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.send_header("WWW-Authenticate", 'Bearer realm="genghis"')
            self.send_header("Content-Length", "12")
            self.end_headers()
            self.wfile.write(b"unauthorized")

        def forbid(self, need):
            """D25: 403 — authenticated but the caller's role is below what this action needs."""
            body = json.dumps({"ok": False, "err": "forbidden", "need_role": need,
                               "your_role": request_role(self.headers)}).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def forward(self, method, body=None, base=None, timeout=15):
            """D30: an inference host forwards this request verbatim to the authority and relays the answer —
            writes (register / lifecycle / config / report / HEARTH replies) and HEARTH state live there.
            `base` = another host's serve instead (a residency order for THAT card, D38)."""
            url = (base or AUTHORITY_BASE) + self.path
            hdrs = {"Content-Type": self.headers.get("Content-Type", "application/json"), **_auth_headers()}
            for h in ("X-Genghis-Token", "X-Genghis-Proxy", "X-Auth-Request-User", "X-Auth-Request-Groups", "Authorization"):
                if self.headers.get(h):
                    hdrs[h] = self.headers[h]
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    payload = r.read(); status = r.status; ctype = r.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as e:
                payload = e.read(); status = e.code; ctype = e.headers.get("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"ok": False, "note": f"{'host' if base else 'authority'} {base or AUTHORITY_BASE} unreachable: {e}",
                                      "err": f"{base or AUTHORITY_BASE} unreachable: {e}"}).encode("utf-8")
                status, ctype = 502, "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            _req_begin()                                          # D45: this thread's per-request state, clean
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if _auth_denied(path, "GET", self.headers, urllib.parse.parse_qs(parsed.query)):
                self.deny(); return
            # D30: a host has no library of its own and no HEARTH state -- the authority answers these.
            if SERVE_MODE == "host" and (path.startswith("/models/") or path in ("/models", "/replies", "/hearth", "/hearth.json")):
                if path.startswith("/models/"):
                    self.send_response(302); self.send_header("Location", AUTHORITY_BASE + path); self.end_headers(); return
                self.forward("GET"); return
            # Model repository: /models (JSON list) and /models/<name.gguf> (streamed download).
            if path == "/models" or path.startswith("/models/"):
                self.serve_models(path); return
            # Prometheus metrics — scrape with Prometheus/Grafana or any HTTP monitor.
            if path == "/metrics":
                body = metrics_text(load_fleet()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # D25: who am I to this coordinator — the proxy-forwarded identity + the role it maps to.
            if path == "/watchdog.json":
                # The watchdog's latest pass (poc/watchdog.py via cron on the authority). A host forwards to the
                # authority; missing file -> {"ts": null} so the Control Room can say "no watchdog yet".
                if SERVE_MODE == "host":
                    self.forward("GET"); return
                try:
                    with open(os.path.join(HERE, "watchdog.json"), encoding="utf-8") as f:
                        body = f.read().encode("utf-8")
                except OSError:
                    body = b'{"ts": null}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            if path in ("/whoami", "/whoami.json"):
                user, groups, role = request_identity(self.headers)
                body = json.dumps({"user": user, "groups": groups, "role": role,
                                   "roles": list(ROLES)}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # D25 slice 1: a served API reference (the "Coordinator API" card links here).
            if path == "/api":
                body = API_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # D39: the drag preview -- /plan.json?model=<id>[&node=<host>|&pooled=1]
            if path == "/formations.json":
                live = live_layout()
                out = {"formations": formations_list(), "current": current_formation(live), "live": live,
                       "applying": formation_status()}
                body = json.dumps(out).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            if path == "/loading.json":
                # D39 pass 2: the live warm-load progress of THIS host, or of `node` (forwarded -- the Control Room you are
                # on may have dropped a model on another host's card). {"loading": null} when nothing is loading.
                q = urllib.parse.parse_qs(parsed.query); node = (q.get("node") or [""])[0]
                out = {"node": node or SELF_ID or SELF_HOST, "loading": loading_status(), "text": loading_text()}
                if node:
                    d = next((d for d in load_fleet().get("donors", []) if d.get("id") == node), None)
                    if d and not is_self_node(d) and d.get("ip"):
                        try:
                            out = _remote_json(f"http://{d['ip']}:{int(d.get('serve_port') or 8899)}/loading.json", ttl=0.5, timeout=3)
                        except Exception:
                            out = {"node": node, "loading": None, "text": "", "unreachable": True}
                body = json.dumps(out).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            if path == "/plan.json":
                q = urllib.parse.parse_qs(parsed.query)
                res = dry_run_plan((q.get("model") or [""])[0], node=(q.get("node") or [None])[0],
                                   pooled=(q.get("pooled") or ["0"])[0] in ("1", "true"))
                body = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # D49: the adapters this host knows about, whether each is armed, and whether it answers.
            if path in ("/adapters", "/adapters.json"):
                out = {"home": home_dir(), "adapters_dir": adapters_dir(), "adapters": []}
                for aid, a in sorted(load_adapters().items()):
                    ok, detail = adapter_reachable(a) if a["enabled"] else (False, "not enabled")
                    out["adapters"].append({
                        "id": aid, "name": a["name"], "description": a.get("description") or "",
                        "transport": a["transport"], "endpoint": f"{a['host']}:{a['port']}",
                        "enabled": a["enabled"], "reachable": ok, "detail": detail,
                        "operations": sorted((a["operations"] or {}).keys()), "source": a["source"]})
                body = json.dumps(out).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # D48: the roles this host can offer, what each would actually run, and every reason one can't.
            if path in ("/roles", "/roles.json"):
                out = {"home": home_dir(), "roles_dir": roles_dir(), "roles": []}
                for rid, role in sorted(load_roles().items()):
                    res = resolve_role(rid)
                    out["roles"].append({
                        "id": rid, "name": role["name"], "description": role["description"],
                        "goal": role["goal"], "requires": role["requires"], "tools": role["tools"],
                        "knowledge": role["knowledge"],
                        "knowledge_status": "declared (retrieval not built yet)" if role["knowledge"] else None,
                        "voice": role["voice"],
                        "voice_status": "declared (voice service not wired yet)" if role["voice"] else None,
                        "model_id": res.get("model_id"), "substituted": res.get("substituted"),
                        "unmet": res.get("unmet") or [], "problem": res.get("problem"),
                        "ok": not res.get("problem"), "why": role_why(res), "source": role["source"]})
                body = json.dumps(out).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # Control Room model panel (D24): registry + goal->model map + default + what's warm right now.
            if path in ("/registry", "/registry.json"):
                cfg = load_config()
                reg = build_registry()
                def _warm_mb(m):
                    """What the model takes WARM at a comfortable window (16k, or its trained max if smaller): the number
                    the card actually has to find -- Nemotron-4B is 2.6 GB on disk and 7.6 GB warm (its KV is huge)."""
                    try:
                        train = gguf_ctx_train(m["path"]) or 32768
                        return int(m["size_mb"] + kv_cache_mb(min(16384, train), m["path"]) + 512)
                    except Exception:
                        return int(m["size_mb"] * 1.1 + 512)
                out = {"models": [{"id": m["id"], "size_mb": m["size_mb"], "kind": m["kind"], "warm_mb": _warm_mb(m),
                                   "caps": m.get("caps") or {}, "caps_why": m.get("caps_why") or ""} for m in reg],
                       "goals": list(GOALS),
                       "goal_models": cfg.get("goal_models") or {},
                       "default_goal": cfg.get("default_goal", GOAL),
                       "default_model": os.path.basename((cfg.get("model") or "").strip()) if (cfg.get("model") or "").strip() else None,
                       "resident": resident_status(),
                       "fetch": fetch_status()}                       # D28: background model pull, if any
                body = json.dumps(out).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # OpenAI-compatible model list (D17): effort-routing goals exposed as model names, so any
            # OpenAI client (Open WebUI, ComfyUI, LangChain) selects a routing goal by "model".
            if path == "/v1/models":
                now = int(time.time())
                # The four effort GOALS (routing) PLUS every concrete model in the local registry (D23), so an
                # OpenAI client's dropdown can pick a goal OR a specific GGUF by name.
                data = [{"id": f"genghis-{g}", "object": "model", "created": now, "owned_by": "genghis"}
                        for g in GOALS]
                # D48: roles sit beside the goals, so a chat client's dropdown offers "what you want done"
                # (genghis-researcher) as readily as "how hard to try" (genghis-balanced).
                for rid, role in sorted(load_roles().items()):
                    res = resolve_role(rid)
                    data.append({"id": f"genghis-{rid}", "object": "model", "created": now, "owned_by": "genghis",
                                 "genghis": {"kind": "role", "name": role["name"],
                                             "description": role["description"], "goal": role["goal"],
                                             "tools": role["tools"], "runs_on": res.get("model_id"),
                                             "ok": not res.get("problem"), "why": role_why(res)}})
                for m in build_registry():
                    c = m.get("caps") or {}
                    data.append({"id": m["id"], "object": "model", "created": now, "owned_by": "genghis",
                                 # `genghis.caps` is what lets a CLIENT (or a role) ask for a capability
                                 # instead of memorising which GGUF happens to have it.
                                 "genghis": {"kind": m["kind"], "size_mb": m["size_mb"],
                                             "caps": {"tools": c.get("tools"), "tool_results": c.get("tool_results"),
                                                      "vision": c.get("vision"), "reasoning": c.get("reasoning"),
                                                      "ctx_train": c.get("ctx_train")},
                                             "why": m.get("caps_why") or ""}})
                body = json.dumps({"object": "list", "data": data}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            if path not in ("/fabric", "/fabric.json", "/hearth", "/hearth.json", "/fleet.json",
                            "/replies", "/config", "/config.json", "/", "/admin", "/control",
                            "/whoami", "/whoami.json", "/api", "/watchdog.json", "/plan.json", "/loading.json", "/formations.json"):
                self.send_response(404); self.end_headers(); return
            # D24: the Control Room is the main dashboard at `/` (fleet + models, live). The older fleet
            # admin stays at `/admin`. Both are self-contained HTML apps served inline.
            if path in ("/", "/control", "/admin"):
                body = (ADMIN_HTML if path == "/admin" else _load_control_html()).encode("utf-8")   # fresh from disk each time
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # System config (human-set: friendly names, roles, default goal).
            #   /config.json -> JSON (the web admin)   ·   /config -> tab-text (the TV, no JSON dependency)
            if path in ("/config", "/config.json"):
                cfg = public_config()          # token REDACTED — never served
                if path == "/config.json":
                    body = json.dumps(cfg).encode("utf-8"); ctype = "application/json"
                else:
                    body = config_text(cfg).encode("utf-8"); ctype = "text/plain; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # HEARTH check-in history (healthcare): the longitudinal record of questions + answers.
            if path == "/replies":
                body = read_replies().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # Single source of truth: the raw authoritative fleet, for planning clients (the laptop) to fetch.
            if path == "/fleet.json":
                body = json.dumps(load_fleet()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # HEARTH presence view: the agent-authored human message (not fleet telemetry).
            if path in ("/hearth", "/hearth.json"):
                m = hearth_message()
                if path == "/hearth.json":
                    body = json.dumps(m).encode("utf-8"); ctype = "application/json"
                else:
                    body = hearth_text(m).encode("utf-8"); ctype = "text/plain; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # Goal is selectable per request so the HEARTH TV can switch run-types with the remote
            # (up/down arrows): e.g. /fabric?goal=biggest. Unknown/absent -> the server's launch GOAL.
            qs = urllib.parse.parse_qs(parsed.query)
            goal = (qs.get("goal", [""])[0] or "").lower() or None   # ?goal wins; else None -> config default_goal
            if goal and goal not in GOALS:
                goal = None
            st = fabric_state(load_fleet(), goal)
            if path == "/fabric.json":
                body = json.dumps(st).encode("utf-8"); ctype = "application/json"
            else:
                body = fabric_text(st).encode("utf-8"); ctype = "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def serve_models(self, path):
            # /models -> JSON list of the repository's .gguf files (name + size).
            if path == "/models":
                items = [{"name": n, "size_mb": round(mb)} for n, mb in list_local_models()]
                body = json.dumps({"models": items, "dir": MODELS_DIR}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            # /models/<name> -> stream the file. basename-only (no path traversal), .gguf only.
            # Supports HTTP Range so an interrupted big-model transfer (42 GB 70B) resumes instead of
            # restarting from zero: a `Range: bytes=<start>-[end]` request gets a 206 with just that span.
            name = os.path.basename(urllib.parse.unquote(path[len("/models/"):]))
            fp = os.path.join(MODELS_DIR, name)
            if not name.lower().endswith(".gguf") or not os.path.isfile(fp):
                self.send_response(404); self.end_headers(); self.wfile.write(b"not found"); return
            size = os.path.getsize(fp)
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            partial = False
            if rng and rng.strip().lower().startswith("bytes="):
                try:
                    s, _, e = rng.strip()[6:].partition("-")
                    start = int(s) if s else 0
                    end = int(e) if e else size - 1
                    if start < 0 or start >= size or end < start:
                        raise ValueError
                    end = min(end, size - 1)
                    partial = True
                except ValueError:
                    # Unsatisfiable range -> 416 with the current length so the client can recover.
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers(); return
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            remaining = length
            with open(fp, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return          # client cancelled — stop quietly

        def do_POST(self):
            _req_begin()                                          # D45: this thread's per-request state, clean
            # /report = a node self-reports live capacity (measure, don't assume).
            # /reply  = a human answers a HEARTH prompt from the TV (listen-back).
            # /config = the web admin sets human config (friendly names, roles, default goal, auth PIN).
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if _auth_denied(path, "POST", self.headers, urllib.parse.parse_qs(parsed.query)):
                self.deny(); return
            if path not in ("/report", "/reply", "/config", "/residency", "/fleet", "/formations", "/v1/chat/completions"):
                self.send_response(404); self.end_headers(); return
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) if n else b""
                data = json.loads(raw.decode("utf-8")) if n else {}
            except Exception:
                self.send_response(400); self.end_headers(); self.wfile.write(b'{"ok":false,"err":"bad json"}'); return
            # D30: every fleet/config/HEARTH write belongs to the authority; a host relays it (and its outcome).
            # /residency and /v1 are about THIS box's GPU and are handled here.
            if SERVE_MODE == "host" and path in ("/report", "/reply", "/config", "/fleet"):
                _REMOTE_CACHE.clear()                       # our next read must see the write
                self.forward("POST", raw or b"{}"); return
            if SERVE_MODE == "host" and path == "/formations" and (data.get("action") or "") in ("save", "delete"):
                _REMOTE_CACHE.clear()                       # the list lives in the authority's config; plan/apply run from here
                self.forward("POST", raw or b"{}"); return
            # OpenAI-compatible chat completion (D17, slice 1: non-streaming). Runs one completion
            # through the real plan/run path; the "model" selects the effort-routing goal.
            if path == "/v1/chat/completions":
                self.handle_chat_completion(data); return
            # D25 role gate: model/goal + load/unload = operator; node names/roles/default-goal/auth + fleet = admin.
            if path in ("/residency", "/formations") and not has_role(self.headers, "operator"):
                self.forbid("operator"); return
            if path == "/fleet" and not has_role(self.headers, "admin"):
                self.forbid("admin"); return
            if path == "/config":
                need = "admin" if (set(data) - {"goal_models"}) else "operator"
                if not has_role(self.headers, need):
                    self.forbid(need); return
            if path == "/reply":
                ok = record_reply(data)
                result = {"ok": ok}
            elif path == "/fleet":
                # D26 node lifecycle (admin): {"action":"retire|remove|restore","id":"<node>"} on the authority.
                act = (data.get("action") or "").strip()
                if act == "register":
                    ok, note = fleet_ops("register", data.get("node") or {})
                else:
                    ok, note = fleet_ops(act, (data.get("id") or "").strip())
                result = {"ok": ok, "note": note}
            elif path == "/residency":
                # D24 load/unload control: {"action":"unload"} frees the warm model's VRAM;
                # {"action":"load","model":"<id>"} pre-warms a model (solo-capable only);
                # {"action":"unload","model":"<id>"} stops that one warm server, the rest of the pool stays (D38);
                # {"node":"<id>", ...} = do it on THAT host's card: forwarded to its serve, answer relayed.
                node = (data.get("node") or "").strip()
                if node:
                    tgt = next((d for d in load_fleet().get("donors", []) if d.get("id") == node), None)
                    if tgt is None:
                        ok = False; result = {"ok": False, "note": f"no node {node} in the fleet"}
                    elif not is_self_node(tgt):
                        if not tgt.get("local") or not tgt.get("ip"):
                            ok = False; result = {"ok": False, "note": f"{node} runs no serve of its own (not a host)"}
                        else:
                            fwd = {k: v for k, v in data.items() if k != "node"}
                            self.forward("POST", json.dumps(fwd).encode("utf-8"),
                                         base=f"http://{tgt['ip']}:{int(tgt.get('serve_port') or 8899)}", timeout=240)
                            return
                    # else: it's us -- fall through and do it here
                act = (data.get("action") or "").strip()
                if act == "unload" and (data.get("model") or "").strip():
                    ok, note = unload_model(data["model"].strip(), why=(data.get("why") or "unloaded by the operator"))
                    result = {"ok": ok, "note": note, "resident": resident_status()}
                elif act == "unload":
                    stop_resident(); ok = True; result = {"ok": True, "note": "unloaded", "resident": resident_status()}
                elif act == "load":
                    ok, note = warm_model((data.get("model") or "").strip(), pooled=bool(data.get("pooled")))
                    result = {"ok": ok, "note": note, "resident": resident_status()}
                else:
                    ok = False; result = {"ok": False, "note": "action must be 'load' or 'unload'"}
            elif path == "/formations":
                act = (data.get("action") or "").strip(); name = (data.get("name") or "").strip()
                if act == "save" and name:
                    items = [({"model": l["model"], "pooled": True} if l.get("pooled") else {"model": l["model"], "node": l["node"]}) for l in live_layout()]
                    if not items:
                        ok = False; result = {"ok": False, "note": "nothing is warm right now -- place some models first, then save"}
                    else:
                        update_config({"formations": {name: items}}); f = formations_list()
                        ok = True; result = {"ok": True, "note": f"saved {name}: " + "; ".join((f"{_tiny_model(i['model'])} across the fabric" if i.get("pooled") else f"{_tiny_model(i['model'])} on {i['node']}") for i in items), "formations": f}
                elif act == "delete" and name:
                    f = formations_list(); ok = name in f
                    if ok:
                        update_config({"formations": {name: ""}}); f = formations_list()   # "" = clear this key (update_config's rule)
                    result = {"ok": ok, "note": (f"deleted {name}" if ok else f"no formation named {name}"), "formations": f}
                elif act == "plan" and name:
                    steps = formation_plan(name); ok = steps is not None
                    result = {"ok": ok, "steps": steps or [], "note": ("; ".join(st["note"] for st in steps) if steps else ("already in place" if ok else f"no formation named {name}"))}
                elif act == "apply" and name:
                    ok, note = apply_formation(name, int(os.environ.get("GENGHIS_SERVE_PORT", "8899")))
                    result = {"ok": ok, "note": note, "applying": formation_status()}
                else:
                    ok = False; result = {"ok": False, "note": "action must be save | delete | plan | apply, with a name"}
            elif path == "/config":
                update_config(data)
                ok = True; result = {"ok": True, "config": public_config()}   # token REDACTED in the echo
            else:
                nid = data.get("id")
                ok = update_donor_report(nid, data)
                result = {"ok": ok, "id": nid}
            body = json.dumps(result).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_chat_completion(self, data):
            """OpenAI-compatible POST /v1/chat/completions (D17 / INTEGRATION.md).
            The request "model" is resolved by the registry (D23): an effort GOAL (`genghis-<goal>`) uses the
            model mapped to that goal (config `goal_models`, else the default), OR a concrete registry model
            name uses that GGUF directly. `stream:true` -> SSE chunks; else one JSON object."""
            # D48: a ROLE (genghis-<role>) is applied to the request BEFORE anything else looks at it --
            # its system prompt goes in front of the conversation and its resolution is narrated. A role
            # that cannot be honoured on this host refuses here, with the reason, instead of quietly
            # running as a plain chat that looks fine and isn't (D31: never silent).
            # Reset per REQUEST, not per connection: this handler instance is reused for keep-alive, so a
            # plain-goal chat arriving after a role chat would otherwise inherit that role's owned tools and
            # silently execute against them.
            self._role_note = self._role_label = None
            self._owned_tools = {}
            rid = (data.get("model") or "").strip()
            rid = rid[len("genghis-"):] if rid.startswith("genghis-") else ""
            if rid and rid not in GOALS and rid in load_roles():
                res = resolve_role(rid)
                if res.get("problem"):
                    self.v1_error(503, role_why(res), "role_unsatisfiable"); return
                self._role_note  = role_why(res)
                self._role_label = f"genghis-{rid}"
                # A DELEGATED request (D34) already carries the role's system prompt — the host that took the
                # chat applied it before forwarding. Applying it again here would stack a second copy in
                # front of the conversation. Resolution still runs, so this host picks the right model from
                # its OWN library (which is the point of delegating), and still labels the reply as the role.
                if not self.headers.get(DELEGATED_HDR):
                    apply_role_to_request(res, data)
                    self._owned_tools = res.get("owned_tools") or {}
                    if res.get("notes"):
                        print("[role] " + " | ".join(res["notes"]), flush=True)
            # D23: the request "model" may be an effort GOAL (genghis-<goal>) OR a concrete registry model
            # by name. resolve_model() maps it to (GGUF path, goal); set_active_model points the run at it.
            _path, goal = resolve_model(data.get("model"))
            sanitize_tool_args(data.get("messages"))       # a malformed tool-call argument in the history must not 500 every turn
            self._loop_note = tool_loop_guard(data)        # the same tool call N times in a row -> tools off for this turn (read by the stream paths)
            try:
                n = int(data.get("max_tokens") or V1_MAX_TOKENS)
            except (TypeError, ValueError):
                n = V1_MAX_TOKENS
            n = max(1, min(n, V1_HARD_MAX_TOKENS))
            data["max_tokens"] = n                           # EVERY path (warm server, delegate, llama-cli) sees a ceiling: a
                                                             # request without one let a 1.5B run 12,000+ tokens and hold the
                                                             # host's run lock for ten minutes (2026-09-16)
            stream = bool(data.get("stream"))
            if stream:                                       # D31: headers + live status BEFORE any waiting
                self.stream_with_status(goal, data, n, _path); return
            self._engine_held = False
            try:
                self._v1_chat_locked(data, _path, goal, n)
            finally:
                if self._engine_held:
                    _V1_RUN_INFO.clear(); _V1_RUN_LOCK.release()

        def _v1_label(self, goal):
            """What to echo back in the response's `model`. A client that asked for `genghis-researcher`
            must see `genghis-researcher` returned, not the effort goal the role happened to resolve to --
            otherwise a chat client relabels the conversation mid-answer and the role looks like it was
            ignored. D48."""
            return getattr(self, "_role_label", None) or f"genghis-{goal}"

        def _adapter_rounds(self, base, data, first_text):
            """D49: run the model→tool→model loop for the tools GENGHIS OWNS (a role's armed adapters).

            Tools the CLIENT supplied are not ours: if the model calls one of those we stop and hand the
            call back, exactly as before (D37). Ours we execute here, append as `role:"tool"` messages, and
            ask the model again — bounded, and when the bound is hit we say so in the answer instead of
            looping forever or truncating in silence."""
            owned = getattr(self, "_owned_tools", None) or {}
            text  = first_text
            trail = []
            for _ in range(ADAPTER_MAX_ROUNDS):
                msg = _req_get("proxy_message") or {}
                calls = msg.get("tool_calls") or []
                mine = [c for c in calls if ((c.get("function") or {}).get("name")) in owned]
                if not calls or not mine:
                    return text, trail                       # nothing of ours left to do
                if len(mine) != len(calls):
                    return text, trail                       # mixed ours/theirs: let the client resolve it
                hist = data.get("messages") or []
                hist = hist + [{"role": "assistant", "content": msg.get("content") or None,
                                "tool_calls": calls}]
                for c in mine:
                    fn   = c.get("function") or {}
                    name = fn.get("name")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    out = run_adapter_tool(owned, name, args)
                    trail.append(f"{name}({', '.join(f'{k}={v!r}' for k, v in (args or {}).items())[:120]}) -> {out[:200]}")
                    hist.append({"role": "tool", "tool_call_id": c.get("id") or name,
                                 "name": name, "content": out})
                data["messages"] = hist
                _req_set("proxy_reasoning", None); _req_set("proxy_message", None); _req_set("proxy_finish", "stop")
                with _inflight(base):
                    text, _ok = _proxy_resident(base, data, False)
            trail.append(f"stopped after {ADAPTER_MAX_ROUNDS} tool rounds — the model kept asking for more")
            return text, trail

        def _stream_owned_tools(self, base, req, calls, delta, think):
            """D49 on the STREAMING path: the first streamed turn ended asking for tools. Run the ones
            GENGHIS owns, narrate each call and its result in the Thinking panel (the user watches the work
            happen rather than a stalled cursor — D31), then keep streaming the model's next turn.

            Returns (finish_reason, handled). handled=False means these calls were not ours to run, so the
            caller relays them to the client untouched, exactly as D37 did."""
            owned = getattr(self, "_owned_tools", None) or {}
            finish = "tool_calls"
            for _ in range(ADAPTER_MAX_ROUNDS):
                mine = [c for c in calls if ((c.get("function") or {}).get("name")) in owned]
                if not calls or not mine or len(mine) != len(calls):
                    return finish, False          # none of ours, or a mix: the client resolves it
                hist = list(req.get("messages") or [])
                hist.append({"role": "assistant", "content": None, "tool_calls": calls})
                for c in mine:
                    fn   = c.get("function") or {}
                    name = fn.get("name")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    shown = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
                    think(f"  {name}({shown[:110]})\n")
                    out = run_adapter_tool(owned, name, args)
                    think(f"    {out[:200]}\n")
                    hist.append({"role": "tool", "tool_call_id": c.get("id") or name,
                                 "name": name, "content": out})
                req["messages"] = hist
                acc, finish = {}, "stop"
                fg = FenceGuard()
                with _inflight(base):
                    for d in _proxy_resident(base, req, True):
                        rc, c2, tc = d.get("reasoning_content"), d.get("content"), d.get("tool_calls")
                        if rc: delta(reasoning_content=rc)
                        if c2:
                            c2 = fg.feed(c2)
                            if c2: delta(content=c2)
                        if tc: merge_tool_call_deltas(acc, tc)
                        if d.get("_finish"): finish = d["_finish"]
                    tail = fg.finish()
                    if tail: delta(content=tail)
                calls = tool_calls_from(acc)
                if finish != "tool_calls" or not calls:
                    return finish, True
            think(f"  stopped after {ADAPTER_MAX_ROUNDS} tool rounds — the model kept asking for more\n")
            return "stop", True

        def _engine_acquire(self, goal, model_name):
            """Take the per-request ENGINE lock (llama-cli: exclusive GPU + single-client donors) -- blocking. Idempotent
            per request. The warm-server paths never take it (D45)."""
            if getattr(self, "_engine_held", False):
                return
            _V1_RUN_LOCK.acquire()
            self._engine_held = True
            _V1_RUN_INFO.update(goal=goal, model=model_name, since=time.time())

        def _v1_chat_locked(self, data, _path, goal, n):
            """The non-streaming chat body, run under _V1_RUN_LOCK (the planner's globals are ours alone here)."""
            stream = False                                   # this IS the non-stream path (the stream one returned above)
            set_active_model(_path)
            if ensure_model_async() is None:                 # D28: not local yet -> fetch in the background,
                self.model_loading_response()                # tell the client what is happening, return NOW
                return
            if not os.path.exists(LLAMA_CLI):                # D28: no engine on this box -> say so, don't crash the socket
                self.v1_error(503, f"GENGHIS has no llama-cli on this host ({LLAMA_CLI}). Run the installer "
                                   f"(install/) to build llama.cpp, or set GENGHIS_LLAMA_CLI.", "engine_missing")
                return

            # D23 slice 2 — RESIDENCY fast path: for a SOLO-on-the-local-anchor plan, keep a warm llama-server
            # holding the model and proxy to it (instant after the first load; it applies the model's own chat
            # template natively). Pooled/split runs, or residency disabled/failed, fall through to llama-cli.
            if residency_enabled():
                plan = _plan_or_pooled(goal)
                if plan:
                    rpc_list, devices, weights, nodes = plan
                    by_id_r = {d["id"]: d for d in load_fleet().get("donors", [])}
                    same_box_solo = len(nodes) == 1 and same_box(by_id_r.get(nodes[0]))       # D46
                    if rpc_list and not same_box_solo and not self.headers.get(DELEGATED_HDR):   # D34: a host with it on its own GPU wins
                        tgt = _delegate_target(os.path.basename(active_model()), load_fleet(), model_mem_mb())
                        if tgt:
                            nid, base, warm = tgt
                            body = json.dumps({**data, "stream": False}).encode("utf-8")
                            req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                                         headers={"Content-Type": "application/json", DELEGATED_HDR: SELF_HOST or "1", **_auth_headers()})
                            code = 200
                            try:
                                with urllib.request.urlopen(req, timeout=1800) as r:
                                    out = r.read()
                            except urllib.error.HTTPError as e:          # the host's own answer (e.g. 503 "fetching the model, N min") -- relay it, don't crash
                                code, out = e.code, e.read()
                            self.send_response(code); self.send_header("Content-Type", "application/json")
                            self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(out)))
                            self.end_headers(); self.wfile.write(out); return
                    pooled_warm = bool((_pool_alive().get(model_path()) or {}).get("shards"))   # D39
                    if (rpc_list or len(nodes) != 1) and not pooled_warm and not same_box_solo:  # pooled/split run needs the local
                        self._engine_acquire(goal, os.path.basename(active_model()))   # D45: the engine lock, only now
                        stop_resident()                                  # GPU's VRAM -> free the warm model first
                    else:                                                # solo on the local anchor, or warm across the fabric
                        base = ensure_resident(model_path(), rpc_list, devices, weights, plan_nodes=nodes)   # the LOCAL file (repo-cache aware), not the configured string
                        if base:
                            if stream:
                                self.stream_resident(goal, nodes, base, data, getattr(self, "_loop_note", "")); return
                            _req_set("proxy_reasoning", None); _req_set("proxy_message", None); _req_set("proxy_finish", "stop")
                            try:
                                with _inflight(base):
                                    text, ok = _proxy_resident(base, data, False)
                                # D49: if the model called a tool GENGHIS owns (a role's armed adapter),
                                # run it here and keep going until the model is done asking.
                                text, _trail = self._adapter_rounds(base, data, text)
                                if _trail:
                                    print("[adapter] " + " | ".join(_trail), flush=True)
                            except RuntimeError as e:                    # the warm server said no: tell the client, don't drop the socket
                                self.v1_error(502, f"[genghis] {e}", "upstream_error"); return
                            now = int(time.time())
                            _msg = {"role": "assistant", "content": fence_html(text)}
                            if _req_get("proxy_reasoning"):
                                _msg["reasoning_content"] = _req_get("proxy_reasoning")
                            _lm = _req_get("proxy_message") or {}
                            if _lm.get("tool_calls"):                        # D37: the model asked for a tool
                                _msg["tool_calls"] = _lm["tool_calls"]
                                if not text: _msg["content"] = None
                            resp = {"id": f"chatcmpl-{now}", "object": "chat.completion", "created": now,
                                    "model": self._v1_label(goal),
                                    "choices": [{"index": 0, "finish_reason": _req_get("proxy_finish", "stop") or "stop", "message": _msg}],
                                    "usage": _req_get("proxy_usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                    "genghis": {"nodes": nodes, "goal": goal, "model_file": os.path.basename(active_model()), "resident": True}}
                            body = json.dumps(resp).encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body); return
                        # resident couldn't start -> free any wedged VRAM and fall through to llama-cli
                        if _resident.get("pinned_by"):                 # D39: unless a fabric placement is the reason
                            alt = pinned_alternative(os.path.basename(active_model()), data.get("model"), load_fleet()) if not self.headers.get(DELEGATED_HDR) else None   # D42
                            if alt and alt[0] == "delegate":
                                _, nid, base_url, _w = alt
                                body = json.dumps({**data, "stream": False}).encode("utf-8")
                                req = urllib.request.Request(base_url + "/v1/chat/completions", data=body,
                                                             headers={"Content-Type": "application/json", DELEGATED_HDR: SELF_HOST or "1", **_auth_headers()})
                                code = 200
                                try:
                                    with urllib.request.urlopen(req, timeout=1800) as r:
                                        out = r.read()
                                except urllib.error.HTTPError as e:      # relay the host's own status (503 = fetching the model)
                                    code, out = e.code, e.read()
                                self.send_response(code); self.send_header("Content-Type", "application/json")
                                self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(out)))
                                self.end_headers(); self.wfile.write(out); return
                            if alt and alt[0] == "substitute":
                                _, alt_path, note = alt
                                print(f"[v1] {note}", flush=True)
                                set_active_model(alt_path)
                                base = ensure_resident(alt_path, [], devices, None, plan_nodes=nodes)
                                if base:
                                    if stream:
                                        self.stream_resident(goal, nodes, base, data, getattr(self, "_loop_note", "")); return
                                    try:
                                        with _inflight(base):
                                            text, ok = _proxy_resident(base, data, False)
                                    except RuntimeError as e:
                                        self.v1_error(502, f"[genghis] {e}", "upstream_error"); return
                                    now = int(time.time())
                                    resp = {"id": f"chatcmpl-{now}", "object": "chat.completion", "created": now, "model": self._v1_label(goal),
                                            "choices": [{"index": 0, "finish_reason": _req_get("proxy_finish", "stop") or "stop",
                                                         "message": {"role": "assistant", "content": fence_html(text)}}],
                                            "usage": _req_get("proxy_usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                            "genghis": {"nodes": nodes, "goal": goal, "model_file": os.path.basename(alt_path), "resident": True, "substituted": True, "note": note}}
                                    body = json.dumps(resp).encode("utf-8")
                                    self.send_response(200); self.send_header("Content-Type", "application/json")
                                    self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(body)))
                                    self.end_headers(); self.wfile.write(body); return
                            self.v1_error(409, "[genghis] " + (resident_status().get("error") or "pinned"), "pinned"); return
                        self._engine_acquire(goal, os.path.basename(active_model()))
                        stop_resident()

            prompt = _chatml(data.get("messages") or [])
            if stream:
                self.stream_chat_completion(goal, prompt, n); return
            self._engine_acquire(goal, os.path.basename(active_model()))   # the per-request engine: one at a time per host
            text, ok, nodes = generate(load_fleet(), goal, prompt, n)
            now = int(time.time())
            resp = {
                "id": f"chatcmpl-{now}", "object": "chat.completion", "created": now,
                "model": self._v1_label(goal),
                "choices": [{"index": 0, "finish_reason": "stop" if ok else "error",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},  # slice 1: counts TBD
                "genghis": {"nodes": nodes, "goal": goal, "model_file": os.path.basename(active_model())},  # our extension: which nodes + model ran it
            }
            if not ok and not text:
                resp["choices"][0]["message"]["content"] = no_plan_reason()
            body = json.dumps(resp).encode("utf-8")
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def v1_error(self, status, message, kind):
            """An OpenAI-style error body clients can display, instead of a dropped connection."""
            body = json.dumps({"error": {"message": message, "type": kind, "code": kind}}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def model_loading_response(self):
            """503 + an OpenAI-style error the client can SHOW (Open WebUI prints error.message verbatim):
            the model is being fetched from the library, with progress; retry shortly. (D28)"""
            st = fetch_status()
            name = st["name"] or os.path.basename(active_model())
            if st["error"] and not st["active"]:
                msg = (f"GENGHIS could not fetch {name} from the model library ({st['error']}). "
                       f"Check the coordinator / model repository, or put the GGUF in the models folder.")
            elif st["total"]:
                msg = (f"GENGHIS is fetching {name} from the model library — "
                       f"{st['done']/1e6:.0f} of {st['total']/1e6:.0f} MB ({st['pct']}%). Try again in a moment.")
            else:
                msg = f"GENGHIS is fetching {name} from the model library (starting) — try again in a moment."
            body = json.dumps({"error": {"message": msg, "type": "model_loading", "code": "model_loading",
                                         "genghis": {"model": name, "pct": st["pct"], "active": st["active"]}}}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Retry-After", "10")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def stream_with_status(self, goal, data, n, model_file=None):
            """D31 — streaming `/v1` that is never silent. Open WebUI (and every OpenAI client) shows only two
            things while it waits: streamed `content`, and streamed `reasoning_content` (rendered as an animated
            "Thinking… N s" block). Anything else — model load, prompt processing, RPC shard streaming — looked
            like a frozen cursor because we sent NOTHING (not even headers) until the first token. Now: headers
            go out immediately; GENGHIS narrates what it is doing through `reasoning_content` (plan → loading →
            processing), ticks every few seconds while a step blocks, relays the model's OWN reasoning as
            reasoning (it was being dropped), then streams the answer."""
            now = int(time.time())
            b = {"id": f"chatcmpl-{now}", "object": "chat.completion.chunk", "created": now, "model": self._v1_label(goal)}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            def sse(o):
                self.wfile.write(f"data: {json.dumps(o)}\n\n".encode("utf-8")); self.wfile.flush()
            def delta(**d):
                sse({**b, "choices": [{"index": 0, "finish_reason": None, "delta": d}]})
            def think(text):
                delta(reasoning_content=text)
            def with_ticks(label, fn):
                """Run fn() (blocking) in a thread; while it runs, tick the status every 3 s so the client's
                timer keeps moving. Returns fn's result (re-raises its exception)."""
                box = {}; snap = _req_snapshot()
                def _run():
                    _req_restore(snap)                            # the worker sees this request's model / goal
                    try: box["r"] = fn()
                    except Exception as e: box["e"] = e
                t = threading.Thread(target=_run, daemon=True); t.start()
                t0 = time.time()
                while t.is_alive():
                    t.join(3.0)
                    if t.is_alive():
                        lt = loading_text() if label in ("loading", "reloading") else ""
                        think(f"  … {label} · {lt}\n" if lt else f"  … {label} ({int(time.time() - t0)} s)\n")
                if "e" in box: raise box["e"]
                return box.get("r")
            def ticked_iter(gen, label, progress=None):
                """Iterate a generator from a worker thread; tick while the FIRST item is pending.
                `progress()` -> str (or None) is appended to each tick so a long wait shows MOTION."""
                import queue
                q = queue.Queue(); END = object(); stop = {"flag": False}; snap = _req_snapshot()
                def _pump():
                    _req_restore(snap)                            # the pump sees this request's model / goal
                    try:
                        for it in gen:
                            if stop["flag"]: break
                            q.put(it)
                    except Exception as e: q.put(e)
                    finally:
                        try: gen.close()                       # -> the engine generator's finally (terminate)
                        except Exception: pass
                        q.put(END)
                threading.Thread(target=_pump, daemon=True).start()
                t0 = time.time(); first = True; done = False
                try:
                    while True:
                        try:
                            it = q.get(timeout=3.0 if first else 1800)
                        except queue.Empty:
                            if first:
                                extra = ""
                                try: extra = (progress() if progress else "") or ""
                                except Exception: extra = ""
                                think(f"  … {label} ({int(time.time() - t0)} s){extra}\n"); continue
                            return
                        if it is END: done = True; return
                        if isinstance(it, Exception): done = True; raise it
                        first = False
                        yield it
                finally:
                    if not done:
                        # The consumer left early (client closed the tab, hit Stop, regenerated). Nobody is
                        # reading this answer: stop the engine NOW instead of letting a 32B run to completion
                        # for no one -- while holding the donor and the run lock.
                        stop["flag"] = True
                        p_ = getattr(run_llama_stream, "current_proc", None)
                        if p_ is not None:
                            try: p_.kill()
                            except Exception: pass
                        try: log_run({"run_id": datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + "_v1abandoned",
                                      "strategy": "v1_stream", "goal": goal, "ok": False, "note": "client left; engine killed"})
                        except Exception: pass
            released = True                                   # D45: the engine lock is taken only by the engine paths below
            def engine_acquire():
                """The per-request engine (llama-cli) is one-at-a-time per host: take its lock, narrating the wait."""
                nonlocal released
                if not released:
                    return
                if not _V1_RUN_LOCK.acquire(blocking=False):
                    def _ahead():
                        i = dict(_V1_RUN_INFO)
                        return (f"a {i.get('goal') or '?'} answer ({_tiny_model(i.get('model') or '?')}, running {int(time.time() - i['since'])} s)"
                                if i.get("since") else "another run")
                    think(f"  this host's engine is busy with {_ahead()} -- queued behind it; answers are capped, so at most a couple of minutes\n")
                    t_q = time.time()
                    while not _V1_RUN_LOCK.acquire(timeout=3.0):
                        think(f"  ... waiting ({int(time.time() - t_q)} s) for {_ahead()}\n")
                released = False
                _V1_RUN_INFO.update(goal=goal, model=name, since=time.time())
            try:
                # NOTHING may sit between acquiring the lock and this try: a raise there leaks the lock for good
                # (a NameError here held the NUC's lock and queued every chat behind a ghost -- 2026-09-16).
                if model_file:
                    set_active_model(model_file)              # this request's overlay: nobody else can clobber it (D45)
                if ensure_model_async() is None:
                    think(f"  {os.path.basename(model_path())} is not on this host yet -- fetching it from the repository in the background; ask again in a minute\n")
                    sse({**b, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
                delta(role="assistant")
                name = os.path.basename(active_model())
                plan = with_ticks("planning over the fleet", lambda: _plan_or_pooled(goal))
                if plan is None:
                    delta(content=no_plan_reason())
                    sse({**b, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
                rpc_list, devices, weights, nodes = plan
                by_id_r = {d["id"]: d for d in load_fleet().get("donors", [])}
                same_box_solo = len(nodes) == 1 and same_box(by_id_r.get(nodes[0]))           # D46: an eGPU on this box
                where = f"solo on {nodes[0]}" if len(nodes) == 1 else f"pooled across {', '.join(nodes)}"
                if same_box_solo: where += " (this box's own eGPU, over loopback)"
                think(f"GENGHIS · {goal} · {name} · {where}\n")
                # D34: if our plan needs the network, prefer a host that holds the model on its own GPU.
                if rpc_list and not same_box_solo and not self.headers.get(DELEGATED_HDR):
                    _req_set("last_why", [])
                    tgt = _delegate_target(name, load_fleet(), model_mem_mb())
                    if not tgt:
                        # The slow path is about to start: streaming the model over the network from here. Say so, with a
                        # time, in the user's status -- a blank cursor for five minutes reads as "broken" (2026-09-16).
                        mb = model_mem_mb(); mins = max(1, int(mb / 1024 / 4 + 0.5))     # ~4 GB/min wired; slower on Wi-Fi
                        why_txt = "; ".join(_req_get("last_why", []) or []) or "no host holds it"
                        think(f"  no host can take {name} on its own GPU right now ({why_txt}).\n"
                              f"  Running it from here over the network instead: sending ~{mb/1024:.0f} GB to {', '.join(nodes)} first "
                              f"-- expect about {mins} min before the first word, then it streams. Stop and ask again in a moment if you "
                              f"would rather wait for the warm host.\n")
                    if tgt:
                        nid, base, warm = tgt
                        think(f"  handing this to {nid} -- it {'holds' if warm else 'can hold'} {name} on its own GPU"
                              f" ({'warm now' if warm else 'loads from its local disk'}; no network per token). Its status follows:\n")
                        pass                                                              # (no engine lock held here -- D45)
                        def _raw(line):
                            self.wfile.write(line.rstrip(b"\r\n") + b"\n\n"); self.wfile.flush()
                        _delegate_stream(base, data, _raw)
                        log_run({"run_id": datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + "_v1delegated", "strategy": "v1_delegate",
                                 "goal": goal, "model": name, "live": [nid], "ok": True, "note": "warm" if warm else "cold-local"})
                        return
                pooled_warm = bool((_pool_alive().get(model_path()) or {}).get("shards"))   # D39: kept warm across the fabric
                use_resident = residency_enabled() and ((not rpc_list and len(nodes) == 1) or pooled_warm or same_box_solo)
                if residency_enabled() and not use_resident:
                    pins = [e for e in _pool_alive().values() if e.get("shards")]
                    if pins:                                 # D39: a cold pooled run would tear down a chosen fabric placement
                        pin = pins[0]
                        delta(content=f"[genghis] {socket.gethostname().lower()} is holding {_tiny_model(os.path.basename(pin['model']))} "
                                      f"across {' + '.join(pin.get('nodes') or [])}; running {name} would need that memory. "
                                      f"Unload the pooled model in the Control Room first, or ask for a model that fits beside it.")
                        sse({**b, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})
                        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
                    engine_acquire()                         # D45: the per-request engine is about to own the GPU
                    stop_resident()                          # pooled run needs the local GPU's VRAM
                if use_resident:
                    warm = resident_status()
                    already = any(e.get("model") == name for e in warm.get("pool", []))   # warm anywhere in the pool (D38)
                    if not already:
                        # D38 hand-over: if loading this here would EVICT a bigger warm model, and another live host
                        # already holds this model warm, send the chat there instead of churning our own card.
                        victims = _pool_evictions_for(model_path())
                        if victims and not self.headers.get(DELEGATED_HDR):
                            tgt = _delegate_target(name, load_fleet(), model_mem_mb())
                            # D45 follow-through: never evict a BIGGER warm model here for a smaller one another host can hold --
                            # a 1.5B title call was evicting the laptop's warm 32B (a 3.5-minute reload for the next chat).
                            # Warm elsewhere still wins outright; "can hold it, and it is smaller than what it would evict" is new.
                            def _mb_of(pth):
                                try: return os.path.getsize(pth) / (1024 * 1024)
                                except Exception: return 0.0
                            smaller = model_mem_mb() < max((_mb_of(v) for v in victims), default=0.0)
                            if tgt and (tgt[2] or smaller):
                                nid, base_url, _w = tgt
                                vict = ", ".join(_tiny_model(os.path.basename(v)) for v in victims)
                                think((f"  {name} is warm on {nid}; loading it here would evict {vict} -- handing this to {nid} instead" + chr(10)) if _w else
                                      (f"  loading {_tiny_model(name)} here would evict the bigger {vict}; {nid} can hold it -- handing this to {nid} instead (it loads there, once)" + chr(10)))
                                pass                                                  # (no engine lock held here -- D45)
                                def _raw(line):
                                    self.wfile.write(line.rstrip(b"\r\n") + b"\n\n"); self.wfile.flush()
                                _delegate_stream(base_url, data, _raw)
                                log_run({"run_id": datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + "_v1delegated", "strategy": "v1_delegate",
                                         "goal": goal, "model": name, "live": [nid], "ok": True, "note": "warm elsewhere; avoided evicting " + ",".join(os.path.basename(v) for v in victims)})
                                return
                        if victims:
                            think(f"  loading {name} into VRAM -- making room by unloading {', '.join(os.path.basename(v) for v in victims)}\n")
                        else:
                            think(f"  loading {name} into VRAM (first time; it stays warm after this)\n")
                    else:
                        think(f"  {name} is warm here\n")
                    if pooled_warm:
                        think(f"  {name} is warm across {' + '.join((_pool_alive().get(model_path()) or {}).get('nodes') or [])} (D39)\n")
                    conv_tok = approx_tokens(data.get("messages"))
                    esc_now = bool((_pool_alive().get(model_path()) or {}).get("escalated"))
                    if esc_now and conv_tok * 1.25 > _solo_fit_ctx(model_path()):
                        think(f"  still on the fabric window (this conversation is ~{conv_tok} tokens; this card alone holds {_solo_fit_ctx(model_path())})\n")
                    elif esc_now:
                        think(f"  dropping back to this card alone -- the conversation fits here again (~{conv_tok} tokens); reloading\n")
                    base = with_ticks("loading" if not already else "activating", lambda: ensure_resident(model_path(), rpc_list, devices, weights, plan_nodes=nodes, conv_tokens=conv_tok))
                    if base is None and _resident.get("pinned_by") and not self.headers.get(DELEGATED_HDR):
                        alt = pinned_alternative(name, data.get("model"), load_fleet())            # D42
                        if alt and alt[0] == "delegate":
                            _, nid, base_url, warm = alt
                            think(f"  {_resident.get('error')}\n  handing this to {nid} -- it {'holds' if warm else 'can hold'} {name} on its own GPU\n")
                            pass                                                      # (no engine lock held here -- D45)
                            def _raw(line):
                                self.wfile.write(line.rstrip(b"\r\n") + b"\n\n"); self.wfile.flush()
                            _delegate_stream(base_url, data, _raw)
                            log_run({"run_id": datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + "_v1delegated", "strategy": "v1_delegate",
                                     "goal": goal, "model": name, "live": [nid], "ok": True, "note": "pinned out here; " + ("warm" if warm else "cold-local")})
                            return
                        if alt and alt[0] == "substitute":
                            _, alt_path, note = alt
                            think(f"  {note}\n")
                            set_active_model(alt_path); name = os.path.basename(alt_path)
                            base = with_ticks("loading", lambda: ensure_resident(alt_path, [], devices, None, plan_nodes=nodes))
                    if base:
                        sse({**b, "genghis": {"nodes": nodes, "goal": goal, "model_file": name, "resident": True,
                                              "ctx": _resident.get("ctx")},
                             "choices": [{"index": 0, "finish_reason": None, "delta": {}}]})
                        think(f"  processing prompt (context {_resident.get('ctx')} tokens)\n")
                        if getattr(self, "_loop_note", ""): think("  " + self._loop_note + chr(10))
                        req = dict(data); req["stream"] = True
                        finish = "stop"; usage_seen = None
                        _owned = getattr(self, "_owned_tools", None) or {}
                        for attempt in (1, 2):
                            fg = FenceGuard()                              # an unfenced HTML page becomes an Artifact, not a wall of text
                            _acc = {}                                      # D49: streamed tool-call fragments, rebuilt
                            try:
                                with _inflight(base):
                                  for d in ticked_iter(_proxy_resident(base, req, True), "processing prompt"):
                                    rc = d.get("reasoning_content"); c = d.get("content"); tc = d.get("tool_calls")
                                    if rc: delta(reasoning_content=rc)      # the model's own thinking — was dropped before
                                    if c:
                                        c = fg.feed(c)
                                        if c: delta(content=c)
                                    if tc:
                                        # D49: if this role has armed adapters, a tool call may be OURS to run --
                                        # hold the fragments until the turn ends and we can see the whole call.
                                        # With no adapters in play this is D37 exactly: relay verbatim.
                                        if _owned: merge_tool_call_deltas(_acc, tc)
                                        else:      delta(tool_calls=tc)
                                    if d.get("_finish"): finish = d["_finish"]
                                    if d.get("_usage"): usage_seen = d["_usage"]
                                tail = fg.finish()
                                if tail: delta(content=tail)
                                if _owned and finish == "tool_calls":
                                    _calls = tool_calls_from(_acc)
                                    finish, _handled = self._stream_owned_tools(base, req, _calls, delta, think)
                                    if not _handled:                        # not ours: the client still gets them
                                        for _c in _calls: delta(tool_calls=[_c])
                                break
                            except ContextTooSmall as ce:
                                need_tok, have_tok = ce.need, ce.have      # copy out: `ce` is unbound once the block ends
                                # D32: grow the warm server's context to fit this conversation — once — if the GPU can
                                want, fit = resident_ctx(model_path(), _local_anchor_free_mb(), need=need_tok)
                                if attempt == 1 and want > have_tok and want <= fit:
                                    think(f"  this conversation is {need_tok} tokens; the warm server holds {have_tok} — "
                                          f"reloading with a {want}-token context (one-time)\n")
                                    base = with_ticks("reloading", lambda: ensure_resident(model_path(), rpc_list, devices, weights, need_ctx=need_tok))
                                    if base:
                                        continue
                                if attempt == 1 and need_tok > fit:
                                    # D43: this card alone cannot hold the window -- but the fabric can: re-plan the SAME chat
                                    # across it with the KV sized for this conversation. Slower per token; it keeps going.
                                    train = gguf_ctx_train(model_path()) or 32768
                                    if need_tok * 1.1 > train:
                                        delta(content=(f"[genghis] This conversation is {need_tok} tokens long; {name} was trained for {train}. "
                                                       f"Start a new chat, or shorten this one (delete older turns)."))
                                        break
                                    think(f"  this conversation is {need_tok} tokens; this card alone holds at most {fit} — "
                                          f"asking the fabric for a bigger window\n")
                                    ep = with_ticks("planning", lambda: escalation_plan(load_fleet(), goal, need_tok))
                                    if ep:
                                        e_rpc, e_dev, e_w, e_nodes = ep
                                        think(f"  re-planning the same chat across {' + '.join(e_nodes)} (slower per token -- it keeps going; "
                                              f"a new chat that fits this card drops back to it)\n")
                                        base = with_ticks("reloading", lambda: ensure_resident(model_path(), e_rpc, e_dev, e_w, plan_nodes=e_nodes,
                                                                                               need_ctx=need_tok, escalated=True))
                                        if base:
                                            rpc_list, devices, weights, nodes = e_rpc, e_dev, e_w, e_nodes
                                            continue
                                        delta(content=f"[genghis] the fabric could not take it: {resident_status().get('error') or 'warm server did not start'}. ")
                                    else:
                                        delta(content="[genghis] the fabric cannot hold this window either right now (no placement with the memory for it). ")
                                delta(content=(f"[genghis] This conversation is {need_tok} tokens long, but the most this model can hold on "
                                               f"this GPU is {fit} tokens. Start a new chat, or shorten this one (delete older turns)."))
                                break
                        sse({**b, "choices": [{"index": 0, "finish_reason": finish, "delta": {}}]})
                        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
                    err = resident_status().get("error") or "warm server unavailable"
                    if _resident.get("pinned_by"):                     # D39: the fabric placement wins; say so and stop
                        delta(content=f"[genghis] {err}")
                        sse({**b, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})
                        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
                    think(f"  {err} — using the per-request engine instead\n")
                    engine_acquire()
                    stop_resident()
                # per-request llama-cli path (pooled/split runs, or residency off/failed)
                engine_acquire()                              # (idempotent) residency off: the engine runs here
                prompt = _chatml(data.get("messages") or [])
                sse({**b, "genghis": {"nodes": nodes, "goal": goal, "model_file": name, "resident": False},
                     "choices": [{"index": 0, "finish_reason": None, "delta": {}}]})
                model_mb = model_mem_mb()
                remote = [x for x in nodes if x != SELF_ID] if rpc_list else []
                cached = bool(remote) and all(x in _delivered_nodes(name) for x in remote)
                if rpc_list and cached:
                    think(f"  loading {name} (~{model_mb/1024:.1f} GB) from {', '.join(remote)}'s tensor cache -- only the "
                          f"uncached slivers cross the network; the donor re-hashes and re-uploads it from its own disk (about a minute per 20 GB), then processing the prompt.\n")
                elif rpc_list:
                    think(f"  streaming {name} (~{model_mb/1024:.1f} GB) to {', '.join(remote)} over the network, then "
                          f"processing the prompt. This is the slow part the FIRST time a model goes to a node -- "
                          f"a donor with the tensor cache keeps it, so the next call skips this.\n")
                else:
                    think("  starting the engine and processing prompt\n")
                # Live progress for the shard stream: bytes the kernel has pushed to the donors vs the model size.
                _p = {"b0": _rpc_bytes_sent(rpc_list), "t0": time.time()}
                def stream_progress():
                    if _p["b0"] is None:
                        return None
                    now_b = _rpc_bytes_sent(rpc_list)
                    if now_b is None:
                        return None
                    sent_mb = max(0, now_b - _p["b0"]) / 1e6
                    el = max(1e-6, time.time() - _p["t0"]); rate = sent_mb / el      # MB/s
                    if sent_mb < 1:
                        return ""
                    if cached:
                        return f" · {sent_mb/1000:.1f} GB crossed the wire (cache load)"
                    pct = min(99, int(100 * sent_mb / max(1, model_mb)))
                    left = (model_mb - sent_mb) / rate if rate > 0.05 else None
                    eta = (f" · ~{int(left/60)} min left" if left and left >= 90 else (f" · ~{int(left)} s left" if left else ""))
                    if sent_mb >= model_mb * 0.98:
                        return " · model delivered, computing the prompt"
                    return f" · {sent_mb/1000:.1f} / {model_mb/1024:.1f} GB ({pct}%) · {rate:.0f} MB/s{eta}"
                sent_any = False; lead = ""; t_run0 = time.time()
                def _late_ledger(perf):
                    """The engine's timing line arrived after the stream closed: still write the ledger row."""
                    try:
                        _n2 = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
                        log_run({"run_id": _n2 + "_v1late", "ts": _n2, "strategy": "v1_stream", "goal": goal, "model": name,
                                 "model_mb": int(model_mb), "live": nodes, "gen_tok_s": perf.get("gen_tok_s"),
                                 "prompt_tok_s": perf.get("prompt_tok_s"), "wall_s": int(time.time() - t_run0),
                                 "ok": perf.get("gen_tok_s") is not None, "note": "timing arrived after the stream closed"})
                        if perf.get("gen_tok_s") is not None and remote:
                            _mark_delivered(name, remote)
                    except Exception:
                        pass
                for chunk in ticked_iter(run_llama_stream(rpc_list, devices, tensor_split=weights, n=n, prompt=prompt,
                                                          on_late_perf=lambda perf: _late_ledger(perf)), "working",
                                         progress=stream_progress if rpc_list else None):
                    if not sent_any:
                        lead = _ANSI.sub("", lead + chunk).lstrip("\r\n \t")
                        if not lead:
                            continue
                        chunk, lead, sent_any = lead, "", True
                    else:
                        chunk = _ANSI.sub("", chunk)
                    delta(content=chunk)
                perf = getattr(run_llama_stream, "last_perf", None) or {}
                wall = int(time.time() - t_run0)
                gen_s, pp_s = perf.get("gen_tok_s"), perf.get("prompt_tok_s")
                if gen_s is not None:
                    think(f"  done · {gen_s:g} t/s generation · {pp_s if pp_s is not None else '?'} t/s prompt · {wall} s wall · {', '.join(nodes)}\n")
                else:
                    think(f"  done · {wall} s wall · {', '.join(nodes)} (the engine's timing line lands in runs.jsonl once it finishes tearing down)\n")
                if gen_s is not None and remote:
                    _mark_delivered(name, remote)
                try:
                    _now = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
                    log_run({"run_id": _now + "_v1", "ts": _now, "strategy": "v1_stream", "goal": goal, "model": name,
                             "model_mb": int(model_mb), "live": nodes, "gen_tok_s": gen_s, "prompt_tok_s": pp_s,
                             "wall_s": wall, "ok": gen_s is not None})
                except Exception:
                    pass
                sse({**b, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                                          # client went away mid-stream — fine
            except Exception as e:
                try:
                    delta(content=f"[genghis] error: {e}")
                    sse({**b, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                except Exception:
                    pass
            finally:
                if not released:
                    _V1_RUN_INFO.clear(); _V1_RUN_LOCK.release()

        def stream_resident(self, goal, nodes, base, data, loop_note=""):
            """SSE streaming for the residency fast path — relay the warm server's OpenAI chunks, injecting the
            genghis extension (nodes/model/resident) on the first chunk."""
            now = int(time.time())
            b = {"id": f"chatcmpl-{now}", "object": "chat.completion.chunk", "created": now, "model": self._v1_label(goal)}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            def sse(o):
                self.wfile.write(f"data: {json.dumps(o)}\n\n".encode("utf-8")); self.wfile.flush()
            try:
                sse({**b, "genghis": {"nodes": nodes, "goal": goal, "model_file": os.path.basename(active_model()), "resident": True},
                     "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant"}}]})
                if loop_note:
                    sse({**b, "choices": [{"index": 0, "finish_reason": None, "delta": {"reasoning_content": f"  {loop_note}\n"}}]})
                req = dict(data); req["stream"] = True
                finish = "stop"; fg = FenceGuard()
                with _inflight(base):
                  for d in _proxy_resident(base, req, True):
                    if d.get("_finish"): finish = d["_finish"]
                    out = {k: v for k, v in d.items() if k in ("content", "reasoning_content", "tool_calls", "role") and v}
                    if out.get("content"):
                        out["content"] = fg.feed(out["content"])
                        if not out["content"]: out.pop("content")
                    if out:
                        sse({**b, "choices": [{"index": 0, "finish_reason": None, "delta": out}]})
                tail = fg.finish()
                if tail: sse({**b, "choices": [{"index": 0, "finish_reason": None, "delta": {"content": tail}}]})
                sse({**b, "choices": [{"index": 0, "finish_reason": finish, "delta": {}}]})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def stream_chat_completion(self, goal, prompt, n):
            """SSE streaming for POST /v1/chat/completions (slice 2): emit OpenAI chat.completion.chunk
            events as tokens arrive, then a final finish_reason chunk and the `[DONE]` sentinel. Every
            OpenAI streaming client (Open WebUI, the openai SDK, LangChain) consumes this."""
            now = int(time.time())
            base = {"id": f"chatcmpl-{now}", "object": "chat.completion.chunk",
                    "created": now, "model": self._v1_label(goal)}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()

            def sse(obj):
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
                self.wfile.flush()

            try:
                plan = _plan_for(load_fleet(), goal)
                if plan is None:                              # no live donors / over capacity
                    sse({**base, "genghis": {"nodes": [], "goal": goal},
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "delta": {"role": "assistant",
                                                "content": no_plan_reason()}}]})
                else:
                    rpc_list, devices, weights, nodes = plan
                    sse({**base, "genghis": {"nodes": nodes, "goal": goal, "model_file": os.path.basename(active_model())},   # first chunk: role + which nodes + model
                         "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant"}}]})
                    sent_any = False
                    lead = ""                                 # accumulate the first tokens so a leading ANSI
                    for delta in run_llama_stream(rpc_list, devices, tensor_split=weights, n=n, prompt=prompt):
                        if not sent_any:
                            # reset that arrives split across read() boundaries can't slip through — strip
                            # the WHOLE accumulated lead, and only emit once it has real (non-space) content.
                            lead = _ANSI.sub("", lead + delta).lstrip("\r\n \t")
                            if not lead:
                                continue
                            delta, lead, sent_any = lead, "", True
                        else:
                            delta = _ANSI.sub("", delta)
                        sse({**base, "choices": [{"index": 0, "finish_reason": None,
                                                  "delta": {"content": delta}}]})
                    sse({**base, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                                          # client disconnected mid-stream — fine

    # ThreadingHTTPServer: one short-lived thread per request so a slow browser client (the web admin)
    # can never block the TV's 5s fabric poll or a donor's /report. Negligible footprint at this volume.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    # Advertise the coordinator on the LAN via mDNS so clients/TVs find it with NO hardcoded IP
    # (zero-dependency; degrades silently if the module or the socket is unavailable).
    responder = None
    if SERVE_MODE == "authority":                   # D30: exactly one box advertises _genghis._tcp
        try:
            import genghis_mdns
            responder = genghis_mdns.advertise(port, {"role": "coordinator", "app": "genghis"})
        except Exception:
            responder = None
    else:
        print(f"== D30 inference host: fleet + config from the authority {AUTHORITY_BASE}; writes forwarded there ==")
        try:
            ok, note = register_self(quiet=True)      # make sure the authority knows this box (idempotent)
            print(f"  registered with the authority: {note}" if ok else f"  (could not register with the authority: {note})")
        except Exception as e:
            print(f"  (registration skipped: {e})")
    # Keep the authority's picture of THIS host's GPU honest: report free VRAM now (a fresh serve holds
    # nothing) and every 60 s (a resident comes and goes; a report pushed during an authority restart is lost).
    adopted = adopt_pool()                                # D38.1: a previous serve's warm servers are OURS again, not orphans
    kill_stale_residents()                                # anything on a resident port that was NOT adopted is an orphan
    if adopted:
        print(f"  warm pool adopted: {', '.join(os.path.basename(k) for k in _pool_alive())} -- no re-warm needed", flush=True)
    def _vram_beat():
        while True:
            try:
                # size of the RESIDENT's file (not the per-request MODEL global) + ~10% for its KV/compute buffers
                held = int(_pool_held_mb())                   # every warm server on this host (D38)
                report_anchor_vram(held)
            except Exception:
                pass
            time.sleep(60)
    threading.Thread(target=_vram_beat, name="vram-beat", daemon=True).start()
    class _Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            """A browser that navigates away mid-response is not an error. The default handler dumped a full
            traceback per refresh (BrokenPipe/ConnectionReset), which made a healthy log look alarming."""
            e = sys.exc_info()[1]
            if isinstance(e, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                return
            super().handle_error(request, client_address)
    with _Server(("0.0.0.0", port), Handler) as srv:
        srv.daemon_threads = True
        mdns = f"mDNS _genghis._tcp advertised as {responder.ip}" if responder else "mDNS off"
        print(f"== HEARTH fabric endpoint: http://0.0.0.0:{port}/fabric  ·  admin: http://0.0.0.0:{port}/  ·  {mdns}  (goal={GOAL}, Ctrl-C to stop) ==")
        import atexit
        if os.environ.get("GENGHIS_POOL_KEEP", "1") in ("0", "false", "no"):
            atexit.register(stop_resident)                # the old behaviour: free the VRAM on exit
        else:
            atexit.register(_pool_save)                   # D38.1: leave the warm servers running; the next serve adopts them
        prefetch_models()                                 # D28: pull missing default/goal models now, not on first chat
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  fabric endpoint stopped.")
        finally:
            stop_resident()
            if responder:
                responder.stop()


def plan_with_settle(fleet):
    """Plan over the live fleet. If it's capacity-short RIGHT NOW but the fleet COULD hold the model
    once busy donors free up (best-case pooled >= need — e.g. a prior run's shard not yet released),
    SETTLE: wait for free RAM to recover, re-fetch the authority, and re-measure. Returns (fleet, dec);
    dec is None only if the model won't fit even at full free memory. (Run #10 finding, 2026-09-04.)"""
    for settle in range(3):                       # first plan + up to 2 settle-and-retry waits
        dec = decide(fleet)                       # calibrates-if-needed + prints scores over LIVE donors
        if dec.get("split") is not None:
            return fleet, dec
        if dec.get("refused"):                    # D41: it fits, and we are declining on purpose -- say so, don't wait
            _req_set("last_refusal", dec["refused"])
            return fleet, None
        need = model_mem_mb(); live = live_donors(fleet)
        free_now = sum(free_mem_mb(d) for d in live)
        best = sum(best_case_mem_mb(d) for d in live)
        if need <= best and settle < 2:
            print(f"\n  -> capacity short now (~{free_now:.0f} MB free) but the fleet CAN hold ~{need:.0f} MB "
                  f"once donors free up (best-case ~{best:.0f} MB) — likely a prior run's shard not yet "
                  f"released. Settling 20s, then re-measuring…")
            time.sleep(20)
            fleet = load_fleet(remote=True)       # refresh live capacity from the authority
            heartbeat(fleet)
            continue
        return fleet, None                        # genuinely won't fit even at full free memory
    return fleet, None


def run_decision(fleet):
    """Execute the decision WITH self-healing: plan over LIVE donors; SETTLE + retry on a transient
    capacity shortfall (a busy donor); on a run failure/drop, re-plan + retry over the survivors."""
    heartbeat(fleet)  # refresh membership first — always plan over the living
    if not live_donors(fleet):
        print("  -> no live donors (all heartbeats down)."); return
    result = (None, None, False)
    for attempt in (1, 2):
        fleet, dec = plan_with_settle(fleet)
        if dec is None:
            print("\n  -> CANNOT RUN: model exceeds the fleet's capacity even at full free memory."); return
        by_id = {d["id"]: d for d in live_donors(fleet)}
        chosen = [by_id[i] for i in dec["nodes"] if i in by_id]
        rpc_list, devices = build_devices(chosen)   # local anchor -> CUDA0, donors -> RPC0.. (M16/D9)
        if dec["split"]:
            # CAPACITY-weighted split, not throughput-weighted: run_decision only splits when the model
            # won't fit solo (D6), so every shard is capacity-constrained — weighting by throughput hands
            # the fast node more than it can hold (the GPU OOM: 18GB asked of a 16GB card). Weight by
            # free_mem_mb so each node fills to the same fraction of its (headroom-derated) capacity.
            w = [free_mem_mb(d) for d in chosen]; s = sum(w) or 1
            weights = [x / s for x in w]; label = f"SPLIT across {[d['id'] for d in chosen]}"
        else:
            weights, label = None, f"SINGLE-NODE on {chosen[0]['id']}"
        print(f"\n== Running (attempt {attempt}): {label} ==")
        print("   (first load streams the model to the donors — patient on a big one)")
        t0 = time.time()
        gen, pp, ok = run_llama(rpc_list, devices, tensor_split=weights, n=32,
                                prompt="In two sentences, what is the GENGHIS Protocol?")
        result = (gen, pp, ok)
        print(f"   -> {'RAN' if ok else 'FAILED'}: gen {gen} t/s | prompt {pp} t/s | {time.time()-t0:.0f}s")
        if ok:
            if not dec["split"] and gen:             # single-node run = an honest per-node signal -> self-tune (v0.3)
                before = chosen[0].get("tps_ema") or chosen[0].get("tokens_per_s_solo")
                observe_tps(chosen[0], gen, fleet)
                print(f"   ~ self-tune: {chosen[0]['id']} tps_ema {before} -> {chosen[0]['tps_ema']} (obs {gen})")
            break
        print("   ! run failed — heartbeat to check fleet health (self-heal)…")   # SELF-HEAL
        dropped = [t[0] for t in heartbeat(fleet) if t[2] == "down"]
        if dropped and attempt == 1:
            print(f"   ! {', '.join(dropped)} went DOWN — re-planning over survivors and retrying.")
            continue
        break
    gen, pp, ok = result
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_run({"run_id": f"{ts}_v2", "ts": ts, "strategy": "genghis_v2_decide",
             "model": os.path.basename(active_model()), "model_mb": round(model_mem_mb()),
             "live": [d["id"] for d in live_donors(fleet)],
             "gen_tok_s": gen, "prompt_tok_s": pp, "ok": ok})
    return gen, ok


def checkins(fleet=None):
    """Review the HEARTH check-in history — the agent's questions and the human's answers over time.
    The healthcare record: glance at the trend (Mon: Good · Tue: Tired · Wed: Not great), not a one-off."""
    url = FLEET_URL.rsplit("/", 1)[0] + "/replies"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as r:
            raw = r.read().decode("utf-8")
    except Exception as e:
        print(f"  (couldn't reach the check-in log at {url}: {e})"); return
    rows = [ln.split("\t") for ln in raw.split("\n") if ln.strip()]
    if not rows:
        print("  No check-ins recorded yet. Pose one: put '?? <question>' + '= <choice>' lines in the"
              " Pi's hearth_message.txt, then answer it on the TV."); return
    print(f"== HEARTH check-ins — {len(rows)} recorded (newest first) ==")
    for r in reversed(rows[-25:]):
        ts  = (r[0] if len(r) > 0 else "?").replace("T", " ")
        q   = r[2] if len(r) > 2 else ""
        ans = r[3] if len(r) > 3 else ""
        print(f"  {ts}   ->  {ans}" + (f"      ({q})" if q else ""))


def main():
    global GOAL, MODEL
    ap = argparse.ArgumentParser(description="GENGHIS coordinator (v0.3)")
    ap.add_argument("action",
                    choices=["init", "register", "registry", "roles", "adapters", "fleet", "calibrate", "decide", "run", "sweep", "heartbeat", "monitor", "view", "serve", "checkins", "models", "discover"],
                    nargs="?", default="decide")
    ap.add_argument("--goal", choices=["fastest", "balanced", "fit", "biggest"], default=GOAL,
                    help="run-type / objective: fastest | balanced(default) | fit | biggest")
    ap.add_argument("rest", nargs="*", help="extra args (e.g. `models pull <name.gguf>`, or `init` options like --node-name)")
    args, _extra = ap.parse_known_args()   # let init's --node-name/--dir/--yes/... flow through into rest
    # Rebuild `rest` in ORIGINAL argv order. argparse hands back the VALUE of an unknown option as a positional
    # (`<ip>` -> args.rest) and the option itself as unknown (`--coord` -> _extra); naively
    # concatenating the two re-ordered `--coord 1.2.3.4` into `1.2.3.4 --coord`, so _init_opt saw a bare
    # flag (True) and `genghis init --coord <ip>` crashed with "'bool' object has no attribute 'split'".
    argv = sys.argv[1:]
    tail = argv[argv.index(args.action) + 1:] if args.action in argv else argv
    rest, skip = [], False
    for t in tail:
        if skip:
            skip = False; continue
        if t == "--goal":
            skip = True; continue                    # --goal X is ours, not init's
        if t.startswith("--goal="):
            continue
        rest.append(t)
    args.rest = rest
    GOAL = args.goal
    # A configured model (config.json "model") overrides the built-in default — host-portable: the Pi
    # sets a POSIX path/name, the laptop keeps its E:\ default. An explicit GENGHIS_MODEL env still wins
    # (applied at import, line ~35). Previously this key was inert (nothing ever rebound MODEL), so a
    # Linux-hosted `serve` used the Windows default path and os.path.basename() (POSIX: '\' is not a
    # separator) mangled it into a 404 on the model fetch.
    if "GENGHIS_MODEL" not in os.environ:
        _cfg_model = (load_config().get("model") or "").strip()
        if _cfg_model:
            MODEL = _cfg_model
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # avoid cp1252 crashes on Windows
    except Exception:
        pass
    if args.action == "init":            # CREATES the fleet — must run before any llama-cli/fleet checks
        init_cmd(args); return
    if args.action == "register":        # announce this box to the authority (D33); needs only the local fleet.json
        register_cmd(args); return
    if args.action == "roles":           # D48: reads the home + the registry; no fleet/llama-cli needed
        roles_cmd(args); return
    if args.action == "adapters":        # D49: reads the home; dials each adapter, runs nothing
        adapters_cmd(args); return
    if args.action == "registry":        # reads local GGUFs + config; no fleet/llama-cli needed
        registry_cmd(args); return
    if args.action == "fleet":           # node lifecycle; talks to the coordinator, no llama-cli needed
        fleet_cmd(args); return
    no_model = ("heartbeat", "monitor", "view", "serve", "checkins", "models", "discover")
    if args.action not in no_model and not os.path.exists(LLAMA_CLI):
        sys.exit(f"llama-cli not found: {LLAMA_CLI}")
    if args.action not in no_model:
        try:
            w = os.path.getsize(model_path()) / (1024 * 1024)
            src = "GGUF" if gguf_arch_params() else "heuristic"
            print(f"[model: {os.path.basename(MODEL)}  ~{model_mem_mb():.0f} MB  "
                  f"= weights {w:.0f} + KV {kv_cache_mb():.0f} @ {N_CTX}ctx ({src}) + 512]")
        except OSError:
            print(f"[model: {os.path.basename(MODEL)}]")
    # `serve` IS the authority (reads its local file); every other command is a planning client that
    # fetches the single source of truth from the Pi (falls back to local cache if unreachable).
    if args.action == "serve":
        c = _init_opt(args.rest or [], "coord")
        if isinstance(c, str) and c:                     # `serve --coord HOST[:PORT]` = be an inference host of HOST
            global FLEET_URL, _EXPLICIT_COORD
            FLEET_URL = f"http://{c if ':' in c else c + ':8899'}/fleet.json"; _EXPLICIT_COORD = True
        decide_serve_mode()
    fleet = None if args.action in ("checkins", "models", "discover") else load_fleet(remote=(args.action != "serve"))
    {"calibrate": calibrate, "decide": decide, "run": run_decision, "sweep": sweep,
     "heartbeat": hb_show, "monitor": monitor, "view": operator_view, "serve": serve,
     "checkins": checkins, "models": models_cmd, "discover": discover_cmd}[args.action](fleet)


if __name__ == "__main__":
    main()
