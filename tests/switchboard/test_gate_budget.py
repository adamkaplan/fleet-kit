"""(#2215 S4) `gates-check`'s inner budget follows the gh shim's limit; a configured budget_ms wins."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import gate_budget  # noqa: E402
import plan_ledger  # noqa: E402

CONFIG = {"plan_gates": {"repos": {"acme/app": {"mode": "enforce"}, "acme/set": {"budget_ms": 2000}}}}


def budget(repo="acme/app", **env):
    with mock.patch.dict(os.environ, env):
        cfg = {"plan_gates": plan_ledger.validate_plan_gates(CONFIG["plan_gates"], "t")}
        return plan_ledger.repo_setting(cfg, repo)["budget_ms"]


class TestGateBudget(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ)   # restored at cleanup: the pop below must not leak into other tests
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("FLEET_SHIM_GATE_SECONDS", None)

    def test_default_sits_between_the_old_budget_and_the_shim_limit(self):
        value = budget()
        self.assertGreater(value, 3000)
        self.assertLess(value, gate_budget.shim_gate_seconds() * 1000)

    def test_shim_override_moves_it(self):
        self.assertLess(budget(FLEET_SHIM_GATE_SECONDS="5"), 5000)
        self.assertGreater(budget(FLEET_SHIM_GATE_SECONDS="10"), budget(FLEET_SHIM_GATE_SECONDS="5"))

    def test_configured_budget_wins(self):
        self.assertEqual(budget("acme/set"), 2000)
        self.assertEqual(budget("acme/set", FLEET_SHIM_GATE_SECONDS="10"), 2000)

    def test_control_unreadable_shim_keeps_the_old_default(self):
        with mock.patch.object(gate_budget, "SHIM_GH", "/nonexistent/gh"):
            self.assertEqual(budget(), gate_budget.OLD_DEFAULT_MS)
            self.assertEqual(gate_budget.OLD_DEFAULT_MS, 3000)


if __name__ == "__main__":
    unittest.main()
