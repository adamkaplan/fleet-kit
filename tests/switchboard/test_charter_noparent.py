"""#1933: the strict read of a parent trusts no wording of a 404 (scenarios S1 to S4 of the plan). The real IntentReader.parent / issue over a fake _api.

S5 (the removed lines of the diff against the integration tip) is left to the reader: no test of this repository counts the diff of its own branch.
build() is called by bin/test-switchboard, which holds Base, CharterRig and TestCharterSetGuard."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb, _load
from support.paths import BIN

REPO = "acme-org/widget-api"
ANSWERS = (   # S1: every text a 404 of the parent endpoint of an existing issue can carry
    "not_found: gh: No parent issue found (HTTP 404)",
    "not_found: gh: Not Found (HTTP 404)",
    "HTTP 404 repository moved or renamed",
    "gh: Something nobody has said yet (HTTP 404)",
    "",
)
ISSUE = {"number": 3, "state": "open", "title": "t", "body": "", "labels": []}


def reader(sbm, answers, paths):
    """A real reader whose _api answers by path: `answers` maps "issue" / "parent" to a dict or an exception."""
    def api(self, path, repo=None, meta=None):
        paths.append(path)
        answer = answers["parent" if path.endswith("/parent") else "issue"]
        if isinstance(answer, Exception):
            raise answer
        return answer
    patches = [mock.patch.object(sbm.IntentReader, "_api", api), mock.patch.object(sbm, "state_dir_path", lambda: Path(tempfile.mkdtemp()))]
    for patch in patches:
        patch.start()
    return sbm.IntentReader("gh", REPO), lambda: [p.stop() for p in patches]


def read(sbm, answers):
    paths = []
    rd, stop = reader(sbm, answers, paths)
    try:
        try:
            return rd.parent(3, REPO, strict=True), paths, None
        except sbm.IntentError as err:
            return None, paths, err
    finally:
        stop()


def check_s1(sbm):
    """Returns the number of the five answers that read as no parent."""
    ok = 0
    for text in ANSWERS:
        got, paths, err = read(sbm, {"issue": ISSUE, "parent": sbm.IntentError("not_found", text)})
        if err is None and got is None:
            ok += 1
    assert ok == len(ANSWERS), "a 404 of any text is no parent: %d of %d" % (ok, len(ANSWERS))


def check_s2(sbm):
    for name, answers in (("issue 404", {"issue": sbm.IntentError("not_found", "HTTP 404"), "parent": sbm.IntentError("not_found", "x")}),
                          ("issue 403", {"issue": sbm.IntentError("gh", "HTTP 403"), "parent": sbm.IntentError("not_found", "x")})):
        got, paths, err = read(sbm, answers)
        assert err is not None and not [p for p in paths if p.endswith("/parent")], "the missing issue is refused before the parent is asked (%s: %r, %r)" % (name, err, paths)
    for name, parent in (("parent 5xx", sbm.IntentError("gh", "HTTP 502")), ("parent 403", sbm.IntentError("gh", "HTTP 403"))):
        got, paths, err = read(sbm, {"issue": ISSUE, "parent": parent})
        assert err is not None, "%s raises" % name
    got, paths, err = read(sbm, {"issue": ISSUE, "parent": {"number": 1}})
    assert err is None and got == 1, "an issue that has a parent returns it"


def check_fresh(sbm):
    """An earlier issue() read left a cache entry that would answer "exists"; the issue now answers 404."""
    answers = {"issue": ISSUE, "parent": sbm.IntentError("not_found", "gh: No parent issue found (HTTP 404)")}
    paths = []
    rd, stop = reader(sbm, answers, paths)
    try:
        rd.issue(3, REPO)
        answers["issue"] = sbm.IntentError("not_found", "HTTP 404")
        del paths[:]
        try:
            rd.parent(3, REPO, strict=True)
            err = None
        except sbm.IntentError as exc:
            err = exc
    finally:
        stop()
    assert err is not None and err.reason == "not_found" and not [p for p in paths if p.endswith("/parent")], "the issue read of the strict parent read is fresh (%r, %r)" % (err, paths)


ENTRY_OLD = '''                if err.reason == "not_found":
                    return None
                raise
            return IssueIntent.from_api(found).number'''
ENTRY_SHAPE = '''                if err.reason == "not_found":
                    if strict and not re.compile(r"(not_found: )?(gh: )?(not found|http 404 no parent)( \\(http 404\\))?", re.I).fullmatch(str(err).strip()):
                        raise IntentError("gh", "404 that is not the no-parent answer: %s" % _one_line(str(err), 60)) from None
                    return None
                raise
            return IssueIntent.from_api(found).number'''
INSERTED = '''            self.invalidate(number, repo)
            self.issue(number, repo)   # raises for a missing or unreadable issue: the parent endpoint is not asked
'''


def mutant(tmp, name, old, new):
    """A copy of the entry with ONE textual replacement, asserted applied; returns the loaded module."""
    directory = Path(tmp) / name
    directory.mkdir()
    shutil.copytree(str(BIN), str(directory), ignore=shutil.ignore_patterns("__pycache__", "test-*"), dirs_exist_ok=True)   # the entry loads its sibling modules from its own directory
    entry = directory / "fleet-switchboard"
    text = entry.read_text()
    assert text.count(old) == 1, "the mutation of %s does not apply once: %d" % (name, text.count(old))
    entry.write_text(text.replace(old, new))
    return _load("t_mutant_" + name, entry)


class TestCharterNoParent(unittest.TestCase):
    def test_s1_a_404_of_any_text_is_no_parent_for_an_issue_that_exists(self):
        check_s1(sb)

    def test_s2_the_missing_issue_and_every_other_failure_is_refused(self):
        check_s2(sb)

    def test_the_issue_read_of_the_strict_parent_read_is_fresh(self):
        check_fresh(sb)

    def test_s4_the_control_passes_and_each_mutant_fails_its_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = mutant(tmp, "control", ENTRY_OLD, ENTRY_OLD)   # control first: the same text, unmutated
            check_s1(control)
            check_s2(control)
            shape = mutant(tmp, "shape", ENTRY_OLD, ENTRY_SHAPE)
            with self.assertRaises(AssertionError, msg="the restored shape check must fail the S1 test") as got:
                check_s1(shape)
            self.assertIn("a 404 of any text is no parent", str(got.exception))
            check_s2(shape)
            lines = mutant(tmp, "lines", INSERTED, "")
            with self.assertRaises(AssertionError, msg="the removed issue read must fail the S2 test") as got:
                check_s2(lines)
            self.assertIn("the missing issue is refused before the parent is asked", str(got.exception))
            check_s1(lines)
            check_fresh(control)
            with self.assertRaises(AssertionError, msg="the issue read of the strict parent read is fresh") as got:
                check_fresh(lines)
            self.assertIn("the issue read of the strict parent read is fresh", str(got.exception))
            noinval = mutant(tmp, "noinval", "            self.invalidate(number, repo)\n", "")
            check_fresh(control)
            with self.assertRaises(AssertionError, msg="the issue read of the strict parent read is fresh") as got:
                check_fresh(noinval)
            self.assertIn("the issue read of the strict parent read is fresh", str(got.exception))


def build(CharterRig, Base, TestCharterSetGuard):
    class TestCharterNoParentSet(CharterRig, Base):
        """S2 (the issue with a parent) and S3 through the real `charter set` and a fake hub."""
        real_set = TestCharterSetGuard.real_set
        NEVER_SAID = sb.IntentError("not_found", "gh: Something nobody has said yet (HTTP 404)")

        def label(self):
            return {"number": 3, "labels": [{"name": sb.orchestrator_label()}]}

        def test_s2_the_issue_with_a_parent_is_an_ask(self):
            (code, out, err), paths = self.real_set({"number": 1}, self.label())
            self.assertEqual(code, 2, err)
            self.assertIn("is an ask, not a charter", err)

        def test_s3_an_unheard_wording_is_recorded_and_a_missing_issue_is_refused(self):
            (code, out, err), paths = self.real_set(self.NEVER_SAID, self.label())
            self.assertEqual(code, 0, err)
            self.assertEqual(self.row("orch-b")["charter"], 3)
            self.registry.upsert(self.world.state, "orch-b", dict(self.row("orch-b"), charter=None))
            (code, out, err), paths = self.real_set(self.NEVER_SAID, sb.IntentError("not_found", "HTTP 404"))
            self.assertEqual(code, 2, err)
            self.assertIsNone(self.row("orch-b")["charter"])
    return TestCharterNoParentSet
