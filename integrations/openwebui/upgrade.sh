#!/usr/bin/env bash
# GENGHIS — move the pinned chat UI to a new version, safely (D47).
#
# The chat UI's image is PINNED in docker-compose.yml (D18) so an install is reproducible, and its own
# "Update" banner is turned off because under a pin it cannot do anything. A pin is a promise to move it
# DELIBERATELY, not never — this script is how you move it:
#
#     back up the database → bump the pin → recreate → re-provision → verify → (roll back if needed)
#
# Usage:
#     integrations/openwebui/upgrade.sh v0.11.4          # upgrade to that tag
#     integrations/openwebui/upgrade.sh --check          # what is running, what is pinned, what is upstream
#     integrations/openwebui/upgrade.sh --rollback       # restore the last backup + the previous pin
#
# It never runs unattended: it prints what it is about to do and asks once. Nothing is deleted, ever — the
# database backups accumulate in backups/openwebui/ so a bad upgrade is one command away from undone.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
ROOT="$PWD"
COMPOSE="$ROOT/docker-compose.yml"
BACKUPS="$ROOT/backups/openwebui"                 # git-ignored (D22)
SERVICE="open-webui"
PROV="$ROOT/integrations/openwebui/provision.py"
PROMPT="$ROOT/integrations/openwebui/system_prompt.txt"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  [ ok ] %s\n' "$*"; }
bad()  { printf '  [ !! ] %s\n' "$*"; }
die()  { bad "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

have docker || die "docker not found — this script manages the Docker chat-UI tier"
[ -f "$COMPOSE" ] || die "no docker-compose.yml at $COMPOSE"
DC="docker compose"; docker compose version >/dev/null 2>&1 || DC="docker-compose"

pinned()  { grep -oE 'ghcr\.io/open-webui/open-webui:[^ ]+' "$COMPOSE" | head -1 | awk -F: '{print $NF}'; }
running() { docker inspect -f '{{.Config.Image}}' "$SERVICE" 2>/dev/null | awk -F: '{print $NF}'; }
upstream() {
  have curl || { echo "?"; return; }
  curl -fsS -m 10 https://api.github.com/repos/open-webui/open-webui/releases/latest 2>/dev/null \
    | grep -oE '"tag_name": *"[^"]+"' | head -1 | cut -d'"' -f4
}

# ---------------------------------------------------------------------------- --check
if [ "${1:-}" = "--check" ] || [ -z "${1:-}" ]; then
  echo "== GENGHIS chat UI =="
  say "pinned in compose : $(pinned)"
  say "running now       : $(running || echo 'not running')"
  u=$(upstream); say "latest upstream   : ${u:-unknown}"
  if [ -n "${u:-}" ] && [ "$u" != "?" ] && [ "$u" != "$(pinned)" ]; then
    echo
    say "A newer version exists. Read its notes FIRST — a security advisory is a reason to move today;"
    say "a feature release can wait for a quiet moment:"
    say "    https://github.com/open-webui/open-webui/releases/tag/$u"
    say "Then:  integrations/openwebui/upgrade.sh $u"
  else
    ok "up to date with the pin"
  fi
  ls -1t "$BACKUPS"/webui-*.db 2>/dev/null | head -3 | sed 's/^/  backup: /'
  exit 0
fi

# ---------------------------------------------------------------------------- --rollback
if [ "$1" = "--rollback" ]; then
  last=$(ls -1t "$BACKUPS"/webui-*.db 2>/dev/null | head -1)
  [ -n "$last" ] || die "no backup in $BACKUPS — nothing to roll back to"
  prev=$(cat "$BACKUPS/.previous-pin" 2>/dev/null || true)
  echo "== Roll back the chat UI =="
  say "restore database : $last"
  say "restore pin      : ${prev:-<unknown — edit docker-compose.yml by hand>}"
  read -r -p "  proceed? [y/N] " a; [ "$a" = y ] || [ "$a" = yes ] || exit 0
  [ -n "$prev" ] && sed -i "s|ghcr.io/open-webui/open-webui:[^ ]*|ghcr.io/open-webui/open-webui:$prev|" "$COMPOSE"
  $DC -f "$COMPOSE" stop "$SERVICE" >/dev/null 2>&1
  docker cp "$last" "$SERVICE":/app/backend/data/webui.db || die "could not restore the database"
  $DC -f "$COMPOSE" up -d "$SERVICE" || die "could not start $SERVICE"
  ok "rolled back to ${prev:-the previous pin} with the database from $(basename "$last")"
  exit 0
fi

# ---------------------------------------------------------------------------- upgrade
NEW="$1"
CUR="$(pinned)"
[ "$NEW" = "$CUR" ] && { ok "already pinned at $NEW — nothing to do"; exit 0; }

echo "== Upgrade the GENGHIS chat UI: $CUR → $NEW =="
say "1. back up webui.db (chat history + the models GENGHIS provisioned) to backups/openwebui/"
say "2. bump the pin in docker-compose.yml"
say "3. pull + recreate the container (the chat UI is down for ~a minute)"
say "4. re-run provisioning (idempotent; it never reverts a setting you chose)"
say "5. verify: the goals list, a chat round-trips, the tool belt, builtin tools still off"
say "   roll back any time with:  $0 --rollback"
echo
read -r -p "  proceed? [y/N] " a; [ "$a" = y ] || [ "$a" = yes ] || exit 0

# 1. backup -------------------------------------------------------------------
mkdir -p "$BACKUPS"
stamp=$(date +%Y%m%d-%H%M%S)
dst="$BACKUPS/webui-$stamp-$CUR.db"
if docker ps --format '{{.Names}}' | grep -qx "$SERVICE"; then
  # SQLite's own backup API: a plain copy can miss the write-ahead log of a live database.
  docker exec "$SERVICE" python3 -c "
import sqlite3
s = sqlite3.connect('/app/backend/data/webui.db')
d = sqlite3.connect('/tmp/webui-backup.db')
s.backup(d); d.close(); s.close()
" 2>/dev/null && docker cp "$SERVICE":/tmp/webui-backup.db "$dst" >/dev/null 2>&1 \
    || docker cp "$SERVICE":/app/backend/data/webui.db "$dst" >/dev/null 2>&1
  docker exec "$SERVICE" rm -f /tmp/webui-backup.db 2>/dev/null
fi
[ -s "$dst" ] || die "the database backup is empty or missing — stopping before anything changes"
ok "backed up $(du -h "$dst" | cut -f1) → ${dst#$ROOT/}"
echo "$CUR" > "$BACKUPS/.previous-pin"

# 2. bump ---------------------------------------------------------------------
cp "$COMPOSE" "$BACKUPS/docker-compose-$stamp.yml"
sed -i "s|ghcr.io/open-webui/open-webui:[^ ]*|ghcr.io/open-webui/open-webui:$NEW|" "$COMPOSE"
[ "$(pinned)" = "$NEW" ] || die "could not rewrite the pin in docker-compose.yml"
ok "pinned $NEW"

# 3. recreate -----------------------------------------------------------------
say "pulling $NEW …"
$DC -f "$COMPOSE" pull "$SERVICE" || { bad "pull failed — restoring the pin"; sed -i "s|ghcr.io/open-webui/open-webui:[^ ]*|ghcr.io/open-webui/open-webui:$CUR|" "$COMPOSE"; exit 1; }
$DC -f "$COMPOSE" up -d "$SERVICE" || die "could not start $SERVICE (roll back: $0 --rollback)"
for i in $(seq 1 60); do
  s=$(docker inspect -f '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || echo starting)
  [ "$s" = healthy ] && break
  sleep 2
done
[ "${s:-}" = healthy ] && ok "container healthy on $NEW" || bad "container is '$s' after 2 min — check: docker logs $SERVICE"

# 4. re-provision --------------------------------------------------------------
if [ -f "$PROV" ]; then
  docker cp "$PROV" "$SERVICE":/tmp/provision.py >/dev/null 2>&1
  [ -f "$PROMPT" ] && docker cp "$PROMPT" "$SERVICE":/tmp/system_prompt.txt >/dev/null 2>&1
  out=$(docker exec "$SERVICE" python3 /tmp/provision.py 2>&1)
  printf '%s\n' "$out" | sed 's/^/  /'
  ok "provisioning re-run"
else
  bad "no provision.py found — skipped (models keep whatever the upgrade left them with)"
fi

# 5. verify --------------------------------------------------------------------
echo
echo "== Verify =="
port=$(grep -oE '"[0-9]+:8080"' "$COMPOSE" | head -1 | tr -d '"' | cut -d: -f1); port=${port:-3080}
code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://localhost:$port/" 2>/dev/null)
[ "$code" = 200 ] && ok "chat UI answers on :$port" || bad "chat UI returned '$code' on :$port"
docker exec "$SERVICE" python3 -c "
import sqlite3
c = sqlite3.connect('/app/backend/data/webui.db')
ids = [r[0] for r in c.execute('select id from model').fetchall()]
goals = [i for i in ids if i.startswith('genghis-')]
prov = c.execute(\"select count(*) from model where meta like '%genghis_provisioned%'\").fetchone()[0]
print(f'  [ ok ] {len(goals)} genghis-* models present, {prov} provisioned rows kept')
" 2>/dev/null || bad "could not read the database — check the container"
echo
say "Now open the chat UI and send one message. If anything is wrong:  $0 --rollback"
say "When it is good, commit the pin bump so every install moves with you:"
say "    git add docker-compose.yml && git commit -m 'chat UI: pin $NEW'"
