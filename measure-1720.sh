#!/bin/bash
# usage: measure-1720.sh <checkout-dir> <label> <loadfactor 0|2|4> [runs]
# Runs the class TestClearNotice N times, counts failing runs, prints failure lines and seconds per run.
d=$1; l=$2; f=$3; n=${4:-10}
cd "$d" || exit 2
cpus=$(nproc); pids=()
for i in $(seq 1 $((cpus*f))); do python3 -c 'while True: pass' & pids+=($!); done
fail=0
for r in $(seq 1 $n); do
  s=$(date +%s.%N)
  bin/test-switchboard -k TestClearNotice > /tmp/m1720-$l-$f-$r.log 2>&1; rc=$?
  e=$(date +%s.%N)
  printf '%s load=%sx run %s rc=%s secs=%.1f\n' "$l" "$f" "$r" "$rc" "$(python3 -c "print($e-$s)")"
  if [ $rc -ne 0 ]; then fail=$((fail+1)); grep -E 'FAIL|Error|BEFORE|did not wait' /tmp/m1720-$l-$f-$r.log | head -4; fi
done
[ ${#pids[@]} -gt 0 ] && kill "${pids[@]}" 2>/dev/null
echo "$l load=${f}x cpus=$cpus failing runs: $fail of $n"
