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


if __name__ == "__main__":
    unittest.main()
