#!/usr/bin/env bash
# fleet-compose-trap.sh: bring a compose project DOWN WITH ITS VOLUMES when a test run ends, however it ends.
#
# Source it from a test harness, then register the project once:
#
#     . tools/fleet-compose-trap.sh
#     fleet_compose_trap -p "$PROJECT" -f compose.test.yml     # the same arguments `docker compose` takes before `down`
#     docker compose -p "$PROJECT" -f compose.test.yml up -d
#     ...run the tests...
#
# On a normal exit, an interrupt (Ctrl-C) or a termination it runs `docker compose <args> down -v --remove-orphans` ONCE. A leaked stack and its
# volumes are what filled a disk; the labels in docs/switchboard.md ("Docker: label convention") are the second line of defence: `fleet-switchboard
# disk reap-docker` removes what carries an expired `fleet.expires` label.
#
# It replaces any EXIT, INT or TERM trap the harness set before: register it first, or call your own cleanup after it.
# Never uses sudo. `DOCKER` names the docker executable (default: docker).

FLEET_COMPOSE_TRAP_ARGS=()
FLEET_COMPOSE_TRAP_DONE=0

_fleet_compose_down() {
  [ "$FLEET_COMPOSE_TRAP_DONE" = 1 ] && return 0
  FLEET_COMPOSE_TRAP_DONE=1
  "${DOCKER:-docker}" compose "${FLEET_COMPOSE_TRAP_ARGS[@]}" down -v --remove-orphans || true
}

_fleet_compose_signal() {
  _fleet_compose_down
  exit "$1"
}

fleet_compose_trap() {
  FLEET_COMPOSE_TRAP_ARGS=("$@")
  FLEET_COMPOSE_TRAP_DONE=0
  trap '_fleet_compose_down' EXIT
  trap '_fleet_compose_signal 130' INT
  trap '_fleet_compose_signal 143' TERM
}
