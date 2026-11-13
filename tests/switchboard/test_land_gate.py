"""`fleet-switchboard land-gate` (ask #2517, first slice): one land token per repository on the land list, a FIFO queue by the time of the request, expiry of a holder that stops
touching or whose pod is gone, `release`, and `move` (the Chief of Staff only). S2 order and status, S3 expiry and release, S4 move, S6 (a repository off the land list is untouched),
the config key. Every fixture is a fake: the clock, the caller's identity and the liveness of a pod. The controls (a mutant of the rule, and the table of the current code) show the checks catch what they are for.
The names are neutral (acme-org/widget-service, lane-a)."""
import json
import re
from unittest import mock

from support.core import Base
from support.loader import captured, sb

REPO = "acme-org/widget-service"
OTHER = "acme-org/docs-site"   # a repository that is not on the land list in the tests that say so


class GateBase(Base):
    def setUp(self):
        super().setUp()
        self.write_config({"land_repos": [REPO]})
        self.t = 1_000_000
        self.caller = ("agent", "lane-a", "worker")
        self.gone = set()
        self.real_alive = sb.lg_alive_reader
        for target, value in (("gates_caller_kind", lambda runner, now: self.caller), ("lg_alive_reader", lambda state_dir: (lambda name: name not in self.gone))):
            patch = mock.patch.object(sb, target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def as_(self, name, role="worker"):
        self.caller = ("agent", name, role)

    def gate(self, *words, who=None):
        if who is not None:
            self.as_(who, "chief-of-staff" if who == "cos" else "worker")
        with captured() as (out, err):
            code = sb.main(["land-gate"] + list(words), runner=lambda *a, **k: None, now=lambda: self.t)
        return code, out.getvalue(), err.getvalue()

    def state(self):
        path = self.state_dir / sb.LAND_GATE_FILE
        return json.loads(path.read_text()) if path.exists() else {}

    def holder(self):
        entry = self.state().get("repos", {}).get(REPO, {})
        return (entry.get("holder") or {}).get("name")

    def queue(self):
        return [q["name"] for q in self.state().get("repos", {}).get(REPO, {}).get("queue", [])]


class TestOrder(GateBase):
    def serve_in_order(self, takers):
        """Each taker asks once, in order, then each releases in turn: the names that held the token, in the order they held it."""
        for name in takers:
            self.t += 5
            self.gate("take", REPO, who=name)
        served = []
        while self.holder():
            served.append(self.holder())
            self.t += 5
            self.gate("release", REPO, who=self.holder())
        return served

    def test_s2_two_takers_are_served_in_order_of_request_and_a_third_is_queued(self):
        code_a, out_a, _ = self.gate("take", REPO, who="lane-a")
        self.t += 10
        code_b, out_b, _ = self.gate("take", REPO, who="lane-b")
        self.t += 10
        code_c, out_c, _ = self.gate("take", REPO, who="lane-c")
        self.assertEqual((code_a, code_b, code_c), (0, 1, 1))
        self.assertTrue(out_a.startswith("HELD %s by lane-a" % REPO), out_a)
        self.assertIn("QUEUED %s at place 1 behind lane-a" % REPO, out_b)
        self.assertIn("QUEUED %s at place 2 behind lane-a" % REPO, out_c)
        self.assertEqual((self.holder(), self.queue()), ("lane-a", ["lane-b", "lane-c"]))
        code, out, _ = self.gate("status", REPO)
        self.assertEqual(code, 0)
        self.assertIn("%s: held by lane-a for 20s, last touch 20s ago; queue 2" % REPO, out)
        self.assertIn("1. lane-b asked 10s ago", out)
        self.assertIn("2. lane-c asked 0s ago", out)

    def test_s2_the_count_of_takers_served_out_of_order_is_zero(self):
        for order in (["lane-a", "lane-b", "lane-c", "lane-d"], ["lane-c", "lane-d", "lane-b", "lane-a"]):   # the second is not alphabetical
            self.assertEqual(self.serve_in_order(order), order)

    def test_the_limit_is_exact_a_holder_at_600_seconds_still_holds_and_at_601_does_not(self):
        self.gate("take", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-b")
        self.t += 600
        self.gate("take", REPO, who="lane-b")
        self.assertEqual(self.holder(), "lane-a")
        self.t += 1
        self.gate("take", REPO, who="lane-b")
        self.assertEqual(self.holder(), "lane-b")

    def test_s2_control_a_rule_that_serves_the_newest_first_is_caught(self):
        def newest_first(entry, name, now):   # a mutant of lg_take: the newcomer jumps the queue
            entry["queue"].insert(0, {"name": name, "at": now, "seen": now})
            return "queued", 1
        with mock.patch.object(sb, "lg_take", newest_first):
            self.assertEqual(self.serve_in_order(["lane-a", "lane-b", "lane-c"]), ["lane-a", "lane-c", "lane-b"])   # the mutant's exact (wrong) order

    def test_s2_control_the_command_is_registered_where_the_current_code_has_none(self):
        self.assertIn("land-gate", sb.build_parser().commands)   # the code before the change has no such command: nothing in it could serialise two landings
        self.assertEqual(self.gate("take", REPO, who="lane-a")[0], 0)

    def test_s8_control_with_no_queue_two_takers_both_get_through_and_the_new_code_refuses_the_second(self):
        def old_behaviour(entry, name, now):   # the behaviour before the gate: nothing serialises, the newest asker simply holds
            entry["holder"] = {"name": name, "since": now, "touched": now}
            return "held", 0
        with mock.patch.object(sb, "lg_take", old_behaviour):
            self.assertEqual(self.gate("take", REPO, who="lane-a")[0], 0)
            self.t += 5
            code, out, _ = self.gate("take", REPO, who="lane-b")
            self.assertEqual(code, 0)   # the second taker is NOT refused: two landings run at once
            self.assertIn("HELD %s by lane-b" % REPO, out)
            self.assertNotIn("QUEUED", out)
        (self.state_dir / sb.LAND_GATE_FILE).unlink()
        self.assertEqual(self.gate("take", REPO, who="lane-a")[0], 0)
        self.t += 5
        code, out, _ = self.gate("take", REPO, who="lane-b")
        self.assertEqual(code, 1)   # the new code: the second is refused, queued, the holder named
        self.assertIn("QUEUED %s at place 1 behind lane-a" % REPO, out)
        self.assertEqual((self.holder(), self.queue()), ("lane-a", ["lane-b"]))

    def test_asking_again_only_shows_the_waiter_is_alive_and_keeps_its_place(self):
        self.gate("take", REPO, who="lane-a")
        self.t += 1
        self.gate("take", REPO, who="lane-b")
        self.gate("take", REPO, who="lane-c")
        self.t += 30
        self.gate("take", REPO, who="lane-b")
        self.assertEqual(self.queue(), ["lane-b", "lane-c"])
        entry = self.state()["repos"][REPO]["queue"][0]
        self.assertEqual((entry["at"], entry["seen"]), (1_000_001, 1_000_031))

    def test_a_person_at_a_shell_has_no_place_and_the_state_is_not_written(self):
        self.caller = ("person", "a person at a shell")
        code, out, err = self.gate("take", REPO)
        self.assertEqual(code, 2)
        self.assertIn("not a fleet agent", err)
        self.assertEqual(self.state(), {})

    def test_names_in_status_are_sanitised(self):
        self.gate("take", REPO, who="lane-a")
        data = self.state()
        data["repos"][REPO]["holder"]["name"] = "lane-a\x1b[31m`x`"   # a name written by something else into the state file
        (self.state_dir / sb.LAND_GATE_FILE).write_text(json.dumps(data))
        code, out, _ = self.gate("status")
        self.assertEqual(code, 0)
        self.assertNotRegex(out, r"[\x00-\x09\x0b-\x1f`]")
        self.assertIn("held by lane-a??31m?x?", out)


class TestExpiry(GateBase):
    def test_s3_the_next_in_the_queue_gets_the_token_when_the_holder_is_past_the_limit(self):
        self.gate("take", REPO, who="lane-a")
        self.t += 60
        self.gate("take", REPO, who="lane-b")
        self.t += 10 * 60 - 61
        self.gate("take", REPO, who="lane-b")
        self.assertEqual(self.holder(), "lane-a")   # 599 s after its last touch: still alive
        self.t += 2
        code, out, _ = self.gate("take", REPO, who="lane-b")
        self.assertEqual((code, self.holder()), (0, "lane-b"), out)
        code, out, _ = self.gate("touch", REPO, who="lane-a")
        self.assertEqual(code, 1)
        self.assertIn("lane-a does not hold the land token", out)
        audit = [json.loads(l) for l in (self.state_dir / "audit.jsonl").read_text().splitlines()]
        self.assertIn(("land_gate.expired", "lane-a", "no touch for 10 minutes"), [(a["event"], (a["detail"] or {}).get("name"), (a["detail"] or {}).get("why")) for a in audit])

    def test_s3_a_holder_whose_pod_is_gone_loses_the_token_at_once(self):
        self.gate("take", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-b")
        self.gone.add("lane-a")
        self.t += 5
        code, out, _ = self.gate("status", REPO)
        self.assertEqual(self.holder(), "lane-b")
        self.assertIn("held by lane-b", out)

    def test_s3_a_waiter_whose_pod_is_gone_or_that_stopped_asking_never_blocks_the_queue(self):
        self.gate("take", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-b")
        self.gate("take", REPO, who="lane-c")
        self.gate("take", REPO, who="lane-d")
        self.gone.add("lane-b")
        self.t += 5 * 60
        self.gate("take", REPO, who="lane-a")
        self.assertNotIn("lane-b", self.queue(), "dropped for its pod, long before the request limit")
        self.assertIn("its pod is gone", [(json.loads(l)["detail"] or {}).get("why") for l in (self.state_dir / "audit.jsonl").read_text().splitlines()])
        self.gate("take", REPO, who="lane-d")
        self.t += 6 * 60   # lane-c has not asked for 11 minutes
        self.gate("touch", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-d")
        self.assertEqual(self.queue(), ["lane-d"])
        self.gate("release", REPO, who="lane-a")
        self.assertEqual((self.holder(), self.queue()), ("lane-d", []))

    def test_s3_a_release_passes_the_token_on_and_a_queued_name_may_leave(self):
        self.gate("take", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-b")
        self.gate("take", REPO, who="lane-c")
        code, out, _ = self.gate("release", REPO, who="lane-b")   # leaves the queue
        self.assertEqual((code, self.queue()), (0, ["lane-c"]), out)
        code, out, _ = self.gate("release", REPO, who="lane-a")
        self.assertEqual((code, self.holder(), self.queue()), (0, "lane-c", []), out)
        code, out, _ = self.gate("release", REPO, who="lane-a")   # nothing held any more
        self.assertEqual(code, 1)

    def test_s3_control_a_rule_with_no_expiry_keeps_a_dead_holder(self):
        def no_expiry(entry, now, expire, alive):
            if entry["holder"] is None and entry["queue"]:
                head = entry["queue"].pop(0)
                entry["holder"] = {"name": head["name"], "since": now, "touched": now}
            return []
        self.gate("take", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-b")
        self.gone.add("lane-a")
        self.t += 3600
        with mock.patch.object(sb, "lg_settle", no_expiry):
            self.gate("take", REPO, who="lane-b")
        self.assertEqual(self.holder(), "lane-a")   # the count of dead holders still holding the token is 1 under the mutant

    def test_the_limit_is_the_configured_number_of_minutes(self):
        self.write_config({"land_repos": [REPO], "land_gate": {"expire_minutes": 2}})
        self.gate("take", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-b")
        self.t += 121
        self.gate("take", REPO, who="lane-b")
        self.assertEqual(self.holder(), "lane-b")


class TestMove(GateBase):
    def fill(self):
        for name in ("lane-a", "lane-b", "lane-c", "lane-d"):
            self.gate("take", REPO, who=name)

    def test_s4_the_chief_of_staff_reorders_the_queue(self):
        self.fill()
        code, out, _ = self.gate("move", REPO, "lane-d", "1", who="cos")
        self.assertEqual((code, self.queue()), (0, ["lane-d", "lane-b", "lane-c"]), out)
        self.gate("move", REPO, "lane-b", "2", who="cos")
        self.assertEqual(self.queue(), ["lane-d", "lane-b", "lane-c"])
        self.gate("move", REPO, "lane-b", "0", who="cos")
        self.assertEqual(self.queue(), ["lane-b", "lane-d", "lane-c"])   # a place below 1 is the front
        self.gate("move", REPO, "lane-b", "99", who="cos")
        self.gate("move", REPO, "lane-d", "1", who="cos")
        self.assertEqual(self.queue(), ["lane-d", "lane-c", "lane-b"])   # a place past the end is the end
        self.assertEqual(self.holder(), "lane-a")

    def test_s4_a_move_by_anyone_else_is_refused_and_audited(self):
        self.fill()
        for who, role in (("lane-b", "worker"), ("lane-x", "orchestrator")):
            self.as_(who, role)
            code, out, _ = self.gate("move", REPO, "lane-d", "1")
            self.assertEqual(code, 1)
            self.assertIn("only the Chief of Staff moves", out)
        self.caller = ("person", "a person at a shell")
        self.assertEqual(self.gate("move", REPO, "lane-d", "1")[0], 2)
        self.assertEqual(self.queue(), ["lane-b", "lane-c", "lane-d"])
        audit = [json.loads(l)["event"] for l in (self.state_dir / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(audit.count("land_gate.move_refused"), 2)

    def test_s4_control_a_rule_that_lets_anyone_move_accepts_one(self):
        self.fill()
        with mock.patch.object(sb, "_lg_caller", lambda runner, now: ("lane-b", True, None)):   # a mutant that reads every caller as the Chief of Staff
            self.assertEqual(self.gate("move", REPO, "lane-d", "1")[0], 0)
        self.assertEqual(self.queue()[0], "lane-d")

    def test_a_move_of_a_name_not_in_the_queue_is_refused(self):
        self.fill()
        self.assertEqual(self.gate("move", REPO, "lane-a", "1", who="cos")[0], 1)   # the holder is not in the queue

    def test_only_the_chief_of_staff_releases_another_names_place(self):
        self.fill()
        self.assertEqual(self.gate("release", REPO, "lane-a", who="lane-b")[0], 1)
        self.assertEqual(self.holder(), "lane-a")
        self.assertEqual(self.gate("release", REPO, "lane-a", who="cos")[0], 0)
        self.assertEqual(self.holder(), "lane-b")


class TestLandList(GateBase):
    def test_s6_a_repository_off_the_land_list_has_no_gate_and_nothing_is_written(self):
        for words in (("take", OTHER), ("touch", OTHER), ("release", OTHER), ("move", OTHER, "lane-b", "1"), ("status", OTHER)):
            code, out, err = self.gate(*words, who="cos")
            self.assertEqual(code, 2, words)
            self.assertIn("is not on the land list", err)
        self.assertEqual(self.state(), {})

    def test_the_repository_name_is_read_without_regard_to_case(self):
        self.assertEqual(self.gate("take", REPO.upper(), who="lane-a")[0], 0)
        self.assertEqual(self.holder(), "lane-a")

    def test_the_config_key_is_checked(self):
        for good in ({}, {"expire_minutes": 1}, {"expire_minutes": 240}, None):
            self.assertEqual(sb.lg_expire_seconds({"land_gate": good}), 60 * (good or {}).get("expire_minutes", 10))
            sb.validate_config({"land_gate": good})
        for bad in ({"expire_minutes": 0}, {"expire_minutes": 241}, {"expire_minutes": True}, {"expire_minutes": "9"}, {"other": 1}, [], "10"):
            with self.subTest(bad=bad):
                with self.assertRaises(sb.ConfigError):
                    sb.validate_config({"land_gate": bad})

    def test_the_liveness_reader_calls_a_pod_gone_only_when_a_whole_read_says_it_was_retired(self):
        reg = mock.Mock()
        cases = (({"whole": True, "agents": {"lane-a": {}}, "retired": {"lane-b": {}}}, True, False, True),   # live, retired, never heard of
                 ({"whole": False, "agents": {}, "retired": {"lane-b": {}}}, True, True, True))
        for read, lane_a, lane_b, stranger in cases:
            reg.read.return_value = read
            with mock.patch.object(sb, "load_module", lambda name: reg):
                alive = self.real_alive(self.state_dir)
            self.assertEqual((alive("lane-a"), alive("lane-b"), alive("lane-x")), (lane_a, lane_b, stranger), read)
        reg.read.side_effect = OSError("unreadable")
        with mock.patch.object(sb, "load_module", lambda name: reg):
            self.assertTrue(self.real_alive(self.state_dir)("lane-a"))   # any doubt reads as alive

    def test_a_busy_gate_says_so_and_exits_1(self):
        with mock.patch.object(sb.land_gate_lock, "__enter__", side_effect=sb.ImpactStateBusy("busy")):
            code, out, err = self.gate("take", REPO, who="lane-a")
        self.assertEqual(code, 1)
        self.assertIn("busy", err)

    def test_a_fleet_name_a_gate_name_cannot_hold_is_refused_not_rewritten(self):
        self.caller = ("agent", "lane a;b", "worker")
        code, out, err = self.gate("take", REPO)
        self.assertEqual(code, 2)
        self.assertEqual(self.state(), {})


class TestCheckRule(GateBase):
    def test_the_check_names_the_holder_and_the_queue_length_and_changes_nothing(self):
        data = {"repos": {REPO: {"holder": {"name": "lane-a", "since": 100, "touched": 100}, "queue": [{"name": "lane-b", "at": 101, "seen": 101}]}}}
        before = json.dumps(data, sort_keys=True)
        ok, line = sb.lg_check(data, REPO, "lane-b", 150, 600, lambda name: True)
        self.assertFalse(ok)
        self.assertIn("lane-a (last touch 50s ago)", line)
        self.assertIn("queue length 1", line)
        self.assertEqual(sb.lg_check(data, REPO, "lane-a", 150, 600, lambda name: True)[0], True)
        self.assertEqual(sb.lg_check(data, REPO, "lane-a", 800, 600, lambda name: True)[0], False)   # past its limit
        self.assertEqual(json.dumps(data, sort_keys=True), before)


class TestCheckCommand(GateBase):
    """`land-gate check` (second slice): the one question the land wrapper and the pod's pre-push hook ask. Exit 0 allowed, 3 refused with the words; it reads and writes nothing."""

    def check(self, repo, name):
        return self.gate("check", repo, "--name", name)

    def test_s5_the_holder_is_allowed_and_another_name_is_refused_naming_the_holder_and_the_queue(self):
        self.gate("take", REPO, who="lane-a")
        self.gate("take", REPO, who="lane-b")
        self.assertEqual(self.check(REPO, "lane-a")[0], 0)
        code, out, _ = self.check(REPO, "lane-b")
        self.assertEqual(code, 3)
        self.assertIn("lane-b does not hold the land token of %s" % REPO, out)
        self.assertIn("held by lane-a", out)
        self.assertIn("queue length 1", out)
        code, out, _ = self.check(REPO, "lane-z")   # nobody asked at all
        self.assertEqual(code, 3)

    def test_s5_a_holder_past_the_limit_is_refused_and_a_free_gate_refuses_everyone(self):
        self.gate("take", REPO, who="lane-a")
        self.t += 601
        self.assertEqual(self.check(REPO, "lane-a")[0], 3)
        self.assertEqual(self.holder(), "lane-a", "a check changes nothing")
        self.assertIn("nobody holds it", self.check(REPO, "lane-b")[1])   # the dead holder is gone at once and nobody is next

    def test_s6_a_repository_off_the_land_list_is_untouched(self):
        code, out, _ = self.check(OTHER, "lane-a")
        self.assertEqual(code, 0)
        self.assertIn("not on the land list", out)
        self.assertEqual(self.state(), {})

    def test_the_name_is_a_fleet_name(self):
        self.assertEqual(self.check(REPO, "a b;c")[0], 2)

    def test_the_check_sees_a_gone_holder_and_a_free_gate_leaves_no_state(self):
        self.gate("take", REPO, who="lane-a")
        self.gone.add("lane-a")
        code, out, _ = self.check(REPO, "lane-a")
        self.assertEqual(code, 3)
        self.assertIn("nobody holds it", out)
        self.assertIn("queue length 0", out)
        self.assertEqual(self.holder(), "lane-a", "the check wrote nothing")
        (self.state_dir / sb.LAND_GATE_FILE).unlink()
        self.assertEqual(self.check(REPO, "lane-a")[0], 3)
        self.assertFalse((self.state_dir / sb.LAND_GATE_FILE).exists())
