"""Ask #2026, scenario S3 (the forwarding): a launch of the role tester through bin/cloud_worker.py hands the launcher `--hub` and `--boss` exactly once.
The stub launcher of bin/test-cloud-worker (its fixture is loaded, not edited) receives the options and logs them; its list of known options is widened
by the two this test reads. Cases: tester with and without the hub setting, another role (none), an orchestrator launch (what it always got), and a mutant."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("cw_forwarding_fixture_mod", str(ROOT / "bin" / "test-cloud-worker"))
_spec = importlib.util.spec_from_loader("cw_forwarding_fixture_mod", _loader)
tcw = importlib.util.module_from_spec(_spec)
sys.modules["cw_forwarding_fixture_mod"] = tcw
_loader.exec_module(tcw)

KNOWN = '"--run-id", "--password-fd", "--json")'
WIDER = '"--run-id", "--password-fd", "--json", "--hub", "--boss", "--agent")'
GUARD = 'options.append("--hub")'


class Forwarding(tcw.Base):
    def setUp(self):
        super().setUp()
        text = tcw.STUB % {"python": sys.executable}
        self.assertEqual(text.count(KNOWN), 1)
        self.stub.write_text(text.replace(KNOWN, WIDER))   # the stub launcher accepts the options this test reads
        self.stub.chmod(0o755)

    launches = 0

    def options(self, *extra, hub_setting=False, name="tst1"):
        brief = self.home / "brief.txt"
        brief.write_text("Judge the pull request.\n")
        env = {"CW_HUB_CHANNEL_URL": "http://hub.example.invalid:8080"} if hub_setting else {}
        with mock.patch.dict(os.environ, env):
            if not hub_setting:
                for k in ("CW_HUB_CHANNEL_URL", "CW_HUB_REGISTRY_SECRET"):
                    os.environ.pop(k, None)
            code, out, err = self.main("launch", name, "--cloud", "--repo", "acme-org/widget-service", "--branch", "main", "--reports-to", "orch",
                                       "--brief-file", str(brief), *extra)
        self.assertEqual(code, 0, err)
        creates = [c for c in self.stub_calls() if c[0] == "create"]
        self.assertEqual(len(creates), self.launches + 1)   # this launch made exactly one create
        self.launches += 1
        return creates[-1][2:]

    def shown(self, argv):
        return [a for a in self.shaped(argv) if a not in ("<fd>",)]

    def test_tester_hands_hub_and_boss_once_with_and_without_the_hub_setting(self):
        for setting in (False, True):
            argv = self.options("--agent", "coder", "--role", "tester", hub_setting=setting, name="tst%d" % int(setting))
            self.assertEqual((argv.count("--hub"), argv.count("--boss")), (1, 1), (setting, argv))
            self.assertEqual(argv[argv.index("--boss") + 1], "orch")

    def test_other_roles_hand_no_hub(self):
        for role in ("coder", None):
            for setting in (False, True):
                extra = ["--role", role] if role else []
                argv = self.options("--agent", "coder", *extra, hub_setting=setting, name="o%s%d" % (role or "n", int(setting)))
                self.assertEqual(argv.count("--hub"), 0, (role, setting, argv))

    def test_an_orchestrator_launch_hands_what_it_handed_before(self):
        argv = self.options("--agent", "orchestrator", "--role", "coder", name="orc1")
        self.assertEqual(self.shown(argv), ["--repo", "acme-org/widget-service", "--branch", "main", "--run-id", "<run-id>", "--agent", "orchestrator",
                                            "--boss", "orch", "--json", "--password-fd", "--run-secret-fd", "model-auth=<fd>", "--run-secret-fd", "git-token=<fd>"])   # the earlier options, byte for byte (the stub's log from origin/integration shows the same)

    def test_mutant_that_drops_the_hub_forwarding_is_caught(self):
        src = (ROOT / "bin" / "cloud_worker.py").read_text()
        self.assertEqual(src.count(GUARD), 1)
        mutated = src.replace(GUARD, "pass")
        self.assertTrue(mutated != src and mutated.count(GUARD) == 0 and len(src) - len(mutated) == len(GUARD) - len("pass"))   # applied, computed from the two texts
        noop = src.replace(GUARD, GUARD)
        with self.assertRaises(AssertionError, msg="the no-op mutation must fail the computation"):
            self.assertNotEqual(noop, src, "the mutation was not applied")
        target = "Forwarding.test_tester_hands_hub_and_boss_once_with_and_without_the_hub_setting"
        control = self.run_copy(src, target)   # the control first: the unmutated copy passes
        self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
        mutant = self.run_copy(src.replace(GUARD, "pass"), target)
        text = mutant.stdout + mutant.stderr
        self.assertNotEqual(mutant.returncode, 0, text)
        self.assertRegex(text, r"Tuples differ: \(0, 1\) != \(1, 1\)")   # anchored: the hub flag was handed 0 times, not once

    def run_copy(self, source, target):
        scratch = Path(tempfile.mkdtemp(prefix="forward-mutant-", dir=str(self.home)))
        for part in ("bin", "cloud"):
            shutil.copytree(str(ROOT / part), str(scratch / part), ignore=shutil.ignore_patterns("__pycache__"))
        (scratch / "bin" / "cloud_worker.py").write_text(source)
        return subprocess.run([sys.executable, str(scratch / "cloud" / "launcher" / "tests" / "test_tester_forwarding.py"), target],
                              capture_output=True, text=True, timeout=300)


if __name__ == "__main__":
    unittest.main()
