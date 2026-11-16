#!/usr/bin/env bash
# owned-tmp.sh: one way for a shell tool to make a temp directory it destroys.
# Source it.
#   owned_tmp_make PREFIX   makes $TMPDIR/PREFIX<random>, writes its owner (the
#     caller's pid) to .owner, prints the path. The caller removes it in a trap
#     on EXIT, INT, TERM.
#   owned_tmp_reap PREFIX [TTL_MIN]   removes the directories $TMPDIR/PREFIX*
#     of a killed run: older than TTL_MIN (default 120) and not owned by a live
#     pid. No sweeper: each tool calls it at its own start, for its own prefix
#     only. A directory with an executable `.reap` runs it first (it stops what
#     runs there); a failing reap keeps the directory.
owned_tmp_make() {
  local d
  d=$(mktemp -d "${TMPDIR:-/tmp}/$1XXXXXX") || return 1
  printf '%s\n' "$$" >"$d/.owner"
  printf '%s\n' "$d"
}
owned_tmp_reap() {
  local d pid
  for d in "${TMPDIR:-/tmp}/$1"*; do
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    [ -n "$(find "$d" -maxdepth 0 -mmin +"${2:-120}" 2>/dev/null)" ] || continue
    pid=$(sed -n 1p "$d/.owner" 2>/dev/null)
    if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then continue; fi
    # what the killed run left running in its directory: the run that made it
    # may leave an executable `.reap` there (it stops what it started); a reap
    # that fails leaves the directory, never deletes it under a live process
    if [ -x "$d/.reap" ]; then "$d/.reap" >/dev/null 2>&1 || continue; fi
    rm -rf "$d"
  done
  return 0
}
