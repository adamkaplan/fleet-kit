"""#1487: the load figure counts a lane's running cloud workers as busy (the tests of that change; the pin changes stay in TestLoadAlert in bin/test-switchboard)."""
import datetime as _dt
import json
import os
import shutil
import types

from support.core import Base
from support.loader import sb


class TestLoadAlertCloudBusy(Base):
    """#1487: cloud workers count as busy until stopped; a cloud-only lane is not "coders full"; an empty lane keeps its alert."""
    T0 = _dt.datetime(2026, 10, 14, 10, 0, tzinfo=_dt.timezone.utc).timestamp()

    def setUp(self):
        super().setUp()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.state_dir), 0o700)
        self.health = sb.load_fleet_health()

    def latest(self):
        return json.loads((self.state_dir / "load.json").read_text())["latest"]

    def _lane_run(self, local, cloud, stopped=()):
        """#1487: one lane `orch` with `local` working coders and `cloud` cloud workers (those in `stopped` have a final report)."""
        cdir = self.state_dir / "cloud"
        cdir.mkdir(exist_ok=True)
        for i in range(cloud):
            (cdir / ("w%d.json" % i)).write_text(json.dumps({"worker": "w%d" % i, "reports_to": "orch", "hard_deadline": "2999-01-01T00:00:00Z"}))
        (self.state_dir / "last-reports.json").write_text(json.dumps({"w%d" % i: {"state": "done", "at": self.T0} for i in stopped}))
        engine = types.SimpleNamespace(state_dir=str(self.state_dir), now=lambda: self.T0, audit=None, actor="daemon", intents=None,
                                       config={"load_alert": self.health.validate({"enabled": True}, "x")})
        mk = lambda n, role, status="idle", boss=None: types.SimpleNamespace(name=n, session_id=n, status=status, fleet={"role": role, "charter": 1, "repo": "acme/widget", "reports_to": boss})
        agents = {"orch": mk("orch", "orchestrator")}
        agents.update({"c%d" % i: mk("c%d" % i, "coder", "working", "orch") for i in range(local)})
        sb.load_tick(engine, types.SimpleNamespace(agents=agents))
        return self.latest()["orch"]

    def test_1487_cloud_workers_count_as_busy_until_stopped(self):
        fig = self._lane_run(0, 3)
        self.assertEqual((fig["busy"], fig["idle"]), (3, 0))   # S2: 3 running cloud, no local
        self.assertEqual(self._lane_run(0, 3, stopped=(0, 1, 2))["busy"], 0)   # all stopped
        shutil.rmtree(str(self.state_dir / "cloud"))
        self.assertEqual(self._lane_run(1, 3)["busy"], 4)   # mixed adds the two
        self.assertEqual(self._lane_run(1, 3, stopped=(0, 1, 2))["busy"], 1)

    def test_1487_a_lane_with_no_worker_of_either_kind_still_alerts(self):
        """S7: busy 0 (no local, no cloud worker) with stalled asks keeps today's alert; a cloud-only lane's total is busy + idle."""
        cfg = self.health.validate({"enabled": True}, "x")
        fig = self.health.figures("orch", {"asks": [], "busy": 0, "idle": 0, "messages": 0, "decisions": 0, "cloud": 0}, [], self.T0, cfg)
        self.assertEqual((fig["busy"], fig["idle"], fig["cloud"]), (0, 0, 0))
        stalled = {"open_asks": 3, "active_asks": 3, "stalled": 3, "busy": 0, "idle": 0, "cloud": 0, "moved_backlog": 0, "moved_1h": 0}
        self.assertEqual(self.health.crossing(stalled, cfg)[1], ["stalled"])
        self.assertEqual(self.health.crossing(dict(stalled, busy=3, cloud=3), cfg)[1], ["stalled"])

    def test_1487_a_cloud_only_lane_is_not_coders_full_but_an_empty_lane_keeps_the_alert(self):
        cfg = self.health.validate({"enabled": True}, "x")
        base = {"open_asks": 5, "active_asks": 5, "stalled": 0, "moved_backlog": 1, "moved_1h": 0, "charter_moves_1h": 0, "idle": 0}
        cloud_only = dict(base, busy=3, cloud=3)
        local_full = dict(base, busy=2, cloud=0)
        self.assertEqual(self.health.crossing(cloud_only, cfg)[1], [])   # S3: no alert from cloud workers alone
        self.assertEqual(self.health.crossing(local_full, cfg)[1], ["outnumbered"])   # today's alert is kept
        self.assertEqual(self.health.crossing(dict(local_full, busy=1, cloud=0), cfg)[1], ["outnumbered"])
