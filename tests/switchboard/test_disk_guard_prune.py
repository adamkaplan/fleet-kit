"""#2157: the disk guard prunes docker's build cache older than six hours, once, bounded, said (S2 to S7, U1). Every docker here is a fake: no real docker command runs."""
import json
import shutil
import subprocess
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from support.loader import _load, sb
from support.paths import BIN

GB = 10 ** 9
HOUR = 3600
DOCKER = "/fake/bin/docker"
COMMAND = "docker builder prune -f --filter until=6h"
A_COMMAND = "the prune command is exactly docker builder prune -f --filter until=6h"
A_HOUR = "a second episode within an hour of the last prune does not prune"
A_RAISE = "a docker failure never raises out of the pass"
Usage = namedtuple("usage", "total used free")


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def check_properties(mod, rig, prunes):
    """Properties over EVERY recorded docker call (AGENTS.md: no exact argument list, no number): `prunes` is the count of prune calls expected."""
    found = rig.prunes()
    need(len(found) == prunes, "%d prune call(s) expected, got %d: %r" % (prunes, len(found), rig.calls))
    for argv, kwargs in zip(rig.calls, rig.kwargs):
        need(argv[1] != "info" or argv[2:3] == ["--format"], "docker info is the only other call: %r" % (argv,))
        need("-a" not in argv and "--all" not in argv, "%s: no -a or --all: %r" % (A_COMMAND, argv))
        need(not {"image", "volume", "system", "container"} & set(argv), "%s: no image, volume, system or container call: %r" % (A_COMMAND, argv))
    for argv, kwargs in ((c, k) for c, k in zip(rig.calls, rig.kwargs) if c[1:3] == ["builder", "prune"]):
        need(argv[1:3] == ["builder", "prune"], "%s: the subcommand pair is builder prune: %r" % (A_COMMAND, argv))
        need("--filter" in argv and argv[argv.index("--filter") + 1:argv.index("--filter") + 2] == ["until=6h"], "%s: the filter is until=6h: %r" % (A_COMMAND, argv))
        timeout = kwargs.get("timeout")
        need(isinstance(timeout, (int, float)) and 0 < timeout <= mod.DISK_PRUNE_GUARD_TIMEOUT, "the prune carries a timeout, positive and at most the guard's bound: %r" % (timeout,))
        need(kwargs.get("stdin") == subprocess.DEVNULL, "the prune has no stdin")


class Audit:
    def __init__(self):
        self.lines = []

    def append(self, actor, event, subject, detail):
        self.lines.append((event, dict(detail)))


class Rig:
    """The guard of `mod` on a fake engine, a fake clock, a fake volume and a fake docker. A context manager: it patches the disk and docker lookup of `mod`."""

    def __init__(self, mod, config, free_gb=40, docker=DOCKER, info="Docker Desktop", freed_gb=5, state=None):
        self.mod, self.free, self.freed, self.docker, self.info, self.mode = mod, [free_gb * GB], freed_gb * GB, docker, info, "ok"
        self.state = Path(state or tempfile.mkdtemp(prefix="pruneguard-"))
        self.calls, self.kwargs, self.clock = [], [], [1000000.0]
        self.audit = Audit()
        agent = mock.Mock(fleet={"role": "chief-of-staff"})
        self.engine = mock.Mock(config=config, state_dir=self.state, audit=self.audit, actor="daemon", discovery=mock.Mock(agents={"cos": agent}, problems=[]))
        self.engine.now = lambda: self.clock[0]
        self.engine._note = lambda event, subject, detail: self.audit.append("daemon", event, subject, detail)
        self.engine.disk_prune_runner = self.runner
        self.guard = None
        self.restart()

    def restart(self):
        self.guard = self.mod.DiskFacts()
        self.guard.bind(self.engine)

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if argv[1] == "info":
            return subprocess.CompletedProcess(argv, 0, self.info + "\n", "")
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        if self.mode == "fail":
            return subprocess.CompletedProcess(argv, 1, "", "boom: no space")
        self.free[0] += self.freed
        return subprocess.CompletedProcess(argv, 0, "Total reclaimed space: 5GB\n", "")

    def __enter__(self):
        self.patches = [mock.patch.object(shutil, "disk_usage", lambda path: Usage(1000 * GB, 1000 * GB - self.free[0], self.free[0])),
                        mock.patch.object(self.mod, "_docker_executable", lambda name: self.docker)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()

    def step(self, minutes=0, free_gb=None):
        self.clock[0] += minutes * 60
        if free_gb is not None:
            self.free[0] = free_gb * GB
        return self.guard()

    def prunes(self):
        return [c for c in self.calls if c[1:3] == ["builder", "prune"]]

    def events(self, name):
        return [d for e, d in self.audit.lines if e == name]


def check_command(mod):   # S2
    with Rig(mod, {"disk_guard": {"prune": True}}) as rig:
        facts = rig.step()
        for minute in range(10, 190, 10):   # three hours of passes in one low episode
            facts = rig.step(10, 40)
        check_properties(mod, rig, 1)
        (pruned,) = rig.events("disk.pruned")
        need(pruned["command"] == COMMAND and pruned["exit_status"] == 0 and pruned["bytes_freed"] == rig.freed, "disk.pruned names the command, the status and the bytes freed: %r" % pruned)
        need(len(facts) == 1 and COMMAND in facts[0].summary and "freed 5.0 GB" in facts[0].summary, "the notice says what ran and how much it freed")   # S5


def check_hour(mod):   # S3
    with Rig(mod, {"disk_guard": {"prune": True}}) as rig:   # an episode of 3 hours: one prune
        for minute in range(0, 180, 10):
            rig.step(10 if minute else 0, 40)
        need(len(rig.prunes()) == 1, "an episode of three hours prunes once: got %d" % len(rig.prunes()))
    with Rig(mod, {"disk_guard": {"prune": True}}) as rig:   # a second episode 30 minutes after the prune
        rig.step()
        rig.step(10, 60)
        facts = rig.step(20, 40)
        facts = rig.step(10, 40)
        need(len(rig.prunes()) == 1, "%s: got %d" % (A_HOUR, len(rig.prunes())))
        need("less than an hour ago" in facts[0].summary, "the notice says the prune was skipped and why")
    with Rig(mod, {"disk_guard": {"prune": True}}) as rig:   # 61 minutes after: one more; then a restart inside the hour: none
        rig.step()
        rig.step(10, 60)
        rig.step(51, 40)
        need(len(rig.prunes()) == 2, "an episode beginning 61 minutes after the last prune prunes: got %d" % len(rig.prunes()))
        rig.step(5, 60)
        rig.restart()
        rig.step(10, 40)
        need(len(rig.prunes()) == 2, "%s (after a restart: the time is read from the file): got %d" % (A_HOUR, len(rig.prunes())))


def check_failures(mod):   # S4
    cases = (("no docker", {"docker": None}, None, "not installed"), ("timeout", {}, "timeout", "did not answer within"), ("exit 1", {}, "fail", "exit status"))
    for label, rig_args, mode, said in cases:
        with Rig(mod, {"disk_guard": {"prune": True}}, **rig_args) as rig:
            rig.mode = mode
            try:
                facts = rig.step()
                for _ in range(3):
                    facts = rig.step(10, 40)
            except Exception as err:
                raise AssertionError("%s: %s raised %r" % (A_RAISE, label, err))
            failed = rig.events("disk.prune_failed")
            need(len(failed) == 1 and said in failed[0]["reason"], "%s: one audit line naming the failure: %r" % (label, failed))
            need(len(facts) == 1 and said in facts[0].summary, "%s: the notice is still sent and says it: %r" % (label, facts))
            need(rig.events("disk.pruned") == [], "%s: nothing was pruned" % label)


def check_off(mod):   # S6
    with Rig(mod, {"disk_guard": {"prune": False}}) as rig:
        rig.step()
        rig.step(10, 40)
        need(rig.calls == [], "the switch false runs no docker at all: %r" % rig.calls)   # nothing at all: no call to check
    with Rig(mod, {"disk_guard": {}}, info="Ubuntu 24.04 LTS") as rig:
        rig.step()
        check_properties(mod, rig, 0)
        need(rig.calls, "docker was asked what it is")
    with Rig(mod, {"disk_guard": {}}) as rig:
        rig.step()
        check_properties(mod, rig, 1)


class TestDiskGuardPrune(unittest.TestCase):
    """The guard's prune (ask #2157), on a fake docker; the mutants are copies of the guard's source."""

    @classmethod
    def copy(cls, name, edits):
        directory = Path(tempfile.mkdtemp(prefix="prune-" + name + "-"))
        shutil.copytree(str(BIN), str(directory / "bin"), ignore=shutil.ignore_patterns("__pycache__", "test-*"))   # the whole of bin/ but the tests: the entry loads its neighbours
        directory = directory / "bin"
        path = directory / "switchboard" / "facts.py"
        text = path.read_text()
        for old, new in edits:
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        path.write_text(text)
        return _load("t_prune_" + name, directory / "fleet-switchboard")

    def test_s2_the_exact_command_once_per_episode_bounded_and_audited(self):
        check_command(sb)

    def test_s3_once_per_episode_never_within_an_hour_and_the_time_survives_a_restart(self):
        check_hour(sb)

    def test_s4_a_missing_docker_a_timeout_and_a_failed_prune_are_said_once_and_the_notice_is_sent(self):
        check_failures(sb)

    def test_s5_the_notice_says_what_ran_and_what_it_freed(self):
        with Rig(sb, {"disk_guard": {"prune": True}}) as rig:
            (fact,) = rig.step()
        self.assertIn("the guard ran `%s` and freed 5.0 GB" % COMMAND, fact.summary)
        self.assertTrue(fact.summary.startswith("disk low: 40 GB free"))

    def test_s6_the_switch_and_docker_desktop_decide_and_the_switch_is_a_boolean(self):
        check_off(sb)
        sb.validate_disk_guard({"prune": True}, "t")
        sb.validate_disk_guard({"min_free_gb": 80, "prune": False}, "t")
        for bad in ("yes", 1, None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(sb.ConfigError):
                    sb.validate_disk_guard({"prune": bad}, "t")
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": "x", "disk_guard": {"prune": "yes"}})

    def test_u1_with_the_prune_off_the_audit_the_notice_and_the_file_are_as_they_were(self):
        notice = ("disk low: 40 GB free on the volume holding the state directory (the guard is 50 GB, hard line 10 GB). `fleet-switchboard disk prune --dry-run` shows what "
                  "would be pruned (docker build cache and images older than a week, never volumes; it measures the state directory's volume, so check where docker keeps its data)")
        for config in ({"disk_guard": {"prune": False}}, {}):   # off by the switch; and a test engine, which has no runner
            with Rig(sb, config) as rig:
                if config:
                    pass
                else:
                    rig.engine.disk_prune_runner = None
                (fact,) = rig.step()
                self.assertEqual(fact.summary, notice)
                self.assertEqual((rig.state / "disk-guard.json").read_text(), '{"episode": 1000000.0, "hard": false, "low": true}')
                rig.step(10, 38)
                rig.step(10, 60)
                self.assertEqual(rig.step(10, 60), [])
                self.assertEqual(rig.audit.lines, [("disk.low", {"free_gb": 40, "min_free_gb": 50, "hard_free_gb": 10, "hard": False}),
                                                   ("disk.recovered", {"free_gb": 60, "min_free_gb": 50, "episode": 1000000})])
                self.assertEqual((rig.state / "disk-guard.json").read_text(), '{"episode": null, "hard": false, "low": false}')
                self.assertEqual(rig.calls, [])

    def test_s7_three_mutants_each_fail_with_their_anchored_text_and_the_controls_pass_first(self):
        control = self.copy("control", [])
        for check in (check_command, check_hour, check_failures):
            check(control)   # the unmutated copy passes first
        mutants = (
            ("dash_a", check_command, A_COMMAND, [('"prune", "-f", "--filter", "until=6h")', '"prune", "-a", "-f", "--filter", "until=6h")')]),
            ("no_hour", check_hour, A_HOUR, [("or now - self.pruned < DISK_PRUNE_MIN_GAP", "or False")]),
            ("raises", check_failures, A_RAISE, [("except subprocess.TimeoutExpired:", "except KeyError:"),
                                                 ("except (OSError, subprocess.SubprocessError, ValueError) as err:\n            reason", "except KeyError as err:\n            reason"),
                                                 ("except Exception as err:   # the guard's pass is never", "except KeyError as err:   # the guard's pass is never")]),
        )
        for name, check, anchor, edits in mutants:
            with self.subTest(mutant=name):
                mutant = self.copy(name, edits)
                with self.assertRaises(AssertionError) as caught:
                    check(mutant)
                self.assertIn(anchor, str(caught.exception))
