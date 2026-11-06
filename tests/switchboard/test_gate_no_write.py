"""(#2230 step 1) `gates-check` makes no write and no needless read: a fake gh records every call."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_gate_probe_reads as base
import test_gate_direct_reads as direct


class TestGateNoWrite(base.TestGateProbeReads):
    def setUp(self):
        super().setUp()
        (self.tmp / "gh").write_text(base.GH_RICH % {"delay": 0, "head": base.HEAD, "log": str(self.log)})

    def methods(self, reads):
        out = []
        for r in reads:
            a = r["argv"]
            if a[:1] == ["api"]:
                out.append(a[a.index("--method") + 1] if "--method" in a else "GET")
        return out

    def test_no_call_of_a_check_is_a_write_and_the_verdicts_are_unchanged(self):
        for kind in ("ready", "merge"):
            done, reads = self.check(kind)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual({m.upper() for m in self.methods(reads)} - {"GET"}, set(), reads)
            self.assertFalse([r for r in reads if r["argv"][:2] in (["pr", "merge"], ["pr", "ready"], ["pr", "comment"])])
            payload = json.loads(done.stdout)
            self.assertEqual((payload["verdict"], payload["mode"]), ("allow", "shadow"))

    def test_a_ready_check_does_not_read_the_floor(self):
        done, reads = self.check("ready")
        self.assertEqual([r["argv"][1] for r in reads if r["argv"][0] == "api" and "/pulls/" in r["argv"][1]], [])
        self.assertEqual([f["gate"] for f in json.loads(done.stdout)["failed"]], ["G0a", "G1"])

    def test_a_get_by_method_is_a_read_for_the_direct_route(self):
        ro = direct.load().gate_read_only
        for argv in (["gh", "api", "x", "--method", "GET"], ["gh", "api", "-X", "get", "x"], ["gh", "api", "--method=GET", "x"]):
            self.assertTrue(ro(argv), argv)
        for argv in (["gh", "api", "x", "--method", "POST"], ["gh", "api", "-X", "PATCH", "x"], ["gh", "api", "--method=DELETE", "x"],
                     ["gh", "api", "x", "--method", "GET", "-f", "a=b"], ["gh", "api", "x", "--input", "-"], ["gh", "api", "x", "-X", "POST", "--method", "GET"]):
            self.assertFalse(ro(argv), argv)


for name in [n for n in dir(base.TestGateProbeReads) if n.startswith("test_")]:
    setattr(TestGateNoWrite, name, None)   # only this file's own tests
if __name__ == "__main__":
    unittest.main()
