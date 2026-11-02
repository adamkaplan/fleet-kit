"""#1901: evidence over the ledger's limit (500 characters, after white space is collapsed and the secrets redacted) is REFUSED, never cut.
build(host) gives the tests the fixtures of bin/test-switchboard. The tests RUN the real commands and the real relay with fakes."""
import contextlib
import io
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb, class_source_fn
from support.paths import BIN, REPO

sys.path.insert(0, str(BIN))
import cloud_relay_daemon as relay  # noqa: E402

# ONE rule: the whole run loads plan_ledger more than once, so never use a module this file imported itself.
# SBP(): the module bin/fleet-switchboard (sb) uses (sb-side tests, the item helpers' LedgerError, the patch target, constants);
# RP(): the module cloud_relay_daemon uses (only the relay-side ledger read).
SBP = lambda: sb.plan_ledger
RP = lambda: relay.plan_ledger

LIMIT = SBP().EVIDENCE_LIMIT
LONG, WHOLE = "e" * 501, "e" * 500
CHECK = re.compile(r"the evidence is 501 characters and the limit is 500: nothing was recorded; put the long text in one comment or a file and give the link, or shorten it")
RPR, RREPO, RHEAD = "acme-org/widget-service#7", "acme-org/widget-service", "a" * 40
CODER, TESTER = "widget-api-coder", "widget-api-tester"


def ledger_bytes(state):
    path = Path(state) / SBP().LEDGER_FILE
    return path.read_bytes() if path.exists() else b""


class RelayBackend:
    def __init__(self):
        self.audits = []

    def live_head(self, pr):
        return RHEAD

    def plan(self, pr):
        return {"source": "acme-org/widget-service#1", "hash": "sha256:" + "b" * 64, "scenarios": {"S1": {"evaluator": "tester", "claim": "pr"}}}

    def pr_facts(self, pr):
        return {"author": None, "notice_senders": ()}

    def evaluators(self, evaluator):
        return [TESTER]

    def holder_of(self, repo, issue):
        return CODER

    def pr_issues(self, pr):
        return {1}

    def gates(self, pr):
        return {"ready": False}

    def report(self, pod, args):
        pass

    def audit(self, kind, pr, detail):
        self.audits.append((kind, detail))


class RelayHub:
    def __init__(self):
        self.queue, self.results, self.n = [], {}, 0

    def submit(self, pod, command, cid, **args):
        self.n += 1
        rid = "%032x" % self.n
        self.queue.append({"id": rid, "pod": pod, "repo": RREPO, "issue": 1, "call": {"call_id": cid, "command": command, "pr": RPR, "args": args, "head": RHEAD}})
        return rid

    def fetch(self):
        return {"requests": list(self.queue[:10])}

    def post(self, rid, body):
        self.results[rid] = body
        self.queue = [q for q in self.queue if q["id"] != rid]


def source_copy(old, new, module=relay):
    """The real cloud_relay_daemon source with ONE replacement, applied exactly once, as a fresh module."""
    src = (BIN / "cloud_relay_daemon.py").read_text()
    assert src.count(old) == 1, old
    mod = types.ModuleType("cloud_relay_daemon_mut1901")
    mod.__file__ = str(BIN / "cloud_relay_daemon.py")
    exec(compile(src.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    return mod


def build():
    host = HOST   # set by bin/test-switchboard
    items = __import__("test_cases_09").TestItemVerdicts

    class LocalBase(host.LedgerBase):
        def prove_text(self, text, *scenarios):
            return self.run_as("coder", "prove", *(scenarios or ("S1",)), "--pr", self.PR, "--evidence", text)

        def verdict_text(self, text, *more):
            return self.run_as("tst", "verdict", "S2", "--pr", self.PR, "--as", "tester", "--result", "pass", "--evidence", text, *more)

        def verdict_all_text(self, text):
            return self.run_as("tst", "verdict", "--pr", self.PR, "--as", "tester", "--all", "--result", "pass", "--head", host.LEDGER_HEAD, "--evidence", text)

        def state_listing(self):
            return sorted((p.name, p.read_bytes()) for p in self.world.state.iterdir() if p.is_file() and p.name.startswith("plan-ledger"))

        def refused(self, call, text):
            before = self.state_listing()
            code, out, err = call(text)
            self.assertEqual(code, 2, "the evidence over the limit must be refused and nothing recorded: the command exited %s, not 2" % code)
            self.assertRegex(err, CHECK, "the refusal line must name the 501 characters and the limit of 500: " + err)
            self.assertIn("501", err)
            self.assertIn("500", err)
            self.assertEqual(self.state_listing(), before, "a refusal writes no byte: no entry, no head pointer")

        def recorded(self, call, text, kind):
            code, out, err = call(text)
            self.assertEqual(code, 0, out + err)
            got = [e for e in self.ledger() if e["kind"] == kind][-1]["evidence"]
            self.assertEqual(got, text)
            self.assertEqual(len(got), 500)
            self.assertFalse(got.endswith("\u2026"))

    class TestEvidenceLimitLocal(LocalBase):
        """S2: prove (one and two scenarios), verdict, verdict --all: 501 refused with nothing written, 500 recorded whole."""

        def test_prove_one_scenario(self):
            self.refused(self.prove_text, LONG)
            self.recorded(self.prove_text, WHOLE, "proof")

        def test_prove_two_scenarios_records_none_when_refused(self):
            self.refused(lambda t: self.prove_text(t, "S1", "S2"), LONG)
            self.assertEqual(self.ledger(), [])
            self.recorded(lambda t: self.prove_text(t, "S1", "S2"), WHOLE, "proof")
            self.assertEqual({e["scenario"] for e in self.ledger()}, {"S1", "S2"})

        def test_verdict(self):
            self.prove_text("ran it", "S2")
            self.refused(self.verdict_text, LONG)
            self.recorded(self.verdict_text, WHOLE, "verdict")

        def test_verdict_all(self):
            self.prove_text("ran it", "S2")
            self.refused(self.verdict_all_text, LONG)
            self.recorded(self.verdict_all_text, WHOLE, "verdict")

    class TestEvidenceLimitItems(items):
        """S2: item-prove and item-verdict (the fixture of TestItemVerdicts, its own tests not run here)."""

        def item_prove(self, text):
            with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx()), mock.patch.object(sb, "identify_caller", lambda *a: "c1"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    return sb.cmd_item_prove(self.args(scenario=["S1"], evidence=text), None, self.now), None
                except (sb.LedgerCommandError, SBP().LedgerError) as err:
                    return None, str(err)

        def one_of(self, call):
            before = (self.state / SBP().LEDGER_FILE).read_bytes() if (self.state / SBP().LEDGER_FILE).exists() else b""
            code, why = call(LONG)
            after = (self.state / SBP().LEDGER_FILE).read_bytes() if (self.state / SBP().LEDGER_FILE).exists() else b""
            self.assertIsNone(code)
            self.assertRegex(why, CHECK)
            return before, after

        def test_item_prove_refuses_501_and_records_500(self):
            self.register()
            before = (self.state / SBP().LEDGER_FILE).read_bytes()
            code, why = self.item_prove(LONG)
            self.assertIsNone(code)
            self.assertRegex(why, CHECK)
            self.assertEqual((self.state / SBP().LEDGER_FILE).read_bytes(), before)
            code, why = self.item_prove(WHOLE)
            self.assertEqual((code, why), (sb.EXIT_OK, None))
            self.assertEqual([e["evidence"] for e in self.entries() if e["kind"] == "proof"], [WHOLE])

        def test_item_verdict_refuses_501_and_records_500(self):
            self.register()
            before = (self.state / SBP().LEDGER_FILE).read_bytes()
            try:
                code, why = self.verdict("t1", evidence=LONG)
            except SBP().LedgerError as err:   # the dispatch writes it with exit 2
                code, why = None, str(err)
            self.assertIsNone(code)
            self.assertRegex(why, CHECK)
            self.assertEqual((self.state / SBP().LEDGER_FILE).read_bytes(), before)
            code, why = self.verdict("t1", evidence=WHOLE)
            self.assertEqual((code, why), (sb.EXIT_OK, None))
            self.assertEqual([e["evidence"] for e in self.verdicts()], [WHOLE])

    for cls in (TestEvidenceLimitItems,):   # the inherited tests of the fixture are not this class's business
        for attr in [a for a in dir(items) if a.startswith("test_")]:
            setattr(cls, attr, None)

    class TestEvidenceLimitCounting(LocalBase):
        """S4: the length counted is that of the redacted, collapsed text."""

        def test_white_space_and_a_redacted_secret_are_counted_as_before(self):
            spaced = "x" * 200 + " " * 240 + "y" * 200            # 640 raw, 401 collapsed
            self.assertEqual(self.prove_text(spaced)[0], 0)
            self.assertEqual(len(self.ledger()[-1]["evidence"]), 401)
            secret = "ghp_" + "0123456789abcdefABCDEF0123" * 2   # 56 characters, redacted to a mark of 10
            text = "token %s " % secret + "z" * (520 - len("token %s " % secret))
            self.assertEqual(len(text), 520)
            code, out, err = self.prove_text(text, "S2")
            self.assertEqual(code, 0, err)
            got = self.ledger()[-1]["evidence"]
            self.assertEqual(len(got), 520 - 46)
            self.assertNotIn(secret, got)
            self.refused(self.prove_text, "w " * 250 + "w")      # 501 after the collapse

    class TestEvidenceLimitRelay(unittest.TestCase):
        """S3: the pod relay with its fake backend."""
        mod = relay

        def setUp(self):
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.state, self.backend, self.hub = self.tmp.name, RelayBackend(), RelayHub()
            self.ex = self.fresh()

        def fresh(self):
            return self.mod.Executor(self.state, self.backend, lambda: 1000)

        def call(self, pod, command, cid, text, ex=None, result="pass"):
            rid = self.hub.submit(pod, command, cid, scenario="S1", result=result, evidence=text)
            self.mod.pass_once(ex or self.ex, self.hub.fetch, self.hub.post)
            return self.hub.results[rid]

        def refusals(self):
            return [a for a in self.backend.audits if a[0] == "relay.refused"]

        def test_501_is_refused_400_and_500_recorded_for_prove_and_verdict(self):
            for pod, command in ((CODER, "prove"), (TESTER, "verdict")):
                before, audits = ledger_bytes(self.state), len(self.refusals())
                got = self.call(pod, command, "r-" + command, LONG)
                self.assertEqual((got["status"], got["record"]), (400, None),
                                 "the relay must refuse the evidence over the limit with 400 and record nothing: it answered %s" % ((got["status"], got["record"]),))
                self.assertRegex(got["reason"], CHECK)
                self.assertEqual(ledger_bytes(self.state), before)
                self.assertEqual(len(self.refusals()), audits + 1)
                got = self.call(pod, command, "r-" + command, WHOLE)   # the same call id after the refusal
                self.assertEqual(got["status"], 200, got)
            self.assertEqual([e["evidence"] for e in RP().read_ledger(self.state)[0]], [WHOLE, WHOLE])
            self.assertEqual(len(self.refusals()), 2)

        def test_replay_with_a_longer_text_is_409(self):
            self.assertEqual(self.call(CODER, "prove", "X", WHOLE)["status"], 200)
            before = ledger_bytes(self.state)
            got = self.call(CODER, "prove", "X", LONG)
            self.assertEqual((got["status"], got["reason"], got["record"]), (409, "call_id reused for a different call", None))
            self.assertEqual(ledger_bytes(self.state), before)

        def test_replay_with_a_longer_text_and_the_index_gone_is_400_and_writes_nothing(self):
            self.assertEqual(self.call(CODER, "prove", "X", WHOLE)["status"], 200)
            (Path(self.state) / self.mod.INDEX_FILE).unlink()
            before, audits = ledger_bytes(self.state), len(self.refusals())
            got = self.call(CODER, "prove", "X", LONG, ex=self.fresh())
            self.assertEqual((got["status"], got["record"]), (400, None))
            self.assertRegex(got["reason"], CHECK)
            self.assertEqual(ledger_bytes(self.state), before)
            self.assertEqual(len(self.refusals()), audits + 1)

    class TestEvidenceLimitMutants(unittest.TestCase):
        """S5: three mutants on source copies, each applied exactly once, control first; each fails the named check with its own text."""

        def relay_case(self, mod):
            case = TestEvidenceLimitRelay("test_501_is_refused_400_and_500_recorded_for_prove_and_verdict")
            case.mod = mod
            return case

        def run_case(self, case):
            case.setUp()
            try:
                getattr(case, case._testMethodName)()
            finally:
                case.doCleanups()

        def local_case(self, evidence_fn):
            case = TestEvidenceLimitLocal("test_prove_one_scenario")
            case.setUp()
            try:
                with mock.patch.object(sb, "_evidence", evidence_fn):
                    case.test_prove_one_scenario()
            finally:
                case.doCleanups()

        def test_controls_pass_first(self):
            self.run_case(self.relay_case(relay))
            self.run_case(TestEvidenceLimitRelay("test_replay_with_a_longer_text_is_409"))
            self.run_case(TestEvidenceLimitRelay("test_replay_with_a_longer_text_and_the_index_gone_is_400_and_writes_nothing"))
            self.local_case(sb._evidence)
            self.assertEqual(SBP().bounded_text("a" * 500, 500, "evidence"), "a" * 500)

        def mutated_evidence(self):
            src = class_source_fn("_evidence")
            old = 'plan_ledger.bounded_text(redact_command_text(args.evidence), plan_ledger.EVIDENCE_LIMIT, "evidence")'
            self.assertEqual(src.count(old), 1)
            scope = dict(vars(sb))
            exec(src.replace(old, "plan_ledger.clip(redact_command_text(args.evidence), plan_ledger.EVIDENCE_LIMIT)"), scope)
            return scope["_evidence"]

        def test_mutant_evidence_clips_again(self):
            self.local_case(sb._evidence)   # control first: the unmutated copy passes the same check
            with self.assertRaises(AssertionError) as ctx:
                self.local_case(self.mutated_evidence())
            self.assertIn("the evidence over the limit must be refused and nothing recorded: the command exited 0, not 2", str(ctx.exception))

        def test_mutant_relay_write_clips_again(self):
            mod = source_copy("        evidence = _checked_evidence(args)\n",
                              '        evidence = plan_ledger.clip(str(args.get("evidence") or "").strip(), plan_ledger.EVIDENCE_LIMIT)\n')
            self.run_case(self.relay_case(relay))   # control first: the unmutated relay passes the same check
            self.assertEqual(mod.__name__, "cloud_relay_daemon_mut1901")
            with self.assertRaises(AssertionError) as ctx:
                self.run_case(self.relay_case(mod))
            self.assertIn("the relay must refuse the evidence over the limit with 400 and record nothing: it answered (200,", str(ctx.exception))

        def test_mutant_bounded_text_refuses_at_the_limit_itself(self):
            real = SBP().bounded_text
            mutant = lambda text, limit, what: real(text, limit - 1, what)
            self.local_case(sb._evidence)   # control first: the real bounded_text passes the same check
            with mock.patch.object(SBP(), "bounded_text", mutant):
                with self.assertRaises(AssertionError) as ctx:
                    self.local_case(sb._evidence)
            self.assertIn("the refusal line must name the 501 characters and the limit of 500: ", str(ctx.exception))
            self.assertIn("the limit is 499", str(ctx.exception))   # the refusal line names 499, not 500

    class TestEvidenceLimitUnchanged(LocalBase):
        """S7: the CI records and the waiver reason are cut or recorded as before."""
        extra_config = {"plan_gates": {"repos": {"acme/app": {"mode": "enforce"}}}}

        def test_gates_ci_record_is_recorded_whole(self):
            self.configure(evaluators={"ci": ["switchboard"], "reviewer": ["rev"], "tester": ["tst"]})
            code, out, err = self.run_as(None, "gates", "ci-record", self.PR)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.ledger()[0]["evidence"], "CI green at %s: every check run complete and passing" % host.LEDGER_HEAD[:12])

        def test_a_waiver_reason_of_201_is_still_cut_to_200(self):
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
            self.configure()
            code, out, err = self.run_as("cos", "waive", self.PR, "--gates", "G1", "--reason", "r" * 201)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(self.ledger()[-1]["reason"]), 200)

    class TestEvidenceLimitItemCi(items):
        """S7: the item CI record with 30 check names is recorded whole, above 500 characters."""

        def test_item_ci_record_with_thirty_names(self):
            names = ["check-%02d-%s" % (n, "q" * 12) for n in range(30)]
            runs = [dict(self.RUN, name=n) for n in names]
            real = self.ctx

            def ctx(author="human", own=True, item=None):
                got = real(author, own, item)
                got.config = {"plan_gates": {"repos": {self.REPO: {"item_checks": [{"name": n, "app": "github-actions"} for n in names]}}}}
                return got
            self.ctx = ctx
            self.assertEqual(self.ci(runs)[0], sb.EXIT_OK)
            text = self.verdicts()[0]["evidence"]
            self.assertGreater(len(text), 500)
            self.assertTrue(text.endswith(names[-1]))

    for cls in (TestEvidenceLimitItemCi,):
        for attr in [a for a in dir(items) if a.startswith("test_") and a != "test_item_ci_record_with_thirty_names"]:
            setattr(cls, attr, None)

    class TestEvidenceLimitDocsAndCode(unittest.TestCase):
        """S6, U5, U6."""

        def test_the_prove_row_states_the_limit_the_refusal_and_the_advice(self):
            text = (REPO / "docs" / "switchboard.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("redacted and clipped"), 0)
            self.assertEqual(text.count("Evidence is redacted; evidence over 500 characters (after white space is collapsed) is refused, never cut: link one comment that holds it, or shorten it."), 1)

        def test_bounded_text_reads_no_file_calls_no_process_and_the_file_is_short(self):
            import ast
            import inspect
            tree = ast.parse(inspect.getsource(SBP().bounded_text))
            called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "") for n in ast.walk(tree) if isinstance(n, ast.Call)}
            self.assertFalse(called & {"open", "read_text", "run", "Popen", "check_output", "system", "read_bytes"}, called)
            self.assertLess(len((BIN / "plan_ledger.py").read_text().splitlines()), 1500)
            self.assertEqual(SBP().clip("a" * 501, 500), "a" * 499 + "\u2026")
            self.assertEqual((SBP().EVIDENCE_LIMIT, SBP().FIELD_LIMIT), (500, 200))

        def test_this_file_makes_no_size_measure(self):
            text = Path(__file__).read_text()
            for needle in ("git" + " diff", "num" + "stat", "origin/" + "integration"):
                self.assertEqual(text.count(needle), 0, needle)

    return (TestEvidenceLimitLocal, TestEvidenceLimitItems, TestEvidenceLimitCounting, TestEvidenceLimitRelay, TestEvidenceLimitMutants,
            TestEvidenceLimitUnchanged, TestEvidenceLimitItemCi, TestEvidenceLimitDocsAndCode)
