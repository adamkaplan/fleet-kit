"""The approval gate G0c reads the plan.approved events and blocks a merge without one (ask #1735).

HOST is bin/test-switchboard (set by its loader line). The audit is a FIXTURE in a temp state directory: a live audit.jsonl and one rotated audit-*.jsonl.gz.
The command tests borrow the merge-gate fixture of the host (acme/app#9 under the plan acme/app#7); the reader and the pure rows use acme-org/widget-api and acme/api#2.
The mutants are the real source with one line replaced: each is applied, asserted applied, and must fail the scenario check with an anchored text, the unmutated source passing it.
"""
import gzip
import inspect
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import plan_ledger  # noqa: E402

HOST = None   # set by bin/test-switchboard
ISSUE, OTHER = "acme/api#2", "acme/api#3"
H_NEW, H_OLD = "sha256:" + "a" * 64, "sha256:" + "b" * 64
PART = REPO / "bin" / "switchboard" / "approval_gate.py"


def event(issue, plan_hash, where="subject"):
    """One audit line of a plan.approved event: the issue in the subject only (before 30 Oct), or in the detail too."""
    detail = {"plan_hash": plan_hash, "from": "cos", "to": "orch"}
    if where == "both":
        detail["issue"] = issue
    elif where == "detail":
        return {"event": "plan.approved", "subject": None, "detail": dict(detail, issue=issue)}
    return {"event": "plan.approved", "subject": issue, "detail": detail}


def audit_dir(live, rotated=(), broken=False):
    """A temp state directory: audit.jsonl with `live`, audit-2026-10-20.jsonl.gz with `rotated` (or a gz file that is not one)."""
    folder = Path(tempfile.mkdtemp(prefix="approval-"))
    (folder / "audit.jsonl").write_text("".join(json.dumps(e) + "\n" for e in live))
    if rotated or broken:
        with gzip.open(str(folder / "audit-2026-10-20.jsonl.gz"), "wb") as handle:
            handle.write(b"this is not a gzip stream" if broken else "".join(json.dumps(e) + "\n" for e in rotated).encode())
        if broken:
            (folder / "audit-2026-10-20.jsonl.gz").write_bytes(b"this is not a gzip stream")
    return folder


def judge(events_fn, state_fn, folder, issue, current):
    """The row the gate would show for `issue` at `current`, read from the fixture directory."""
    return state_fn(events_fn(folder, issue, current), issue, current)


def expect(got, want, what):
    if got != want:
        raise AssertionError("approval gate: %s: got %s, want %s" % (what, got, want))


def scenario_rows(events_fn, state_fn):
    """S2 and S4 on the fixture: the five rows, the subject-only event, the event in a rotated audit, the unreadable audit."""
    live = [event(ISSUE, H_NEW, "both"), event(OTHER, H_OLD)]
    expect(judge(events_fn, state_fn, audit_dir(live), ISSUE, H_NEW)[0], "ok", "an event at the current hash")
    old = judge(events_fn, state_fn, audit_dir([event(ISSUE, H_OLD, "both")]), ISSUE, H_NEW)
    expect(old[0], "missing", "events only at an older hash")
    expect((H_NEW in old[1], H_OLD in old[1]), (True, True), "the older-hash detail names both hashes")
    expect(judge(events_fn, state_fn, audit_dir([]), ISSUE, H_NEW)[0], "missing", "no event at all")
    expect(judge(events_fn, state_fn, audit_dir([event(OTHER, H_NEW)]), ISSUE, H_NEW)[0], "missing", "an event of another issue")
    expect(state_fn([], None, H_NEW)[0], "n/a", "no plan link")
    expect(judge(events_fn, state_fn, audit_dir([event(ISSUE, H_NEW, "subject")]), ISSUE, H_NEW)[0], "ok", "an event whose issue is only in its subject")
    expect(judge(events_fn, state_fn, audit_dir([], [event(ISSUE, H_NEW)]), ISSUE, H_NEW)[0], "ok", "an event only in a rotated compressed audit")
    expect(judge(events_fn, state_fn, audit_dir([event(ISSUE, H_OLD)], [event(ISSUE, H_NEW)]), ISSUE, H_NEW)[0], "ok", "an event at the current hash in a rotated audit")
    bad = judge(events_fn, state_fn, audit_dir([], broken=True), ISSUE, H_NEW)
    expect((bad[0], "could not be read" in bad[1]), ("missing", True), "an unreadable audit")


def source_of(fn):
    return inspect.getsource(fn)


def mutated_state(old, new):
    """approval_state from the real source with `old` replaced by `new` (asserted applied)."""
    text = source_of(plan_ledger.approval_state)
    if old not in text:
        raise AssertionError("mutant not applied: %r" % old)
    space = dict(vars(plan_ledger))
    exec(text.replace(old, new, 1), space)
    return space["approval_state"]


def mutated_events(old, new):
    """approval_events from the real part with `old` replaced by `new` (asserted applied)."""
    text = PART.read_text()
    if old not in text:
        raise AssertionError("mutant not applied: %r" % old)
    space = dict(vars(HOST.sb))
    exec(compile(text.replace(old, new, 1), str(PART), "exec"), space)
    return space["approval_events"]


class TestApprovalGateReader(unittest.TestCase):
    """S2, S4, S7, U2, U3 on a fixture audit."""

    def test_the_rows_of_the_fixture_audit(self):
        scenario_rows(HOST.sb.approval_events, plan_ledger.approval_state)

    def test_the_unmutated_control_passes_the_scenario_check(self):
        scenario_rows(mutated_events("count % 500 == 0", "count % 500 == 0"), mutated_state("seen:", "seen:"))

    def test_mutant_a_missing_approval_read_as_ok_is_caught(self):
        state = mutated_state('    return "missing", "no plan.approved event for', '    return "ok", "no plan.approved event for')
        with self.assertRaisesRegex(AssertionError, r"^approval gate: no event at all: got ok, want missing$"):
            scenario_rows(HOST.sb.approval_events, state)

    def test_mutant_only_detail_issue_read_is_caught(self):
        events = mutated_events('want in (str(entry.get("subject") or "").lower(), str(detail.get("issue") or "").lower())', 'want == str(detail.get("issue") or "").lower()')
        with self.assertRaisesRegex(AssertionError, r"^approval gate: an event whose issue is only in its subject: got missing, want ok$"):
            scenario_rows(events, plan_ledger.approval_state)

    def test_mutant_the_hash_ignored_is_caught(self):
        state = mutated_state("if current_hash and current_hash in seen:", "if seen:")
        with self.assertRaisesRegex(AssertionError, r"^approval gate: events only at an older hash: got ok, want missing$"):
            scenario_rows(HOST.sb.approval_events, state)

    def test_the_reader_stops_at_the_first_file_with_the_current_hash_and_at_its_bounds(self):
        read = HOST.sb.approval_events
        early = audit_dir([event(ISSUE, H_NEW)], broken=True)   # the live audit holds the current hash: the unreadable rotated audit is never opened
        self.assertEqual(read(early, ISSUE, H_NEW), [{"plan_hash": H_NEW}])
        folder = audit_dir([event(ISSUE, H_OLD)], [event(ISSUE, H_OLD)])
        self.assertIsNone(read(folder, ISSUE, H_NEW, files=1), "more files than the bound")
        ticks = iter(range(0, 1000, 5))
        self.assertIsNone(read(folder, ISSUE, H_NEW, clock=lambda: next(ticks), seconds=2), "a read past its time bound")
        self.assertEqual(read(folder, ISSUE, H_NEW), [{"plan_hash": H_OLD}, {"plan_hash": H_OLD}])

    def test_the_decision_is_one_short_pure_function_and_the_reader_reads_no_github_and_writes_nothing(self):
        body = source_of(plan_ledger.approval_state)
        self.assertLessEqual(len(body.splitlines()), 30)
        for word in ("open(", "Path(", "read_text", "subprocess", "runner"):
            self.assertNotIn(word, body)
        part = PART.read_text()
        reader = part[part.index("def approval_events"):part.index("def approval_issue")]
        for word in ("gh(", "runner", "subprocess", ".write", "AuditLog", "'w'", '"w"', "read_pr", "_api"):
            self.assertNotIn(word, reader)
        self.assertNotIn("plan_hash(", part, "the current hash is the plan's own, read through plan_check.plan_hash by the plan read")
        me = Path(__file__).read_text()
        self.assertEqual(me.count("git " + "diff"), 0)
        self.assertEqual(me.count("git" + " merge-base"), 0)


class TestApprovalGateCommands(unittest.TestCase):
    """S2, S3, S5, S6 through the commands, on the merge-gate fixture of the host (acme/app#9, plan acme/app#7)."""

    def setUp(self):
        def route(me, argv, **kwargs):   # the plan issue and its comments come from the ledger fixture, the rest from the merge-gate fake
            joined = " ".join(argv)
            if argv[0] == HOST.FAKE_GH and re.search(r"/issues/7(\s|$|/)", joined):
                return HOST.LedgerBase.route(me, argv, **kwargs)
            return HOST.TestMergeGateCommand.route(me, argv, **kwargs)
        host_class = type("ApprovalHost", (HOST.TestMergeGateCommand,), {"runTest": lambda self: None, "route": route})
        self.h = host_class("runTest")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.args = types.SimpleNamespace(pr=self.h.PR)
        self.h.pr.update(headRefOid=HOST.MG_HEAD)

    def enforce(self, gates=("G0a", "G0c", "G1")):
        self.h.extra_config = {"merge_gate": {"repos": {"acme/app": {}}}, "plan_gates": {"repos": {"acme/app": {"mode": "enforce", "enforce_gates": list(gates)}}}}
        self.h.configure()

    def current(self):
        return self.h.gates()[1]["plan_hash"]

    def approve(self, plan_hash, issue="acme/app#7", where="subject"):
        with open(str(self.h.world.state / "audit.jsonl"), "a") as handle:
            handle.write(json.dumps(event(issue, plan_hash, where)) + "\n")

    def refusal(self):
        return HOST.sb.approval_refusal(self.args, self.h.route, self.h.world.clock.now)

    def row(self):
        return self.h.row(self.h.gates()[1], "G0c")

    def test_the_gates_row_is_untracked_and_nothing_refuses_where_the_repo_does_not_name_g0c(self):
        report = self.h.gates()[1]
        self.assertEqual((self.h.row(report, "G0c")["status"], "G0c" in report["untracked"]), ("untracked", True))
        self.assertIsNone(self.refusal())
        self.enforce(("G0a", "G1"))
        self.assertEqual(self.row()["status"], "untracked")
        self.assertIsNone(self.refusal())

    def test_the_gates_row_where_the_repo_enforces_it(self):
        self.enforce()
        current = self.current()
        self.assertEqual(self.row()["status"], "missing")
        self.approve("sha256:" + "c" * 64)
        row = self.row()
        self.assertEqual(row["status"], "missing")
        self.assertIn(current, row["detail"])
        self.assertIn("sha256:" + "c" * 64, row["detail"])
        self.approve(current)
        self.assertEqual(self.row()["status"], "ok")

    def test_the_merge_gate_refuses_a_base_of_the_integration_branch_and_of_the_default_branch_and_passes_with_an_event(self):
        self.enforce()
        current = self.current()
        for base in ("integration", "main"):
            self.h.fake.base = base
            text = self.refusal()
            self.assertTrue(text and self.h.PR in text, base)
            self.assertIn("acme/app#7", text)
            self.assertIn(current, text)
            self.assertIn("the Chief of Staff", text)
            code, out, err = self.h.run_as("coder", "merge-gate", self.h.PR, "--check", "--json")
            self.assertEqual(code, 10, base)
            self.assertIn(current, json.loads(out)["reason"])
        self.approve(current)
        for base in ("integration", "main"):
            self.h.fake.base = base
            self.assertIsNone(self.refusal(), base)
        self.h.fake.base, self.h.fake.default = "feature", "main"
        self.assertIsNone(self.refusal())

    def test_a_waiver_passes_it_is_audited_and_listed_and_a_new_head_voids_it(self):
        self.enforce()
        self.assertTrue(self.refusal())
        code, out, err = self.h.run_as("cos", "waive", self.h.PR, "--gates", "G0c", "--reason", "the owner decided to ship it before the approval is recorded")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.refusal())
        self.assertEqual(len(self.h.audit_lines("plan.approval.waived")), 1)
        self.assertIn("G0c", json.dumps(self.h.audit_lines("plan.approval.waived")[0]))
        self.assertIn("G0c", json.dumps([e for e in self.h.ledger() if e.get("kind") == "waiver"]))   # the waiver list is the ledger's waiver entries
        self.h.pr.update(headRefOid="c" * 40)
        self.h.fake.head = "c" * 40
        self.assertTrue(self.refusal())


