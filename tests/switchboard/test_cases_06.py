"""Test classes moved out of bin/test-switchboard (text unchanged): cases_06."""
import argparse
import ast
import traceback
import base64
import calendar
import collections
import contextlib
import errno
import copy
import datetime as _dt
import fcntl
import gzip
import fnmatch
import hashlib
import hmac
import importlib.machinery
import importlib.util
import inspect
import http.client
import io
import itertools
import select
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote
import shlex
import shutil
import textwrap
import zipfile
import signal
import socket
import stat
import tarfile
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
import unittest
import uuid
import urllib.parse
from unittest import mock
from support.core import Base, FakeRunner
from support.fake_hubs import FakeTriageHub
from support.fake_jev import SHIM
from support.installs_and_keys import ACME_API, ACME_WEB, CloudReaperInstall, DECISIONS_PLUGIN, GOOD_DONE_WHEN, InstallCase, RealStatusAfterInstall, WrittenFilesAreGuarded
from support.loader import SWITCHBOARD, class_source, sb, sc, switchboard_source
from support.paths import BIN, REPO
from support.policy_and_import import FAKE_V1, FLEET_HOOKS, FORBIDDEN_IN_PUBLIC_TEXT, POLICY_REPLAY, V1_CHILD, V1_CHILD_ID, V1_MAIN, V1_MAIN_ID, convert, policy_raw_config, v1_export
from support.scenario_roles import AGENT_FILES, JEV_REPLAY, WORKER_ROLES, _stdlib_roots, role_of
from support.wire_fakes import FAKE_OPENCODE, SES, herdr_typing_violations


class TestGithubVerification(Base):
    SECRET = "s" * 64

    def test_the_signature_is_the_hmac_of_the_raw_body(self):
        body = b'{"a": 1}'
        header = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(sb.sign_body(self.SECRET, body), header)
        self.assertTrue(sb.signature_matches(self.SECRET, body, header))

    def test_anything_else_is_refused(self):
        body = b"{}"
        good = sb.sign_body(self.SECRET, body)
        for what, header in (("none", None), ("empty", ""), ("no prefix", good[len("sha256="):]),
                             ("other body", sb.sign_body(self.SECRET, b"{ }")),
                             ("other secret", sb.sign_body("x" * 64, body)), ("truncated", good[:-2]),
                             ("sha1", "sha1=" + good[len("sha256="):]), ("non-ascii", "sha256=\u00e9" * 3)):
            with self.subTest(what=what):
                self.assertFalse(sb.signature_matches(self.SECRET, body, header))
        self.assertFalse(sb.signature_matches("", body, sb.sign_body("", body)), "no secret, no match")

    def test_the_comparison_is_constant_time(self):
        self.assertIn("hmac.compare_digest", inspect.getsource(sb.signature_matches))
        self.assertNotIn("==", inspect.getsource(sb.signature_matches))


class ClosingRunner:
    """herdr's runner that closes a pane the moment the engine reads it with
    `agent get`: the pane closes between the decision and the delivery."""

    def __init__(self, herdr, pane):
        self.herdr, self.pane, self.fired = herdr, pane, False

    def __call__(self, argv, **kwargs):
        if list(argv[1:3]) == ["agent", "get"] and argv[3:4] == [self.pane] and not self.fired:
            self.fired = True
            del self.herdr.panes[self.pane]
        return self.herdr(argv, **kwargs)


class TestV2ClientPR4(Base):
    def test_argv_of_the_two_new_reads(self):
        for call, op in ((lambda c: c.permission_list(SES), "session.permission.list"),
                         (lambda c: c.form_list(SES), "session.form.list")):
            runner = FakeRunner()
            call(sb.V2Client("/opt/lab/bin/opencode", {}, runner, 7))
            self.assertEqual(runner.argv, ["/opt/lab/bin/opencode", "api", op, "--param", "sessionID=" + SES])
        self.assertEqual(class_source("V2Client").count("self.runner("), 1)


class TestParseCheckpoint(unittest.TestCase):
    def test_several_phrasings(self):
        for line, want in (
                ("CHECKPOINT: Report at 30 minutes (kind working, one line); overdue at 60.", (1800, 3600)),
                ("CHECKPOINT: report in 20m, overdue 45m", (1200, 2700)),
                ("checkpoint: at 15 min, overdue at 30 min", (900, 1800)),
                ("CHECKPOINT: Report at 1 hour; overdue at 90 minutes", (3600, 5400)),
                ("CHECKPOINT: Report at 2h ... overdue at 3", (7200, 10800)),
                ("CHECKPOINT: report after 45 seconds, overdue after 2m", (45, 120))):
            with self.subTest(line=line):
                self.assertEqual(sb.parse_checkpoint("ISSUE: x\n" + line + "\nSTOP: y\n"), want)

    def test_an_unparseable_line_is_none_never_an_error(self):
        for brief in ("CHECKPOINT: <a UTC time or an agreed event, with an overdue time>",
                      "CHECKPOINT: when the PR is up", "CHECKPOINT: Report at 30 minutes", "no such line",
                      "CHECKPOINT: Report at 60 minutes; overdue at 30", "CHECKPOINT: at 30; overdue at 60", "", None):
            with self.subTest(brief=brief):
                self.assertIsNone(sb.parse_checkpoint(brief))


class TestIntentParsing(unittest.TestCase):
    def test_the_first_line_is_the_intent_and_done_when_is_its_own_line(self):
        body = "Intro\n\n## Intent\nFix the agents.\nDone when: both answer.\n\n## Notes\nx\n"
        self.assertEqual(sb.parse_intent(body), ("Fix the agents.", "both answer."))

    def test_missing_parts_are_none(self):
        self.assertEqual(sb.parse_intent("no section at all"), (None, None))
        self.assertEqual(sb.parse_intent("## Intent\nJust this\n"), ("Just this", None))
        self.assertEqual(sb.parse_intent("## Intent\nDone when: later\n"), (None, "later"))
        self.assertEqual(sb.parse_intent("## Intent\n\n## Next\nDone when: not mine\n"), (None, None))
        self.assertEqual(sb.parse_intent(None), (None, None))

    def test_tolerates_crlf_heading_levels_case_and_bullets(self):
        body = "### intent  \r\n- Make it work\r\n- **Done when:** it works\r\n"
        self.assertEqual(sb.parse_intent(body), ("- Make it work", "it works"))

    def test_the_section_ends_at_the_next_heading_and_fences_are_ignored(self):
        body = "```\n## Intent\nnot this\n```\n## Intent\nthis\n```\n## Other\n```\nDone when: x\n## After\nno\n"
        self.assertEqual(sb.parse_intent(body), ("this", "x"))


class TestIntegrationBranchVerdictText(unittest.TestCase):
    """#776: what plan a PR into an integration branch must link, said once in the docs and once in the coordination skill."""
    DOC = ("The ledger never uses the pull request's base branch to choose or relax a plan: a pull request, including one into an integration branch, is gated by the plan its body links with a `PLAN:` line (or `--plan`), "
           "and `verdict --all` refuses with \"no per-PR scenario of this plan is judged by reviewer\" (naming the evaluator you passed) when the linked plan has no per-PR scenario naming that evaluator.")
    SKILL = ("For a PR into an integration branch, link the epic's plan or a plan of its own with at least one scenario judged by `reviewer` (a scenario with no `claim` is per pull request), "
             "or put the line `per-pr scenarios: none because <reason of at least 20 characters>` in the plan (then G1 reads n/a and there is no reviewer verdict to record).")

    DRAFT = ("Some repositories' workflows skip draft pull requests (a coder lost four hours reading that as a failing CI): when a draft shows no CI runs, "
             "look at the workflow's draft filter and mark the PR ready before waiting for CI (if there is no filter, the cause is elsewhere).")

    def test_the_draft_sentence_is_present_once(self):
        self.assertEqual((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().count(self.DRAFT), 1)

    def test_the_doc_passage_is_present_once(self):
        self.assertEqual((REPO / "docs" / "switchboard.md").read_text().count(self.DOC), 1)

    def test_the_skill_sentence_is_present_once(self):
        self.assertEqual((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().count(self.SKILL), 1)

    def test_the_refusal_wording_is_the_one_the_command_prints(self):
        self.assertIn("no per-PR scenario of this plan is judged by %s", switchboard_source())

    def test_the_texts_name_roles_never_agents_or_people(self):
        for text in (self.DOC, self.SKILL):
            self.assertNotRegex(text, r"(?i)\b(coder\d|cos\b|fleet-upkeep|adam\b)")


class TestLaunchMemoryTest(unittest.TestCase):
    """#758: the launch memory test is said once in the coordination skill; no swap-used percentage rule anywhere."""
    SENTENCE = "Launch a new worker or tester only when `memory_pressure` reports a free percentage of at least 40 and the `Swapouts` counter of `vm_stat` is unchanged between two reads 30 seconds apart (swap used is sticky on macOS and is not the test); launch applies it itself; `--memory-ok <reason>` overrides."

    def test_the_sentence_is_in_the_coordination_skill_exactly_once(self):
        self.assertEqual((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().count(self.SENTENCE), 1)

    def test_no_skill_or_definition_states_a_swap_used_percentage_rule(self):
        files = list(REPO.glob("skills/*/SKILL.md")) + list(REPO.glob("agents/opencode/*.md")) + list(REPO.glob("agents/copilot/*.agent.md"))
        self.assertTrue(files)
        for f in files:
            self.assertNotRegex(f.read_text(), r"(?i)swap[^.\n]{0,40}\d+\s*(%|percent)", f.name)

    def test_the_sentence_names_roles_never_agents_or_people(self):
        skill = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        sentence = skill[skill.index("Launch a new worker or tester only when"):].split("\n", 1)[0]
        self.assertNotRegex(sentence, r"(?i)\b(coder\d|cos\b|fleet-upkeep|adam\b)")


class TestPR7Source(unittest.TestCase):
    def test_the_engine_still_never_types(self):
        self.assertEqual(herdr_typing_violations(switchboard_source()), [])

    def test_the_decision_model_section_is_one_clearly_marked_block(self):
        source = switchboard_source()
        self.assertEqual(source.count("\n# PR 7: the decision model"), 1)

    def test_the_replay_answers_name_no_private_text(self):
        text = JEV_REPLAY.read_text().lower()
        for fragment in ("sk-", "bearer", "http"):
            self.assertNotIn(fragment, text)


class TestShimHygiene(unittest.TestCase):
    def test_it_is_executable_portable_stdlib_only_and_impersonal(self):
        text = SHIM.read_text()
        self.assertTrue(os.access(str(SHIM), os.X_OK))
        self.assertEqual(text.splitlines()[0], "#!/usr/bin/env python3")
        roots = _stdlib_roots()
        for node in ast.walk(ast.parse(text)):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            for name in names:
                origin = importlib.util.find_spec(name).origin or ""
                self.assertTrue(origin in ("built-in", "frozen") or os.path.realpath(origin).startswith(roots), name)
        self.assertIn("\nimport sys\n", text)
        pattern = re.compile("|".join(["jan" + "us", "sp" + "ire", "adk" + "aplan", "/Us" + "ers/", "direc" + "tor(?!y|ies)"]),
                             re.IGNORECASE)
        for number, line in enumerate(text.splitlines(), 1):
            self.assertIsNone(pattern.search(line), "%d: %s" % (number, line))


class PolicyCountingJev:
    """Wraps a decision model and counts how often it was asked."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def decide(self, state, questions):
        self.calls += 1
        return self.inner.decide(state, questions)


class PolicyRig:
    """What a scenario's tool-call steps share: the judge, its Jev, and what each step answered."""

    def __init__(self, run):
        world = run.world
        self.config = policy_raw_config()
        self.config["cos_orders_file"] = run.cos_orders_path  # PR 18: the file this run's Chief of Staff has
        self.jev = PolicyCountingJev(sb.build_jev(self.config))
        self.judge = sb.PolicyJudge(self.config, world.engine.v2, getattr(world, "intents", None), self.jev,
                                    sb.AuditLog(world.audit_path), world.state, world.clock.now)
        self.results = {}


class TestPolicyRedaction(unittest.TestCase):
    def test_credential_shapes_are_removed(self):
        for secret, text in (
                ("sk-or-test-0123456789abcdef", "curl -H 'Authorization: Bearer sk-or-test-0123456789abcdef' x"),
                ("ghp_0123456789abcdefABCDEF", "git push https://ghp_0123456789abcdefABCDEF@host/r"),
                ("hunter2hunter2", "deploy --password hunter2hunter2"),
                ("s3cr3tvalue99", "API_TOKEN=s3cr3tvalue99 ./run"),
                ("AKIA0123456789ABCDEF", "aws --key AKIA0123456789ABCDEF s3 ls"),
                ("realpass", "git clone https://user:realpass@example.test/r.git")):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, sb.redact_command_text(text))

    def test_a_known_key_is_removed_wherever_it_appears_and_ordinary_text_survives(self):
        self.assertNotIn("KEYVALUE", sb.redact_command_text("echo KEYVALUE", ["KEYVALUE"]))
        self.assertEqual(sb.redact_command_text("git log --oneline -5"), "git log --oneline -5")
        self.assertEqual(sb.redact_command_text(None), "")


class TestStandingAuthority(unittest.TestCase):
    def test_the_line_in_the_charter_prose(self):
        body = "```yaml\norchestrator: platform\n```\n\nStanding authority: may merge green PRs\n\nMore.\n"
        self.assertEqual(sb.parse_standing_authority(body), "may merge green PRs")

    def test_forms_that_count(self):
        for line in ("standing authority: may merge", "- Standing authority: may merge", "**Standing authority:** may merge",
                     "  Standing Authority :   may merge  "):
            with self.subTest(line=line):
                self.assertEqual(sb.parse_standing_authority("text\n" + line + "\n"), "may merge")

    def test_absent_empty_none_or_fenced_is_none_stated(self):
        for body in (None, "", "no such line", "Standing authority:", "Standing authority: none",
                     "Standing authority: None stated.", "```\nStanding authority: may merge\n```\n",
                     "~~~\nStanding authority: may merge\n~~~\n", "A Standing authority: may merge (not at line start)"):
            with self.subTest(body=body):
                self.assertIsNone(sb.parse_standing_authority(body))

    def test_the_first_line_wins(self):
        self.assertEqual(sb.parse_standing_authority("Standing authority: one\nStanding authority: two\n"), "one")

    def test_the_skill_documents_it_in_one_short_paragraph(self):
        text = (REPO / "skills" / "fleet-charter" / "SKILL.md").read_text()
        self.assertEqual(text.count("Standing authority:"), 2)  # the syntax line and the example line
        self.assertIn("none stated", text)


class TestFleetHooksSource(unittest.TestCase):
    def test_the_plugin_holds_no_policy_and_reaches_nothing_else(self):
        code = "\n".join(re.sub(r"//.*$", "", line) for line in (FLEET_HOOKS / "index.js").read_text().splitlines())
        for needle in ("jev", "openrouter", "herdr", "fetch(", "exec(", "execSync", "process.env.PATH", "http"):
            self.assertNotIn(needle, code.lower(), needle)
        self.assertEqual(re.findall(r'\["judge-tool"\]', code), ['["judge-tool"]'])

    def test_it_is_not_installed_or_linked_anywhere(self):
        for path in ("install.sh", "bin/fleet-doctor"):
            file = REPO / path
            if file.exists():
                self.assertNotIn("fleet-hooks", file.read_text(), path)

    def test_the_new_files_carry_no_personalisation(self):
        pattern = re.compile("|".join(["jan" + "us", "sp" + "ire", "adk" + "aplan", "/Us" + "ers/", "direc" + "tor(?!y|ies)"]),
                             re.IGNORECASE)
        files = list(FLEET_HOOKS.iterdir()) + [POLICY_REPLAY] + sorted(sc.DEFAULT_DIR.glob("p[1-6]-*.json"))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(pattern.search(line), "%s:%d %s" % (path.name, number, line))


class FakeV1:
    """The OpenCode v1 binary as the import sees it: it answers `export <id>` from
    a table of exports and refuses to be asked anything else, so a test fails if
    the command ever runs v1 for another purpose."""

    def __init__(self, exports=None):
        self.exports = {i: json.dumps(e) for i, e in (exports or {}).items()}
        self.calls = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if len(argv) != 3 or argv[:2] != [FAKE_V1, "export"]:
            raise AssertionError("v1 may only be asked `export <id>`; got %r" % argv)
        if argv[2] not in self.exports:
            return subprocess.CompletedProcess(argv, 1, "", "Session not found: %s\n" % argv[2])
        return subprocess.CompletedProcess(argv, 0, self.exports[argv[2]], "")


class TestImportOrder(unittest.TestCase):
    def test_a_parent_comes_before_its_child_whatever_the_input_order(self):
        parent, child = v1_export(), v1_export(V1_CHILD)
        for given in ([child, parent], [parent, child]):
            self.assertEqual([e["info"]["id"] for e in sb.import_order(given)], [V1_MAIN_ID, V1_CHILD_ID])

    def test_a_parent_outside_the_batch_is_assumed_to_exist(self):
        self.assertEqual([e["info"]["id"] for e in sb.import_order([v1_export(V1_CHILD)])], [V1_CHILD_ID])

    def test_a_cycle_is_refused(self):
        a, b = v1_export(), v1_export(V1_CHILD)
        a["info"]["parentID"] = V1_CHILD_ID
        with self.assertRaises(sb.ImportV1Error):
            sb.import_order([a, b])

    def test_the_child_points_at_the_parents_new_id(self):
        child = convert(v1_export(V1_CHILD), fleet=None)["info"]
        self.assertEqual(child["parentID"], convert()["info"]["id"])


class TestImportV1Config(Base):
    def test_the_defaults(self):
        self.assertEqual(sb.V1_MOVE_DEFAULTS, {"v1_opencode": None, "import_body_limit_bytes": 120000})
        self.assertIsNone(sb.load_config()["v1_opencode"])

    def test_v1_must_be_an_absolute_path_and_not_the_v2_binary(self):
        for raw, word in (({"v1_opencode": "opencode"}, "absolute"), ({"v1_opencode": 3}, "absolute"),
                          ({"v1_opencode": "/a/b", "opencode": "/a/b"}, "same binary"),
                          ({"import_body_limit_bytes": 10}, "at least"), ({"import_body_limit_bytes": True}, "at least"),
                          ({"import_body_limit_bytes": "9"}, "at least")):
            with self.subTest(raw=raw):
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.validate_config(raw)
                self.assertIn(word, str(caught.exception))
        self.assertEqual(sb.validate_config({"v1_opencode": FAKE_V1, "opencode": FAKE_OPENCODE})["v1_opencode"], FAKE_V1)


class TestImportV1AsAProcess(Base):
    def test_a_dry_run_through_the_real_entry_point_and_a_script_standing_in_for_v2(self):
        v2 = self.fake_bin("opencode-v2", 'echo \'{"data": [], "cursor": {"previous": null, "next": null}}\'\n')
        self.write_config({"opencode": str(v2)})
        proc = self.run_cli(SWITCHBOARD, "import-v1", "--file", str(V1_MAIN), "--name", "chief", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would import: v1 %s" % V1_MAIN_ID, proc.stdout)
        self.assertFalse(self.state_dir.exists(), "a dry run does not even make the state directory")

    def test_the_command_is_listed_in_help(self):
        self.assertIn("import-v1", self.run_cli(SWITCHBOARD, "--help").stdout)


class MutantSpliceError(Exception):
    """A mutant whose target is not in the function it names: a broken test, never a caught mutant."""


class FakeOwnerlessHub(FakeTriageHub):
    """The triage hub for #605: the open list is paged by `cap` pages of 100 (cut when more remain) and the repository answers its own count of open items."""

    def __init__(self):
        super().__init__()
        self.open_total = {}

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        if "state=open" in path and "/issues?" in path and cap is not None:
            self.calls.append(path)
            repo = re.search(r"repos/([^/]+/[^/]+)/", path).group(1)
            if repo in self.unreadable:
                raise sb.GitHubUnavailable("HTTP 404: Not Found")
            everything = list(self.open.get(repo, {}).values())
            if "direction=asc" in path:   # GitHub sorts by the updated time; the fake does too
                everything.sort(key=lambda record: record["updated_at"])
            elif "sort=" not in path:
                everything.sort(key=lambda record: record["updated_at"], reverse=True)   # the default listing is newest first
            return everything[:cap * 100], len(everything) > cap * 100
        return super().read_pages(path, key, timeout, older, cap)

    def open_issues_count(self, repo):
        self.calls.append("repos/%s" % repo)
        return self.open_total.get(repo, len(self.open.get(repo, {})))


class TestReadWholeAuditTable(unittest.TestCase):
    """#805: the docs audit lists every absence-driven consumer with its input, whether it checked before, and its fix or how it is guarded."""
    NAMED = ("escalation-key prune", "triage seen-memory prune", "T5 and T6", "load alert", "stalled count", "sweep S2", "once-only memory",
             "closed-label", "merged-label", "reminders prune", "temp janitor", "worktree reclaim", "container reaper", "retire", "auto-retire",
             "delivered-message", "cursor prunes", "ownerless", "alert-issue wake", "T1 and T11", "chat-clear offers", "notice expiry")

    def rows(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        start = text.index("**Reads that are not whole: the audit of every absence-driven consumer (#805).**")
        block = text[start:text.index("**The stale digest (#349).**", start)]
        return [[c.strip() for c in line.strip().strip("|").split("|")] for line in block.splitlines() if line.startswith("| ") and not line.startswith("| ---") and not line.startswith("| Consumer")]

    def test_every_named_consumer_has_a_row_with_all_four_columns_filled(self):
        rows = self.rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row), 4, row)
            self.assertTrue(all(row), row)
        joined = " ".join(" ".join(r) for r in rows).lower()
        for name in self.NAMED:
            self.assertIn(name.lower(), joined, name)


class TestReportScenarios(Base):
    def test_the_pr13_catalog_is_present_with_a_control_each(self):
        found = {s["id"]: s for s in map(sc.load, sc.scenario_files(sc.DEFAULT_DIR))}
        for sid, fault_name in (("R4", "idle-wakes"), ("R5", "idle-wakes"), ("R6", "no-nudge-once"),
                                ("R7", "no-fail-open"), ("R8", "no-rider")):
            with self.subTest(scenario=sid):
                self.assertIn(sid, found)
                self.assertEqual(found[sid]["since_pr"], 13)
                self.assertEqual(found[sid]["control"].get("fault"), fault_name)

    def test_every_new_fault_is_known_and_off_outside_the_lab(self):
        for name in ("idle-wakes", "no-rider", "no-nudge-once", "no-fail-open", "ignore-reports", "no-description"):
            with self.subTest(fault=name):
                self.assertIn(name, sb.KNOWN_FAULTS)
                os.environ[sb.FAULT_ENV] = name
                os.environ.pop(sb.LAB_ENV, None)
                self.assertFalse(sb.fault(name))
                os.environ[sb.LAB_ENV] = "1"
                self.assertTrue(sb.fault(name))

    def test_the_report_step_is_in_the_vocabulary(self):
        self.assertEqual(set(sc.STEP_FIELDS["report"]), {"state", "text", "issue"})


class TestPlanOperationsPerBoss(unittest.TestCase):
    LINKS = {"a": {"kind": "report", "boss": "boss-a", "revision": 1, "digest": "x"},
             "b": {"kind": "report", "boss": "boss-b", "revision": 1, "digest": "x"},
             "old": {"kind": "report", "revision": 1, "digest": "x"},
             "q": {"kind": "question", "boss": None, "revision": 1, "digest": "x"}}

    def withdrawn(self, **kw):
        return sorted(op["key"] for op in sb.decision_core.plan_operations({}, self.LINKS, **kw) if op["op"] == "withdraw")

    def test_unreadable_boss_a_does_not_stop_withdrawal_for_boss_b(self):
        got = self.withdrawn(errored_bosses={"boss-a"})
        self.assertIn("b", got)
        self.assertIn("q", got)
        self.assertNotIn("a", got)

    def test_a_link_with_no_known_boss_keeps_the_cautious_freeze(self):
        self.assertNotIn("old", self.withdrawn(errored_bosses={"boss-a"}))

    def test_no_boss_error_withdraws_everything_answered(self):
        self.assertEqual(self.withdrawn(), ["a", "b", "old", "q"])

    def test_an_error_with_no_boss_still_freezes_every_report(self):
        got = self.withdrawn(errored_kinds={"report"})
        self.assertEqual(got, ["q"])


class TestPlanTemplateDoc(unittest.TestCase):
    TEMPLATE = REPO / "docs" / "plan-template.md"

    def test_the_good_example_in_the_template_passes_and_the_bad_ones_fail_for_the_labelled_reason(self):
        text = self.TEMPLATE.read_text()
        blocks = [b for _, b in sb.plan_check.fenced_blocks(text)]
        full = [b for b in blocks if b and b[0].strip() == "scenarios:"]
        self.assertEqual(len(full), 4, "the schema, the good example and the two defective ones, each with `scenarios:`")
        good = "\n".join(["```yaml"] + full[1] + ["```"])
        self.assertEqual(sb.plan_check.check(good, GOOD_DONE_WHEN)["result"], "pass")
        for block, code in zip(full[2:], ("vague-pass", "no-discriminator")):
            plan = "\n".join(["```yaml"] + block + ["```", "unchanged: none because this is only a documentation example"])
            codes = [d["code"] for d in sb.plan_check.check(plan, "(1) one thing")["defects"]]
            self.assertEqual(codes, [code], "the bad example fails for its labelled reason only")
        self.assertIn("DW-2 is not covered", text)
        for needle in ("evaluator consistency", "plan hash", "authority"):
            self.assertIn(needle, text)

    def test_the_template_is_referenced_and_carries_nothing_private(self):
        self.assertIn("plan-template.md", (REPO / "docs" / "switchboard.md").read_text())
        for number, line in enumerate(self.TEMPLATE.read_text().splitlines(), 1):
            self.assertIsNone(FORBIDDEN_IN_PUBLIC_TEXT.search(line), "%d: %s" % (number, line))
        for number, line in enumerate((BIN / "plan_check.py").read_text().splitlines(), 1):
            self.assertIsNone(FORBIDDEN_IN_PUBLIC_TEXT.search(line), "%d: %s" % (number, line))


class TestDecisionsRoleText(unittest.TestCase):
    def test_the_boss_roles_say_a_decision_once_with_its_id_and_point_to_the_list(self):
        for path in AGENT_FILES:
            if role_of(path) not in ("chief-of-staff", "orchestrator"):
                continue
            text = " ".join(path.read_text().split())
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertIn("read-only Decisions", text)
                self.assertIn('"see Decisions"', text)
                self.assertIn("with its id", text)
                self.assertIn("remove your `awaiting-cos` label" if role_of(path) == "orchestrator"  # PR 18: the tiers
                              else "decisions resolve", text)
                self.assertEqual("fleet-switchboard report question" in text, role_of(path) == "orchestrator")

    def test_the_workers_are_not_given_the_boss_text(self):
        for path in AGENT_FILES:
            if role_of(path) in WORKER_ROLES:
                self.assertNotIn("Decisions", path.read_text(), path.name)

    def test_the_skills_say_it_briefly(self):
        charter = " ".join((REPO / "skills" / "fleet-charter" / "SKILL.md").read_text().split())
        for phrase in ("read-only **Decisions** list", "fleet-switchboard decisions", '("see Decisions")', "remove the\nlabel".replace("\n", " ")):
            self.assertIn(phrase, charter)
        coordination = " ".join((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().split())
        for phrase in ("read-only Decisions list", "Say it once", "still waiting"):
            self.assertIn(phrase, coordination)


class TestD6Scenario(Base):
    def test_d6_is_present_with_a_control_that_names_what_it_breaks(self):
        found = {s["id"]: s for s in map(sc.load, sc.scenario_files(sc.DEFAULT_DIR))}
        self.assertIn("D6", found)
        d6 = found["D6"]
        self.assertEqual((d6["since_pr"], d6["control"]["fault"]), (15, "bare-issue-number"))
        self.assertIn("offline", d6["tiers"])
        self.assertEqual(sorted(d6["repos"]), [ACME_API, ACME_WEB])
        self.assertEqual([s["repo"] for s in d6["steps"] if s["do"] == "awaiting"], [ACME_API, ACME_WEB])

    def test_the_awaiting_step_may_name_a_repo(self):
        self.assertEqual(sorted(sc.STEP_FIELDS["awaiting"]), ["issue", "label", "on", "repo", "title"])  # PR 18: label


class TestInstallOnLinux(InstallCase):
    PLATFORM = "linux"

    def test_the_unit_is_written_and_enabled_and_removed_again(self):
        out, error = self.install("--opencode", self.machine.binary)
        self.assertIsNone(error, error)
        self.assertTrue(self.service_path().is_file())
        commands = self.machine.commands("systemctl")
        self.assertIn(["systemctl", "--user", "daemon-reload"], commands)
        self.assertIn(["systemctl", "--user", "enable", "--now", "fleet-kit-switchboard.service"], commands)
        second, _ = self.install("--opencode", self.machine.binary)
        self.assertEqual(self.statuses(second)["service"], ["already done"])
        self.install("--uninstall")
        self.assertFalse(self.service_path().exists())
        self.assertIn(["systemctl", "--user", "disable", "--now", "fleet-kit-switchboard.service"],
                      self.machine.commands("systemctl"))


class TestInstallOnAnUnknownPlatform(InstallCase):
    PLATFORM = "plan9"

    def test_the_service_is_skipped_with_the_reason(self):
        out, error = self.install("--opencode", self.machine.binary)
        self.assertIsNone(error, error)
        self.assertIn("skipped (no service manager", self.statuses(out)["service"][0] + out)


class TestCloudReaperInstallOnMacOS(CloudReaperInstall, WrittenFilesAreGuarded, InstallCase):
    PLATFORM = "darwin"

    def test_the_written_plist_is_loaded_at_login_restarts_and_lints(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        import plistlib
        body = plistlib.loads(self.reaper_path().read_bytes())
        self.assertIs(body["RunAtLoad"], True)
        self.assertIs(body["KeepAlive"], True)
        boots = [c for c in self.machine.calls if c[:2] == ["launchctl", "bootstrap"] and c[-1] == str(self.reaper_path())]
        self.assertEqual(len(boots), 1)
        if not shutil.which("plutil"):
            self.skipTest("plutil is macOS only")
        done = subprocess.run(["plutil", "-lint", str(self.reaper_path())], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())


class TestCloudReaperInstallOnLinux(CloudReaperInstall, WrittenFilesAreGuarded, InstallCase):
    PLATFORM = "linux"

    def test_the_written_unit_is_enabled_now_and_verifies(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIn(["systemctl", "--user", "enable", "--now", "fleet-kit-cloud-reaper.service"], self.machine.calls)
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze is not here")
        done = subprocess.run(["systemd-analyze", "verify", str(self.reaper_path())], capture_output=True)
        for word in (b"Unknown key", b"Invalid", b"Failed to parse", b"Unknown section"):
            self.assertNotIn(word, done.stderr)


class TestRealStatusAfterInstallOnMacOS(RealStatusAfterInstall, InstallCase):
    PLATFORM = "darwin"

    def test_the_service_it_just_wrote_is_installed_whatever_the_host_is(self):
        for host in ("darwin", "linux"):
            with self.subTest(host=host):
                self.assertIn("service    installed (%s)" % self.service_path(), self.installed_status("darwin", host))


class TestRealStatusAfterInstallOnLinux(RealStatusAfterInstall, InstallCase):
    PLATFORM = "linux"

    def test_the_service_it_just_wrote_is_installed_whatever_the_host_is(self):
        for host in ("linux", "darwin"):
            with self.subTest(host=host):
                self.assertIn("service    installed (%s)" % self.service_path(), self.installed_status("linux", host))


class OrdersReachTheJudgeWhole:
    """Every path an orders text takes to the judge's input: a charter's (resolve_policy_context), the Chief of Staff's
    file (context_of) and an owner's agent's inbound answer (judge_inbound_answer). Each is driven through its real code
    with 30 long orders; the last order must be in the judge's input, whole."""

    @staticmethod
    def long_orders(prefix, count=30, width=250):
        return ["%s %02d %s end-%s-%02d" % (prefix, n, "x" * width, prefix, n) for n in range(count)]

    class Spy:
        def __init__(self):
            self.seen = []

        def decide(self, state, questions):
            self.seen.append(state)
            raise sb.JevError("unreachable", "spy")


class AlreadyDecidedRefusal(Exception):
    """What the real adapter raises for a 409 "already decided": a permanent refusal that says it is settled outside."""

    permanent = True
    settled_outside = True


class TestOwnerEvaluatorsAreRare(unittest.TestCase):
    """The planning skill and the plan template say owner evaluators are rare and `claim: epic` (the cos rule: no routine PR waits on the owner)."""

    def test_the_skill_and_the_template_say_so(self):
        skill = (REPO / "skills" / "fleet-planning" / "SKILL.md").read_text()
        template = (REPO / "docs" / "plan-template.md").read_text()
        for text in (skill, template):
            self.assertIn("Owner evaluators are rare", text)
            self.assertIn("own eyes are the test", text)
            self.assertIn("claim: epic", text)
            self.assertIn("never for a routine PR" if text is skill else "never waits on the owner", text)
        self.assertIn("its own light plan", template)


class TestOwnReviewLaws(unittest.TestCase):
    """fleet-kit's own laws at its root: installed from the starter set, so `review laws` works on fleet-kit itself."""

    FILES = ("REVIEW_LAWS.md", "CODING_STANDARDS.md", "SECURITY_STANDARDS.md", "TEST_STANDARDS.md")
    ID = re.compile(r"\bR-(?:LAW|CODE|SEC|TEST)-[0-9]+\b")

    def test_the_four_files_exist_at_the_root_and_open_with_the_extends_line(self):
        version = (REPO / "docs" / "review-laws" / "VERSION").read_text().strip()
        for name in self.FILES:
            with self.subTest(name=name):
                self.assertTrue((REPO / name).is_file(), name)
                self.assertEqual((REPO / name).read_text().splitlines()[0], "extends: fleet-kit-review-laws @ %s" % version)

    def test_the_root_rules_are_byte_identical_to_the_starter_set_until_a_pr_says_otherwise(self):
        for name in self.FILES:
            with self.subTest(name=name):
                root = (REPO / name).read_text().split("\n## This repository")[0]
                starter = (REPO / "docs" / "review-laws" / name).read_text()
                self.assertEqual(root.strip(), starter.strip(), "a deliberate edit of a standard changes this test in the same PR")

    def test_the_root_copy_starts_as_the_starter_set_no_rule_id_added_or_dropped(self):
        for name in self.FILES:
            with self.subTest(name=name):
                root = set(self.ID.findall((REPO / name).read_text().split("\n## This repository")[0]))
                starter = set(self.ID.findall((REPO / "docs" / "review-laws" / name).read_text()))
                self.assertEqual((root - starter, starter - root), (set(), set()))


class TestCoderBooks(Base):
    """#549: a coder finished (retired, or its final report made) is on the books but not counted among a workspace's coders."""

    def obs(self, name, session="ses_1"):
        return sb.Observation(name, session, "w1:p1", "idle", {"name": name, "role": "coder", "reports_to": "orch"})

    def test_retired_and_finished_coders_are_not_counted(self):
        retired = {"gone": {"session": "ses_1", "at": 1}, "reused": {"session": "ses_old", "at": 1}, "anysession": {"at": 1}}
        last = {"done1": {"state": "done"}, "failed1": {"state": "failed"}, "busy1": {"state": "working"}, "quiet": {"send_to": {"orch": 5}}}
        finished = lambda name, session="ses_1": sb.coder_finished(self.obs(name, session), retired, last)
        self.assertTrue(finished("gone"))
        self.assertTrue(finished("anysession"))
        self.assertFalse(finished("reused"))      # the name is taken again by a new session
        self.assertTrue(finished("done1"))
        self.assertTrue(finished("failed1"))
        self.assertFalse(finished("busy1"))
        self.assertFalse(finished("quiet"))
        self.assertFalse(finished("nobody"))


class TestDecisionsStaleBound(unittest.TestCase):
    """Work item #449: the staleness bound of the decisions list is 180 s on both sides, and the docs say so."""

    def test_s1_the_daemon_and_plugin_bounds_are_180_seconds(self):
        self.assertEqual(sb.DECISIONS_STALE_SECONDS, 180)
        self.assertIn("export const STALE_MS = 180_000", (DECISIONS_PLUGIN / "decisions.mjs").read_text())
        self.assertIn("export const DECISIONS_STALE_MS = 180_000", (DECISIONS_PLUGIN / "health.mjs").read_text(), "the health view's copy of the bound")

    def test_s3_the_docs_no_longer_say_120_s_for_the_decisions_staleness(self):
        self.assertNotIn("older than 120 s", (DECISIONS_PLUGIN / "README.md").read_text())
        lines = (REPO / "docs" / "switchboard.md").read_text().splitlines()
        stale = [line for line in lines if "120 s" in line and ("stale" in line or "Staleness" in line or "not updating" in line or "older than" in line)]
        self.assertEqual(stale, [])
        self.assertGreaterEqual(len([line for line in lines if "180 s" in line and ("stale" in line or "Staleness" in line or "not updating" in line or "older than" in line)]), 4)


class StubHub:
    def __init__(self, entries):
        self.entries = entries

    def pr_event_snapshot(self):
        return {ref: dict(entry) for ref, entry in self.entries.items()}


class EjectionHub:
    def __init__(self, owner="orch"):
        self.owner, self.routed, self.notified = owner, [], {}

    def notice_names(self, repo, number):
        return self.notified.get((repo, number), [])

    def route_of(self, item, agents, now):
        self.routed.append(item.numbers)
        return (self.owner, "closing") if self.owner else (None, None)


class TestScratchFileRule(unittest.TestCase):
    """Ask #653: the coordination skill tells coders to keep scratch files in a private mktemp -d directory."""

    RULE = 'Write scratch files (PR bodies, message bodies, notes) under a private directory made with mktemp -d, never under a fixed name in the shared temp folder: another agent may write the same name, and a body file is published as it stands.'

    def test_s1_the_skill_carries_the_scratch_rule_exactly_once(self):
        text = (REPO / "skills/fleet-coordination/SKILL.md").read_text()
        self.assertEqual(text.count(self.RULE), 1)

    def test_s2_it_is_never_inside_a_code_fence(self):
        inside = False
        for line in (REPO / "skills/fleet-coordination/SKILL.md").read_text().splitlines():
            if line.strip().startswith("```"):
                inside = not inside
            elif inside:
                self.assertNotIn("mktemp -d", line)


class TestMergeGateDocs(unittest.TestCase):
    """Clause 9 and the neutral-names rule: the section says the gate, the lock, the waiver and that it is the fleet's own merge queue; neither it nor the
    new files hold a real pull request number or a private name."""

    def section(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        return text.split("## The merge gate and the merge lock", 1)[1].split("\n## ", 1)[0]

    def test_the_section_states_the_gate_the_lock_the_waiver_and_the_queue(self):
        section = self.section()
        self.assertIn("**It is the fleet's own merge queue.**", section)
        for statement in ("`fleet-switchboard merge-gate <repo#N>`** reads and writes nothing", "**The lock.**", "--gates MG"):
            self.assertEqual(section.count(statement), 1, statement)

    def test_the_section_states_pass_merge_and_why_a_held_pull_request_keeps_the_two_step_form(self):
        paragraphs = [p for p in self.section().split("\n\n") if "--pass --merge" in p and "held" in p.lower() and "two-step" in p]
        self.assertEqual(len(paragraphs), 1)

    def test_the_configuration_example_uses_neutral_names(self):
        section = self.section()
        self.assertIn("acme-org/widget-service", section)
        self.assertEqual(re.findall(r"#\d{3,}", section), ["#962", "#952"])
