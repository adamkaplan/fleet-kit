"""The gate spool reader of ask #2216: the shim's `gate-failopen.jsonl` is read every pass, rotated before its limit, and its fail-opens and over-reviews are seen."""
import json
import os
import stat
import types
from pathlib import Path
from support.core import Base
from support.loader import sb
from support.herdr_world import World
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

NOW = 1_800_000_000.0
BUSY, QUIET = "acme/busy", "acme/quiet"


class TestGateSpool(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("coder",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.state = Path(self.world.state)
        self.path = self.state / sb.GATE_SPOOL_NAME
        self.notes = []
        self.clock = [NOW]
        self.engine = types.SimpleNamespace(state_dir=self.state, now=lambda: self.clock[0], config={},
                                            _note=lambda event, ref, detail: self.notes.append((event, detail)))
        self.reader = sb.GateSpoolFacts()
        self.reader.bind(self.engine)

    def write(self, *records, raw=()):
        with open(str(self.path), "a") as out:
            for r in records:
                out.write(json.dumps(r, sort_keys=True) + "\n")
            for r in raw:
                out.write(r)

    def fail_open(self, repo, n, reason="no answer within 3s", ts=None):
        return {"ts": int(ts or self.clock[0]), "event": "fail_open", "pr": "%s#%d" % (repo, n), "for": "merge", "detail": reason}

    def find(self, cfg=None):
        state = types.SimpleNamespace(repos={}, sources={}, unattributed=0)
        found = sb.sweep_findings(self.engine, state, [], BUSY, self.clock[0], dict(sb.SWEEP_DEFAULTS, **(cfg or {})), None)
        return [f for f in found if f[0] == "S11"], state

    def test_s2_a_full_spool_is_rotated_by_the_pass_and_no_line_is_read_twice(self):
        filler = []
        while not self.path.exists() or self.path.stat().st_size < sb.GATE_SPOOL_ROTATE_AT:
            filler.append(self.fail_open(QUIET, len(filler)))
            self.write(filler[-1])
        self.assertGreaterEqual(self.path.stat().st_size, sb.GATE_SPOOL_ROTATE_AT)
        self.reader()
        self.assertFalse(self.path.exists(), "the full spool was renamed")
        self.assertTrue(self.path.with_name(sb.GATE_SPOOL_NAME + ".1").exists())
        before = len(self.reader.events)
        self.assertEqual(before, len(filler), "every line read once")
        self.write(self.fail_open(QUIET, 9999))   # the shim starts a new file
        self.reader()
        self.reader()
        self.assertEqual(len(self.reader.events), before + 1, "lines read twice: a pass over an unchanged spool adds none")
        self.assertLess(self.path.stat().st_size, sb.GATE_SPOOL_CAP, "writes the shim would refuse: the file is far below its limit")

    def test_s2_a_restart_keeps_the_position_and_a_rotation_by_the_shim_is_followed(self):
        self.write(*[self.fail_open(QUIET, i) for i in range(3)])
        self.reader()
        second = sb.GateSpoolFacts()
        second.bind(self.engine)
        self.write(self.fail_open(QUIET, 10))
        os.replace(str(self.path), str(self.path.with_name(sb.GATE_SPOOL_NAME + ".1")))   # the shim's own rotation (the fix of the shim)
        self.write(self.fail_open(QUIET, 11))
        second()
        self.assertEqual(len(second.events), 3 + 2, "3 from the saved events, the unread rest of .1 and the new file, none twice")
        second()
        self.assertEqual(len(second.events), 5)

    def test_s3_a_burst_raises_one_finding_naming_the_repository_and_a_quiet_hour_none(self):
        self.write(*[self.fail_open(BUSY, i, "no answer within 3s") for i in range(25)], self.fail_open(BUSY, 99, "no switchboard to ask"),
                   *[self.fail_open(QUIET, i) for i in range(3)])
        self.reader()
        found, state = self.find()
        self.assertEqual(len(found), 1, found)
        self.assertIn(BUSY, found[0][1])
        text = state.details[found[0]]
        self.assertIn("26", text)
        self.assertIn("no answer within 3s", text)
        self.assertEqual(self.find({"failopen_per_hour": 100})[0], [], "a higher configured rate raises none")
        self.clock[0] += 2 * 3600
        self.assertEqual(self.find()[0], [], "a quiet hour (the burst is older than an hour) raises none")

    def test_s3_the_rate_is_a_setting_with_a_stated_default(self):
        self.assertEqual(sb.SWEEP_DEFAULTS["failopen_per_hour"], 20)
        for bad in (0, -1, True, "5"):
            with self.assertRaises(sb.ConfigError):
                sb.validate_sweep_config({"sweep": {"failopen_per_hour": bad}}, "t")
        config = {"sweep": {"failopen_per_hour": 5}}
        sb.validate_sweep_config(config, "t")
        self.assertEqual(config["sweep"]["failopen_per_hour"], 5)

    def test_s4_an_over_review_line_is_shown_on_its_pull_request(self):
        pr = "%s#7" % BUSY
        self.write({"ts": int(self.world.engine.now()), "event": "over_review", "pr": pr, "for": "merge", "detail": "by cos at abc | over: G1 | reason: urgent"})
        self.reader()
        decisions = [{"id": "pr:" + pr, "repo": BUSY, "number": 7, "tier": "human", "kind": "notice", "title": "held"},
                     {"id": "pr:%s#8" % BUSY, "repo": BUSY, "number": 8, "tier": "human", "kind": "notice", "title": "other"}]
        engine = self.world.engine
        reader = sb.GateSpoolFacts()
        reader.bind(engine)
        reader()
        shown = [d for d in sb.sweep_decisions(engine, decisions, engine.now()) if d.get("over_review")]
        self.assertEqual([d["number"] for d in shown], [7], "over-review lines missing from their pull request: the other has none")
        self.assertIn("urgent", shown[0]["over_review"][0])
        reader()
        self.assertEqual(len(reader.over_reviews()[pr]), 1, "read once")

    def test_u1_an_unreadable_spool_is_said_once_and_the_pass_completes(self):
        self.write(self.fail_open(QUIET, 1), raw=["{not json\n", "[1,2]\n", '{"event": 5}\n', "\xff\xfe\n"])
        self.reader()
        self.assertEqual(len(self.reader.events), 1, "bad lines are skipped as data")
        self.path.chmod(0)
        try:
            if os.access(str(self.path), os.R_OK):
                self.skipTest("this account reads a mode 0 file")
            for _ in range(3):
                self.assertEqual(self.reader(), [])
            self.assertEqual(len([n for n in self.notes if n[0] == "gate_spool.unreadable"]), 1, "said once")
        finally:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.path.unlink()
        self.path.mkdir()   # a directory where the file should be
        self.assertEqual(self.reader(), [])
        self.assertEqual(self.find()[0], [])
