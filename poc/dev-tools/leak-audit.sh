#!/usr/bin/env bash
# GENGHIS leak audit — greps the SHIPPING files for anything that belongs to the machine it was built on (D22).
# Run from the repo root before a public push:  bash poc/dev-tools/leak-audit.sh
# Exit 1 if any shipping file matches. The private journal (STATUS/DECISIONS/RESULTS/notes) is exempt on purpose.
#
# THIS SCRIPT CARRIES NO NAMES. It used to hard-code the reference lab's hostnames, accounts and domain — which made
# the audit itself the tidiest inventory of them in the repo, and it ships (2026-09-22). So it now checks two things:
#
#   1. SHAPES that are never right in a shipping file, whoever you are — private/CGNAT addresses, MAC addresses,
#      e-mail addresses, absolute Windows/Unix home paths, an `ssh user@host` with a literal host.
#   2. WORDS from your own private list: $GENGHIS_HOME/LEAK_WORDS.txt (D36 — the private home repo owns the list of
#      things that must never ship: hostnames, accounts, your domain, a TV id, anything else). One per line, '#'
#      comments. Without it the audit still works; it just cannot know YOUR names.
#
# Exit codes: 0 clean · 1 hits · 2 not a repo.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2

# --- 1. shapes (no names) -------------------------------------------------------------------------------------
# RFC1918 + CGNAT (Tailscale) + link-local, MACs, e-mails, Windows user dirs, /home/<user> and ~<user> paths.
SHAPES='(^|[^0-9.])(10|127)\.[0-9]+\.[0-9]+\.[0-9]+'
SHAPES="$SHAPES"'|192\.168\.[0-9]+\.[0-9]+'
SHAPES="$SHAPES"'|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+'
SHAPES="$SHAPES"'|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+'   # 100.64/10 CGNAT: a real tailnet address
SHAPES="$SHAPES"'|169\.254\.[0-9]+\.[0-9]+'
SHAPES="$SHAPES"'|\b([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b'                      # MAC
SHAPES="$SHAPES"'|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'                 # e-mail
SHAPES="$SHAPES"'|[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+'                            # C:\Users\someone
SHAPES="$SHAPES"'|/home/[A-Za-z0-9._-]+/'                                         # /home/someone/
SHAPES="$SHAPES"'|ssh +[A-Za-z0-9._-]+@[A-Za-z0-9._-]+'                           # ssh user@host

# Shapes that are FINE in a shipping file: documented ranges, loopback/example addresses, placeholders, and the
# project's own addresses. Anything matching these is not a leak.
ALLOW='100\.64\.0\.0/10|10\.0\.0\.0/8|192\.168\.0\.0/16|172\.16\.0\.0/12'         # ranges named as ranges
ALLOW="$ALLOW"'|127\.0\.0\.1|0\.0\.0\.0|10\.0\.2\.2'                              # loopback / bind-all / QEMU host
ALLOW="$ALLOW"'|192\.168\.1\.x|192\.168\.x\.x|<[^>]*>|CHANGE-ME|example\.(com|org|net)'
ALLOW="$ALLOW"'|users\.noreply\.github\.com'
ALLOW="$ALLOW"'|ssh +<|ssh +user@|ssh +\$|ssh +-|C:\\+Users\\+<|/home/<'

# --- 2. your own words (private, optional) --------------------------------------------------------------------
PATTERN="$SHAPES"
HOME_WORDS="${GENGHIS_HOME:-$HOME/genghis-home}/LEAK_WORDS.txt"
if [ -f "$HOME_WORDS" ]; then
  # escape regex metacharacters in each word, then OR them together
  extra=$(grep -v '^[[:space:]]*#' "$HOME_WORDS" | grep -v '^[[:space:]]*$' | sed 's/[][\.*^$(){}?+|/\\]/\\&/g' | paste -sd'|' -)
  [ -n "$extra" ] && PATTERN="$PATTERN|$extra"
  words_note="+ $(grep -cv '^[[:space:]]*#\|^[[:space:]]*$' "$HOME_WORDS") private word(s)"
else
  words_note="(no LEAK_WORDS.txt — set GENGHIS_HOME to check your own names too)"
fi

# The journal is exempt on purpose: it is the project's private record and does not ship (docs/PUBLIC_RELEASE.md §2).
EXEMPT='^(STATUS\.md|DECISIONS\.md|Project\.md|poc/RESULTS\.md|poc/coordinator-notes\.md|docs/PUBLIC_RELEASE\.md|backups/|logs/|security_reports/|quarantine/|analysis/|Support/)'

# This file SHIPS and is made of address/MAC/path regexes, so the shape rules fire on nearly every line of it.
# It is exempt from SHAPES only -- your private WORDS are still checked here, because a tool that exempts
# itself entirely can never catch its own leak, and this one had a private word sitting in a COMMENT
# (2026-09-22, found by hand). The same class of slip had already hit the public-decisions generator, whose
# own comment once quoted a subnet from the word list. That generator needs no exemption: it passes the full
# check. Add a file here only when the shape rules genuinely cannot tell its regexes from a leak.
SHAPES_ONLY_EXEMPT='^(poc/dev-tools/leak-audit\.sh)$'

hits=0
while IFS= read -r f; do
  [[ "$f" =~ $EXEMPT ]] && continue
  case "$f" in *.png|*.jpg|*.jpeg|*.svg|*.gguf|*.ico|*.woff*|*.ttf) continue;; esac
  if [[ "$f" =~ $SHAPES_ONLY_EXEMPT ]]; then
    [ -z "${extra:-}" ] && continue                 # no private word list -> nothing to check here
    out=$(grep -n -i -E "$extra" "$f" 2>/dev/null)
    if [ -n "$out" ]; then
      echo "== $f  (a PRIVATE WORD in a file that ships)"; echo "$out" | cut -c1-160
      hits=$((hits+1))
    fi
    continue
  fi
  # -i on BOTH: a private word list is lowercase, but prose capitalises names. A case-sensitive audit waved
  # every capitalised form of every listed word straight through (found 2026-09-22 by testing the guard
  # instead of trusting it). The allow-list matches case-insensitively too, so exemptions stay symmetrical.
  out=$(grep -n -i -E "$PATTERN" "$f" 2>/dev/null | grep -v -i -E "$ALLOW")
  if [ -n "$out" ]; then
    echo "== $f"; echo "$out" | cut -c1-160
    hits=$((hits+1))
  fi
done < <(git ls-files)

echo
if [ "$hits" -eq 0 ]; then echo "leak audit: 0 shipping files match $words_note — clean."; exit 0
else echo "leak audit: $hits shipping file(s) match $words_note — scrub before a public push (docs/PUBLIC_RELEASE.md)."; exit 1; fi
