"""#2243: a due reminder of a lane that is not in a turn reaches it in the next pass, and a ceiling wake is delivered by the pass that writes it."""
from support.loader import sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

PASS = 30   # seconds between two passes of the test world


def build(PR4Base):
    class TestReminderIdleWake(PR4Base):
        def setUp(self):
            super().setUp()
            config = {"steer_after_minutes": 600}
            self.world = self.make_world(config=config)
            self.write_config(dict({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}, **config))
            self.world.add_agent("w-idle", fleet={"role": "coder", "reports_to": "platform", "issue": 10})
            self.world.add_agent("w-other", fleet={"role": "coder", "reports_to": "platform", "issue": 11})

        def stale(self):
            """herdr reads 'working' for the lane while v2 has no turn: the status is stale."""
            self.world.herdr.panes[self.world.panes["w-idle"]]["status"] = "working"

        def run_passes(self, count, due):
            """Runs `count` passes, other traffic every pass; returns {reminder id: the pass number it was delivered in (1 = the first pass at or after due)}."""
            found, ids = {}, dict(due)
            for number in range(1, count + 1):
                self.world.fact("w-other", "other:%d" % number, "traffic %d" % number)
                self.world.tick(PASS)
                self.world.engine.run_pass()
                for event in self.world.audit():
                    if event["event"] == "v2.synthetic" and event.get("subject") == "w-idle":
                        for key in event["detail"].get("keys", []):
                            for name, item_id in ids.items():
                                if key == "reminder:" + item_id:
                                    found.setdefault(name, number)
            return found

        def two_due(self):
            now = self.world.clock.now()
            return {"first": sb.add_reminder(self.world.state, "w-idle", now + 10, now - 600, "roll the build")["id"],
                    "second": sb.add_reminder(self.world.state, "w-idle", now + 250, now - 600, "check the roll")["id"]}

        def test_repro_idle(self):
            due = self.two_due()
            print("IDLE", self.run_passes(40, due))
            print([e["detail"] for e in self.world.audit() if e["event"] in ("reminder.ceiling", "reminder.waiting", "v2.synthetic") and e.get("subject") == "w-idle"])

        def test_repro_stale(self):
            self.stale()
            due = self.two_due()
            print("STALE", self.run_passes(40, due))
            print([e["detail"] for e in self.world.audit() if e["event"] in ("reminder.ceiling", "reminder.waiting", "v2.synthetic") and e.get("subject") == "w-idle"])
        def test_repro_stale_late(self):
            self.stale()
            now = self.world.clock.now()
            due = {"a": sb.add_reminder(self.world.state, "w-idle", now - 700, now - 3600, "roll the build")["id"]}
            print("STALELATE", self.run_passes(5, due))
            print([e["detail"] for e in self.world.audit() if e["event"] in ("reminder.ceiling", "reminder.waiting", "v2.synthetic") and e.get("subject") == "w-idle"])

        def test_repro_engaged(self):
            self.world.you("w-idle"); self.world.tick(1000)
            self.world.v2.advance()
            print("STATUS", self.world.herdr.status(self.world.panes["w-idle"]))
            due = self.two_due()
            print("ENGAGED", self.run_passes(40, due))
            print([e["detail"] for e in self.world.audit() if e["event"] in ("reminder.ceiling", "reminder.waiting", "v2.synthetic") and e.get("subject") == "w-idle"])
    return TestReminderIdleWake
