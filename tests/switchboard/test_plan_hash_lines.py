"""A plan hash tells apart two plan texts that parse differently (#1834): S2, S3, S4, S5, S6, S8 and S9 of the plan.

Pure: it runs the real functions of bin/plan_check.py and bin/plan_ledger.py on fixture files and writes nothing outside a temporary directory.
A mutant is applied to a COPY of the source, asserted applied once, and run beside its control. No test here measures a pull request's size."""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_PL_REPO = Path(__file__).resolve().parents[2]
_PL_BIN = _PL_REPO / "bin"
_PL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plan_hash_lines"
if str(_PL_BIN) not in sys.path:
    sys.path.insert(0, str(_PL_BIN))

_PL_DONE_WHEN = "(1) an owner can commission an agent; (2) it answers a chat message."
_PL_MARKER = "<!-- fleet-switchboard:from=fleet-registry -->"


def _pl_load(name, path=None, source=None, as_name=None):
    spec = importlib.util.spec_from_file_location(as_name or name, str(path or _PL_BIN / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    if source is None:
        spec.loader.exec_module(module)
    else:
        exec(compile(source, str(path or name), "exec"), module.__dict__)
    return module


def _pl_real(name):
    return _pl_load(name, as_name="t1834_" + name)


def _pl_mutate(name, old, new):
    """The module `name` built from a source copy with `old` replaced by `new`; asserts the mutation applied once."""
    source = (_PL_BIN / (name + ".py")).read_text(encoding="utf-8")
    assert source.count(old) == 1, "the mutation target is not found exactly once: %r" % old
    mutated = source.replace(old, new)
    assert mutated != source, "the mutation was not applied"
    return _pl_load(name, source=mutated, as_name="t1834m_" + name)


def _pl_fixture(name):
    return (_PL_FIXTURES / name).read_bytes().decode("utf-8")


def _pl_read(pc, text):
    """What `plan check` says of a text: (result, scenario count) or ("unreadable", 0)."""
    try:
        got = pc.check(text, _PL_DONE_WHEN)
    except pc.PlanFormatError:
        return ("unreadable", 0)
    return (got["result"], got["scenarios"])


class TestPlanHashLinesPair(unittest.TestCase):
    """S2: the plan as written and the plan with one lost newline have two hashes and two results."""

    def setUp(self):
        self.good = _pl_fixture("plan_14.md")
        self.lost = _pl_fixture("plan_14_lost_newline.md")

    def check(self, pc):
        """The one check, run on the control and on the mutant."""
        a, b = _pl_read(pc, self.good), _pl_read(pc, self.lost)
        assert a == ("pass", 14), "the plan as written does not pass with 14 scenarios: %r" % (a,)
        assert b[0] == "fail", "the lost-newline text does not fail: %r" % (b,)
        assert pc.plan_hash(self.good) != pc.plan_hash(self.lost), "the two texts share one hash"

    def test_the_two_texts_differ_in_hash_and_in_result(self):
        pc = _pl_real("plan_check")
        self.assertEqual(self.lost.replace("  - id: U1    covers", "  - id: U1\n    covers"), self.good)
        self.assertEqual(_pl_read(pc, self.good), ("pass", 14))
        self.assertEqual(_pl_read(pc, self.lost)[0], "fail")
        self.assertEqual(len({pc.plan_hash(self.good), pc.plan_hash(self.lost)}), 2)

    def test_the_control_one_text_read_twice_gives_one_hash(self):
        pc = _pl_real("plan_check")
        self.assertEqual(len({pc.plan_hash(self.good), pc.plan_hash(self.good)}), 1)
        self.check(pc)

    def test_the_older_collapsed_form_gave_one_hash_to_the_two_texts(self):
        pc = _pl_real("plan_check")
        self.assertEqual(pc.plan_hash_collapsed(self.good), pc.plan_hash_collapsed(self.lost))

    def test_a_mutant_that_collapses_newlines_again_fails_with_its_anchored_text(self):
        pc = _pl_mutate("plan_check", '    return "sha256:" + hashlib.sha256("\\n".join(lines).encode("utf-8")).hexdigest()\n',
                        '    return "sha256:" + hashlib.sha256(" ".join("\\n".join(lines).split()).encode("utf-8")).hexdigest()\n')
        with self.assertRaisesRegex(AssertionError, "the two texts share one hash"):
            self.check(pc)


class TestPlanHashLinesWhitespace(unittest.TestCase):
    """S3: four ignored forms are one hash; four changed forms are four other hashes."""

    def setUp(self):
        self.pc = _pl_real("plan_check")
        self.base = _pl_fixture("plan_14.md")

    def check(self, pc):
        """The one check (S3), run on the control and on the mutant."""
        lines = self.base.split("\n")
        forms = [self.base,
                 "\n".join(l + ("  \t" if l.strip() else "") for l in lines),
                 self.base.replace("\n", "\r\n"),
                 "\n\n\n" + self.base + "\n\n"]
        self.assertEqual(len({pc.plan_hash(f) for f in forms}), 1, "the four ignored forms give 1 hash")

    def test_the_four_ignored_forms_give_one_hash(self):
        self.check(self.pc)

    def test_the_four_changed_forms_give_four_other_hashes(self):
        base = self.base
        forms = [base.replace("    proof: test\n", "\n    proof: test\n", 1),
                 base.replace("  - id: S1", "   - id: S1", 1),
                 base.replace("\n", "\r", 1),
                 base.replace("when an owner", "when  an owner", 1)]
        for f in forms:
            self.assertNotEqual(f, base)
        got = {self.pc.plan_hash(f) for f in forms}
        self.assertEqual(len(got), 4)
        self.assertNotIn(self.pc.plan_hash(base), got)

    def test_a_rule_that_keeps_all_whitespace_is_not_this_rule(self):
        pc = _pl_mutate("plan_check", '    lines = [l.rstrip(" \\t") for l in text.replace("\\r\\n", "\\n").split("\\n")]\n',
                        '    lines = text.split("\\n")\n')
        self.check(self.pc)
        with self.assertRaisesRegex(AssertionError, "the four ignored forms give 1 hash"):
            self.check(pc)


class TestPlanHashLinesVariants(unittest.TestCase):
    """S4: no single lost newline of the scenarios block changes the result or the count and keeps the hash."""

    FIXTURES = ("plan_14.md", "plan_6.md", "plan_3.md")

    def variants(self, text):
        start = text.index("scenarios:\n")
        end = text.index("```\n", start)
        for i in range(start, end):
            if text[i] == "\n":
                yield text[:i] + text[i + 1:]

    def test_no_variant_that_reads_differently_keeps_the_hash_of_the_original(self):
        pc = _pl_real("plan_check")
        for name in self.FIXTURES:
            text = _pl_fixture(name)
            original = (_pl_read(pc, text), pc.plan_hash(text))
            different = kept = 0
            for variant in self.variants(text):
                if _pl_read(pc, variant) != original[0]:
                    different += 1
                    kept += pc.plan_hash(variant) == original[1]
            self.assertGreater(different, 0, name)
            self.assertEqual(kept, 0, name)

    def test_the_older_collapsed_form_keeps_the_hash_of_such_variants(self):
        pc = _pl_real("plan_check")
        for name in self.FIXTURES:
            text = _pl_fixture(name)
            reads = _pl_read(pc, text)
            kept = sum(_pl_read(pc, v) != reads and pc.plan_hash_collapsed(v) == pc.plan_hash_collapsed(text) for v in self.variants(text))
            self.assertGreaterEqual(kept, 1, name)


class TestPlanHashLinesBindings(unittest.TestCase):
    """S5: bindings of five kinds at three hashes read as bound; those of an edited text read as stale."""

    KINDS = ("approval", "proof", "verdict", "consensus", "ask")

    def setUp(self):
        self.pc = _pl_real("plan_check")
        self.text = _pl_fixture("plan_6.md") + "\n" + _PL_MARKER + "\n"
        self.hashes = self.pc.plan_hashes(self.text)

    def entries(self, pl, plan_hash):
        return [{"kind": k, "plan_hash": plan_hash, "head": pl.ask_head(plan_hash)} for k in self.KINDS]

    def bound(self, pl):
        n = 0
        for value in self.hashes:
            for e in pl.as_current(self.entries(pl, value), self.hashes):
                n += e["plan_hash"] == self.hashes[0] and e["head"] == pl.ask_head(self.hashes[0])
        return n

    def stale(self, pl):
        edited = self.pc.plan_hashes(self.text.replace("agents, when", "agents, if", 1))
        return sum(e["plan_hash"] != self.hashes[0] for v in edited for e in pl.as_current(self.entries(pl, v), self.hashes))

    def check(self, pl):
        assert self.bound(pl) == 15, "bound %d of 15" % self.bound(pl)

    def test_fifteen_of_fifteen_bound_and_five_of_each_edited_form_stale(self):
        pl = _pl_real("plan_ledger")
        self.assertEqual(len(self.hashes), 3)
        self.assertEqual(self.bound(pl), 15)
        self.assertEqual(self.stale(pl), 5 * 3)

    def test_the_control_a_binding_at_the_new_hash_is_bound(self):
        pl = _pl_real("plan_ledger")
        out = pl.as_current(self.entries(pl, self.hashes[0]), self.hashes)
        self.assertEqual([e["plan_hash"] for e in out], [self.hashes[0]] * 5)
        self.check(pl)

    def test_canonical_hash_maps_each_member_to_the_new_hash(self):
        self.assertEqual([self.pc.canonical_hash(h, self.hashes) for h in self.hashes], [self.hashes[0]] * 3)

    def test_a_mutant_that_reads_the_new_hash_only_fails_with_its_anchored_text(self):
        pl = _pl_mutate("plan_ledger", "    new, olds = hashes[0], set(hashes[1:]) - {hashes[0]}\n", "    new, olds = hashes[0], set()\n")
        with self.assertRaisesRegex(AssertionError, "bound 5 of 15"):
            self.check(pl)


class TestPlanHashLinesPairOrder(unittest.TestCase):
    """S9: the order and the duplicates of plan_hashes; canonical_hash; the ledger is not rewritten."""

    def setUp(self):
        self.pc, self.pl = _pl_real("plan_check"), _pl_real("plan_ledger")

    def test_a_marker_text_has_three_members_in_order(self):
        text = _pl_fixture("plan_3.md") + "\n" + _PL_MARKER + "\n"
        got = self.pc.plan_hashes(text)
        self.assertEqual(got, (self.pc.plan_hash(text), self.pc.plan_hash_collapsed(text), self.pc.plan_hash_legacy(text)))
        self.assertEqual(len(set(got)), 3)

    def test_a_single_line_plain_text_has_one_member(self):
        self.assertEqual(len(self.pc.plan_hashes("just one line")), 1)

    def test_a_plain_text_of_several_lines_has_two_members(self):
        text = _pl_fixture("plan_3.md")
        got = self.pc.plan_hashes(text)
        self.assertEqual(got, (self.pc.plan_hash(text), self.pc.plan_hash_collapsed(text)))

    def test_canonical_hash_maps_members_and_leaves_another_value(self):
        text = _pl_fixture("plan_3.md") + "\n" + _PL_MARKER + "\n"
        got = self.pc.plan_hashes(text)
        self.assertEqual([self.pc.canonical_hash(h, got) for h in got], [got[0]] * 3)
        other = "sha256:" + "0" * 64
        self.assertEqual(self.pc.canonical_hash(other, got), other)

    def test_the_ledger_on_disk_is_the_same_after_the_read(self):
        text = _pl_fixture("plan_3.md") + "\n" + _PL_MARKER + "\n"
        got = self.pc.plan_hashes(text)
        entries = [{"kind": "proof", "plan_hash": h, "head": self.pl.ask_head(h)} for h in got]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries), encoding="utf-8")
            before = path.read_bytes()
            read = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            snapshot = copy.deepcopy(read)
            out = self.pl.as_current(read, got)
            self.assertEqual(read, snapshot)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual({e["plan_hash"] for e in out}, {got[0]})


class TestPlanHashLinesDocs(unittest.TestCase):
    """S8: one section of each document names the three ignored forms and says that anything else changes the hash."""

    def section(self, relative, marker):
        text = (_PL_REPO / "docs" / relative).read_text(encoding="utf-8")
        self.assertEqual(text.count(marker), 1, relative)
        return text.split(marker, 1)[1].split("\n\n", 1)[1 if marker.startswith("####") else 0].split("\n#", 1)[0].lower()

    def test_each_document_names_the_three_forms_and_nothing_else(self):
        for relative, marker in (("plan-template.md", "#### What the plan hash ignores (#1834)"), ("switchboard.md", "**What the plan hash ignores (#1834).**")):
            s = self.section(relative, marker)
            for phrase in ("spaces and tabs at the end of a line", "carriage return of a crlf pair", "blank lines at the start and at the end", "anything else changes"):
                self.assertIn(phrase, s, relative)


class TestPlanHashLinesCloudCopy(unittest.TestCase):
    """S6: the pod's copy of plan_check.py is the file of bin/."""

    def test_the_two_files_are_identical(self):
        self.assertEqual((_PL_REPO / "cloud" / "worker" / "plan_check.py").read_bytes(), (_PL_BIN / "plan_check.py").read_bytes())
