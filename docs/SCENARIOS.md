# Scenarios — what GENGHIS is for

Two fictitious people, written to keep the project honest. Every design decision about *where a model
lives* and *how you control it* should be checked against both. If a feature helps neither, it is not
the project. (Names and situations are invented; nothing here describes a real person or site.)

## Case 1 — Larry: one small host, a drawer full of donors

Larry has a laptop with a lower-end RTX 30-series card, an old PC, three Android phones and a Pi or
three. None of them can hold the model he wants. He finds GENGHIS on GitHub, follows the Guide, and
installs it correctly the first time — because the Guide is pinned to the release and checks its own
version. Now the memory of everything he already owns is one pool, and a model that was out of reach
runs. He did not work extra days to save for RAM nobody can afford.

What Larry's fabric looks like: **one host** (the laptop — the only card that can anchor a model) and
**many donors** (everything else: memory the pool borrows over the LAN). The dashboard he cares about
is one column: **the fabric** — which model is up across all of it, what it spans, how fast it honestly
is. Per-host warm pools barely matter to him; there is one host.

What this case demands of the project:
- **Install that works, once** — the pinned, versioned Guide and `provision` (release checklist §4b).
- **The pooled model as a first-class thing** on the dashboard, warm if it can be, with its plan visible
  (which nodes, which anchor, measured speed) — not a cold per-request run hidden behind a number.
- **Every box is a donor; every box with a card is also a host.** Larry's old PC with a GPU earns a
  column for free the day it gets one.
- **Phones as donors** — plausible (`ggml-rpc-server` under Termux, `termux-wake-lock`), untested.
  Slow memory over Wi-Fi is still memory that lets the model load at all — the thesis exactly. The
  Guide must not claim it until one phone has done it.

## Case 2 — Johnny: a procedure of formations over Tailscale

Johnny is a contractor supporting an advanced system for a government aerospace programme. On the day a
new piece is integrated, calibration means running the system's numbers through a modelled world across
every combination of weather, flight regime, material stress and temperature, each level predictable but
different in reference to the others. His secured laptop holds a model that does the integration and
initial priming. He then connects over his own Tailscale to **two other models, each responsible for its
own checks**. When those pass he stays connected, **drops all models and loads one huge model** to run the
"world" for every condition.

What Johnny's fabric looks like: his laptop is a **host that is never a donor** (away over the internet —
no RPC across it, D35). The site holds hosts and donors he never touches by hand. His day is three
**formations**, in order:

| step | formation | what is warm where |
|---|---|---|
| 1 | `prime` | his own model, solo on his laptop |
| 2 | `checks` | host A: model A · host B: model B (each warm on its own card) |
| 3 | `world` | everything unloaded; one huge model warm **across the whole site fabric** |

He does not click twelve buttons in the right order every morning. Each step is **one action** —
a click, a CLI line, or a POST from the procedure that drives the calibration.

What this case demands of the project:
- **Formations** — a named layout of *what is warm where*, including the fabric model, switchable in one
  action from the Control Room, the CLI or the API. The switch computes the unloads, **says what it will
  unload** (the eviction honesty of D38, fleet-wide), and does them in the right order.
- **The fabric model can be warm** (pooled residency) and, while it is, it **pins** the donors it spans —
  a formation with a fabric model and one with per-host pools are different modes, chosen, never
  stumbled into.
- **One control spot that reaches every card**, from the LAN or the tailnet: the authority's page or API;
  the authority does the reaching. (Built 2026-09-15 for warm/unload; formations extend it.)
- **Away hosts are hosts, not donors** — Johnny's laptop uses the fabric; it is never pulled into it.

## The test these two set

- A feature that makes Larry's *one* model run, or Johnny's *three steps* one action each, belongs.
- A dashboard element must answer "what is warm where, and what would change if I do this?" for both:
  Larry's fabric column; Johnny's formation switch.
- Speed is reported as measured, never implied: a warm pooled model is *warm*, not *fast*.

See D39 in [DECISIONS.md](../DECISIONS.md) for the design these cases shaped.
