"""Ask #1876: the author's About is read from an issue body or a report text. Neutral names (acme)."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import decision_core as core  # noqa: E402
import decision_sync  # noqa: E402
from support.loader import sb  # noqa: E402
from support.orders_and_adapters import EA, FakeDoorServer, fake_mapping  # noqa: E402

REPO_NAME = "acme/api"
ABOUT = "Acme sells widgets; this asks whether the build may ship, and each choice changes the next release."
ANCHOR = "a raise went out without its about string"


class StrictDoor(FakeDoorServer):
    """A receiver that refuses a raise with no about string, as the real tool does."""

    def op_raise(self, args, ident, seat):
        if not isinstance(args.get("about"), str) or not args["about"]:
            self.refused = getattr(self, "refused", 0) + 1
            return 400, {"code": "bad", "message": "Invalid arguments for tool raise: about"}, {}
        return super().op_raise(args, ident, seat)


class Host(decision_sync.Host):
    def __init__(self):
        self.events = []

    def audit(self, event, subject, detail):
        self.events.append((event, subject, detail))


def entry(n, kind="issue", tier="human", about=ABOUT, rows=None):
    found = {"id": "D%d" % n, "title": "Decision %d" % n, "headline": "Headline %d" % n, "kind": kind, "tier": tier,
             "repo": REPO_NAME, "ask": n, "agent": "w1", "since": 1, "recommendation": "yes"}
    if about:
        found["about"] = about
    if rows:
        found["rows"] = rows
    return found


SETTINGS = {"repos": [REPO_NAME], "kinds": ["issue", "report"], "tiers": ["orchestrator", "cos", "human"], "poll_seconds": 1,
            "retry_base_seconds": 5, "retry_max_seconds": 60, "outbox_alarm_depth": 100,
            "tier_map": {"orchestrator": "low", "cos": "moderate", "human": "high"}}


class AboutRig:
    """An engine on an adapter on a strict receiver; `adapter_module` is the adapter's code (a copy for a mutant)."""

    def __init__(self, test, adapter_module=None, door=StrictDoor, **mapping):
        self.module = adapter_module or EA
        self.door = door()
        test.addCleanup(self.door.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="about-"))
        test.addCleanup(shutil.rmtree, self.tmp, True)
        data = fake_mapping(self.door.url, "tool", **mapping)
        data.setdefault("tier_map", {})
        self.adapter = self.module.McpRestAdapter(data, sleep=lambda s: None, environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY})
        self.host = Host()
        self.engine = decision_sync.SyncEngine(SETTINGS, self.tmp, self.adapter, self.host)

    def run(self, entries, passes=2):
        seeds = {e["id"]: "seed:%s" % e["id"] for e in entries}
        for _ in range(passes):
            status = self.engine.cycle(entries, [], seeds)
        return status


class TestAboutParsing(unittest.TestCase):
    def test_issue_bodies(self):   # S8
        cases = [
            ("## Plan\nx\n\n## About\nAcme sells\n  widgets.\n\n## Options\nA", "Acme sells widgets."),
            ("intro\n## About\nAt the\nend", "At the end"),
            ("## About\n\n## Options\nA", None),
            ("## Plan\nno about here", None),
            ("## About\nfirst one\n## Other\nz\n## About\nsecond", "first one"),
        ]
        got = [core.parse_about(body) for body, _ in cases]
        self.assertEqual(sum(1 for g, (_, want) in zip(got, cases) if g == want), 5, msg="about of five bodies: %r" % got)

    def test_report_paragraph(self):
        self.assertEqual(core.parse_about_paragraph("Ask?\n\nAbout: Acme\nneeds it.\n\nMore"), "Acme needs it.")
        self.assertIsNone(core.parse_about_paragraph("no paragraph"))
        self.assertIsNone(core.parse_about(None))


def check_about(test, adapter_module=None):
    """S2: four decisions each carrying an About are raised: none refused, each about equals the author's text."""
    world = AboutRig(test, adapter_module)
    rows = [{"ref": "r1", "finding": "f", "recommendation": "do"}]
    world.run([entry(1), entry(2, tier="cos"), entry(3, kind="report"), entry(4, rows=rows)])
    refused, items = getattr(world.door, "refused", 0), list(world.door.items.values())
    carried = [i for i in items if i["_args"].get("about") == ABOUT]
    if refused or len(carried) != 4:
        raise AssertionError(ANCHOR + ": %d of 4 refused, %d of 4 carry the About" % (refused, len(carried)))
    return world


class TestAboutCarried(unittest.TestCase):
    def test_four_decisions_carry_their_about(self):   # S2
        check_about(self)

    def test_the_mutant_without_the_field_fails_and_the_control_passes(self):   # S3
        copy = Path(tempfile.mkdtemp(prefix="about-mutant-"))
        self.addCleanup(shutil.rmtree, copy, True)
        for name in ("external_adapter.py", "decision_core.py"):
            shutil.copy(REPO / "bin" / name, copy / name)

        def load():
            spec = importlib.util.spec_from_file_location("external_adapter_about_copy", copy / "external_adapter.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        check_about(self, load())   # the control, unmutated
        text = (copy / "external_adapter.py").read_text()
        old = '            body[f.get("about", "about")] = d["about"]\n'
        self.assertEqual(text.count(old), 1, "the mutation target is not there")
        (copy / "external_adapter.py").write_text(text.replace(old, "            pass\n"))
        self.assertNotEqual((copy / "external_adapter.py").read_text(), text, "the mutation was not applied")
        with self.assertRaisesRegex(AssertionError, ANCHOR):
            check_about(self, load())


class RefusingDoor(FakeDoorServer):
    """A receiver that refuses every raise for good."""

    def op_raise(self, args, ident, seat):
        return 400, {"code": "bad", "message": "Invalid arguments for tool raise"}, {}


def lines_of(rig, needle, now=2.0):
    config = {"external_decisions": dict(SETTINGS, enabled=True)}
    text = sb.external_status_line(config, str(rig.tmp), now) or ""
    return [l for l in text.splitlines() if needle in l]


def about_less(n):
    return entry(n, about=None)


class TestAboutGate(unittest.TestCase):
    def test_decisions_with_no_about_are_not_sent_and_are_shown(self):   # S2b
        rig = AboutRig(self)
        rig.run([about_less(1), dict(about_less(2), kind="report"), entry(3)])
        self.assertEqual(len(rig.door.items), 1)
        shown = lines_of(rig, "needs an About")
        self.assertEqual(len(shown), 2, shown)
        self.assertTrue(shown[0].endswith("D1") and shown[1].endswith("D2"), shown)
        self.assertEqual(rig.engine.failed, {}, "never refused for good")

    def test_a_refused_raise_is_audited_with_its_display_id_and_title(self):   # S4
        rig = AboutRig(self, door=RefusingDoor)
        rig.run([entry(1), entry(2)])
        found = [d for e, k, d in rig.host.events if e == "external.permanent_failure"]
        self.assertEqual(len(found), 2)
        self.assertEqual(sorted((d["display_id"], d["title"]) for d in found), [("D1", "Decision 1"), ("D2", "Decision 2")])

    def test_a_refused_raise_is_shown_with_its_decision(self):   # S5
        rig = AboutRig(self, door=RefusingDoor)
        rig.run([entry(1), entry(2)])
        shown = lines_of(rig, "raise refused")
        self.assertEqual(len(shown), 2, shown)
        self.assertTrue(all(("D%d" % n) in l for n, l in ((1, shown[0]), (2, shown[1]))), shown)
        self.assertIn("2 permanent failure(s)", sb.external_status_line({"external_decisions": dict(SETTINGS, enabled=True)}, str(rig.tmp), 2.0))
        clean = AboutRig(self)
        clean.run([entry(1)])
        self.assertEqual(lines_of(clean, "raise refused"), [])

    def failed_table(self, rig, entries):
        keys = [core.decision_key("seed:%s" % e["id"]) for e in entries]
        failed = {k: {"op": "raise", "at": 1, "sig": ["old", "x", "y"],
                      "error": 'MCP error -32602: Input validation error: Invalid arguments for tool raise path ["about"] expected string'}
                  for k in keys}
        (rig.tmp / decision_sync.LINKS_FILE).write_text(json.dumps({"links": {}, "failed": failed}))
        return decision_sync.SyncEngine(SETTINGS, rig.tmp, rig.adapter, rig.host)

    def test_the_failed_raises_are_sent_again_once_when_each_has_an_about(self):   # S6
        entries = [entry(n) for n in range(1, 16)]
        rig = AboutRig(self)
        rig.engine = self.failed_table(rig, entries)
        rig.run(entries, passes=1)
        self.assertEqual((len(rig.door.items), len(rig.engine.failed), len(rig.engine.links)), (15, 0, 15))
        bare = [about_less(n) for n in range(1, 16)]
        other = AboutRig(self)
        other.engine = self.failed_table(other, bare)
        other.run(bare, passes=1)
        self.assertEqual((len(other.door.items), len(lines_of(other, "needs an About"))), (0, 15))

    def test_linked_decisions_are_not_revised_when_an_about_arrives(self):   # U1
        entries = [entry(n, about=None) for n in range(1, 31)]   # linked once, before an About was asked for
        rig = AboutRig(self)
        rig.run([dict(e, about=ABOUT) for e in entries], passes=1)   # linked
        links = json.dumps(rig.engine.links, sort_keys=True)
        sent = []
        real = rig.adapter.push
        rig.adapter.push = lambda operation, key: (sent.append(operation["op"]), real(operation, key))[1]
        rig.run([dict(e, about="A different background text.") for e in entries], passes=2)
        self.assertEqual(sent.count("revise"), 0, sent)
        self.assertEqual(json.dumps(rig.engine.links, sort_keys=True), links)


class TestAboutManifest(unittest.TestCase):
    def test_the_field_name_defaults_and_a_manifest_may_name_it(self):   # U3
        class Named(FakeDoorServer):
            RAISE_OPT = FakeDoorServer.RAISE_OPT | {"background"}
        plain = AboutRig(self)
        self.assertNotIn("about", plain.adapter.m.data["fields"]["raise"])
        plain.run([entry(1)])
        self.assertEqual([i["_args"].get("about") for i in plain.door.items.values()], [ABOUT])
        named = AboutRig(self, door=Named, fields={"raise": dict(fake_mapping("http://x")["fields"]["raise"], about="background")})
        named.run([entry(1)])
        self.assertEqual([i["_args"].get("background") for i in named.door.items.values()], [ABOUT])
        self.assertNotIn("about", list(named.door.items.values())[0]["_args"])


if __name__ == "__main__":
    unittest.main()
