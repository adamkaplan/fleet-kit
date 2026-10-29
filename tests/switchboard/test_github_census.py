"""The census of process kinds behind the GitHub calls the records do not count (#1501): scenarios S1b, S2-S5 and U1.

A fake process table holds a planted token value in an environment, a planted command line argument and a planted connection; the audit
line must name the three kinds with a count and hold none of the planted values. The two mutants at the end print the environment
values and the command lines: each must fail a test here."""
import json
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb

GH_IP = "140.82.112.5"
PLANTED_TOKEN = "ghp_PLANTEDTOKENVALUE0123456789abcdef"
PLANTED_ARG = "--planted-argument-7d41c9"
PLANTED_CMD = "planted-command-line-secret"
PLANTED = (PLANTED_TOKEN, PLANTED_ARG, PLANTED_CMD)


def fake_table():
    return [
        {"comm": "agentd", "cmdline": ["agentd", "x"], "environ": {"GH_TOKEN": PLANTED_TOKEN, "HOME": "/home/user"}, "peers": []},
        {"comm": "toolrun", "cmdline": ["toolrun", PLANTED_ARG, PLANTED_CMD], "environ": {"PATH": "/bin"}, "peers": [GH_IP]},
        {"comm": "poller", "cmdline": ["poller"], "environ": {}, "peers": ["10.0.0.9"]},   # neither: not in the census
        {"comm": "poller2", "cmdline": ["poller2", PLANTED_ARG], "environ": {"GITHUB_TOKEN": PLANTED_TOKEN}, "peers": [GH_IP]},
    ]


PS_TEXT = "\n".join([
    "/sbin/launchd", "/usr/libexec/logd", "/Applications/Some App.app/Contents/MacOS/Some App",
    "/Applications/Slack.app/Contents/MacOS/Slack Helper (Renderer)", "/usr/libexec/identityservicesd",
    "/Applications/" + "Deep/" * 40 + "tool-with-a-long-path", "/usr/bin/" + PLANTED_CMD, ""])
LSOF_TEXT = "\n".join([
    "COMMAND     PID     USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME",
    "identitys   612 someuser   34u  IPv6 0x3c15b55e50c02f91      0t0  TCP [fe80:17::aa:bb]:1024->[fe80:17::cc:dd]:1024 (ESTABLISHED)",
    "Some\\x20App  4321 someuser   18u  IPv4 0xab12cd      0t0  TCP 192.168.1.5:51234->140.82.112.6:443 (ESTABLISHED)",
    "Some\\x20App  4321 someuser   19u  IPv4 0xab12ce      0t0  TCP 192.168.1.5:51235->140.82.112.6:443 (ESTABLISHED)",
    "Slack\\x20   777 someuser   20u  IPv4 0xab12cf      0t0  TCP 192.168.1.5:51236->192.30.253.9:443 (ESTABLISHED)",
    "Safari      888 someuser   21u  IPv4 0xab12d0      0t0  TCP 192.168.1.5:51237->17.253.1.1:443 (ESTABLISHED)",
    "Safari      889 someuser   22u  IPv6 0xab12d1      0t0  TCP [fe80::1]:51238->[2606:50c0:8000::154]:443 (ESTABLISHED)",
    "toolrun     890 someuser   23u  IPv4 0xab12d2      0t0  TCP 192.168.1.5:51239->140.82.112.6:443 (ESTABLISHED) " + PLANTED_ARG, ""])


class Rig(unittest.TestCase):
    def setUp(self):
        # process-wide caches keyed by path (state_write's last-written memory, the audit fsync clock) must not carry one test into the next
        v2 = sys.modules.get("switchboard.v2_transport")
        if v2 is not None:
            v2._STATE_LAST.clear()
        sb.AuditLog._synced.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.audit_path = self.state / "audit.jsonl"
        self.gc = sb.load_module("github_census")
        self._real_read_table = self.gc.read_table
        self.clock = [1_011_610.0]
        self.reset = 1_015_200
        self.used = {"acme-a": 0, "acme-b": 0}
        self.reads = []
        self.engine = types.SimpleNamespace(state_dir=self.state, now=lambda: self.clock[0], _note=lambda *a, **k: None, actor="switchboard",
                                            audit=sb.AuditLog(self.audit_path),
                                            reads=types.SimpleNamespace(sessions={
                                                "s1": ({"model": {"id": "m", "providerID": "github-copilot"}}, 0, "idle"),
                                                "s2": ({"model": {"id": "m", "providerID": "github-copilot"}}, 0, "idle"),
                                                "s3": ({"model": {"id": "m", "providerID": "other-provider"}}, 0, "idle")}))
        self.table = fake_table()
        self.table_calls = []

        def runner(argv, **kw):
            self.reads.append(argv)
            if argv[1:3] == ["auth", "token"]:
                return subprocess.CompletedProcess(argv, 0, "tok\n", "")
            account = "acme-a" if kw.get("env", {}).get("GH_TOKEN") == "tok" else "acme-b"
            body = {"resources": {"core": {"limit": 5000, "used": self.used[account], "remaining": 5000 - self.used[account], "reset": self.reset}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        self.accounts = sb.GhAccounts({"gh": "/fake/bin/gh", "call_timeout_seconds": 5, "gh_users": {"acme/*": "acme-a"}}, runner, lambda: self.clock[0], environ={})
        self.engine.accounts = self.accounts
        self.runner = runner
        for name, patch in (("read_table", lambda: self.table_calls.append(1) or self.table), ("github_addresses", lambda: {GH_IP})):
            p = mock.patch.object(self.gc, name, patch)
            p.start()
            self.addCleanup(p.stop)

    def read_users(self):
        self.accounts.read_usage = lambda: {a: (u, 5000 - u, 5000, self.reset) for a, u in self.used.items()}

    def lines(self, event=None):
        if not self.audit_path.exists():
            return []
        rows = [json.loads(l) for l in self.audit_path.read_text().splitlines()]
        return [r for r in rows if event is None or r["event"] == event]

    def step(self, seconds=60, spend=None):
        self.clock[0] += seconds
        for account, n in (spend or {}).items():
            self.used[account] += n
        self.engine.usage_read_at = None
        sb.record_github_calls(self.engine)

    def whole_state_text(self):
        return "\n".join(p.read_text(errors="replace") for p in self.state.rglob("*") if p.is_file())


class TestSamples(Rig):
    def test_s1b_two_accounts_over_twelve_minutes_give_two_sample_lines_each_with_three_fields_and_no_secret(self):
        self.read_users()
        for _ in range(12):
            self.step(60)
        samples = self.lines("github.sample")
        for account in ("acme-a", "acme-b"):
            mine = [s for s in samples if s["subject"] == account]
            self.assertEqual(len(mine), 2, samples)
            for row in mine:
                self.assertEqual(set(row["detail"]), {"used", "recorded", "sessions"})
                self.assertEqual(row["detail"]["sessions"], {"github-copilot": 2, "other-provider": 1})
        text = self.audit_path.read_text()
        for planted in PLANTED + ("cmdline",):
            self.assertNotIn(planted, text)
        self.assertEqual(self.table_calls, [], "no census below the threshold")


class TestCensus(Rig):
    def burst(self, n=150):
        self.read_users()
        self.step(60)
        self.step(60, {"acme-a": n})

    def test_s2_an_outside_count_over_the_threshold_writes_one_line_naming_the_kinds_with_counts(self):
        self.burst(150)
        found = self.lines("github.census")
        self.assertEqual(len(found), 1)
        detail = found[0]["detail"]
        self.assertEqual(found[0]["subject"], "acme-a")
        self.assertEqual(detail["outside"], 150)
        kinds = {k["kind"]: k["count"] for k in detail["kinds"]}
        self.assertEqual(kinds, {"agentd": 1, "toolrun": 1, "poller2": 1})

    def test_s3_no_planted_value_is_in_the_line_or_in_any_file_the_pass_writes(self):
        self.burst(150)
        self.assertEqual(len(self.lines("github.census")), 1)
        text = self.whole_state_text()
        for planted in PLANTED + ("/home/user", GH_IP):
            self.assertNotIn(planted, text)

    def test_s4_fifty_outside_runs_no_census_and_a_hundred_fifty_over_three_passes_runs_it_once(self):
        self.burst(50)
        self.assertEqual((self.table_calls, self.lines("github.census")), ([], []))
        self.step(60, {"acme-a": 150})
        self.step(60, {"acme-a": 150})
        self.step(60, {"acme-a": 150})
        self.assertEqual(len(self.table_calls), 1)
        self.assertEqual(len(self.lines("github.census")), 1)
        self.step(200)
        self.step(60, {"acme-a": 150})
        self.assertEqual(len(self.table_calls), 2, "five minutes later it may run again")

    def test_recorded_calls_are_not_outside(self):
        self.read_users()
        self.step(60)
        sb.agent_calls.fold(str(self.state), self.clock[0], {"acme-a": 150})
        self.step(60, {"acme-a": 150})
        self.assertEqual(self.table_calls, [])

    def test_u1_a_census_that_cannot_list_processes_is_recorded_once_and_the_pass_finishes(self):
        self.read_users()
        calls = []
        with mock.patch.object(self.gc, "read_table", side_effect=PermissionError("no")), \
                mock.patch.object(self.accounts, "read", side_effect=lambda *a, **k: calls.append(a)):
            self.step(60)
            self.step(60, {"acme-a": 150})
            self.clock[0] += 300
            self.step(60, {"acme-a": 150})
        failed = self.lines("github.census_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["detail"], {"error": "PermissionError"})
        self.assertEqual(calls, [], "the census makes no GitHub call")
        self.assertEqual(self.lines("github.census"), [])
        self.assertTrue((self.state / "github-usage.json").exists(), "the pass finished")

    def test_the_real_table_reader_keeps_names_never_values_or_command_lines(self):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root)
            (proc / "net").mkdir()
            (proc / "net" / "tcp").write_text("  sl  local rem st\n   0: 0100007F:1F90 0100007F:0050 01 0:0 0:0 0 0 0 4242 1\n")
            one = proc / "77"
            (one / "fd").mkdir(parents=True)
            (one / "comm").write_text("toolrun\n")
            (one / "cmdline").write_bytes(PLANTED_ARG.encode() + b"\0")
            (one / "environ").write_bytes(b"GH_TOKEN=" + PLANTED_TOKEN.encode() + b"\0HOME=/home/user\0")
            (one / "fd" / "3").symlink_to("socket:[4242]")
            table = self._real_read_table(str(proc))
        self.assertEqual(table, [{"comm": "toolrun", "environ": {"GH_TOKEN": ""}, "peers": ["127.0.0.1"]}])
        self.assertNotIn(PLANTED_TOKEN + PLANTED_ARG, json.dumps(table))


class TestMacosReader(Rig):
    """S2b: the macOS reader on OUTPUT TEXT in the real shapes (the pod has no macOS: the real run is done on the Mac)."""

    def fake_run(self, ps=PS_TEXT, lsof=LSOF_TEXT, record=None):
        def run(argv, **kw):
            if record is not None:
                record.append((argv, kw))
            text = ps if argv[0].endswith("ps") else lsof
            return subprocess.CompletedProcess(argv, 0, text.encode(), b"")
        return run

    def test_s2b_the_macos_text_gives_the_kinds_with_a_count_and_none_of_the_planted_values(self):
        calls = []
        with mock.patch.object(self.gc.subprocess, "run", self.fake_run(record=calls)), \
                mock.patch.object(self.gc, "read_table", lambda: self._real_read_table("/nonexistent-proc-root")), \
                mock.patch.object(self.gc, "github_addresses", lambda: set()):
            self.read_users()
            self.step(60)
            self.step(60, {"acme-a": 150})
        found = self.lines("github.census")
        self.assertEqual(len(found), 1, self.lines())
        kinds = {k["kind"]: (k["count"], k["via"]) for k in found[0]["detail"]["kinds"]}
        self.assertEqual(sorted(kinds), ["Slack_Helper__Renderer_", "Some_App", "toolrun"])
        self.assertTrue(all(v == (1, ["connection"]) for v in kinds.values()), kinds)
        text = self.audit_path.read_text() + self.whole_state_text()
        for planted in PLANTED + ("140.82.112.6", "4321", "someuser", "192.168.1.5"):
            self.assertNotIn(planted, text)
        for argv, kw in calls:
            self.assertIn(argv, (self.gc.PS_ARGV, self.gc.LSOF_ARGV), "no flag that prints environments")
            self.assertIn("timeout", kw)

    def test_the_parsers_on_the_real_shapes(self):
        self.assertEqual(self.gc.parse_ps(PS_TEXT.splitlines())[:3], ["launchd", "logd", "Some_App"])
        found = self.gc.parse_lsof(LSOF_TEXT.splitlines())
        self.assertEqual(found["4321"], ("Some App", ["140.82.112.6", "140.82.112.6"], True))
        self.assertNotIn("612", found, "IPv6 peers are never counted")
        self.assertEqual(found["888"][1], [], "a non GitHub peer is not counted")

    def test_a_hang_or_a_missing_command_is_one_failed_line_and_the_pass_goes_on(self):
        def hang(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        with mock.patch.object(self.gc.subprocess, "run", hang), \
                mock.patch.object(self.gc, "read_table", lambda: self._real_read_table("/nonexistent-proc-root")):
            self.read_users()
            self.step(60)
            self.step(60, {"acme-a": 150})
            self.step(60, {"acme-a": 150})
        self.assertEqual([l["detail"] for l in self.lines("github.census_failed")], [{"error": "TimeoutExpired"}])

    def test_the_rows_read_and_the_processes_scanned_are_capped(self):
        many = "\n".join("/usr/bin/p%d" % i for i in range(self.gc.MAX_ROWS + 50))
        with mock.patch.object(self.gc.subprocess, "run", self.fake_run(ps=many)):
            self.assertEqual(len(self.gc._run_text(self.gc.PS_ARGV)), self.gc.MAX_ROWS)
        self.assertEqual(len(self.gc.parse_ps(many.splitlines())), self.gc.MAX_PROCESSES)
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root)
            (proc / "net").mkdir()
            for n in range(1, 8):
                (proc / str(n)).mkdir()
                (proc / str(n) / "comm").write_text("x\n")
            with mock.patch.object(self.gc, "MAX_PROCESSES", 3):
                self.assertEqual(len(self._real_read_table(str(proc))), 3)


class TestDocumentation(unittest.TestCase):
    def test_s6_the_documentation_says_the_line_never_holds_a_value(self):
        text = (Path(__file__).resolve().parents[2] / "docs" / "switchboard.md").read_text()
        section = text[text.index("**Census of process kinds (#1501).**"):]
        section = section[:section.index("\n")]
        self.assertEqual(len(re.findall(r"never holds a value, a command line, an argument, an address or a token", section)), 1)


class TestMutants(Rig):
    """S5: a census that prints environment values, and one that prints command lines, each fails a test above."""

    def mutant_audit(self, leak):
        def census(table, addresses):
            found = self.real_census(table, addresses)
            for row, proc in zip(found["kinds"], [p for p in table]):
                row["leak"] = leak(proc)
            return found
        return census

    def setUp(self):
        super().setUp()
        self.real_census = self.gc.census

    def run_s3_with(self, leak):
        with mock.patch.object(self.gc, "census", self.mutant_audit(leak)):
            self.read_users()
            self.step(60)
            self.step(60, {"acme-a": 150})
        text = self.audit_path.read_text() + self.whole_state_text()
        return [p for p in PLANTED if p in text]

    def test_s5_the_mutant_that_prints_environment_values_is_caught(self):
        self.assertTrue(self.run_s3_with(lambda p: json.dumps(p.get("environ"))), "the planted-value check must see an environment value")

    def test_s5_the_mutant_that_prints_command_lines_is_caught(self):
        self.assertTrue(self.run_s3_with(lambda p: " ".join(p.get("cmdline") or [])), "the planted-value check must see a command line")

    def test_the_real_census_leaks_nothing(self):
        self.assertEqual(self.run_s3_with(lambda p: "x"), [])


if __name__ == "__main__":
    unittest.main()
