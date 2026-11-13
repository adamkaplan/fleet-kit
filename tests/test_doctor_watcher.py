#!/usr/bin/env python3
"""The doctor's run_to cap must never run the doctor's cleanup (ask #2023).

S1/S2: a scratch shell sources `run_to` and `cleanup` from the text of bin/fleet-doctor and starts and ends the
command at once many times. S3: the doctor itself, with stub herdr and gh, many runs. No thread is started here (every test thread would be a daemon thread).
"""
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
DOCTOR = BIN / "fleet-doctor"
RUNS = 300         # harness iterations (a count, not a time)
DOCTOR_RUNS = 20
CAP = 30           # the cap handed to run_to for a command that returns at once (a value, not asserted on)

NEW = '''  # The watcher polls at one-second steps and ends by itself; the doctor never signals it, so
  # it cannot run the inherited cleanup trap.
  ( w=0; while [ "$w" -lt "$secs" ]; do sleep 1; kill -0 "$cmdpid" 2>/dev/null || exit 0; w=$((w+1)); done
    kill -9 "$cmdpid" 2>/dev/null ) >/dev/null 2>&1 &
  wait "$cmdpid" 2>/dev/null
  rc=$?
'''

EXTRACT = r'''awk '/^cleanup\(\) /{print} /^run_to\(\) \{/{p=1} p{print} p&&/^}/{p=0}' "$1" > "$HDIR/fn.sh"'''

HARNESS = EXTRACT + r'''
n="$2"; stop="$3"
. "$HDIR/fn.sh"
bad=0; i=0
while [ "$i" -lt "$n" ]; do
  TMPDIR_FD=$(mktemp -d "$HDIR/d.XXXXXX"); OUT="$TMPDIR_FD/out"
  trap cleanup EXIT INT TERM
  run_to "$CAP" true
  [ -d "$TMPDIR_FD" ] || bad=$((bad+1))
  rm -rf "$TMPDIR_FD"
  [ "$stop" = 1 ] && [ "$bad" -gt 0 ] && break
  i=$((i+1))
done
trap - EXIT INT TERM
echo "$bad"
'''

STATUS = EXTRACT + r'''
. "$HDIR/fn.sh"; TMPDIR_FD=$(mktemp -d "$HDIR/d.XXXXXX"); OUT="$TMPDIR_FD/out"
run_to 30 sh -c "echo hi; exit 3"; echo "$? $LAST_OUT"
run_to 2 sleep 30; echo "$?"
'''


def mutate(text, old, new):
    """Replace old by new; the mutation must have been applied, computed from the two texts."""
    assert text.count(old) == 1, "the text to mutate does not occur once"
    out = text.replace(old, new)
    assert out != text, "the mutation was not applied"
    return out


def run_scratch(shell, script, doctor_text, *args):
    hd = tempfile.mkdtemp(prefix="doctor-watcher.")
    try:
        d = Path(hd) / "doctor"
        d.write_text(doctor_text)
        env = dict(os.environ, HDIR=hd, CAP=str(CAP))
        p = subprocess.run(shell + ["-c", script, "h", str(d)] + [str(a) for a in args], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=600)
        return p.stdout.split("\n")
    finally:
        shutil.rmtree(hd, True)


def early_removals(doctor_text, n, stop=0):
    """Count early removals over n starts (a bounded count; used for the fixed watcher, where 0 is expected)."""
    assert n > 0
    return int(run_scratch(["bash"], HARNESS, doctor_text, n, stop)[-2])


def load_liveness():
    path = str(BIN / "test-host-liveness")
    loader = importlib.machinery.SourceFileLoader("thl_for_watcher", path)
    spec = importlib.util.spec_from_loader("thl_for_watcher", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.text = DOCTOR.read_text()

    def test_s2_no_early_removal(self):
        self.assertEqual(early_removals(self.text, RUNS), 0)

    def test_s2_status_output_and_cap(self):
        shells = [["bash"], ["dash"], ["busybox", "sh"]]
        for sh in [s for s in shells if shutil.which(s[0])]:
            lines = run_scratch(sh, STATUS, self.text)
            self.assertEqual(lines[0], "3 hi", sh)
            self.assertNotEqual(lines[1].strip(), "0", sh)

    def test_s3_doctor_runs(self):
        mod = load_liveness()
        for i in range(DOCTOR_RUNS):
            lines = mod.run_doctor(["host:h1/a1"], None, panes=())
            self.assertTrue(lines, "run %d printed no charters line" % i)
            self.assertFalse([l for l in lines if "skipped" in l or "no pane list" in l], "run %d: %r" % (i, lines))

    def test_noop_mutation_fails_the_computation(self):
        with self.assertRaises(AssertionError) as cm:
            mutate(self.text, NEW, NEW)
        self.assertIn("the mutation was not applied", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
