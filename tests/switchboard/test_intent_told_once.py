"""The #1437 tests (an Intent edit is told once), moved out of bin/test-switchboard. build() is called by bin/test-switchboard, which holds TestIntentEditWiring and GithubRig."""
from pathlib import Path
from unittest import mock
from support.loader import sb
from support.scenario_roles import GH_BIN, GH_REPO


def edited_body(intent, done):
    return "## Intent\n%s\nDone when: %s\n" % (intent, done)


def build(Wiring, GithubRig):
    class TestIntentEditToldOnce(Wiring):
        """#1437: an Intent edit is told once per recipient, keyed by issue, edit time and body hash, and the record outlives a restart."""

        def deliver(self, hub, name="cos"):
            for fact in [f for f in hub.facts() if f.kind == "intent.edit" and f.recipient == name]:
                hub.told([fact.key], name)   # the engine's confirmation; the transcript window is not consulted (it cannot match)
                hub.acknowledge([fact.key], name)

        def count(self, hub, name="cos"):
            return len([f for f in hub.facts() if f.kind == "intent.edit" and f.recipient == name])

        def payload(self, new, at):
            issue = dict(self.gh.issues[12], body=new, updated_at=at)
            return {"action": "edited", "issue": issue, "sender": {"login": "you"}, "changes": {"body": {"from": self.OLD}}}

        def test_ten_wakes_with_an_unmatched_transcript_tell_it_once(self):
            self.edit()
            total = 0
            for _ in range(10):
                total += self.count(self.hub)
                self.deliver(self.hub)
            self.assertEqual(total, 1)

        def test_ten_wakes_with_no_acknowledge_offer_a_told_edit_once(self):
            """The recorded case: the edit is never acknowledged, so only the told-record filter stops the repeats."""
            self.edit()
            (fact,) = self.intent_facts()
            self.hub.told([fact.key], "cos")
            offered = [self.count(self.hub) for _ in range(10)]
            self.assertEqual(offered, [0] * 10)

        def test_a_failed_record_write_is_audited_once_per_episode(self):
            self.edit()
            (fact,) = self.intent_facts()
            with mock.patch.object(sb.os, "replace", side_effect=OSError("disk")):
                self.hub.told([fact.key], "cos")
                self.hub.told([fact.key], "cos")
            lines = [l for l in self.rig.world.audit_path.read_text().splitlines() if "%s could not be written" % sb.INTENT_TOLD_FILE in l]
            self.assertEqual(len(lines), 1)
            self.assertNotIn("disk", lines[0])

        def test_two_recipients_are_each_told_once(self):
            rig = GithubRig(self, agents={"platform": {"role": "orchestrator", "charter": 1},
                                          "cos": {"role": "chief-of-staff"}, "cos2": {"role": "chief-of-staff"}})
            rig.gh.issue(1)
            rig.gh.issue(12, parent=1)
            rig.hub.tick()
            rig.world.engine.intents = None
            issue = dict(rig.gh.issues[12], body=self.NEW, updated_at=sb.iso(rig.world.clock.now()))
            rig.gh.send("issues", {"action": "edited", "issue": issue, "sender": {"login": "you"},
                                   "changes": {"body": {"from": self.OLD}}})
            seen = {"cos": 0, "cos2": 0}
            for _ in range(4):
                for name in seen:
                    seen[name] += self.count(rig.hub, name)
                    self.deliver(rig.hub, name)
            self.assertEqual(seen, {"cos": 1, "cos2": 1})

        def test_a_second_real_edit_is_told_once_too(self):
            self.edit()
            first = self.count(self.hub)
            self.deliver(self.hub)
            self.rig.world.clock.t += 60
            self.edit(old=self.NEW, new=edited_body("Add /health", "returns 200, JSON and a version"))
            second = self.count(self.hub)
            self.deliver(self.hub)
            self.assertEqual((first, second, self.count(self.hub)), (1, 1, 0))

        def test_the_record_survives_a_restart(self):
            self.edit()
            payload = self.payload(self.NEW, sb.iso(self.rig.world.clock.now()))
            self.assertEqual(self.count(self.hub), 1)
            self.deliver(self.hub)
            fresh = sb.GitHubHub(self.rig.world.config, self.rig.world.state, None, self.gh, None, self.rig.world.clock.now)
            fresh.attach(self.rig.world.engine)
            fresh.note_issue_edit("issues", GH_REPO, payload)
            self.assertEqual(self.count(fresh), 0)

        def test_a_corrupt_record_reads_as_empty_and_does_not_raise(self):
            (Path(self.rig.world.state) / sb.INTENT_TOLD_FILE).write_text("{not json")
            self.edit()
            self.assertEqual(self.count(self.hub), 1)

        def count_github_writes(self):
            """Count every `gh api` call that is not a plain read (a method other than GET, or a body field)."""
            writes, plain = [], self.gh.runner_plain

            def counting(argv, kwargs):
                method = argv[argv.index("-X") + 1] if "-X" in argv else argv[argv.index("--method") + 1] if "--method" in argv else "GET"
                if method.upper() != "GET" or any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in argv):
                    writes.append(argv[-1])
                return plain(argv, kwargs)
            self.gh.runner_plain = counting
            return writes

        def told_record_read_does_not_write(self, prepare):
            writes = self.count_github_writes()
            prepare()
            self.edit()
            self.assertEqual(self.count(self.hub), 1)             # the edit still counts once
            self.deliver(self.hub)                                # the told-record read and write, on state only
            self.assertEqual(self.count(self.hub), 0)
            self.assertEqual(writes, [])                          # and GitHub was never written to

        def test_a_corrupt_record_raises_nothing_and_writes_nothing_to_github(self):
            self.told_record_read_does_not_write(
                lambda: (Path(self.rig.world.state) / sb.INTENT_TOLD_FILE).write_text("{not json"))

        def test_a_missing_record_raises_nothing_and_writes_nothing_to_github(self):
            def prepare():
                self.assertFalse((Path(self.rig.world.state) / sb.INTENT_TOLD_FILE).exists())
            self.told_record_read_does_not_write(prepare)

        def test_the_write_counter_sees_a_github_write(self):
            writes = self.count_github_writes()
            with self.assertRaises(AssertionError):  # the fake has no write route; the call is counted before it answers
                self.gh.runner(["%s" % GH_BIN, "api", "-X", "POST", "repos/%s/issues/12/comments" % GH_REPO])
            self.assertEqual(len(writes), 1)

    for _name in dir(Wiring):   # the inherited tests are TestIntentEditWiring's own: they run there, not again here
        if _name.startswith("test") and _name not in vars(TestIntentEditToldOnce):
            setattr(TestIntentEditToldOnce, _name, None)
    return TestIntentEditToldOnce
