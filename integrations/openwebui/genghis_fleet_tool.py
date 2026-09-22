"""
title: GENGHIS Fleet
author: The GENGHIS Protocol
description: Let the chat ask the fleet about itself — which nodes are up, what is warm, and how a goal would be planned.
version: 0.1.0
requirements: requests
"""
# Open WebUI "Tool" (Workspace -> Tools -> + -> paste). Each public method with type hints + a docstring becomes a
# callable tool. Runs INSIDE the Open WebUI container: the coordinator on the same box is host.docker.internal:8899
# (the compose file adds that host alias). Change `base` in the Valves if the UI runs elsewhere.

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        base: str = Field(default="http://host.docker.internal:8899",
                          description="The GENGHIS serve this UI talks to (authority or host)")

    def __init__(self):
        self.valves = self.Valves()

    def fleet_status(self) -> str:
        """Report the GENGHIS fleet right now: how many nodes are up, each node's state, the pooled memory,
        the default goal and which model is warm on this host. Use when the user asks how the fleet, the
        cluster, the nodes, the fabric or the Fold is doing."""
        try:
            fab = requests.get(f"{self.valves.base}/fabric.json", timeout=8).json()
            reg = requests.get(f"{self.valves.base}/registry.json", timeout=8).json()
        except Exception as e:
            return f"Could not reach the coordinator at {self.valves.base}: {e}"
        lines = [f"Fleet: {fab.get('up')}/{fab.get('total')} nodes up · coordinator {fab.get('coordinator')} ({fab.get('mode')})"]
        for n in fab.get("nodes", []):
            extra = " · ".join(x for x in (n.get("thrput"), n.get("free")) if x)
            lines.append(f"- {n.get('name') or n.get('id')}: {n.get('state')} — {n.get('why', '')}" + (f" ({extra})" if extra else ""))
        res = reg.get("resident") or {}
        lines.append(f"Warm on this host: {res.get('model') if res.get('up') else 'nothing'} · default goal: {reg.get('default_goal')}")
        gm = reg.get("goal_models") or {}
        if gm:
            lines.append("Goal map: " + ", ".join(f"{k} → {v}" for k, v in gm.items()))
        wd = None
        try:
            wd = requests.get(f"{self.valves.base}/watchdog.json", timeout=5).json()
        except Exception:
            pass
        if wd and wd.get("ts"):
            lines.append(f"Watchdog: verified {wd['ts'][11:16]} · {wd.get('nodes_up')}/{wd.get('nodes_expected')} nodes")
        return "\n".join(lines)

    def models_available(self) -> str:
        """List the models in the GENGHIS registry (name and size) and the effort goals a chat can pick."""
        try:
            reg = requests.get(f"{self.valves.base}/registry.json", timeout=8).json()
        except Exception as e:
            return f"Could not reach the coordinator: {e}"
        out = [f"- {m['id']} ({m.get('size_mb', 0) / 1024:.1f} GB, {m.get('kind', 'text')})" for m in reg.get("models", [])]
        return "Models:\n" + "\n".join(out) + "\nGoals: " + ", ".join(reg.get("goals", []))
