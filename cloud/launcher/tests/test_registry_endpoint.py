"""The launch row of a cloud agent holds its kind and no endpoint; a registry failure is said on standard error and goes on (ask #1510).

These tests RUN the launch, the stop and the clear (bin/test-cloud-worker's fake cluster and stub launcher, a scratch state directory) and read
the registry's own file. The mutants copy the source tree to a scratch directory, assert the change was applied, and run the behaviour tests there:
a mutant is caught when that run exits 1.
"""
import concurrent.futures
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BIN = ROOT / "bin"
SECRET = "ghp_" + "A" * 36
IN_MUTANT = os.environ.get("CW1510_IN_MUTANT") == "1"


def _load_base():
    loader = importlib.machinery.SourceFileLoader("t1510_tcw", str(BIN / "test-cloud-worker"))
    spec = importlib.util.spec_from_loader("t1510_tcw", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["t1510_tcw"] = module
    loader.exec_module(module)
    return module


TCW = _load_base()
sbcw = importlib.util.module_from_spec(importlib.util.spec_from_file_location("t1510_cw", str(BIN / "cloud_worker.py")))
sbcw.__spec__.loader.exec_module(sbcw)
REG = importlib.util.module_from_spec(importlib.util.spec_from_file_location("t1510_reg", str(BIN / "registry.py")))
REG.__spec__.loader.exec_module(REG)


class Behaviour(TCW.Base):
    def rows(self):
        return REG.read(str(self.state))["agents"]

    def blocked_registry(self):
        """The registry's file cannot be read or written: a directory stands where registry.json goes."""
        os.makedirs(str(self.state), mode=0o700, exist_ok=True)
        (self.state / REG.FILE).unlink(missing_ok=True)   # #1046: the base seeded the boss row, a file
        (self.state / REG.FILE).mkdir()

    def audit_lines(self, word):
        return [l for p in self.state.rglob("*") if p.is_file() and p.suffix != ".password" for l in p.read_text(errors="replace").splitlines() if word in l]

    @contextlib.contextmanager
    def counting(self):
        """The registry module the switchboard loads, with its retire recorded (and still run)."""
        module = TCW.sb.load_module("registry")
        calls = []
        real = module.retire
        with mock.patch.object(module, "retire", lambda *a, **k: (calls.append(a[1:3]) if a[2] == "cloud-worker" else None, real(*a, **k))[1]):
            yield calls

    def address(self, host):
        return "https://%s.example.test:%s" % (host, self.remote.url.rsplit(":", 1)[1])   # the stub's port-forward still reaches the fake remote

    def launch_at(self, name, url):
        with mock.patch.dict(os.environ, {"STUB_URL": url}):
            return self.launch(name)

    def past_deadline(self, name):
        path = self.state / "cloud" / (name + ".json")
        record = json.loads(path.read_text())
        record["hard_deadline"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(record))

    def test_matches_two_launches_write_one_row_each_with_kind_cloud_and_no_endpoint(self):
        urls = {"coder7": self.address("worker-aaa"), "coder8": self.address("worker-bbb")}
        for name, url in urls.items():
            code, out, err = self.launch_at(name, url)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out.strip().splitlines()[-1])["url"], json.loads((self.state / "cloud" / (name + ".json")).read_text())["url"])
        rows = self.rows()
        self.assertLessEqual(set(urls), set(rows))   # a row for each launch
        for name in ("coder7", "coder8", "orch"):
            self.assertIn(name, rows)
        for name, url in urls.items():
            self.assertEqual(rows[name]["kind"], "cloud")
            self.assertNotIn("endpoint", rows[name])
            self.assertEqual(json.loads((self.state / "cloud" / (name + ".json")).read_text())["url"], url)
            self.assertFalse(rows[name].get("pane"))
        self.assertFalse(any("endpoint" in r for r in rows.values()))

    def test_matches_a_launch_whose_registry_is_unavailable_writes_no_row_and_goes_on(self):
        self.blocked_registry()
        url = self.address("worker-aaa")
        code, out, err = self.launch_at("coder7", url)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["name"], "coder7")
        lines = [l for l in err.splitlines() if l.startswith("registry: coder7: ")]
        self.assertEqual(len(lines), 1, err)
        self.assertNotIn(url, lines[0])
        self.assertNotIn("worker-aaa", lines[0])
        self.assertEqual(len(self.audit_lines("cloud.registry_error")), 1)
        self.assertTrue((self.state / REG.FILE).is_dir())   # no row, and no file written in its place
        self.assertEqual([p for p in self.home.rglob(REG.FILE) if p.is_file()], [])

    def test_matches_stop_retires_the_row_once_and_a_second_run_retires_nothing(self):
        self.assertEqual(self.launch_at("coder7", self.address("worker-aaa"))[0], 0)
        self.assertIn("coder7", self.rows())
        with self.counting() as calls:
            self.assertEqual(self.main("retire", "coder7")[0], 0)
            self.assertNotIn("coder7", self.rows())
            self.assertEqual(calls, [("coder7", "cloud-worker")])
            self.main("retire", "coder7")   # a second run: the record is gone, the cloud path retires nothing more
            self.assertEqual(calls, [("coder7", "cloud-worker")])
        self.assertIn("coder7", REG.read(str(self.state))["retired"])

    def test_matches_a_failed_stop_leaves_the_row(self):
        self.launch_at("coder7", self.address("worker-aaa"))
        with self.counting() as calls, mock.patch.dict(os.environ, {"STUB_STOP_EXIT": "1"}):
            code, out, err = self.main("retire", "coder7")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(self.rows()["coder7"]["kind"], "cloud")
        self.assertNotIn("coder7", REG.read(str(self.state))["retired"])

    def test_matches_the_cloud_sweep_retires_the_row_once_and_a_second_run_retires_nothing(self):
        stub = self.stub.read_text().replace('elif command == "status":', 'elif command == "snapshot":\n    print(json.dumps({"pods": []}))\nelif command == "status":', 1)
        self.stub.write_text(stub)
        self.launch_at("coder7", self.address("worker-aaa"))
        self.past_deadline("coder7")
        with self.counting() as calls:
            code, out, err = self.main("cloud-clear")
            self.assertEqual(code, 0, err)
            self.assertIn("cleared coder7", out)
            self.assertEqual(calls, [("coder7", "cloud-worker")])
            self.main("cloud-clear")
            self.assertEqual(calls, [("coder7", "cloud-worker")])
        self.assertNotIn("coder7", self.rows())
        self.assertIn("coder7", REG.read(str(self.state))["retired"])
        code, out, err = self.main("cloud-clear")   # a second run: no record, nothing more
        self.assertEqual(code, 0)
        self.assertIn("no cloud worker records", out)
        self.assertIn("coder7", REG.read(str(self.state))["retired"])

    def test_matches_a_credentialed_record_address_fails_the_launch_before_any_row(self):
        with mock.patch.dict(os.environ, {"STUB_URL": "https://user:pw@worker.example.internal:4096"}):
            code, out, err = self.launch("coder7")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.rows()), ["orch"])   # no launch row; only the seeded boss (#1046)

    def test_matches_a_registry_that_fails_says_one_line_without_endpoint_or_secret(self):
        url = self.remote.url
        host = url.split("//")[1].split(":")[0]

        class Problem:
            upsert = staticmethod(lambda s, n, a: {"problem": "bad %s %s" % (url, host)})
            retire = staticmethod(lambda s, n, b, r: {"problem": "bad"})

        class Raises:
            upsert = staticmethod(lambda s, n, a: (_ for _ in ()).throw(RuntimeError("boom %s %s" % (url.upper(), SECRET))))
            retire = staticmethod(lambda s, n, b, r: (_ for _ in ()).throw(RuntimeError("boom " + SECRET)))
        for fake, what, args in ((Problem, "upsert", ({"endpoint": url, "kind": "cloud"},)), (Raises, "upsert", ({"endpoint": url, "kind": "cloud"},)),
                                 (Raises, "retire", ("done",))):
            audit = []
            engine = types.SimpleNamespace(actor="op", audit=types.SimpleNamespace(append=lambda a, ev, n, d: audit.append((ev, n, d))))
            err = io.StringIO()
            with mock.patch.dict(sbcw.H, {"load_module": lambda name: fake, "_one_line": lambda t, n: " ".join(str(t).split())[:n]}), contextlib.redirect_stderr(err):
                sbcw._registry_note(engine, what, str(self.state), "w1", *args, address=url)
            text = err.getvalue()
            self.assertEqual(len(text.strip().splitlines()), 1, text)
            self.assertIn("w1", text)
            for bad in (url, url.upper(), host, SECRET):
                self.assertNotIn(bad, text)
            self.assertEqual(len(audit), 1)
            self.assertNotIn(SECRET, repr(audit))


    PLANTED = ("vault-prod-kv-7", "https://vault.internal.test:8443/v1/x", "/var/lib/secret-store/key")

    def planted_run(self, what):
        text = "failed with " + " and ".join(self.PLANTED)

        class Raises:
            upsert = staticmethod(lambda s, n, a: (_ for _ in ()).throw(OSError(text)))
            retire = staticmethod(lambda s, n, b, r: (_ for _ in ()).throw(OSError(text)))
        audit = []
        engine = types.SimpleNamespace(actor="op", audit=types.SimpleNamespace(append=lambda a, ev, n, d: audit.append((ev, n, d))))
        err = io.StringIO()
        args = ({"kind": "cloud"},) if what == "upsert" else ("done",)
        with mock.patch.dict(sbcw.H, {"load_module": lambda name: Raises, "_one_line": lambda t, n: " ".join(str(t).split())[:n]}), contextlib.redirect_stderr(err):
            sbcw._registry_note(engine, what, str(self.state), "w1", *args, address="")
        return text, err.getvalue(), audit

    def test_matches_planted_text_never_reaches_the_row_the_audit_or_the_standard_error(self):
        for what in ("upsert", "retire"):
            with self.subTest(what):
                text, err, audit = self.planted_run(what)
                for planted in self.PLANTED:   # the control: the exception itself shows them, so a leak would be seen
                    self.assertIn(planted, text)
                lines = [l for l in err.splitlines() if l.strip()]
                self.assertEqual(len(lines), 1, err)
                self.assertIn("the registry write failed (OSError)", lines[0])
                self.assertEqual(len(audit), 1)
                written = "".join(p.read_text(errors="replace") for p in self.state.rglob("*") if p.is_file()) if self.state.exists() else ""
                for planted in self.PLANTED:
                    self.assertNotIn(planted, err, "the exception text reached the note")
                    self.assertNotIn(planted, repr(audit), "the exception text reached the note")
                    self.assertNotIn(planted, written, "the exception text reached the note")

    def test_matches_planted_text_launch_and_stop_still_end_0(self):
        text = "failed " + " ".join(self.PLANTED)
        module = TCW.sb.load_module("registry")
        boom = lambda *a, **k: (_ for _ in ()).throw(OSError(text))
        with mock.patch.object(module, "upsert", boom):
            code, out, err = self.launch_at("coder7", self.address("worker-aaa"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len([l for l in err.splitlines() if l.startswith("registry: coder7: ")]), 1, err)
        for planted in self.PLANTED:
            self.assertNotIn(planted, err, "the exception text reached the note")
        self.assertEqual(self.launch_at("coder8", self.address("worker-bbb"))[0], 0)
        with mock.patch.object(module, "retire", boom):
            code, out, err = self.main("retire", "coder8")
        self.assertEqual(code, 0, err)
        self.assertEqual(len([l for l in err.splitlines() if l.startswith("registry: coder8: ")]), 1, err)
        for planted in self.PLANTED:
            self.assertNotIn(planted, err, "the exception text reached the note")

    def test_matches_a_corrupt_registry_file_goes_on_with_one_line_and_no_row(self):
        os.makedirs(str(self.state), mode=0o700, exist_ok=True)
        (self.state / REG.FILE).write_text("this is not json {")
        code, out, err = self.launch_at("coder7", self.address("worker-aaa"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["name"], "coder7")
        self.assertEqual(len([l for l in err.splitlines() if l.startswith("registry: coder7: ")]), 1, err)
        self.assertNotIn("coder7", (self.state / REG.FILE).read_text())

    def test_matches_the_stop_removes_only_the_exact_named_row(self):
        self.assertEqual(self.launch_at("coder1", self.address("worker-aaa"))[0], 0)
        self.assertEqual(self.launch_at("coder10", self.address("worker-bbb"))[0], 0)
        before = json.dumps(self.rows()["coder10"], sort_keys=True)
        self.assertEqual(self.main("retire", "coder1")[0], 0)
        rows = self.rows()
        self.assertNotIn("coder1", rows)
        self.assertIn("coder10", rows)
        self.assertEqual(json.dumps(rows["coder10"], sort_keys=True), before)


@unittest.skipIf(IN_MUTANT, "a mutant run does not run the mutants")
class Mutants(unittest.TestCase):
    """A scratch copy of the tree with ONE change asserted applied; the behaviour tests run in it and must exit 1."""
    CASES = (
        ("the exception text restored in the note", "bin/cloud_worker.py", 'say("the registry write failed (%s)" % type(err).__name__)', "say(str(err))", "AssertionError: .*the exception text reached the note"),
        ("the endpoint written again", "bin/cloud_worker.py", '"state": "working"}, address=record["url"])', '"state": "working", "endpoint": record["url"]}, address=record["url"])', "^(FAIL|ERROR): test_matches_two_launches_write_one_row_each_with_kind_cloud_and_no_endpoint "),
        ("a row written though the registry is unavailable", "bin/cloud_worker.py",
         "        if got[\"problem\"]:\n            say(got[\"problem\"])",
         "        if got[\"problem\"]:\n            say(got[\"problem\"])\n            open(os.path.join(os.path.dirname(str(state)), \"registry.json\"), \"w\").write(\"{}\") if what == \"upsert\" else None", "^(FAIL|ERROR): test_matches_a_launch_whose_registry_is_unavailable_writes_no_row_and_goes_on "),
        ("the row left behind at the stop", "bin/cloud_worker.py", '    _registry_note(engine, "retire", state, args.name, reason)', "    pass", "^(FAIL|ERROR): test_matches_stop_retires_the_row_once_and_a_second_run_retires_nothing "),
        ("the row left behind at the clear", "bin/cloud_worker.py",
         '        _registry_note(engine, "retire", state, name, "cleared: the pod is gone and the hard deadline has passed")', "        pass", "^(FAIL|ERROR): test_matches_the_cloud_sweep_retires_the_row_once_and_a_second_run_retires_nothing "),
        ("the row retired before the stop", "bin/cloud_worker.py",
         "    try:\n        with Cluster() as cluster:\n            code, body, note = run_launcher(runner, \"stop\", args.name, extra, STOP_SECONDS, cluster=cluster)",
         "    _registry_note(engine, \"retire\", state, args.name, reason)\n    try:\n        with Cluster() as cluster:\n            code, body, note = run_launcher(runner, \"stop\", args.name, extra, STOP_SECONDS, cluster=cluster)", "^(FAIL|ERROR): test_matches_a_failed_stop_leaves_the_row "),
    )

    @staticmethod
    def link_tree(copy, real):
        """A scratch tree of ROOT: the paths in `real` are copied (the mutation and the test run touch only those), everything else is a symlink."""
        def build(src, dst, rels):
            dst.mkdir(parents=True, exist_ok=True)
            for entry in sorted(src.iterdir()):
                if entry.name in (".git", "__pycache__"):
                    continue
                here = [r for r in rels if r == entry.name or r.startswith(entry.name + "/")]
                if not here:
                    (dst / entry.name).symlink_to(entry)
                elif entry.name in here:
                    shutil.copytree(entry, dst / entry.name, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                else:
                    build(entry, dst / entry.name, [r[len(entry.name) + 1:] for r in here])
        build(ROOT, copy, list(real))

    def run_tree(self, rel=None, old=None, new=None, only=None):
        """Run Behaviour in a scratch copy; with a mutation, it is asserted applied (counted). Returns (exit code, output, applied)."""
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "tree"
            self.link_tree(copy, ("cloud/launcher", "bin"))
            applied = False
            occurrences = 0
            if rel:
                target = copy / rel
                text = target.read_text()
                occurrences = text.count(old)
                self.assertEqual(occurrences, 1, "the mutation does not apply once")
                target.write_text(text.replace(old, new))
                applied = target.read_text() != text and occurrences == 1
                self.assertTrue(applied, "the mutation was not applied")
            done = subprocess.run([sys.executable, "-m", "unittest"] + (["-f", "test_registry_endpoint.Behaviour." + only] if only else ["-f", "test_registry_endpoint.Behaviour"]), cwd=str(copy / "cloud/launcher/tests"),
                                  env=dict(os.environ, CW1510_IN_MUTANT="1"), capture_output=True, text=True, timeout=900)
            return done.returncode, done.stderr, applied

    _control_cache = None   # the run of the unmutated copy, made once for the two tests that read it

    def control_run(self):
        if Mutants._control_cache is None:
            Mutants._control_cache = self.run_tree()
        return Mutants._control_cache

    def test_matches_the_unchanged_copy_passes(self):
        code, output, _ = self.control_run()
        self.assertEqual(code, 0, output[-1500:])

    @staticmethod
    def counts(output):
        """(tests run, failures plus errors) read from the unittest summary on standard error."""
        ran = re.search(r"^Ran (\d+) tests?", output, re.M)
        bad = re.search(r"^FAILED \((.*)\)", output, re.M)
        failed = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", bad.group(1))) if bad else 0
        return (int(ran.group(1)) if ran else 0), failed

    def test_matches_each_mutant_fails_with_the_check_named(self):
        control_code, control_output, _ = self.control_run()
        ran, control_failed = self.counts(control_output)
        control_passed = ran - control_failed if control_code == 0 else 0
        self.assertEqual(control_code, 0, "the control must pass first: " + control_output[-1500:])
        def one(case):
            label, rel, old, new, anchor = case
            names = re.search(r"\(FAIL\|ERROR\): (test_\w+)", anchor)
            return self.run_tree(rel, old, new, only=names.group(1) if names else None)
        # a mutant is killed by its first failing check: run only the anchored test when the anchor names one, else stop at the first failure
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(one, self.CASES))
        for (label, rel, old, new, anchor), (code, output, applied) in zip(self.CASES, results):
            with self.subTest(label):
                named = bool(re.search(anchor, output, re.M))
                _, mutant_failed = self.counts(output)
                print("mutant %s: control passes %d of %d, mutant fails %d checks (exit %d), anchored check named %s, mutation applied %s"
                      % (label, control_passed, ran, mutant_failed, code, named, applied))
                self.assertEqual(code, 1, label)
                self.assertTrue(named, "%s: the anchored text %r is missing" % (label, anchor))


class Docs(unittest.TestCase):
    def test_matches_both_documents_say_no_endpoint_why_and_the_stderr_line(self):
        for rel in ("docs/cloud-worker-interface.md", "cloud/launcher/README.md"):
            text = " ".join((ROOT / rel).read_text().split())
            found = [bool(re.search(p, text)) for p in (
                r"switchboard-side launch writes the agent's registry row",
                r"no pane and no endpoint",
                r"`send` by the registry has no fallback and no address a Mac lane can reach exists yet",
                r"ask #1587",
                r"retire[s]? it once|retire it once",
                r"never refuses a launch or keeps a pod",
                r"one line on standard error")]
            self.assertTrue(all(found), (rel, found))


if __name__ == "__main__":
    unittest.main()
