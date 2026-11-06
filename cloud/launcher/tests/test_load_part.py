#!/usr/bin/env python3
"""Offline tests: bin/cloud_worker.py loads its parts through ONE function, _load_part (ask #2282).

Run directly: python3 cloud/launcher/tests/test_load_part.py
"""
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PARTS = {
    "cloud_cluster": "CLUSTER_NAMES",
    "cloud_launch_again": (),   # reached as cloud_launch_again.<name>; it binds the globals (checked below)
    "cloud_inspect": ("INSPECT_SECONDS", "_inspect_short", "cmd_inspect"),
    "cloud_lane_cap": ("lane_m_workers", "_ended", "_stopped_ids"),
}
ANCHOR = "a part was loaded but not registered"


def load(bin_dir, alias):
    spec = importlib.util.spec_from_file_location(alias, str(Path(bin_dir) / "cloud_worker.py"))
    worker = importlib.util.module_from_spec(spec)
    sys.modules[alias] = worker
    spec.loader.exec_module(worker)
    return worker


def check(worker):
    """Every part is in sys.modules under its old name and every name it binds is here, identical; return the count of names."""
    count = 0
    for name, names in PARTS.items():
        part = getattr(worker, name)
        if sys.modules.get(name) is not part:
            raise AssertionError(ANCHOR + ": " + name)
        if isinstance(names, str):
            names = getattr(worker, names)
        for n in names:
            if getattr(worker, n, None) is not getattr(part, n):
                raise AssertionError("a bound name differs: %s.%s" % (name, n))
            count += 1
    if sys.modules["cloud_launch_again"].H is not vars(worker):
        raise AssertionError("bind did not receive the globals")
    return count


class LoadPart(unittest.TestCase):
    def test_s2_parts_registered_and_names_bound(self):
        worker = load(ROOT / "bin", "cw_load_part_real")
        self.assertFalse([n for n in dir(worker) if n.startswith("_load_") and n != "_load_part"])
        count = check(worker)
        print("names checked:", count)
        self.assertGreater(count, 50)

    def test_s4_mutant_skipping_sys_modules_fails(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(ROOT / "bin", tmp / "bin")
        control = load(tmp / "bin", "cw_load_part_control")
        check(control)   # control first: the copy passes
        path = tmp / "bin" / "cloud_worker.py"
        src = path.read_text()
        line = "    sys.modules[name] = module\n"
        self.assertEqual(src.count(line), 1)
        path.write_text(src.replace(line, ""))
        for n in PARTS:
            sys.modules.pop(n, None)
        mutant = load(tmp / "bin", "cw_load_part_mutant")
        with self.assertRaises(AssertionError) as ctx:
            check(mutant)
        self.assertIn(ANCHOR, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
