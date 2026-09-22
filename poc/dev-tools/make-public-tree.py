#!/usr/bin/env python3
"""Build the PUBLIC tree from this private working tree — reproducibly, and check it.

The public repo is born from a single clean commit (D22). That is not "copy the tree minus a few files":
the journal files go, the generated decision log is renamed to the place it is referenced from, and the
README carries two things that are true of the PRIVATE repo and false of the public one — a "Private
preview" notice and a documentation table listing files that do not ship. Doing that by hand once is how a
release goes out with seven broken links on its front page (which is exactly what the first attempt did).

    python3 poc/dev-tools/make-public-tree.py <outdir> [--commit]

It exports `git archive HEAD` (tracked files only, so nothing untracked can sneak in), applies the
transforms, then VERIFIES: no private word from $GENGHIS_HOME/LEAK_WORDS.txt, no link to a file that does
not exist, no leftover journal file. It exits non-zero if any check fails — a broken public tree should not
be committable by accident.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# Everything in the "does NOT ship" column of docs/PUBLIC_RELEASE.md §2.
DROP = [
    "STATUS.md", "DECISIONS.md", "Project.md",
    "poc/RESULTS.md", "poc/coordinator-notes.md",
    "docs/PUBLIC_RELEASE.md",
]
RENAME = [("DECISIONS-public.md", "DECISIONS.md")]     # generated -> the name the docs link to


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


def export(outdir):
    """Tracked files only — `git archive` cannot pick up an untracked stray."""
    if os.path.exists(outdir):
        # git writes its objects read-only; on Windows that makes rmtree fail, so a second run of this
        # tool could not clear its own previous output. Clear the bit and retry rather than giving up.
        def _force(func, path, _exc):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(outdir, onerror=_force)
    os.makedirs(outdir)
    tar = os.path.join(outdir, "_export.tar")
    with open(tar, "wb") as f:
        subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, stdout=f, check=True)
    with tarfile.open(tar) as t:
        t.extractall(outdir)
    os.remove(tar)


def transform_readme(path):
    """Remove what is true of the private repo and false of the public one."""
    s = io.open(path, encoding="utf-8").read()
    before = s

    # 1. the "Private preview" blockquote — it describes THIS tree, not the published one. Done line-wise
    #    on purpose: the regex version silently matched nothing and left the notice in a tree that passed
    #    every other check, which is exactly the failure this tool exists to prevent.
    out, dropping = [], False
    for ln in s.split("\n"):
        if ln.lstrip().startswith("> **Private preview.**"):
            dropping = True
            continue
        if dropping:
            if ln.lstrip().startswith(">"):
                continue                                # still inside the blockquote
            dropping = False
            if not ln.strip():
                continue                                # and its trailing blank line
        out.append(ln)
    s = "\n".join(out)

    # 2. documentation-table rows pointing at files that do not ship
    rows = [ln for ln in s.split("\n")]
    keep = []
    for ln in rows:
        if ln.startswith("|") and re.search(r"\]\((STATUS\.md|DECISIONS-public\.md|docs/PUBLIC_RELEASE\.md|"
                                            r"poc/RESULTS\.md|poc/coordinator-notes\.md)\)", ln):
            continue
        keep.append(ln)
    s = "\n".join(keep)

    # 3. remaining inline links to non-shipping files -> keep the words, drop the link
    s = re.sub(r"\[([^\]]+)\]\((?:STATUS\.md|poc/RESULTS\.md|poc/coordinator-notes\.md|"
               r"docs/PUBLIC_RELEASE\.md|Support/)\)", r"\1", s)
    # the live fleet file is git-ignored; the example is what ships
    s = s.replace("[`poc/fleet.json`](poc/fleet.json)", "[`poc/fleet.example.json`](poc/fleet.example.json)")

    if s != before:
        io.open(path, "w", encoding="utf-8", newline="").write(s)
    return s != before


def strip_links(path, targets):
    """Keep the prose, drop links whose target does not ship."""
    s = io.open(path, encoding="utf-8").read()
    pat = r"\[([^\]]+)\]\((?:\.\./)?(?:%s)\)" % "|".join(re.escape(t) for t in targets)
    new = re.sub(pat, r"\1", s)
    if new != s:
        io.open(path, "w", encoding="utf-8", newline="").write(new)
    return new != s


def check_links(outdir):
    bad = []
    for root, dirs, files in os.walk(outdir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            f = os.path.join(root, fn)
            for m in re.finditer(r"\[([^\]]*)\]\(([^)#]+?)(?:#[^)]*)?\)",
                                 io.open(f, encoding="utf-8", errors="replace").read()):
                t = m.group(2).strip()
                if t.startswith(("http://", "https://", "mailto:")):
                    continue
                if not os.path.exists(os.path.normpath(os.path.join(root, t))):
                    bad.append((os.path.relpath(f, outdir), t))
    return bad


def check_words(outdir):
    home = os.environ.get("GENGHIS_HOME", "")
    wl = os.path.join(home, "LEAK_WORDS.txt") if home else ""
    if not wl or not os.path.exists(wl):
        return None                                     # cannot check: caller decides how loud to be
    words = [w.strip() for w in io.open(wl, encoding="utf-8")
             if w.strip() and not w.lstrip().startswith("#")]
    pat = re.compile("|".join(re.escape(w) for w in words), re.I)
    skip = {".png", ".jpg", ".jpeg", ".svg", ".ico", ".gguf", ".woff", ".woff2", ".ttf"}
    hits = {}
    for root, dirs, files in os.walk(outdir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in skip:
                continue
            f = os.path.join(root, fn)
            try:
                t = io.open(f, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in set(x.group(0).lower() for x in pat.finditer(t)):
                hits.setdefault(m, []).append(os.path.relpath(f, outdir))
    return hits


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_commit = "--commit" in sys.argv
    if not args:
        sys.exit(__doc__)
    outdir = os.path.abspath(args[0])

    print("== exporting tracked tree ==")
    export(outdir)

    for rel in DROP:
        p = os.path.join(outdir, rel)
        if os.path.exists(p):
            os.remove(p)
            print("  dropped  %s" % rel)
    for src, dst in RENAME:
        s, d = os.path.join(outdir, src), os.path.join(outdir, dst)
        if os.path.exists(s):
            os.replace(s, d)
            print("  renamed  %s -> %s" % (src, dst))

    print("== transforms ==")
    if transform_readme(os.path.join(outdir, "README.md")):
        print("  README.md: private-preview notice removed, journal rows dropped, dead links unlinked")
    nonship = ["STATUS.md", "poc/RESULTS.md", "poc/coordinator-notes.md", "docs/PUBLIC_RELEASE.md", "Support/"]
    for rel in ("CHANGELOG.md", "DECISIONS.md", os.path.join("docs", "COMMANDS.md")):
        p = os.path.join(outdir, rel)
        if os.path.exists(p) and strip_links(p, nonship):
            print("  %s: links to non-shipping files unlinked" % rel)

    print("== checks ==")
    ok = True
    # A rename destination is not a leftover: the public DECISIONS.md is the GENERATED log wearing the
    # journal's name, which is the whole point of the rename.
    renamed_to = {d for _, d in RENAME}
    left = [r for r in DROP if r not in renamed_to and os.path.exists(os.path.join(outdir, r))]
    print("  journal files left : %s" % (", ".join(left) if left else "none"))
    ok &= not left

    # A transform that silently matches nothing is worse than no transform: the tree looks clean and still
    # tells the reader it is somebody's private working copy. Assert the removals actually happened.
    rd = io.open(os.path.join(outdir, "README.md"), encoding="utf-8", errors="replace").read()
    stale = [p for p in ("Private preview", "](STATUS.md)", "](poc/RESULTS.md)",
                         "](docs/PUBLIC_RELEASE.md)", "](poc/coordinator-notes.md)") if p in rd]
    print("  README leftovers   : %s" % (", ".join(stale) if stale else "none"))
    ok &= not stale

    bad = check_links(outdir)
    print("  broken links       : %d" % len(bad))
    for f, t in bad[:20]:
        print("      %-28s -> %s" % (f, t))
    ok &= not bad

    hits = check_words(outdir)
    if hits is None:
        print("  private words      : NOT CHECKED (set GENGHIS_HOME)")
    else:
        print("  private words      : %d" % len(hits))
        for w, ps in sorted(hits.items()):
            print("      %-20s %s" % (w, ", ".join(sorted(set(ps))[:3])))
        ok &= not hits

    if do_commit and ok:
        run(["git", "init", "-q", "-b", "main"], cwd=outdir)
        run(["git", "add", "-A"], cwd=outdir)
        print("  git repo initialised and staged (commit it yourself, with your own message)")

    print("\n%s" % ("PUBLIC TREE OK — %s" % outdir if ok else "NOT OK — fix the above before publishing"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
