"""The rehearsal brief and the reading of a rehearsal pod's report (#1785): `fleet-switchboard rehearse brief|read`."""
import json
import re
import subprocess
import sys
from pathlib import Path

from support.loader import captured, sb
from support.scenario_roles import FAKE_GH, FAKE_REPO

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "fleet-planning" / "SKILL.md"
TEMPLATE = REPO / "docs" / "plan-template.md"
GUARDS = REPO / "docs" / "guard-classes.json"
HOST = REPO / "bin" / "switchboard" / "agents_verify.py"

BLOCK = """```yaml
rehearsal:
  base: integration
  pins:
    - file: bin/a.py
      old: "x = 1"
      new: "x = 2"
      why: the value
    - file: bin/b.py
      old: "y = 1"
      new: "y = 2"
      why: the other value
    - file: docs/c.md
      old: "z"
      new: "zz"
      why: the doc
  additions:
    - "command: frob"
    - "test class: TestFrob"
  tests:
    - TestAlpha
    - TestBeta
```
"""
PASSING = """## Plan

plan-format: 2
size: S (one thing)

```yaml
clauses:
  DW-1: "the thing works"
```

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given a thing, when it runs, then it works
    proof: test
    pass: count equals 1
    discriminates: a thing that does not work shows count equals 0 and fails
    evaluator: ci
```
"""
FAILING = "## Plan\n\nno format line here\n"
HEADER = "file | line | old text | the test that failed or the line that must change\n"
REPORT = "branch: rehearse/x\n" + HEADER + "%s"
ROWS2 = "bin/a.py | 3 | x = 1 | TestAlpha\nbin/b.py | 9 | y = 1 | TestBeta\n"


class FakeDone:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, ""


def build(HandoffCase):
    class TestPlanRehearsal(HandoffCase):
        """#1785: the brief names every pin, addition and test; the report is read into a table; a pod that pushed is refused."""

        def setUp(self):
            super().setUp()
            self.calls = []
            self.origin = FakeDone(0, "")

        def runner(self, argv, **kwargs):
            self.calls.append(argv)
            if isinstance(self.origin, Exception):
                raise self.origin
            return self.origin

        def file(self, name, text):
            path = self.home / name
            path.write_text(text)
            return str(path)

        def rehearse(self, *argv):
            with captured() as (out, err):
                code = sb.main(["rehearse"] + list(argv), runner=self.runner, now=self.world.clock.now)
            return code, out.getvalue(), err.getvalue()

        def brief(self, block=BLOCK):
            return self.rehearse("brief", self.file("plan.md", PASSING + block))

        def read(self, report, plan=None):
            return self.rehearse("read", self.file("report.md", report), "--plan", self.file("plan2.md", plan or PASSING + BLOCK), "--repo", str(self.home))

        def test_s3_the_brief_names_three_pins_and_two_tests(self):
            code, out, _ = self.brief()
            self.assertEqual(code, 0)
            for item in ("bin/a.py", "bin/b.py", "docs/c.md", "TestAlpha", "TestBeta", "Base: integration"):
                self.assertIn(item, out)
            self.assertEqual(out.count("do NOT commit or push"), 1)
            self.assertEqual(out.count("ignore the wip-note"), 1)

        def test_s3_a_report_with_two_differences_gives_two_rows(self):
            code, out, _ = self.read(REPORT % ROWS2)
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 3)   # the header and 2 rows

        def test_s4_a_branch_on_the_origin_is_refused_with_one_line(self):
            self.origin = FakeDone(0, "abc\trefs/heads/rehearse/x\n")
            code, out, err = self.read(REPORT % ROWS2)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertIn("rehearse/x", err)
            self.assertEqual(self.calls[0][:3], ["git", "ls-remote", "--heads"])

        def test_s4_a_report_with_no_branch_is_refused(self):
            code, out, err = self.read(HEADER + ROWS2)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(len(err.strip().splitlines()), 1)

        def test_s5_an_empty_report_a_short_row_and_an_unreadable_origin_exit_one_line(self):
            cases = [(REPORT % ROWS2, FakeDone(2, "")), (REPORT % ROWS2, OSError("no git")), ("", FakeDone()), (REPORT % "bin/a.py | 3 | x\n", FakeDone())]
            for report, origin in cases:
                self.origin = origin
                code, out, err = self.read(report)
                self.assertEqual((code, out), (1, ""), report)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)

        def test_s9_the_brief_names_the_classes_that_a_command_and_a_test_class_reach(self):
            guards = json.loads(GUARDS.read_text())
            want = {g["class"] for g in guards if {"new command", "new test class"} & set(g["reach"])}
            other = {g["class"] for g in guards} - want
            code, out, _ = self.brief()
            self.assertEqual(code, 0)
            for name in want:
                self.assertIn(name, out)
            for name in other:
                self.assertNotIn(name, out)
            self.assertEqual(out.count("ONE process"), 1 if any(g["one_process"] for g in guards if g["class"] in want) else 0)

        def test_s9_every_class_of_the_guard_list_is_held_by_a_test_file(self):
            text = "".join(p.read_text(errors="replace") for p in (REPO / "tests").rglob("*.py")) + (REPO / "bin" / "test-switchboard").read_text()
            missing = [g["class"] for g in json.loads(GUARDS.read_text()) if not re.search(r"^class %s\b" % g["class"], text, re.M)]
            self.assertEqual(missing, [])

        def test_s7_the_rule_is_the_same_in_the_skill_the_template_and_the_help(self):
            sentences = sb.REHEARSE_RULE_SENTENCES
            self.assertEqual(len(sentences), 5)
            places = {}
            for path in (SKILL, TEMPLATE):
                places[path.name] = sb.rehearse_section_of(path.read_text(), sb.REHEARSE_HEADING)
            with captured() as (out, err):
                try:
                    sb.main(["rehearse", "--help"], runner=self.runner, now=self.world.clock.now)
                except SystemExit:
                    pass
            places["help"] = out.getvalue()
            self.assertEqual(len(places), 3)
            for name, text in places.items():
                self.assertIsNotNone(text, name)
                self.assertEqual(sb.rehearse_sentences_of(text), list(sentences), name)
            drifted = places["SKILL.md"].replace("only for a change", "only for any change")
            self.assertEqual(len(sb.rehearse_sentences_of(drifted)), 4)

        def test_u2_the_section_runs_nothing_but_ls_remote_and_a_row_is_text(self):
            source = HOST.read_text().split("# ---- rehearse (#1785)", 1)[1]
            for word in ("git push", "git commit", '"gh"', "launch", "os.system", "shell=True"):
                self.assertNotIn(word, source)
            self.origin = FakeDone(0, "")
            code, out, _ = self.read(REPORT % "bin/a.py | 3 | rm -rf / | TestAlpha\n")
            self.assertEqual(code, 0)
            self.assertIn("rm -rf /", out)
            self.assertEqual(len(self.calls), 1)

        def base_tree(self):
            """A copy of origin/integration, or a skip when the ref is not in this checkout (a shallow clone)."""
            if subprocess.run(["git", "rev-parse", "--verify", "-q", "origin/integration"], cwd=str(REPO), stdout=subprocess.PIPE).returncode:
                self.skipTest("origin/integration is not in this checkout")
            dest = self.home / "base"
            dest.mkdir()
            archive = subprocess.run(["git", "archive", "origin/integration"], cwd=str(REPO), stdout=subprocess.PIPE, check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
            return dest

        def plan_check(self, root, text):
            done = subprocess.run([sys.executable, str(Path(root) / "bin" / "fleet-switchboard"), "plan", "check", "--file", self.file("q.md", text)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            return done.returncode, done.stdout

        def test_u3_plan_check_gives_the_same_verdict_defects_and_hash_before_and_after(self):
            base = self.base_tree()
            for text in (PASSING, FAILING):
                self.assertEqual(self.plan_check(base, text), self.plan_check(REPO, text))
            for text in (PASSING, FAILING):   # the block itself changes neither the verdict nor the defect lines
                bare, with_block = self.plan_check(REPO, text), self.plan_check(REPO, text + BLOCK)
                self.assertEqual(bare[0], with_block[0])
                keep = lambda out: [l for l in out.splitlines() if "hash" not in l.lower()]
                self.assertEqual(keep(bare[1]), keep(with_block[1]))
            self.assertNotEqual(self.plan_check(REPO, PASSING)[0], self.plan_check(REPO, FAILING)[0])

        def test_s2_both_documents_carry_the_rule_with_its_three_parts(self):
            for path in (SKILL, TEMPLATE):
                text = sb.rehearse_section_of(path.read_text(), sb.REHEARSE_HEADING)
                self.assertIsNotNone(text, path.name)
                folded = " ".join(text.split())
                for part in ("no commit and no push runs before the plan is approved", "the plan then names each row first", "skipped only for a change of documents only", "the approver of the plan judges it"):
                    self.assertIn(part, folded, path.name)
            planted = " ".join(sb.rehearse_section_of(SKILL.read_text(), sb.REHEARSE_HEADING).split()).replace("the approver of the plan judges it", "")
            self.assertNotIn("the approver of the plan judges it", planted)

        def numstat(self):
            if subprocess.run(["git", "rev-parse", "--verify", "-q", "origin/integration"], cwd=str(REPO), stdout=subprocess.PIPE).returncode:
                self.skipTest("origin/integration is not in this checkout")
            out = subprocess.run(["git", "diff", "--numstat", "origin/integration...HEAD"], cwd=str(REPO), stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
            return [l.split("\t") for l in out.splitlines()]

        def test_s2_the_documents_gain_inserted_lines_only(self):
            rows = {r[2]: r for r in self.numstat()}
            for name in ("skills/fleet-planning/SKILL.md", "docs/plan-template.md"):
                if name in rows:
                    self.assertEqual(rows[name][1], "0", name)

        def test_s8_no_existing_test_line_changes_and_no_pin_change_commit(self):
            for added, removed, name in self.numstat():
                if name.startswith("tests/") or name == "bin/test-switchboard":
                    self.assertEqual(removed, "0", name)
            log = subprocess.run(["git", "log", "--format=%s", "origin/integration..HEAD"], cwd=str(REPO), stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
            self.assertNotIn("pin change of #1785", log)

        def test_s9_a_planted_unknown_class_is_the_one_failing_name(self):
            text = "class TestKnown(object):\n    pass\n"
            found = [c for c in ("TestKnown", "TestPlantedUnknown") if not re.search(r"^class %s\b" % c, text, re.M)]
            self.assertEqual(found, ["TestPlantedUnknown"])
    return TestPlanRehearsal
