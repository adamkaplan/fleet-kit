"""Ask #2475: the gh shim allows ONE more narrow DELETE, repos/O/R/issues/N/sub_issue with the one field sub_issue_id=NUMBER (GitHub counts closed sub-issues toward its
100 limit, so a lane must be able to unlink one). S2: the exact form passes. S3: every near-miss is refused. S4: everything else on sub_issue(s) stays as it was.
U1: the controls are mutants of the CURRENT source. Fakes only."""
import unittest

from test_gh_shim_body import BodyRig
from test_gh_shim_hook import mutant, source

PATH = "repos/o/r/issues/12/sub_issue"
FIELD = ["-F", "sub_issue_id=34"]


def delete(path=PATH, *more):
    return ["api", "-X", "DELETE", path, *more]


class UnlinkBase(unittest.TestCase):
    def rig(self, **kw):
        r = BodyRig(**kw)
        self.addCleanup(r.close)
        return r

    def passes(self, rig, args):
        done, calls, _ = rig.go(args)
        return done, calls


class TestGhShimUnlink(UnlinkBase):
    def setUp(self):
        self.r = self.rig()

    def test_s2_the_exact_form_passes_to_the_real_gh_unchanged(self):
        for label, args in (("-F", delete(PATH, *FIELD)), ("-f", delete(PATH, "-f", "sub_issue_id=34")), ("--field", delete(PATH, "--field", "sub_issue_id=34")),
                            ("leading slash", delete("/" + PATH, *FIELD)), ("api host", delete("https://api.github.com/" + PATH, *FIELD)),
                            ("method after path", ["api", PATH, "-X", "DELETE", *FIELD])):
            with self.subTest(label):
                done, calls = self.passes(self.r, args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual([c["argv"] for c in calls], [args])

    def test_s3_near_misses_are_refused(self):
        bad = {"non-numeric number": delete("repos/o/r/issues/abc/sub_issue", *FIELD), "empty number": delete("repos/o/r/issues//sub_issue", *FIELD),
               "number with plus": delete("repos/o/r/issues/1+2/sub_issue", *FIELD),
               "extra segment": delete(PATH + "/1", *FIELD), "extra leading segment": delete("repos/o/r/x/issues/12/sub_issue", *FIELD),
               "dot segment": delete("repos/o/r/issues/12/./sub_issue", *FIELD), "dotdot": delete("repos/o/r/issues/13/../12/sub_issue", *FIELD),
               "encoded segment": delete("repos/o/r/issues/12/sub%5Fissue", *FIELD), "encoded number": delete("repos/o/r/issues/%31%32/sub_issue", *FIELD),
               "double slash": delete("repos/o/r//issues/12/sub_issue", *FIELD), "trailing slash": delete(PATH + "/", *FIELD),
               "other host": delete("https://example.invalid/" + PATH, *FIELD), "other host //": delete("//example.invalid/" + PATH, *FIELD),
               "query string": delete(PATH + "?x=1", *FIELD), "no field": delete(PATH), "wrong field": delete(PATH, "-F", "sub_issue=34"),
               "non-numeric id": delete(PATH, "-F", "sub_issue_id=abc"), "id from file": delete(PATH, "-F", "sub_issue_id=@/etc/passwd"),
               "extra field": delete(PATH, *FIELD, "-f", "x=y"), "two ids": delete(PATH, *FIELD, *FIELD), "extra body": delete(PATH, *FIELD, "-f", "body=hello"),
               "--input": delete(PATH, "--input", "-"), "field and --input": delete(PATH, *FIELD, "--input", "-"),
               "override header": delete(PATH, *FIELD, "-H", "X-HTTP-Method-Override: PUT")}
        for label, args in bad.items():
            with self.subTest(label):
                done, calls = self.passes(self.r, args)
                self.assertEqual(calls, [], "passed: %s" % label)
                self.assertNotEqual(done.returncode, 0)

    def test_s4_everything_else_stays_as_before(self):
        other = {"POST on the path": ["api", "-X", "POST", PATH, *FIELD], "PATCH on the path": ["api", "-X", "PATCH", PATH, *FIELD],
                 "PUT on the path": ["api", "-X", "PUT", PATH, *FIELD], "field without method (POST)": ["api", PATH, *FIELD],
                 "DELETE of sub_issues": delete("repos/o/r/issues/12/sub_issues", *FIELD), "DELETE of the issue": delete("repos/o/r/issues/12"),
                 "DELETE of the parent": delete("repos/o/r/issues/12/parent", *FIELD),
                 "DELETE of sub_issue_priority": delete("repos/o/r/issues/12/sub_issues/priority", *FIELD),
                 "DELETE of a pull": delete("repos/o/r/pulls/12/sub_issue", *FIELD), "DELETE of repositories/ID": delete("repositories/1/issues/12/sub_issue", *FIELD)}
        for label, args in other.items():
            with self.subTest(label):
                done, calls = self.passes(self.r, args)
                self.assertEqual(calls, [], "passed: %s" % label)
        done, calls = self.passes(self.r, ["api", "-X", "POST", "repos/o/r/issues/12/sub_issues", "-F", "sub_issue_id=34"])
        self.assertEqual(len(calls), 1, "the existing POST sub_issues row still passes")
        done, calls, seen = self.r.go(["api", PATH])
        self.assertEqual(done.returncode, 0, done.stderr)


class TestGhShimUnlinkControls(UnlinkBase):
    ROW = '("sub-issue-unlink", r"issues/[0-9]+/sub_issue", ("DELETE",))'

    def test_without_the_row_the_exact_form_is_refused(self):
        r = self.rig(shim_source=mutant("gh", self.ROW, '("sub-issue-unlink", r"issues/never-matches", ("DELETE",))'))
        done, calls = self.passes(r, delete(PATH, *FIELD))
        self.assertEqual(calls, [])
        self.assertIn("REST_WRITES", done.stderr)

    def test_with_a_wider_number_a_non_numeric_number_would_pass(self):
        r = self.rig(shim_source=mutant("gh", self.ROW, '("sub-issue-unlink", r"issues/[^/]+/sub_issue", ("DELETE",))'))
        done, calls = self.passes(r, delete("repos/o/r/issues/abc/sub_issue", *FIELD))
        self.assertEqual(len(calls), 1, "control: the numeric number is what refuses it")

    def test_with_more_methods_a_post_would_pass(self):
        r = self.rig(shim_source=mutant("gh", self.ROW, '("sub-issue-unlink", r"issues/[0-9]+/sub_issue", ("DELETE", "POST"))'))
        done, calls = self.passes(r, ["api", "-X", "POST", PATH, *FIELD])
        self.assertEqual(len(calls), 1, "control: the method list is what refuses it")

    def test_without_the_field_check_an_extra_field_would_pass(self):
        old = 'len(fields) != 1 or not re.fullmatch(r"sub_issue_id=[0-9]+", fields[0])'
        r = self.rig(shim_source=mutant("gh", old, "False"))
        for args in (delete(PATH, *FIELD, "-f", "x=y"), delete(PATH, "-F", "sub_issue_id=abc"), delete(PATH)):
            done, calls = self.passes(r, args)
            self.assertEqual(len(calls), 1, "control: the field check is what refuses %s" % args)

    def test_without_the_input_and_query_checks_they_would_pass(self):
        r = self.rig(shim_source=mutant("gh", 'if found[0] == "sub-issue-unlink" and (has_input or "?" in raw or ', 'if found[0] == "sub-issue-unlink" and ('))
        done, calls = self.passes(r, delete(PATH + "?x=1", *FIELD))
        self.assertEqual(len(calls), 1, "control: the query check is what refuses it")

    def test_the_mutated_sources_differ_from_the_real_one(self):
        self.assertIn(self.ROW, source("gh"))
