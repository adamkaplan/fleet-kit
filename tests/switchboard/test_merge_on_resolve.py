"""Merge on resolve (#2204, S2-S5): the daemon step of bin/switchboard/merge_on_resolve.py on a real fixture repository and a fake hub (fake merge and notice sinks)."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PART = REPO / "bin" / "switchboard" / "merge_on_resolve.py"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def load_part(text=None):
    scope = {}
    exec(compile(text if text is not None else PART.read_text(), str(PART), "exec"), scope)
    return scope


def git(cwd, *argv, check=True):
    done = subprocess.run(["git", "-C", str(cwd)] + list(argv), capture_output=True, text=True, env=ENV)
    if check and done.returncode != 0:
        raise AssertionError("git %s: %s" % (argv, done.stderr))
    return done.stdout.strip()


def write(cwd, name, text):
    Path(cwd, name).write_text(text)
    git(cwd, "add", name)


class Fixture:
    """main holds a.txt and b.txt; `feature` forks, changes f.txt and is read at A."""

    def __init__(self, root):
        self.dir = Path(root)
        git(self.dir, "init", "-q", "-b", "main")
        for name in ("a.txt", "b.txt"):
            write(self.dir, name, "1\n2\n3\n")
        git(self.dir, "commit", "-qm", "base")
        git(self.dir, "checkout", "-qb", "feature")
        write(self.dir, "f.txt", "feature\n")
        git(self.dir, "commit", "-qm", "feature work")
        self.approved = git(self.dir, "rev-parse", "HEAD")
        git(self.dir, "checkout", "-q", "main")
        write(self.dir, "b.txt", "1\n2\n3\n4\n")
        git(self.dir, "commit", "-qm", "tip moves")
        git(self.dir, "checkout", "-q", "feature")

    def clean_merge(self):
        git(self.dir, "merge", "-q", "--no-ff", "-m", "merge the tip", "main")

    def hand_merge(self):
        git(self.dir, "merge", "-q", "--no-ff", "--no-commit", "main")
        write(self.dir, "a.txt", "edited by hand\n")   # a change the merge did not make
        git(self.dir, "commit", "-qm", "merge the tip, resolved by hand")

    def code_commit(self):
        write(self.dir, "f.txt", "feature, more\n")
        git(self.dir, "commit", "-qm", "more feature work")

    def view(self, labels=("lane:merge-on-resolve",), comments=None):
        head = git(self.dir, "rev-parse", "HEAD")
        comments = [{"body": "Review of %s: APPROVE\nlooks right" % self.approved[:12]}] if comments is None else comments
        return {"state": "OPEN", "labels": [{"name": l} for l in labels], "comments": comments, "headRefOid": head, "baseRefName": "main"}


class Hub:
    def __init__(self, fx, drops=None, merge_code=0):
        self.merges, self.notices, self.fx = [], [], fx
        self.ctx = {"runner": subprocess.run, "git_dir": fx.dir, "prepare": lambda r, n, b: ("main", "feature"),
                    "is_held": lambda labels: any("hold" in str(l) for l in labels), "drops": lambda r, n: drops, "union": lambda r, n: None,
                    "merge": self.merge, "tell": lambda r, n, key, text: self.notices.append((key, text))}
        self.merge_code = merge_code

    def merge(self, repo, number):
        self.merges.append((repo, number))
        return self.merge_code, ["merged"] if self.merge_code == 0 else ["refused by a gate"]


class TestMergeOnResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = Fixture(self.tmp.name)
        self.part = load_part()

    def run_case(self, hub, view, part=None):
        return (part or self.part)["mor_run"](hub.ctx, "acme/app", 7, view)

    def test_s2_a_clean_git_made_tip_merge_after_the_read_merges_once_and_tells_the_lane(self):
        self.fx.clean_merge()
        hub = Hub(self.fx)
        self.assertEqual(self.run_case(hub, self.fx.view())[0], "merged")
        self.assertEqual(len(hub.merges), 1)
        self.assertEqual(len(hub.notices), 1)
        self.assertIn("was merged", hub.notices[0][1])

    def test_s3_a_hand_resolved_merge_is_not_merged_and_the_lane_is_told_why(self):
        self.fx.hand_merge()
        hub = Hub(self.fx)
        self.assertEqual(self.run_case(hub, self.fx.view())[0], "refused")
        self.assertEqual(len(hub.merges), 0)
        self.assertEqual(len([n for n in hub.notices if "hand resolution" in n[1]]), 1)

    def test_s4_a_code_commit_a_hold_label_and_no_mark_merge_nothing_and_each_names_its_reason(self):
        cases = []
        self.fx.code_commit()
        self.fx.clean_merge()
        cases.append(("code commit", self.fx.view(), "other than a tip merge"))
        clean = Fixture(tempfile.mkdtemp(dir=self.tmp.name))
        clean.clean_merge()
        cases.append(("hold", clean.view(labels=("lane:merge-on-resolve", "lane:hold-owner")), "hold label"))
        cases.append(("no mark", clean.view(labels=()), "not marked"))
        for name, view, reason in cases:
            fx = self.fx if name == "code commit" else clean
            hub = Hub(fx)
            self.run_case(hub, view)
            self.assertEqual(len(hub.merges), 0, name)
            self.assertEqual(len([n for n in hub.notices if reason in n[1]]), 1, name)

    def test_a_reason_is_told_for_each_head_and_the_gates_refuse_too(self):
        self.fx.clean_merge()
        hub = Hub(self.fx, drops="merge of acme/app#7 refused: its head drops 1 tip hunk(s)")
        self.assertEqual(self.run_case(hub, self.fx.view())[0], "refused")
        self.assertEqual(len(hub.merges), 0)
        self.assertIn("drops 1 tip hunk", hub.notices[0][1])
        hub = Hub(self.fx, merge_code=1)
        self.assertEqual(self.run_case(hub, self.fx.view())[0], "refused")
        self.assertIn("refused by a gate", hub.notices[0][1])

    def test_a_changes_requested_read_after_the_approve_merges_nothing(self):
        self.fx.clean_merge()
        hub = Hub(self.fx)
        comments = [{"body": "Review of %s: APPROVE" % self.fx.approved[:12]}, {"body": "Review of %s: CHANGES REQUESTED" % self.fx.approved[:12]}]
        self.assertEqual(self.run_case(hub, self.fx.view(comments=comments))[0], "skip")
        self.assertEqual(hub.merges, [])

    def test_s5_the_control_and_the_mutant_without_the_tree_comparison(self):
        text = PART.read_text()
        old = "if made.splitlines()[0] != tree:"
        self.assertEqual(text.count(old), 1)
        control = load_part(text)   # the control first: the real step refuses the hand resolution and merges the clean one
        mutant = load_part(text.replace(old, "if False:"))
        self.fx.hand_merge()
        hub = Hub(self.fx)
        self.assertEqual(self.run_case(hub, self.fx.view(), control)[0], "refused")
        self.assertEqual(len(hub.merges), 0)
        hub = Hub(self.fx)
        self.assertEqual(self.run_case(hub, self.fx.view(), mutant)[0], "merged", "the mutant without the tree comparison must merge the hand-resolved case")
        self.assertEqual(len(hub.merges), 1)
        clean = Fixture(tempfile.mkdtemp(dir=self.tmp.name))
        clean.clean_merge()
        hub = Hub(clean)
        self.assertEqual(self.run_case(hub, clean.view(), control)[0], "merged")

    def test_the_tick_tells_a_reason_once_per_head_and_keeps_it_across_a_restart(self):
        self.fx.hand_merge()
        hub = Hub(self.fx)
        notes, told = [], []

        class Engine:
            state_dir = self.tmp.name
            mor_last = 0
            clock = [1000.0]

            def now(self):
                return self.clock[0]

            def _note(self, *a):
                notes.append(a)
        view = json.dumps(self.fx.view())
        hub.ctx["gh"] = lambda argv: (0, view, "")
        env = {"repos": lambda: ["acme/app"], "marked": lambda repo: [7], "context": lambda repo, n: dict(hub.ctx)}
        for step in range(3):
            engine = Engine()   # a new engine each time: a restart keeps the told reason in its state file
            engine.clock[0] = 1000.0 + 100 * step
            self.part["merge_on_resolve_tick"](engine, env)
        self.assertEqual(len(hub.notices), 1)
        self.assertEqual(hub.merges, [])


if __name__ == "__main__":
    unittest.main()
