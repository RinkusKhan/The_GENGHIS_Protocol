#!/usr/bin/env python3
"""GENGHIS watchdog — the fleet's "was everything alive at HH:MM?" record (stdlib only).

Runs on the AUTHORITY from cron (every 15 min). For every node in fleet.json it probes the RPC port (donor
alive?) and :8899 (a serve — authority or inference host — alive?), plus the authority's own /fabric.json.
Appends ONE line per pass to watchdog.log and rewrites watchdog.json (the latest pass), which the coordinator
serves at /watchdog.json so the Control Room can show "verified HH:MM · N/M up".

    */15 * * * * /usr/bin/python3 ~/genghis/watchdog.py >> ~/genghis/watchdog.cron.log 2>&1

Why: after a week of "something broke overnight?", the answer must be a `tail -20 watchdog.log`, not a hunt.
"""
import datetime, json, os, socket, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.environ.get("GENGHIS_FLEET", os.path.join(HERE, "fleet.json"))
LOG = os.path.join(HERE, "watchdog.log")
LATEST = os.path.join(HERE, "watchdog.json")
SERVE_PORT = int(os.environ.get("GENGHIS_SERVE_PORT", "8899"))


def tcp(ip, port, timeout=2.0):
    t0 = time.time()
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True, round((time.time() - t0) * 1000, 1)
    except OSError:
        return False, None


def pinned(d, max_age_s=900):
    """A donor holding a shard of some host's POOLED warm model (D39): its single-client ggml-rpc-server is
    occupied, so a TCP probe times out -- that is "busy for a host", not "down". The booking is refreshed by the
    host that holds the model; stale = trust the probe again."""
    for h in (d.get("shard_held") or {}).values() if isinstance(d.get("shard_held"), dict) else []:
        try:
            if (datetime.datetime.now() - datetime.datetime.fromisoformat(h.get("ts") or "")).total_seconds() < max_age_s:
                return True
        except Exception:
            pass
    return False


def http_json(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def main():
    try:
        with open(FLEET, encoding="utf-8") as f:
            fleet = json.load(f)
    except Exception as e:
        print(f"watchdog: cannot read {FLEET}: {e}", file=sys.stderr); sys.exit(2)
    now = datetime.datetime.now()
    nodes = []
    serve_seen = set()            # ips whose :8899 we have already probed (one serve per box, not per card)
    for d in fleet.get("donors", []):
        nid, ip, port = d.get("id"), d.get("ip"), d.get("port")
        role = d.get("role", "compute")
        rec = {"id": nid, "ip": ip, "role": role}
        if ip and port and role in (None, "compute"):
            if pinned(d):
                rec["rpc"] = True; rec["rpc_ms"] = None; rec["pinned"] = True     # occupied by a pooled model, not down
            else:
                up, lat = tcp(ip, port)
                rec["rpc"] = up; rec["rpc_ms"] = lat
        elif ip and role == "surface":
            up, _ = tcp(ip, port or 26101)
            rec["surface"] = up
        if ip and ip not in serve_seen:
            # D46: a second card on a box (an eGPU) is its own node with the SAME ip -- probe :8899 once per BOX, or the
            # line reads "hosts: nuc-155h(authority), nuc-5060ti(authority)" as if there were two of them.
            serve_seen.add(ip)
            up, _ = tcp(ip, SERVE_PORT, timeout=1.5)
            if up:
                fab = http_json(f"http://{ip}:{SERVE_PORT}/fabric.json")
                rec["serve"] = bool(fab)
                if fab:
                    rec["serve_mode"] = fab.get("mode"); rec["serve_build"] = fab.get("coord_version")
                    if fab.get("gpu_access"):                       # the host cannot open its own GPU (render group)
                        rec["gpu_problem"] = fab["gpu_access"]
        nodes.append(rec)
    # the authority itself (this box)
    fab = http_json(f"http://127.0.0.1:{SERVE_PORT}/fabric.json")
    authority = {"serve": bool(fab), "build": fab.get("coord_version") if fab else None,
                 "nodes_seen": len(fab.get("nodes", [])) if fab else 0,
                 "gpu_problem": (fab.get("gpu_access") or "") if fab else ""}

    # every node counts, by its OWN liveness signal: a donor by its RPC port, a surface by its port, an
    # anchor-only box (no RPC endpoint) by whether its serve answers. "5/5 donors" read as a mystery when
    # the fleet has 7 nodes.
    def alive(n):
        if "rpc" in n: return n["rpc"]
        if "surface" in n: return n["surface"]
        return bool(n.get("serve"))
    for n in nodes:
        n["up"] = alive(n)
    up = [n for n in nodes if n["up"]]
    down = [n["id"] for n in nodes if not n["up"]]
    donors = [n for n in nodes if "rpc" in n]
    hosts = [f"{n['id']}({n.get('serve_mode', '?')})" for n in nodes if n.get("serve")]
    # a host that is UP but cannot open its own GPU is not "all-up" in any sense the user cares about: every model
    # load there fails. Flag it by name, with the fix, on the same line the morning check reads.
    gpu_problems = [{"id": n["id"], "problem": n["gpu_problem"]} for n in nodes if n.get("gpu_problem")]
    if authority.get("gpu_problem") and not any(g["problem"] == authority["gpu_problem"] for g in gpu_problems):
        gpu_problems.append({"id": "authority", "problem": authority["gpu_problem"]})
    summary = {"ts": now.isoformat(timespec="seconds"), "nodes_up": len(up), "nodes_expected": len(nodes),
               "donors_up": sum(1 for n in donors if n["rpc"]), "donors_expected": len(donors),
               "down": down, "hosts": hosts, "authority": authority, "nodes": nodes, "gpu_problems": gpu_problems}
    pinned_ids = [n["id"] for n in nodes if n.get("pinned")]
    summary["pinned"] = pinned_ids
    line = (f"{now:%Y-%m-%d %H:%M} nodes {len(up)}/{len(nodes)}"
            + (f" DOWN:{','.join(down)}" if down else " all-up")
            + (f" PINNED:{','.join(pinned_ids)}" if pinned_ids else "")
            + f" (donors {summary['donors_up']}/{len(donors)})"
            + f" | hosts: {', '.join(hosts) or 'none'}"
            + f" | authority: {'up' if authority['serve'] else 'DOWN'} ({authority['build']})"
            + "".join(f" | NO-GPU {g['id']}: {g['problem']}" for g in gpu_problems))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # The authority's fleet.json carries each node's status/last_seen, but the serve only writes them when it PLANS
    # (a browser poll heartbeats without persisting, by design -- a read must not rewrite the source of truth on every
    # refresh). So a quiet fleet leaves that file days stale: the Tegra came back and fleet.json still said
    # "down, last_seen 2026-09-20" (2026-09-22). This pass already probed every node -- write what it saw, so the
    # persisted record (and the Pi's nightly cold-spare copy of it) is never more than 15 minutes old.
    try:
        changed = False
        for n in nodes:
            d = next((x for x in fleet.get("donors", []) if x.get("id") == n["id"]), None)
            if d is None or "rpc" not in n:
                continue
            new = "up" if n["up"] else "down"
            if d.get("status") != new:
                d["status"] = new; changed = True
            if n["up"] and d.get("last_seen") != now.isoformat(timespec="seconds"):
                d["last_seen"] = now.isoformat(timespec="seconds"); changed = True
        if changed:
            ftmp = FLEET + ".tmp"
            with open(ftmp, "w", encoding="utf-8") as f:
                json.dump(fleet, f, indent=1)
            os.replace(ftmp, FLEET)
            print(f"[watchdog] fleet.json liveness refreshed: {', '.join(n['id'] + '=' + ('up' if n['up'] else 'down') for n in nodes if 'rpc' in n)}")
    except Exception as e:
        print(f"[watchdog] could not refresh fleet.json liveness: {e}", file=sys.stderr)

    tmp = LATEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    os.replace(tmp, LATEST)
    print(line)
    provision_chat_ui()


def provision_chat_ui():
    """If the Docker UI tier runs on this box, provision Open WebUI (integrations/openwebui/provision.py) every pass:
    a GGUF dropped in the folder shows up in the chat picker with Open WebUI's defaults (builtin tools ON, no prompt)
    until it is provisioned -- within 15 min of landing, not never. First-time-only per model: a user's own changes
    are never reverted. Quiet unless something changed (that line lands in the cron log)."""
    import shutil, subprocess
    if not shutil.which("docker"):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(os.path.dirname(here), "integrations", "openwebui")
    prov, prompt = os.path.join(src, "provision.py"), os.path.join(src, "system_prompt.txt")
    if not (os.path.exists(prov) and os.path.exists(prompt)):
        return
    try:
        up = subprocess.run(["docker", "ps", "--filter", "name=^open-webui$", "--format", "{{.Names}}"],
                            capture_output=True, text=True, timeout=10).stdout.strip()
        if up != "open-webui":
            return
        subprocess.run(["docker", "cp", prov, "open-webui:/tmp/provision.py"], check=True, timeout=20, capture_output=True)
        subprocess.run(["docker", "cp", prompt, "open-webui:/tmp/system_prompt.txt"], check=True, timeout=20, capture_output=True)
        out = subprocess.run(["docker", "exec", "open-webui", "python3", "/tmp/provision.py", "--quiet"],
                             capture_output=True, text=True, timeout=60)
        if out.stdout.strip():
            print("[watchdog] chat UI provisioned: " + " | ".join(out.stdout.strip().splitlines()))
    except Exception as e:
        print(f"[watchdog] chat UI provisioning skipped: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
