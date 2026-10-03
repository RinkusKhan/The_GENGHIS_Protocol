#!/usr/bin/env python3
"""GENGHIS -- provision an Open WebUI instance so a first-time user gets a chat that just works.

Everything here was learned the hard way on the reference fleet (2026-09-15/16) and is what a stranger's install
would otherwise be missing:

  * Builtin Tools OFF on every GENGHIS model.  Open WebUI 0.11 hands the model ~30 internal functions (search chats,
    read/write/delete memories, fetch URLs, run code, knowledge bases...) by default; a local model shown that many
    tools calls them ALL and never answers.  There is no global switch -- it is a per-model capability, default on.
  * A small default tool belt (Fleet + Weather + Wikipedia when installed), none on the fastest/task model.
  * Native function calling on the models that can drive tools.
  * The GENGHIS system prompt (system_prompt.txt): diagrams as Mermaid in the answer, pages/charts as a single-file
    HTML Artifact with an exact Chart.js skeleton -- local models draw reliably from a template, not from nothing.
  * Web search ON (DuckDuckGo, keyless) and the task model = genghis-fastest (titles/tags/follow-ups never wake the 32B).

Idempotent and RESPECTFUL: a model is provisioned once (stamped meta.genghis_provisioned); after that it is the user's --
turn a tool off, write your own prompt, and no later run reverts it.  New models that appear later (a GGUF dropped in the
folder) are provisioned the first time they are seen, which is why the authority's watchdog re-runs this every 15 min.

D47 -- an upgrade is not a choice.  The stamp alone could not tell "the user turned this off" from "the chat UI's
upgrade reset it", so a future release could silently restore the thirty-builtin-tools behaviour and nothing would say
so.  Each provisioned row therefore also records WHAT GENGHIS WROTE (meta.genghis_applied) and the chat-UI version it
was written under (meta.genghis_ui_version).  From then on:
  * value == what GENGHIS wrote                      -> untouched; nothing to do.
  * value differs, chat-UI version UNCHANGED         -> the user changed it.  Kept, and said out loud once (a human
                                                       run prints it; the watchdog's --quiet run stays silent).
  * value differs, chat-UI version CHANGED           -> the upgrade moved it.  Re-applied, and said out loud.
  * `--reset <model-id|all>`                         -> re-apply GENGHIS's defaults on purpose (what you run after
                                                       an experiment you have forgotten the details of).
Nothing is ever reverted without either a version change to point at or an explicit --reset.
It edits Open WebUI's SQLite database
directly (webui.db) because these settings live only there -- env vars are defaults for a FRESH database and are
ignored once one exists.  Restart Open WebUI afterwards so it re-reads them.

Usage (on the box that runs the compose file):
    docker cp integrations/openwebui/provision.py open-webui:/tmp/provision.py
    docker cp integrations/openwebui/system_prompt.txt open-webui:/tmp/system_prompt.txt
    docker exec open-webui python3 /tmp/provision.py            # add --dry-run to see what would change
    docker restart open-webui
Or against a copied database:  python3 provision.py --db /path/to/webui.db --prompt system_prompt.txt
"""
import argparse, json, os, sqlite3, sys, time

GOALS = ("fastest", "balanced", "fit", "biggest")
SMALL_MB = 3000          # a model under this can't drive tools (the 1.5B): no tool belt, plain calling
PROV_VERSION = 3         # a row stamped meta.genghis_provisioned == this is the USER'S from then on: never touched again.
                         # Bumping it re-visits every row ONCE, but a field is re-applied only if it still holds OUR earlier
                         # default (a prompt that begins with PROMPT_MARK); anything the user changed is kept.
WATCHED = ("capabilities.builtin_tools", "toolIds", "function_calling")   # D47: the fields an upgrade must not silently move
PROMPT_MARK = "You run on the user's own GENGHIS fleet."
TOOLS_DEFAULT = ["genghis_fleet", "weather_open_meteo", "wikipedia_tool"]   # only those actually installed are kept
ROLE_FEATURES = ["code_interpreter"]   # a role (genghis-coder, -researcher, ...) opens with the Code Interpreter on: the model
                                       # writes Python, the chat runs it in the browser on the files in its Files panel. Plain
                                       # (non-native) calling, because in native mode the interpreter is one of Open WebUI's
                                       # builtin tools, which stay OFF (they flood a local model) -- so it never arrived.
# The Code Interpreter runs Pyodide from Open WebUI's OWN copy (/app/build/pyodide): its package list names ~250 packages,
# but the image ships the files of only ~50. GENGHIS tells the model it can use OpenCV and scikit-image (CI_HOWTO); in the
# first real photo repair (2026-09-28) `import cv2` failed with "Failed to fetch" and the model fell back to Pillow. So the
# promised packages, and what they depend on, are fetched from Pyodide's CDN for exactly this build, checked against the
# sha256 in its own package list, and put where the browser looks. Re-run by the watchdog: a recreated container heals.
PYODIDE_DIR = "/app/build/pyodide"
PYODIDE_EXTRAS = ["opencv-python", "scikit-image"]
PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v{version}/full/{file}"
CAPS_DEFAULT = {"file_context": True, "vision": False, "file_upload": True, "web_search": True, "image_generation": False,
                "code_interpreter": True, "citations": True, "status_updates": True, "builtin_tools": False}


def ui_version():
    """The chat UI's own version, so a reset can be attributed to an upgrade rather than to the user (D47)."""
    try:
        from open_webui.env import VERSION      # running inside the container
        return str(VERSION)
    except Exception:
        pass
    for path in ("/app/package.json", os.path.join(os.path.dirname(__file__), "package.json")):
        try:
            with open(path, encoding="utf-8") as f:
                v = json.load(f).get("version")
                if v:
                    return str(v)
        except Exception:
            continue
    return ""


def _get(p, m, field):
    """Read one WATCHED field from (params, meta)."""
    if field == "function_calling":
        return p.get("function_calling")
    if field == "toolIds":
        return sorted(m.get("toolIds") or [])
    if field.startswith("capabilities."):
        return (m.get("capabilities") or {}).get(field.split(".", 1)[1])
    return None


def _set(p, m, field, value):
    if field == "function_calling":
        p["function_calling"] = value
    elif field == "toolIds":
        m["toolIds"] = list(value or [])
    elif field.startswith("capabilities."):
        caps = dict(m.get("capabilities") or {}); caps[field.split(".", 1)[1]] = value; m["capabilities"] = caps


def pyodide_extras(dry_run=False):
    """Fetch the Code Interpreter packages GENGHIS promises (PYODIDE_EXTRAS + their dependencies) that Open WebUI's
    Pyodide lists but does not ship. Returns (fetched, failed) as lists of lines; nothing to do = two empty lists."""
    import hashlib, urllib.request
    lock_p, pkg_p = os.path.join(PYODIDE_DIR, "pyodide-lock.json"), os.path.join(PYODIDE_DIR, "package.json")
    if not (os.path.exists(lock_p) and os.path.exists(pkg_p)):
        return [], []                                  # not inside the Open WebUI container (a --db run): nothing to do
    lock = json.load(open(lock_p, encoding="utf-8"))["packages"]
    version = json.load(open(pkg_p, encoding="utf-8"))["version"]
    norm = lambda n: n.lower().replace("_", "-").replace(".", "-")   # `depends` says lazy_loader, the key is lazy-loader
    keys = {norm(k): k for k in lock}
    need, stack = [], list(PYODIDE_EXTRAS)
    while stack:
        k = keys.get(norm(stack.pop()))
        if not k or k in need:
            continue
        need.append(k); stack += lock[k].get("depends") or []
    fetched, failed = [], []
    for k in sorted(need):
        f = lock[k]["file_name"]; dst = os.path.join(PYODIDE_DIR, f)
        if os.path.exists(dst):
            continue
        if dry_run:
            fetched.append(f"would fetch {k} ({f})"); continue
        try:
            with urllib.request.urlopen(PYODIDE_CDN.format(version=version, file=f), timeout=120) as r:
                data = r.read()
            if hashlib.sha256(data).hexdigest() != lock[k].get("sha256"):
                failed.append(f"{k}: the download did not match Pyodide's checksum -- not installed"); continue
            with open(dst + ".tmp", "wb") as out:
                out.write(data)
            os.replace(dst + ".tmp", dst)
            fetched.append(f"Code Interpreter package {k} ({len(data) // 1024} KB)")
        except Exception as e:
            failed.append(f"{k}: could not fetch ({e.__class__.__name__}: {e}) -- the model can't import it until this works")
    return fetched, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/backend/data/webui.db")
    ap.add_argument("--prompt", default=None, help="system_prompt.txt (default: next to this script, else /tmp)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="print only when something changed (for the watchdog)")
    ap.add_argument("--reset", metavar="MODEL|all", default=None,
                    help="re-apply GENGHIS's defaults to this model (or all) even though it is provisioned -- "
                         "for undoing an experiment you no longer remember the details of (D47)")
    a = ap.parse_args()
    say = (lambda *x: None) if a.quiet else print
    prompt_path = a.prompt or next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt"),
                                                 "/tmp/system_prompt.txt") if os.path.exists(p)), None)
    system = open(prompt_path, encoding="utf-8").read().strip() if prompt_path else ""
    got, bad = pyodide_extras(a.dry_run)
    for line in got:
        print(("" if a.dry_run else "fetched: ") + line)
    for line in bad:
        print("PROBLEM:", line)                        # said even under --quiet: a promised package is missing
    if not system:
        say("warning: system_prompt.txt not found -- models keep their current system prompt")
    if not os.path.exists(a.db):
        sys.exit(f"no database at {a.db} (is this the Open WebUI container / data volume?)")
    if not a.dry_run:
        bak = a.db + ".bak-provision"
        if not os.path.exists(bak):                  # SQLite's own backup: a plain file copy misses the WAL's recent writes
            src = sqlite3.connect(a.db); dst = sqlite3.connect(bak); src.backup(dst); dst.close(); src.close()
            say("backup:", bak)
    c = sqlite3.connect(a.db)
    now = int(time.time()); changes = []; notes = []          # notes = things we deliberately did NOT change (D47)
    UI_NOW = ui_version()

    installed = {r[0] for r in c.execute("select id from tool")}
    tools = [t for t in TOOLS_DEFAULT if t in installed]
    missing = [t for t in TOOLS_DEFAULT if t not in installed]
    if missing:
        say("tools not installed yet (paste them under Workspace -> Tools, then re-run):", ", ".join(missing))
    admin = c.execute("select id from user where role='admin' order by created_at limit 1").fetchone()
    if not admin:
        sys.exit("no admin user yet -- open Open WebUI once, create the first account, then re-run")
    admin = admin[0]

    # --- every model the fleet exposes: the four goals + each GGUF in the folder (a NEW file dropped in the folder
    #     later shows up in the picker with Open WebUI's defaults -- builtin tools ON, no prompt -- until this runs again;
    #     the authority's watchdog re-runs it every 15 min for exactly that reason) ---
    targets = {f"genghis-{g}": {"tools": g != "fastest", "native": g != "fastest"} for g in GOALS}
    try:
        import urllib.request
        bases = json.loads(c.execute("select value from config where key='openai.api_base_urls'").fetchone()[0] or "[]")
    except Exception:
        bases = []
    for base in bases:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=5) as r:
                for m in (json.loads(r.read().decode("utf-8")).get("data") or []):
                    g = m.get("genghis") or {}
                    if not g or not m.get("id"):
                        continue                                   # only GENGHIS's own entries carry `genghis`
                    if g.get("kind") == "role":                    # a role acts through GENGHIS's own tools (run server-side)
                        targets[m["id"]] = {"tools": False, "native": False, "role": True}   # and the Code Interpreter
                        continue
                    big = (g.get("size_mb") or 0) >= SMALL_MB
                    targets[m["id"]] = {"tools": big, "native": big}
        except Exception as e:
            say(f"could not list models at {base} ({e}) -- provisioning the goals only")
    for mid, t in targets.items():
        row = c.execute("select params, meta from model where id=?", (mid,)).fetchone()
        p = json.loads(row[0] or "{}") if row else {}
        m = json.loads(row[1] or "{}") if row else {"profile_image_url": "/static/favicon.png", "description": None,
                                                     "suggestion_prompts": None, "tags": []}
        stamp = m.get("genghis_provisioned") if row else None
        forced = a.reset is not None and a.reset in ("all", mid)
        if stamp == PROV_VERSION and not forced:
            # D47: provisioned already -- so the only question is whether anything MOVED since we wrote it, and who
            # moved it. `genghis_applied` is what GENGHIS wrote; `genghis_ui_version` is the chat UI it was written
            # under. A change under the SAME chat-UI version is the user's and is kept (said once, on a human run);
            # a change across an upgrade is the upgrade's and is put back.
            applied = m.get("genghis_applied") or {}
            was_ui = m.get("genghis_ui_version")
            if not applied:                            # stamped before D47: adopt today's values as the baseline
                base = {f: _get(p, m, f) for f in WATCHED}
                if not a.dry_run:
                    m2 = json.loads(json.dumps(m)); m2["genghis_applied"] = base; m2["genghis_ui_version"] = UI_NOW
                    c.execute("update model set meta=?, updated_at=? where id=?", (json.dumps(m2), now, mid))
                notes.append(f"{mid}: recorded today's settings as the baseline (D47; nothing changed)")
                continue
            drift = [f for f in WATCHED if _get(p, m, f) != applied.get(f)]
            if not drift:
                continue                               # untouched since GENGHIS wrote it
            if was_ui and UI_NOW and was_ui != UI_NOW:
                want_p2, want_m2 = dict(p), json.loads(json.dumps(m))
                for f in drift:
                    _set(want_p2, want_m2, f, applied.get(f))
                want_m2["genghis_ui_version"] = UI_NOW
                changes.append(f"{mid}: the chat UI moved {was_ui} -> {UI_NOW} and reset {', '.join(drift)} -- put back "
                               f"(GENGHIS set these; `--reset` or the UI is how YOU change them)")
                if not a.dry_run:
                    c.execute("update model set params=?, meta=?, updated_at=? where id=?",
                              (json.dumps(want_p2), json.dumps(want_m2), now, mid))
            else:
                notes.append(f"{mid}: you changed {', '.join(drift)} since GENGHIS set it -- left as you have it "
                             f"(`--reset {mid}` puts GENGHIS's defaults back)")
            continue
        upgrade = row is not None and stamp is not None and not forced   # older stamp: touch only what is still ours
        want_p = dict(p); want_m = json.loads(json.dumps(m)); want_m["genghis_provisioned"] = PROV_VERSION
        if not upgrade:                                # first time, or an explicit --reset: GENGHIS's defaults
            caps = dict(want_m.get("capabilities") or {})
            for k, v in CAPS_DEFAULT.items():
                caps.setdefault(k, v)
            caps["builtin_tools"] = False
            want_m["capabilities"] = caps
            want_m["toolIds"] = tools if t["tools"] else []
            if t["native"]:
                want_p["function_calling"] = "native"
            if t.get("role"):
                want_p["function_calling"] = "legacy"           # plain mode, NAMED: in Open WebUI 0.11 an unset value means native,
                                                                # where the interpreter is a builtin tool (off) -- it vanished
                caps["code_interpreter"] = True
                caps["web_search"] = False                     # no Open WebUI search in front of a role: it slowed every
                want_m["capabilities"] = caps                  # Coder answer with 9 irrelevant pages (2026-09-26); the
                                                               # Researcher searches through GENGHIS's own web adapter
                want_m["defaultFeatureIds"] = list(ROLE_FEATURES)
        if system and (not upgrade or (p.get("system") or "").startswith(PROMPT_MARK)):
            want_p["system"] = system                  # ours before -> ours now; a user's own prompt is never replaced
        want_m["genghis_applied"] = {f: _get(want_p, want_m, f) for f in WATCHED}   # D47: what GENGHIS wrote, to compare against
        want_m["genghis_ui_version"] = UI_NOW
        if row is None:
            changes.append(f"create model row {mid}")
            if not a.dry_run:
                c.execute("insert into model (id,user_id,base_model_id,name,params,meta,updated_at,created_at,is_active) values (?,?,?,?,?,?,?,?,1)",
                          (mid, admin, None, mid, json.dumps(want_p), json.dumps(want_m), now, now))
        elif want_p != p or want_m != m:
            diff = [k for k in ("function_calling", "system") if want_p.get(k) != p.get(k)]
            diff += [k for k in ("capabilities", "toolIds") if want_m.get(k) != m.get(k)]
            changes.append(f"{'reset' if forced else ('upgrade' if upgrade else 'provision')} {mid}: {', '.join(diff) or 'stamped'}"
                           + ("" if upgrade or forced else " (first time; yours to change from now on)"))
            if not a.dry_run:
                c.execute("update model set params=?, meta=?, updated_at=? where id=?", (json.dumps(want_p), json.dumps(want_m), now, mid))

    # --- config: web search on (keyless DuckDuckGo), task model = fastest ---
    def want_cfg(key, value):
        cur = c.execute("select value from config where key=?", (key,)).fetchone()
        if cur is None or json.loads(cur[0]) != value:
            changes.append(f"config {key} = {json.dumps(value)}")
            if not a.dry_run:
                if cur is None:
                    c.execute("insert into config (key, value, updated_at) values (?,?,?)", (key, json.dumps(value), now))
                else:
                    c.execute("update config set value=?, updated_at=? where key=?", (json.dumps(value), now, key))
    if c.execute("select 1 from config where key='web.search.engine'").fetchone() is None or \
       json.loads(c.execute("select value from config where key='web.search.engine'").fetchone()[0] or '""') in ("", None):
        want_cfg("web.search.engine", "duckduckgo")
    want_cfg("web.search.enable", True)
    want_cfg("task.model.default", "genghis-fastest")
    want_cfg("task.model.external", "genghis-fastest")

    if not a.dry_run:
        c.commit()
    if changes or not a.quiet:
        print(("would change" if a.dry_run else "changed") + f" {len(changes)} thing(s):")
    for ch in changes:
        print("  -", ch)
    if not changes:
        say("  nothing -- already provisioned")
    # D47: differences GENGHIS chose NOT to touch. A human run should see them (that is how a forgotten experiment
    # gets found); the watchdog's --quiet run stays silent, or it would say the same thing every 15 minutes.
    if notes and not a.quiet:
        print("left alone (yours):")
        for n in notes:
            print("  -", n)
    if not a.dry_run and any(ch.startswith("config ") for ch in changes):
        print("a config setting changed: docker restart open-webui  (model rows take effect without a restart)")


if __name__ == "__main__":
    main()
