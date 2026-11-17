"""The #2551 tests: a blocked agent whose newest message ended on a provider stream failure is resumed by the next pass, at most three times an hour,
and the next failure goes to the Chief of Staff; a working agent or one with an open request is never resumed. build() is called by bin/test-switchboard."""
import json
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

        def escalations(self):
            return [e for e in self.world.audit() if e["event"] == "stream_error.escalated"]

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
            self.assertEqual([(e["subject"], e["detail"]["count"], e["detail"]["error"]) for e in self.escalations()], [("coder", 4, ERROR)])
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
            text = open(os.path.realpath(sb.__file__)).read()
            start = text.index("def stream_error_tick")
            end = text.index("\ndef ended_turn_text", start)
            block = text[start:end]
            self.assertEqual(block.count(old), 1, old)
            scope = dict(sb.__dict__)
            exec(compile(block.replace(old, new, 1), "mutant", "exec"), scope)
            return mock.patch.object(sb, "stream_error_tick", scope["stream_error_tick"])

        def force_working_errored(self):
            """A working agent wrongly read as errored, and the shared resume with its own working refusal taken out: only the tick's guard is left."""
            def wrong(engine, observation, now):
                err, mid = sb.last_turn_error_and_id(engine, observation.session_id)
                observation.turn_error, observation.turn_error_id, observation.errored = err, mid, True
                return True
            send = lambda engine, caller, name, reason, discovery=None, window=None: engine.v2.synthetic(
                engine.discover(engine.now()).agents[name].session_id, sb.RESUME_TEXT, {"fleet": {"keys": ["k%d" % engine.now()], "from": "switchboard"}}, True, "queue", "msg_k%d" % engine.now(), "r")
            for target, value in (("read_errored_turn", wrong), ("resume_ended_turn", send)):
                patcher = mock.patch.object(sb, target, value)
                patcher.start()
                self.addCleanup(patcher.stop)

        def check_working_guard(self):
            self.pane["status"] = "working"
            self.fail()
            self.tick()
            self.assertEqual(len(self.resumes()), 0, "working guard: resumes sent to a working agent")

        def test_s3_the_working_guard_mutant_is_killed(self):
            self.force_working_errored()
            self.check_working_guard()   # passing control on the unmutated code
            with self.mutant('seen.status != "blocked" or not getattr(seen, "errored", False) or ', ""):
                with self.assertRaisesRegex(AssertionError, r"^1 != 0 : working guard: resumes sent to a working agent$"):
                    self.check_working_guard()

        def check_hourly_cap(self):
            (self.world.state / sb.STREAM_RESUME_STATE).unlink() if (self.world.state / sb.STREAM_RESUME_STATE).exists() else None
            before_r, before_f = len(self.resumes()), len(self.findings())
            for _ in range(4):
                self.fail()
                self.tick()
            self.assertEqual(len(self.resumes()) - before_r, 3, "hourly cap: resumes sent in 4 failures")
            self.assertEqual(len(self.findings()) - before_f, 1, "hourly cap: findings sent in 4 failures")

        def test_s3_the_cap_mutant_is_killed(self):
            self.check_hourly_cap()   # passing control on the unmutated code
            with self.mutant("len(times) >= STREAM_RESUME_MAX", "False"):
                with self.assertRaisesRegex(AssertionError, r"^4 != 3 : hourly cap: resumes sent in 4 failures$"):
                    self.check_hourly_cap()

        def check_phrase(self, phrase):
            (self.world.state / sb.STREAM_RESUME_STATE).unlink() if (self.world.state / sb.STREAM_RESUME_STATE).exists() else None
            before = len(self.resumes())
            self.fail("provider failure: " + phrase)
            self.tick(90)
            self.assertEqual(len(self.resumes()) - before, 1, "stream error phrase %r was not resumed" % phrase)

        def test_every_stream_error_phrase_is_resumed_and_a_mutant_missing_it_is_caught(self):
            self.assertGreaterEqual(len(sb.STREAM_ERRORS), 4)
            for phrase in sb.STREAM_ERRORS:
                with self.subTest(phrase=phrase):
                    self.check_phrase(phrase)   # passing control first
                    without = sb.re.compile("|".join(p for p in sb.STREAM_ERRORS if p != phrase), sb.re.I)
                    with mock.patch.object(sb, "STREAM_ERROR", without):
                        with self.assertRaisesRegex(AssertionError, r"^0 != 1 : stream error phrase %r was not resumed$" % phrase):
                            self.check_phrase(phrase)

        def test_secrets_and_ansi_control_sequences_are_scrubbed_from_audit_and_escalation(self):
            secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
            dirty = ERROR + " \x1b[31m\x07Bearer " + secret
            for _ in range(4):
                self.fail(dirty)
                self.tick(90)
            self.assertEqual(len(self.resumes()), 3)
            (found,) = self.findings()
            (esc,) = self.escalations()
            for text in [e["detail"]["reason"] for e in self.audited()] + [found["text"], esc["detail"]["error"]]:
                self.assertIn("[redacted]", text)
                for forbidden in (secret, "\x1b", "[31m", "\x07"):
                    self.assertNotIn(forbidden, text)

        def test_refused_resume_preserves_prior_cooldown_and_new_message_before_tick_skips_resume(self):
            prior_at = self.world.clock.now() - 120
            sb.blocked_update(self.world.state, sb.RESUME_STATE, lambda s: {"coder": {"at": prior_at}})
            self.fail()
            discovery = self.world.engine.discover(self.world.clock.now())
            self.world.v2.ask_permission(self.sid)   # opens after read_errored_turn, before stream_error_tick's resume_ended_turn re-checks
            sb.stream_error_tick(self.world.engine, discovery)
            self.assertEqual(self.resumes(), [])
            self.assertEqual(json.loads((self.world.state / sb.RESUME_STATE).read_text()), {"coder": {"at": prior_at}})
            self.assertFalse((self.world.state / sb.STREAM_RESUME_STATE).exists())
            self.world.v2.answer_permissions(self.sid)
            self.fail()
            discovery = self.world.engine.discover(self.world.clock.now())
            self.world.v2.sessions[self.sid]["messages"].append({"id": "msg_after", "type": "assistant", "text": "recovered"})
            sb.stream_error_tick(self.world.engine, discovery)
            self.assertEqual(self.resumes(), [])

        def test_u1_an_open_request_is_never_resumed_and_a_bad_or_oversized_state_file_is_recovered(self):
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
            now = self.world.clock.now()
            bloated = {"fresh": {"times": "bad", "last_id": 123}}
            for i in range(70):
                bloated["stale-%d" % i] = {"times": [now - 7200], "last_id": "m"}
                bloated["recent-%d" % i] = {"times": [now - 100 - i], "last_id": "m"}
            (self.world.state / sb.STREAM_RESUME_STATE).write_text(json.dumps(bloated))
            self.world.v2.sessions[fresh]["messages"].append({"id": "msg_f2", "type": "assistant", "error": {"type": "provider", "message": ERROR}})
            self.tick()
            saved = json.loads((self.world.state / sb.STREAM_RESUME_STATE).read_text())
            self.assertEqual(len(saved["fresh"]["times"]), 1)
            self.assertEqual([k for k in saved if k.startswith("stale-")], [])
            self.assertEqual(len(saved), sb.STREAM_RESUME_AGENTS_MAX)
            self.assertIn("fresh", saved)

    return TestStreamErrorResume
