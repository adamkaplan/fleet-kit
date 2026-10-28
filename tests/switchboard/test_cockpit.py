"""The Cockpit's fleet feed (ask #1423): `fleet-switchboard cockpit --json` and the file the daemon writes, $STATE/cockpit.json.

Real code on a fixture fleet (acme-org/widget-service style names): scenarios S1 to S7 and U1 of the plan. The feed is read-only: it reads state files, makes no
network call, needs no credential, calls no verb that answers or resolves a decision, and writes one file.
"""
import argparse
import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb

REPO = Path(__file__).resolve().parents[2]
PART = REPO / "bin" / "switchboard" / "cockpit.py"
DOC = REPO / "docs" / "cockpit-feed.md"
T0 = 1_800_000_000.0   # a fixed clock: 2027-01-15
CARDS = ("decisions", "held_prs", "lanes", "blocked", "heads_up")


def entry(kind, repo="acme-org/widget-service", ask=None, number=None, tier="human", title="a question", agent="orch-a", since="2027-01-14T10:00:00Z", **extra):
    found = {"id": "x%s" % (ask or number or agent), "title": title, "headline": title, "ask": ask, "repo": repo, "agent": agent, "kind": kind, "tier": tier, "since": since, "number": number}
    found.update(extra)
    return found


def fleet_state(root, decisions=None, agents=None, reports=None):
    """A state directory the way the daemon leaves it: decisions.json, registry.json and last-reports.json."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "decisions.json").write_text(json.dumps({"generation": 3, "written_at": "2027-01-15T00:00:00Z", "daemon_pid": 1, "decisions": decisions if decisions is not None else DECISIONS, "errors": []}))
    (root / "registry.json").write_text(json.dumps({"version": 1, "agents": agents if agents is not None else AGENTS, "retired": {}}))
    (root / "last-reports.json").write_text(json.dumps(reports if reports is not None else REPORTS))
    return root


DECISIONS = [
    entry("issue", ask=7, title="Choose the retention for widget exports", agent="coder-1", since="2027-01-14T09:00:00Z"),
    entry("question", ask=None, title="Which region first?", agent="coder-2", since="2027-01-14T11:00:00Z"),
    entry("issue", ask=8, title="A cos-tier question the owner does not see", tier="cos"),
    entry("captain", number=41, title="Add the export endpoint", agent=None, since="2027-01-14T12:00:00Z", holds=["merge-captain", "hold-policy"], note="held for the owner: policy file touched"),
    entry("notice", number=42, title="widget-service#42 is ready for review", agent="coder-1", since="2027-01-14T13:00:00Z", note="not ack'd"),
]
AGENTS = {
    "orch-a": {"role": "orchestrator", "charter": 1, "state": "working", "since": int(T0) - 3600, "session": "ses_example1"},
    "orch-b": {"role": "orchestrator", "charter": 2, "state": "idle", "since": int(T0) - 7200},
    "coder-1": {"role": "coder", "charter": 1, "state": "blocked", "since": int(T0) - 1800},
    "coder-2": {"role": "coder", "charter": 2, "state": "working", "since": int(T0) - 100},
}
REPORTS = {"coder-1": {"state": "blocked", "at": int(T0) - 1700, "text": "blocked on the retention decision"}, "orch-a": {"state": "working", "at": int(T0) - 60}}


def build(state, now=T0):
    with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(state).parent)}):
        document, _, _ = sb.cockpit_next(Path(state), now)
    return document


def verb(root, now=T0):
    """What `cockpit --json` prints, run in process on a state directory named fleet-switchboard under `root`."""
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root)}), contextlib.redirect_stdout(out):
        code = sb.cmd_cockpit(argparse.Namespace(json=True), lambda: now)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cockpit-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.state = fleet_state(self.root / "fleet-switchboard")


class TestFeedShape(Fixture):
    def test_s1_the_five_cards_carry_exactly_the_agreed_fields(self):
        code, doc = verb(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(doc), ["cards", "generation", "produced_at", "schema_version"])
        self.assertEqual(doc["schema_version"], 1)
        self.assertIsInstance(doc["generation"], int)
        self.assertEqual(tuple(doc["cards"]), tuple(sorted(CARDS)) if list(doc["cards"]) == sorted(doc["cards"]) else tuple(doc["cards"]))
        self.assertEqual(sorted(doc["cards"]), sorted(CARDS))
        for card, items in doc["cards"].items():
            self.assertTrue(items, "the fixture has an item of the %s card" % card)
            for item in items:
                self.assertEqual(sorted(item), sorted(sb.COCKPIT_FIELDS[card]), card)
        self.assertEqual({d["kind"] for d in doc["cards"]["decisions"]}, {"issue", "question"}, "the human tier only: the cos-tier question is not there")
        self.assertEqual([p["number"] for p in doc["cards"]["held_prs"]], [41])
        self.assertEqual(doc["cards"]["held_prs"][0]["holds"], ["merge-captain", "hold-policy"])
        self.assertEqual({lane["state"] for lane in doc["cards"]["lanes"]}, {"ok", "idle"})
        (blocked,) = doc["cards"]["blocked"]
        self.assertEqual(blocked["reason"], "blocked on the retention decision")
        self.assertTrue(blocked["blocked_on"].startswith("dec:"), "it waits on the decision its agent raised")

    def test_the_real_command_line_prints_the_same_document_and_exits_0(self):
        env = dict(os.environ, XDG_STATE_HOME=str(self.root))
        done = subprocess.run([sys.executable, str(REPO / "bin" / "fleet-switchboard"), "cockpit", "--json"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        doc = json.loads(done.stdout)
        self.assertEqual(sorted(doc["cards"]), sorted(CARDS))
        self.assertEqual(subprocess.run([sys.executable, str(REPO / "bin" / "fleet-switchboard"), "cockpit"], capture_output=True, text=True, env=env, timeout=60).returncode, 2, "no --json: usage")

    def test_a_missing_or_damaged_state_reads_as_empty_cards_not_an_error(self):
        empty = self.root / "empty" / "fleet-switchboard"
        empty.mkdir(parents=True)
        (empty / "decisions.json").write_text("{not json")
        doc = build(empty)
        self.assertEqual(doc["cards"], {card: [] for card in CARDS})


class TestIdsAndEtags(Fixture):
    def test_s2_ids_are_stable_and_match_the_pattern_and_only_the_changed_item_changes_its_etag(self):
        first, second = build(self.state), build(self.state, now=T0 + 500)
        ids = [item["id"] for items in first["cards"].values() for item in items]
        self.assertTrue(ids)
        self.assertEqual([i for i in ids if not sb.COCKPIT_ID_RE.match(i)], [], "0 ids fail the pattern")
        self.assertEqual({k: [(i["id"], i["etag"]) for i in v] for k, v in first["cards"].items()}, {k: [(i["id"], i["etag"]) for i in v] for k, v in second["cards"].items()})
        for text in ids:
            self.assertNotRegex(text, r"widget|acme|#", "no repository or number in an id")
        changed = [dict(d) for d in DECISIONS]
        changed[0] = dict(changed[0], title="Choose the retention for widget exports (revised)")
        fleet_state(self.state, decisions=changed)
        third = build(self.state)
        differs = [(card, a["id"]) for card in CARDS for a, b in zip(first["cards"][card], third["cards"][card]) if a["etag"] != b["etag"]]
        self.assertEqual(len(differs), 1, differs)
        self.assertEqual([a["id"] for card in CARDS for a, b in zip(first["cards"][card], third["cards"][card]) if a["id"] != b["id"]], [], "ids did not change")

    def test_an_etag_is_the_first_16_hex_of_the_canonical_json_without_it(self):
        import hashlib
        doc = build(self.state)
        item = doc["cards"]["held_prs"][0]
        body = {k: v for k, v in item.items() if k != "etag"}
        self.assertEqual(item["etag"], hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16])


class TestCapsAndScrub(Fixture):
    def test_s3_every_string_is_capped_the_items_are_50_newest_first_and_control_characters_are_gone(self):
        many = [entry("issue", ask=100 + n, title=("T%d " % n) + "x" * 500 + "\x1b[31m\nline", since="2027-01-%02dT10:00:00Z" % (1 + n % 28)) for n in range(60)]
        fleet_state(self.state, decisions=many)
        doc = build(self.state)
        items = doc["cards"]["decisions"]
        self.assertEqual(len(items), 50)
        self.assertLessEqual(max(len(i["title"]) for i in items), 120)
        self.assertEqual([i["since"] for i in items], sorted((i["since"] for i in items), reverse=True), "newest first")
        text = json.dumps(doc, ensure_ascii=False)
        self.assertEqual(re.findall(r"[\x00-\x1f\x7f-\x9f]", text), [], "0 control characters remain")
        for card, caps in (("held_prs", {"title": 120, "reason": 200}), ("heads_up", {"note": 300})):
            fleet_state(self.state, decisions=[entry("captain" if card == "held_prs" else "notice", number=1, title="y" * 900, note="z" * 900)])
            for item in build(self.state)["cards"][card]:
                for key, cap in caps.items():
                    self.assertLessEqual(len(item[key]), cap, (card, key))

    def test_at_most_five_holds_each_capped(self):
        fleet_state(self.state, decisions=[entry("captain", number=2, holds=["h%d-%s" % (n, "q" * 90) for n in range(9)])])
        (item,) = build(self.state)["cards"]["held_prs"]
        self.assertEqual(len(item["holds"]), 5)
        self.assertLessEqual(max(len(h) for h in item["holds"]), 40)

    def test_s4_a_token_a_path_and_a_diff_planted_in_a_title_and_a_note_never_reach_the_document(self):
        planted = ["ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "/home/someone/secrets/id_rsa", "src/app/config/secret.py", "diff --git a/x b/x", "+password = hunter2", "@@ -1,2 +1,2 @@",
                   "sk-live-ABCDEFGHIJKLMNOP1234", "C:\\Users\\someone\\key.pem"]
        diff = "\n".join(["fix the thing", "diff --git a/x b/x", "@@ -1,2 +1,2 @@", "+password = hunter2", "-old"])
        dirty = [entry("issue", ask=9, title="issue body: %s and %s" % (planted[0], planted[1])), entry("issue", ask=10, title=diff),
                 entry("captain", number=3, title="pr %s" % planted[2], note=diff + " " + planted[6]), entry("notice", number=4, title="heads-up %s %s" % (planted[3], planted[7]), note=diff)]
        fleet_state(self.state, decisions=dirty)
        doc = build(self.state)
        text = json.dumps(doc)
        for marker in ("ghp_ABCDEF", "/home/someone", "secrets/id_rsa", "src/app/config", "diff --git", "password", "hunter2", "@@ -1", "sk-live", "Users\\\\someone", "id_rsa"):
            self.assertNotIn(marker, text, marker)
        keys = {key for items in doc["cards"].values() for item in items for key in item}
        self.assertTrue(keys <= {key for fields in sb.COCKPIT_FIELDS.values() for key in fields}, keys)

    def test_a_title_that_starts_with_a_sign_is_kept(self):
        fleet_state(self.state, decisions=[entry("issue", ask=5, title="+1 reviewer needed on the export")])
        self.assertEqual(build(self.state)["cards"]["decisions"][0]["title"], "+1 reviewer needed on the export")


class TestDocument(unittest.TestCase):
    QUESTIONS = ("Q1", "Q2", "Q3", "Q4")

    @staticmethod
    def doc_fields(text):
        """{card: [keys]} from the field table of docs/cockpit-feed.md: rows `| card | key, key |`."""
        found = {}
        for line in text.splitlines():
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0] in CARDS:
                found[cells[0]] = [k.strip().strip("`") for k in cells[1].split(",")]
        return found

    def test_s5_the_field_table_equals_the_allowlist_in_both_directions_and_the_four_questions_are_listed(self):
        text = DOC.read_text()
        self.assertEqual(self.doc_fields(text), {card: list(fields) for card, fields in sb.COCKPIT_FIELDS.items()})
        for question in self.QUESTIONS:
            self.assertRegex(text, r"%s\b.*" % question)
        self.assertEqual(len(re.findall(r"raised, not built", text)), 3)
        self.assertRegex(text, r"Q2:.*Answered by the Chief of Staff")

    def test_s5_a_field_added_to_the_code_only_or_to_the_document_only_fails_the_comparison(self):
        text = DOC.read_text()
        wider = {card: list(fields) for card, fields in sb.COCKPIT_FIELDS.items()}
        wider["decisions"].append("path")
        self.assertNotEqual(self.doc_fields(text), wider, "a field added to the code only is a difference")
        extra = text.replace("| decisions |", "| decisions |", 1)
        row = next(l for l in text.splitlines() if l.strip().startswith("| decisions"))
        self.assertNotEqual(self.doc_fields(text.replace(row, row.rstrip().rstrip("|") + ", path |")), {card: list(f) for card, f in sb.COCKPIT_FIELDS.items()}, "a field added to the document only is a difference")
        self.assertEqual(extra, text)


class TestWriting(Fixture):
    def engine(self, clock):
        return mock.Mock(state_dir=self.state, now=lambda: clock[0])

    def run_passes(self, clock, seconds, step):
        writes, last = [], None
        for tick in range(0, seconds + 1, step):
            clock[0] = T0 + tick
            with mock.patch("time.monotonic", lambda: tick + 0.0):
                if sb.publish_cockpit(self.engine(clock)):
                    writes.append(tick)
        return writes

    def test_s6_written_on_change_and_at_least_every_60_seconds_atomically_with_mode_0600(self):
        sb._STATE_LAST.clear()
        clock = [T0]
        writes = self.run_passes(clock, 300, 20)
        path = self.state / "cockpit.json"
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        gaps = [b - a for a, b in zip(writes, writes[1:])]
        self.assertLessEqual(max(gaps), 60, writes)
        self.assertEqual(writes[0], 0)
        inside = [w for w in writes if 0 < w < 60]
        self.assertEqual(inside, [], "an unchanged state inside 60 seconds causes 0 rewrites")
        self.assertEqual([f for f in os.listdir(str(self.state)) if f.startswith("cockpit.json.")], [], "no temp file is left")
        first = json.loads(path.read_text())
        self.assertEqual(first["generation"], 1)

    def test_s6_a_change_is_rewritten_at_once_with_a_higher_generation(self):
        sb._STATE_LAST.clear()
        clock = [T0]
        with mock.patch("time.monotonic", lambda: 0.0):
            self.assertTrue(sb.publish_cockpit(self.engine(clock)))
        generation = json.loads((self.state / "cockpit.json").read_text())["generation"]
        fleet_state(self.state, decisions=DECISIONS + [entry("issue", ask=77, title="One more")])
        clock[0] += 5
        with mock.patch("time.monotonic", lambda: 5.0):
            self.assertTrue(sb.publish_cockpit(self.engine(clock)), "a change inside 60 seconds is written")
        after = json.loads((self.state / "cockpit.json").read_text())
        self.assertEqual(after["generation"], generation + 1)
        clock[0] += 70
        with mock.patch("time.monotonic", lambda: 75.0):
            self.assertTrue(sb.publish_cockpit(self.engine(clock)), "the heartbeat rewrites an unchanged state")
        again = json.loads((self.state / "cockpit.json").read_text())
        self.assertEqual(again["generation"], after["generation"], "no content change, no new generation")
        self.assertNotEqual(again["produced_at"], after["produced_at"])


class TestReadOnly(Fixture):
    @staticmethod
    def listing(root):
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}

    def test_s7_the_feed_part_names_no_verb_that_answers_resolves_or_defers_and_calls_nothing_that_writes(self):
        tree = ast.parse(PART.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        forbidden = {"cmd_decision_write", "cmd_decisions", "deliver_answer", "answer_decision", "resolve_decision", "defer_decision", "mark_escalated", "mark_resolved",
                     "write_escalated", "subprocess", "urlopen", "request", "HTTPConnection", "socket", "runner", "gh_json", "_gh_json", "send_message", "cmd_send", "cmd_report"}
        self.assertEqual(sorted(names & forbidden), [], "no decision write verb, no process, no network name in the feed part")
        source = PART.read_text()
        for word in ("answer(", "resolve(", "defer("):
            self.assertNotIn(word, source)

    def test_s7_a_run_changes_nothing_but_cockpit_json(self):
        sb._STATE_LAST.clear()
        before = self.listing(self.root)
        clock = [T0]
        with mock.patch("time.monotonic", lambda: 0.0):
            sb.publish_cockpit(mock.Mock(state_dir=self.state, now=lambda: clock[0]))
        verb(self.root)
        after = self.listing(self.root)
        self.assertEqual(sorted(set(after) - set(before)), ["fleet-switchboard/cockpit.json"])
        self.assertEqual({k: v for k, v in after.items() if k != "fleet-switchboard/cockpit.json"}, before, "every other byte is identical")

    def test_u1_decisions_json_and_the_decisions_report_are_unchanged_by_the_feed(self):
        sb._STATE_LAST.clear()
        file = self.state / "decisions.json"
        baseline_bytes, baseline = file.read_bytes(), sb.decisions_report(self.state, T0)
        with mock.patch("time.monotonic", lambda: 0.0):
            sb.publish_cockpit(mock.Mock(state_dir=self.state, now=lambda: T0))
        verb(self.root)
        self.assertEqual(file.read_bytes(), baseline_bytes)
        self.assertEqual(sb.decisions_report(self.state, T0), baseline)
        self.assertEqual(len(baseline["decisions"]), len(DECISIONS), "no decision was dropped composing the feed")


if __name__ == "__main__":
    unittest.main()
