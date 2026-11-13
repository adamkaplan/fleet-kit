#!/usr/bin/env bash
# checks.sh [--retest] BASE-REF: run EVERY test of each AREA the change touches
# (areas.mjs, classify.mjs: the project's `areas` in land.json) and print one
# summary line.
# Run from the root of the tree under test, by `land` (the submitter's tree,
# already rebased onto the latest main) and by the lander's Tier 1 gate (the
# rebased combination).
# What is kept between runs of a tree: run-steps.mjs (one owner).
# EVERY step of the touched areas runs to its end, and a red run prints ONE
# report of all the failed steps and test files and keeps them (failed.json in
# the state dir). --retest runs ONLY those (the failed files and their steps'
# prerequisites) and attests nothing: it is the loop between fixes, until
# green; then a plain run does the whole touched set ONCE on the final tree.
# Exit: 0 green | 13 a check failed (its name and output tail are printed).
# Summary line: stdout; the same line in $LAND_CHECKS_OUT:
#   areas=<the areas run> tests=<the runners' own count> files=<test files>
#   checks=<step:seconds,...> total=<s>s tree=<12 hex of the tree id>
set -uo pipefail
retest=
[ "${1:-}" != --retest ] || { retest=1; shift; }
base=${1:?usage: checks.sh [--retest] BASE-REF}
here=$(cd "$(dirname "$0")" && pwd)
root=$(git rev-parse --show-toplevel)
cd "$root" || exit 1
# shellcheck source=versions.sh
. "$here/versions.sh"
# the tree under test's pinned tool versions: every step below runs with them
spire_versions "$root" || exit 1
mb=$(git merge-base "$base" HEAD)
tree=$(git rev-parse 'HEAD^{tree}')
info=$(node "$here/classify.mjs" "$mb" HEAD) ||
  { echo "checks: cannot classify the change" >&2; exit 2; }
stateDir=$(git rev-parse --path-format=absolute --git-common-dir)/spire-land
mkdir -p "$stateDir"
done_line() {
  echo "$1"
  [ -z "${LAND_CHECKS_OUT:-}" ] || echo "$1" > "$LAND_CHECKS_OUT"
}

t_all=$(date +%s)
# the steps of the touched areas, from the project's land.json: install, then
# each area's steps and the environment recipes they name (run-steps.mjs); a
# failed step exits 13 there
export LAND_STATE_DIR=$stateDir
[ -z "$retest" ] || export LAND_RETEST=1
out=$(printf '%s' "$info" | node "$here/run-steps.mjs" "$mb") || exit $?
f() {
  node -e '
    const o = JSON.parse(process.argv[1]);
    const v = o[process.argv[2]];
    console.log(Array.isArray(v) ? v.join(",") : v);
  ' "$out" "$1"
}
if [ -n "$retest" ]; then
  line="retest green: $(f ran) (the failed steps and files only: run land,"
  line="$line which runs every test of the touched areas once on the final tree)"
  done_line "$line"
  exit 0
fi
notes=$(f notes)
unmapped=$(node -e '
  const a = JSON.parse(process.argv[1]).areas;
  console.log(a.full ? a.unmapped.slice(0, 3).join(",") : "");
' "$info")
total=$(( $(date +%s) - t_all ))
line="areas=$(f areas) tests=$(f tests) files=$(f files) checks=$(f ran)"
line="$line total=${total}s tree=${tree:0:12}"
line="$line${unmapped:+ full-suite-for-unmapped=$unmapped}"
line="$line${notes:+ skipped=$notes}"
done_line "$line"
