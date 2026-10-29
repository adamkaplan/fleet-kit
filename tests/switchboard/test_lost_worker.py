"""Tests of the lost-worker detector (ask #1481, bin/lost_worker.py): one case per loss kind, a clean end, a repeat, a duplicate, an unsendable message,
and two mutants (alerts on every worker, alerts on none) that the same cases must catch. Fake records only; nothing is read from a real state directory."""
import datetime
import importlib.util
import json
import os
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0


def load():
    spec = importlib.util.spec_from_file_location("lost_worker_under_test", str(REPO / "bin" / "lost_worker.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


class Audit:
    def __init__(self, path):
        self.path = Path(path)

    def append(self, actor, event, subject=None, detail=None, **extra):
        with open(self.path, "a") as handle:
            handle.write(json.dumps({"ts": stamp(NOW), "actor": actor, "event": event, "subject": subject, "detail": detail}) + "\n")


class Engine:
    actor = "switchboard"

    def __init__(self, directory, fail=False):
        self.audit = Audit(Path(directory) / "audit.jsonl")
        self.audit.path.touch()
        self.sent, self.fail, self.cloud_watch = [], fail, {"records": []}

    def send(self, sender, recipient, text, **kw):
        if self.fail:
            raise OSError("no channel")
        self.sent.append((recipient, text))
        return {"sent": True}


def line(path, **item):
    with open(path, "a") as handle:
        handle.write(json.dumps(item) + "\n")


class Rig:
    def __init__(self, mod, fail=False):
        self.mod, self.dir = mod, tempfile.mkdtemp()
        self.engine = Engine(self.dir, fail)
        self.launcher = os.path.join(self.dir, "launcher.jsonl")
        open(self.launcher, "a").close()
        self.sb = types.SimpleNamespace(parse_iso=parse, parse_bounded=json.loads, _one_line=lambda t, n=200: str(t)[:n], retired=lambda e, n: False)
        self.alive = {}
        self.remember()

    def remember(self):   # a valid memory with no offsets: the cases below are not first passes
        with open(os.path.join(self.dir, "lost-workers.json"), "w") as handle:
            json.dump({"offsets": {}, "alerted": {}}, handle)

    def created(self, name, deadline=NOW - 60, lane="lane-a", branch="ask-7"):
        line(self.engine.audit.path, ts=stamp(NOW - 7200), actor="x", event="cloud.created", subject=name, detail={"hard_deadline": stamp(deadline)})
        self.engine.cloud_watch["records"].append({"worker": name, "reports_to": lane, "branch": branch})

    def stopped(self, name, reason="idle"):
        line(self.launcher, ts=stamp(NOW - 30), worker=name, runtime_id="pod/" + name, action="stop", ok=True, reason=reason)

    def passes(self, n=1, at=NOW):
        out = []
        for _ in range(n):
            out += self.mod.run_pass(self.sb, self.engine, at, self.alive, self.launcher)
        return out


def scenarios(mod):
    """Every case against `mod`; the names of the cases that failed (a mutant must fail at least one)."""
    failed = []

    def check(name, ok):
        if not ok:
            failed.append(name)

    r = Rig(mod)   # S2: silent pod: two messages (lane and chief of staff), five fields
    r.created("w1")
    got = r.passes()
    check("S2 count", len(got) == 1 and sorted(x[0] for x in r.engine.sent) == ["chief-of-staff", "lane-a"])
    text = r.engine.sent[0][1] if r.engine.sent else ""
    check("S2 fields", all(f in text for f in ("w1", "ask-7", "pod_no_stop", stamp(NOW - 7200)[:19], "Evidence")))

    r = Rig(mod)   # a pod still before its deadline is not lost
    r.created("w1", deadline=NOW + 600)
    check("before deadline", r.passes() == [])

    r = Rig(mod)   # S3: the other kinds
    r.engine.audit.append("x", "cloud.create_failed", "w2", {"reason": "quota"})
    line(r.launcher, ts=stamp(NOW - 5), worker="", runtime_id="reaper", action="refresh-cluster-token", ok=False, reason="login failed")
    r.alive["loc"] = {"lane": "boss", "ask": "ask-9"}
    first = r.passes()
    r.alive.clear()
    got = first + r.passes(at=NOW + 10)
    check("S3 kinds", sorted(x["kind"] for x in got) == ["local_worker", "reaper_missed", "refused_launch"])
    check("S3 named", all(("kind=" + x["kind"]) in x["text"] for x in got))

    r = Rig(mod)   # S4: six passes, one alert for the loss
    r.created("w1")
    r.passes(6)
    check("S4 duplicate", len([m for m in r.engine.sent if m[0] == "lane-a"]) == 1)
    r.engine.sent.clear()
    r.passes()
    check("S4 again", r.engine.sent == [])

    r = Rig(mod)   # S5: a stop of reason done and a final report: no alert
    r.created("w1")
    r.stopped("w1", "done")
    r.created("w2")
    line(r.launcher, ts=stamp(NOW - 40), worker="w2", runtime_id="pod/w2", action="report", ok=True, reason="")
    check("S5 clean", r.passes() == [] and r.engine.sent == [])

    r = Rig(mod)   # S5b: work already done (final text says so), pod ends at the hard limit with pushed false: a clean end, not a loss
    r.created("w3")
    line(r.launcher, ts=stamp(NOW - 40), worker="w3", runtime_id="pod/w3", action="report", ok=True, reason="work done: PRs already open, nothing new to push")
    line(r.launcher, ts=stamp(NOW - 30), worker="w3", runtime_id="pod/w3", action="stop", ok=True, reason="hard limit", pushed=False)
    check("S5b done then hard limit", r.passes() == [] and r.engine.sent == [])
    r = Rig(mod)   # pushed false alone proves nothing: a pod with that and neither a stop nor a report is still lost
    r.created("w4")
    line(r.launcher, ts=stamp(NOW - 30), worker="w4", runtime_id="pod/w4", action="send", ok=True, reason="pushed=false")
    check("S5c pushed false alone is not a clean end", len(r.passes()) == 1)

    r = Rig(mod)   # S6: the third launch of an ask
    for n in ("a1", "a2", "a3"):
        r.created(n)
    texts = {x["name"]: x["text"] for x in r.passes()}
    check("S6 repeat", "repeat" not in texts.get("a1", "repeat") and texts.get("a3", "").count("repeat") == 1 and "repeat: 2 earlier" in texts.get("a3", ""))

    r = Rig(mod, fail=True)   # U1: a send that fails is audited, the pass finishes
    r.created("w1")
    try:
        r.passes(3)
        ok = True
    except Exception:
        ok = False
    with open(r.engine.audit.path) as handle:
        said = [x for x in handle if "lost_worker.alert_not_sent" in x]
    check("U1 pass", ok and len(said) == 2)   # once per recipient, never again on later passes
    return failed


class TestLostWorker(unittest.TestCase):
    def test_every_case_passes_on_the_real_detector(self):
        self.assertEqual(scenarios(load()), [])

    def test_the_four_kinds_are_named(self):
        self.assertEqual(load().KINDS, ("pod_no_stop", "local_worker", "refused_launch", "reaper_missed"))

    def test_mutant_that_alerts_on_every_worker_is_caught(self):
        mod = load()
        real = mod._losses

        def every(state, sb, engine, discovery, now, records):
            out = real(state, sb, engine, discovery, now, records)
            for name, c in state.get("created", {}).items():
                out.append({"key": "x:%s" % name, "kind": "pod_no_stop", "name": name, "ask": "a", "at": c["at"], "lane": None, "line": "all"})
            return out
        mod._losses = every
        self.assertTrue(scenarios(mod))

    def test_mutant_that_alerts_on_none_is_caught(self):
        mod = load()
        mod._losses = lambda *a, **k: []
        self.assertTrue(scenarios(mod))

    def test_the_documentation_names_the_alert_and_the_kinds(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        section = text[text.index("## A lost worker is announced"):].split("\n## ", 1)[0]
        self.assertIn("[lost worker]", section)
        for kind in load().KINDS:
            self.assertIn("`%s`" % kind, section)


class TestLostWorkerReadRound(unittest.TestCase):
    def rig(self, memory=True):
        r = Rig(load())
        if not memory:
            os.unlink(os.path.join(r.dir, "lost-workers.json"))
        return r

    def alerts(self, r):
        return [m for m in r.engine.sent if m[0] == "lane-a"]

    def test_s9_cap_and_backlog(self):
        r = self.rig()
        for n in range(25):
            r.created("w%02d" % n)
        counts = []
        for _ in range(3):
            r.engine.sent.clear()
            got = r.passes()
            rolls = [m[1] for m in r.engine.sent if "more lost worker" in m[1]]
            counts.append((len(got), [x.split(" more")[0].split()[-1] for x in rolls]))
        self.assertEqual(counts, [(10, ["15"]), (10, ["5"]), (5, [])])
        r.engine.sent.clear()
        self.assertEqual(r.passes(), [])
        self.assertEqual(r.engine.sent, [])

    def test_s10_first_pass_alerts_only_recent(self):
        r = self.rig(memory=False)
        for n in range(40):
            r.created("old%d" % n)
        for n in range(2):
            line(r.engine.audit.path, ts=stamp(NOW - 300), actor="x", event="cloud.created", subject="new%d" % n, detail={"hard_deadline": stamp(NOW - 60)})
        self.assertEqual(sorted(x["name"] for x in r.passes()), ["new0", "new1"])
        self.assertEqual(r.passes(), [])

    def test_s10b_valid_memory_alerts_across_a_restart_gap(self):
        r = self.rig()
        r.passes()
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="gap", detail={"hard_deadline": stamp(NOW - 60)})
        self.assertEqual([x["name"] for x in r.passes()], ["gap"])

    def test_mutant_that_applies_the_one_hour_rule_with_a_valid_memory_is_caught(self):
        mod = load()
        real = mod._load
        mod._load = lambda sb, path: (real(sb, path)[0], "none")
        r = Rig(mod)
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="gap", detail={"hard_deadline": stamp(NOW - 60)})
        self.assertEqual(r.passes(), [])   # the mutant alerts nothing: the s10b case above fails on it

    def test_s11_name_cleaned_and_corrupt_memory(self):
        r = self.rig()
        name = "bad\x00\x07" + "n" * 100000
        r.created(name)
        r.passes()
        text = r.engine.sent[0][1]
        self.assertFalse(any(ord(c) < 32 for c in text))
        self.assertLessEqual(text.split(" (ask")[0].count("n"), 80)
        r = self.rig()
        with open(os.path.join(r.dir, "lost-workers.json"), "w") as handle:
            handle.write("{not json")
        r.created("w1")
        self.assertEqual(r.passes(), [])
        self.assertEqual(r.engine.sent, [])
        with open(r.engine.audit.path) as handle:
            self.assertEqual(len([x for x in handle if "lost_worker.memory_corrupt" in x]), 1)
        line(r.engine.audit.path, ts=stamp(NOW - 100), actor="x", event="cloud.created", subject="w2", detail={"hard_deadline": stamp(NOW - 60)})
        self.assertEqual([x["name"] for x in r.passes()], ["w2"])
        self.assertTrue(json.load(open(os.path.join(r.dir, "lost-workers.json")))["offsets"])

    def test_memory_file_is_private(self):
        r = self.rig()
        r.created("w1")
        r.passes()
        self.assertEqual(os.stat(os.path.join(r.dir, "lost-workers.json")).st_mode & 0o777, 0o600)

    def test_local_worker_is_possibly_lost(self):
        r = self.rig()
        r.alive["loc"] = {"lane": "boss", "ask": "ask-9"}
        r.passes()
        r.alive.clear()
        self.assertIn("possibly lost", r.passes(at=NOW + 10)[0]["text"])


if __name__ == "__main__":
    unittest.main()
