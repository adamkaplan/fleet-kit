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

    def created(self, name, deadline=NOW - 400, lane="lane-a", branch="ask-7"):
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
    line(r.launcher, ts=stamp(NOW - 3000), worker="w4", runtime_id="pod/w4", action="send", ok=True, reason="pushed=false")
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

    def test_the_recency_rule_and_its_state_are_documented_and_present(self):
        import ast
        tree = ast.parse((REPO / "bin" / "lost_worker.py").read_text())
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        text = (REPO / "docs" / "switchboard.md").read_text()
        section = text[text.index("## A lost worker is announced"):].split("\n## ", 1)[0]
        for name in ("reaper_outage", "reaper_gap", "catchup", "reaper_new"):
            self.assertIn(name, strings)
        for name in ("reaper_outage", "reaper_gap", "catchup", "RECENT_SECONDS", "READ_BYTES"):
            self.assertIn("`%s`" % name, section)


class TestLostWorkerReadRound(unittest.TestCase):
    def rig(self, memory=True):
        r = Rig(load())
        if not memory:
            os.unlink(os.path.join(r.dir, "lost-workers.json"))
        return r

    def alerts(self, r):
        return [m for m in r.engine.sent if m[0] == "lane-a"]

    def run_full_tables(self):
        mod = load()
        mod.SEEN_MAX = 5
        r = Rig(mod)
        path = r.engine.audit.path
        r.created("lost", deadline=NOW + 600)   # created with no stop, oldest creation, due only at the second pass
        line(path, ts=stamp(NOW - 7000), actor="x", event="cloud.created", subject="old", detail={"hard_deadline": stamp(NOW - 400)})
        line(r.launcher, ts=stamp(NOW - 5000), worker="old", runtime_id="pod/old", action="stop", ok=True, reason="finished")
        for n in range(3):
            r.created("f%d" % n)
        first = r.passes()   # five creations: the cap is not passed yet
        for n in range(3):
            r.stopped("f%d" % n)
        for n in range(3):   # later stops pass the cap
            line(r.launcher, ts=stamp(NOW - 20), worker="x%d" % n, runtime_id="pod/x%d" % n, action="stop", ok=True, reason="finished")
        return [x for x in first + r.passes(at=NOW + 1000) if x["kind"] == "pod_no_stop"]

    def test_2164_s2_a_stopped_worker_is_never_alerted_after_the_trim(self):
        self.assertEqual([x for x in self.run_full_tables() if x["name"] == "old"], [])

    def test_2164_s3_a_worker_with_no_stop_is_still_alerted(self):
        self.assertEqual(len([x for x in self.run_full_tables() if x["name"] == "lost"]), 1)

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
            line(r.engine.audit.path, ts=stamp(NOW - 300), actor="x", event="cloud.created", subject="new%d" % n, detail={"hard_deadline": stamp(NOW - 400)})
        self.assertEqual(sorted(x["name"] for x in r.passes()), ["new0", "new1"])
        self.assertEqual(r.passes(), [])

    def test_s10b_valid_memory_alerts_across_a_restart_gap(self):
        r = self.rig()
        r.passes()
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="gap", detail={"hard_deadline": stamp(NOW - 400)})
        self.assertEqual([x["name"] for x in r.passes()], ["gap"])

    def test_mutant_that_applies_the_one_hour_rule_with_a_valid_memory_is_caught(self):
        mod = load()
        real = mod._load
        mod._load = lambda sb, path: (real(sb, path)[0], "none")
        r = Rig(mod)
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="gap", detail={"hard_deadline": stamp(NOW - 400)})
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
        line(r.engine.audit.path, ts=stamp(NOW - 100), actor="x", event="cloud.created", subject="w2", detail={"hard_deadline": stamp(NOW - 400)})
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

    def pad(self, path, size, key="launcher"):
        """Filler lines (no loss in them) until the file grows by at least `size` bytes."""
        written = 0
        with open(path, "a") as handle:
            while written < size:
                if key == "launcher":
                    text = json.dumps({"ts": stamp(NOW - 50000), "worker": "fill", "runtime_id": "pod/fill", "action": "send", "ok": True, "reason": "x" * 200}) + "\n"
                else:
                    text = json.dumps({"ts": stamp(NOW - 50000), "actor": "x", "event": "x.pad", "subject": "pad", "detail": {"n": "x" * 200}}) + "\n"
                handle.write(text)
                written += len(text)

    def failed(self, r, ago, reason="login failed"):
        line(r.launcher, ts=stamp(NOW - ago), worker="", runtime_id="reaper", action="refresh-cluster-token", ok=False, reason=reason)

    def reaper_texts(self, r):
        return [m[1] for m in r.engine.sent if "kind=reaper_missed" in m[1] or m[1].startswith("[lost worker] reaper:")]

    def big_old_audit(self, r):
        """A launcher audit of about 3 MiB: 8 old failed reaper lines in three chunks, then one young failed line."""
        for n in range(3):
            self.failed(r, 5 * 3600 + n * 100, "old %d" % n)
        self.pad(r.launcher, 1 << 20)
        for n in range(3, 6):
            self.failed(r, 4 * 3600 + n * 100, "old %d" % n)
        self.pad(r.launcher, 1 << 20)
        for n in range(6, 8):
            self.failed(r, 2 * 3600 + n * 100, "old %d" % n)
        self.pad(r.launcher, 1 << 20)
        self.failed(r, 60, "young")

    def test_s2_old_failed_reaper_lines_are_never_alerted_on_any_pass(self):
        r = self.rig(memory=False)
        self.big_old_audit(r)
        self.assertGreater(os.stat(r.launcher).st_size, 3 * self.mod_read_bytes())
        got = []
        for _ in range(4):
            got += r.passes()
        texts = self.reaper_texts(r)
        self.assertEqual(len(got), 2)
        self.assertEqual(len([t for t in texts if t.startswith("[lost worker] reaper:")]), 1)
        self.assertIn("8 failed reaper line(s)", [t for t in texts if t.startswith("[lost worker] reaper:")][0])
        own = [t for t in texts if "kind=reaper_missed" in t]
        self.assertEqual(len(own), 1)
        self.assertIn("latest reason young", own[0])
        self.assertNotIn("old", own[0].replace("[lost worker]", ""))

    def mod_read_bytes(self):
        return load().READ_BYTES

    def test_s3_one_outage_one_message_until_the_reaper_passes_again(self):
        r = self.rig()
        for n in range(5):
            self.failed(r, 500 - n * 100, "login failed %d" % n)
        got = r.passes()
        self.assertEqual(len(got), 1)
        text = got[0]["text"]
        self.assertIn("5 failed reaper line(s)", text)
        self.assertIn(stamp(NOW - 500)[:19], text)
        self.assertIn(stamp(NOW - 100)[:19], text)
        self.assertIn("latest reason login failed 4", text)
        self.assertEqual(len(r.engine.sent), 1)
        self.failed(r, 90)
        self.failed(r, 80)
        self.assertEqual(r.passes(), [])
        self.assertEqual(len(r.engine.sent), 1)
        line(r.launcher, ts=stamp(NOW - 70), worker="", runtime_id="reaper", action="refresh-cluster-token", ok=True, reason="")
        self.failed(r, 60, "again")
        got = r.passes()
        self.assertEqual(len(got), 1)
        self.assertIn("1 failed reaper line(s)", got[0]["text"])
        self.assertEqual(len(r.engine.sent), 2)

    def test_s4_catch_up_applies_the_recency_rule_to_every_kind(self):
        r = self.rig(memory=False)
        self.pad(r.engine.audit.path, 2 << 20, "switchboard")
        self.pad(r.launcher, 1 << 20)
        r.alive["oldloc"] = {"lane": "boss", "ask": "ask-9"}
        got = r.passes(at=NOW - 3 * 3600)
        state_path = os.path.join(r.dir, "lost-workers.json")
        self.assertTrue(json.load(open(state_path))["catchup"])
        r.alive.clear()
        r.alive["newloc"] = {"lane": "boss", "ask": "ask-9"}
        got += r.passes(at=NOW - 300)
        r.alive.clear()
        r.created("oldpod", deadline=NOW - 3 * 3600)
        line(r.engine.audit.path, ts=stamp(NOW - 300), actor="x", event="cloud.created", subject="newpod", detail={"hard_deadline": stamp(NOW - 400)})
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.create_failed", subject="oldref", detail={})
        line(r.engine.audit.path, ts=stamp(NOW - 100), actor="x", event="cloud.create_failed", subject="newref", detail={})
        got += r.passes()
        self.assertEqual(sorted((x["kind"], x["name"]) for x in got),
                         [("local_worker", "newloc"), ("pod_no_stop", "newpod"), ("refused_launch", "newref")])
        self.assertFalse(json.load(open(state_path))["catchup"])
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="gap", detail={"hard_deadline": stamp(NOW - 400)})
        self.assertEqual([x["name"] for x in r.passes()], ["gap"])   # outside a catch-up a restart gap is still alerted

    def test_s6_a_detector_down_for_hours_says_once_how_many_it_missed(self):
        r = self.rig()
        for n in range(5):
            self.failed(r, 3 * 3600 - n * 60, "gap %d" % n)
        got = r.passes()
        self.assertEqual(len(got), 1)
        text = got[0]["text"]
        self.assertTrue(text.startswith("[lost worker] reaper: 5 failed reaper line(s) while the detector was down or before this pass"))
        self.assertIn("latest reason gap 4", text)
        self.assertIn(stamp(NOW - 3 * 3600)[:19], text)
        self.assertEqual(r.passes(), [])
        self.failed(r, 30, "recent")
        got = r.passes()
        self.assertEqual(len(got), 1)
        self.assertIn("kind=reaper_missed", got[0]["text"])
        self.assertEqual(len(r.engine.sent), 2)

    def test_u1_a_memory_written_by_the_old_code_is_never_re_alerted_from(self):
        r = self.rig()
        self.failed(r, 100)
        size = os.stat(r.launcher).st_size
        key = "refresh-cluster-token@" + stamp(NOW - 100)
        with open(os.path.join(r.dir, "lost-workers.json"), "w") as handle:
            json.dump({"offsets": {"launcher": size, "switchboard": 0}, "alerted": {"reaper_missed:" + key: {"ask": "-", "at": NOW}},
                       "reaper": {key: {"at": NOW - 100, "line": "x"}}}, handle)
        self.assertEqual(r.passes(2), [])
        self.assertEqual(r.engine.sent, [])

    def test_malformed_memory_is_dropped_and_alerting_goes_on(self):
        import ast
        module = load()
        outage = {"first": NOW - 200, "latest": NOW - 100, "count": 2, "reason": "r", "sent": False, "closed_by": None}
        gap = {"first": NOW - 9000, "latest": NOW - 8000, "count": 2, "reason": "r"}
        bad_numbers = ["x", True, None, float("nan"), -1, 5e12, [1]]
        table = [("reaper_outage", "wrong type", {"sent": False}), ("reaper_outage", "wrong type", "x"), ("reaper_outage", "wrong type", 5),
                 ("reaper_gap", "wrong type", {"count": 3}), ("reaper_gap", "wrong type", [1]),
                 ("reaper_new", "wrong type", 5), ("reaper_new", "wrong type", "x"), ("reaper_new", "wrong type", {"at": 1, "line": "x"}),
                 ("reaper_new", "wrong type", [{"at": "x", "line": 1}, 7, [1]])]
        for record, base, fields in (("reaper_outage", outage, ("first", "latest", "count", "reason", "sent", "closed_by")),
                                     ("reaper_gap", gap, ("first", "latest", "count", "reason"))):
            for field in fields:
                for bad in (bad_numbers if field not in ("sent",) else ["x", 1, None]) + ([10 ** 30, 1.5, 0] if field == "count" else []) + ([5, None] if field == "reason" else []):
                    table.append((record, field, dict(base, **{field: bad})))
                if field not in ("closed_by",):
                    table.append((record, field, {k: v for k, v in base.items() if k != field}))
        # completeness from the code: every field a stored record is built with must be corrupted by some row above
        tree = ast.parse((REPO / "bin" / "lost_worker.py").read_text())
        built = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in ("reaper_outage", "reaper_gap"):
                        built.setdefault(target.slice.value, set()).update(k.value for k in node.value.keys if isinstance(k, ast.Constant))
        for record, fields in built.items():
            covered = {f for r, f, shape in table if r == record and f != "wrong type"}
            self.assertEqual(fields, covered, record)
        old_memory = [("old", {"reaper": {"k@t": {"at": NOW - 100, "line": "x"}}, "alerted": {"reaper_missed:k@t": {"ask": "-", "at": NOW}}}),
                      ("old", {"alerted": {"reaper_missed:k@t": 5}})]
        cases = [(r, shape) for r, f, shape in table] + old_memory
        for record, shape in cases:
            r = self.rig()
            with open(os.path.join(r.dir, "lost-workers.json"), "w") as handle:
                json.dump({"offsets": {}, "alerted": {}, record if record != "old" else "x": shape} if record != "old" else dict({"offsets": {}, "alerted": {}}, **shape), handle)
            self.failed(r, 60, "recent")
            for _ in range(2):
                got = r.passes()   # a second pass: the memory written by the first must not wedge it either
                if record != "old":
                    self.assertTrue(got or self.reaper_texts(r), (record, shape))
            if record != "old":
                self.assertTrue(self.reaper_texts(r), (record, shape))
        r = self.rig()   # a recent failed pod alerts on the same pass as a malformed record
        with open(os.path.join(r.dir, "lost-workers.json"), "w") as handle:
            json.dump({"offsets": {}, "alerted": {}, "reaper_outage": {"sent": False}, "reaper_gap": {"count": "x"}, "reaper_new": 5}, handle)
        r.created("w1")
        self.assertEqual([x["name"] for x in r.passes()], ["w1"])

    BAD = [None, "x", 5, [1], {"a": 1}, True, 10 ** 30, {"a": {"b": [1]}}]
    VALID = {"reaper_ok": [5], "catchup": [True], "stop": [{"a": 1}], "report": [{"a": 1}], "active": [{"a": 1}], "gone": [{"a": 1}], "slow_bad": [5]}   # the one bad value of the table that is a good value of that key
    KEYS = ("offsets", "alerted", "created", "stop", "report", "active", "refused", "seen", "gone", "reaper", "reaper_ok", "reaper_outage", "reaper_gap",
            "reaper_new", "catchup", "slow", "slow_bad")

    def bad_table(self):
        return {key: [b for b in self.BAD if not any(type(b) is type(v) and b == v for v in self.VALID.get(key, []))] for key in self.KEYS}

    def keys_read(self):
        """Every constant key the module reads or writes on the loaded memory (`state` or `data`), from the code."""
        import ast
        found = set()
        for node in ast.walk(ast.parse((REPO / "bin" / "lost_worker.py").read_text())):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("state", "data") and isinstance(node.slice, ast.Constant):
                found.add(node.slice.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in ("state", "data")
                    and node.func.attr in ("get", "setdefault", "pop") and node.args and isinstance(node.args[0], ast.Constant)):
                found.add(node.args[0].value)
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) and any(isinstance(c, ast.Name) and c.id in ("state", "data") for c in node.comparators):
                found.add(node.left.value)
        return {k for k in found if isinstance(k, str)} - {"_gap_ready"}

    def check_every_key(self, module):
        """Raises when any bad value of any memory key makes a pass raise, is kept in the saved memory, or stops a recent failed reaper line alerting."""
        global load
        real, load = load, (lambda: module)
        try:
            table = self.bad_table()
            unlisted = self.keys_read() - set(table)
            self.assertEqual(unlisted, set(), "a memory key the code reads has no row in the bad-value table: %s" % sorted(unlisted))
            self.assertEqual(set(module._CLEANERS), set(table))
            for key, values in table.items():
                for bad in values:
                    probe = {key: json.loads(json.dumps(bad))}
                    module._sanitize(probe)   # the one place the loaded memory is checked: the bad value does not come out of it
                    self.assertNotEqual(probe.get(key, "<missing>"), bad, (key, bad))
                    r = self.rig()
                    with open(os.path.join(r.dir, "lost-workers.json"), "w") as handle:
                        json.dump({"offsets": {}, "alerted": {}, key: bad}, handle)
                    line(r.launcher, ts=stamp(NOW - 500), worker="", runtime_id="reaper", action="refresh-cluster-token", ok=True, reason="")
                    self.failed(r, 60, "recent")
                    r.created("w1", deadline=NOW + 600)
                    r.alive["loc"] = {"lane": "boss", "ask": "ask-9"}
                    for _ in range(2):   # the second pass reads what the first wrote
                        r.passes()
                    self.assertTrue(self.reaper_texts(r), (key, bad))
                    saved = json.load(open(os.path.join(r.dir, "lost-workers.json")))
                    self.assertNotEqual(saved.get(key, "<missing>"), bad, (key, bad))
        finally:
            load = real

    def test_every_memory_key_is_cleaned_in_one_place_and_the_table_is_complete_by_construction(self):
        self.check_every_key(load())

    def test_skipping_the_check_of_any_one_memory_key_is_caught(self):
        src = (REPO / "bin" / "lost_worker.py").read_text()
        for key in self.KEYS:
            old = '    "%s": ' % key
            start = src.index(old, src.index("_CLEANERS = {"))
            end = src.index(",\n", start)
            mutant = self.mutant(src[start:end + 2], old + "(lambda v: v),\n")
            with self.assertRaises(Exception, msg=key):
                self.check_every_key(mutant)

    def test_the_catch_up_flag_is_set_only_with_no_usable_memory(self):
        r = self.rig()   # a valid memory, an audit more than one chunk behind
        self.pad(r.engine.audit.path, 3 << 19, "switchboard")
        state_path = os.path.join(r.dir, "lost-workers.json")
        self.assertEqual(r.passes(), [])
        self.assertFalse(json.load(open(state_path))["catchup"])
        r.created("restart-gap")   # an old loss in the second chunk
        self.assertEqual([x["name"] for x in r.passes()], ["restart-gap"])
        self.assertFalse(json.load(open(state_path))["catchup"])

    def test_a_launcher_file_that_exists_but_cannot_be_read_is_not_the_end(self):
        for make in (lambda p: os.symlink(os.path.join(os.path.dirname(p), "nowhere"), p), os.mkdir):
            r = self.rig(memory=False)
            os.unlink(r.launcher)
            make(r.launcher)
            state_path = os.path.join(r.dir, "lost-workers.json")
            r.passes()
            self.assertTrue(json.load(open(state_path))["catchup"])
            if os.path.isdir(r.launcher):
                os.rmdir(r.launcher)
            else:
                os.unlink(r.launcher)
            open(r.launcher, "w").close()
            r.created("oldpod", deadline=NOW - 3 * 3600)
            self.assertEqual(r.passes(), [])   # read with the rule still on: the old loss is skipped
            self.assertFalse(json.load(open(state_path))["catchup"])
        r = self.rig(memory=False)   # a missing file is the end
        os.unlink(r.launcher)
        r.passes()
        self.assertFalse(json.load(open(os.path.join(r.dir, "lost-workers.json")))["catchup"])

    def mutant(self, old, new, extra=()):
        src = (REPO / "bin" / "lost_worker.py").read_text()
        for a, b in ((old, new),) + tuple(extra):
            self.assertEqual(src.count(a), 1, a)
            src = src.replace(a, b)
        path = os.path.join(tempfile.mkdtemp(), "lost_worker_mutant.py")
        with open(path, "w") as handle:
            handle.write(src)
        spec = importlib.util.spec_from_file_location("lost_worker_mutant", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def failing_tests(self, module):
        """The new tests that fail when they run against `module`."""
        failed = []
        for name in ("test_s2_old_failed_reaper_lines_are_never_alerted_on_any_pass", "test_s3_one_outage_one_message_until_the_reaper_passes_again",
                     "test_s6_a_detector_down_for_hours_says_once_how_many_it_missed", "test_s4_catch_up_applies_the_recency_rule_to_every_kind"):
            global load
            real = load
            load = lambda: module
            try:
                case = TestLostWorkerReadRound(name)
                getattr(case, name)()
            except AssertionError:
                failed.append(name)
            finally:
                load = real
        return failed

    def test_the_new_tests_pass_on_the_real_module_and_each_mutant_is_caught(self):
        self.assertEqual(self.failing_tests(load()), [])
        mutants = {
            "recency only on the first pass": self.mutant("    _reaper(state, now)\n", "    _reaper(state, now if first else 0)\n"),
            "one message per line": self.mutant('"key": "reaper_missed:outage@%s" % outage["first"]', '"key": "reaper_missed:outage@%s:%d" % (outage["first"], outage["count"])',
                                                (('            outage["count"] += 1\n', '            outage["count"] += 1\n            outage["sent"] = False\n'),)),
            "one message per old line": self.mutant('    if isinstance(gap, dict) and gap.get("count") and state.get("_gap_ready"):',
                                                    '    for _i in range(gap["count"] if isinstance(gap, dict) and gap.get("count") and state.get("_gap_ready") else 0):',
                                                    (('"key": "reaper_gap:%s:%s:%d" % (gap["first"], gap["latest"], gap["count"])',
                                                      '"key": "reaper_gap:%s:%s:%d:%d" % (gap["first"], gap["latest"], gap["count"], _i)'),)),
            "old lines dropped with no summary": self.mutant('state["_gap_ready"] = at_end', 'state["_gap_ready"] = False'),
        }
        for label, module in mutants.items():
            self.assertTrue(self.failing_tests(module), label)
        named = ("test_malformed_memory_is_dropped_and_alerting_goes_on", "test_the_catch_up_flag_is_set_only_with_no_usable_memory",
                 "test_a_launcher_file_that_exists_but_cannot_be_read_is_not_the_end")
        self.assertEqual(self.failing_named(load(), named), [])
        for name, module in (("validation removed", self.mutant("def _sanitize(state):\n", "def _sanitize(state):\n    return\n")),
                             ("flag set on every pass", self.mutant('(first or state.get("catchup") is True) and not at_end', "not at_end")),
                             ("unreadable file is the end", self.mutant("return False   # a file that exists", "return True   # a file that exists"))):
            self.assertTrue(self.failing_named(module, named), name)

    def failing_named(self, module, names):
        failed = []
        for name in names:
            global load
            real = load
            load = lambda: module
            try:
                getattr(TestLostWorkerReadRound(name), name)()
            except Exception:
                failed.append(name)
            finally:
                load = real
        return failed


class TestLostWorkerMemoryOnly(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.r = Rig(self.mod)
        os.unlink(os.path.join(self.r.dir, "lost-workers.json"))
        os.mkdir(os.path.join(self.r.dir, "lost-workers.json"))   # a directory at the memory path: neither written nor set aside
        for name, age in (("recent", 300), ("old", 3 * 3600)):
            line(self.r.engine.audit.path, ts=stamp(NOW - age), actor="x", event="cloud.created", subject=name, detail={"hard_deadline": stamp(NOW - 400)})

    def corrupt_lines(self):
        with open(self.r.engine.audit.path) as handle:
            return len([x for x in handle if "lost_worker.memory_corrupt" in x])

    def check(self, r):
        self.assertEqual([x["name"] for x in r.passes()], ["recent"])
        self.assertEqual(self.corrupt_lines(), 1)
        self.assertEqual(r.passes(), [])
        self.assertEqual(self.corrupt_lines(), 1)
        self.assertEqual(len([m for m in r.engine.sent if m[0] == "chief-of-staff"]), 1)

    def test_s11b_memory_only_alerts_once_and_audits_once(self):
        self.check(self.r)

    def test_mutant_that_audits_every_pass_and_alerts_nothing_is_caught(self):
        real = self.mod.run_pass

        def mutant(sb, engine, now, alive=None, launcher_audit=None):
            engine.audit.append(engine.actor, "lost_worker.memory_corrupt", None, {})
            return []
        self.mod.run_pass = mutant
        with self.assertRaises(AssertionError):
            self.check(self.r)
        self.mod.run_pass = real

    def test_failed_save_does_not_repeat_the_first_run_rule(self):
        r = Rig(self.mod)
        os.unlink(os.path.join(r.dir, "lost-workers.json"))
        line(r.engine.audit.path, ts=stamp(NOW - 300), actor="x", event="cloud.created", subject="recent", detail={"hard_deadline": stamp(NOW - 400)})
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="old", detail={"hard_deadline": stamp(NOW - 400)})
        real = self.mod._save
        self.mod._save = lambda path, state: (_ for _ in ()).throw(OSError("disk full"))
        self.assertEqual([x["name"] for x in r.passes()], ["recent"])
        self.assertEqual(r.passes(), [])   # not re-alerted
        self.mod._save = real
        line(r.engine.audit.path, ts=stamp(NOW - 100), actor="x", event="cloud.created", subject="newer", detail={"hard_deadline": stamp(NOW - 400)})
        self.assertEqual([x["name"] for x in r.passes()], ["newer"])

    def load_mutant(self, *pairs):
        src = (REPO / "bin" / "lost_worker.py").read_text()
        for old, new in zip(pairs[::2], pairs[1::2]):
            self.assertIn(old, src)
            src = src.replace(old, new)
        module = types.ModuleType("lost_worker_mutant")
        exec(compile(src, "lost_worker_mutant", "exec"), module.__dict__)
        return module

    def check_proc_cap(self):
        r = Rig(self.mod)
        path = os.path.join(r.dir, "lost-workers.json")
        os.unlink(path)
        os.mkdir(path)
        self.mod.ALERTED_MAX, self.mod.PASS_CAP = 30, 10 ** 6
        for i in range(100):
            line(r.engine.audit.path, ts=stamp(NOW - 300 - i), actor="x", event="cloud.created", subject="k%03d" % i, detail={"hard_deadline": stamp(NOW - 400)})
        r.passes()
        mem = self.mod._PROC_ALERTED[path]
        self.assertEqual(len(mem), 30)
        self.assertTrue(any("k000" in k for k in mem))   # the newest is kept

    def test_in_process_memory_is_capped_and_keeps_the_newest(self):
        self.check_proc_cap()

    def test_mutant_that_never_prunes_the_in_process_memory_is_caught(self):
        self.mod = self.load_mutant("    _PROC_ALERTED[path] = dict(alerted)", "    pass", "    while len(alerted) > ALERTED_MAX:", "    _PROC_ALERTED.setdefault(path, {}).update(alerted)\n    while len(alerted) > ALERTED_MAX:")
        with self.assertRaises(AssertionError):
            self.check_proc_cap()

    def check_audit_per_path(self):
        a, b = Rig(self.mod), Rig(self.mod)
        for r in (a, b):
            os.unlink(os.path.join(r.dir, "lost-workers.json"))
            with open(os.path.join(r.dir, "lost-workers.json"), "w") as h:
                h.write("{not json")
            os.chmod(r.dir, 0o500) if False else None
        # a corrupt file that cannot be set aside: replace by a directory-free trick: make the file undeletable via patched os
        real_replace, real_unlink = os.replace, os.unlink
        os.replace = lambda *x, **k: (_ for _ in ()).throw(OSError("no"))
        os.unlink = lambda *x, **k: (_ for _ in ()).throw(OSError("no"))
        try:
            for r in (a, b, a, b):
                r.passes()
        finally:
            os.replace, os.unlink = real_replace, real_unlink
        for r in (a, b):
            with open(r.engine.audit.path) as h:
                self.assertEqual(len([x for x in h if "lost_worker.memory_corrupt" in x]), 1)

    def test_audit_line_is_once_per_path(self):
        self.check_audit_per_path()

    def test_mutant_with_one_process_wide_audit_flag_is_caught(self):
        class OnePath(set):
            def __contains__(self, item):
                return len(self) > 0
        self.mod._PROC_AUDITED = OnePath()
        with self.assertRaises(AssertionError):
            self.check_audit_per_path()


class TestLostWorkerSlowPass(unittest.TestCase):
    """#2096: a stop behind a slow reaper pass is told as late, not lost."""

    def slow(self, r, n=870, at=NOW - 300, reason=None):
        line(r.launcher, ts=stamp(at), worker="", runtime_id="reaper", action="slow-reap-pass", ok=False,
             reason=reason or "the pass took %d seconds (limit 600)" % n)

    def pod_lines(self, r, got):
        return [x for x in got if x["kind"] == "pod_no_stop"]

    def s2_case(self, mod):
        r = Rig(mod)
        self.slow(r)
        r.created("w1", deadline=NOW - 600)
        return r, r.passes()

    def check_s2(self, mod):
        r, got = self.s2_case(mod)
        late = [x for x in got if "late, not lost" in x.get("text", "") and "870 seconds" in x["text"]]
        lost = [x for x in got if x["kind"] == "pod_no_stop" and "late, not lost" not in x["text"]]
        self.assertEqual(len(late), 1, "a late stop behind a slow pass is late, not lost")
        self.assertEqual(len(lost), 0, "a late stop behind a slow pass is late, not lost")

    def test_s2_a_late_pod_is_told_as_late_naming_the_pass(self):
        self.check_s2(load())

    def test_s3_a_pod_well_past_its_deadline_is_lost_as_today(self):
        r = Rig(load())
        self.slow(r)
        r.created("w1", deadline=NOW - 1800)
        got = r.passes()
        self.assertEqual([x for x in got if "late, not lost" in x["text"]], [])
        self.assertEqual(len([x for x in got if x["key"].startswith("pod_no_stop:w1")]), 1)

    def test_s4_a_late_pod_then_its_stop_raises_nothing_more(self):
        r = Rig(load())
        self.slow(r)
        r.created("w1", deadline=NOW - 600)
        self.assertEqual(len(self.pod_lines(r, r.passes())), 1)
        r.stopped("w1")
        self.assertEqual(self.pod_lines(r, r.passes(at=NOW + 10)), [])
        self.assertEqual(self.pod_lines(r, r.passes(at=NOW + 2000)), [])

    def audit_lines(self, r):
        with open(r.engine.audit.path) as handle:
            return len([x for x in handle if "lost_worker.slow_pass_unreadable" in x])

    def test_s4_a_malformed_slow_line_falls_back_with_one_audit_line(self):
        r = Rig(load())
        self.slow(r, reason="took long")
        r.created("w1", deadline=NOW - 600)
        got = r.passes()
        self.assertEqual([x["key"].split(":")[0] for x in self.pod_lines(r, got)], ["pod_no_stop"])
        self.assertEqual(self.audit_lines(r), 1)
        r.passes(at=NOW + 5)
        self.assertEqual(self.audit_lines(r), 1)

    def test_s4_an_unreadable_launcher_audit_falls_back_with_one_audit_line(self):
        r = Rig(load())
        os.unlink(r.launcher)
        os.mkdir(r.launcher)   # exists and cannot be read as a file
        r.created("w1", deadline=NOW - 600)
        got = r.passes()
        self.assertEqual([x["key"].split(":")[0] for x in got], ["pod_no_stop"])
        self.assertEqual(self.audit_lines(r), 1)
        r.passes(at=NOW + 5)
        self.assertEqual(self.audit_lines(r), 1)

    def test_a_late_alert_is_not_an_earlier_lost_worker_of_its_ask(self):
        r = Rig(load())
        self.slow(r)
        r.created("a1", deadline=NOW - 600)
        r.passes()
        r.created("a2", deadline=NOW - 1800)
        got = {x["name"]: x["text"] for x in r.passes(at=NOW + 1)}
        self.assertIn("first loss of this ask", got["a2"])

    def test_a_pod_later_truly_lost_still_raises_its_own_key(self):
        r = Rig(load())
        self.slow(r)
        r.created("w1", deadline=NOW - 600)
        r.passes()
        got = self.pod_lines(r, r.passes(at=NOW + 2000))
        self.assertEqual([x["key"].split(":")[0] for x in got], ["pod_no_stop"])

    def test_s5_a_mutant_that_ignores_the_pass_length_is_caught(self):
        source = (REPO / "bin" / "lost_worker.py").read_text()
        needle = "if now - deadline < longest:"
        self.assertEqual(source.count(needle), 1)
        control = load()   # the control first: the unmutated detector passes
        self.check_s2(control)
        mutant = types.ModuleType("lost_worker_mutant")
        exec(compile(source.replace(needle, "if False:"), "lost_worker_mutant", "exec"), mutant.__dict__)
        with self.assertRaises(AssertionError) as caught:
            self.check_s2(mutant)
        self.assertIn("a late stop behind a slow pass is late, not lost", str(caught.exception))


    def reaper_missed(self, got):
        return [x for x in got if x["kind"] == "reaper_missed"]

    def check_not_a_failure(self, mod):
        r = Rig(mod)
        for i in range(3):
            self.slow(r, n=700 + i, at=NOW - 600 * (i + 1))
        got = self.reaper_missed(r.passes())
        self.assertEqual(len(got), 0, "a slow reaper pass is not a reaper failure")

    def test_2101_three_slow_passes_alone_raise_no_reaper_missed(self):
        self.check_not_a_failure(load())

    def test_2101_a_day_of_slow_passes_and_one_failed_refresh_raises_one(self):
        r = Rig(load())
        for i in range(30):
            self.slow(r, n=650 + i, at=NOW - 100 - 10 * i)
        line(r.launcher, ts=stamp(NOW - 5), worker="", runtime_id="reaper", action="refresh-cluster-token", ok=False, reason="login failed")
        got = self.reaper_missed(r.passes())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["key"], "reaper_missed:outage@%s" % (NOW - 5))
        self.assertIn("1 failed reaper line(s)", got[0]["line"])
        self.assertIn("login failed", got[0]["line"])

    def test_2101_a_mutant_that_counts_slow_passes_again_is_caught(self):
        source = (REPO / "bin" / "lost_worker.py").read_text()
        needle = " and not _is_slow_line(item):"
        self.assertEqual(source.count(needle), 1)
        self.check_not_a_failure(load())   # the control first
        mutant = types.ModuleType("lost_worker_mutant2")
        exec(compile(source.replace(needle, ":"), "lost_worker_mutant2", "exec"), mutant.__dict__)
        with self.assertRaises(AssertionError) as caught:
            self.check_not_a_failure(mutant)
        self.assertIn("a slow reaper pass is not a reaper failure", str(caught.exception))


class TestChiefOnlyWhenNeeded(unittest.TestCase):
    """#2202: a loss the launcher's own record shows as an ok stop goes to its lane only; any other loss still goes to the lane and the Chief of Staff."""

    def delivered(self, stop):
        mod = load()
        r = Rig(mod)
        r.created("w1")
        if stop:
            r.stopped("w1")
        loss = {"key": "pod_no_stop:w1:%s" % (NOW - 7200), "kind": "pod_no_stop", "name": "w1", "ask": "ask-7", "at": NOW - 7200, "lane": "lane-a", "line": "x"}
        mod._losses = lambda *a, **k: [dict(loss)]   # the loss is given; only its delivery is under test
        r.passes()
        return sorted(to for to, _ in r.engine.sent)

    def test_s4_clean_stop_goes_to_the_lane_only(self):
        self.assertEqual(self.delivered(True), ["lane-a"])

    def test_s4_no_stop_goes_to_the_lane_and_the_chief(self):
        self.assertEqual(self.delivered(False), ["chief-of-staff", "lane-a"])


class TestRefusedLaunchIsToldToItsLane(unittest.TestCase):
    """#2303: a refused launch is told to the lane named in its `cloud.create_failed` line, never to the Chief of Staff; no readable lane: the Chief of Staff alone."""

    def refused(self, mod, *bosses):
        r = Rig(mod)
        for n, boss in enumerate(bosses):
            detail = {"exit": 1, "reason": "cluster full"}
            if boss is not None:
                detail["boss"] = boss
            line(r.engine.audit.path, ts=stamp(NOW - 100 - n), actor="x", event="cloud.create_failed", subject="w%d" % n, detail=detail)
        got = r.passes()
        r.passes()   # a second pass repeats nothing
        return got, sorted(to for to, _ in r.engine.sent), r.engine.sent

    def today(self):
        src = (REPO / "bin" / "lost_worker.py").read_text()
        for new, old in (('"lane": r.get("boss") or None,', '"lane": record.get("reports_to"),'), ('if loss["kind"] == "refused_launch":', 'if False:')):
            self.assertEqual(src.count(new), 1, new)
            src = src.replace(new, old)
        mod = types.ModuleType("lost_worker_today")
        exec(compile(src, "lost_worker_today", "exec"), mod.__dict__)
        return mod

    def check_s2(self, mod):
        got, to, sent = self.refused(mod, "lane-a", "lane-b")
        self.assertEqual(to.count("chief-of-staff"), 0, "S2 the Chief of Staff is told none")
        self.assertEqual(to, ["lane-a", "lane-b"], "S2 each lane once")
        self.assertTrue(all(("lane=" + t) in m for t, m in sent), "S2 the text names the lane")

    def check_s3(self, mod):
        got, to, _ = self.refused(mod, None)
        self.assertEqual(to, ["chief-of-staff"], "S3 the Chief of Staff alone, once")

    def test_s2_each_lane_is_told_once_and_the_chief_none(self):
        self.check_s2(load())

    def test_s3_an_unreadable_lane_reaches_the_chief_once(self):
        self.check_s3(load())
        got, to, _ = self.refused(load(), "")
        self.assertEqual(to, ["chief-of-staff"])   # an empty boss is no lane

    def test_a_boss_that_is_not_a_lane_name_is_no_lane(self):
        for bad in (5, "x" * 90, "lane\nx", "Lane A", "lane-a, kind=pod_no_stop"):
            got, to, _ = self.refused(load(), bad)
            self.assertEqual(to, ["chief-of-staff"], repr(bad))

    def test_the_alert_key_is_unchanged_and_an_old_line_with_no_boss_is_told_once(self):
        got, to, _ = self.refused(load(), "lane-a", None)
        self.assertEqual(sorted(x["key"].split("@")[0] for x in got), ["refused_launch:w0", "refused_launch:w1"])
        self.assertEqual(sorted(to), ["chief-of-staff", "lane-a"])

    def test_a_loss_of_another_kind_keeps_its_recipients(self):
        mod = load()
        r = Rig(mod)
        r.created("w1")
        r.passes()
        self.assertEqual(sorted(to for to, _ in r.engine.sent), ["chief-of-staff", "lane-a"])

    def test_s5_control_today_s_code_tells_the_chief_of_both(self):
        old = self.today()
        got, to, _ = self.refused(old, "lane-a", "lane-b")
        self.assertEqual(to.count("chief-of-staff"), 2)   # the control: the Chief of Staff is told of both, no lane is
        self.assertNotIn("lane-a", to)
        with self.assertRaises(AssertionError) as caught:
            self.check_s2(old)
        self.assertIn("S2 the Chief of Staff is told none", str(caught.exception))


class TestAskIsItsOwn(unittest.TestCase):
    """#2246: a lost worker is counted under its own ask, never under its name prefix."""

    def test_s2_readers_of_two_issues_and_a_builder_of_a_third_are_each_their_own_ask(self):
        mod = load()
        recs = {"cr2244": {"issue": 2243}, "cr2250": {"issue": 2249}, "cb2251": {"branch": "c2251-a"}}
        asks = {n: mod._ask_of(n, r) for n, r in recs.items()}
        self.assertEqual(len(set(asks.values())), 3)
        self.assertEqual(sum(1 for n, a in asks.items() if a in ("cr", "cb")), 0)

    def test_s3_repeat_counts_only_its_own_ask(self):
        mod = load()
        asks = [mod._ask_of("cr%d" % i, {}) for i in (1, 2, 3)]
        self.assertEqual(len(set(asks)), 3)

    def test_the_full_name_is_the_last_resort(self):
        self.assertEqual(load()._ask_of("cr2244", {}), "cr2244")


class TestPodReportedLostOnlyWhenSilent(unittest.TestCase):
    """#2255: the case of 6 November 2026: a pod past its hard deadline that is stopped seconds later, or still noting and pushing, is not lost."""
    G1 = "        if now - deadline <= GRACE:\n"
    G2 = "        if now - newest <= GRACE and newest > c[\"at\"]:\n"

    def module(self, mutant=False):
        src = (REPO / "bin" / "lost_worker.py").read_text()
        if mutant:   # today's rule: no grace and no activity read
            for gate in (self.G1, self.G2):
                self.assertEqual(src.count(gate), 1)
                src = src.replace(gate, gate.replace(gate.strip()[3:-1], "False"))
        mod = types.ModuleType("lost_worker_gate")
        exec(compile(src, "lost_worker_gate", "exec"), mod.__dict__)
        return mod

    def lost(self, mod, setup, at=NOW):
        r = Rig(mod)
        setup(r)
        return [x for x in r.passes(at=at) if x["kind"] == "pod_no_stop"]

    def seconds_past(self, r):   # the pod tg: deadline 3 s before the check, launcher actions after the launch
        r.created("tg", deadline=NOW - 3)

    def noting(self, r):   # renewal, nudge, wip note, final note after the launch, the last one 10 s ago; deadline 8 minutes ago
        r.created("tg", deadline=NOW - 480)
        for back, action in ((3000, "renew"), (2400, "idle-nudge"), (1200, "wip-note"), (10, "final-note")):
            line(r.launcher, ts=stamp(NOW - back), worker="tg", runtime_id="pod/tg", action=action, ok=True, reason="")

    def pushing(self, r):
        r.created("ti", deadline=NOW - 480)
        line(r.engine.audit.path, ts=stamp(NOW - 20), actor="x", event="push_watch.sent", subject="ti", detail={})

    def silent(self, r):
        r.created("cf", deadline=NOW - 3000)
        line(r.launcher, ts=stamp(NOW - 6000), worker="cf", runtime_id="pod/cf", action="renew", ok=True, reason="")

    def check_s2(self, mod):
        self.assertEqual(self.lost(mod, self.seconds_past), [], "S2 a pod seconds past its deadline is not lost")

    def check_s3(self, mod):
        self.assertEqual(self.lost(mod, self.noting), [], "S3 a pod still noting is not lost")
        self.assertEqual(self.lost(mod, self.pushing), [], "S3 a pod still pushing is not lost")

    def check_s4(self, mod):
        got = self.lost(mod, self.silent)
        self.assertEqual(len(got), 1, "S4 a silent pod is reported once")
        self.assertEqual(got[0]["at"], NOW - 6000, "S4 the last event is the newest action, not the launch")

    def test_s2_a_pod_stopped_seconds_after_its_deadline_raises_no_notice(self):
        self.check_s2(self.module())

    def test_s3_a_pod_still_noting_or_pushing_raises_no_notice(self):
        self.check_s3(self.module())

    def test_s4_a_silent_pod_is_still_reported_with_its_newest_event(self):
        self.check_s4(self.module())

    def quiet_only(self, r):   # the only line in the grace is push_watch.quiet: no push, not a sign of life
        r.created("tq", deadline=NOW - 480)
        line(r.engine.audit.path, ts=stamp(NOW - 20), actor="x", event="push_watch.quiet", subject="tq", detail={})

    def test_a_push_watch_quiet_line_is_not_a_sign_of_life(self):
        self.assertEqual(len(self.lost(self.module(), self.quiet_only)), 1)
        src = (REPO / "bin" / "lost_worker.py").read_text()
        old = src.replace('event == "push_watch.sent"', 'event in ("push_watch.sent", "push_watch.quiet")')
        self.assertNotEqual(old, src)
        mod = types.ModuleType("lost_worker_head")
        exec(compile(old, "lost_worker_head", "exec"), mod.__dict__)
        self.assertEqual(self.lost(mod, self.quiet_only), [], "control: head holds it back")

    def test_controls_today_s_rule_raises_the_false_notices(self):
        old = self.module(mutant=True)
        self.check_s4(old)   # the control: the silent pod is reported on today's rule too
        for check, text in ((self.check_s2, "S2 a pod seconds"), (self.check_s3, "S3 a pod still")):
            with self.assertRaises(AssertionError) as caught:
                check(old)
            self.assertIn(text, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
