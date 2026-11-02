"""#1884: the daemon's dead verdict is decided by the registry's one function, `registry.evidence_shows_absent`. Neutral names (acme).
The tests RUN the real functions (`pane_state`, `pane_health_pass`, `daemon_shows_absent`, `registry.shows_dead`) over the fake multiplexer; the only read
of source text is S3 (an ast read of who tests the evidence keys) and the mutants (a text copy of one function, applied once)."""
import ast
import json
from pathlib import Path
from unittest import mock

from support.core import Base
from support.herdr_world import World
from support.loader import SWITCHBOARD, sb

BIN = SWITCHBOARD.parent
NAME = "platform"
MUTANT_LINE = 'return bool(load_module("registry").evidence_shows_absent(evidence))'


def calls_of(fn):
    return {getattr(c.func, "attr", getattr(c.func, "id", None)) for c in ast.walk(fn) if isinstance(c, ast.Call)}


def functions(paths):
    out = {}
    for path in paths:
        for fn in [n for n in ast.walk(ast.parse(Path(path).read_text())) if isinstance(n, ast.FunctionDef)]:
            out.setdefault(fn.name, []).append(fn)
    return out


def tests_keys_for_death(fn):
    """A comparison of the key pane_listed or process_found (not a build of the dict, not a read for display)."""
    return any(isinstance(c, ast.Compare) and any(isinstance(k, ast.Constant) and k.value in ("pane_listed", "process_found") for k in ast.walk(c))
               for c in ast.walk(fn))


def reaches(table, start, goal, seen=None):
    seen = seen if seen is not None else set()
    if start in seen:
        return False
    seen.add(start)
    for fn in table.get(start, ()):
        names = calls_of(fn)
        if goal in names or any(reaches(table, n, goal, seen) for n in names if n in table):
            return True
    return False


def source_copy_function(name, old, new):
    """A function of the entry, from a TEXT COPY of its source with `old` replaced once by `new`; asserted applied once by the caller (returns count)."""
    text = SWITCHBOARD.read_text()
    fn = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    segment = ast.get_source_segment(text, fn)
    count = segment.count(old)
    namespace = dict(sb.__dict__)
    exec(compile(segment.replace(old, new), str(SWITCHBOARD), "exec"), namespace)
    return namespace[name], count


# Each situation: how the multiplexer is arranged (the fake of tests/switchboard/support/herdr_world.py), and what the OLD rules answered.
#  list: the whole list holds the pane ("listed"), not ("absent"), cannot be read ("unreadable"); process: the pane's foreground ("runs" the session, a
#  bare "shell", "unread"); get: pane_get ("ok", "notfound", "error"); judged: the agent is absent from the agent list (the old loop looks).
DEAD_CASES = {
    "pane gone": dict(list="absent", process="unread", get="notfound", missing=True),
    "listed bare shell": dict(list="listed", process="shell", get="ok", missing=True),
    "listed, process unread": dict(list="listed", process="unread", get="ok", missing=True),
    "list cannot be read": dict(list="unreadable", process="shell", get="notfound", missing=True),
    "pane_get not found, still listed, process runs": dict(list="listed", process="runs", get="notfound", missing=False),
    "pane_get fails, not not-found": dict(list="listed", process="runs", get="error", missing=False),
    "row with no pane": dict(list="absent", process="unread", get="notfound", missing=False, nopane=True),
    "listed, no recorded session": dict(list="listed", process="shell", get="ok", missing=True, nosession=True),   # PC-3 (iv): no session, no process to judge
}
CHANGED = ("list cannot be read", "listed, process unread", "pane_get not found, still listed, process runs", "listed, no recorded session")


def old_pane_state(c):
    if c.get("nopane"):
        return "not judged"
    if c["get"] == "notfound":
        return "dead"
    if c["get"] == "error" or c["process"] == "unread":
        return "unknown"
    return "attached" if c["process"] == "runs" else "detached"


def old_loop(c):
    if c.get("nopane") or not c["missing"]:
        return "not judged"
    return "held" if c["process"] == "runs" else "dead"


class Rig:
    """One world, one agent, arranged as a case says."""

    def __init__(self, test, case):
        self.test = test
        self.case = case
        test.rigs = getattr(test, "rigs", 0) + 1   # one state directory per world: two worlds of one test never share a record
        self.world = World(test, agents=(NAME,), state=test.home / ("world%d" % test.rigs))
        self.engine = self.world.engine
        self.engine.state_dir = self.world.state
        self.herdr = self.world.herdr
        self.pane, self.session = self.world.panes[NAME], self.world.sessions[NAME]
        self.registry = sb.load_module("registry")
        self.reconcile = sb.load_module("agent_reconcile")
        self.row = {"pane": self.pane, "session": None if case.get("nosession") else self.session}

    def pass_once(self, offset=0):
        self.engine.discover(self.world.clock.now())
        sb.pane_health_pass(self.engine, self.world.clock.now() + offset)

    def arrange(self, for_pane_state=False):
        c, herdr = self.case, self.herdr
        self.saved = herdr.panes.get(self.pane, getattr(self, "saved", None))   # the agent's pane as the fake holds it (put back by `rediscover`)
        if c.get("nosession"):   # the registry row never got a session id; the agent is no longer discovered
            self.registry.upsert(self.world.state, NAME, {"pane": self.pane, "session": None, "role": None, "workspace": None, "cwd": None})
        if c.get("nopane"):
            self.row = {"pane": None, "session": self.session, "endpoint": "http://acme.invalid/x"}
            return
        if c["process"] == "shell" and for_pane_state:   # the pane is up and an agent pane of the fake, its foreground a bare shell and a screen that names nobody
            herdr.panes[self.pane].update(program="zsh", title="OpenCode", screen="  Build auto · model\n")
        elif c["list"] == "absent":
            del herdr.panes[self.pane]
        elif c["process"] in ("shell", "unread") and c["missing"]:   # no longer an agent of the fake, still a terminal of its list
            del herdr.panes[self.pane]
            herdr.plain_terminals = [{"pane_id": self.pane, "processes": ["zsh"], **({"process_fails": True} if c["process"] == "unread" else {})}]
        if c["list"] == "unreadable":
            self.patch(sb.HerdrClient, "pane_list", side_effect=sb.HerdrError("down", "herdr is down"))
        if c["get"] == "error":
            self.patch(sb.HerdrClient, "pane_get", side_effect=sb.HerdrError("boom", "herdr said boom"))
        elif c["get"] == "notfound" and c["list"] != "absent" and self.pane in herdr.panes:
            self.patch(sb.HerdrClient, "pane_get", side_effect=sb.HerdrError("failed", "pane %s not found" % self.pane))
        if c["process"] == "unread" and self.pane in herdr.panes:
            self.patch(sb.HerdrClient, "pane_process_info", side_effect=sb.HerdrError("down", "process list cannot be read"))

    def patch(self, target, attr, **kw):
        patcher = mock.patch.object(target, attr, **kw)
        patcher.start()
        self.test.addCleanup(patcher.stop)

    def evidence(self, state="dead"):
        row = dict(self.row, state=state)
        return row, self.reconcile.evidence_of(row, sb.herdr_live_check(self.engine.herdr)(NAME, row))

    def new_daemon_dead(self):
        return sb.daemon_shows_absent(self.engine, NAME, dict(self.row))

    def shows_dead(self):
        row, evidence = self.evidence()
        return self.registry.shows_dead(row, evidence)

    def events(self, event="fleetpane.dead"):
        return [e for e in self.world.audit() if e["event"] == event]

    def pane_state(self):
        return sb.pane_state(self.engine, NAME, self.session, self.pane)[0]

    def three_passes(self):
        """The agent is seen once (so the pass knows it), then the world changes and it is absent from the agent list for PANE_MISSING_PASSES passes."""
        self.pass_once()
        self.arrange()
        for n in range(sb.PANE_MISSING_PASSES):
            self.pass_once(offset=n + 1)

    def absent_passes(self, count, start=0):
        """`count` more passes with the agent absent from the agent list (offsets after `start`)."""
        for n in range(count):
            self.pass_once(offset=start + n + 1)

    def rediscover(self):
        """The agent comes back to the agent list (its pane as before): a pass that DECIDES for it, the streak is over."""
        self.herdr.panes[self.pane] = self.saved
        self.herdr.plain_terminals = []
        self.pass_once(offset=100)

    def undecidable_lines(self):
        return self.events("pane.undecidable")

    def recorded(self):
        return (sb.read_pane_health(self.world.state)["states"].get(NAME) or {}).get("state")


class TestDaemonDeadVerdict(Base):

    # -- S2 / S9: the table
    def table(self):
        rows = {}
        for label, case in DEAD_CASES.items():
            rig = Rig(self, case)
            rig.arrange()
            ps, lp = old_pane_state(case), old_loop(case)
            rows[label] = {"old pane_state": ps, "old loop": lp,
                           "new daemon": "dead" if rig.new_daemon_dead() else "not dead", "shows_dead": "dead" if rig.shows_dead() else "not dead"}
        return rows

    def test_S2_the_table_has_8_cases_by_4_deciders_and_prints_32_answers(self):
        rows = self.table()
        self.assertEqual((len(rows), sum(len(r) for r in rows.values())), (8, 32))
        for label, row in rows.items():
            self.assertEqual(row["new daemon"], row["shows_dead"], label)   # one function: the daemon and the registry agree on every case
        self.assertEqual({k: v["new daemon"] for k, v in rows.items()},
                         {"pane gone": "dead", "listed bare shell": "dead", "listed, process unread": "not dead", "list cannot be read": "not dead",
                          "pane_get not found, still listed, process runs": "not dead", "pane_get fails, not not-found": "not dead", "row with no pane": "not dead",
                          "listed, no recorded session": "not dead"})

    def test_S9_exactly_the_four_cases_of_the_plan_differ(self):
        rows = self.table()
        differing = sorted(k for k, r in rows.items() if ("dead" in (r["old pane_state"], r["old loop"])) != (r["new daemon"] == "dead"))
        self.assertEqual(differing, sorted(CHANGED))
        self.assertEqual(len(differing), 4)
        doc = (BIN.parent / "docs" / "agent-registry.md").read_text().split("## One dead verdict", 1)[1].split("\n## ", 1)[0]
        for phrase in ("cannot be read", "process that cannot be read", "pane_get", "no recorded session", "Changed cases (four)"):
            self.assertIn(phrase, doc)

    # -- S3: one function
    def test_S3_exactly_one_function_tests_the_evidence_keys_for_death_and_four_callers_reach_it(self):
        table = functions([BIN / "registry.py", BIN / "agent_reconcile.py", SWITCHBOARD])
        testers = sorted(n for n, fns in table.items() if any(tests_keys_for_death(f) for f in fns))
        self.assertEqual(testers, ["evidence_shows_absent"])
        for caller in ("shows_dead", "reconcile_remove", "reconcile_on_read", "pane_health_pass"):
            self.assertTrue(reaches(table, caller, "evidence_shows_absent"), caller)

    # -- S4: a read that failed is never death
    def test_S4_four_cases_are_not_recorded_dead_and_write_no_dead_event(self):
        cases = {"pane_get fails, not not-found": DEAD_CASES["pane_get fails, not not-found"], "list cannot be read": DEAD_CASES["list cannot be read"],
                 "listed, process unread": DEAD_CASES["listed, process unread"], "row with no pane": DEAD_CASES["row with no pane"]}
        for label, case in cases.items():
            with self.subTest(label):
                rig = Rig(self, case)
                if case.get("nopane"):
                    rig.arrange()
                    self.assertFalse(rig.new_daemon_dead())
                    self.assertTrue(rig.registry.no_pane_to_judge(rig.row))
                    continue
                rig.three_passes()
                self.assertNotEqual(rig.recorded(), "dead")
                self.assertNotEqual((rig.registry.read(rig.world.state)["agents"].get(NAME) or {}).get("state"), "dead")
                self.assertEqual(rig.events(), [])
                if case["get"] != "ok":
                    self.assertEqual(rig.pane_state(), sb.PANE_UNKNOWN)

    def test_S4_the_listed_pane_with_the_process_unread_keeps_its_last_state(self):
        rig = Rig(self, DEAD_CASES["listed, process unread"])
        rig.three_passes()
        self.assertNotIn(rig.recorded(), ("dead", None))   # the loop leaves the last known state (here `attached`): it records no death

    # -- S5: absence, three passes
    def test_S5_three_passes_of_absence_record_dead_once_and_the_registry_removes_it_on_the_same_evidence(self):
        rig = Rig(self, DEAD_CASES["pane gone"])
        rig.three_passes()
        self.assertEqual(rig.recorded(), "dead")
        self.assertEqual(len(rig.events()), 1)
        row, evidence = rig.evidence(state=None)
        row = rig.registry.read(rig.world.state)["agents"][NAME]
        self.assertEqual(row["state"], "dead")
        evidence = rig.reconcile.evidence_of(row, sb.herdr_live_check(rig.engine.herdr)(NAME, row))
        got = rig.registry.reconcile_remove(rig.world.state, NAME, evidence, True)
        self.assertTrue(got["changed"], got)
        self.assertNotIn(NAME, rig.registry.read(rig.world.state)["agents"])
        self.assertEqual((evidence["pane_listed"], evidence["process_found"]), (False, False))

    # -- S10: a listed bare shell
    def test_S10_a_listed_bare_shell_is_detached_in_pane_state_and_dead_where_death_is_recorded(self):
        rig = Rig(self, DEAD_CASES["listed bare shell"])
        rig.arrange(for_pane_state=True)
        self.assertEqual(rig.pane_state(), sb.PANE_DETACHED)
        self.assertTrue(rig.shows_dead())
        loop = Rig(self, DEAD_CASES["listed bare shell"])
        loop.three_passes()
        self.assertEqual(loop.recorded(), "dead")
        self.assertEqual(len(loop.events()), 1)
        row = loop.registry.read(loop.world.state)["agents"][NAME]
        self.assertTrue(loop.registry.shows_dead(row, loop.reconcile.evidence_of(row, sb.herdr_live_check(loop.engine.herdr)(NAME, row))))

    def test_S10_the_same_pane_with_the_process_unread_is_not_dead(self):
        loop = Rig(self, DEAD_CASES["listed, process unread"])
        loop.three_passes()
        self.assertEqual((loop.recorded(), loop.events()), ("attached", []))   # not dead: the loop changes nothing
        self.assertFalse(loop.shows_dead())
        state = Rig(self, DEAD_CASES["listed bare shell"])
        state.arrange(for_pane_state=True)
        state.patch(sb.HerdrClient, "pane_process_info", side_effect=sb.HerdrError("down", "process list cannot be read"))
        self.assertNotEqual(state.pane_state(), sb.PANE_DEAD)   # pane_state's own answer to an unread process is `unknown` (it cannot say `detached` without the process)
        self.assertFalse(state.shows_dead())


class TestDaemonDeadVerdictMutants(Base):
    """S6 and S10: a mutant of one function of the entry (a text copy, applied once) fails the check; a control passes first."""

    def run_check(self, name, old, new, expect):
        control = self.violations_with(None)
        self.assertEqual(control, [], "control: the check must pass on the unmutated daemon")
        func, count = source_copy_function(name, old, new)
        self.assertEqual(count, 1, "the mutation must be applied exactly once")
        found = self.violations_with((name, func))
        self.assertTrue(any(expect in f for f in found), found)

    def violations_with(self, patch):
        if patch is None:
            return self.collect()
        with mock.patch.object(sb, patch[0], patch[1]):
            return self.collect()

    def collect(self):
        found = []
        rig = Rig(self, DEAD_CASES["listed bare shell"])
        rig.three_passes()
        if not (rig.recorded() == "dead" and len(rig.events()) == 1):
            found.append("a listed bare shell is not recorded dead by the loop")
        rig = Rig(self, DEAD_CASES["listed, process unread"])
        rig.three_passes()
        if rig.recorded() == "dead" or rig.events():
            found.append("an unread process is recorded dead")
        return found

    def test_S6_mutant_the_daemon_decides_death_by_its_own_test_again(self):
        self.run_check("daemon_shows_absent", MUTANT_LINE,
                       'return evidence.get("pane_listed") is False and evidence.get("process_found") is False',
                       "a listed bare shell is not recorded dead by the loop")

    def test_S6_mutant_a_failed_read_is_taken_as_absence(self):
        self.run_check("herdr_live_check", 'return {"pane": True, "process": None}\n    return check',
                       'return {"pane": True, "process": False}\n    return check', "an unread process is recorded dead")

    def test_S10_mutant_death_requires_the_pane_to_be_gone(self):
        self.run_check("daemon_shows_absent", MUTANT_LINE, 'return evidence.get("pane_listed") is False and bool(load_module("registry").evidence_shows_absent(evidence))',
                       "a listed bare shell is not recorded dead by the loop")


class TestUndecidableStreak(Base):
    """S11 (PC-6): a LISTED pane whose agent row has no recorded session id is answered `unknown` by the missing-pass loop; the streak is said ONCE."""

    N = property(lambda self: sb.PANE_MISSING_PASSES)

    def streak(self, answers, rig=None):
        """A rig for the no-session case, the agent seen once, then absent: PANE_MISSING_PASSES - 1 passes that cannot answer, then `answers` passes that answer unknown."""
        rig = rig or Rig(self, DEAD_CASES["listed, no recorded session"])
        rig.pass_once()
        rig.arrange()
        rig.absent_passes(self.N - 1 + answers)
        return rig

    def test_S11_two_answers_write_no_line_three_write_one_six_still_one_and_never_dead(self):
        for answers, lines in ((2, 0), (3, 1), (6, 1)):
            with self.subTest(answers=answers):
                rig = self.streak(answers)
                self.assertEqual(len(rig.undecidable_lines()), lines)
                self.assertNotEqual(rig.recorded(), "dead")
                self.assertEqual(rig.events(), [])   # 0 fleetpane.dead
                if lines:
                    line = rig.undecidable_lines()[0]
                    self.assertEqual((line["name"] if "name" in line else NAME, line["detail"]["pane"], line["detail"]["passes"]), (NAME, rig.pane, self.N))

    def test_S11_a_pass_that_decides_clears_the_streak_and_a_later_streak_writes_a_new_line(self):
        rig = self.streak(3)
        self.assertEqual(len(rig.undecidable_lines()), 1)
        rig.rediscover()
        self.assertEqual(sb.read_pane_health(rig.world.state)["states"][NAME].get("undecidable"), None)
        self.assertNotEqual(rig.recorded(), "dead")
        self.assertEqual(len(rig.undecidable_lines()), 1)
        rig.arrange()   # the same agent, no recorded session again: a later streak
        rig.absent_passes(self.N - 1 + 2, start=100)
        self.assertEqual(len(rig.undecidable_lines()), 1)   # 2 answers: not yet
        rig.absent_passes(1, start=200)
        self.assertEqual(len(rig.undecidable_lines()), 2)
        self.assertEqual(rig.events(), [])

    def test_S11_the_streak_is_kept_in_the_agent_state_record_for_the_doctor(self):
        rig = self.streak(4)
        self.assertEqual(sb.read_pane_health(rig.world.state)["states"][NAME]["undecidable"], 4)

    def test_S11_mutant_a_line_for_each_pass(self):
        control = self.collect(None)
        self.assertEqual(control, [], "control: the check must pass on the unmutated daemon")
        func, count = source_copy_function("undecidable_streak", 'record["undecidable"] == PANE_MISSING_PASSES and ', "")
        self.assertEqual(count, 1, "the mutation must be applied exactly once")
        found = self.collect(func)
        self.assertTrue(any("pane.undecidable is written more than once for one streak" in f for f in found), found)

    def collect(self, func):
        with mock.patch.object(sb, "undecidable_streak", func or sb.undecidable_streak):
            rig = self.streak(6)
        return [] if len(rig.undecidable_lines()) == 1 else ["pane.undecidable is written more than once for one streak"]
