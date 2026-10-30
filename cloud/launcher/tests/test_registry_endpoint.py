"""The launch row of a cloud agent holds its kind and no endpoint; a registry failure is said on standard error and goes on (ask #1510).

These tests RUN the launch, the stop and the clear (bin/test-cloud-worker's fake cluster and stub launcher, a scratch state directory) and read
the registry's own file. The mutants copy the source tree to a scratch directory, assert the change was applied, and run the behaviour tests there:
a mutant is caught when that run exits 1.
"""
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
        self.assertEqual(sorted(rows), ["coder7", "coder8"])
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
        self.assertEqual(sorted(REG.read(str(self.state))["retired"]), ["coder7"])

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
        self.assertEqual(sorted(REG.read(str(self.state))["retired"]), ["coder7"])

    def test_matches_a_credentialed_record_address_fails_the_launch_before_any_row(self):
        with mock.patch.dict(os.environ, {"STUB_URL": "https://user:pw@worker.example.internal:4096"}):
            code, out, err = self.launch("coder7")
        self.assertEqual(code, 1)
        self.assertEqual(self.rows(), {})

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


@unittest.skipIf(IN_MUTANT, "a mutant run does not run the mutants")
class Mutants(unittest.TestCase):
    """A scratch copy of the tree with ONE change asserted applied; the behaviour tests run in it and must exit 1."""
    CASES = (
        ("the endpoint written again", "bin/cloud_worker.py", '"state": "working"}, address=record["url"])', '"state": "working", "endpoint": record["url"]}, address=record["url"])'),
        ("a row written though the registry is unavailable", "bin/cloud_worker.py",
         "    except Exception as err:\n        say(str(err))",
         "    except Exception as err:\n        say(str(err))\n        H[\"load_module\"](\"registry\").upsert(__import__(\"tempfile\").gettempdir(), name, dict(args[0])) if what == \"upsert\" else None"),
        ("the row left behind at the stop", "bin/cloud_worker.py", '    _registry_note(engine, "retire", state, args.name, reason)', "    pass"),
        ("the row left behind at the clear", "bin/cloud_worker.py",
         '        _registry_note(engine, "retire", state, name, "cleared: the pod is gone and the hard deadline has passed")', "        pass"),
        ("the row retired before the stop", "bin/cloud_worker.py",
         "    try:\n        with Cluster() as cluster:\n            code, body, note = run_launcher(runner, \"stop\", args.name, [\"--reason\", reason], STOP_SECONDS, cluster=cluster)",
         "    _registry_note(engine, \"retire\", state, args.name, reason)\n    try:\n        with Cluster() as cluster:\n            code, body, note = run_launcher(runner, \"stop\", args.name, [\"--reason\", reason], STOP_SECONDS, cluster=cluster)"),
    )

    def run_mutant(self, rel, old, new):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            copy = Path(tmp) / "tree"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", tmp and Path(tmp).name))
            target = copy / rel
            text = target.read_text()
            self.assertEqual(text.count(old), 1, "the mutation does not apply once")
            target.write_text(text.replace(old, new))
            self.assertNotEqual(target.read_text(), text, "the mutation was not applied")
            env = dict(os.environ, CW1510_IN_MUTANT="1")
            done = subprocess.run([sys.executable, "-m", "unittest", "test_registry_endpoint.Behaviour"], cwd=str(copy / "cloud/launcher/tests"),
                                  env=env, capture_output=True, text=True, timeout=900)
            return done.returncode

    def test_matches_the_unchanged_copy_passes(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            copy = Path(tmp) / "tree"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", Path(tmp).name))
            done = subprocess.run([sys.executable, "-m", "unittest", "test_registry_endpoint.Behaviour"], cwd=str(copy / "cloud/launcher/tests"),
                                  env=dict(os.environ, CW1510_IN_MUTANT="1"), capture_output=True, text=True, timeout=900)
            self.assertEqual(done.returncode, 0, done.stderr[-1500:])

    def test_matches_each_mutant_exits_1(self):
        for label, rel, old, new in self.CASES:
            with self.subTest(label):
                self.assertEqual(self.run_mutant(rel, old, new), 1, label)


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
