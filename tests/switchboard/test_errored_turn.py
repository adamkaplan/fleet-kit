"""The #1697 tests: an agent whose last turn ended on a provider error, with nothing open, is idle-with-error (not blocked). build() is called by bin/test-switchboard, which holds Base and World."""
import os
from unittest import mock
from support.loader import captured, sb

REAL = ("c1-permission", "c2-question", "c3-both", "c4-permission-and-error")
ERRORS = ("c5-econnreset", "c6-499", "c7-timeout")
AMBIGUOUS = ("c8-other-error", "c9-permission-list-unreadable", "c10-question-list-unreadable")
FLEET = {"role": "coder", "reports_to": "cos"}


def build(Base, World, FAKE_OPENCODE, FAKE_HERDR):
    class TestErroredTurn(Base):
        def setUp(self):
            super().setUp()
            self.world = World(self, agents=(), config={"v2_reads_per_pass": 200})
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
            for name in REAL + ERRORS + AMBIGUOUS:
                self.world.add_agent(name, fleet=FLEET)
                self.pane(name)["status"] = "blocked"
            v2 = self.world.v2
            sid = self.world.sessions
            v2.ask_permission(sid["c1-permission"])
            v2.ask_form(sid["c2-question"])
            v2.ask_permission(sid["c3-both"])
            v2.ask_form(sid["c3-both"])
            v2.ask_permission(sid["c4-permission-and-error"])
            for name, text in (("c4-permission-and-error", "HTTP 499 client closed request"), ("c5-econnreset", "read ECONNRESET"),
                               ("c6-499", "HTTP 499 client closed request"), ("c7-timeout", "request timed out"),
                               ("c8-other-error", "model refused the content"), ("c9-permission-list-unreadable", "ECONNRESET"),
                               ("c10-question-list-unreadable", "ECONNRESET")):
                v2.sessions[sid[name]]["messages"].append({"id": "msg_err_" + name, "type": "assistant", "error": {"type": "provider", "message": text}})
            self.broken = {}
            engine = self.world.engine
            for method in ("permission_list", "form_list"):
                real = getattr(engine.v2, method)
                setattr(engine.v2, method, self.guarded(method, real))

        def guarded(self, method, real):
            def read(session_id, *args, **kw):
                if self.broken.get(session_id) == method:
                    raise sb.V2Error("timeout", None, "slow")
                return real(session_id, *args, **kw)
            return read

        def pane(self, name):
            return self.world.herdr.panes[self.world.panes[name]]

        def break_reads(self):
            self.broken = {self.world.sessions["c9-permission-list-unreadable"]: "permission_list",
                           self.world.sessions["c10-question-list-unreadable"]: "form_list"}

        def read_all(self):
            self.break_reads()
            found = self.world.engine.discover(self.world.clock.now()).agents
            return {name: getattr(found[name], "errored", False) for name in REAL + ERRORS + AMBIGUOUS}

        def check_table(self):
            got = self.read_all()
            self.assertEqual(sum(1 for n in REAL if not got[n]), 4)
            self.assertEqual(sum(1 for n in ERRORS if got[n]), 3)
            self.assertEqual(sum(1 for n in AMBIGUOUS if not got[n]), 3)

        def test_s2_a_table_of_ten_cases_through_the_real_pane_read(self):
            self.check_table()
            found = self.world.engine.discover(self.world.clock.now()).agents
            for name in REAL + AMBIGUOUS:   # the status stays blocked: the report and its resume are unchanged
                self.assertEqual(found[name].status, "blocked")

        def holds(self):
            return [e for e in self.world.audit() if e["event"] == "hold"]

        def test_s3_a_send_to_the_errored_agent_is_delivered_and_woken_once_and_to_a_real_prompt_is_held(self):
            self.break_reads()
            outcome = self.world.engine.send("cos", "c5-econnreset", "please carry on")
            self.assertEqual((outcome["sent"], outcome["action"]), (True, "wake"))
            self.assertEqual(len(self.world.admissions("c5-econnreset")), 1)
            self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "v2_reads_per_pass": 200})
            with captured() as (out, err):
                code = sb.main(["send", "--from", "cos", "c6-499", "please carry on"], runner=self.world.route, now=self.world.clock.now,
                               wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(len(self.world.admissions("c6-499")), 1)
            self.assertEqual(self.holds(), [])
            for name in REAL:
                self.assertFalse(self.world.engine.send("cos", name, "hello")["sent"])
                self.assertEqual(self.world.admissions(name), [])
            self.assertEqual(len(self.holds()), 4)
            for line in self.holds():
                self.assertIn("is blocked on a permission request or a question", line["detail"]["reason"])

        def status_lines(self):
            self.break_reads()
            self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "v2_reads_per_pass": 200})
            report = sb.collect_status(self.world.route, self.world.clock.now)
            return sb.format_status(report).splitlines()

        def test_s4_the_status_view_says_idle_after_a_provider_error_and_blocked_for_a_real_prompt(self):
            lines = self.status_lines()
            mine = [l for l in lines if l.lstrip().startswith("c5-econnreset")]
            self.assertEqual(len(mine), 1)
            self.assertEqual(len([l for l in mine if "idle after a provider error" in l]), 1)
            real = [l for l in lines if l.lstrip().startswith("c1-permission")]
            self.assertEqual(len(real), 1)
            self.assertIn("blocked", real[0])
            self.assertNotIn("idle after a provider error", real[0])

        def test_s5_m1_every_error_case_read_blocked_fails_the_table(self):
            with mock.patch.object(sb, "PROVIDER_ERROR", sb.re.compile(r"(?!x)x")):
                with self.assertRaises(AssertionError):
                    self.check_table()

        def test_s5_m2_a_real_prompt_read_idle_with_error_fails_the_table(self):
            def always(engine, observation, now):
                observation.errored = observation.status == "blocked"
                return observation.errored
            with mock.patch.object(sb, "read_errored_turn", always):
                with self.assertRaises(AssertionError):
                    self.check_table()

        def counted(self):
            calls = []
            v2 = self.world.engine.v2
            for method in ("permission_list", "form_list", "message_list"):
                real = getattr(v2, method)

                def spy(session_id, *a, __real=real, __method=method, **k):
                    if __method != "message_list" or k.get("limit") == 1:
                        calls.append((__method, session_id))
                    return __real(session_id, *a, **k)
                setattr(v2, method, spy)
            return calls

        def test_s7_read_budget(self):
            for name in REAL + ERRORS + AMBIGUOUS:
                self.pane(name)["status"] = "idle" if name != "c5-econnreset" else "blocked"
            for n, name in enumerate(("c1-permission", "c2-question", "c3-both", "c4-permission-and-error", "c6-499")):
                self.pane(name)["status"] = "working" if n % 2 else "idle"
            calls = self.counted()
            self.world.engine.discover(self.world.clock.now())
            sid = self.world.sessions["c5-econnreset"]
            self.assertEqual([c for c in calls if c[1] != sid], [], "no extra read for a working or idle agent")
            self.assertLessEqual(len([c for c in calls if c[1] == sid]), 3)

        def test_s7_an_exhausted_read_budget_reads_a_blocked_agent_blocked(self):
            for name in REAL + ERRORS + AMBIGUOUS:
                self.pane(name)["status"] = "idle" if name != "c5-econnreset" else "blocked"
            self.world.engine.reads.pass_fills = self.world.engine.config["v2_reads_per_pass"]
            calls = self.counted()
            found = self.world.engine.discover(self.world.clock.now()).agents
            self.assertFalse(found["c5-econnreset"].errored)
            self.assertEqual(calls, [])
    return TestErroredTurn
