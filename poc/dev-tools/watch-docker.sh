#!/bin/bash
# GENGHIS stray-container trap — auto-reconnects if the event stream drops.
LOG="$(dirname "$0")/logs/docker-strays.log"; mkdir -p "$(dirname "$LOG")"
KNOWN="^(open-webui|genghis-grafana|genghis-prometheus)$"
echo "[$(date '+%F %T')] trap armed (auto-reconnect)" >> "$LOG"
while true; do
  docker events --filter type=container --filter event=create \
    --format '{{.Actor.Attributes.name}}|{{.Actor.Attributes.image}}|{{.Actor.ID}}' 2>>"$LOG" |
  while IFS='|' read -r name img id; do
    echo "$name" | grep -qE "$KNOWN" && continue
    { echo "=== [$(date '+%F %T')] STRAY: name=$name image=$img ==="
      docker inspect "$id" --format 'cmd={{json .Config.Cmd}} entrypoint={{json .Config.Entrypoint}} labels={{json .Config.Labels}} restart={{.HostConfig.RestartPolicy.Name}} ppid-hint={{.Config.Hostname}}' 2>/dev/null
    } >> "$LOG"
  done
  echo "[$(date '+%F %T')] stream dropped — reconnecting in 3s" >> "$LOG"; sleep 3
done
