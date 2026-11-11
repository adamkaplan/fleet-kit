"""#2059: the owner is told when the Chief of Staff is blocked: a new record, owner-alarm.json, written by the blocked report and drawn by the owner's panel
(S2 to S5, U3). The panel's own tests are node tests (plugins/fleet-decisions-tui/alarm.test.mjs)."""
import importlib.machinery
import importlib.util
import json
import re
import unittest
from unittest import mock

from support.core import Base
from support.herdr_world import World
from support.loader import SWITCHBOARD, sb
from support.rebind import rebound

NOT_THE_BLOCKED_AGENT = "the owner is told, not the blocked agent"
ANCHOR = "            owner_alarm_start(engine.state_dir, observation.name,"


class TestCosBlockedOwner(Base):
    """The record the owner's panel draws, over the real pass with a fake herdr (the record is the channel: one object, one line of text)."""

    def build(self, held=True, status="blocked", worker=False):
        world = World(self, agents=(), config={})
        world.add_agent("cos", fleet={"role": "chief-of-staff"})
        if worker:
            world.add_agent("coder", fleet={"role": "coder", "reports_to": "cos"})
        self.world = world
        who = "coder" if worker else "cos"
        world.herdr.panes[world.panes[who]]["status"] = status
        if held:
            world.engine.send("x", who, "hello")
        world.engine.run_pass()
        return world

    def record(self):
        return sb._read_small(self.world.state, sb.OWNER_ALARM_STATE).get("alarm")

    def lines(self):
        return [e for e in self.world.audit() if e["event"] in ("blocked.report", "blocked.repeat", "blocked.end") and e["subject"] == "cos"]

    def test_the_owner_is_told_once_with_the_name_the_minutes_what_it_waits_on_and_the_resume_command(self):   # S2, U3
        world = self.build()
        self.assertIsNone(self.record(), "under the threshold nothing is raised")
        world.tick(1500)
        world.engine.run_pass()
        found = self.record()
        self.assertEqual((found["agent"], found["minutes"], found["ended"]), ("cos", 25, None))
        for part in ("cos", "25 minutes", "an ended turn", sb.RESUME_COMMAND + " cos"):
            self.assertIn(part, found["text"])
        self.assertNotIn("\n", found["text"])
        self.assertIsNone(re.search(r"(?i)token|secret|bearer|ghp_", json.dumps(found)))

    def test_it_repeats_hourly_and_ends_with_one_line_and_quiet_cases_raise_nothing(self):   # S3
        world = self.build()
        world.tick(1500)
        world.engine.run_pass()
        world.tick(600)
        world.engine.run_pass()
        self.assertEqual(len(self.lines()), 1, "not at every pass")
        world.tick(65 * 60)
        world.engine.run_pass()
        self.assertEqual([e["event"] for e in self.lines()], ["blocked.report", "blocked.repeat"])
        self.assertEqual(self.record()["minutes"], 100)
        world.herdr.panes[world.panes["cos"]]["status"] = "idle"
        world.tick(60)
        world.engine.run_pass()
        self.assertEqual(len(self.lines()), 3)
        self.assertIsNotNone(self.record()["ended"])
        self.assertIn("block has ended", self.record()["text"])

    def test_a_record_that_cannot_be_written_is_noted_and_the_report_still_goes_out(self):   # S6
        world = self.build()
        world.tick(1500)
        real = sb.blocked_update
        def refuse(state_dir, name, change):
            if name == sb.OWNER_ALARM_STATE:
                raise OSError("disk full")
            return real(state_dir, name, change)
        with mock.patch.object(sb, "blocked_update", refuse), mock.patch.object(sb.sys, "stderr") as err:
            world.engine.run_pass()
        self.assertIsNone(self.record())
        self.assertEqual(len(self.lines()), 1, "the audited report is made without the record")
        self.assertIn("owner-alarm: not written (OSError)", "".join(c.args[0] for c in err.write.call_args_list))

    def test_a_chief_of_staff_blocked_with_nothing_held_raises_nothing(self):   # S3
        world = self.build(held=False)
        world.tick(5000)
        world.engine.run_pass()
        self.assertIsNone(self.record())

    def test_a_chief_of_staff_that_is_not_blocked_raises_nothing(self):   # S3
        world = self.build(status="idle")
        world.tick(5000)
        world.engine.run_pass()
        self.assertIsNone(self.record())

    def test_a_blocked_worker_is_reported_as_before_and_the_owner_gets_nothing(self):   # S4
        world = self.build(worker=True)
        world.tick(1500)
        world.engine.run_pass()
        self.assertIsNone(self.record())
        (report,) = [e for e in world.audit() if e["event"] == "blocked.report"]
        self.assertEqual((report["subject"], report["detail"]["told"]), ("coder", ["cos"]))
        self.assertIn("coder has been blocked for 25 minutes", report["detail"]["text"])

    def scenario(self):
        world = self.build()
        world.tick(1500)
        world.engine.run_pass()
        self.assertEqual(self.record()["agent"], "cos")
        self.assertEqual(sb.read_reminders(world.state), [], NOT_THE_BLOCKED_AGENT)
        (report,) = [e for e in world.audit() if e["event"] == "blocked.report"]
        self.assertEqual(report["detail"]["told"], [], NOT_THE_BLOCKED_AGENT)

    def run_scenario(self):   # a fresh test case (its own state directory), under the module now bound as `sb`
        result = unittest.TestResult()
        TestCosBlockedOwner("scenario").run(result)
        return result

    def mutant_module(self, old, new):
        text = SWITCHBOARD.read_text()
        self.assertEqual(text.count(old), 1)
        loader = importlib.machinery.SourceFileLoader("t_fleet_switchboard_alarm_mutant", str(SWITCHBOARD))
        loader.get_data = lambda path: text.replace(old, new).encode()
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module

    def test_a_mutant_that_alarms_the_blocked_agent_itself_is_caught_and_the_control_passes_first(self):   # S5
        control = self.run_scenario()   # the control: the real code passes
        self.assertEqual((control.failures, control.errors), ([], []))
        module = self.mutant_module(ANCHOR, "            recipients.append(observation.name)\n" + ANCHOR)
        with mock.patch.dict(globals(), {"sb": module}), rebound("sb", module):
            mutant = self.run_scenario()
        self.assertEqual(len(mutant.failures), 1, (mutant.failures, mutant.errors))
        self.assertIn(NOT_THE_BLOCKED_AGENT, mutant.failures[0][1])


class BlockedReportOwnerAlarm:
    """The two killer tests of the blocked-section guard table; mixed into TestBlockedReport (bin/test-switchboard), which supplies block(), wait() and audits()."""

    def alarm(self):
        return sb._read_small(self.world.state, sb.OWNER_ALARM_STATE).get("alarm")

    def test_a_blocked_chief_of_staff_is_recorded_for_the_owner_once_and_the_end_is_recorded(self):   # #2059
        world = World(self, agents=(), config={})
        world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.world = world
        world.herdr.panes[world.panes["cos"]]["status"] = "blocked"
        world.engine.send("x", "cos", "hello")
        world.engine.run_pass()
        world.tick(1500)
        world.engine.run_pass()
        found = self.alarm()
        self.assertEqual((found["agent"], found["minutes"], found["ended"]), ("cos", 25, None))
        world.herdr.panes[world.panes["cos"]]["status"] = "idle"
        world.tick(60)
        world.engine.run_pass()
        self.assertIsNotNone(self.alarm()["ended"])

    def test_a_blocked_worker_leaves_no_owner_alarm(self):   # #2059
        self.block()
        self.wait(25)
        self.assertEqual(len(self.audits()), 1)
        self.assertIsNone(self.alarm())


if __name__ == "__main__":
    unittest.main()
