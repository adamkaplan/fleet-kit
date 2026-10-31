"""The docker baseline test of bin/test-cloud-worker is hermetic over the memory probe (ask #1717).

It runs the one pinned test in copies of the tree outside the repository: LINUX-LIKE (no probe) and MACOS-LIKE (fake probes), then a MUTANT
(the pinned line put back) in both: the mutant fails only where the probe reads, with the stored line, and passes as the control where it does not."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TEST = "TestNeedsDocker.test_s4_u1_unmarked_cloud_and_local_launches_are_the_stored_baseline"
NEW = '}), mock.patch.object(sb, "memory_read", return_value=None):   # a local launch'
OLD = '}):   # a local launch'
STORED = "memory test not available"
SW = "bin/" + "fleet-" + "switchboard"
PROBES_LINE = re.compile(r'^MEMORY_PROBES = \{.*\}.*$', re.M)
SLEEP_LINE = "memory_sleep = time.sleep"


def _write_exec(path, text):
    path.write_text("#!/bin/sh\n" + text + "\n")
    path.chmod(0o755)


def make_copy(base, name, macos):
    dest = Path(base) / name
    dest.mkdir()
    for part in ("bin", "cloud", "tests/golden", "tests/switchboard", "shims", "agents", "templates", "docs"):
        src = ROOT / part
        if src.exists():
            shutil.copytree(src, dest / part, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"), symlinks=True)
    for top in ROOT.glob("*.md"):
        shutil.copy2(top, dest / top.name)
    os.chmod(dest, 0o700)
    if macos:
        probes = Path(base) / (name + "-probes")
        probes.mkdir()
        _write_exec(probes / "memory_pressure", "echo 'System-wide memory free percentage: 80%'")
        _write_exec(probes / "vm_stat", "echo 'Swapouts: 5.'")
        target = dest / SW
        text = target.read_text()
        new_probes = 'MEMORY_PROBES = {"memory_pressure": "%s", "vm_stat": "%s"}' % (probes / "memory_pressure", probes / "vm_stat")
        text, n1 = PROBES_LINE.subn(lambda m: new_probes, text, count=1)
        assert n1 == 1 and text.count(SLEEP_LINE) == 1
        text = text.replace(SLEEP_LINE, "memory_sleep = lambda seconds: None")
        target.write_text(text)
    return dest


def mutate(dest):
    target = dest / "bin" / "test-cloud-worker"
    text = target.read_text()
    count = text.count(NEW)
    target.write_text(text.replace(NEW, OLD) if count == 1 else text)
    return count == 1


def run(dest):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, str(dest / "bin" / "test-cloud-worker"), TEST], cwd=str(dest), env=env,
                          capture_output=True, text=True, timeout=80)


class NeedsDockerHermetic(unittest.TestCase):
    def test_s2_s3_baseline_hermetic_with_mutant_and_control(self):
        began = time.time()
        base = tempfile.mkdtemp(prefix="nd-hermetic-")
        try:
            linux = make_copy(base, "linux", False)
            mac = make_copy(base, "mac", True)
            mut_linux = make_copy(base, "mut-linux", False)
            mut_mac = make_copy(base, "mut-mac", True)
            applied = mutate(mut_linux) and mutate(mut_mac)
            probe = subprocess.run([sys.executable, "-c", (
                "import importlib.machinery as m, importlib.util as u;"
                "l=m.SourceFileLoader('sb', %r);s=u.spec_from_loader('sb', l);mod=u.module_from_spec(s);l.exec_module(mod);"
                "import subprocess;print(mod.memory_read(subprocess.run))") % str(mac / SW)],
                capture_output=True, text=True, timeout=60)
            self.assertRegex(probe.stdout.strip(), r"^\(80, 5\)$", probe.stdout + probe.stderr)
            results = [run(linux), run(mac)]
            s2 = sum(1 for r in results if r.returncode == 0)
            print("S2: docker baseline test passes in %d of 2 conditions" % s2)
            control = run(mut_linux)
            mutant = run(mut_mac)
            c = int(control.returncode == 0)
            m = int(mutant.returncode != 0 and STORED in (mutant.stdout + mutant.stderr))
            print("S3: control passes %d of 1, mutant fails %d of 1 with the stored line, mutation applied %s" % (c, m, applied))
            print("seconds: %.1f" % (time.time() - began))
            for r in results:
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(applied)
            self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
            self.assertEqual(m, 1, mutant.stdout + mutant.stderr)
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
