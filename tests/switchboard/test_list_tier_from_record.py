"""#2491: `decisions list` takes the tier of a row from the escalation record at read time, by the daemon's own key and test (decision_core.is_escalated).

The cause: `decisions escalate` writes escalated-requests.json at once, but the list printed the tier the daemon computed at its last pass (decisions.json), so an escalated
pull request showed as the Chief of Staff's until the next pass. S2 (recorded after the daemon's last write: shown as the owner's), S3 (a record for another head is not applied),
S4 (an unreadable record: the daemon's tier, one note, nothing written), S5 (the control: the current list, `decisions_report`, keeps the cos tier for the S2 case), U1 (the daemon's
own tier from the same shared function). Standard library only."""
import json
import subprocess
import time
from support.core import Base
from support.loader import captured, sb

HEAD, OTHER = "a" * 40, "b" * 40


def row(number, head, tier="cos", **more):
    return dict({"id": "pr:widget#%d" % number, "kind": "captain", "tier": tier, "repo": "acme/widget", "number": number, "head": head, "title": "T%d" % number, "agent": None,
                 "since": 1}, **more)


class TestListTierFromRecord(Base):
    def setUp(self):
        super().setUp()
        self.state = self.state_dir
        self.state.mkdir(parents=True, exist_ok=True)

    def daemon_file(self, rows):
        path = self.state / "decisions.json"
        path.write_text(json.dumps({"decisions": rows, "errors": [], "generation": 1, "written_at": sb.iso(time.time())}))
        return path

    def record(self, *keys):
        (self.state / "escalated-requests.json").write_text(json.dumps({"requests": {k: sb.iso(time.time()) for k in keys}}))

    def tiers(self, report):
        return {d["number"]: d["tier"] for d in report["decisions"]}

    def test_s2_an_escalation_recorded_after_the_daemons_write_is_the_owners_at_once(self):
        self.daemon_file([row(7, HEAD)])
        self.record("captain:acme/widget#7@" + HEAD)
        listed = sb.decisions_listing(self.state, time.time())
        self.assertEqual(self.tiers(listed), {7: "human"})
        self.assertEqual(len([d for d in listed["decisions"] if d["tier"] == "cos"]), 0)
        self.assertNotIn("escalation_note", listed)

    def test_s5_control_the_current_list_keeps_the_cos_tier_for_the_same_case(self):
        self.daemon_file([row(7, HEAD)])
        self.record("captain:acme/widget#7@" + HEAD)
        self.assertEqual(self.tiers(sb.decisions_report(self.state, time.time())), {7: "cos"})   # what the list showed before: the daemon's last tier

    def test_s3_a_record_for_another_head_is_not_applied(self):
        self.daemon_file([row(7, HEAD), row(8, HEAD)])
        self.record("captain:acme/widget#7@" + OTHER, "captain:acme/widget#9@" + HEAD)
        self.assertEqual(self.tiers(sb.decisions_listing(self.state, time.time())), {7: "cos", 8: "cos"})

    def test_a_row_without_a_head_is_never_moved_and_a_human_row_is_never_lowered(self):
        self.daemon_file([row(7, None), row(8, HEAD, tier="human")])
        self.record("captain:acme/widget#7@None", "captain:acme/widget#8@" + HEAD)
        self.assertEqual(self.tiers(sb.decisions_listing(self.state, time.time())), {7: "cos", 8: "human"})

    def test_a_request_row_is_moved_by_its_session_and_request(self):
        self.daemon_file([{"id": "d1", "kind": "permission", "tier": "cos", "session": "ses1", "request": "req9", "title": "x", "number": 1}])
        self.record("ses1:req9")
        self.assertEqual(self.tiers(sb.decisions_listing(self.state, time.time())), {1: "human"})

    def test_s4_an_unreadable_record_leaves_the_daemons_tier_with_one_note_and_writes_nothing(self):
        path = self.daemon_file([row(7, HEAD)])
        (self.state / "escalated-requests.json").write_text("{not json")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.state.iterdir()}
        listed = sb.decisions_listing(self.state, time.time())
        self.assertEqual(self.tiers(listed), {7: "cos"})
        self.assertEqual(len([d for d in listed["decisions"] if d["tier"] == "human"]), 0)
        self.assertIn("cannot be read", listed["escalation_note"])
        self.assertNotIn("\n", listed["escalation_note"])
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.state.iterdir()}, before)
        self.assertTrue(path.exists())

    def test_s4_a_missing_record_is_noted_in_one_line_when_a_cos_row_could_move(self):
        self.daemon_file([row(7, HEAD)])
        listed = sb.decisions_listing(self.state, time.time())
        self.assertEqual(self.tiers(listed), {7: "cos"})
        self.assertIn("is missing", listed["escalation_note"])
        self.assertNotIn("\n", listed["escalation_note"])

    def test_s4_a_missing_record_is_not_noted_when_no_cos_row_could_move(self):
        self.daemon_file([row(7, HEAD, tier="human"), row(8, None)])
        listed = sb.decisions_listing(self.state, time.time())
        self.assertEqual((self.tiers(listed), "escalation_note" in listed), ({7: "human", 8: "cos"}, False))

    def test_s4_a_malformed_record_says_so(self):
        self.daemon_file([row(7, HEAD)])
        (self.state / "escalated-requests.json").write_text("[]")
        self.assertIn("malformed", sb.decisions_listing(self.state, time.time())["escalation_note"])

    def test_an_oversized_record_is_not_read_whole(self):
        self.daemon_file([row(7, HEAD)])
        (self.state / "escalated-requests.json").write_text('{"requests": {"k": "' + "x" * (sb.ESCALATED_READ_MAX + 10) + '"}}')
        self.assertIn("too large", sb.decisions_listing(self.state, time.time())["escalation_note"])

    def test_the_daemons_own_file_is_unchanged_by_the_read(self):
        path = self.daemon_file([row(7, HEAD)])
        self.record("captain:acme/widget#7@" + HEAD)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        sb.decisions_listing(self.state, time.time())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(json.loads(path.read_text())["decisions"][0]["tier"], "cos")

    def test_u1_the_daemon_and_the_list_share_one_key_and_one_test(self):
        core = sb.decision_core
        key = core.captain_escalation_key("acme/widget", 7, HEAD)
        self.assertEqual(key, "captain:acme/widget#7@" + HEAD)
        self.assertEqual(core.entry_escalation_key(row(7, HEAD)), key)
        entry = core.labels_entry("acme/widget", 7, "widget", {"head": HEAD, "title": "t"}, core.is_escalated(key, {key: "t"}))
        self.assertEqual((entry["tier"], core.entry_escalation_key(entry)), ("human", key))
        self.assertEqual(core.labels_entry("acme/widget", 7, "widget", {"head": OTHER, "title": "t"}, core.is_escalated(core.captain_escalation_key("acme/widget", 7, OTHER), {key: "t"}))["tier"], "cos")

    def test_the_command_prints_the_escalated_row_and_the_note_and_writes_nothing(self):
        path = self.daemon_file([row(7, HEAD), row(8, HEAD)])
        self.record("captain:acme/widget#7@" + HEAD)
        before = path.read_bytes()
        refuse = lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "no")
        with captured() as (out, err):
            code = sb.main(["decisions", "--tier", "all", "--json"], runner=refuse, now=time.time)
        body = json.loads(out.getvalue())
        self.assertEqual({d["number"]: d["tier"] for d in body["decisions"]}, {7: "human", 8: "cos"})
        (self.state / "escalated-requests.json").write_text("[")
        with captured() as (out, err):
            sb.main(["decisions", "--tier", "all", "--json"], runner=refuse, now=time.time)
        body = json.loads(out.getvalue())
        self.assertEqual({d["number"]: d["tier"] for d in body["decisions"]}, {7: "cos", 8: "cos"})
        self.assertIn("cannot be read", body["escalation_note"])
        self.assertEqual(path.read_bytes(), before)

    def test_a_hostile_record_or_row_falls_back_and_never_raises(self):
        self.daemon_file([row(7, HEAD)])
        (self.state / "escalated-requests.json").write_text("[" * 200000)
        self.assertIn("cannot be read", sb.decisions_listing(self.state, time.time())["escalation_note"])
        (self.state / "escalated-requests.json").unlink()
        import os
        os.mkfifo(str(self.state / "escalated-requests.json"))
        self.assertIn("cannot be read", sb.decisions_listing(self.state, time.time())["escalation_note"])
        self.assertEqual(sb.decision_core.entry_escalation_key({"kind": "captain", "repo": "a/b", "number": float("inf"), "head": HEAD}), None)

    def test_the_daemons_own_read_has_no_size_bound(self):
        big = {"captain:acme/widget#7@" + HEAD: "t", "pad": "x" * (sb.ESCALATED_READ_MAX + 10)}
        (self.state / "escalated-requests.json").write_text(json.dumps({"requests": big}))
        self.assertIn("captain:acme/widget#7@" + HEAD, sb.read_escalated(self.state))

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.state.iterdir()}

    def test_the_success_path_and_the_command_write_nothing_anywhere_in_the_state_directory(self):
        self.daemon_file([row(7, HEAD)])
        self.record("captain:acme/widget#7@" + HEAD)
        before = self.snapshot()
        self.assertEqual(self.tiers(sb.decisions_listing(self.state, time.time())), {7: "human"})
        with captured():
            sb.main(["decisions", "--tier", "all", "--json"], runner=lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "no"), now=time.time)
        self.assertEqual(self.snapshot(), before)

    def test_hostile_rows_next_to_a_valid_row_leave_the_valid_one_moved(self):
        self.daemon_file(["junk", {"kind": "captain", "repo": "a/b", "number": 1e999, "head": HEAD, "tier": "cos"},
                          {"kind": "captain", "repo": "a/b", "number": 2, "head": 5, "tier": "cos"}, row(7, HEAD)])
        self.record("captain:acme/widget#7@" + HEAD)
        listed = sb.decisions_listing(self.state, time.time())
        self.assertEqual([d["tier"] for d in listed["decisions"] if isinstance(d, dict) and d.get("number") == 7], ["human"])

    def test_no_note_when_no_cos_row_could_move(self):
        self.daemon_file([row(7, HEAD, tier="human"), {"id": "n", "kind": "notice", "tier": "cos", "title": "x"}])
        (self.state / "escalated-requests.json").write_text("{bad")
        self.assertNotIn("escalation_note", sb.decisions_listing(self.state, time.time()))
