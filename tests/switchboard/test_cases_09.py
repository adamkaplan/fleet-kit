"""Test classes moved out of bin/test-switchboard (text unchanged): cases_09."""
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
from support.core import Base
from support.loader import captured, sb, switchboard_source


class TestItemVerdicts(unittest.TestCase):
    """#770 (plan fleet-kit#777): verdicts for a work item that has no pull request. Temp dirs and fakes only. Each attack has a test and a mutant: the
    mutant is the weakened defence swapped in with mock.patch, and the same attack then SUCCEEDS (the test would fail on it)."""

    REPO, N, ITEM = "acme-org/widget-service", 777, "acme-org/widget-service#777"
    PIN, MOVED = "a" * 40, "b" * 40
    T0 = 1_000_000

    def setUp(self):
        self.iv = sb.load_module("item_verdicts")
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = Path(self.dir)
        self.clock = [self.T0]
        self.audit = []
        self.sessions = {"orch": "ses_o", "t1": "ses_t1", "t2": "ses_t2", "c1": "ses_c1", "n1": "ses_n1"}
        self.fleets = {"orch": {"role": "orchestrator", "repo": self.REPO, "charter": 587},
                       "t1": {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N},
                       "t2": {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": 778},
                       "c1": {"role": "coder", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N},
                       "n1": {"role": "coder", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": 700}}
        self.scenarios = [{"id": "S1", "evaluator": "tester", "proof": "live", "claim": "pr"},
                          {"id": "S2", "evaluator": "ci", "proof": "test", "claim": "pr"},
                          {"id": "S3", "evaluator": "tester", "proof": "measure", "claim": "epic"},
                          {"id": "S4", "evaluator": "tester", "proof": "measure"}]
        self.plan = {"result": "pass", "defects": [], "scenarios_parsed": self.scenarios, "plan_hash": "sha256:p", "text": "", "source": "plan"}
        self.pages = {}

    def now(self):
        return self.clock[0]

    def entries(self):
        return sb.plan_ledger.read_ledger(self.state)[0]

    def register(self, tester="t1", item=None, head=None, orch="orch", session=None):
        item = item or self.ITEM
        return sb.plan_ledger.append_entry(self.state, {"kind": "item_tester", "pr": item, "item": item, "tester": tester, "session": session or self.sessions.get(tester),
                                                         "orchestrator": orch, "head": head or self.PIN, "plan_ref": None, "by": orch}, self.now())

    def ctx(self, author="human", own=True, item=None):
        item = item or self.ITEM
        number = int(item.rsplit("#", 1)[1])

        class Audit:
            def append(inner, actor, event, subject=None, detail=None, **extra):
                self.audit.append((event, subject, detail))
        intents = types.SimpleNamespace(_api=lambda path, repo=None: self.pages[path] if path in self.pages else (_ for _ in ()).throw(sb.IntentError("not_found", path)))
        engine = types.SimpleNamespace(intents=intents, audit=Audit(), actor="t", herdr=None, v2=None)
        return types.SimpleNamespace(engine=engine, state=self.state, config={"plan_gates": {"repos": {self.REPO: {"item_checks": [{"name": "build", "app": "github-actions"}]}}}},
                                     repo=self.REPO, number=number, pr=item, item=item, entries=self.entries(), problem=None, facts={}, unreadable=None, source="plan",
                                     plan=self.plan, plan_problem=None, issue={"author": author, "body": "", "state": "open", "parent": None}, plan_own=own)

    def args(self, **kw):
        base = dict(item=self.ITEM, pr=None, plan=None, ask=None, head=None, scenario="S1", all_scenarios=False, evaluator="tester", result="pass", evidence="ran it", command=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def verdict(self, caller, retired=None, **kw):
        """Run the real cmd_item_verdict as `caller`; (exit, why) where why is the refusal sentence or None."""
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), retired or {})
        item = kw.get("item_key") or self.ITEM
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx(item=item)), mock.patch.object(sb, "identify_caller", lambda *a: caller), \
                mock.patch.object(sb, "item_agents", lambda c: (launch, self.sessions, self.fleets)):
            try:
                return sb.cmd_item_verdict(self.args(**{k: v for k, v in kw.items() if k != "item_key"}), None, self.now), None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def verdicts(self):
        return [e for e in self.entries() if e.get("kind") == "verdict"]

    # ---- S1 / DW-1: the registration and the verdict
    def test_s1_a_registered_tester_records_a_verdict_for_its_item(self):
        self.register()
        code, why = self.verdict("t1")
        self.assertEqual((code, why), (sb.EXIT_OK, None))
        got = self.verdicts()
        self.assertEqual([(v["pr"], v["by"], v["head"], v["evaluator"]) for v in got], [(self.ITEM, "t1", self.PIN, "tester")])

    def test_s1_launch_registers_the_tester_for_that_item_by_the_switchboard(self):
        self.pages.update({"repos/%s/issues/777" % self.REPO: {"user": {"login": "human"}, "body": "PLAN: https://github.com/acme-org/widget-service/issues/777#issuecomment-1",
                                                               "state": "open", "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/770"},
                           "repos/%s/issues/770" % self.REPO: {"parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/587"},
                           "repos/%s" % self.REPO: {"default_branch": "main"}, "repos/%s/commits/main" % self.REPO: {"sha": self.PIN}})
        agents = {n: types.SimpleNamespace(fleet=f) for n, f in self.fleets.items()}
        ctx = self.ctx()
        ctx.engine.discover = lambda now: types.SimpleNamespace(agents=agents)
        ctx.engine.now = lambda: self.now()
        ctx.engine.config = {}
        args = types.SimpleNamespace(role="tester", issue=777, name="t1", reports_to="orch")
        sb.register_item_tester(ctx.engine, self.state, args, self.REPO, "orch", {"session": "ses_t1"}, self.now)
        reg = self.iv.registrations(self.entries(), self.ITEM)
        self.assertEqual([(e["tester"], e["session"], e["orchestrator"], e["head"]) for e in reg], [("t1", "ses_t1", "orch", self.PIN)])
        self.assertEqual([a[0] for a in self.audit], ["plan.item.register"])
        # a launch run by a coder (not the orchestrator) registers nothing, and says why
        sb.register_item_tester(ctx.engine, self.state, args, self.REPO, "c1", {"session": "ses_t1"}, self.now)
        self.assertEqual(len(self.iv.registrations(self.entries(), self.ITEM)), 1)
        self.assertEqual(self.audit[-1][0], "plan.item.refused")

    def test_s2_a_tester_registered_for_item_a_cannot_write_for_item_b(self):
        self.register("t1")
        other = "acme-org/widget-service#778"
        code, why = self.verdict("t1", item_key=other)
        self.assertIsNone(code)
        self.assertIn("not registered", why)
        self.assertEqual(self.verdicts(), [])
        self.register("t2", item=other)
        self.assertEqual(self.verdict("t1", item_key=other)[0], None)     # t2 is registered for B; t1 still is not

    def test_s2_mutant_matching_on_role_and_repo_only_lets_it_through(self):
        self.register("t1")
        other = "acme-org/widget-service#778"
        with mock.patch.object(self.iv, "registration_problem", lambda *a: None), mock.patch.object(self.iv, "pinned", lambda e, i: self.PIN):
            code, why = self.verdict("t1", item_key=other)
        self.assertEqual(code, sb.EXIT_OK, "the mutant (no per-item registration) records a verdict for item B")

    # ---- S10: a forged or handmade launch record
    def test_s10_a_handmade_launch_record_with_no_switchboard_entry_registers_nothing(self):
        code, why = self.verdict("t1")          # the launch record is perfect (launched_by the orchestrator) but no ledger entry exists
        self.assertIsNone(code)
        self.assertIn("not registered", why)
        self.assertEqual(self.verdicts(), [])

    def test_s10_mutant_trusting_the_launch_record_alone_accepts_it(self):
        with mock.patch.object(self.iv, "registration_problem", lambda *a: None), mock.patch.object(self.iv, "pinned", lambda e, i: self.PIN):
            self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)

    def test_s10_a_coder_launching_its_own_tester_is_refused_by_the_launch_record_and_writes_no_registration(self):
        self.fleets["t1"]["launched_by"] = "c1"; self.fleets["t1"]["reports_to"] = "c1"
        self.register("t1", orch="c1")      # even a ledger entry naming the coder as the orchestrator cannot make it a launched tester
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("not a tester launched", why)

    def test_s10_a_replayed_ledger_line_breaks_the_chain(self):
        self.register()
        self.verdict("t1")
        path = self.state / sb.plan_ledger.LEDGER_FILE
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines + [lines[-1]]) + "\n")      # the verdict line copied once more
        self.assertIsNotNone(sb.plan_ledger.read_ledger(self.state)[1])

    # ---- S5: the reference commit does not move under the verdict
    def test_s5_a_verdict_names_the_pinned_commit_and_no_other(self):
        self.register()
        code, why = self.verdict("t1", head=self.MOVED)
        self.assertIsNone(code)
        self.assertIn("reference commit", why)
        self.assertEqual(self.verdicts(), [])
        self.assertEqual(self.verdict("t1", head=self.PIN)[0], sb.EXIT_OK)
        self.assertEqual(self.verdicts()[0]["head"], self.PIN)

    def test_s5_mutant_reading_the_tip_at_verdict_time_accepts_a_moved_commit(self):
        self.register()
        with mock.patch.object(self.iv, "pinned", lambda e, i: self.MOVED):
            code, why = self.verdict("t1", head=self.MOVED)
        self.assertEqual(code, sb.EXIT_OK, "the mutant stamps whatever the tip is now")

    def test_s5_a_new_registration_voids_the_verdicts_at_the_old_commit(self):
        self.register()
        self.verdict("t1")
        self.clock[0] += 60
        self.register("t1", head=self.MOVED)
        report = sb.item_gates_report(self.ctx(), set())
        row = next(r for r in report["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "missing")
        self.assertTrue(any("head moved" in v for v in row["void"]))

    # ---- S3/S4: CI verdicts of the repository's own checks
    RUN = {"name": "build", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}

    def ci(self, runs, on_branch=True, checks=True, workflows=None):
        self.register()
        self.pages.update({"repos/%s" % self.REPO: {"default_branch": "main"},
                           "repos/%s/compare/main...%s" % (self.REPO, self.PIN): {"status": "identical" if on_branch else "diverged"},
                           })
        for n in range(1, 6):
            self.pages["repos/%s/commits/%s/check-runs?per_page=100&page=%d" % (self.REPO, self.PIN, n)] = {"check_runs": runs[(n - 1) * 100:n * 100], "total_count": len(runs)}
        ctx = self.ctx()
        if not checks:
            ctx.config = {"plan_gates": {"repos": {self.REPO: {}}}}
        if workflows is not None:
            ctx.config["plan_gates"]["repos"][self.REPO]["ci_workflows"] = workflows
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: ctx):
            try:
                return sb.cmd_item_ci_record(types.SimpleNamespace(rest=[self.ITEM], plan=None, ask=None), None, self.now), None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def test_s3_every_listed_check_green_at_the_reference_commit_records_one_ci_verdict_by_the_recorder(self):
        self.assertEqual(self.ci([self.RUN])[0], sb.EXIT_OK)
        got = self.verdicts()
        self.assertEqual([(v["scenario"], v["by"], v["head"], v["evaluator"]) for v in got], [("S2", sb.plan_ledger.CI_RECORDER, self.PIN, "ci")])
        self.assertEqual([a[0] for a in self.audit if a[0] == "plan.item.ci_record"], ["plan.item.ci_record"])

    def test_s3_an_agent_cannot_type_a_ci_verdict_for_an_item(self):
        self.register()
        code, why = self.verdict("t1", scenario="S2", evaluator="ci")
        self.assertIsNone(code)
        self.assertIn("ci", why)
        self.assertEqual(self.verdicts(), [])

    def test_s4_nothing_configured_a_same_named_check_of_another_app_or_a_commit_off_the_branch_records_nothing(self):
        other = dict(self.RUN, app={"slug": "evil-workflow"})
        for runs, kwargs, reason in (([self.RUN], {"checks": False}, "no item_checks"), ([other], {}, "no run of build by github-actions"), ([self.RUN], {"on_branch": False}, "not on the default branch"),
                                     ([dict(self.RUN, conclusion="failure")], {}, "did not succeed"), ([dict(self.RUN, status="in_progress", conclusion=None)], {}, "not complete")):
            self.setUp()
            code, why = self.ci(runs, **kwargs)
            self.assertIsNone(code, (runs, kwargs))
            self.assertIn(reason, why)
            self.assertEqual(self.verdicts(), [], (runs, kwargs))

    def test_s4_mutant_matching_a_check_by_name_alone_records_the_faked_one(self):
        other = dict(self.RUN, app={"slug": "evil-workflow"})
        real = self.iv.ci_decision
        by_name = lambda runs, checks: real(runs, [dict(c, app=r["app"]["slug"]) for c in checks for r in runs[:1]] if runs else checks)
        with mock.patch.object(self.iv, "ci_decision", by_name):
            self.assertEqual(self.ci([other])[0], sb.EXIT_OK, "the mutant accepts the same-named run of another app")

    def _wf(self, run, concl, status="completed"):
        return dict(self.RUN, conclusion=concl, status=status, details_url="https://github.com/%s/actions/runs/%d/job/1" % (self.REPO, run))

    def _same_workflow(self):
        import sys
        ci = sb.load_module("ci_runs")
        ci._CACHE.clear(); ci._FAILED.clear()
        old = ci.READER
        ci.READER = lambda repo, run: (1, "push")
        self.addCleanup(lambda: (setattr(ci, "READER", old), ci._CACHE.clear(), ci._FAILED.clear()))

    def test_1237_plan_item_ci_record_and_the_gate_agree_on_a_cancelled_first_attempt(self):
        self._same_workflow()
        runs = [self._wf(100, "cancelled"), self._wf(101, "success")]
        self.assertEqual(self.ci(runs)[0], sb.EXIT_OK)
        rollup = [{"workflowName": "ci", "name": "build", "conclusion": "CANCELLED", "status": "COMPLETED", "detailsUrl": "https://github.com/%s/actions/runs/100/job/1" % self.REPO},
                  {"workflowName": "ci", "name": "build", "conclusion": "SUCCESS", "status": "COMPLETED", "detailsUrl": "https://github.com/%s/actions/runs/101/job/1" % self.REPO}]
        self.assertEqual(sb.pr_ci_of(rollup), "green")

    def test_1237_mutant_by_name_any_in_the_plan_item_path_refuses_the_green_head(self):
        self._same_workflow()
        with mock.patch.object(sb.load_module("ci_runs"), "latest_check_runs", lambda runs, repo: runs):
            code, why = self.ci([self._wf(100, "cancelled"), self._wf(101, "success")])
        self.assertIsNone(code)
        self.assertIn("did not succeed", why)

    # ---- #1237 round 3: ONE fixtures table of API shapes, run through BOTH the gate and the item path: the same verdict from both
    def _cr(self, run, concl, job=1, at="10", cid=1, name="build", status="completed"):
        return dict(self.RUN, name=name, conclusion=concl, status=status, id=cid, started_at="2026-10-15T%s:00:00Z" % at, completed_at="2026-10-15T%s:30:00Z" % at,
                    details_url="https://github.com/%s/actions/runs/%d/job/%d" % (self.REPO, run, job))

    def shape_rows(self):
        """(label, check runs in the Checks API shape, {run id: (workflow id, display name)} (a missing run is unreadable), expected workflow ids, green?)."""
        cr = self._cr
        many = [cr(100, "success", name="x%d" % i, cid=i) for i in range(1, 120)] + [cr(100, "success", cid=500), cr(101, "failure", cid=501)]
        return [
            ("green baseline", [cr(100, "success")], {100: (1, "ci")}, [], True),
            ("cancelled-then-green", [cr(100, "cancelled"), cr(101, "success")], {100: (1, "ci"), 101: (1, "ci")}, [], True),
            ("rerun inside one run", [cr(100, "cancelled", job=1, at="10", cid=1), cr(100, "success", job=2, at="11", cid=2)], {100: (1, "ci")}, [], True),
            ("rerun inside one run, the newest red", [cr(100, "success", job=1, at="10", cid=1), cr(100, "failure", job=2, at="11", cid=2)], {100: (1, "ci")}, [], False),
            ("two workflows with one display name, one red", [cr(100, "success"), cr(101, "failure")], {100: (1, "ci"), 101: (2, "ci")}, [], False),
            ("more than one page", many, {100: (1, "ci"), 101: (2, "ci")}, [], False),
            ("a missing expected workflow", [cr(100, "success")], {100: (1, "ci")}, ["2"], False),
            ("an expected workflow present", [cr(100, "success")], {100: (7, "anything")}, ["7"], True),
            ("an author-named workflow test", [cr(100, "success")], {100: (9, "test")}, ["7"], False),
            ("unreadable identity", [cr(100, "success")], {}, [], False),
        ]

    def gate_verdict(self, runs, wf, expected):
        names = {100: "ci", 101: "ci"}
        rollup = [{"workflowName": wf.get(int(r["details_url"].split("/runs/")[1].split("/")[0]), (0, "ci"))[1], "name": r["name"], "conclusion": r["conclusion"].upper(), "status": "COMPLETED",
                   "startedAt": r["started_at"], "completedAt": r["completed_at"], "detailsUrl": r["details_url"]} for r in runs[:99]]
        if len(runs) > 99:   # the gate reads one rollup page: more than it holds is never green (the item path pages and reads the red run)
            rollup = rollup[:99] + [{"workflowName": "ci", "name": "build", "conclusion": "FAILURE", "status": "COMPLETED", "detailsUrl": runs[-1]["details_url"]}]
        facts = sb.pr_facts_from({"state": "OPEN", "headRefOid": "a" * 40, "statusCheckRollup": rollup})
        cfg = {"plan_gates": {"repos": {self.REPO: {"ci_workflows": expected}}}}
        ctx = types.SimpleNamespace(unreadable=None, facts=facts, plan={"result": "pass", "scenarios_parsed": []}, plan_problem=None, engine=None, pr=1, config=cfg, repo=self.REPO)
        return sb.ci_record_core(ctx, lambda: 0, quiet=True)["status"] == "nothing"

    def run_shapes(self):
        ci = sb.load_module("ci_runs")
        got = {}
        for label, runs, wf, expected, want in self.shape_rows():
            self.setUp()
            ci._CACHE.clear(); ci._FAILED.clear(); ci._PATH.clear()
            old = ci.READER
            ci.READER = lambda repo, run, wf=wf: (wf[run][0], "push") if run in wf else None
            try:
                code, why = self.ci(runs, workflows=expected)
                item = code == sb.EXIT_OK
                ci._CACHE.clear(); ci._FAILED.clear()
                gate = self.gate_verdict(runs, wf, expected)
            finally:
                ci.READER = old
                ci._CACHE.clear(); ci._FAILED.clear()
            got[label] = (item, gate, want)
        return got

    def test_1237_every_api_shape_gives_the_same_verdict_from_the_gate_and_the_item_path(self):
        for label, (item, gate, want) in self.run_shapes().items():
            self.assertEqual((item, gate), (want, want), label)

    def test_1237_the_docs_list_exactly_the_shapes_of_the_table(self):
        import re as _re
        text = open(os.path.join(os.path.dirname(os.path.abspath(sb.__file__)), "..", "docs", "switchboard.md")).read()
        block = _re.search(r"<!-- ci-shapes:begin -->\n(.*?)<!-- ci-shapes:end -->", text, _re.S).group(1)
        listed = [line[2:].strip() for line in block.splitlines() if line.startswith("- ")]
        self.assertEqual(listed, [row[0] for row in self.shape_rows()])

    def test_1237_mutant_by_name_only_in_the_item_path_fails_the_rerun_row(self):
        with mock.patch.object(sb.load_module("ci_runs"), "latest_check_runs", lambda runs, repo: [r for r in runs if isinstance(r, dict)]):
            got = self.run_shapes()
        self.assertNotEqual(got["rerun inside one run"][0], True)
        self.assertNotEqual(got["cancelled-then-green"][0], True)

    def test_1237_mutant_matching_the_display_name_lets_the_author_named_workflow_pass(self):
        ci = sb.load_module("ci_runs")
        real = ci.missing_workflows
        with mock.patch.object(ci, "missing_workflows_of_checks", lambda runs, expected, repo: []):
            got = self.run_shapes()
        self.assertTrue(got["a missing expected workflow"][0])   # the item path with the list ignored reads green: the table row catches it
        self.assertTrue(got["an author-named workflow test"][0])

    def test_1237_over_the_page_cap_is_refused_naming_the_cap_and_a_failed_page_too(self):
        runs = [self.RUN] + [dict(self.RUN, name="x%d" % i) for i in range(249)]
        code, why = self.ci(runs)
        self.assertEqual(code, sb.EXIT_OK)   # 250 runs are read in 3 pages
        self.setUp()
        code, why = self.ci([self.RUN] + [dict(self.RUN, name="x%d" % i) for i in range(599)])
        self.assertIsNone(code)
        self.assertIn("more than 500 check runs", why)
        self.setUp()
        self.register()
        real = self.ci
        code, why = real(runs[:150])
        self.assertEqual(code, sb.EXIT_OK)

    def test_1237_mutant_one_page_only_reads_a_long_list_as_whole(self):
        ci = sb.load_module("ci_runs")
        with mock.patch.object(ci, "CHECK_PAGES", 1):
            code, why = self.ci([self.RUN] + [dict(self.RUN, name="x%d" % i) for i in range(249)])
        self.assertIsNone(code)   # with the cap at one page it refuses at 100: the cap is the named constant, not a silent cut
        self.assertIn("more than 100 check runs", why)

    def test_s4_the_item_checks_key_is_validated_by_its_own_module_and_defaults_to_none(self):
        self.assertEqual(self.iv.validate_item_checks(None, "c"), [])
        self.assertEqual(self.iv.validate_item_checks([{"name": "build", "app": "github-actions"}], "c"), [{"name": "build", "app": "github-actions"}])
        for bad in ("build", [{"name": "build"}], [{"name": "", "app": "x"}], [{"name": "a", "app": "b", "x": 1}], [{"name": "a", "app": "b"}] * 2):
            with self.assertRaises(ValueError):
                self.iv.validate_item_checks(bad, "c")
        raw = {"repos": {self.REPO: {"item_checks": [{"name": "build", "app": "github-actions"}]}}}
        self.assertEqual(sb.plan_ledger.validate_plan_gates(raw, "c")["repos"][self.REPO]["item_checks"], raw["repos"][self.REPO]["item_checks"])
        self.assertNotIn("item_checks", sb.plan_ledger.validate_plan_gates({"repos": {self.REPO: {}}}, "c")["repos"][self.REPO])

    # ---- S6/S7: the item-level claim
    def test_s6_an_item_claims_only_pr_scenarios_never_an_ask_level_live_one(self):
        self.register()
        code, why = self.verdict("t1", all_scenarios=True, scenario=None, head=self.PIN)
        self.assertEqual(code, sb.EXIT_OK)
        self.assertEqual([v["scenario"] for v in self.verdicts()], ["S1"])        # S3 is claim: epic, S4 is tester + measure with no claim (read as epic)
        for sid in ("S3", "S4"):
            code, why = self.verdict("t1", scenario=sid)
            self.assertIsNone(code)
            self.assertIn("epic", why)
        self.assertEqual(len(self.verdicts()), 1)

    def test_s6_mutant_giving_the_item_every_scenario_claims_the_live_one(self):
        self.register()
        with mock.patch.object(sb.plan_ledger, "claim_of", lambda s: "pr"):
            self.verdict("t1", all_scenarios=True, scenario=None, head=self.PIN)
        self.assertIn("S3", [v["scenario"] for v in self.verdicts()])

    def test_s7_an_item_that_links_the_asks_plan_claims_only_what_is_sliced_to_it(self):
        self.register()
        self.scenarios[0]["slice"] = "#778"
        self.scenarios.append({"id": "S5", "evaluator": "tester", "proof": "live", "claim": "pr", "slice": "#777"})
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), {})
        results = {}
        for sid in ("S1", "S5"):
            with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx(own=False)), mock.patch.object(sb, "identify_caller", lambda *a: "t1"), \
                    mock.patch.object(sb, "item_agents", lambda c: (launch, self.sessions, self.fleets)):
                try:
                    results[sid] = sb.cmd_item_verdict(self.args(scenario=sid), None, self.now)
                except sb.LedgerCommandError as err:
                    results[sid] = str(err)
        self.assertIn("not sliced", results["S1"])
        self.assertEqual(results["S5"], sb.EXIT_OK)
        self.assertEqual([v["scenario"] for v in self.verdicts()], ["S5"])

    # ---- S8: independence
    def test_s8_the_author_notifier_orchestrator_the_orchestrators_coder_and_a_coders_session_are_refused(self):
        self.register()
        self.fleets["nt"] = {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N}
        self.sessions["nt"] = self.sessions["c1"]            # a tester running in the session of the item's coder
        self.register("nt")
        self.fleets["human"] = {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N}
        self.sessions["human"] = "ses_h"
        self.register("human")                                # a tester that is also the issue's GitHub author
        self.fleets["wk"] = {"role": "coder", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": 700}   # the orchestrator's coder on ANOTHER item (the notifier)
        self.sessions["wk"] = "ses_wk"
        reasons = {}
        for who in ("human", "c1", "wk", "nt"):
            if who in ("c1", "wk"):
                self.register(who)
            code, why = self.verdict(who)
            self.assertIsNone(code, who)
            reasons[who] = why
        self.assertIn("wrote or runs", reasons["human"])        # the issue's author, a launched tester
        self.assertIn("wrote or runs", reasons["nt"])           # a launched tester in the session of the item's coder
        self.assertIn("not a tester launched", reasons["c1"])   # the coders are no launched testers at all
        self.assertIn("not a tester launched", reasons["wk"])
        self.assertEqual(self.verdicts(), [])
        writers = self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions)
        self.assertTrue({"orch", "c1", "wk", "human", "nt"} <= writers)       # the exclusion itself, whatever else refuses first
        self.assertEqual(self.iv.verdict_problem(self.ITEM, "wk", self.entries(), writers, "ses_wk", self.now(), self.PIN)[:2], "wk")

    def test_s8_mutant_excluding_only_the_issue_author_records_the_others(self):
        for who in ("c1", "wk"):
            self.fleets[who] = dict(self.fleets.get(who, {}), role="tester", repo=self.REPO, reports_to="orch", launched_by="orch", issue=self.N)
            self.sessions.setdefault(who, "ses_" + who)
            self.register(who)
        with mock.patch.object(self.iv, "excluded", lambda item, number, ask, author, entries, fleets, sessions: {author}):
            codes = [self.verdict(who)[0] for who in ("c1",)]
        self.assertEqual(codes, [sb.EXIT_OK], "the mutant lets the orchestrator's other worker record")

    def test_s9_a_tester_that_later_proves_for_the_item_loses_its_verdicts_at_read_time(self):
        self.register()
        self.verdict("t1")
        before = sb.item_gates_report(self.ctx(), self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions))
        self.assertEqual(next(r for r in before["scenarios"] if r["id"] == "S1")["verdict"]["by"], "t1")
        self.assertEqual(self.prove("t1")[0], sb.EXIT_OK)
        after = sb.item_gates_report(self.ctx(), self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions))
        row = next(r for r in after["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "missing")
        self.assertTrue(any("author" in m for m in row["missing"]))

    def test_s9_mutant_computing_the_writers_at_write_time_only_still_reads_ok(self):
        self.register()
        self.verdict("t1")
        self.assertEqual(self.prove("t1")[0], sb.EXIT_OK)
        report = sb.item_gates_report(self.ctx(), set())          # the mutant reads with no writers
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "ok")

    def test_s17_a_registered_tester_that_reports_done_keeps_its_verdict_and_a_coders_report_excludes_the_coder(self):
        self.register()
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)
        writers = self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions)   # a report by t1 adds nobody: reports are not read
        self.assertNotIn("t1", writers)
        self.assertIn("c1", writers)
        report = sb.item_gates_report(self.ctx(), writers)
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "ok")
        self.register("c1")
        self.assertIsNone(self.verdict("c1")[0])

    def test_s18_a_registration_expires_after_24_hours_and_ends_with_the_session_or_retirement(self):
        self.register()
        self.clock[0] += 23 * 3600
        self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)
        self.clock[0] += 2 * 3600
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("registration expired", why)
        self.register()                                      # a fresh registration for ses_t1
        self.sessions["t1"] = "ses_other"                    # the tester now runs in another session
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("another session", why)
        self.sessions["t1"] = "ses_t1"
        code, why = self.verdict("t1", retired={"t1": {"session": None}})
        self.assertIsNone(code)
        self.assertIn("not a tester launched", why)

    def test_s18_mutant_never_expiring_records_at_25_hours(self):
        self.register()
        self.clock[0] += 25 * 3600
        with mock.patch.object(self.iv, "TTL", 10 ** 9):
            self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)

    def test_a_replayed_verdict_at_an_old_commit_is_refused_after_a_repin(self):
        self.register()
        self.clock[0] += 60
        self.register("t1", head=self.MOVED)
        code, why = self.verdict("t1", head=self.PIN)
        self.assertIsNone(code)
        self.assertIn("reference commit", why)

    # ---- S12: audit
    def test_s12_a_registration_a_verdict_and_a_refusal_are_audited_with_the_item_the_writer_and_the_reference_commit(self):
        self.register()
        self.verdict("t1")
        self.verdict("c1")
        self.verdict("t1", head=self.MOVED)
        events = {e: d for e, s, d in self.audit}
        self.assertEqual(events["plan.item.verdict"]["by"], "t1")
        self.assertEqual((events["plan.item.verdict"]["item"], events["plan.item.verdict"]["reference_commit"]), (self.ITEM, self.PIN))
        refused = [d for e, s, d in self.audit if e == "plan.item.refused"]
        self.assertEqual([r["item"] for r in refused], [self.ITEM] * 2)
        self.assertEqual(sorted(r["by"] for r in refused), ["c1", "t1"])
        self.assertTrue(all(r["reference_commit"] == self.PIN for r in refused))

    # ---- S13 to S15: the draft line
    def g4(self, **facts):
        report = sb.plan_ledger.evaluate_gates(None, [], None, dict({"head": "a" * 40}, **facts), "acme-org/widget-service#9")
        return next(r for r in report["gates"] if r["gate"] == "G4")["missing"]

    def test_s13_a_draft_pr_with_no_ci_says_the_draft_line(self):
        self.assertEqual(self.g4(ci="none", draft=True), ["draft: no CI run seen; this repo's workflow may skip drafts; mark ready, then wait"])

    def test_s14_a_ready_pr_with_a_failing_check_or_an_unknown_draft_flag_keeps_the_failing_line(self):
        line = ["CI is failing (needs green at the head, or a second agent's re-run)"]
        self.assertEqual(self.g4(ci="failing", draft=False), line)
        self.assertEqual(self.g4(ci="failing", draft=True), line)      # a draft that HAS runs and a failing check: the failing line
        self.assertEqual(self.g4(ci="failing"), line)
        self.assertEqual(self.g4(ci="failing", draft=None), line)

    def test_s15_the_draft_flag_comes_from_isdraft_not_from_the_title(self):
        data = {"title": "draft: x", "state": "OPEN", "headRefOid": "a" * 40, "isDraft": False}
        self.assertIs(sb.pr_facts_from(data)["draft"], False)
        self.assertIs(sb.pr_facts_from(dict(data, title="x", isDraft=True))["draft"], True)
        self.assertIn("isDraft", sb.PR_VIEW_FIELDS.split(","))

    # ---- U2 / U3
    def test_u2_a_pull_request_number_is_refused_on_the_item_path_and_nothing_changes_for_pr(self):
        self.pages["repos/%s/issues/9" % self.REPO] = {"pull_request": {"url": "x"}, "user": {"login": "a"}}
        engine = self.ctx().engine
        with self.assertRaises(sb.LedgerCommandError) as raised:
            sb.item_issue(engine, self.REPO, 9)
        self.assertIn("is a pull request", str(raised.exception))
        self.assertEqual(sb.plan_ledger.KINDS[:13], ("proof", "verdict", "consensus", "waiver", "round", "lane_session", "lane", "response", "escalation", "decision", "abandoned", "reminder", "review_record"))

    def test_u3_a_ledger_with_item_entries_reads_with_the_chain_intact_and_read_ledger_never_looks_at_kinds(self):
        self.register()
        self.verdict("t1")
        self.assertEqual(sb.plan_ledger.read_ledger(self.state)[1], None)
        tree = ast.parse(Path(sb.plan_ledger.__file__).read_text())
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "read_ledger")
        names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
        self.assertFalse(names & {"KINDS", "ITEM_KINDS", "EVIDENCE_KINDS"}, "read_ledger must not judge kinds: an older build must read a ledger with item entries")

    # ---- item-prove and the refusals of the registration
    def prove(self, caller, sid="S1", item_key=None):
        args = types.SimpleNamespace(item=item_key or self.ITEM, scenario=[sid], plan=None, ask=None, head=None, evidence="ran it", result="pass", command=None)
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx()), mock.patch.object(sb, "identify_caller", lambda *a: caller):
            try:
                return sb.cmd_item_prove(args, None, self.now), None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def test_item_prove_records_at_the_reference_commit_audits_it_and_makes_its_author_an_excluded_writer(self):
        code, why = self.prove("c1")
        self.assertIsNone(code)
        self.assertIn("no reference commit", why)              # nothing is registered yet
        self.register()
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        proofs = [e for e in self.entries() if e.get("kind") == "proof"]
        self.assertEqual([(e["pr"], e["by"], e["head"], e["scenario"]) for e in proofs], [(self.ITEM, "c1", self.PIN, "S1")])
        self.assertIn("plan.item.proof", [a[0] for a in self.audit])
        self.assertEqual(self.prove("t1", sid="S2")[0], sb.EXIT_OK)      # a tester that proves becomes an author: its verdict is then refused
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("wrote or runs", why)
        self.assertEqual(self.prove("c1", sid="S3")[0], sb.EXIT_PARTIAL)   # an epic-claimed scenario is not claimed by an item
        self.assertEqual(len([e for e in self.entries() if e.get("kind") == "proof"]), 2)    # c1's S1 and t1's S2; S3 recorded nothing

    def register_world(self, **page_changes):
        self.pages.update({"repos/%s/issues/777" % self.REPO: {"user": {"login": "human"}, "body": "", "state": "open", "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/770"},
                           "repos/%s/issues/770" % self.REPO: {"parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/587"},
                           "repos/%s" % self.REPO: {"default_branch": "main"}, "repos/%s/commits/main" % self.REPO: {"sha": self.PIN}})
        self.pages.update(page_changes)
        agents = {n: types.SimpleNamespace(fleet=f) for n, f in self.fleets.items()}
        ctx = self.ctx()
        ctx.engine.discover = lambda now: types.SimpleNamespace(agents=agents)
        ctx.engine.now = lambda: self.now()
        ctx.engine.config = {}
        return ctx.engine

    def register_try(self, engine, launched_by="orch", reports_to="orch"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sb.register_item_tester(engine, self.state, types.SimpleNamespace(role="tester", issue=777, name="t1", reports_to=reports_to), self.REPO, launched_by,
                                    {"session": "ses_t1"}, self.now)
        return len(self.iv.registrations(self.entries(), self.ITEM)), [a for a in self.audit if a[0].startswith("plan.item.")]

    def test_every_refusal_of_a_registration_writes_no_entry_and_audits_why(self):
        issue = "repos/%s/issues/777" % self.REPO
        base = {"user": {"login": "human"}, "body": "", "state": "open", "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/770"}
        cases = (("is not open", dict(self.pages, **{issue: dict(base, state="closed")}), {}),
                 ("does not reach", {}, {"orch_charter": 999}),
                 ("could not be read", {"repos/%s/commits/main" % self.REPO: {"sha": "xyz"}}, {}),
                 ("not run by an orchestrator", {}, {"launched_by": "c1"}),
                 ("report to the orchestrator", {}, {"reports_to": "c1"}))
        for reason, changes, extra in cases:
            self.setUp()
            self.fleets["orch"]["charter"] = extra.get("orch_charter", 587)
            engine = self.register_world(**({issue: dict(base, state="closed")} if reason == "is not open" else changes))
            count, audit = self.register_try(engine, launched_by=extra.get("launched_by", "orch"), reports_to=extra.get("reports_to", "orch"))
            self.assertEqual(count, 0, reason)
            self.assertEqual([a[0] for a in audit], ["plan.item.refused"], reason)
            self.assertIn(reason, audit[0][2]["why"])
        self.setUp()                                           # a pull request number: this path does not apply, and says nothing
        engine = self.register_world(**{issue: {"pull_request": {"url": "x"}, "user": {"login": "a"}}})
        self.assertEqual(self.register_try(engine), (0, []))
        self.assertTrue(self.iv.lane_ok([777, 770, 587], 587))
        self.assertFalse(self.iv.lane_ok([777, 770], 587))
        self.assertFalse(self.iv.lane_ok([777], None))

    # ---- the pull request commands never write for an item; gates item reads only what the item path wrote; the read fails closed; the append re-checks
    def pr_path(self, command, caller="t2", picture=(None, "PLAN: acme-org/widget-service#777", [], "no pull request found"), **kw):
        """Run an ORDINARY pull request command (`prove`, `verdict`, `gates ci-record`, `waive`) with --pr naming the item's number."""
        args = types.SimpleNamespace(pr=self.ITEM, plan=None, ask=None, head=self.PIN, scenario="S1", all_scenarios=False, evaluator="tester", result="pass", evidence="x",
                                     command=None, gates="G1", reason="a long enough reason to waive")
        for k, v in kw.items():
            setattr(args, k, v)
        ctx = self.ctx()
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), {})
        with mock.patch.object(sb, "build_engine", lambda *a, **k: (ctx.engine, self.state, {"intent_repo": self.REPO})), mock.patch.object(sb, "pr_picture", lambda *a: picture), \
                mock.patch.object(sb, "identify_caller", lambda *a: caller), mock.patch.object(sb, "checked_plan", lambda *a, **k: self.plan), \
                mock.patch.object(sb, "launched_testers_for", lambda c: (launch, self.sessions, self.fleets)), mock.patch.object(sb, "gates_caller_kind", lambda *a: ("agent", "x", "chief-of-staff")):
            try:
                with captured() as (out, err):
                    code = getattr(sb, command)(args, None, self.now)
                if code == sb.EXIT_USAGE:   # #1049: a recording command ends `not recorded: <reason>` (exit 2) instead of raising
                    return None, err.getvalue()
                return code, None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def issue_page(self):
        self.pages["repos/%s/issues/777" % self.REPO] = {"user": {"login": "human"}, "body": "", "state": "open"}

    def test_a_pull_request_command_refuses_an_issue_number_and_writes_nothing(self):
        self.issue_page()
        self.register("t1")
        self.register("t2", item="acme-org/widget-service#778", head=self.MOVED)
        before = len(self.entries())
        for command in ("cmd_prove", "cmd_verdict", "cmd_gates_ci_record", "cmd_waive"):
            for caller in ("t2", "t1"):         # a tester registered for ANOTHER item, and the tester registered for this one
                code, why = self.pr_path(command, caller=caller, scenario=["S1"] if command == "cmd_prove" else "S1", pr=self.ITEM)
                self.assertIsNone(code, (command, caller))
                self.assertTrue("work item" in why or "is an issue" in why, (command, caller, why))
        self.assertEqual(len(self.entries()), before)           # nothing was written by any of them

    def test_a_pull_request_command_refuses_an_issue_number_that_has_no_registration_too(self):
        self.issue_page()
        for command in ("cmd_verdict", "cmd_prove"):
            code, why = self.pr_path(command, scenario=["S1"] if command == "cmd_prove" else "S1")
            self.assertIsNone(code)
            self.assertIn("is an issue, not a pull request", why)
        self.assertEqual(self.entries(), [])

    def test_mutant_without_the_refusal_the_pull_request_command_writes_the_item_verdict_around_every_defence(self):
        self.issue_page()
        sb.plan_ledger.append_entry(self.state, {"kind": "proof", "pr": self.ITEM, "scenario": "S9", "plan_hash": "sha256:p", "head": self.PIN, "result": "pass", "by": "c1"}, self.now())
        with mock.patch.object(sb, "item_entry_refusal", lambda *a: None), mock.patch.object(sb, "ask_subject", lambda *a: None):
            code, why = self.pr_path("cmd_verdict")              # t2 is registered for nothing, and there is no registration for the item
        self.assertEqual(code, sb.EXIT_OK, why)
        forged = [e for e in self.entries() if e.get("kind") == "verdict"]
        self.assertEqual([(e["by"], e["pr"]) for e in forged], [("t2", self.ITEM)])
        # and with the refusal in place the same call is refused (the real guard), while gates item would ignore the entry anyway
        self.assertIsNone(self.pr_path("cmd_verdict")[0])

    def test_a_real_pull_request_is_unaffected_by_the_refusal(self):
        self.issue_page()
        facts = {"head": self.PIN, "ci": "green"}
        ctx = self.ctx()
        ctx.engine.intents = types.SimpleNamespace(_api=lambda *a, **k: (_ for _ in ()).throw(AssertionError("a readable pull request must not be looked up as an issue")))
        with mock.patch.object(sb, "pr_picture", lambda *a: (facts, "", [], None)):
            args = types.SimpleNamespace(pr="acme-org/widget-service#9", plan=None, ask=None)
            got = sb._ledger_context(args, None, self.now, True, parts=(ctx.engine, self.state, {"intent_repo": self.REPO}))
        self.assertEqual((got.pr, got.facts["head"]), ("acme-org/widget-service#9", self.PIN))

    def test_gates_item_ignores_an_entry_it_cannot_tie_to_the_item_path(self):
        self.register("t1")
        entry = {"plan_hash": "sha256:p", "head": self.PIN, "result": "pass", "pr": self.ITEM, "scenario": "S1"}
        sb.plan_ledger.append_entry(self.state, dict(entry, kind="proof", by="c9"), self.now())
        sb.plan_ledger.append_entry(self.state, dict(entry, kind="verdict", by="t2", evaluator="tester"), self.now())       # what the pull request path used to write
        writers = self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions)
        report = sb.item_gates_report(self.ctx(), writers)
        row = next(r for r in report["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "missing")
        self.assertEqual((row["proof"], row["verdict"]), (None, None))
        with mock.patch.object(self.iv, "visible", lambda entries, item, recorder: entries):         # mutant: no tie to the item path
            row = next(r for r in sb.item_gates_report(self.ctx(), writers)["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "ok")

    def test_a_tester_verdict_counts_only_for_a_tester_registered_when_it_wrote_it(self):
        self.register("t1")
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)
        good = next(e for e in self.entries() if e.get("kind") == "verdict")
        self.assertTrue(self.iv.tied(good, self.entries(), sb.plan_ledger.CI_RECORDER))
        self.clock[0] += 3 * 24 * 3600                                  # read long after the registration expired (a finished, retired tester keeps its verdicts)
        self.assertTrue(self.iv.tied(good, self.entries(), sb.plan_ledger.CI_RECORDER))
        for forged in (dict(good, session="ses_other"), dict(good, by="t2"), dict(good, via="pr"), dict(good, seq=1)):
            self.assertFalse(self.iv.tied(forged, self.entries(), sb.plan_ledger.CI_RECORDER), forged)
        stale = dict(good, ts=good["ts"] + self.iv.TTL + 1)             # written after the registration had expired
        self.assertFalse(self.iv.tied(stale, self.entries(), sb.plan_ledger.CI_RECORDER))

    def unreadable_world(self):
        self.register("t1")
        self.fleets["nt"] = {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N}
        self.sessions["nt"] = self.sessions["c1"]                       # a tester in the session of the item's coder
        self.register("nt")
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        sb.plan_ledger.append_entry(self.state, {"kind": "verdict", "pr": self.ITEM, "scenario": "S1", "plan_ref": "plan", "plan_hash": "sha256:p", "head": self.PIN, "result": "pass",
                                                 "by": "nt", "evaluator": "tester", "evidence": "x", "via": "item", "session": self.sessions["c1"]}, self.now())

    def gates_item(self, agents):
        ctx = self.ctx()
        args = types.SimpleNamespace(rest=[self.ITEM], plan=None, ask=None, json=True, item=None)
        out = io.StringIO()
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: ctx), mock.patch.object(sb, "item_agents", agents), contextlib.redirect_stdout(out):
            code = sb.cmd_item_gates(args, None, self.now)
        return code, json.loads(out.getvalue())

    def test_gates_item_fails_closed_when_the_running_agents_cannot_be_read(self):
        self.unreadable_world()
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), {})
        code, report = self.gates_item(lambda c: (launch, self.sessions, self.fleets))
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "missing")      # agents read: the session-mate of a coder is excluded

        def broken(ctx):
            raise sb.ItemAgentsUnreadable("the running agents cannot be read (OSError: down)")
        code, report = self.gates_item(broken)
        self.assertEqual((code, report["ready"], report["scenarios"]), (sb.EXIT_ERROR, False, []))
        self.assertIn("NOT READY", report["rows"][0]["missing"][0])
        code, report = self.gates_item(lambda c: ({}, {}, {}))         # mutant: fail open, the writer set shrinks to the author, the proofs and the registrar
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "ok")

    def test_item_agents_raises_when_discovery_or_the_retired_list_fails(self):
        ctx = self.ctx()
        ctx.engine.now = lambda: self.now()
        ctx.engine.discover = lambda now: (_ for _ in ()).throw(OSError("herdr down"))
        with self.assertRaises(sb.ItemAgentsUnreadable):
            sb.item_agents(ctx)

    def test_the_append_rechecks_the_ledger_under_the_lock_and_refuses_a_replaced_registration(self):
        self.register()
        real = sb.plan_ledger.append_many_checked

        def racing(state, build, now):      # between the reads and the append another registration (a new pin) lands
            sb.plan_ledger.append_entry(state, {"kind": "item_tester", "pr": self.ITEM, "item": self.ITEM, "tester": "t1", "session": "ses_t1", "orchestrator": "orch", "head": self.MOVED,
                                                "plan_ref": None, "by": "orch"}, self.now())
            return real(state, build, now)
        with mock.patch.object(sb.plan_ledger, "append_many_checked", racing):
            code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("reference commit", why)
        self.assertEqual(self.verdicts(), [])
        self.assertEqual([a for e, s, a in self.audit if e == "plan.item.refused"][-1]["stage"], "append")
        self.audit.clear()
        self.setUp()
        self.register()

        def unchecked(state, build, now):   # mutant: the check made on the snapshot from BEFORE the race (what the old append amounted to)
            snapshot = sb.plan_ledger.read_ledger(state)[0]
            sb.plan_ledger.append_entry(state, {"kind": "item_tester", "pr": self.ITEM, "item": self.ITEM, "tester": "t1", "session": "ses_t1", "orchestrator": "orch", "head": self.MOVED,
                                                "plan_ref": None, "by": "orch"}, self.now())
            return sb.plan_ledger.append_many(state, build(snapshot), now)
        with mock.patch.object(sb.plan_ledger, "append_many_checked", unchecked):
            self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)         # without the re-check the verdict at the old pin is written

    def test_append_many_checked_refuses_with_the_ledger_unchanged(self):
        self.register()
        before = self.entries()

        def refuse(entries):
            raise sb.plan_ledger.LedgerError("no")
        with self.assertRaises(sb.plan_ledger.LedgerError):
            sb.plan_ledger.append_many_checked(self.state, refuse, self.now())
        self.assertEqual(self.entries(), before)
        got = sb.plan_ledger.append_many_checked(self.state, lambda have: [{"kind": "verdict", "pr": self.ITEM, "scenario": "S1", "result": "pass"}] * 2, self.now())
        self.assertEqual([e["seq"] for e in got], [len(before) + 1, len(before) + 2])
        self.assertIsNone(sb.plan_ledger.read_ledger(self.state)[1])

    def test_u1_the_pull_request_rules_are_untouched(self):
        import subprocess as sp
        for name in ("may_record_verdict", "scenario_state", "authors_of"):
            self.assertTrue(callable(getattr(sb.plan_ledger, name)))
        text = switchboard_source()
        for name in ("ci_record_core", "launched_testers_for"):
            self.assertEqual(text.count("def %s(" % name), 1)


class ReviewCommentLineTests(Base):
    HEAD = "3f2a9c1b7d4e" + "0" * 28

    def handle(self, body, cid=9001, state="open", author="alice", who="bob", action="created", merged=False, issue=True):
        pr = {"number": 10, "state": state, "user": {"login": author}, "body": "", "pull_request": {"merged_at": "x" if merged else None}}
        payload = {"action": action, "issue": pr, "comment": {"id": cid, "body": body, "user": {"login": who}}}
        return [i for i in sb._wh_issue_comment("o/r", payload, 1.0) if i.key.startswith("github.reviewcomment:")]

    def line(self, verdict, who="bob", signed=None):
        tail = " (signed %s)" % signed if signed else ""
        return "PR #10 3f2a9c1b7d4e review comment by %s%s: %s (a comment, not a recorded verdict)" % (who, tail, verdict)

    def test_each_verdict_word_in_the_table_is_delivered(self):
        for word in sb.REVIEW_VERDICTS:
            items = self.handle("Review of %s: %s\nsecret sentence" % (self.HEAD, word))
            self.assertEqual([i.summary for i in items], [self.line(word)])
            self.assertNotIn("secret", items[0].summary)

    def test_no_marker_cloud_bot_is_delivered(self):
        items = self.handle("Review of %s: APPROVE" % self.HEAD, who="acme-cloud[bot]")
        self.assertEqual([i.summary for i in items], [self.line("APPROVE", "acme-cloud[bot]")])

    def test_shared_account_with_reader_marker_is_delivered_with_signer(self):
        body = "Review of %s: CHANGES REQUESTED\n<!-- fleet-switchboard:from=rev-1 -->" % self.HEAD
        items = self.handle(body, who="alice")   # same login as the PR author
        self.assertEqual([i.summary for i in items], [self.line("CHANGES REQUESTED", "alice", "rev-1")])
        self.assertEqual(items[0].review_signer, "rev-1")

    def test_author_lane_marker_is_not_delivered_at_routing(self):
        agents = {"orch": types.SimpleNamespace(fleet={"role": "orchestrator"}), "w1": types.SimpleNamespace(fleet={"reports_to": "orch"}),
                  "rev-1": types.SimpleNamespace(fleet={})}
        self.assertTrue(sb._review_lane_member(agents, "w1", "orch"))
        self.assertTrue(sb._review_lane_member(agents, "orch", "orch"))
        self.assertFalse(sb._review_lane_member(agents, "rev-1", "orch"))
        self.assertFalse(sb._review_lane_member(agents, "ghost", "orch"))

    def test_route_drops_author_lane_and_audits_unowned(self):
        agents = {"orch": types.SimpleNamespace(fleet={"role": "orchestrator"}), "w1": types.SimpleNamespace(fleet={"reports_to": "orch"})}
        for signer, owner, expect in (("w1", "orch", False), ("rev-1", "orch", True), ("rev-1", None, False)):
            item = sb.GitHubItem("github.reviewcomment:1", "github.comment", "x", "o/r", [], [], "bob", 1.0, [10], True)
            item.review_signer = signer
            audits = []
            item.sender = "acme-cloud[bot]"
            hub = types.SimpleNamespace(queue={item.key: item}, lock=threading.RLock(), _unrouted=set(), _filtered=set(),
                                        _unrouted_batch=[], _filtered_batch=[], _routed_batch=[], _audit=lambda *a: audits.append(a),
                                        route_of=lambda *a: (owner, "closing"), _fleet_login=lambda l: True, _unowned_seen=lambda k: False)
            sb.GitHubHub._route(hub, item, types.SimpleNamespace(problems=[], agents=agents), 1.0)
            self.assertEqual(item.recipient == "orch", expect)
            self.assertEqual([a[0] for a in audits], ["github.reviewcomment.unowned"] if owner is None else [])

    def test_unmatching_forms_are_unknown_never_wrong(self):
        for tail in (" not APPROVE", " do not APPROVE", ": APPROVE.", ": approve", ": CHANGES REQUESTED, not APPROVE", " APPROVE",
                     ": APPROVE now", ":\tAPPROVE", ": APPROVE\tx", "", ":APPROVE"):
            items = self.handle("Review of %s%s" % (self.HEAD, tail))
            self.assertEqual([i.summary for i in items], [self.line("UNKNOWN")], tail)

    def test_trailing_whitespace_is_still_strict(self):
        self.assertEqual(self.handle("Review of %s: APPROVE \t" % self.HEAD)[0].summary, self.line("APPROVE"))

    def test_not_delivered(self):
        ok = "Review of %s: APPROVE" % self.HEAD
        self.assertEqual(self.handle(ok, state="closed"), [])
        self.assertEqual(self.handle(ok, merged=True), [])
        self.assertEqual(self.handle("x\n" + ok), [])
        self.assertEqual(self.handle(" " + ok), [])
        self.assertEqual(self.handle("\ufeff" + ok), [])
        self.assertEqual(self.handle(ok.replace("Review", "REVIEW")), [])
        self.assertEqual(self.handle("Review of %s: APPROVE" % self.HEAD[:39]), [])
        self.assertEqual(self.handle("Review of %s0: APPROVE" % self.HEAD), [])
        self.assertEqual(self.handle("Review of %s: APPROVE" % self.HEAD.upper()), [])
        self.assertEqual(self.handle(ok, action="edited"), [])

    def test_catchup_never_copies_text_and_shares_the_key(self):
        body = "Review of %s: APPROVE\nsecret sentence" % self.HEAD
        comment = {"id": 9001, "body": body, "user": {"login": "bob"}, "issue_url": "https://api.github.com/repos/o/r/issues/10"}
        self.assertIsNone(sb._comment_item("o/r", comment, None, 1.0))   # no PR record: the catch-up loop passes one (see ReviewCommentEndToEndTests)
        hook = self.handle(body)[0]
        self.assertEqual(hook.key, "github.reviewcomment:9001")
        self.assertNotIn("github.comment:9001", [hook.key])

    def test_key_is_by_comment_id(self):
        a = self.handle("Review of %s: APPROVE" % self.HEAD)[0].key
        self.assertEqual(a, self.handle("Review of %s: APPROVE" % self.HEAD)[0].key)
        self.assertNotEqual(a, self.handle("Review of %s: APPROVE" % self.HEAD, cid=9002)[0].key)

    def test_plain_comment_keeps_old_item(self):
        pr = {"number": 10, "state": "open", "user": {"login": "a"}, "pull_request": {}}
        items = sb._wh_issue_comment("o/r", {"action": "created", "issue": pr, "comment": {"id": 5, "body": "hi", "user": {"login": "b"}}}, 1.0)
        self.assertEqual([i.key for i in items], ["github.comment:5"])
