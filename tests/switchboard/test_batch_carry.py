"""(#955) A batch pull request (base `main`, head `integration`) carries its members' recorded clears: the pure decision over a REAL git repository, and the daemon's reads of it.
Moved from bin/test-switchboard (its line ceiling); the classes are found by the runner itself (#2206)."""
import ast
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from support.carry_git import GitHubOverGit
from support.core import Base
from support.herdr_world import World
from support.loader import sb
from support.paths import BIN
from support.scenario_roles import FAKE_GH
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE


def _host():
    """The runner's module (bin/test-switchboard): the discovery imports this file while the runner is loading, so the host is found by its file name, as the loader lines of the other files hand it over.
    None when the file is imported alone: the daemon tests then skip, the pure ones still run."""
    for module in list(sys.modules.values()):
        if str(getattr(module, "__file__", "") or "").endswith("test-switchboard") and hasattr(module, "TestClearCarryInTheDaemon"):
            return module
    return None


HOST = _host()
if HOST is not None:
    TestClearCarryInTheDaemon = HOST.TestClearCarryInTheDaemon
else:   # imported alone: only the names the daemon tests read at definition time, so the file imports; the class is skipped
    TestClearCarryInTheDaemon = type("_HostClearCarry", (Base,), {"REPO": "acme-org/widget-api", "PR": "acme-org/widget-api#7", **{n: None for n in ("route", "events", "adds", "clear", "hold")}})


class BatchOverGit(GitHubOverGit):
    """(#955) The slice of GitHub's answers a batch pull request (base `main`, head `integration`) and its member pull requests need, over a REAL git repository: each member is a branch squash-merged
    into `integration` with the title `<title> (#N)`, and main is merged into `integration` by true merges."""

    REPO = "acme-org/widget-api"

    def __init__(self, repo_dir):
        super().__init__(repo_dir, branch="integration", base="main", number=100)
        self.members, self.squash, self.member_base = {}, {}, {}

    def forget(self):
        self.answers = {}

    def member_files(self, number):
        return self.files_between("integration", self.members[number])

    def _answer(self, path, head):
        route, _, query = path.partition("?")
        tail = "/".join(route.split("/")[3:])
        parsed = {k: [v] for k, v in (q.split("=", 1) for q in query.split("&") if q)}
        if tail == "":
            return {"default_branch": "main"}
        if tail == "pulls/100":
            files = self.files_between("main", head)
            return {"head": {"sha": head, "ref": "integration", "repo": {"full_name": self.REPO}}, "base": {"ref": "main"}, "changed_files": self.changed_files_override if self.changed_files_override is not None else len(files)}
        for number in self.members:
            if tail == "pulls/%d" % number:
                return {"head": {"sha": self.rev(self.members[number])}, "base": {"ref": self.member_base.get(number, "integration"), "sha": self.rev("main")}, "merged": True, "merge_commit_sha": self.squash[number], "changed_files": len(self.member_files(number))}
            if tail == "pulls/%d/files" % number:
                return self.page(self.member_files(number), parsed)
        if tail.startswith("compare/"):
            answer = super()._answer(path, head)
            for item in answer["commits"]:
                item["commit"] = {"message": self.git("log", "-1", "--format=%B", item["sha"]).strip()}
            return answer
        if tail.startswith("commits/"):
            sha = tail[len("commits/"):]
            parents = self.git("rev-list", "--parents", "-n", "1", sha).split()[1:]
            return {"sha": sha, "parents": [{"sha": p} for p in parents], "files": self.page(self.files_between(parents[0], sha), parsed)}
        return super()._answer(path, head)


class TestBatchCarry(unittest.TestCase):
    """(#955) The clears of a batch's members carry to the batch pull request only when its diff is exactly their cleared diffs. Real git; neutral names."""

    REPO = BatchOverGit.REPO
    PROTECTED = ("bin/impact_hold.py", "docs/review-laws/impact-matrix.json")

    def setUp(self):
        self.cc = sb.load_module("clear_carry")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "repo"
        self.api = self.make(self.dir)

    @staticmethod
    def write(directory, path, text):
        target = Path(directory) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    @classmethod
    def make(cls, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        api = BatchOverGit(directory)
        g = api.git
        g("init", "-q", "-b", "main")
        g("config", "user.email", "dev@example.test")
        g("config", "user.name", "Dev")
        g("config", "commit.gpgsign", "false")
        for path, text in (("README.md", "widget api\n"), ("docs.md", "d1\nd2\nd3\n"), ("a.txt", "one\ntwo\nthree\n"), ("b.txt", "uno\ndos\ntres\n"), ("bin/impact_hold.py", "x = 1\n")):
            cls.write(directory, path, text)
        g("add", "-A")
        g("commit", "-q", "-m", "base")
        g("branch", "integration")
        return api

    def member(self, number, path, text, title="a change", extra=None):
        g = self.api.git
        g("checkout", "-q", "-B", "m%d" % number, "integration")
        self.write(self.dir, path, text)
        g("add", "-A")
        g("commit", "-q", "-m", "work")
        self.api.members[number] = "m%d" % number
        g("checkout", "-q", "integration")
        g("merge", "-q", "--squash", "m%d" % number)
        if extra:
            self.write(self.dir, *extra)
            g("add", "-A")
        g("commit", "-q", "-m", "%s (#%d)" % (title, number))
        self.api.squash[number] = self.api.rev("integration")
        self.api.forget()

    def main_moves(self, path="docs.md", text=None, merge=True, edit=None):
        g = self.api.git
        g("checkout", "-q", "main")
        self.write(self.dir, path, text if text is not None else "d1\nd2\nd3\nmain moved %s\n" % self.api.rev("main")[:6])
        g("add", "-A")
        g("commit", "-q", "-m", "main moves")
        g("checkout", "-q", "integration")
        if merge:
            if edit:
                g("merge", "-q", "--no-commit", "--no-ff", "main", check=False)
                self.write(self.dir, *edit)
                g("add", "-A")
                g("commit", "-q", "-m", "Merge main into integration")
            else:
                g("merge", "-q", "--no-ff", "--no-edit", "main")
        self.api.forget()

    def head(self):
        return self.api.rev("integration")

    def decide(self, held=(), cleared=None, closure=None, protected=None, head=None, by="the owner"):
        heads = {n: self.api.rev(b) for n, b in self.api.members.items()}
        cleared = heads if cleared is None else cleared

        def member_state(number, member_head):
            return {"held": number in held, "cleared_head": cleared.get(number), "by": by}
        return self.cc.batch_decide(self.api.get, self.REPO, 100, head or self.head(), member_state, closure or (lambda n, files, h: []), self.PROTECTED if protected is None else protected)

    def standard(self):
        self.member(11, "a.txt", "one\nTWO\nthree\n")
        self.main_moves()
        self.member(12, "c.txt", "new file\n")
        self.member(13, "b.txt", "uno\nDOS\ntres\n")
        self.main_moves()

    def test_s1_the_reason_lists_every_held_member_with_who_cleared_it_and_what_no_clear_covers(self):
        for n in range(1, 16):
            self.member(n, "f%d.txt" % n, "line %d\n" % n)
            if n in (1, 3, 5, 7, 9, 11, 13, 14, 15):
                self.main_moves()
        merges = self.decide(held=range(1, 9))["merges"]
        verdict = self.decide(held=range(1, 9), cleared={n: self.api.rev("m%d" % n) for n in range(1, 8)}, by="the Chief of Staff")   # the clear of #8 is missing
        self.assertFalse(verdict["carried"])
        summary = verdict["summary"]
        self.assertIn("held members (8)", summary)
        self.assertEqual(len(re.findall(r"#\d+ cleared by the Chief of Staff at [0-9a-f]{8}", summary)), 7)
        self.assertIn("#8 not cleared", summary)
        self.assertIn("never held (7): #9, #10, #11, #12, #13, #14, #15", summary)
        self.assertIn("merges of main: %d" % merges, summary)
        self.assertEqual(merges, 9)
        self.assertIn("#8 held by the floor and never cleared", verdict["reason"])
        self.assertTrue(self.decide(held=range(1, 9))["carried"], "with every clear in place the same batch carries")

    def test_s2_every_line_is_attributed_to_its_member_from_the_recorded_cleared_head_with_no_stored_fingerprint(self):
        self.standard()
        self.api.calls.clear()
        verdict = self.decide(held=(11, 12))   # a clear recorded before the carry existed: a head and nothing else
        self.assertEqual((verdict["carried"], verdict["uncovered"], verdict["lines"]), (True, [], 5))
        for number in (11, 12, 13):
            self.assertTrue([c for c in self.api.calls if "pulls/%d/files" % number in c], "the lines of #%d are read from its recorded head" % number)
        self.assertEqual([(m["number"], m["head"]) for m in verdict["members"]], [(n, self.api.rev("m%d" % n)) for n in (11, 12, 13)])

    def test_s3_the_exact_union_of_cleared_members_and_unheld_members_with_clean_merges_of_main_carries(self):
        self.standard()
        verdict = self.decide(held=(11, 12))
        self.assertEqual((verdict["carried"], verdict["reason"], verdict["uncovered"], verdict["merges"]), (True, None, [], 2))

    def test_s4_one_extra_line_an_amended_member_a_merge_that_touched_a_member_line_and_an_uncleared_member_hold_and_name_it(self):
        self.standard()
        g = self.api.git
        g("checkout", "-q", "-B", "good", "integration")
        good_main = self.api.rev("main")
        for label, setup, held, cleared, word in (
                ("an extra line", lambda: self.main_moves("docs.md", "d1\nd2\nd3\nx\n", edit=("README.md", "widget api\nsneaked in\n")), (11, 12), None, "no member's change"),
                ("an amended member", lambda: None, (11, 12), {11: "f" * 40, 12: self.api.rev("m12")}, "amended after its clear"),
                ("a merge that touched a member line", lambda: self.main_moves("docs.md", "d1\nd2\nd3\nlater\n", edit=("a.txt", "one\nTWO\nthree\nresolved\n")), (11, 12), None, "member #11 changed it"),
                ("an uncleared member", lambda: None, (11, 12, 13), {11: self.api.rev("m11"), 12: self.api.rev("m12")}, "#13 held by the floor and never cleared")):
            with self.subTest(label):
                g("checkout", "-q", "-B", "integration", "good")
                g("branch", "-f", "main", good_main)
                setup()
                self.api.forget()
                verdict = self.decide(held=held, cleared=cleared)
                self.assertFalse(verdict["carried"], label)
                self.assertIn(word, verdict["reason"], label)

    def test_s4_a_commit_that_is_not_a_squash_of_a_member_holds(self):
        self.standard()
        self.write(self.dir, "a.txt", "one\nTWO\nthree\nfour\n")
        self.api.git("commit", "-q", "-am", "direct commit")
        self.api.forget()
        verdict = self.decide(held=(11, 12))
        self.assertFalse(verdict["carried"])
        self.assertIn("neither a merge of main nor the squash of a pull request", verdict["reason"])

    def test_s4_a_squash_that_is_not_its_members_change_and_a_base_that_is_not_the_default_branch_hold(self):
        self.standard()
        self.member(15, "c2.txt", "n\n", extra=("README.md", "widget api\nrode along\n"))
        verdict = self.decide(held=(11, 12))
        self.assertFalse(verdict["carried"])
        self.assertIn("is not the change of its pull request", verdict["reason"])
        self.setUp()
        self.standard()
        real = self.api.get
        self.api.get = lambda path: {"default_branch": "trunk"} if path == "repos/%s" % self.REPO else real(path)
        verdict = self.decide(held=(11, 12))
        self.assertFalse(verdict["carried"])
        self.assertIn("base is not the default branch", verdict["reason"])

    def test_s5_a_member_that_touches_a_never_carried_path_or_the_clear_closure_needs_a_clear_of_its_own(self):
        for label, kwargs in (("a table path", {"path": "bin/impact_hold.py", "closure": None}),
                              ("a path of the matrix itself", {"path": "docs/review-laws/impact-matrix.json", "closure": None}),
                              ("a closure line", {"path": "b.txt", "closure": lambda n, files, h: ["it changes impact_clear_core, part of the clear (head file)"]})):
            with self.subTest(label):
                self.setUp()
                self.member(11, kwargs["path"], "changed\n")
                verdict = self.decide(held=(11,), closure=kwargs["closure"])
                self.assertFalse(verdict["carried"], label)
                self.assertIn("needs a clear of its own", verdict["reason"], label)

    def test_s5_the_table_is_the_matrixs_and_an_empty_table_refuses(self):
        self.member(11, "a.txt", "one\nTWO\nthree\n")
        self.assertTrue(self.decide(held=(11,))["carried"])
        verdict = self.decide(held=(11,), protected=())
        self.assertFalse(verdict["carried"])
        self.assertIn("no list of protected paths", verdict["reason"])
        real = sb.impact_floor.carry_never_paths()
        self.assertIn("bin/impact_hold.py", real)
        self.member(12, "bin/impact_hold.py", "x = 2\n")
        self.assertFalse(self.decide(held=(11,), protected=real)["carried"], "the matrix's own table, not a copy, makes the refusal")

    def test_u2_an_unreadable_member_list_a_truncated_diff_an_omitted_patch_and_a_head_that_moved_hold(self):
        self.standard()
        old = self.head()
        for label, setup in (("an unreadable member", lambda: setattr(self.api, "fail", "pulls/12")),
                             ("a truncated diff", lambda: setattr(self.api, "changed_files_override", 99)),
                             ("a head that moved", lambda: None)):
            with self.subTest(label):
                self.api.fail, self.api.changed_files_override = None, None
                setup()
                self.api.forget()
                if label == "a head that moved":
                    self.api.git("commit", "--allow-empty", "-q", "-m", "moves")
                verdict = self.decide(held=(11, 12), head=(old if label == "a head that moved" else None))
                self.assertFalse(verdict["carried"], label)
                self.assertIsNotNone(verdict["reason"], label)
        with self.subTest("an omitted patch"):
            self.setUp()
            self.member(11, "blob.bin", "x")
            real = self.api.get
            def omitted(path):
                answer = real(path)
                if path.startswith("repos/%s/pulls/11/files" % self.REPO):
                    answer = [dict(f, patch=None, changes=3) for f in answer]
                return answer
            self.api.get = omitted
            self.assertFalse(self.decide(held=(11,))["carried"])

    def test_u4_a_new_commit_on_the_batch_branch_is_a_new_head_and_a_clear_for_another_head_does_not_cover(self):
        self.standard()
        old = self.head()
        self.assertTrue(self.decide(held=(11, 12), head=old)["carried"])
        self.member(14, "b2.txt", "more\n")
        verdict = self.decide(held=(11, 12, 14), cleared={11: self.api.rev("m11"), 12: self.api.rev("m12")})
        self.assertFalse(verdict["carried"])
        self.assertIn("#14 held by the floor and never cleared", verdict["reason"])
        self.assertIn("head moved", self.decide(held=(11, 12), head=old)["reason"])

    def test_s8_the_documentation_describes_the_batch_carry_beside_the_single_pull_request_carry(self):
        text = (BIN.parent / "docs" / "switchboard.md").read_text()
        self.assertIn("### The clears of a batch's members carry to the batch (#955)", text)
        self.assertLess(text.index("### An owner's clear carries over a pure merge of main (#510)"), text.index("### The clears of a batch's members carry to the batch (#955)"))
        self.assertIn("gate.batch_carried", text)
        self.assertNotIn("whoever cleared that member earlier", text)

    def test_s7_a_mutant_that_carries_without_the_line_check_or_for_another_member_head_or_skips_the_never_carried_check_fails(self):
        source = (BIN / "clear_carry.py").read_text()

        def run(cc):
            """The names of the checks that fail against this build of the module."""
            failed = []
            for label, build, expect in (("union carries", lambda w: None, True), ("extra line holds", lambda w: (w.main_moves("docs.md", "d1\nd2\nd3\nx\n", edit=("README.md", "widget api\nsneaked in\n"))), False),
                                         ("amended member holds", lambda w: None, False), ("squash extra holds", lambda w: w.member(15, "c2.txt", "n\n", extra=("README.md", "widget api\nrode along\n")), False), ("table path holds", lambda w: w.member(15, "bin/impact_hold.py", "x = 2\n"), False)):
                with tempfile.TemporaryDirectory() as tmp:
                    world = TestBatchCarry("test_s3_the_exact_union_of_cleared_members_and_unheld_members_with_clean_merges_of_main_carries")
                    world.cc = cc
                    world.api = world.make(Path(tmp) / "r")
                    world.dir = Path(tmp) / "r"
                    world.standard()
                    build(world)
                    cleared = {11: "f" * 40, 12: world.api.rev("m12")} if label == "amended member holds" else None
                    held = (11, 12, 15) if label == "table path holds" else (11, 12)
                    if label == "table path holds":
                        cleared = {n: world.api.rev("m%d" % n) for n in (11, 12, 15)}
                    if world.decide(held=held, cleared=cleared)["carried"] is not expect:
                        failed.append(label)
            return failed
        self.assertEqual(run(self.cc), [], "the real module passes all four")
        for name, old, new, label in (("no line check", "extra = _excess(seen, expected)", "extra = {}", "extra line holds"),
                                      ("another member's head", "if held and cleared_head != own_head:", "if False:", "amended member holds"),
                                      ("no never-carried check", "if path and _norm(path) in tables:", "if False:", "table path holds"),
                                      ("no squash check", 'if fingerprint_of(squash, "", True) != fingerprint_of(own, "", True):', "if False:", "squash extra holds")):
            self.assertEqual(source.count(old), 1, name)
            mutant = types.ModuleType("clear_carry_mutant_%s" % name.replace(" ", "_"))
            exec(compile(source.replace(old, new), "clear_carry_mutant", "exec"), mutant.__dict__)
            self.assertIn(label, run(mutant), "the mutant '%s' is caught" % name)

    def mutant(self, old, new, name):
        """clear_carry with ONE check edited out (the source is read, one edit, compiled): a test that holds against the real module and carries against this one fails when the check is removed."""
        source = (BIN / "clear_carry.py").read_text()
        self.assertEqual(source.count(old), 1, name)
        module = types.ModuleType("clear_carry_mutant_%s" % name.replace(" ", "_"))
        exec(compile(source.replace(old, new), "clear_carry_mutant", "exec"), module.__dict__)
        return module

    def test_a_commit_is_bound_to_the_merge_of_its_member_into_integration(self):
        self.standard()
        self.api.squash[11] = "9" * 40   # GitHub says #11 was merged by another commit: the commit titled (#11) is not its merge
        self.api.forget()
        verdict = self.decide(held=(11, 12))
        self.assertFalse(verdict["carried"])
        self.assertIn("is not the merge of pull request #11 into integration", verdict["reason"])
        self.api.squash[11] = self.api.git("log", "--format=%H", "--grep", "(#11)", "-n", "1").strip()
        self.api.member_base[11] = "main"   # merged into another branch
        self.api.forget()
        self.assertIn("is not the merge of pull request #11 into integration", self.decide(held=(11, 12))["reason"])
        self.api.member_base.clear()
        self.api.forget()
        self.assertTrue(self.decide(held=(11, 12))["carried"], "and with the binding true it carries")
        mutant = self.mutant('if pull.get("merged") is not True or pull.get("merge_commit_sha") != c["sha"] or base_ref_of(pull) != branch:', "if False:", "no merge binding")
        self.api.squash[11] = "9" * 40
        self.api.forget()
        broken = mutant.batch_decide(self.api.get, self.REPO, 100, self.head(), lambda n, h: {"held": n in (11, 12), "cleared_head": self.api.rev("m%d" % n), "by": "the owner"}, lambda n, f, h: [], self.PROTECTED)
        self.assertTrue(broken["carried"], "the mutant without the binding carries a commit that is not the member's merge, which the real check refuses")

    def test_a_merge_whose_second_parent_is_not_on_the_default_branch_holds(self):
        self.standard()
        g = self.api.git
        earlier = g("log", "--format=%H", "--grep", "(#11)", "-n", "1").strip()   # a commit of integration, not of main
        tip = self.head()
        tree = g("rev-parse", tip + "^{tree}").strip()
        merge = g("commit-tree", tree, "-p", tip, "-p", earlier, "-m", "Merge side into integration").strip()
        g("reset", "-q", "--hard", merge)
        self.api.forget()
        verdict = self.decide(held=(11, 12))
        self.assertFalse(verdict["carried"])
        self.assertIn("is not on main: only a merge of the base branch is carried", verdict["reason"])
        mutant = self.mutant("check_main_side(get, repo, base, {\"sha\": c[\"sha\"], \"parents\": c[\"parents\"]})", "pass", "no second parent check")
        broken = mutant.batch_decide(self.api.get, self.REPO, 100, self.head(), lambda n, h: {"held": n in (11, 12), "cleared_head": self.api.rev("m%d" % n), "by": "the owner"}, lambda n, f, h: [], self.PROTECTED)
        self.assertTrue(broken["carried"], "the mutant without the check carries a merge of a side branch")

    def test_a_member_in_the_batch_twice_holds(self):
        class Anything(str):   # a merge commit sha that equals every commit: isolates the duplicate check from the merge binding
            def __eq__(self, other):
                return True

            def __ne__(self, other):
                return False
            __hash__ = str.__hash__
        a, b, root = "a" * 40, "b" * 40, "c" * 40

        def get(path):
            if "/compare/" in path:
                return {"status": "ahead", "total_commits": 2, "commits": [{"sha": b, "parents": [{"sha": a}], "commit": {"message": "second (#11)"}},
                                                                            {"sha": a, "parents": [{"sha": root}], "commit": {"message": "first (#11)"}}]}
            if path.endswith("/pulls/11"):
                return {"merged": True, "merge_commit_sha": Anything("d" * 40), "base": {"ref": "integration"}}
            raise AssertionError(path)
        with self.assertRaises(self.cc.CarryVoid) as raised:
            self.cc.batch_members(get, self.REPO, "main", b)
        self.assertIn("is in the batch twice", str(raised.exception))
        mutant = self.mutant('if any(m["number"] == number for m in members):', "if False:", "no duplicate check")
        members, _ = mutant.batch_members(get, self.REPO, "main", b)
        self.assertEqual([m["number"] for m in members], [11, 11], "the mutant without the check lists the member twice")


@unittest.skipIf(HOST is None, "runs under bin/test-switchboard, which holds the daemon fixtures of TestClearCarryInTheDaemon")
class TestBatchCarryInTheDaemon(Base):
    """(#955) In the daemon: one audit line per batch head, no label, every reader of "is it held" agrees, the owner's revoke takes it back for good, and a new head is judged afresh."""

    REPO, PR = BatchOverGit.REPO, "acme-org/widget-api#100"
    route = TestClearCarryInTheDaemon.__dict__["route"]
    events, adds = TestClearCarryInTheDaemon.__dict__["events"], TestClearCarryInTheDaemon.__dict__["adds"]

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "repo"
        helper = TestBatchCarry("test_s3_the_exact_union_of_cleared_members_and_unheld_members_with_clean_merges_of_main_carries")
        helper.dir = self.dir
        self.api = helper.make(self.dir)
        helper.api = self.api
        self.helper = helper
        real_rev = self.api.rev
        self.api.rev = lambda ref: real_rev("integration" if ref == "feature" else ref)   # the shared fake `route` reads the pull request's head as `feature`
        helper.standard()
        self.writes, self.api_gets, self.labels = [], 0, []
        self.world = World(self, agents=(), config={"gh": FAKE_GH, "impact_hold": {"enabled": True, "carry_clear": True}})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.engine = self.world.new_engine("daemon")
        self.engine.gh_runner, self.engine.state_dir = self.route, self.world.state
        self.names = sb.label_names(self.engine.config)
        self.record_clears(11, 12)
        self.held = (11, 12)
        patcher = mock.patch.object(sb, "pr_floor_report", self.floor)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def head(self):
        return self.api.rev("integration")

    def record_clears(self, *numbers):
        state, _ = sb.read_impact_state(self.world.state)
        for n in numbers:
            state["clears"]["%s#%d" % (self.REPO, n)] = {"head": self.api.rev("m%d" % n), "reason": "cleared on its own pull request", "by": "owner", "at": 1}
        sb.write_impact_state(self.world.state, state)

    def floor(self, engine, repo, number, rated_pairs=()):
        if number == 100:
            return {"pr": self.PR, "head": self.head, "head_ref": "integration", "owner_decision": "hold", "owner_reasons": ["a top tier path"], "floor_paths": ["x"], "globs": ["x"], "reasons": ["a top tier path"], "complete": True, "unknown": None}
        return {"pr": "%s#%d" % (repo, number), "head": self.api.rev("m%d" % number), "owner_decision": "hold" if number in self.held else "none", "owner_reasons": [], "complete": True, "unknown": None}

    def judge(self):
        report = {"pr": self.PR, "head": self.head, "head_ref": "integration", "reasons": ["a top tier path"], "floor_paths": ["x"], "globs": ["x"], "owner_decision": "hold"}
        return sb.apply_impact_hold(self.engine, self.REPO, 100, report, {"labels": []})

    def state(self):
        return sb.read_impact_state(self.world.state)[0]

    def test_s6_one_audit_line_per_head_no_label_and_a_revoke_holds_it_again_for_good(self):
        self.assertEqual(self.judge(), 0)
        for _ in range(3):
            self.judge()
        (line,) = self.events("gate.batch_carried")
        self.assertEqual(line["detail"]["head"], self.head)
        self.assertEqual({m["pr"]: m["head"] for m in line["detail"]["members"]}, {"%s#%d" % (self.REPO, n): self.api.rev("m%d" % n) for n in (11, 12, 13)})
        self.assertEqual(self.writes, [], "no label and nothing else is written to GitHub")
        self.assertTrue(sb.batch_carried(self.engine, self.state(), self.REPO, self.PR, self.head))
        args = types.SimpleNamespace(pr=self.PR, reason="taking the carry back, it is a fixture", revoke=True)
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "impact_hold": {"enabled": True, "carry_clear": True}})
        with mock.patch.object(sb, "gates_caller_kind", return_value=("person", "owner")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sb.cmd_impact_clear(args, self.route, self.world.clock.now), 0)
        self.assertFalse(sb.batch_carried(self.engine, self.state(), self.REPO, self.PR, self.head), "after the revoke the batch is held again")
        for _ in range(3):
            self.judge()
        self.assertEqual(len(self.events("gate.batch_carried")), 1, "and does not carry again at this head")
        self.assertEqual(len(self.events("gate.clear_revoked")), 1)

    def test_u3_the_daemon_the_gate_the_dry_run_and_the_sweep_all_read_a_carried_batch_as_not_held(self):
        self.judge()
        state, head = self.state(), self.head
        self.assertEqual(sb.gate_floor_fact(self.engine, self.world.state, self.REPO, 100, head)["cleared"], True, "gate G6")
        report = self.floor(self.engine, self.REPO, 100)
        self.assertEqual(sb.impact_plan(self.engine, report, [], state, {}, None)[0], "cleared", "the dry run")
        self.engine.ci_record_state = types.SimpleNamespace(pr_state={self.PR: {"head": head}})
        self.assertTrue(sb.sweep_clear_agrees(self.engine, self.REPO, 100, [self.names["merge-captain"]], "automatic"), "the sweep")
        self.assertEqual((self.judge(), self.adds()), (0, []), "the daemon's pass")
        self.engine.config["impact_hold"]["carry_clear"] = False
        self.assertFalse(sb.batch_carried(self.engine, state, self.REPO, self.PR, head), "switched off: held again, every reader")

    def test_u4_a_new_head_is_judged_afresh_and_the_carry_creates_no_clear(self):
        self.judge()
        before = self.state()["clears"]
        self.helper.member(14, "b2.txt", "more\n")
        self.held = (11, 12, 14)   # the floor holds the new member and nobody has cleared it
        self.judge()
        state = self.state()
        self.assertFalse(sb.batch_carried(self.engine, state, self.REPO, self.PR, self.head), "the carry is bound to a head")
        self.assertEqual(state["clears"], before, "the carry made no clear")
        self.assertEqual(len(self.events("gate.batch_carried")), 1)
        (held,) = self.events("gate.batch_carry_held")
        self.assertEqual(held["detail"]["head"], self.head)
        self.assertEqual(state["batch_void"][self.PR]["head"], self.head)

    def test_a_member_clear_taken_back_while_the_batch_was_judged_is_not_carried(self):
        report = self.floor(self.engine, self.REPO, 100)
        verdict = sb.impact_batch_decision(self.engine, self.REPO, 100, dict(report, rehold=None))
        self.assertTrue(verdict["carried"])
        state, _ = sb.read_impact_state(self.world.state)
        del state["clears"]["%s#11" % self.REPO]   # the owner revokes a member's clear after the verdict
        sb.write_impact_state(self.world.state, state)
        self.assertFalse(sb._batch_record(self.engine, self.REPO, 100, report, verdict))
        self.assertEqual(self.events("gate.batch_carried"), [])
        self.assertIn("taken back", self.events("gate.batch_carry_held")[0]["detail"]["reason"])

    def test_a_rating_that_holds_the_batch_refuses_the_carry(self):
        report = dict(self.floor(self.engine, self.REPO, 100), rehold=None, review_action="hold")
        verdict = sb.impact_batch_decision(self.engine, self.REPO, 100, report)
        self.assertFalse(verdict["carried"])
        self.assertIn("needs a clear of its own", verdict["reason"])

    def test_the_owner_taking_back_a_member_clear_after_the_carry_voids_the_batch_carry(self):
        self.judge()
        self.assertTrue(sb.batch_carried(self.engine, self.state(), self.REPO, self.PR, self.head))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "impact_hold": {"enabled": True, "carry_clear": True}})
        args = types.SimpleNamespace(pr="%s#11" % self.REPO, reason="taking the member clear back, a fixture", revoke=True)
        with mock.patch.object(sb, "gates_caller_kind", return_value=("person", "owner")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sb.cmd_impact_clear(args, self.route, self.world.clock.now), 0)
        self.assertFalse(sb.batch_carried(self.engine, self.state(), self.REPO, self.PR, self.head))
        self.assertEqual(self.state()["batch_void"][self.PR]["head"], self.head)
        self.judge()
        self.assertEqual(len(self.events("gate.batch_carried")), 1, "and it does not carry again at that head")

    def test_u1_a_pull_request_whose_head_is_not_the_integration_branch_is_not_a_batch(self):
        report = dict(self.floor(self.engine, self.REPO, 100), head_ref="feature", rehold=None)
        gets = self.api.calls[:]
        self.assertIsNone(sb.impact_batch_decision(self.engine, self.REPO, 100, report))
        self.assertEqual(self.api.calls, gets, "no read of GitHub for a pull request that is not from the integration branch")
        self.assertEqual((self.events("gate.batch_carried"), self.events("gate.batch_carry_held")), ([], []))

    def route_batch(self, argv, **kwargs):
        """The shared route, with the body of #100 written back (the marker edit) and the labels of #100 following the adds and deletes."""
        if argv[0] == FAKE_GH and argv[1] == "api" and "--method" in argv:
            method = argv[argv.index("--method") + 1]
            if method == "PATCH" and argv[2].endswith("/pulls/100"):
                self.body = json.loads(kwargs["input"])["body"]
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            if method == "DELETE" and "/issues/100/labels/" in argv[2]:
                name = urllib.parse.unquote(argv[2].rsplit("/", 1)[1])
                self.labels = [l for l in self.labels if l != name]
            if method == "POST" and argv[2].endswith("/issues/100/labels"):
                self.labels = self.labels + [l for l in json.loads(kwargs["input"]).get("labels", []) if l not in self.labels]
        return self.route(argv, **kwargs)

    def unclear(self):
        state = self.state()
        state["clears"].clear()
        sb.write_impact_state(self.world.state, state)

    def g6(self):
        raw = {"state": "OPEN", "headRefOid": self.head, "title": "t", "labels": [{"name": n} for n in self.labels], "body": self.body, "author": {"login": "bot"}}
        facts = sb.pr_facts_from(raw)
        facts["floor"] = sb.gate_floor_fact(self.engine, self.world.state, self.REPO, 100, self.head)
        rows = sb.plan_ledger.evaluate_gates(None, [], None, facts, self.PR)
        (row,) = [r for r in rows["gates"] if r["gate"] == "G6"]
        return row

    def test_m1_a_batch_held_first_and_carried_later_is_released_like_the_single_pull_request_carry_and_the_real_g6_reads_it_cleared(self):
        self.engine.gh_runner = self.route_batch
        self.unclear()
        self.body = "MERGE: captain\n\nthe batch\n"
        self.assertEqual(self.judge() >= 0, True)   # members not cleared yet: held, both labels written
        self.assertEqual(sorted(self.labels), sorted([self.names["merge-captain"], self.names["hold-impact"]]))
        self.assertIn(self.PR, self.state()["applied"])
        self.assertEqual(self.g6()["status"], "waiting", "held: both labels and the marker")
        self.record_clears(11, 12)   # the owner clears the members afterwards; the SAME head
        self.judge()
        state = self.state()
        self.assertTrue(sb.batch_carried(self.engine, state, self.REPO, self.PR, self.head))
        self.assertNotIn(self.PR, state["applied"], "the daemon's record of its labels is dropped")
        self.assertEqual(self.labels, [], "the labels the daemon wrote are removed")
        (released,) = self.events("gate.hold_released")
        self.assertEqual(sorted(released["detail"]["removed"]), sorted([self.names["merge-captain"], self.names["hold-impact"]]))
        self.assertTrue(self.body.startswith("MERGE: automatic"), "the marker line is neutralised: the gate reads the body while no label says otherwise")
        row = self.g6()
        self.assertEqual((row["status"], row["detail"]), ("ok", "not a held PR"), "the REAL gate G6 reads the carried batch as cleared")
        self.assertEqual(len(self.events("gate.batch_carried")), 1)
        self.judge()
        self.assertEqual(len(self.events("gate.hold_released")), 1, "once")

    def test_m2_a_batch_opened_first_and_its_members_cleared_after_is_carried_at_the_next_pass_at_the_same_head(self):
        self.unclear()
        self.judge()
        state = self.state()
        self.assertEqual(state["batch_void"][self.PR]["soft"], "uncleared")
        self.judge()   # no new clear: not judged again (memoised by the clear state)
        self.assertEqual(len(self.events("gate.batch_carry_held")), 1)
        self.record_clears(11, 12)
        self.judge()
        self.assertTrue(sb.batch_carried(self.engine, self.state(), self.REPO, self.PR, self.head))
        self.assertNotIn(self.PR, self.state()["batch_void"], "the refusal of an earlier pass is gone")

    def test_m2_a_failed_read_is_retried_and_is_bounded(self):
        real_get = self.api.get
        failing = [True]

        def get(path):
            if failing[0]:
                raise RuntimeError("a transient failure of GitHub")
            return real_get(path)
        self.api.get = get
        self.judge()
        void = self.state()["batch_void"][self.PR]
        self.assertEqual((void["soft"], void["tries"]), ("read", 1))
        failing[0] = False
        self.judge()
        self.assertTrue(sb.batch_carried(self.engine, self.state(), self.REPO, self.PR, self.head), "the same head, read again: carried")
        # bounded: a read that keeps failing is judged at most 3 times for a head
        self.api.get = get
        failing[0] = True
        state = self.state()
        state["carried"].pop(self.PR)
        sb.write_impact_state(self.world.state, state)
        for _ in range(6):
            self.judge()
        self.assertEqual(self.state()["batch_void"][self.PR]["tries"], 3)

    def test_a_definite_refusal_is_kept_for_the_head(self):
        self.helper.member(14, "b2.txt", "more\n")
        self.held = (11, 12, 14)
        self.record_clears(14)
        self.helper.write(self.dir, "README.md", "widget api\nextra\n")
        self.api.git("commit", "-q", "-am", "direct commit")
        self.api.forget()
        self.judge()
        self.assertIsNone(self.state()["batch_void"][self.PR].get("soft"))
        gets = self.api_gets
        self.judge()
        self.assertEqual(self.api_gets, gets, "a definite refusal is not read again at this head")

    def test_every_place_the_batch_carry_writes_or_releases_is_in_one_table(self):
        """Derived from the source (AST): every function of bin/impact_hold.py that mentions a batch AND writes (set, setdefault, pop) one of the carry's state keys, or removes labels, edits the marker or tells the lane."""
        import ast
        source = (BIN / "impact_hold.py").read_text()
        keys, calls, found = {"carried", "batch_void", "applied"}, {"change_issue_labels", "neutralise_marker", "notify_clear", "write_impact_state"}, {}
        for fn in ast.walk(ast.parse(source)):
            if not isinstance(fn, ast.FunctionDef) or "batch" not in (ast.get_source_segment(source, fn) or ""):
                continue
            touched = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("setdefault", "pop") and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in keys:
                    touched.add((node.args[0].value, node.func.attr))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "pop" and isinstance(node.func.value, ast.Call) and isinstance(node.func.value.func, ast.Attribute) and node.func.value.func.attr == "setdefault" and node.func.value.args and isinstance(node.func.value.args[0], ast.Constant) and node.func.value.args[0].value in keys:
                    touched.add((node.func.value.args[0].value, "pop"))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "pop" and isinstance(node.func.value, ast.Subscript) and isinstance(node.func.value.slice, ast.Constant) and node.func.value.slice.value in keys:
                    touched.add((node.func.value.slice.value, "pop"))
                if isinstance(node, (ast.Assign, ast.Delete)):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript):
                            inner = target.value
                            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == "setdefault" and inner.args and isinstance(inner.args[0], ast.Constant) and inner.args[0].value in keys:
                                touched.add((inner.args[0].value, "set"))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in calls and node.func.id != "write_impact_state":
                    touched.add((node.func.id, "call"))
            if touched:
                found[fn.name] = touched
        table = {   # the members checked, one row per function of impact_hold.py that mentions a batch and writes or releases carry state
            "_batch_record": {"carried", "batch_void"},   # the verdict: a carry (+ gate.batch_carried), or a refusal (definite, or soft and judged again)
            "_batch_release": {"applied", "change_issue_labels", "neutralise_marker", "notify_clear"},   # the release of the hold the daemon wrote: record, labels, marker, notice
            "cmd_impact_clear": {"batch_void", "carried"},   # the owner's revoke of a batch carry, and of a member's clear that a carry relied on
            "apply_impact_hold": {"notify_clear"},   # the notice of the carry of ONE pull request (unchanged, PC-1); it reaches a batch only through `_batch_release`
            "_apply_impact_hold_locked": {"applied", "carried", "change_issue_labels"},   # the carry of ONE pull request (unchanged, PC-1): it only READS a batch (`batch_carried`)
        }
        self.assertEqual({k: {name for name, _ in v} for k, v in found.items()}, table, "a place that writes or releases the carry's state is not in the table: add it, with a test")
        for name in ("_batch_record", "_batch_release", "cmd_impact_clear"):
            self.assertIn(name, found)


