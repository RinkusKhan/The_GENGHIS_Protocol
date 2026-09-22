# `home.example/` — the shape of a GENGHIS home

GENGHIS ships **mechanisms**. What *your* fleet actually does — which roles exist, what they research,
whose voice answers the phone — is a **choice**, and choices live in a home directory that the project
never sees (D36).

This folder is the example. Copy it somewhere private and point GENGHIS at it:

```bash
export GENGHIS_HOME=/path/to/my-home      # or set "home_dir" in config.json
```

With `GENGHIS_HOME` unset, GENGHIS reads this folder, so a fresh install has working stock roles instead
of an empty feature. Nothing here is anyone's real setup.

## `roles/`

One `.json` file per role. A **role** is not a model — it is

```
base model + system prompt + tool belt + knowledge + effort goal
```

and two roles can share one base model. You say *what you want done*; GENGHIS decides what runs it.

Call a role from any OpenAI-compatible client by name: `genghis-researcher`, `genghis-coder`.

### The fields

| field | meaning |
|---|---|
| `id` | lowercase; becomes `genghis-<id>`. Cannot be an effort goal name. |
| `name`, `description` | what a human sees in a model dropdown |
| `goal` | `fastest` · `balanced` · `fit` · `biggest` — which tier picks the model |
| `model` | *optional* hard pin to one GGUF. Honoured even if it falls short (you are told). |
| `requires` | capabilities the role needs; GENGHIS checks them against the model and **refuses out loud** rather than half-working |
| `system` / `system_file` | the system prompt, inline or in a neighbouring file |
| `tools` | the tool belt this role expects. **Declared, not installed** — GENGHIS cannot reach into your chat client; it tells you when a role that needs tools was called without any. |
| `knowledge` | a folder of documents. **Declared only — retrieval is not built yet**, and GENGHIS says so rather than pretending. |
| `voice` | a speech service for ears and mouth. **Declared only — not wired yet.** |

### `requires` keys

| key | checked against |
|---|---|
| `tools` | `"native"` · `"partial"` · `"none"` — read from the model's own chat template |
| `tool_results` | does the template render a tool's reply? **If false, every tool result is silently dropped** and the model answers from imagination. This is the one that bites without a symptom. |
| `ctx_train` | the model's trained context length |
| `vision` | a matching projector is actually paired with the model |
| `reasoning` | the model emits reasoning content |

`genghis roles` lists what each role would really run. `genghis roles <id>` explains one in full,
including every substitution and every unmet requirement.

## Not roles

`personas/`, `routines/`, `voices/`, `dialplan/` are the other kinds of choice a home holds. They are
placeholders here; the mechanisms that read them are built in their own phases.
