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
        self.assertEqual(sorted(doc), ["cards", "generation", "produced_at", "schema_version", "sources"])
        self.assertEqual(doc["schema_version"], 1)
        self.assertIsInstance(doc["generation"], int)
        self.assertEqual(list(doc["cards"]), sorted(CARDS), "the cards are written in a fixed order (sorted keys)")
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

    def test_a_missing_or_damaged_state_reads_as_empty_cards_and_unreadable_sources(self):
        empty = self.root / "empty" / "fleet-switchboard"
        empty.mkdir(parents=True)
        (empty / "decisions.json").write_text("{not json")
        doc = build(empty)
        self.assertEqual(doc["cards"], {card: [] for card in CARDS})
        self.assertEqual(doc["sources"], {"decisions": "unreadable", "registry": "unreadable", "reports": "unreadable"}, "and it says so: it is not an all-clear")


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

    # One sample per alternative of each scrub regex (R-TEST-5 of the test review of #1436). The completeness test splits the compiled pattern, so a new
    # alternative with no sample fails it; each sample would survive if its own alternative were removed or weakened.
    TOKEN_SAMPLES = ["ghp_" + "A" * 20, "github_pat_" + "A" * 25, "sk-" + "a" * 20, "xoxb-1234567890ab", "AKIA" + "A" * 14, "eyJ" + "a" * 12 + "." + "b" * 12, "abcdef0123456789" * 2, "Qz" * 20]
    PATH_SAMPLES = ["/home/u/x", "C:\\Users\\u\\k.pem", "a/b/c.txt"]
    INLINE_SAMPLES = ["diff --git a/x b/x", "@@ -1,2 +3,4 @@"]
    LINE_SAMPLES = ["diff --git a b", "index 1a2b3c..4d5e6f", "@@ -1 +1 @@", "+++ b/file", "--- a/file", "+added"]

    @staticmethod
    def alternatives(regex):
        """The alternatives of a compiled pattern, read from its source: split at top level, else inside the one group that wraps it (escapes and classes skipped)."""
        def split(at):
            depth, cls, esc, out, cur = 0, False, False, [], ""
            for ch in regex.pattern:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif cls:
                    cls = ch != "]"
                elif ch == "[":
                    cls = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "|" and depth == at:
                    out.append(cur)
                    cur = ""
                    continue
                cur += ch
            return out + [cur]
        top = split(0)
        return top if len(top) > 1 else split(1)

    def test_every_alternative_of_each_scrub_regex_has_a_sample(self):
        self.assertEqual(len(self.alternatives(sb._COCKPIT_TOKEN)), len(self.TOKEN_SAMPLES))
        self.assertEqual(len(self.alternatives(sb._COCKPIT_PATH)), len(self.PATH_SAMPLES))
        self.assertEqual(len(self.alternatives(sb._COCKPIT_DIFF_INLINE)), len(self.INLINE_SAMPLES))
        self.assertEqual(len(self.alternatives(sb._COCKPIT_DIFF_LINE)), len(self.LINE_SAMPLES))

    def test_each_token_path_and_inline_diff_alternative_is_replaced_in_a_title_and_a_note(self):
        for samples, mark in ((self.TOKEN_SAMPLES, "[removed]"), (self.PATH_SAMPLES, "[path]"), (self.INLINE_SAMPLES, "[removed]")):
            for sample in samples:
                self.assertEqual(sb.cockpit_text("see %s now" % sample, 300), "see %s now" % mark, sample)
                fleet_state(self.state, decisions=[entry("issue", ask=21, title="see %s now" % sample), entry("notice", number=7, title="see %s now" % sample), entry("captain", number=9, title="t", note="see %s now" % sample)])
                doc = build(self.state)
                self.assertEqual(doc["cards"]["decisions"][0]["title"], "see %s now" % mark, sample)
                self.assertEqual(doc["cards"]["heads_up"][0]["note"], "see %s now" % mark, sample)
                self.assertEqual(doc["cards"]["held_prs"][0]["reason"], "see %s now" % mark, sample)

    def test_each_diff_line_alternative_drops_the_line_in_a_title_and_a_note(self):
        for sample in self.LINE_SAMPLES:
            value = "keep\n" + sample
            self.assertEqual(sb.cockpit_text(value, 300), "keep", sample)
            fleet_state(self.state, decisions=[entry("issue", ask=22, title=value), entry("notice", number=8, title=value), entry("captain", number=10, title="t", note=value)])
            doc = build(self.state)
            self.assertEqual(doc["cards"]["decisions"][0]["title"], "keep", sample)
            self.assertEqual(doc["cards"]["heads_up"][0]["note"], "keep", sample)
            self.assertEqual(doc["cards"]["held_prs"][0]["reason"], "keep", sample)

    def test_every_control_character_class_is_replaced_by_a_space(self):
        for ch in ("\x00", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"):
            self.assertEqual(sb.cockpit_text("a%sb" % ch, 50), "a b", repr(ch))

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
        row = next(l for l in text.splitlines() if l.strip().startswith("| decisions"))
        self.assertNotEqual(self.doc_fields(text.replace(row, row.rstrip().rstrip("|") + ", path |")), {card: list(f) for card, f in sb.COCKPIT_FIELDS.items()}, "a field added to the document only is a difference")


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
        # the list above is a floor: every function of the daemon that answers, resolves or defers a decision is named by its verb prefix, derived from the code
        entry_tree = ast.parse((REPO / "bin" / "fleet-switchboard").read_text())
        verbs = {n.name for n in ast.walk(entry_tree) if isinstance(n, ast.FunctionDef) and re.match(r"cmd_(decision_)?(answer|resolve|defer)", n.name)}
        self.assertEqual(sorted(names & verbs), [], "no answer, resolve or defer verb of the daemon is named in the feed part: %s" % sorted(verbs))
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


class TestBranches(Fixture):
    """Each branch of the feed builder that the feed's own scenarios did not reach, with the input that takes it."""

    def feed_file(self):
        return self.state / "cockpit.json"

    def write_feed(self, body):
        self.feed_file().write_text(body if isinstance(body, str) else json.dumps(body))

    def test_a_damaged_feed_file_is_rebuilt_from_generation_one(self):
        for body in ("{not json", "[]", json.dumps({"schema_version": 1, "generation": "x", "cards": {}}), json.dumps({"schema_version": 1, "generation": 4, "cards": []})):
            self.write_feed(body)
            self.assertEqual(sb.cockpit_current(self.feed_file()), (0, None), body)
            self.assertEqual(build(self.state)["generation"], 1, body)

    def test_a_feed_of_another_schema_version_is_not_continued(self):
        cards = build(self.state)["cards"]
        self.write_feed({"schema_version": 2, "generation": 9, "cards": cards})
        self.assertEqual(sb.cockpit_current(self.feed_file()), (0, None))
        self.assertEqual(build(self.state)["generation"], 1, "not 10: another version's generation is not carried")

    def test_a_missing_feed_file_starts_at_one_and_a_current_one_is_continued(self):
        self.assertEqual(sb.cockpit_current(self.feed_file()), (0, None))
        doc = build(self.state)
        self.write_feed(doc)
        self.assertEqual(sb.cockpit_current(self.feed_file()), (doc["generation"], doc["cards"]))
        self.assertEqual(build(self.state)["generation"], doc["generation"], "unchanged cards keep the generation")

    def blocked_fleet(self, agents):
        fleet_state(self.state, decisions=[], agents=agents, reports={})
        return build(self.state)["cards"]

    def test_a_blocked_agent_that_raised_no_decision_waits_on_its_boss_lane(self):
        cards = self.blocked_fleet({"boss": {"role": "orchestrator", "charter": 5, "state": "working", "since": int(T0) - 10},
                                    "w": {"role": "coder", "charter": 5, "state": "blocked", "since": int(T0) - 20}})
        (blocked,) = cards["blocked"]
        self.assertEqual(blocked["blocked_on"], "lane:boss")
        self.assertEqual(blocked["reason"], "blocked", "no report: the plain word")

    def test_a_blocked_agent_with_no_boss_waits_on_its_own_lane(self):
        cards = self.blocked_fleet({"w": {"role": "coder", "charter": 9, "state": "blocked", "since": int(T0) - 20}})
        (blocked,) = cards["blocked"]
        self.assertEqual(blocked["blocked_on"], "lane:w")

    def test_a_blocked_agent_that_raised_a_decision_waits_on_the_newest_one(self):
        decisions = [entry("issue", ask=1, agent="w", since="2027-01-14T09:00:00Z"), entry("issue", ask=2, agent="w", since="2027-01-14T10:00:00Z")]
        fleet_state(self.state, decisions=decisions, agents={"w": {"role": "coder", "charter": 9, "state": "blocked", "since": int(T0) - 20}}, reports={})
        cards = build(self.state)["cards"]
        newest = next(d for d in cards["decisions"] if d["since"] == "2027-01-14T10:00:00Z")
        self.assertEqual(cards["blocked"][0]["blocked_on"], newest["id"])

    def test_every_lane_state_of_the_registry_is_mapped_or_left_out_on_purpose(self):
        agents = {"o-%s" % state: {"role": "orchestrator", "charter": n, "state": state, "since": int(T0) - 10} for n, state in enumerate(list(sb.COCKPIT_LANE_STATES) + ["starting", "unknown"])}
        lanes = {lane["id"]: lane["state"] for lane in self.blocked_fleet(agents)["lanes"]}
        for state, shown in sb.COCKPIT_LANE_STATES.items():
            self.assertEqual(lanes.get("lane:o-%s" % state), shown, state)
        self.assertEqual(lanes["lane:o-detached"], "down")
        self.assertEqual(lanes["lane:o-dead"], "down")
        self.assertNotIn("lane:o-starting", lanes, "a state with no mapping is left out, not guessed")
        self.assertEqual(len(lanes), len(sb.COCKPIT_LANE_STATES))

    def test_a_publish_that_raises_is_reported_and_the_rest_of_the_pass_runs(self):
        names = ["prune_delivered_reminders", "friction_tick", "load_tick", "prune_report_riders", "cloud_watch_tick", "publish_decisions", "schedule_judge_warm", "write_catching_up", "health_write"]
        calls = []
        patches = [mock.patch.object(sb, n, side_effect=lambda *a, _n=n, **k: calls.append(_n)) for n in names]
        patches.append(mock.patch.object(sb, "publish_cockpit", side_effect=RuntimeError("boom")))
        engine = mock.Mock(now=lambda: T0)
        report = {"errors": []}
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            sb.finish_pass(engine, None, report)
        self.assertEqual([c for c in calls if c in ("write_catching_up", "health_write", "schedule_judge_warm")], ["write_catching_up", "health_write", "schedule_judge_warm"], "the steps after the feed still run")
        engine._failed.assert_called_once()
        self.assertEqual(engine._failed.call_args[0][:2], ("cockpit", "cockpit"))


def break_source(state, source, how):
    """Make one source unreadable: its file gone, damaged, a symlink to a good file, or over the size cap."""
    name = {"decisions": "decisions.json", "registry": "registry.json", "reports": "last-reports.json"}[source]
    path = Path(state) / name
    if how == "missing":
        path.unlink()
    elif how == "damaged":
        path.write_text("{not json")
    elif how == "symlink":
        target = Path(state) / (name + ".real")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.write_text(json.dumps({"pad": "x" * 1_100_000}))


class TestSources(Fixture):
    """S9: an unreadable source is not an all-clear. The document says which of the three places could not be read; the verb exits 3; the daemon keeps the last good file."""
    HOWS = ("missing", "damaged", "symlink", "oversize")

    def fresh(self):
        for path in list(self.state.iterdir()):
            path.unlink()
        fleet_state(self.state)

    def test_s9_a_readable_fleet_says_ok_for_each_source_and_exits_0(self):
        code, doc = verb(self.root)
        self.assertEqual((code, doc["sources"]), (0, {"decisions": "ok", "registry": "ok", "reports": "ok"}))
        self.assertEqual(sorted(sb.COCKPIT_SOURCES), sorted(doc["sources"]))

    def test_s9_the_verb_exits_3_and_names_the_unreadable_source_for_each_of_the_three(self):
        for source in sb.COCKPIT_SOURCES:
            for how in [h for h in self.HOWS if (source, h) != ("registry", "symlink")]:   # the registry's own loader is not this feed's to change
                with self.subTest(source=source, how=how):
                    self.fresh()
                    break_source(self.state, source, how)
                    code, doc = verb(self.root)
                    self.assertEqual(code, 3)
                    self.assertEqual(doc["sources"], {s: "unreadable" if s == source else "ok" for s in sb.COCKPIT_SOURCES})
                    self.assertEqual(sorted(doc["cards"]), sorted(CARDS), "the document is still printed")

    def test_s9_an_empty_state_directory_is_three_unreadable_sources_not_five_quiet_cards(self):
        empty = self.root / "bare" / "fleet-switchboard"
        empty.mkdir(parents=True)
        code, doc = verb(self.root / "bare")
        self.assertEqual(code, 3)
        self.assertEqual(set(doc["sources"].values()), {"unreadable"})

    def test_s9_the_real_command_line_exits_3_on_an_empty_state_directory(self):
        bare = self.root / "bare2"
        (bare / "fleet-switchboard").mkdir(parents=True)
        done = subprocess.run([sys.executable, str(REPO / "bin" / "fleet-switchboard"), "cockpit", "--json"], capture_output=True, text=True, env=dict(os.environ, XDG_STATE_HOME=str(bare)), timeout=60)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(json.loads(done.stdout)["sources"]["decisions"], "unreadable")

    def publish(self, tick):
        with mock.patch("time.monotonic", lambda: float(tick)):
            return sb.publish_cockpit(mock.Mock(state_dir=self.state, now=lambda: T0 + tick))

    def test_s9_the_daemon_keeps_the_last_good_file_byte_for_byte_when_any_source_cannot_be_read(self):
        feed = self.state / "cockpit.json"
        for source in sb.COCKPIT_SOURCES:
            for how in [h for h in self.HOWS if (source, h) != ("registry", "symlink")]:   # the registry's own loader is not this feed's to change
                with self.subTest(source=source, how=how):
                    sb._STATE_LAST.clear()
                    self.fresh()
                    self.assertTrue(self.publish(0))
                    good = feed.read_bytes()
                    break_source(self.state, source, how)
                    self.assertFalse(self.publish(500), "past the heartbeat, still nothing is written")
                    self.assertEqual(feed.read_bytes(), good)

    def test_s9_a_pass_on_an_empty_state_directory_creates_no_file(self):
        sb._STATE_LAST.clear()
        for path in self.state.iterdir():
            path.unlink()
        self.assertFalse(self.publish(0))
        self.assertFalse((self.state / "cockpit.json").exists())

    def test_the_sources_are_in_the_documents_key_list(self):
        self.assertEqual(sb.COCKPIT_DOCUMENT_KEYS, ("schema_version", "generation", "produced_at", "sources", "cards"))


class TestScrubFailsClosed(Fixture):
    def broken(self):
        return mock.patch.object(sb, "cockpit_text", side_effect=RuntimeError("secret ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 inside"))

    def test_s9_the_verb_prints_one_line_and_exits_4_with_no_document_and_no_traceback(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}), self.broken(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sb.cmd_cockpit(argparse.Namespace(json=True), lambda: T0)
        self.assertEqual(code, 4)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("ghp_", err.getvalue(), "the error text is not echoed: it could hold what the scrub meant to remove")

    def test_s9_the_daemon_keeps_the_last_good_file_when_the_scrub_fails(self):
        sb._STATE_LAST.clear()
        feed = self.state / "cockpit.json"
        engine = mock.Mock(state_dir=self.state, now=lambda: T0)
        with mock.patch("time.monotonic", lambda: 0.0):
            self.assertTrue(sb.publish_cockpit(engine))
        good = feed.read_bytes()
        with mock.patch("time.monotonic", lambda: 500.0), self.broken():
            with self.assertRaises(sb.CockpitWithheld):
                sb.publish_cockpit(engine)
        self.assertEqual(feed.read_bytes(), good)


class TestScrubPatterns(Fixture):
    """The widened scrub (best effort). One sample per alternative of `_COCKPIT_SECRET`; removing an alternative lets its sample through."""
    SECRET_SAMPLES = [
        "https://bob:pw@git.example.com/acme-org/widget-service.git",
        "postgres://db.internal:5432/widgets",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Basic dXNlcjpwYXNz",
        "Bearer abc.def-ghi",
        "X-Api-Key: abcd1234",
        "db_password=hunter2",
        "ops@example.com",
        "10.0.0.5:8080",
        "sk_live_" + "a1B2" * 6,
        "QUJDREVGR0hJSktMTU5PUA==",
        "Qa1" * 10,
    ]
    EXTRA = ["mongodb+srv://cluster0.example.net/app", "mysql://root@localhost/app", "redis://cache:6379/0", "http://user@host/x", "login=bob", "api_key=zzz", "Cookie: sid",
             "AIza" + "A" * 35, "SG." + "a" * 20 + "." + "b" * 20, "npm_" + "a" * 32, "glpat-" + "a" * 20, "hf_" + "a" * 32, "ya29." + "a" * 25, "ASIA" + "A" * 16, "rk_test_" + "a" * 20,
             "Proxy-Authorization: Basic abc", "-----BEGIN CERTIFICATE-----", "-----BEGIN OPENSSH PRIVATE KEY-----"]

    def test_every_alternative_has_a_sample(self):
        self.assertEqual(len(TestCapsAndScrub.alternatives(sb._COCKPIT_SECRET)), len(self.SECRET_SAMPLES))

    def test_each_sample_is_replaced_whole_in_a_title_a_note_and_a_reason(self):
        for sample in self.SECRET_SAMPLES + self.EXTRA:
            with self.subTest(sample=sample):
                self.assertEqual(sb.cockpit_text("see %s now" % sample, 300), "see [removed] now")
                fleet_state(self.state, decisions=[entry("issue", ask=31, title="see %s now" % sample), entry("notice", number=7, title="see %s now" % sample), entry("captain", number=9, title="t", note="see %s now" % sample)])
                doc = build(self.state)
                self.assertEqual(doc["cards"]["decisions"][0]["title"], "see [removed] now")
                self.assertEqual(doc["cards"]["heads_up"][0]["note"], "see [removed] now")
                self.assertEqual(doc["cards"]["held_prs"][0]["reason"], "see [removed] now")

    def test_each_alternative_removed_lets_its_own_sample_through(self):
        parts = TestCapsAndScrub.alternatives(sb._COCKPIT_SECRET)
        for index, sample in enumerate(self.SECRET_SAMPLES):
            with self.subTest(sample=sample):
                self.assertEqual(sb._COCKPIT_SECRET.sub("[removed]", sample), "[removed]")
                mutant = re.compile("|".join(parts[:index] + parts[index + 1:]))
                self.assertNotEqual(mutant.sub("[removed]", sample), "[removed]", "the sample is caught by another alternative too: the test would not notice this one removed")

    def test_ordinary_titles_are_kept(self):
        for title in ("Choose the retention for widget exports", "Which region first?", "Rotate the API keys next quarter", "Reviewer needed on #41", "ExportRetentionPolicyConfiguration"):
            self.assertEqual(sb.cockpit_text(title, 300), title)

    def test_every_format_character_is_removed_from_every_string(self):
        import unicodedata
        formats = [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Cf"]
        self.assertTrue(len(formats) > 100)
        self.assertEqual([hex(ord(c)) for c in formats if sb.cockpit_text("a%sb" % c, 20) != "ab"], [])
        for ch in ("\u202e", "\u200b", "\u2066", "\u2069"):
            self.assertEqual(sb.cockpit_text("pay%sload" % ch, 20), "payload", repr(ch))
        self.assertEqual(sb.cockpit_text("gh\u200bp_" + "A" * 20, 50), "[removed]", "a token split by a zero-width character is joined and removed")
        fleet_state(self.state, decisions=[entry("issue", ask=40, title="a\u202eb", agent="c\u200bd")])
        text = json.dumps(build(self.state), ensure_ascii=False)
        self.assertEqual([c for c in text if unicodedata.category(c) == "Cf"], [])


class TestFileRead(Fixture):
    def test_a_symlinked_feed_file_is_not_followed(self):
        real = self.state / "real.json"
        real.write_text(json.dumps(build(self.state)))
        (self.state / "cockpit.json").symlink_to(real)
        self.assertEqual(sb.cockpit_current(self.state / "cockpit.json"), (0, None))

    def test_an_oversize_feed_file_is_not_read(self):
        doc = build(self.state)
        doc["pad"] = "x" * 1_100_000
        (self.state / "cockpit.json").write_text(json.dumps(doc))
        self.assertEqual(sb.cockpit_current(self.state / "cockpit.json"), (0, None))

    def test_the_writer_does_not_write_through_a_symlinked_temp_name(self):
        sb._STATE_LAST.clear()
        victim = self.root / "victim"
        victim.write_text("keep")
        (self.state / ("cockpit.json.%d.tmp" % os.getpid())).symlink_to(victim)
        with mock.patch("time.monotonic", lambda: 0.0):
            with self.assertRaises(OSError):
                sb.publish_cockpit(mock.Mock(state_dir=self.state, now=lambda: T0))
        self.assertEqual(victim.read_text(), "keep")


class TestDocumentTable(unittest.TestCase):
    def test_s9_the_document_keys_and_sources_are_on_the_docs_table_in_both_directions(self):
        rows = {}
        for line in DOC.read_text().splitlines():
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0] in ("document", "sources"):
                rows[cells[0]] = tuple(k.strip().strip("`") for k in cells[1].split(","))
        self.assertEqual(rows, {"document": sb.COCKPIT_DOCUMENT_KEYS, "sources": sb.COCKPIT_SOURCES})

    def test_the_docs_say_what_the_scrub_does_not_promise_and_where_freshness_lives(self):
        text = DOC.read_text()
        for phrase in ("best effort", "Q3", "status 3", "status 4", "180 seconds", "produced_at"):
            self.assertIn(phrase, text)
        self.assertRegex(text, r"(?i)title text is outside the scrub")


if __name__ == "__main__":
    unittest.main()
