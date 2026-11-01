"""#1714: `fleet-switchboard comment --edit URL` edits the caller's own comment, signed once; an approved plan needs its approval id.
build(host) takes the host module of bin/test-switchboard (its HandoffCase and helpers) and returns the test classes."""
import json
import re
import subprocess
from unittest import mock


def build(host):
    sb, REPO_NAME, GH = host.sb, host.FAKE_REPO, host.FAKE_GH
    mark = lambda name: "<!-- fleet-switchboard:from=%s -->" % name
    PLAN = "## Plan (scenarios)\n\nsize: S\n\nthe plan text\n\n" + mark("cos")

    class CommentGH:
        """A fake GitHub of comments: one login for every lane; records every write."""

        def __init__(self):
            self.store, self.writes, self.calls = {}, [], []

        def add(self, ident, issue, body, login="shared-account"):
            self.store[ident] = {"id": ident, "issue": issue, "body": body, "created_at": "2027-01-01T00:00:%02dZ" % ident,
                                 "user": {"login": login}, "html_url": "https://github.com/%s/issues/%d#issuecomment-%d" % (REPO_NAME, issue, ident)}

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            path, method = argv[2], (argv[argv.index("--method") + 1] if "--method" in argv else "GET")
            one = re.match(r"^repos/%s/issues/comments/(\d+)$" % re.escape(REPO_NAME), path)
            many = re.match(r"^repos/%s/issues/(\d+)/comments(\?.*)?$" % re.escape(REPO_NAME), path)
            if one and method == "GET":
                return self.answer(argv, self.store.get(int(one.group(1))))
            if one and method == "PATCH":
                self.writes.append(("PATCH", int(one.group(1))))
                self.store[int(one.group(1))]["body"] = json.loads(kwargs["input"])["body"]
                return self.answer(argv, self.store[int(one.group(1))])
            if many and method == "GET":
                return self.answer(argv, [c for c in self.store.values() if c["issue"] == int(many.group(1))])
            if many and method == "POST":
                self.writes.append(("POST", int(many.group(1))))
                ident = max(self.store, default=0) + 1
                self.add(ident, int(many.group(1)), json.loads(kwargs["input"])["body"])
                return self.answer(argv, self.store[ident])
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")

        def answer(self, argv, data):
            if data is None:
                return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    class TestCommentEdit(host.HandoffCase):
        def setUp(self):
            super().setUp()
            self.gh = CommentGH()
            self.world.add_agent("platform", fleet={"role": "coder"})
            self.plan_hash = sb.plan_check.plan_hash(PLAN)

        def url(self, ident, issue=7):
            return "https://github.com/%s/issues/%d#issuecomment-%d" % (REPO_NAME, issue, ident)

        def edit(self, ident, text="new text\n", *extra, issue=7):
            path = self.home / "edit.md"
            path.write_text(text)
            with host.captured() as (out, err):
                code = sb.main(["comment", "--edit", self.url(ident, issue), "--body-file", str(path)] + list(extra), runner=self.route,
                               now=self.world.clock.now, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def approve_plan(self, ident=2, issue=7, body="Approved at %s", signer="chief"):
            self.gh.add(ident, issue, (body % self.plan_hash) + "\n\n" + mark(signer))

        def test_s2_s5_an_own_comment_is_edited_in_place_signed_once_and_read_back(self):
            self.gh.add(1, 7, "old\n\n" + mark("cos"))
            code, out, err = self.edit(1, "new text\n")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.gh.writes, [("PATCH", 1)])
            self.assertEqual(self.gh.store[1]["body"], "new text\n\n" + mark("cos"))
            self.assertEqual(json.loads(out)["comment"], self.url(1))
            self.assertEqual(self.gh.store[1]["body"].count("fleet-switchboard:from="), 1)

        def test_s5_a_text_that_holds_the_marker_still_ends_with_it_once(self):
            self.gh.add(1, 7, "old\n\n" + mark("cos"))
            code, out, err = self.edit(1, "line\n\n%s\n" % mark("cos"))
            self.assertEqual(code, 0, err)
            self.assertEqual(self.gh.store[1]["body"], "line\n\n" + mark("cos"))

        def test_s3_u1_another_lanes_or_an_unsigned_comment_is_refused_with_one_line_and_no_write(self):
            self.gh.add(1, 7, "theirs\n\n" + mark("platform"))
            self.gh.add(2, 7, "nobody's")
            for ident in (1, 2):
                code, out, err = self.edit(ident)
                self.assertNotEqual(code, 0)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertIn("is not yours", err)
            self.assertEqual(self.gh.writes, [])
            self.assertEqual({c["user"]["login"] for c in self.gh.store.values()}, {"shared-account"})
            self.gh.add(3, 7, "mine\n\n" + mark("cos"))
            self.assertEqual(self.edit(3)[0], 0)

        def test_s4_an_approved_plan_needs_the_approval_id_and_says_it_lapses(self):
            self.gh.add(1, 7, PLAN)
            self.approve_plan(2)
            code, out, err = self.edit(1)
            self.assertNotEqual(code, 0)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("approval 2", err)
            self.assertEqual(self.gh.writes, [])
            code, out, err = self.edit(1, "new plan\n", "--approval", "9")
            self.assertNotEqual(code, 0)
            self.assertEqual(self.gh.writes, [])
            code, out, err = self.edit(1, "## Plan (scenarios)\n\nchanged\n", "--approval", "2")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.gh.writes, [("PATCH", 1)])
            self.assertEqual(err.count("approval 2 lapses with the new plan hash"), 1, err)

        def test_u2_an_id_that_does_not_show_the_approval_of_the_current_hash_is_refused(self):
            self.gh.add(1, 7, PLAN)
            self.approve_plan(2)
            self.gh.add(3, 7, "Approved at %s\n\n%s" % (sb.plan_check.plan_hash("an older plan"), mark("chief")))
            self.gh.add(4, 7, "not approved at %s\n\n%s" % (self.plan_hash, mark("chief")))
            self.gh.add(5, 8, "Approved at %s\n\n%s" % (self.plan_hash, mark("chief")))
            for ident in ("3", "4", "5"):
                code, out, err = self.edit(1, "x\n", "--approval", ident)
                self.assertNotEqual(code, 0, ident)
            self.assertEqual(self.gh.writes, [])

        def test_s6_a_comment_that_is_not_a_plan_needs_no_approval_and_an_unapproved_plan_edits(self):
            self.gh.add(1, 7, "a receipt\n\n" + mark("cos"))
            self.approve_plan(2)
            self.assertEqual(self.edit(1)[0], 0)
            self.gh.add(3, 8, PLAN)
            self.assertEqual(self.edit(3, "x\n", issue=8)[0], 0)
            self.assertEqual(self.gh.writes, [("PATCH", 1), ("PATCH", 3)])

        def check_other_lanes_comment_is_refused(self):
            self.gh.add(1, 7, "theirs\n\n" + mark("platform"))
            code, out, err = self.edit(1)
            if self.gh.writes or code == 0:
                raise AssertionError("own-comment check: another lane's comment was written (%r)" % (self.gh.writes,))

        def test_s6_the_control_of_the_mutant_passes(self):
            self.check_other_lanes_comment_is_refused()

        def test_s6_mutant_a_command_that_skips_the_own_comment_check_fails_with_the_check_named(self):
            import comment_edit   # the glue has put bin/ on sys.path once an edit ran; the control above ran none, so make sure
            with mock.patch.object(comment_edit, "marked_name", lambda body: "cos"):
                with self.assertRaisesRegex(AssertionError, "own-comment check"):
                    self.check_other_lanes_comment_is_refused()

        def test_u5_comment_without_edit_still_posts_a_new_signed_comment(self):
            path = self.home / "c.md"
            path.write_text("hello")
            with host.captured() as (out, err):
                code = sb.main(["comment", "--issue", "2", "--body-file", str(path)], runner=self.route, now=self.world.clock.now,
                               socket_call=self.world.herdr.socket)
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(self.gh.writes, [("POST", 2)])
            self.assertEqual(self.gh.store[1]["body"], "hello\n\n" + mark("cos"))

    return TestCommentEdit
