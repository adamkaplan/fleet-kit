"""The launch row of a cloud agent holds its endpoint; a registry failure is said on standard error and goes on (ask #1510)."""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "bin")


def load(name):
    spec = importlib.util.spec_from_file_location("t1510_" + name, os.path.join(BIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sbcw = load("cloud_worker")
REG = load("registry")
SECRET = "ghp_" + "A" * 36


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = os.path.join(tmp.name, "state")
        os.makedirs(self.state)
        self.audit = []
        self.engine = types.SimpleNamespace(actor="op", audit=types.SimpleNamespace(append=lambda a, ev, n, d: self.audit.append((ev, n, d))))

    def note(self, what, *args, registry=REG, state=None):
        err = io.StringIO()
        with mock.patch.dict(sbcw.H, {"load_module": lambda name: registry, "_one_line": lambda t, n: " ".join(str(t).split())[:n]}), \
                contextlib.redirect_stderr(err):
            sbcw._registry_note(self.engine, what, self.state if state is None else state, *args)
        return err.getvalue()

    def row(self, name):
        return REG.get(self.state, name)

    def launch_row(self, name, url):
        return {"session": "ses_" + name, "repo": "acme-org/widget-service", "role": "coder", "kind": "cloud", "run_id": "run_" + name, "state": "working", "endpoint": url}

    def test_s2_two_launches_two_rows_two_endpoints(self):
        for name, url in (("w1", "https://worker-aaa.example.test:4096"), ("o1", "https://worker-bbb.example.test:4096")):
            self.assertEqual(self.note("upsert", name, self.launch_row(name, url)), "")
        rows = [self.row("w1"), self.row("o1")]
        self.assertEqual([r["kind"] for r in rows], ["cloud", "cloud"])
        self.assertEqual([r["endpoint"] for r in rows], ["https://worker-aaa.example.test:4096", "https://worker-bbb.example.test:4096"])
        self.assertEqual(len({r["endpoint"] for r in rows}), 2)

    def test_s2_the_launch_call_passes_the_records_own_address(self):
        src = open(os.path.join(BIN, "cloud_worker.py")).read()
        self.assertRegex(src, r'_registry_note\(engine, "upsert".*"endpoint": record\["url"\]')

    def test_s2_unwritable_state_writes_no_row_says_so_and_goes_on(self):
        blocker = os.path.join(self.state, "file")
        open(blocker, "w").close()
        err = self.note("upsert", "w1", self.launch_row("w1", "https://worker-aaa.example.test:4096"), state=blocker)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("registry: w1: "))
        self.assertIsNone(self.row("w1"))

    def test_s4_credentialed_endpoint_is_refused_by_the_rule_and_said_once(self):
        url = "https://user:pw@worker-aaa.example.test:4096"
        err = self.note("upsert", "w1", self.launch_row("w1", url))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertNotIn(url, err)
        self.assertIsNone(self.row("w1"))
        self.assertEqual([a[0] for a in self.audit], ["cloud.registry_error"])

    def test_s4_problem_and_raise_say_one_line_without_endpoint_or_secret(self):
        url = "https://worker-aaa.example.test:4096"

        class Problem:
            upsert = staticmethod(lambda s, n, a: {"problem": "bad %s" % url})
            retire = staticmethod(lambda s, n, b, r: {"problem": "bad"})

        class Raises:
            upsert = staticmethod(lambda s, n, a: (_ for _ in ()).throw(RuntimeError("boom %s %s" % (url, SECRET))))
            retire = staticmethod(lambda s, n, b, r: (_ for _ in ()).throw(RuntimeError("boom " + SECRET)))
        for fake, what, args in ((Problem, "upsert", (self.launch_row("w1", url),)), (Raises, "upsert", (self.launch_row("w1", url),)), (Raises, "retire", ("done",))):
            self.audit.clear()
            err = self.note(what, "w1", *args, registry=fake)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("w1", err)
            self.assertNotIn(url, err)
            self.assertNotIn(SECRET, err)
            self.assertEqual(len(self.audit), 1)
            self.assertNotIn(SECRET, repr(self.audit))

    def test_s3_retire_once_and_a_second_pass_retires_nothing(self):
        self.note("upsert", "w1", self.launch_row("w1", "https://worker-aaa.example.test:4096"))
        calls = []
        real = REG.retire
        fake = types.SimpleNamespace(upsert=REG.upsert, retire=lambda *a: (calls.append(a), real(*a))[1])
        self.note("retire", "w1", "done", registry=fake)
        self.assertIsNone(self.row("w1"))
        self.note("retire", "w1", "done", registry=fake)   # the clear pass: the row is gone
        self.assertEqual(len([c for c in calls if self.row("w1") is None]), 2)   # the registry's own retire is idempotent: no row appears
        self.assertIsNone(self.row("w1"))

    def test_s3_a_failed_stop_leaves_the_row(self):
        self.note("upsert", "w1", self.launch_row("w1", "https://worker-aaa.example.test:4096"))
        notes = []
        with mock.patch.object(sbcw, "_registry_note", lambda *a: notes.append(a)), mock.patch.object(sbcw, "Cluster", mock.MagicMock), \
                mock.patch.object(sbcw, "_may_retire", lambda k, r: True), \
                mock.patch.dict(sbcw.H, {"LaunchError": RuntimeError, "build_engine": lambda r, n, c: (self.engine, self.state, None),
                                         "gates_caller_kind": lambda r, n: ("person", "x"), "EXIT_OK": 0, "EXIT_ERROR": 1, "PROG": "sb"}), \
                mock.patch.object(sbcw, "run_launcher", lambda *a, **k: (3, None, "kubectl error")), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                sbcw.cmd_retire(types.SimpleNamespace(name="w1", reason="done", undo=False), None, None, {"runtime_id": "pod/w1"})
        self.assertEqual(notes, [])
        self.assertIsNotNone(self.row("w1"))

    def test_s5_mutants_are_killed(self):
        applied = []
        src = open(os.path.join(BIN, "cloud_worker.py")).read()
        mutated = src.replace(', "endpoint": record["url"]', "")
        applied.append(mutated != src)
        self.assertNotRegex(mutated, r'"endpoint": record\["url"\]')   # the endpoint-dropping mutant fails the S2 call check
        self.assertTrue(all(applied), "the mutation was not applied")
        # the row-left-behind mutant: a retire that does nothing leaves the row, which the S3 assertion refuses
        self.note("upsert", "w1", self.launch_row("w1", "https://worker-aaa.example.test:4096"))
        noop = types.SimpleNamespace(upsert=REG.upsert, retire=lambda *a: {"problem": None})
        self.note("retire", "w1", "done", registry=noop)
        self.assertIsNotNone(self.row("w1"), "the mutant did not leave the row")
        # a row for a launch with no state: an empty state writes nothing under the real rule
        err = self.note("upsert", "w2", self.launch_row("w2", "https://worker-aaa.example.test:4096"), state=os.path.join(self.state, "file-not-dir", "x"))
        self.assertIsNone(self.row("w2"))
        self.assertTrue(err)


class Docs(unittest.TestCase):
    def test_s6_both_documents_say_who_when_retire_reaper_and_failure_rule(self):
        root = os.path.dirname(BIN)
        for rel in ("docs/cloud-worker-interface.md", "cloud/launcher/README.md"):
            with open(os.path.join(root, rel)) as handle:
                text = " ".join(handle.read().split())
            found = [bool(re.search(p, text)) for p in (
                r"switchboard-side launch writes the agent's registry row",
                r"worker's own address as (the )?`endpoint`",
                r"retire[s]? it once|retire it once",
                r"reaper's own stop of a pod writes nothing",
                r"never refuses a launch or keeps a pod")]
            self.assertTrue(all(found), (rel, found))


if __name__ == "__main__":
    unittest.main()
