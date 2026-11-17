"""The #2551 tests: a blocked agent whose newest message ended on a provider stream failure is resumed by the next pass, at most three times an hour,
and the next failure goes to the Chief of Staff; a working agent or one with an open request is never resumed. build() is called by bin/test-switchboard."""
import os
from unittest import mock
from support.loader import sb

ERROR = "OpenAI Chat stream ended without finish_reason"
FLEET = {"role": "coder", "reports_to": "cos"}


def build(Base, World, FAKE_OPENCODE, FAKE_HERDR):
    class TestStreamErrorResume(Base):
        def setUp(self):
            super().setUp()
            self.world = World(self, agents=(), config={"v2_reads_per_pass": 200})
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
            self.world.add_agent("coder", fleet=FLEET)
            self.pane = self.world.herdr.panes[self.world.panes["coder"]]
            self.pane["status"] = "blocked"
            self.sid = self.world.sessions["coder"]
            self.count = 0

        def fail(self, text=ERROR):
            self.count += 1
            self.world.v2.sessions[self.sid]["messages"].append({"id": "msg_err%d" % self.count, "type": "assistant", "error": {"type": "provider", "message": text}})

        def tick(self, seconds=90):
            self.world.clock.t += seconds
            self.world.engine.run_pass()

        def resumes(self):
            return [a for a in self.world.admissions("coder") if a["text"] == sb.RESUME_TEXT]

        def audited(self):
            return [e for e in self.world.audit() if e["event"] == "resume"]

        def findings(self):
            return [a for a in self.world.admissions("cos") if "[stream-error]" in a["text"]]

        def test_s1_resumed_within_two_minutes_three_times_an_hour_then_the_fourth_goes_to_cos(self):
            for n in range(3):
                self.fail()
                self.tick(90)
                self.assertEqual(len(self.resumes()), n + 1)
                self.tick(90)   # the same ended turn is not resumed twice
                self.assertEqual(len(self.resumes()), n + 1)
            self.assertEqual([e["detail"]["reason"].endswith(ERROR) for e in self.audited()], [True] * 3)
            self.fail()
            self.tick(90)
            self.tick(90)
            self.assertEqual(len(self.resumes()), 3)
            (found,) = self.findings()
            self.assertIn("coder", found["text"])
            self.assertIn(ERROR, found["text"])
            self.world.clock.t += 3600
            self.fail()
            self.tick(90)
            self.assertEqual(len(self.resumes()), 4)

        def test_s2_a_working_agent_or_a_turn_without_a_provider_error_is_never_resumed(self):
            self.pane["status"] = "working"
            self.fail()
            self.tick()
            self.pane["status"] = "blocked"
            self.world.v2.sessions[self.sid]["messages"].append({"id": "msg_ok", "type": "assistant", "error": {"type": "provider", "message": "model refused"}})
            self.tick()
            self.assertEqual((self.resumes(), self.audited(), self.findings()), ([], [], []))

        def mutant(self, old, new):
            path = os.path.join(os.path.dirname(os.path.realpath(sb.__file__)), "switchboard", "observing.py")
            text = open(path).read()
            self.assertIn(old, text)
            start = text.index("def stream_error_tick")
            real = sb.stream_error_tick
            self.addCleanup(setattr, sb, "stream_error_tick", real)
            scope = sb.__dict__
            exec(compile(text[start:].replace(old, new, 1), "mutant", "exec"), scope)
            self.addCleanup(scope.__setitem__, "stream_error_tick", real)

        def force_working_errored(self):
            """A working agent wrongly read as errored, and the shared resume with its own working refusal taken out: only the tick's guard is left."""
            self.pane["status"] = "working"
            self.fail()
            real = sb.read_errored_turn

            def wrong(engine, observation, now):
                observation.turn_error, observation.errored = ERROR, True
                return True
            send = lambda engine, caller, name, reason, discovery=None: engine.v2.synthetic(
                engine.discover(engine.now()).agents[name].session_id, sb.RESUME_TEXT, {"fleet": {"keys": ["k%d" % engine.now()], "from": "switchboard"}}, True, "queue", "msg_k%d" % engine.now(), "r")
            for target, value in (("read_errored_turn", wrong), ("resume_ended_turn", send)):
                patcher = mock.patch.object(sb, target, value)
                patcher.start()
                self.addCleanup(patcher.stop)

        def test_s3_the_working_guard_mutant_is_killed(self):
            self.force_working_errored()
            self.tick()
            self.assertEqual(self.resumes(), [], "the control: the tick's own guard refuses a working agent")

        def test_s3_the_working_guard_mutant_resumes_a_working_agent(self):
            self.mutant('seen.status != "blocked" or not getattr(seen, "errored", False) or ', "")
            self.force_working_errored()
            self.tick()
            self.assertEqual(len(self.resumes()), 1, "the mutant has no working guard, so S2's assertion of 0 resumes would fail")

        def test_s3_the_cap_mutant_is_killed(self):
            self.mutant("len(times) >= STREAM_RESUME_MAX", "False")
            for _ in range(4):
                self.fail()
                self.tick()
            self.assertEqual((len(self.resumes()), self.findings()), (4, []))   # the mutant breaks the cap that S1 asserts

        def test_u1_an_open_request_is_never_resumed_and_a_bad_state_file_is_recovered(self):
            self.world.v2.ask_permission(self.sid)
            self.fail()
            self.tick()
            self.assertEqual((self.resumes(), self.audited()), ([], []))
            self.world.add_agent("asker", fleet=FLEET)
            asker = self.world.sessions["asker"]
            self.world.herdr.panes[self.world.panes["asker"]]["status"] = "blocked"
            self.world.v2.ask_form(asker)
            self.world.v2.sessions[asker]["messages"].append({"id": "msg_e", "type": "assistant", "error": {"type": "provider", "message": ERROR}})
            self.tick()
            self.assertEqual(self.world.admissions("asker"), [])
            (self.world.state / sb.STREAM_RESUME_STATE).write_text("{not json")   # recovered: an unreadable state file is an empty one
            self.world.add_agent("fresh", fleet=FLEET)
            fresh = self.world.sessions["fresh"]
            self.world.herdr.panes[self.world.panes["fresh"]]["status"] = "blocked"
            self.world.v2.sessions[fresh]["messages"].append({"id": "msg_f", "type": "assistant", "error": {"type": "provider", "message": ERROR}})
            self.tick()
            self.assertEqual(len([a for a in self.world.admissions("fresh") if a["text"] == sb.RESUME_TEXT]), 1)

    return TestStreamErrorResume
