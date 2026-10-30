#!/usr/bin/env python3
"""A launch of a name that exists returns it; a no-wait launch is finished by launching the same name again (ask #1619, scenarios S2-S8, S10, S11).

The switchboard side runs against the stub launcher and the fake cluster of bin/test-cloud-worker (its Base temporary directory is outside the
repository). Mutants and the old behaviour run in scratch copies outside the repository, in a child started through stub_support.start.
Run directly: python3 cloud/launcher/tests/test_launch_idempotent.py
"""
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

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
_loader = importlib.machinery.SourceFileLoader("cw_test_cloud_worker_mod", str(ROOT / "bin" / "test-cloud-worker"))
_spec = importlib.util.spec_from_loader("cw_test_cloud_worker_mod", _loader)
tcw = importlib.util.module_from_spec(_spec)
sys.modules["cw_test_cloud_worker_mod"] = tcw
_loader.exec_module(tcw)
stub_support = tcw.stub_support

PAST = "2000-01-01T00:00:00Z"
POD = "coder7-7f9c"
GATE = '        done = _launch_again(args, runner, engine, state, existing, repo, branch)\n'


class Launch(tcw.Base):
    def setUp(self):
        super().setUp()
        text = self.stub.read_text()
        text = text.replace('"--json")', '"--json", "--no-wait", "--agent", "--boss")', 1)   # the launcher's create knows these (the stub predates them)
        text = text.replace('elif command == "status":', 'elif command == "snapshot":\n    p = os.environ["STUB_DIR"] + "/snapshot.json"\n'
                            '    print(open(p).read() if os.path.exists(p) else json.dumps({"pods": []}))\nelif command == "status":', 1)
        self.stub.write_text(text)
        for name in ("CW_HUB_CHANNEL_URL", "CW_HUB_REGISTRY_SECRET"):
            os.environ.pop(name, None)

    def creates(self):
        return [c for c in self.stub_calls() if c[0] == "create"]

    def run_id_of(self, call):
        return call[call.index("--run-id") + 1]

    def line(self, out):
        return json.loads(out.strip().splitlines()[-1])

    def record_file(self, name="coder7"):
        return self.state / "cloud" / (name + ".json")

    def record(self):
        return json.loads(self.record_file().read_text())

    def end_record(self):
        record = self.record()
        record["hard_deadline"] = PAST
        self.record_file().write_text(json.dumps(record))

    def pods(self, phase="Running", ready=True, present=True):
        pods = [{"name": POD, "worker": "coder7", "phase": phase, "ready": ready}] if present else []
        (self.stub_dir / "snapshot.json").write_text(json.dumps({"pods": pods, "nodes": [], "nodes_read": True}))

    def launch_again(self, *extra, name="coder7"):
        return self.main("launch", name, "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main",
                         "--role", "coder", "--reports-to", "orch", *extra)

    def sessions(self):
        return [b for b in self.remote.bodies if b[0] == "POST" and b[1] == "/api/session"]

    def briefs(self):
        return [b for b in self.remote.bodies if b[0] == "POST" and b[1].endswith("/prompt")]


class TestExists(Launch):
    def test_s3_second_launch_returns_the_existing_worker_and_creates_nothing(self):
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        first, before, kube = self.line(out), self.record_file().read_bytes(), len(self.jsonl("kubectl.log"))
        calls = len(self.stub_calls())
        code, out, err = self.launch()
        again = self.line(out)
        lines = [x for x in err.splitlines() if x.startswith("exists: returned worker coder7 (pod pod/")]
        print("S3: creates by the second launch %d, same runtime id %d, exit %d, line exists %d" % (
            len(self.creates()) - 1, again["runtime_id"] == first["runtime_id"], code, len(lines)))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(again.pop("existing"))
        self.assertEqual(again, first)
        self.assertEqual(len(self.stub_calls()), calls, "the launcher was called")
        self.assertEqual(len(self.jsonl("kubectl.log")), kube, "the cluster was called")
        self.assertEqual(self.record_file().read_bytes(), before)

    def test_s4_launch_after_a_retire_makes_a_new_worker(self):
        self.assertEqual(self.launch()[0], 0)
        code, out, err = self.main("retire", "coder7")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.record_file().exists())
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        runs = [self.run_id_of(c) for c in self.creates()]
        print("S4: creates after the retire %d, run id differs %d" % (len(runs) - 1, runs[0] != runs[1]))
        self.assertEqual(len(runs), 2)
        self.assertNotEqual(runs[0], runs[1])
        self.assertNotIn("existing", self.line(out))

    def test_s5_ended_record_is_decided_by_the_pod(self):
        self.assertEqual(self.launch()[0], 0)
        self.end_record()
        self.pods(present=True)
        before = self.record_file().read_bytes()
        code, out, err = self.launch()
        naming = "retire coder7" in err
        present = len(self.creates()) - 1
        self.assertEqual(code, 1)
        self.assertEqual(self.record_file().read_bytes(), before)
        self.pods(present=False)
        code2, out2, err2 = self.launch()
        absent = len(self.creates()) - 1
        print("S5: creates with the pod absent %d, with the pod present %d, refusals naming retire %d" % (absent, present, naming))
        self.assertEqual(code2, 0, err2)
        self.assertEqual((present, absent, naming), (0, 1, True))
        self.assertEqual(self.record()["hard_deadline"], "2099-10-14T14:00:00Z")

    def test_s6_a_name_that_differs_in_case_is_an_invalid_name(self):
        self.assertEqual(self.launch()[0], 0)
        before = self.record_file().read_bytes()
        calls = len(self.stub_calls())
        code, out, err = self.launch(name="Coder7")
        print("S6: refusals as an invalid name %d, creates %d, record unchanged %d" % (
            code == 1 and "name must match" in err, len(self.creates()) - 1, self.record_file().read_bytes() == before))
        self.assertEqual(code, 1)
        self.assertIn("name must match", err)
        self.assertEqual(out.strip(), "")
        self.assertEqual(len(self.stub_calls()), calls)
        self.assertEqual(self.record_file().read_bytes(), before)


class TestStarting(Launch):
    def no_wait(self):
        brief = self.home / "brief.txt"
        brief.write_text("Build the thing.\n")
        return self.launch_again("--no-wait", "--brief-file", str(brief))

    def test_s7_no_wait_requests_the_pod_and_returns(self):
        code, out, err = self.no_wait()
        creates = self.creates()
        polls = [x for x in err.splitlines() if x.startswith("starting: coder7 requested; run the same launch command again")]
        print("S7: create calls %d, readiness waits %d, records in state starting %d, poll line %d" % (
            len(creates), len([c for c in self.stub_calls() if c[0] == "snapshot"]), self.record().get("state") == "starting", len(polls)))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(creates), 1)
        self.assertIn("--no-wait", creates[0])
        self.assertNotIn("--wait", creates[0])
        self.assertEqual([c for c in self.stub_calls() if c[0] == "snapshot"], [])
        self.assertEqual(self.record()["state"], "starting")
        self.assertEqual(len(polls), 1)
        self.assertIn("read coder7 / status", polls[0])
        self.assertEqual(self.line(out)["state"], "starting")
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.briefs(), [])

    def test_s8_the_same_launch_again_finishes_it_once(self):
        self.assertEqual(self.no_wait()[0], 0)
        self.pods(ready=False)
        code, out, err = self.no_wait()
        still = [x for x in err.splitlines() if x == "still starting: coder7"]
        self.assertEqual(code, 0, err)
        self.assertEqual((len(self.creates()), len(still), self.sessions(), self.briefs()), (1, 1, [], []))
        self.pods(ready=True)
        code, out, err = self.no_wait()
        print("S8: creates 0 and 0, still-starting lines %d, sessions made %d, briefs delivered %d" % (len(still), len(self.sessions()), len(self.briefs())))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(len(self.briefs()), 1)
        self.assertIn("Build the thing.", self.briefs()[0][2]["text"])
        done = self.line(out)
        self.assertEqual((done["session"], "state" in done), ("ses_remote1", False))
        self.assertEqual(self.record()["state"], "ready")
        self.assertFalse((self.state / "cloud" / "coder7.brief").exists())
        code, out, err = self.no_wait()   # once more: the worker is returned, nothing is sent again
        self.assertEqual(code, 0, err)
        self.assertTrue(self.line(out)["existing"])
        self.assertEqual((len(self.creates()), len(self.sessions()), len(self.briefs())), (1, 1, 1))

    def test_s8_a_failed_pod_says_so_and_names_retire(self):
        self.assertEqual(self.no_wait()[0], 0)
        self.pods(phase="Failed", ready=False)
        code, out, err = self.no_wait()
        self.assertEqual(code, 1)
        self.assertIn("retire coder7", err)
        self.assertEqual((len(self.creates()), self.sessions(), self.briefs()), (1, [], []))

    def test_no_wait_does_not_combine_with_a_stack_pod(self):
        code, out, err = self.launch_again("--no-wait", "--stack")
        self.assertEqual(code, 1)
        self.assertEqual(self.creates(), [])


def run_child(case, test_file, selector, mutations):
    """Copy the parts of the tree the test reads to a scratch directory OUTSIDE the repository, apply the mutations (each counted when applied),
    run `selector` of `test_file` there in a child and return (applied, exit status, output)."""
    scratch = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, scratch, True)
    ignore = shutil.ignore_patterns("__pycache__")
    for part in ("bin", "cloud/launcher", "cloud/worker/tests"):
        shutil.copytree(ROOT / part, os.path.join(scratch, part), ignore=ignore)
    applied = 0
    for rel, old, new in mutations:
        path = Path(scratch, rel)
        text = path.read_text()
        if text.count(old) == 1:
            path.write_text(text.replace(old, new, 1))
            applied += 1
    child = stub_support.start(case, [sys.executable, os.path.join(scratch, "cloud", "launcher", "tests", test_file), selector],
                               cwd=scratch, env=dict(os.environ, TMPDIR=scratch), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = child.communicate(timeout=300)
    return applied, child.returncode, out


MUTANT_SECOND_LAUNCH_CREATES = ("bin/cloud_worker.py", GATE, "        done = None\n")
OLD_SECOND_LAUNCH_REFUSED = ("bin/cloud_worker.py", GATE, '        raise LaunchError("a cloud worker named %s already exists (retire %s ends it)" % (name, name))\n')
MUTANT_RETIRE_LEAVES_FAILED_POD = ("cloud/launcher/cloud_worker.py", 'if phase in ("Failed", "Succeeded"):   # whatever',
                                   'if phase in ("Failed", "Succeeded") and ann(pod, "kept") != "true":   # whatever')


class Probe(Launch):
    def test_probe_second_launch(self):   # run only in a scratch copy: prints what the second launch did
        self.assertEqual(self.launch()[0], 0)
        code, out, err = self.launch()
        print("PROBE code=%d already_exists=%d creates=%d" % (code, "already exists" in err, len(self.creates())))


class TestMutants(unittest.TestCase):
    def test_s2_the_old_second_launch_exits_1_with_already_exists(self):
        applied, status, out = run_child(self, Path(__file__).name, "Probe.test_probe_second_launch", [OLD_SECOND_LAUNCH_REFUSED])
        shown = "PROBE code=1 already_exists=1 creates=1" in out
        print("S2: second launch exit 1 text already exists %d (the code before the change, scratch copy; mutation applied %d)" % (shown, applied))
        self.assertEqual(applied, 1)
        self.assertTrue(shown, out[-800:])

    def test_s10_the_two_mutants_fail_their_scenario_tests(self):
        results = []
        for test_file, selector, mutation in (
                (Path(__file__).name, "TestExists.test_s3_second_launch_returns_the_existing_worker_and_creates_nothing", MUTANT_SECOND_LAUNCH_CREATES),
                ("test_stop_failed_pod.py", "TestRetireFailedPod.test_s9_retires_as_stated", MUTANT_RETIRE_LEAVES_FAILED_POD)):
            applied, status, out = run_child(self, test_file, selector, [mutation])
            results.append((applied, status == 1 and "FAILED" in out))
        print("S10: %d of 2 mutants fail, %d of 2 mutations applied" % (sum(1 for r in results if r[1]), sum(r[0] for r in results)))
        self.assertEqual(results, [(1, True), (1, True)])


class TestDocs(unittest.TestCase):
    def test_s11_the_documents_say_the_three_things(self):
        texts = [(ROOT / "docs" / "cloud-worker-interface.md").read_text().lower(), (ROOT / "cloud" / "launcher" / "README.md").read_text().lower()]
        sentences = ("a second launch of an existing worker returns it", "the no-wait launch is finished by launching the same name again",
                     "retire` deletes a failed pod")
        found = [s for s in sentences if all(s in t for t in texts)]
        print("S11: %d of 3 sentences found" % len(found))
        self.assertEqual(len(found), 3)


if __name__ == "__main__":
    unittest.main()
