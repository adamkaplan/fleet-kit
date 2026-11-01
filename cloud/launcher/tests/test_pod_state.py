"""Ask #1768: the pod's state and last events reach the run record (`pod_state` of run.json) before the pod is deleted.
Offline: a fake cluster object (no kubectl); every file is written in a temporary directory. Prints S lines (counts and names only)."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("CW_PROBE_LAUNCHER") or os.path.dirname(HERE))   # a mutant's scratch copy, when probed
import cloud_worker as cw  # noqa: E402
import run_record  # noqa: E402


def say(line):
    print(line, file=sys.stderr)


class Mask(unittest.TestCase):
    def test_the_address_mask(self):
        cases = {"dial 10.1.2.3 failed": "dial [address] failed", "to 10.1.2.3:443 refused": "to [address] refused",
                 "get registry.example.org:5000 timeout": "get [address] timeout",
                 "node aks-pool-0001 not ready": "node aks-pool-0001 not ready", "at 10:30 it ended": "at 10:30 it ended"}
        bad = [k for k, v in cases.items() if cw.mask_addresses(k) != v]
        say("S5 mask: %d of %d cases masked as asked" % (len(cases) - len(bad), len(cases)))
        self.assertEqual(bad, [])


class Writer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)
        with open(os.path.join(self.tmp.name, "run.json"), "w") as f:
            json.dump({"worker": "w1", "exit_reason": "stop"}, f)

    def read(self):
        with open(os.path.join(self.tmp.name, "run.json")) as f:
            return json.load(f)

    def test_the_field_is_added_and_the_earlier_fields_stay(self):
        run_record.Recorder.add_pod_state(self.tmp.name, {"phase": "Failed", "events": [], "events_read": "yes"})
        got = self.read()
        say("S2 writer: fields %s" % ",".join(sorted(got)))
        self.assertEqual(got["worker"], "w1")
        self.assertEqual(got["pod_state"]["phase"], "Failed")

    def test_a_state_that_cannot_be_written_is_the_one_word(self):
        run_record.Recorder.add_pod_state(self.tmp.name, {"bad": object()})
        self.assertEqual(self.read()["pod_state"], "unreadable")
        say("S4 writer: unwritable state gives 1 word")

    def test_a_missing_record_never_raises(self):
        run_record.Recorder.add_pod_state(os.path.join(self.tmp.name, "absent"), {"phase": "Failed"})
        say("S4 writer: a missing record raised 0 times")


SECRET = "s3cr3t-value-of-the-pod-0123456789"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PLANTED = [TOKEN, SECRET, "10.20.30.40", "registry.internal.example:5000"]


class FakeKube:
    """A fake cluster: one pod, its events; a log of every call in order."""
    def __init__(self, phase="Failed", events=(), refuse=None):
        self.log, self.known, self.refuse, self.deleted = [], {SECRET}, refuse, False
        self.events = list(events)
        self.pod = {"metadata": {"name": "cw-w1", "uid": "u1", "annotations": {"cw/started": "2026-11-01T08:00:00Z", "cw/run-id": "r1"}},
                    "spec": {"nodeName": "aks-pool-0007"},
                    "status": {"phase": phase, "containerStatuses": [{"name": "worker", "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]
                               if phase == "Failed" else [{"name": "worker", "state": {"running": {}}}]}}

    def scrub(self, text, limit=300):
        return cw.scrub(text, self.known, limit)

    def get_pod(self, name):
        self.log.append("get_pod")
        return None if self.deleted else self.pod

    def pod_events(self, name):
        self.log.append("pod_events")
        if self.deleted:   # the cluster's events of a deleted pod are no longer read
            return []
        if self.refuse:
            raise self.refuse
        return list(self.events)

    def exec(self, *a, **k):
        self.log.append("exec")
        return json.dumps({"pushed": False, "wip_branch": None, "skipped": [], "note": "nothing"})

    def annotate(self, *a, **k):
        pass

    def get_secret(self, name):
        return None

    delete_secret = delete_run_secret = get_secret

    def delete(self, name, uid=None):
        self.log.append("delete")
        self.deleted = True


def ev(i, message="m", when=None):
    return {"type": "Warning", "reason": "R%d" % i, "message": message, "lastTimestamp": when or "2026-11-01T09:%02d:00Z" % i}


class Stop(unittest.TestCase):
    def stop(self, kube, cause=None, record=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"CW_CLOUD_AUDIT": os.path.join(tmp.name, "audit.jsonl")}
        lau = cw.Launcher(kube, env, clock=lambda: cw.parse_iso("2026-11-01T09:10:00Z"))
        lau.recorder.read_messages = lambda pod: (None, "no transcript in the fake")
        lau.recorder.head_sha = lambda pod: None
        rec = os.path.join(tmp.name, "rec")
        import argparse
        a = argparse.Namespace(worker="w1", reason="finished", run_id=None, record_dir=rec, cause=cause)
        self.launcher = lau
        res = lau.stop(a)
        runs = os.listdir(rec)
        with open(os.path.join(rec, runs[0], "run.json")) as f:
            return res, json.load(f), f"{rec}/{runs[0]}/run.json"

    def test_a_failed_pod_reaches_the_record_by_retire_and_by_the_reaper(self):
        ok = 0
        for cause in (None, "pod_failed"):
            kube = FakeKube(events=[ev(1), ev(2), ev(3)])
            _, rec, _ = self.stop(kube, cause)
            ps = rec["pod_state"]
            c = ps["containers"][0]
            self.assertEqual((ps["phase"], c["reason"], c["exit_code"], ps["node"], len(ps["events"]), ps["events_read"]),
                             ("Failed", "OOMKilled", 137, "aks-pool-0007", 3, "yes"))
            self.assertLess(kube.log.index("pod_events"), kube.log.index("delete"))
            self.assertEqual(kube.log.count("get_pod"), 1)
            self.assertEqual(rec["end_hook"]["note"], "pod was Failed; end hook not run")
            ok += 1
        say("S2 stop: %d of 2 stops hold phase, reason, exit code, node, 3 of 3 events, events read before the delete" % ok)

    def test_at_most_20_newest_events_each_cut_to_200(self):
        kube = FakeKube(events=[ev(i, "back off " * 56) for i in range(30)])
        ps = self.stop(kube)[1]["pod_state"]
        names = [e.split(":")[0] for e in ps["events"]]
        self.assertEqual(len(ps["events"]), 20)
        self.assertTrue(all(len(e) <= 200 for e in ps["events"]))
        self.assertEqual(names, ["Warning R%d" % i for i in range(10, 30)])
        say("S3 stop: %d events, longest %d characters, oldest 10 absent" % (len(ps["events"]), max(len(e) for e in ps["events"])))

    def test_an_unreadable_cluster_is_one_word_and_the_pod_is_deleted(self):
        n = 0
        for err in (cw.CloudError("Forbidden: events is forbidden"), TimeoutError("timed out")):
            kube = FakeKube(refuse=err)
            res, rec, _ = self.stop(kube)
            ps = rec["pod_state"]
            self.assertEqual((ps["events_read"], ps["events"], ps["phase"], ps["node"]), ("unreadable", [], "Failed", "aks-pool-0007"))
            self.assertIn("delete", kube.log)
            self.assertEqual(rec["end_hook"]["note"], "pod was Failed; end hook not run")
            self.assertEqual(res["note"], "pod was Failed; end hook not run")
            n += 1
        say("S4 stop: %d of 2 refused reads give unreadable with 0 events and the pod deleted" % n)

    def test_no_planted_value_reaches_the_file(self):
        msg = "pull %s failed, token %s, secret %s, dial 10.20.30.40:443 and registry.internal.example:5000" % ("img", TOKEN, SECRET)
        kube = FakeKube(events=[ev(1, msg), ev(2, "back-off from registry.internal.example:5000")])
        _, rec, path = self.stop(kube)
        with open(path) as f:
            text = f.read()
        found = [i for i, v in enumerate(PLANTED) if v in text]
        self.assertEqual(found, [])
        self.assertIn("[address]", text)
        self.assertIn("[redacted]", text)
        say("S5 stop: %d of 4 planted values in run.json, %d address marks" % (len(found), text.count("[address]")))

    def test_a_running_pod_reads_no_events(self):
        kube = FakeKube(phase="Running", events=[ev(1)])
        res, rec, _ = self.stop(kube)
        ps = rec["pod_state"]
        self.assertEqual((ps["phase"], ps["events_read"], ps["events"], ps["node"]), ("Running", "no", [], "aks-pool-0007"))
        self.assertEqual(ps["containers"][0]["state"], "running")
        self.assertEqual((kube.log.count("pod_events"), kube.log.count("get_pod")), (0, 1))
        other = ("worker", "repository", "start", "end", "exit_reason", "head_sha", "transcript", "end_hook")
        self.assertEqual([k for k in other if k not in rec], [])
        say("S6 stop: phase Running, events_read no, %d reads of events, %d read of the pod, %d of 8 other fields" % (
            kube.log.count("pod_events"), kube.log.count("get_pod"), len(other)))

    def test_a_pod_that_is_gone_writes_no_pod_state(self):
        kube = FakeKube()
        kube.get_pod = lambda name: None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import argparse
        lau = cw.Launcher(kube, {"CW_CLOUD_AUDIT": os.path.join(tmp.name, "a.jsonl")}, clock=lambda: cw.parse_iso("2026-11-01T09:10:00Z"))
        res = lau.stop(argparse.Namespace(worker="w1", reason="x", run_id=None, record_dir=os.path.join(tmp.name, "r"), cause=None))
        self.assertTrue(res["pod_gone"])
        self.assertFalse(os.path.exists(os.path.join(tmp.name, "r")))
        say("U1 stop: a pod already gone writes 0 records")


def probe():
    """Every behaviour the plan names, checked against the launcher importable now; returns the list of what is wrong (empty: as asked)."""
    bad = []
    t = Stop("test_at_most_20_newest_events_each_cut_to_200")

    def stopped(kube, cause=None):
        try:
            return t.stop(kube, cause)
        except Exception as e:
            bad.append("the stop raised %s" % type(e).__name__)
            return None
    try:
        kube = FakeKube(events=[ev(1, "a " + TOKEN), ev(2, "b " + SECRET), ev(3, "c 10.20.30.40 registry.internal.example:5000")])
        got = stopped(kube)
        if got:
            ps = got[1].get("pod_state") if isinstance(got[1].get("pod_state"), dict) else {}
            if "pod_events" not in kube.log or "delete" not in kube.log or kube.log.index("pod_events") > kube.log.index("delete") or len(ps.get("events", [])) != 3:
                bad.append("the pod state was read after the delete")
            if (ps.get("containers") or [{}])[0].get("reason") != "OOMKilled" or ps.get("node") != "aks-pool-0007":
                bad.append("the reason or the node is missing")
            text = open(got[2]).read()
            if any(v in text for v in PLANTED):
                bad.append("a planted value reached the file")
        kube = FakeKube(events=[ev(i, "back off " * 56) for i in range(30)])
        got = stopped(kube)
        if got:
            events = (got[1].get("pod_state") or {}).get("events", [])
            if len(events) != 20 or any(len(e) > 200 for e in events):
                bad.append("the events are not 20 of at most 200 characters")
        kube = FakeKube(phase="Running", events=[ev(1)])
        if stopped(kube) and kube.log.count("pod_events"):
            bad.append("the events of a running pod were read")
        kube = FakeKube(refuse=cw.CloudError("Forbidden"))
        got = stopped(kube)
        ps = got[1].get("pod_state") if got else None
        if got and (not isinstance(ps, dict) or ps.get("events_read") != "unreadable" or ps.get("phase") != "Failed"):
            bad.append("an unreadable cluster lost the pod state")
        if got and "delete" not in kube.log:
            bad.append("the pod was not deleted")
    finally:
        t.doCleanups()
    return bad


LAUNCHER_DIR = os.path.dirname(HERE)
CALL = '                self._record_pod_state(info["record"], pod, name)\n'
AFTER_DELETE = '                self._remove(name, nonce, uid)\n                print("cloud-worker: pod was %s; end hook not run; deleted" % phase, file=sys.stderr)\n'
MUTANTS = [   # (name, [(old text, new text)], the anchored failure text)
    ("read after the delete", [(CALL, "                pass\n"), (AFTER_DELETE, AFTER_DELETE + CALL)], "the pod state was read after the delete"),
    ("events of a running pod", [('        if phase == "Running":\n            return state\n', '        if False:\n            return state\n')], "the events of a running pod were read"),
    ("all events kept", [("items[-POD_EVENTS_MAX:]", "items")], "the events are not 20 of at most 200 characters"),
    ("events uncut", [("self.kube.scrub(text, 1000))[:POD_EVENT_CHARS]", "self.kube.scrub(text, 1000))")], "the events are not 20 of at most 200 characters"),
    ("a failed read raises", [("        except Exception:   # a Forbidden answer", "        except KeyError:   # a Forbidden answer")], "an unreadable cluster lost the pod state"),
    ("events copied as given", [("mask_addresses(self.kube.scrub(text, 1000))", "text")], "a planted value reached the file"),
]


class Mutants(unittest.TestCase):
    """A scratch copy of the launcher per mutant (never the file itself); the control (the unmutated copy) runs FIRST and passes."""
    def child(self, directory):
        import subprocess
        env = dict(os.environ, CW_PROBE_LAUNCHER=directory, PYTHONPATH=os.path.join(os.path.dirname(LAUNCHER_DIR), "..", "bin"))   # path_walk lives in bin/
        r = subprocess.run([sys.executable, "-B", os.path.join(HERE, "test_pod_state.py"), "--probe"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        return [x for x in r.stdout.splitlines() if x]

    def scratch(self, tmp, name):
        import shutil
        d = os.path.join(tmp, name)
        os.mkdir(d)
        for f in os.listdir(LAUNCHER_DIR):
            if f.endswith(".py"):
                shutil.copy(os.path.join(LAUNCHER_DIR, f), d)
        return d

    def test_the_control_passes_first_and_each_mutant_fails_with_its_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = self.child(self.scratch(tmp, "control"))
            self.assertEqual(control, [])
            say("S7 control: passes with 0 problems")
            caught = 0
            for i, (name, edits, anchor) in enumerate(MUTANTS):
                d = self.scratch(tmp, "m%d" % i)
                path = os.path.join(d, "cloud_worker.py")
                with open(path) as f:
                    src = f.read()
                for old, new in edits:
                    self.assertEqual(src.count(old), 1, "%s: the mutation text must occur once" % name)
                    src = src.replace(old, new)
                with open(path, "w") as f:
                    f.write(src)
                self.assertNotEqual(src, open(os.path.join(LAUNCHER_DIR, "cloud_worker.py")).read(), "%s: mutation not applied" % name)
                got = self.child(d)
                self.assertIn(anchor, got, "%s: not caught, problems %r" % (name, got))
                caught += 1
                say("S7 mutant %r: applied once, failed with %r" % (name, anchor))
            self.assertEqual(caught, len(MUTANTS))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":   # the mutants' child process: prints the problems, one per line
        print("\n".join(probe()))
        sys.exit(0)
    unittest.main()
