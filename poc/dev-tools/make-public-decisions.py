#!/usr/bin/env python3
"""Generate the PUBLIC decision log from the private one (D22 · D36).

`DECISIONS.md` is the most valuable document in this repo for someone who did not build it — it is the *why*
behind every rule the code enforces — and it is also the file that names the reference lab throughout. So the
public copy is **generated, never hand-written**: one source of truth, and a public file that cannot drift.

    python3 poc/dev-tools/make-public-decisions.py            # writes docs/DECISIONS-public.md
    python3 poc/dev-tools/make-public-decisions.py --check    # exit 1 if the file on disk is out of date

What it redacts, in order:
  1. **Your own names**, from `$GENGHIS_HOME/REDACTIONS.tsv` (private, D36): `pattern<TAB>replacement`, one per
     line, applied as regexes in order. This is where hostnames/accounts/domains become role words ("the
     authority", "the maintainer") so the prose still reads like prose.
  2. **Anything in `$GENGHIS_HOME/LEAK_WORDS.txt` that rule 1 did not already handle** — replaced with a generic
     placeholder, so a word you forgot to map can never ship. (It will read awkwardly; that is the point — you
     will see it and add a mapping.)
  3. **Shapes**, whoever you are: private/CGNAT/link-local addresses, MAC addresses, e-mail addresses, Windows
     user directories, `/home/<user>/`. Documented ranges (`100.64.0.0/10`), loopback and placeholders are kept.

It changes nothing else: every decision, every reason, every measured number stays. A redaction that would eat a
*number* is a bug — the numbers are the evidence.
"""
import os, re, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "DECISIONS.md")
# At the repo ROOT, not in docs/. The body is copied verbatim from the root-level journal, so its links are
# root-relative; putting the copy in docs/ broke every one of them, and the public repo renames this file to
# DECISIONS.md at the root anyway. Generating it where it will actually live keeps the links correct in both
# repos instead of neither.
OUT = os.path.join(ROOT, "DECISIONS-public.md")
def _home():
    """The private home repo (D36). $GENGHIS_HOME wins; otherwise try the usual spots -- and accept a Git-Bash
    style path (/x/genghis-home) on Windows, where Python needs X:/genghis-home."""
    cands = [os.environ.get("GENGHIS_HOME"), os.path.join(os.path.expanduser("~"), "genghis-home")]
    for c in cands:
        if not c:
            continue
        for v in (c, re.sub(r"^/([a-zA-Z])/", lambda m: m.group(1) + ":/", c)):
            if os.path.isdir(v):
                return v
    return cands[-1]

HOME = _home()

HEADER = """<!-- GENERATED — do not edit by hand.
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

"""

# --- shapes: never right in a public file, whoever you are -------------------------------------------------
KEEP = re.compile(r"100\.64\.0\.0/10|10\.0\.0\.0/8|192\.168\.0\.0/16|172\.16\.0\.0/12|127\.0\.0\.1|0\.0\.0\.0|"
                  r"192\.168\.1\.x|192\.168\.x\.x|169\.254\.x\.x")
SHAPES = [
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "<node-ip>"),        # NOT (?:192\.168|10)\. + three octets:
    (re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "<node-ip>"),     # that asks for FIVE and matches nothing
    (re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"), "<node-ip>"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"), "<tailnet-ip>"),
    (re.compile(r"\b169\.254\.\d{1,3}\.\d{1,3}\b"), "169.254.x.x"),
    (re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b"), "<mac>"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<you>@<your-domain>"),
    # the replacement is appended literally (not via re.sub), so it needs single backslashes, not escaped ones
    (re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+"), "C:\\Users\\<you>"),
    (re.compile(r"/home/[A-Za-z0-9._-]+/"), "/home/<you>/"),
]


def load_map():
    """[(compiled pattern, replacement)] from the private REDACTIONS.tsv, longest pattern first so a longer
    name is redacted before a shorter one that is a prefix of it."""
    path = os.path.join(HOME, "REDACTIONS.tsv")
    rules = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
                    continue
                pat, rep = line.split("\t", 1)
                rules.append((pat.strip(), rep.strip()))
    except OSError:
        return None
    rules.sort(key=lambda r: -len(r[0]))
    return [(re.compile(p), r) for p, r in rules]


def load_words():
    path = os.path.join(HOME, "LEAK_WORDS.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return sorted((l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")), key=len, reverse=True)
    except OSError:
        return []


def redact(text, rules, words):
    def shapes(line):
        # apply shape rules, but never touch a fragment the KEEP list protects
        for pat, rep in SHAPES:
            parts, pos = [], 0
            for m in pat.finditer(line):
                frag = m.group(0)
                keep = KEEP.search(line[max(0, m.start() - 12):m.end() + 12])
                parts.append(line[pos:m.start()])
                parts.append(frag if (keep and frag in keep.group(0)) else rep)
                pos = m.end()
            parts.append(line[pos:])
            line = "".join(parts)
        return line

    for pat, rep in (rules or []):                   # 1. your names -> role words, so the prose still reads
        text = pat.sub(rep, text)
    # 1b. A role word that lands where a name used to start a sentence has to be capitalised, or the public
    #     log reads "the maintainer's ruling:" mid-paragraph. Generic: only the map's OWN replacements are
    #     touched, and only at a sentence boundary — nothing else in the prose is re-cased.
    for _, rep in (rules or []):
        if rep[:1].islower():
            text = re.sub(r"(^|(?<=\. )|(?<=\? )|(?<=! ))" + re.escape(rep),
                          lambda m: m.group(1) + rep[0].upper() + rep[1:], text, flags=re.M)
    # 2. shapes -> placeholders WHILE THEY ARE STILL WHOLE: a word-list entry that is an address PREFIX (a subnet
    #    you listed) would otherwise eat the start of an address and leave a stub no shape rule can recognise.
    text = "\n".join(shapes(l) for l in text.split("\n"))
    # 3. anything the map forgot: generic, deliberately awkward. THIS FILE CARRIES NO NAMES -- it used to
    #    exempt one first name by hardcoding it here, which put that name in a shipping file to keep it OUT
    #    of the output. A word you want rendered readably belongs in your private REDACTIONS.tsv instead.
    for w in words:
        if w and w in text:
            text = re.sub(re.escape(w), "<redacted>", text)
    return text


def build():
    with open(SRC, encoding="utf-8") as f:
        body = f.read()
    # drop the private header (everything before the first ---) and re-title
    i = body.find("\n---\n")
    body = body[i + 5:] if i != -1 else body
    rules, words = load_map(), load_words()
    if rules is None:
        print(f"note: no {HOME}/REDACTIONS.tsv — names will fall back to <redacted> (readable prose needs the map)",
              file=sys.stderr)
    out = HEADER + redact(body, rules, words).lstrip("\n")
    out = out.rstrip("\n") + f"\n\n---\n_Generated from the project's private decision log on {datetime.date.today().isoformat()}._\n"
    return out


def main():
    text = build()
    if "--check" in sys.argv:
        try:
            with open(OUT, encoding="utf-8") as f:
                cur = f.read()
        except OSError:
            cur = ""
        # ignore only the trailing generation date
        strip = lambda s: re.sub(r"_Generated from .*_\n?$", "", s).rstrip()
        if strip(cur) != strip(text):
            print(f"{os.path.relpath(OUT, ROOT)} is OUT OF DATE — run: python3 poc/dev-tools/make-public-decisions.py")
            return 1
        print(f"{os.path.relpath(OUT, ROOT)} is up to date.")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    n = text.count("\n## D")
    print(f"wrote {os.path.relpath(OUT, ROOT)} — {n} decisions, {len(text.splitlines())} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
