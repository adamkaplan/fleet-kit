"""#2305: `launch --cloud --base BRANCH`: the setup text names the base, a refused base creates nothing, the record carries it.
Run: python3 -m unittest tests.test_cloud_base"""
import argparse
import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "switchboard"))
import cloud_worker  # noqa: E402
import test_first_push_notice as fpn  # noqa: E402  (its launch stubs)

DEFAULT_SENTENCE = "create it from the default branch if it does not exist yet"
BASE = "integration/topic"


def launch(base="unset", source=None, part_source=None):
    """(prompt text, saved records, created pods, SystemExit-or-Refusal or None) of the REAL `_launch`, the cluster and the pod stubbed.
    `part_source`: a textual variant of bin/cloud_base.py, put in place of the part for this launch."""
    module = fpn.CW() if source is None else fpn.mutated_module(source)
    sent, saved, created = [], [], []

    class Pod(fpn.PodSessionStub):
        def prompt(self, session, text, *a, **k):
            sent.append(text)

    ns = dict(name="coder5", agent="worker", reports_to="lead", boss_unregistered="test", checkpoint="30m", resume=False, headless=False, dir=None, shape=None,
              stack=False, brief_file=None, idle=None, hard=None, role="coder", issue=None, charter=None, model=None, reasoning=None, brief=None, branch="b", repo="o/r")
    if base != "unset":
        ns["base"] = base
    args = argparse.Namespace(**ns)
    engine = types.SimpleNamespace(now=lambda: fpn.BASE, discover=lambda at: types.SimpleNamespace(agents={}), config={"call_timeout_seconds": 5}, audit=None, actor="t")
    lib = types.SimpleNamespace(check_repo_preflight=lambda repo, env: "ok", CloudError=Exception, AGENTS=("worker",))
    stubs = dict(use_settings=lambda: "test", check_allowed=lambda repo: None, _lib=lambda: lib, launcher_argv=lambda: ["x"], VaultRecovery=lambda *a: None,
                 Cluster=fpn.FakeCluster, model_entry=lambda: "m", git_token=lambda *a, **k: "t",
                 run_launcher=lambda *a, **k: (created.append(a) or 0, {}, ""),
                 _read_password=lambda fd: "pw", _record_from_create=lambda n, b: {"runtime_id": "r1", "url": "https://pod.example"},
                 _write_private=lambda *a: None, save_record=lambda state, rec: saved.append(dict(rec)), load_record=lambda *a: None, _audit=lambda *a, **k: None,
                 tunnel=lambda cluster, record: contextlib.nullcontext(object()), remote_v2=lambda *a: Pod(), _registry_note=lambda *a, **k: None, _model_served=lambda *a, **k: None)
    problem = None
    with tempfile.TemporaryDirectory() as state, contextlib.ExitStack() as stack:
        heads = {"launch_repo": lambda a: "o/r", "build_engine": lambda *a: (engine, state, None), "read_brief": lambda *a: "do it\n", "fleet_record": lambda *a: {}}
        for key, value in stubs.items():
            stack.enter_context(mock.patch.object(module, key, value, create=True))
        stack.enter_context(mock.patch.dict(module.H, heads))
        stack.enter_context(mock.patch.object(module, "SETUP_PREAMBLE", module.SETUP_PREAMBLE))   # restored after the launch, whatever the part set
        if part_source is not None:
            variant = types.ModuleType("cloud_base_variant")
            exec(compile(part_source, "cloud_base_variant", "exec"), variant.__dict__)
            variant.bind(vars(module))
            stack.enter_context(mock.patch.object(module.cloud_pr_labels, "cloud_base", variant))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        try:
            module._launch(args, None, lambda: fpn.BASE, lambda st: None)
        except cloud_worker.Refusal as err:
            problem = err
    return (sent[0] if sent else None), saved, created, problem


def check_base_in_text(text, base):
    if base not in text or DEFAULT_SENTENCE in text:
        raise AssertionError("the base was not in the setup text")


PART = (Path(cloud_worker.__file__).parent / "cloud_base.py")


class TestBase(unittest.TestCase):
    def test_a_base_is_named_as_the_start_of_the_branch_and_the_default_branch_is_not(self):
        text, saved, _, problem = launch(BASE)
        self.assertIsNone(problem)
        check_base_in_text(text, BASE)
        self.assertIn("switch to the branch b (create it from origin/integration/topic", text)
        self.assertNotIn("default branch", text)
        self.assertEqual([r.get("base") for r in saved if "base" in r], [BASE])

    def test_no_base_gives_todays_text_byte_for_byte(self):
        today = cloud_worker.SETUP_PREAMBLE % {"repo": "o/r", "branch": "b"} + "do it\n"
        for given in ("unset", None):
            text, saved, _, problem = launch(given)
            self.assertIsNone(problem)
            self.assertEqual(text, today)
            self.assertIn(DEFAULT_SENTENCE, text)
            self.assertFalse([r for r in saved if "base" in r])

    def test_a_launch_with_a_base_does_not_leak_into_the_next_launch_without_one(self):
        launch(BASE)
        text, _, _, _ = launch("unset")
        self.assertIn(DEFAULT_SENTENCE, text)

    def test_the_base_is_not_handed_to_the_launcher(self):
        _, _, created, _ = launch(BASE)
        self.assertTrue(created)
        self.assertNotIn("--base", repr(created))

    def test_a_refused_base_exits_2_and_creates_nothing(self):
        for bad in ("../x", "-x", "", "a b", "a/../b", "a//b", "/a", "a/", "x" * 201, "a;b"):
            text, saved, created, problem = launch(bad)
            self.assertIsInstance(problem, cloud_worker.Refusal, repr(bad))
            self.assertIn("--base", str(problem))
            self.assertEqual((text, saved, created), (None, [], []), repr(bad))

    def test_a_good_base_with_dots_and_dashes_is_accepted(self):
        for good in ("main", "release-1.2", "cw/base-idle", "a.b/c_d"):
            self.assertIsNone(launch(good)[3], good)

    def test_control_the_unmutated_part_passes_the_text_check(self):
        text, _, _, _ = launch(BASE, part_source=PART.read_text())
        check_base_in_text(text, BASE)

    def test_mutant_without_the_base_in_the_text_fails_with_anchored_text(self):
        src = PART.read_text()
        old = '    H["SETUP_PREAMBLE"] = text\n'
        self.assertEqual(src.count(old), 1, "anchored text")
        mutant = src.replace(old, "    pass\n")
        self.assertNotEqual(mutant, src, "the mutation was a no-op")
        text, _, _, _ = launch(BASE, part_source=mutant)
        with self.assertRaisesRegex(AssertionError, "^the base was not in the setup text$"):
            check_base_in_text(text, BASE)


class TestOption(unittest.TestCase):
    def test_the_option_is_declared_on_launch(self):
        source = (Path(cloud_worker.__file__).parent / "fleet-switchboard").read_text()
        self.assertEqual(source.count('launch.add_argument("--base", help="With --cloud: the branch the worker\'s branch is created from'), 1)


if __name__ == "__main__":
    unittest.main()
