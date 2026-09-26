# Models — the one-folder rule

Everything about models in GENGHIS comes down to **one idea**:

> **Put your GGUF files in one folder — the authority's. GENGHIS lists them, you pick which one each speed setting uses, and it runs them — pooling compute and memory across your fleet.**

That's it. You don't sync or copy anything to other machines by hand: a host that needs a model fetches it from the
authority's library by name, and the donors that lend their GPU/RAM **never need the model file** — the host streams
each donor its slice of the weights over RPC (and a donor keeps that slice cached on its own disk for next time).

---

## 1 · Where models go

Drop `.gguf` files in the **authority's** models folder — that folder is the fleet's **model library**, served at
`/models`:

| Where | Default folder |
|---|---|
| Any box (installer layout) | `<repo>/poc/models/` — e.g. `~/genghis-src/poc/models/` on Linux (override: `GENGHIS_MODELS_DIR`, or the Windows installer's `-ModelsDir`) |

Every box scans its own folder (plus the folder its configured model lives in). To add more folders on a box, set
`"models_dirs": ["D:/more-models", "..."]` in that box's `config.json`. A one-box install has one folder and that's all.

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

## More than one host: the library does the copying

With several hosts (say the authority and your laptop), you still stage a model **once**, in the authority's folder:
```bash
scp your-model.gguf <you>@<authority>:~/genghis-src/poc/models/
```
Any host can then run it by bare name: if the file isn't local it is **fetched from the library** (resumable, into
that host's own `poc/models/`), and the chat says so while it happens. A host's `serve` also pre-fetches the models its
speed settings use when it starts. To fetch one ahead of time: `models` lists the library, `models pull <name.gguf>`
downloads one. GENGHIS never pushes your files anywhere on its own — hosts pull from the library you staged.

---

### Model residency (warm models) — on by default

GENGHIS keeps the model you're using **warm in memory** (a resident `llama-server`), so only the **first**
call to a model loads it from disk — every call after that is fast. Switching to a different model loads the
new one once, then it's warm too. Measured on a 32B: **~15 s first call, ~2.8 s after** (it used to be ~15 s
*every* time).

- A host keeps **several** models warm at once, as many as its card holds (D38), and hands a request to another
  host that already has the model warm rather than evicting its own.
- A **big pooled model** (one too large for a single machine, e.g. a 70B on `biggest`) can be kept warm **across the
  fabric** too: drag it onto the fabric strip in the Control Room's *Pool* (D39). Otherwise it is split across the
  fleet on demand and frees the cards when done.
- Turn residency off with `GENGHIS_RESIDENCY=0` (or `"residency": false` in `config.json`) if you'd rather
  reclaim the VRAM between calls.
- Needs the `llama-server` binary built alongside `llama-cli` (the installers build it).

### Roles are models too

`/v1/models` also lists **roles** (`genghis-researcher`, `genghis-coder`, and any you add in your home folder): a
role is instructions + tools + a knowledge folder + a speed setting, and GENGHIS picks the model it runs from the
role's `"prefer"` list and what your library holds. See [USAGE.md → roles](../USAGE.md#say-what-you-want-done--roles-d48d50)
and [`home.example/README.md`](../home.example/README.md).
