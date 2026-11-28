"""switchboard_health: stale or missing relay / floor-live files are reported."""
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("switchboard_health", REPO / "tools" / "switchboard_health.py")
sh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sh)


class TestSwitchboardHealth(unittest.TestCase):
    def test_fresh(self):
        self.assertEqual(sh.check_freshness(1000, {"relay": 990, "floor-live": 950}), [])

    def test_stale(self):
        got = sh.check_freshness(1000, {"relay": 700, "floor-live": 990})
        self.assertEqual([n for n, _ in got], ["relay"])

    def test_missing(self):
        got = sh.check_freshness(1000, {"relay": 999})
        self.assertEqual([n for n, _ in got], ["floor-live"])

    def test_main_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sh.main([d]), 1)
            for n in ("relay", "floor-live.json"):
                Path(d, n).write_text("x")
            self.assertEqual(sh.main([d]), 0)
            old = time.time() - 1000
            os.utime(Path(d, "relay"), (old, old))
            self.assertEqual(sh.main([d]), 1)

    def test_unreadable_is_not_missing(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            real = os.stat
            def deny(path, *a, **k):
                if str(path).endswith("relay"):
                    raise PermissionError(13, "Permission denied")
                return real(path, *a, **k)
            out = io.StringIO()
            orig = os.stat
            os.stat = deny
            try:
                with redirect_stdout(out):
                    sh.main([d])
            finally:
                os.stat = orig
            self.assertIn("relay: unreadable", out.getvalue())
            self.assertNotIn("relay: missing", out.getvalue())


if __name__ == "__main__":
    unittest.main()
