"""#2246: the issue a launch names is stored in the cloud record as record['issue'] (an int), beside record['reports_to'].
Run: python3 -m unittest tests.test_record_issue"""
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


def saved(issue, source=None):
    """The record the REAL `_launch` hands to save_record (the module from `source` text when given); the cluster and the pod are stubbed."""
    module = fpn.CW() if source is None else fpn.mutated_module(source)
    records = []
    args = argparse.Namespace(name="coder5", agent="worker", reports_to="lead", boss_unregistered="test", checkpoint="30m", resume=False, headless=False, dir=None, shape=None,
                              stack=False, brief_file=None, idle=None, hard=None, role="coder", issue=issue, charter=None, model=None, reasoning=None, brief=None, branch="b", repo="o/r")
    engine = types.SimpleNamespace(now=lambda: fpn.BASE, discover=lambda at: types.SimpleNamespace(agents={}), config={"call_timeout_seconds": 5}, audit=None, actor="t")
    lib = types.SimpleNamespace(check_repo_preflight=lambda repo, env: "ok", CloudError=Exception, AGENTS=("worker",))
    stubs = dict(use_settings=lambda: "test", check_allowed=lambda repo: None, _lib=lambda: lib, launcher_argv=lambda: ["x"], VaultRecovery=lambda *a: None,
                 Cluster=fpn.FakeCluster, model_entry=lambda: "m", git_token=lambda *a, **k: "t", run_launcher=lambda *a, **k: (0, {}, ""),
                 _read_password=lambda fd: "pw", _record_from_create=lambda n, b: {"runtime_id": "r1", "url": "https://pod.example"},
                 _write_private=lambda *a: None, save_record=lambda state, record: records.append(dict(record)), load_record=lambda *a: None,
                 _audit=lambda *a, **k: None, tunnel=lambda cluster, record: contextlib.nullcontext(object()), remote_v2=lambda *a: fpn.PodSessionStub(),
                 _registry_note=lambda *a, **k: None)
    with tempfile.TemporaryDirectory() as state, contextlib.ExitStack() as stack:
        heads = {"launch_repo": lambda a: "o/r", "build_engine": lambda *a: (engine, state, None), "read_brief": lambda *a: "brief", "fleet_record": lambda *a: {}}
        for key, value in stubs.items():
            stack.enter_context(mock.patch.object(module, key, value, create=True))
        stack.enter_context(mock.patch.dict(module.H, heads))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        module._launch(args, None, lambda: fpn.BASE, lambda st: None)
    if not records:
        raise AssertionError("no record was saved")
    return records[0]


def check(record):
    if record.get("reports_to") != "lead":
        raise AssertionError("the stub launch did not store reports_to: %r" % record)
    if record.get("issue") != 2243:
        raise AssertionError("record['issue'] is not 2243: %r" % record.get("issue"))


class TestRecordIssue(unittest.TestCase):
    def test_launch_with_issue_writes_an_int(self):
        record = saved(2243)
        check(record)
        self.assertIs(type(record["issue"]), int)

    def test_issue_given_as_text_is_stored_as_an_int(self):
        self.assertEqual(saved("2243")["issue"], 2243)

    def test_no_issue_no_key(self):
        self.assertNotIn("issue", saved(None))

    def test_mutant_without_the_line_fails_with_anchored_text(self):
        src = Path(cloud_worker.__file__).read_text()
        old = '                record["issue"] = int(issue)'
        self.assertEqual(src.count(old), 1, "anchored text")
        check(saved(2243, source=src))   # control: the unchanged text passes
        mutant = src.replace(old, "                pass")
        with self.assertRaisesRegex(AssertionError, "^record\\['issue'\\] is not 2243"):
            check(saved(2243, source=mutant))


if __name__ == "__main__":
    unittest.main()
