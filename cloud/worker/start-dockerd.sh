#!/bin/sh
# Start the nested Docker daemon (runs as root via sudo, inside a Sysbox pod only).
set -eu
if docker info >/dev/null 2>&1; then exit 0; fi
nohup dockerd >/var/log/dockerd.log 2>&1 &
i=0
while ! docker info >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -le 60 ] || { echo "dockerd did not start; see /var/log/dockerd.log" >&2; exit 1; }
  sleep 1
done
