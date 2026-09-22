# Models — the one-folder rule

Everything about models in GENGHIS comes down to **one idea**:

> **Put your GGUF files in one folder. GENGHIS lists them, you pick which one each speed setting uses, and it runs them — pooling compute and memory across your fleet.**

That's it. You don't stage, sync, or copy anything to other machines. The donors that lend you their GPU/RAM **never need the model file** — the machine you launch from streams the *compute* to them, not the model.

---

## 1 · Where models go

Drop `.gguf` files in your models folder on the machine you launch from:

| Platform | Default folder |
|---|---|
| Windows | `E:\models\` (or wherever `GENGHIS_MODEL` points) |
| Linux / Pi | `~/genghis/models/` |

GENGHIS scans that folder (plus the folder your configured model lives in). To add more folders, set
`"models_dirs": ["D:/more-models", "..."]` in `config.json`.

## 2 · See what GENGHIS found

```bash
python genghis_coordinator.py registry
```
```
== model registry (5 model(s)) ==
  Llama-3.3-70B-Instruct-Q4_K_M.gguf   40550 MB
  qwen2.5-1.5b-instruct-q4_k_m.gguf     1065 MB
  Qwen2.5-32B-Instruct-Q4_K_M.gguf     18931 MB
  SmolVLM-500M-Instruct-Q8_0.gguf        416 MB  [VLM +mmproj]
  ...
== goal -> model ==
  fastest  -> qwen2.5-1.5b-instruct-q4_k_m.gguf
  balanced -> (default: Qwen2.5-32B-Instruct-Q4_K_M.gguf)
  ...
```
A model tagged **`[VLM]`** is a vision model — it needs its `mmproj` companion file and an image, so it isn't
offered as a plain text chat model.

## 3 · Pick which model each speed setting uses

The four settings — **fastest · balanced · fit · biggest** — are *routing goals* (how the fleet is assembled),
not models. You choose which model each one runs:

```bash
python genghis_coordinator.py registry map fastest  qwen2.5-1.5b     # fastest -> the small, instant model
python genghis_coordinator.py registry map biggest  Llama-3.3-70B    # biggest -> the model no single box holds
python genghis_coordinator.py registry default Qwen2.5-32B           # everything else -> this
```
Names are fuzzy — a unique substring is enough. Clear one with `registry unmap <goal>`.

## 4 · Use it

Any OpenAI-compatible client (Open WebUI, the `openai` SDK, `curl`) sees **both** the goals and every model in
your folder, listed at `/v1/models`. Pick either:
- **A speed setting** — `genghis-fastest`, `genghis-biggest`, … → runs the model you mapped to it.
- **A specific model by name** — `Qwen2.5-32B-Instruct-Q4_K_M.gguf` → runs exactly that one.

In **Open WebUI**, they all appear in the model dropdown. Switch freely.

---

## Advanced (optional): sharing models across MULTIPLE launch machines

**Skip this unless you launch models from more than one computer.** If you do, the coordinator can hold a
shared **model store** so you don't copy a 40 GB file to every machine — see the collapsible *Advanced* block
in [INSTALL.md](../INSTALL.md#1--coordinator-a-raspberry-pi-or-any-always-on-linux-box). It is always a
**manual, opt-in** step (`scp` a model up, then `models pull`); GENGHIS never pushes your files anywhere on
its own. For a single-machine setup, ignore it entirely — the one-folder rule above is all you need.

---

### Model residency (warm models) — on by default

GENGHIS keeps the model you're using **warm in memory** (a resident `llama-server`), so only the **first**
call to a model loads it from disk — every call after that is fast. Switching to a different model loads the
new one once, then it's warm too. Measured on a 32B: **~15 s first call, ~2.8 s after** (it used to be ~15 s
*every* time).

- A **big pooled model** (one too large for a single machine, e.g. a 70B on `biggest`) isn't kept warm — it's
  split across the fleet on demand and frees the local GPU when done.
- Turn residency off with `GENGHIS_RESIDENCY=0` (or `"residency": false` in `config.json`) if you'd rather
  reclaim the VRAM between calls.
- Needs the `llama-server` binary built alongside `llama-cli` (`cmake --build … --target llama-server`).
