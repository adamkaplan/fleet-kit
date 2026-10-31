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
        line(r.engine.audit.path, ts=stamp(NOW - 300), actor="x", event="cloud.created", subject="newpod", detail={"hard_deadline": stamp(NOW - 60)})
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.create_failed", subject="oldref", detail={})
        line(r.engine.audit.path, ts=stamp(NOW - 100), actor="x", event="cloud.create_failed", subject="newref", detail={})
        got += r.passes()
        self.assertEqual(sorted((x["kind"], x["name"]) for x in got),
                         [("local_worker", "newloc"), ("pod_no_stop", "newpod"), ("refused_launch", "newref")])
        self.assertFalse(json.load(open(state_path))["catchup"])
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="gap", detail={"hard_deadline": stamp(NOW - 60)})
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
    VALID = {"reaper_ok": [5], "catchup": [True], "stop": [{"a": 1}], "report": [{"a": 1}], "gone": [{"a": 1}]}   # the one bad value of the table that is a good value of that key
    KEYS = ("offsets", "alerted", "created", "stop", "report", "refused", "seen", "gone", "reaper", "reaper_ok", "reaper_outage", "reaper_gap",
            "reaper_new", "catchup")

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
            line(self.r.engine.audit.path, ts=stamp(NOW - age), actor="x", event="cloud.created", subject=name, detail={"hard_deadline": stamp(NOW - 60)})

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
        line(r.engine.audit.path, ts=stamp(NOW - 300), actor="x", event="cloud.created", subject="recent", detail={"hard_deadline": stamp(NOW - 60)})
        line(r.engine.audit.path, ts=stamp(NOW - 3 * 3600), actor="x", event="cloud.created", subject="old", detail={"hard_deadline": stamp(NOW - 60)})
        real = self.mod._save
        self.mod._save = lambda path, state: (_ for _ in ()).throw(OSError("disk full"))
        self.assertEqual([x["name"] for x in r.passes()], ["recent"])
        self.assertEqual(r.passes(), [])   # not re-alerted
        self.mod._save = real
        line(r.engine.audit.path, ts=stamp(NOW - 100), actor="x", event="cloud.created", subject="newer", detail={"hard_deadline": stamp(NOW - 60)})
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
            line(r.engine.audit.path, ts=stamp(NOW - 300 - i), actor="x", event="cloud.created", subject="k%03d" % i, detail={"hard_deadline": stamp(NOW - 60)})
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


if __name__ == "__main__":
    unittest.main()
