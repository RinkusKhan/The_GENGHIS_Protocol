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
| `prefer` | *optional* the model files this role is best at, in order. The first one the library holds that meets `requires` runs; if none can, the `goal` picks, and the reply says why. Softer than `model`. |
| `think` | *optional* `true`/`false`: whether a reasoning model thinks before answering in this role. Unset = the model's default from config `thinking`. |
| `max_tokens` | *optional* this role's answer budget when the chat client names none; may go above the usual 4,096 ceiling, up to 16,384. A role that thinks needs it: a Researcher thinking through two sources used ~2,800 tokens before answering. |
| `model` | *optional* hard pin to one GGUF. Honoured even if it falls short (you are told). |
| `requires` | capabilities the role needs; GENGHIS checks them against the model and **refuses out loud** rather than half-working |
| `system` / `system_file` | the system prompt, inline or in a neighbouring file |
| `tools` | the tool belt this role expects. A name that matches an **adapter** in `adapters/` (armed, and reachable) becomes tools GENGHIS runs itself (D49); tools your chat client supplies stay the client's. GENGHIS tells you when a role that needs tools was called with none. |
| `max_rounds` | *optional* how many tool round-trips one request may take (default 6, at most 24). A coder reads, edits, builds, reads the errors and edits again: give it 12-16. |
| `knowledge` | a folder of documents the role **reads**: each request is searched and up to 4 cited passages go in front of the question. See [Knowledge](#knowledge) below. |
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

### Knowledge

```json
"knowledge": "~/papers"
```

An absolute path, `~/…`, or a path relative to the role file (so a home you copy elsewhere keeps working). On every
request GENGHIS searches the folder with your latest message and puts up to **4 passages** (at most about 6,000
characters) inside the role's system prompt, tagged `[K1]`…`[K4]` with the file (and page, for a PDF) so the model can
cite them. Nothing leaves the box, and there is no index file: the folder is indexed in memory and re-indexed when a
file changes.

- **What it reads:** `.txt` `.md` `.markdown` `.rst` `.csv` `.json` `.html` `.htm`, and **PDFs if `pypdf` is
  installed** in the Python the serve runs (Ubuntu/Debian: `sudo apt install python3-pypdf`; Windows:
  `"<that python.exe>" -m pip install --user pypdf`, since `py` can launch a *different* Python). The installers offer
  it on every box a person uses, and `verify` checks it against the running serve and prints the exact command. Installing it takes effect on the next question, with no restart. A scanned PDF has no text to read
  and needs OCR first.
- **What it tells you:** anything it could not read (a PDF without `pypdf`, a scanned PDF, an unsupported type, a file
  over 8 MB, a folder past 2,000 files) is counted and named. You see that in `genghis roles <id>`, in `/roles.json`, and
  in the note on every request. It never quietly reads less than you think.
- **See what the model would get:** `genghis roles <id> "your question"` prints the passages, best first.
- **What it is not:** keyword search (BM25). It finds passages that share *words* with your question, not ones that
  share *meaning*: ask with the words your documents use. A question that shares no word with anything adds nothing,
  and says so.
- **Citing is up to the model.** Passages are tagged for citation, but a small model may use them without citing. If
  a role must cite, pin or require a larger model.
- **Where the folder lives:** on the box your chat client talks to. If that box hands the chat to another host, the
  passages travel with it.

## Adapters — tools GENGHIS runs itself (D49, D53)

`adapters/<id>.json` connects a program (Blender, Visual Studio, your files, the web) to any role whose `tools` names it. The model never writes code:
it picks one of the file's named **operations** and gives arguments, which GENGHIS binds as plain values.

- **`enabled`** is `false` in the shipped file, on purpose. Switching it on lets a model change that program (in
  Blender: look, add, move, delete by exact name). GENGHIS never switches it on for you.
- **`node`** says which fleet host runs the program, when it isn't the box answering the chat. Blender's add-on listens
  only on its own machine (its one operation is "run this Python", so that port must stay closed), so a role running on
  another host sends each named operation to that node's GENGHIS serve, which runs it there. The node's **own copy** of
  the file decides whether it is switched on, and it accepts the request only from another GENGHIS host.
- **`web`** (`"transport": "web"`) is built into GENGHIS: `search` and `read_page`, nothing else, on the public
  internet only (a private or local address is refused, at every redirect too). Off as shipped; the stock
  Researcher names it, so switching it on turns that role from a reader into a researcher.
- **`mcp`** (`"transport": "mcp"`, D55) plugs in any tool server that speaks the Model Context Protocol, such as
  Visual Studio's ([`visualstudio.json`](adapters/visualstudio.json)): GENGHIS connects to its `url` and offers the model
  **only the tools the file's `allow` list names**. An MCP adapter with no `allow` offers nothing. A server's other tools
  never reach the model.
- **`files`** (`"transport": "files"`, D55) is built in ([`files.json`](adapters/files.json)): list, read, find and search
  files **only inside the folders its `roots` name**; symlinks and `..` cannot leave them, and key and credential files
  (`.env`, `*.pem`, `id_rsa`, `.ssh/`) are never opened. `"write": true` adds `write_file` and `replace_text`; every write
  keeps the previous version under `<root>/.genghis-backup/` and the chat shows the diff. There is no delete.
  **Workspace mode** (what the Control Room's *Coder workspace* card sets): one `root` you can point anywhere (`D:\Coder`,
  or `F:\Project` for a quick fix on a USB stick) and a `project` folder inside it; the Coder then sees only that
  project, and its backups stay inside it, so archiving a project takes its history along. A system drive's root, the
  OS and program folders, your whole user folder and its AppData, any `.ssh`, and any folder holding GENGHIS's own home
  are refused as a root.
- **Keep web and local access in different roles.** A role that can both fetch web pages and reach your files or programs
  can be steered by a page it reads into sending what it sees out, in the address of the next page it fetches. GENGHIS
  warns in the chat when a role's belt mixes them.
- A tool-driving role needs a model that makes **real** tool calls. Some fine-tunes pass the template check but write
  their calls as plain text (BlenderLLM does), so nothing ever happens. `prefer` a general model that calls tools.

## Services — programs you start and stop from the Control Room (D56)

`services/<id>.json` puts a card for one program on the Control Room, with **Start**, **Stop** and **Open**. It lives in
the home of the box that runs the program; every host's Control Room shows it and passes a click to that box, which runs
only what its own file says. See [`services/example.json.example`](services/example.json.example); rename it to `.json`
to use it.

| field | meaning |
|---|---|
| `id`, `name`, `desc`, `glyph`, `cls` | the card (`cls`: `steel` or `ember`) |
| `start` | the command that starts it, as a list of arguments (or one shell string). It may keep running; GENGHIS starts it detached, so it outlives a serve restart |
| `stop` | the command that stops it. **It must not exit 0 until the program is gone**: an app can close its port and hang in shutdown while still holding its GPU memory. Escalate to a hard kill if you need to; a non-zero exit keeps the card held |
| `probe` | an address that answers (any HTTP reply) while it runs |
| `open` | the link behind **Open**; `open_note` is said on the card while it runs |
| `gpu` | `true` = it needs the box's whole GPU: while it runs, GENGHIS plans nothing onto that card, hands no chat to it, reclaims pooled shards there, and shows it HELD. Start asks before unloading a model GENGHIS keeps warm there |
| `node` | which fleet node's GPU it uses (default: this box) |
| `start_s` | how long it may take to answer after starting (default 120 s) |
| `log` | a file for its output, rewritten at each start. Use this rather than `cmd /c ... > file` on Windows: GENGHIS starts the program detached (no console), and cmd's `>` then reaches the program as nothing (0-byte log) |

## Not roles

`personas/`, `routines/`, `voices/`, `dialplan/` are the other kinds of choice a home holds. They are
placeholders here; the mechanisms that read them are built in their own phases.
