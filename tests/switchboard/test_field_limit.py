"""#1905: a proof's command or a waiver's reason over the ledger's field limit (200 characters, after the redaction and the collapse of white space)
is REFUSED, never cut. build() gives the tests the fixtures of bin/test-switchboard. The tests RUN the real commands and the real relay with fakes."""
import contextlib
import io
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb, class_source_fn
from support.paths import BIN, REPO

sys.path.insert(0, str(BIN))
# ONE rule: never use a plan_ledger module this file imported itself (the whole run loads it more than once).
# sb.plan_ledger: the module of bin/fleet-switchboard (sb-side tests: caught LedgerError, ledger file, claim_line, limits, bounded_text).
# relay.plan_ledger: only the relay-side ledger read (test_..relay..), where cloud_relay_daemon is the code under test.
relay_pl = lambda: fl_relay.plan_ledger
import cloud_relay_daemon as fl_relay  # noqa: E402
import test_evidence_limit as fl_ev  # noqa: E402  (the fakes of ask #1901's relay tests)

FL_LONG, FL_WHOLE = "c" * 201, "c" * 200
FL_RLONG, FL_RWHOLE = "r" * 201, "r" * 200


def fl_check(what, n=201):
    return re.compile(r"the %s is %d characters and the limit is 200: nothing was recorded; put the long text in one comment or a file and give the link, or shorten it" % (what, n))


def build():
    host = HOST   # set by bin/test-switchboard
    items = __import__("test_cases_09").TestItemVerdicts

    class FieldBase(host.LedgerBase):
        extra_config = {"plan_gates": {"repos": {"acme/app": {"mode": "enforce"}}}}

        def prove_cmd(self, text, *scenarios, evidence="ran it"):
            return self.run_as("coder", "prove", *(scenarios or ("S1",)), "--pr", self.PR, "--evidence", evidence, "--command", text)

        def waive_reason(self, text):
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
            self.configure()
            return self.run_as("cos", "waive", self.PR, "--gates", "G1", "--reason", text)

        def state_snapshot(self):
            """Every file of the state directory (the ledger, its head pointer, the audit logs): a refusal changes none."""
            return sorted((str(p.relative_to(self.world.state)), p.read_bytes()) for p in self.world.state.rglob("*") if p.is_file())

        def refused(self, call, text, what):
            before = self.state_snapshot()
            code, out, err = call(text)
            self.assertEqual(code, 2, out + err)
            self.assertRegex(err, fl_check(what))
            self.assertIn("201", err)
            self.assertIn("200", err)
            self.assertEqual(self.state_snapshot(), before, "a refusal writes no byte: no entry, no head pointer, no audit line")

        def recorded(self, call, text, kind, field):
            code, out, err = call(text)
            self.assertEqual(code, 0, out + err)
            entry = [e for e in self.ledger() if e["kind"] == kind][-1]
            self.assertEqual(entry[field], text)
            self.assertEqual(len(entry[field]), 200)
            self.assertFalse(entry[field].endswith("\u2026"))
            return entry

    class TestFieldLimitLocal(FieldBase):
        """S2: prove (one and two scenarios) and waive: 201 refused with nothing written, 200 recorded whole."""

        def test_prove_one_scenario(self):
            self.refused(self.prove_cmd, FL_LONG, "command")
            self.recorded(self.prove_cmd, FL_WHOLE, "proof", "command")

        def test_prove_two_scenarios_records_none_when_refused(self):
            two = lambda t: self.prove_cmd(t, "S1", "S2")
            self.refused(two, FL_LONG, "command")
            self.assertEqual(self.ledger(), [])
            self.recorded(two, FL_WHOLE, "proof", "command")
            self.assertEqual({e["scenario"] for e in self.ledger()}, {"S1", "S2"})

        def test_waive(self):
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
            self.configure()
            self.refused(self.waive_reason, FL_RLONG, "reason")
            self.assertEqual(self.ledger(), [])
            entry = self.recorded(self.waive_reason, FL_RWHOLE, "waiver", "reason")
            self.assertEqual(entry["evidence"], FL_RWHOLE)

    class TestFieldLimitItems(items):
        """S2: item-prove (the fixture of TestItemVerdicts, its own tests not run here)."""

        def item_prove(self, text):
            with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx()), mock.patch.object(sb, "identify_caller", lambda *a: "c1"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    return sb.cmd_item_prove(self.args(scenario=["S1"], command=text), None, self.now), None
                except (sb.LedgerCommandError, sb.plan_ledger.LedgerError) as err:
                    return None, str(err)

        def test_item_prove_refuses_201_and_records_200(self):
            self.register()
            ledger = self.state / sb.plan_ledger.LEDGER_FILE
            before, audits = ledger.read_bytes(), len(self.audit)
            code, why = self.item_prove(FL_LONG)
            self.assertIsNone(code)
            self.assertRegex(why, fl_check("command"))
            self.assertIn("200", why)
            self.assertEqual(ledger.read_bytes(), before)
            self.assertEqual(len(self.audit), audits)
            code, why = self.item_prove(FL_WHOLE)
            self.assertEqual((code, why), (sb.EXIT_OK, None))
            got = [e["command"] for e in self.entries() if e["kind"] == "proof"]
            self.assertEqual(got, [FL_WHOLE])

    for attr in [a for a in dir(items) if a.startswith("test_")]:
        setattr(TestFieldLimitItems, attr, None)

    class TestFieldLimitRelay(unittest.TestCase):
        """S3: the pod relay with its fake backend."""
        mod = fl_relay

        def setUp(self):
            import tempfile
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.state, self.backend, self.hub = self.tmp.name, fl_ev.RelayBackend(), fl_ev.RelayHub()
            self.ex = self.mod.Executor(self.state, self.backend, lambda: 1000)

        def call(self, text):
            args = {"scenario": "S1", "result": "pass", "evidence": "ran it", "command": text}
            rid = self.hub.submit(fl_ev.CODER, "prove", "c-1")
            self.hub.queue[-1]["call"]["args"] = args   # the hub's own keyword `command` is the call's name, so the argument is set here
            self.mod.pass_once(self.ex, self.hub.fetch, self.hub.post)
            return self.hub.results[rid]

        def refusals(self):
            return [a for a in self.backend.audits if a[0] == "relay.refused"]

        def test_201_is_refused_400_and_200_recorded(self):
            got = self.call(FL_LONG)
            self.assertEqual((got["status"], got["record"]), (400, None))
            self.assertRegex(got["reason"], fl_check("command"))
            self.assertEqual(fl_ev.ledger_bytes(self.state), b"")
            self.assertEqual(len(self.refusals()), 1)
            got = self.call(FL_WHOLE)   # the same call id after the refusal
            self.assertEqual(got["status"], 200, got)
            self.assertEqual([e["command"] for e in relay_pl().read_ledger(self.state)[0]], [FL_WHOLE])
            self.assertEqual(len(self.refusals()), 1)

    class TestFieldLimitCounting(FieldBase):
        """S4: the length counted is that of the redacted, collapsed text."""

        def test_white_space_and_a_redacted_secret_are_counted_as_before(self):
            spaced = "x" * 75 + " " * 150 + "y" * 75
            self.assertEqual((len(spaced), len(" ".join(spaced.split()))), (300, 151))
            self.assertEqual(self.prove_cmd(spaced)[0], 0)
            self.assertEqual(len(self.ledger()[-1]["command"]), 151)
            secret = "ghp_" + ("0123456789abcdefABCDEF0123" * 2)[:46]   # 50 characters, redacted to a mark of 10
            text = "token %s " % secret + "z" * (230 - len("token %s " % secret))
            self.assertEqual(len(text), 230)
            self.assertEqual(len(sb.redact_command_text(text)), 190)
            code, out, err = self.prove_cmd(text, "S2")
            self.assertEqual(code, 0, err)
            got = self.ledger()[-1]["command"]
            self.assertEqual(len(got), 190)
            self.assertNotIn(secret, got)
            self.refused(self.prove_cmd, "w " * 100 + "w", "command")      # 201 after the collapse

    class TestFieldLimitMutants(unittest.TestCase):
        """S5: four mutants on source copies, one per site, each applied exactly once, control first; each fails with the check's own text."""

        def local_case(self, name, method):
            case = globals_of[name](method)
            case.setUp()
            try:
                getattr(case, method)()
            finally:
                case.doCleanups()

        def relay_case(self, mod):
            case = TestFieldLimitRelay("test_201_is_refused_400_and_200_recorded")
            case.mod = mod
            case.setUp()
            try:
                case.test_201_is_refused_400_and_200_recorded()
            finally:
                case.doCleanups()

        def mutated(self, name, old, new, wrap=None):
            src = class_source_fn(name)
            self.assertEqual(src.count(old), 1, old)
            original = getattr(sb, name)
            try:
                exec(src.replace(old, new), vars(sb))   # the module's own globals: the patches of the fixtures still apply to the copy
                fn = getattr(sb, name)
            finally:
                setattr(sb, name, original)
            return fn if wrap is None else sb.recording(wrap)(fn)

        def run_mutant(self, name, old, new, case, method, wrap=None):
            fn = self.mutated(name, old, new, wrap)
            with mock.patch.object(sb, name, fn):
                with self.assertRaises(AssertionError) as ctx:
                    self.local_case(case, method)
            return str(ctx.exception)

        def test_controls_pass_first(self):
            self.local_case("local", "test_prove_one_scenario")
            self.local_case("local", "test_waive")
            self.local_case("items", "test_item_prove_refuses_201_and_records_200")
            self.relay_case(fl_relay)

        def test_mutant_cmd_prove_clips_again(self):
            old = 'plan_ledger.bounded_text(redact_command_text(args.command or ""), plan_ledger.FIELD_LIMIT, "command")'
            got = self.run_mutant("cmd_prove", old, 'plan_ledger.clip(redact_command_text(args.command or ""))', "local", "test_prove_one_scenario", wrap="proof")
            self.assertIn("0 != 2", got)   # a command that clips records and exits 0 where 2 is the refusal

        def test_mutant_cmd_item_prove_clips_again(self):
            old = 'plan_ledger.bounded_text(redact_command_text(args.command or ""), plan_ledger.FIELD_LIMIT, "command")'
            got = self.run_mutant("cmd_item_prove", old, 'plan_ledger.clip(redact_command_text(args.command or ""))', "items", "test_item_prove_refuses_201_and_records_200")
            self.assertIn("is not None", got)   # it answered an exit code where the refusal raises

        def test_mutant_cmd_waive_clips_again(self):
            old = 'plan_ledger.bounded_text(redact_command_text(" ".join(args.reason.split())), plan_ledger.FIELD_LIMIT, "reason")'
            got = self.run_mutant("cmd_waive", old, 'plan_ledger.clip(redact_command_text(" ".join(args.reason.split())), plan_ledger.FIELD_LIMIT)', "local", "test_waive")
            self.assertIn("0 != 2", got)

        def test_mutant_relay_write_clips_again(self):
            src = (BIN / "cloud_relay_daemon.py").read_text()
            old = "command=_checked_command(args)"
            self.assertEqual(src.count(old), 1)
            mod = fl_ev.source_copy(old, 'command=plan_ledger.clip(str(args.get("command") or ""))')
            with self.assertRaises(AssertionError) as ctx:
                self.relay_case(mod)
            self.assertIn("!= (400, None)", str(ctx.exception))   # a relay that clips answers 200 where 400 is the refusal

    globals_of = {"local": TestFieldLimitLocal, "items": TestFieldLimitItems}

    class TestFieldLimitUnchanged(FieldBase):
        """S7: the evidence rule of #1901 and the claim line limit of 1000 did not move."""

        def test_evidence_400_with_a_command_of_150_is_recorded_whole(self):
            command, evidence = "k" * 150, "e" * 400
            self.assertEqual(self.prove_cmd(command, evidence=evidence)[0], 0)
            entry = self.ledger()[-1]
            self.assertEqual((entry["evidence"], entry["command"]), (evidence, command))

        def test_evidence_501_is_refused_by_the_evidence_rule(self):
            before = self.state_snapshot()
            code, out, err = self.prove_cmd("k" * 150, evidence="e" * 501)
            self.assertEqual(code, 2, out + err)
            self.assertIn("the evidence is 501 characters and the limit is 500", err)
            self.assertEqual(self.state_snapshot(), before)

        def test_a_claim_line_of_900_is_recorded_as_before(self):
            text = "S1 " * 299 + "S1"
            self.assertEqual(len(text), 899)
            body = "PLAN: acme/app#7\nCLAIMS: " + text
            line = sb.plan_ledger.claim_line(self.PR, host.LEDGER_HEAD, body)
            self.assertEqual(line["claim_text"], text)
            longer = sb.plan_ledger.claim_line(self.PR, host.LEDGER_HEAD, "PLAN: acme/app#7\nCLAIMS: " + "s" * 1001)["claim_text"]
            self.assertEqual((len(longer), longer[-1]), (1000, "\u2026"))

    class TestFieldLimitDocsAndCode(unittest.TestCase):
        """S6, U4, U5."""

        def test_the_prove_row_and_the_waive_section_state_the_limit_and_the_refusal(self):
            text = (REPO / "docs" / "switchboard.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("A command over 200 characters is refused, never cut."), 1)
            self.assertEqual(text.count('--reason "<at least 20 and at most 200 characters; a longer reason is refused, never cut>"'), 1)

        def test_the_ledger_module_has_the_one_rule_and_the_limits_are_unchanged(self):
            self.assertEqual((sb.plan_ledger.EVIDENCE_LIMIT, sb.plan_ledger.FIELD_LIMIT, sb.plan_ledger.CLAIM_TEXT_LIMIT), (500, 200, 1000))
            self.assertEqual(sb.plan_ledger.bounded_text("a" * 200, sb.plan_ledger.FIELD_LIMIT, "command"), "a" * 200)
            with self.assertRaises(sb.plan_ledger.LedgerError):
                sb.plan_ledger.bounded_text("a" * 201, sb.plan_ledger.FIELD_LIMIT, "command")

        def test_this_file_makes_no_size_measure(self):
            text = Path(__file__).read_text()
            for needle in ("git" + " diff", "num" + "stat", "origin/" + "integration"):
                self.assertEqual(text.count(needle), 0, needle)

    return (TestFieldLimitLocal, TestFieldLimitItems, TestFieldLimitRelay, TestFieldLimitCounting, TestFieldLimitMutants,
            TestFieldLimitUnchanged, TestFieldLimitDocsAndCode)
