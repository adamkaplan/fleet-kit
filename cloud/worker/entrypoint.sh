#!/bin/sh
# Bring up the nested Docker daemon when the runtime allows it, then run the command.
# Outside Sysbox (no nested support) the worker still starts, without Docker.
set -eu
if [ "${WORKER_DOCKER:-1}" = "1" ]; then
  sudo /usr/local/bin/start-dockerd || echo "worker: nested Docker unavailable" >&2
fi
exec "$@"
