#!/usr/bin/env python3
"""`inspect NAME` (ask #2115, slice 2): the launcher's read-only look inside a pod and the switchboard's command that shows it to the pod's boss.

Offline: a wrapper around the fake kubectl answers `exec` of `cw-pod-inspect` and of `git` from a stub file (no such helper in the image: the fallback);
every other call is the fake kubectl's. The helper's output is the agreed shape of slice 1. No cloud, no network, no real credential.
"""
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_cloud_status_summary import Base, sb, CLOUD, LAUNCHER, ALIVE   # noqa: E402

WRAPPER = '''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if "exec" in a:
    tool = a[a.index("exec") + 3:]
    cfg = json.load(open(os.environ["INSPECT_STUB"]))
    if tool[0] == "cw-pod-inspect":
        if cfg.get("helper") is None:
            sys.stderr.write("executable file not found in $PATH\\n"); sys.exit(126)
        sys.stdout.write(cfg["helper"]); sys.exit(0)
    if tool[0] == "git":
        what = tool[3]
        key = {"rev-parse": "branch", "rev-list": "unpushed", "status": "dirty"}[what]
        sys.stdout.write(cfg["git"][key]); sys.exit(0)
os.execv(os.environ["INSPECT_REAL_KUBECTL"], [os.environ["INSPECT_REAL_KUBECTL"]] + a)
'''
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2
KNOWN = "synthetic-cluster-token"   # the cluster token of the fake login: the launcher's known set holds it
URL = "https://builder:Pa55w0rdZ@git.example.internal/acme/widget.git"
MARK = "[redacted]"
FIELDS = ("source", "in_flight", "last_calls", "last_text", "idle_seconds", "branch", "unpushed", "dirty", "last_push", "last_error", "missing")
BOSS = ("agent", "orch", "orchestrator")
OTHER = ("agent", "other-orch", "orchestrator")
HELPER = {"in_flight": {"tool": "bash", "seconds": 340},
          "last_calls": [{"tool": "bash", "status": "error", "seconds_ago": 400}, {"tool": "read", "status": "ok", "seconds_ago": 410}],
          "last_text": "running the long test", "idle_seconds": None, "branch": "cf1/wip", "unpushed": {"count": 2, "shas": ["a" * 40, "b" * 40]},
          "dirty": 3, "last_push": "2026-11-04T10:00:00Z", "missing": []}


class Inspect(Base):
    def setUp(self):
        super().setUp()
        wrapper = self.home / "kubectl-wrap"
        wrapper.write_text(WRAPPER)
        wrapper.chmod(0o700)
        self.stubfile = self.home / "inspect-stub.json"
        patch = mock.patch.dict(os.environ, {"CW_KUBECTL": str(wrapper), "INSPECT_REAL_KUBECTL": os.path.join(HERE, "fake_kubectl"),
                                             "INSPECT_STUB": str(self.stubfile)})
        patch.start()
        self.addCleanup(patch.stop)
        self.stub({})
        self.record("coder7", hard=ALIVE)
        pod = self.pod("coder7-" + hashlib.sha1(b"coder7").hexdigest()[:4], "coder7", hard=ALIVE)
        self.put_pods(pod)
        state = self.cluster()
        state["activity"] = {pod["metadata"]["name"]: {"busy": False, "last_activity": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
        self.write_cluster(state)

    def stub(self, helper, git=None):
        self.stubfile.write_text(json.dumps({"helper": None if helper is None else (helper if isinstance(helper, str) else json.dumps(helper)),
                                              "git": git or {"branch": "cf1/wip\n", "unpushed": "a" * 40 + "\n" + "b" * 40 + "\n", "dirty": " M a.py\n?? b.txt\n"}}))

    def as_caller(self, kind, *argv):
        with mock.patch.object(sb, "gates_caller_kind", lambda runner, now: kind):
            return self.run_command(*argv)

    def fields(self, out):
        return dict(line.split(": ", 1) for line in out.splitlines())


class TestTheRead(Inspect):
    def test_s2_the_caller_gets_all_eleven_fields(self):
        self.stub(HELPER)
        code, out, err = self.as_caller(BOSS, "inspect", "coder7")
        self.assertEqual(code, 0, err)
        got = self.fields(out)
        self.assertEqual(tuple(got), FIELDS)
        self.assertEqual(len(got), 11)
        self.assertEqual(json.loads(got["in_flight"]), {"seconds": 340, "tool": "bash"})
        self.assertEqual(json.loads(got["unpushed"])["count"], 2)
        self.assertIn("bash error 400s ago", got["last_error"])
        self.assertEqual(got["source"], '"cw-pod-inspect"')

    def test_the_short_line_has_the_agreed_shape(self):
        self.stub(HELPER)
        code, out, err = self.as_caller(BOSS, "inspect", "coder7", "--short")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "coder7: running bash for 340s | last: bash error | idle ? | branch cf1/wip, 2 unpushed, dirty 3 | last push 2026-11-04T10:00:00Z")

    def test_the_fallback_reads_git_and_the_activity_and_lists_what_is_missing(self):
        self.stub(None)
        code, out, err = self.as_caller(BOSS, "inspect", "coder7")
        self.assertEqual(code, 0, err)
        got = self.fields(out)
        self.assertEqual(got["source"], '"fallback"')
        self.assertEqual(json.loads(got["branch"]), "cf1/wip")
        self.assertEqual(json.loads(got["unpushed"])["count"], 2)
        self.assertEqual(json.loads(got["dirty"]), 2)
        self.assertGreaterEqual(json.loads(got["idle_seconds"]), 110)
        self.assertEqual(sorted(json.loads(got["missing"])), ["in_flight", "last_calls", "last_error", "last_push", "last_text"])

    def test_s2_another_lanes_pod_is_refused(self):
        self.stub(HELPER)
        code, out, err = self.as_caller(OTHER, "inspect", "coder7")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("only coder7's own boss may inspect it", err)
        self.assertNotIn("bash", err)

    def test_a_person_is_refused_too(self):
        code, out, err = self.as_caller(("person", "a person at a shell"), "inspect", "coder7")
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")


class TestTheScrub(Inspect):
    def planted(self):
        return dict(HELPER, last_text="note " + TOKEN + " end", in_flight={"tool": "run-" + KNOWN, "seconds": 5}, last_push=URL, branch="cf1/wip")

    def run_with(self, launcher):
        env = {CLOUD.LAUNCHER_ENV: launcher}
        with mock.patch.dict(os.environ, env):
            return self.as_caller(BOSS, "inspect", "coder7")

    def test_s3_three_planted_secrets_never_print_and_each_has_the_mark(self):
        self.stub(self.planted())
        code, out, err = self.as_caller(BOSS, "inspect", "coder7")
        self.assertEqual(code, 0, err)
        for secret in (TOKEN, KNOWN, "Pa55w0rdZ", URL):
            self.assertNotIn(secret, out + err)
        self.assertGreaterEqual(out.count(MARK), 3, out)

    def test_s3_a_launcher_without_the_scrub_prints_them_mutant(self):
        """Control first (the real launcher prints none), then the mutant: `applied` is computed from the two texts, a no-op fails, the anchored text is unique."""
        self.stub(self.planted())
        code, control, err = self.run_with(os.path.join(LAUNCHER, "cloud-worker"))
        self.assertEqual(code, 0, err)
        self.assertEqual([s for s in (TOKEN, KNOWN, "Pa55w0rdZ") if s in control], [])
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        copy = root / "cloud" / "launcher"   # the launcher finds the kit's bin/ two levels up
        shutil.copytree(LAUNCHER, str(copy), symlinks=True, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        (root / "bin").mkdir()
        for module in ("path_walk.py", "registry.py", "record_json.py"):
            shutil.copy2(os.path.join(os.path.dirname(os.path.dirname(LAUNCHER)), "bin", module), str(root / "bin" / module))
        target = copy / "cloud_worker.py"
        old = target.read_text()
        anchor = "        scrub_ = self.kube.scrub\n"
        self.assertEqual(old.count(anchor), 1)
        new = old.replace(anchor, "        scrub_ = lambda v, limit=300: str(v)[:limit]\n", 1)
        self.assertNotEqual(old, new)   # applied, computed from the two texts: a no-op mutation fails here
        self.assertEqual(len(old.splitlines()), len(new.splitlines()))
        target.write_text(new)
        launcher = [str(copy / n) for n in ("cloud-worker",) if (copy / n).exists()]
        self.assertTrue(launcher, os.listdir(str(copy)))
        code, mutant, err = self.run_with(launcher[0])
        self.assertEqual(code, 0, err)
        self.assertEqual([s for s in (TOKEN, KNOWN, "Pa55w0rdZ") if s in mutant], [TOKEN, KNOWN, "Pa55w0rdZ"], mutant)


if __name__ == "__main__":
    unittest.main()
