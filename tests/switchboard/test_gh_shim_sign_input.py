"""#1696: `gh api --input` bodies are signed or refused, graphql body mutations are refused, two review-comment endpoints are signed.

Reuses the Rig of test_gh_shim_sign.py. The completeness check here counts 21 forms: 5 SIGNED_COMMANDS + 2 CLOSE_COMMANDS + 6 SIGNED_API + 8 graphql names.
"""
import ast
import json
import os
import re
import stat
import unittest

from test_gh_shim_sign import (API_EXAMPLES, MARK, OLD_EXAMPLE, REPO, SHIM_DIR, Rig, forms_without_an_example as old_forms_without_an_example, pairs)

MUTATIONS = ("addComment", "addPullRequestReview", "addPullRequestReviewComment", "addPullRequestReviewThread", "addPullRequestReviewThreadReply",
             "updateIssueComment", "updatePullRequestReview", "updatePullRequestReviewComment")
NEW_API_EXAMPLES = {"pr-review-comment": "repos/o/r/pulls/comments/5", "pr-review-reply": "repos/o/r/pulls/1/comments/5/replies"}
ALL_API = dict(API_EXAMPLES, **NEW_API_EXAMPLES)
SIGNED = MARK % "alice-bot"
SPELLINGS = ("/graphql", "graphql/", "graphql?x=1", "https://api.github.com/graphql", "GraphQL")
URL_COMMENTS = "https://api.github.com/repos/o/r/issues/1/comments"


def names_in(source, name):
    """Every string key of the dict (or element of the set) assigned to `name` at module level."""
    found = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and re.fullmatch(r"[a-z]+[A-Za-z]+", sub.value) and sub.value[0].islower():
                    found.append(sub.value)
            if isinstance(node.value, ast.Dict):
                found = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    return found


def forms_missing(source):
    missing = []
    for label, _pattern in pairs(source, "SIGNED_API"):
        if label not in ALL_API:
            missing.append(("api", label))
    for group, verb in pairs(source, "SIGNED_COMMANDS"):
        if group not in OLD_EXAMPLE:
            missing.append((group, verb))
    for group, verb in pairs(source, "CLOSE_COMMANDS"):
        if group not in OLD_EXAMPLE or verb != "close":
            missing.append((group, verb))
    for name in names_in(source, "GRAPHQL_BODY_MUTATIONS"):
        if name not in MUTATIONS:
            missing.append(("graphql", name))
    return missing


def count_forms(source):
    return (len(pairs(source, "SIGNED_COMMANDS")) + len(pairs(source, "CLOSE_COMMANDS")) + len(pairs(source, "SIGNED_API"))
            + len(names_in(source, "GRAPHQL_BODY_MUTATIONS")))


def hermetic_rig(gated=None):
    """#1696: a Rig whose shim reads the merge gate config from the rig's own private directory (MG_HOME), never the account's real home: a machine's own
    config cannot change the result. No `gated` repo: no config at all (nothing gated). With one: a config.json that lists it under merge_gate.repos."""
    rig = Rig()
    shim = rig.shims / "gh"
    text = shim.read_text()
    assert text.count("\nMG_HOME = None") == 1
    shim.write_text(text.replace("\nMG_HOME = None", "\nMG_HOME = %r" % str(rig.home), 1))
    if gated:
        config = rig.home / ".config" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"merge_gate": {"repos": {gated: {}}}}))
    return rig


def gql(name):
    return "mutation{%s(input:{subjectId:\"X\",body:\"hello\"}){clientMutationId}}" % name


def battery(rig):
    bad = []
    path = rig.home / "in.json"
    path.write_text('{"body":"x"}')
    done, calls = rig.run("api", ALL_API["issue-comments"], "--input", str(path))
    if len(calls) != 1 or SIGNED not in "".join(calls[0]["files"].values()):
        bad.append("--input file not signed")
    done, calls = rig.run("api", ALL_API["reviews"], "--input", "-", stdin='{"body":"x"}')
    if len(calls) != 1 or calls[0]["stdin"].count(SIGNED) != 1:
        bad.append("--input stdin not signed")
    path.write_text('{"event":"COMMENT"}')
    done, calls = rig.run("api", ALL_API["reviews"], "--input", str(path))
    if len(calls) != 1 or calls[0]["argv"][-1] == str(path) or calls[0]["files"].get(calls[0]["argv"][-1]) != '{"event":"COMMENT"}':
        bad.append("no-body --input passed by path, not by temp copy")
    fifo = rig.home / "fifo.json"
    os.mkfifo(str(fifo))
    done, calls = rig.run("api", ALL_API["reviews"], "--input", str(fifo))
    if calls or "is not a regular file" not in done.stderr:
        bad.append("a FIFO --input was not refused")
    for label, endpoint in NEW_API_EXAMPLES.items():
        done, calls = rig.run("api", endpoint, "-X", "PATCH", "-f", "body=x")
        if len(calls) != 1 or calls[0]["argv"][-1].count(SIGNED) != 1:
            bad.append("endpoint not signed: " + label)
    for spelling in SPELLINGS:
        done, calls = rig.run("api", spelling, "-f", "query=" + gql("addComment"))
        if calls or done.returncode != 1:
            bad.append("graphql spelling let through: " + spelling)
    for flag in ("-f", "-F"):
        done, calls = rig.run("api", URL_COMMENTS, flag, "body=x")
        if len(calls) != 1 or calls[0]["argv"][-1].count(SIGNED) != 1:
            bad.append("URL form not signed: " + flag)
    path.write_text('{"body":"x"}')
    done, calls = rig.run("api", URL_COMMENTS, "--input", str(path))
    if len(calls) != 1 or SIGNED not in "".join(calls[0]["files"].values()):
        bad.append("URL form --input not signed")
    for name in MUTATIONS:
        done, calls = rig.run("api", "graphql", "-f", "query=" + gql(name))
        if calls or done.returncode != 1:
            bad.append("graphql let through: " + name)
    return bad


class TestGhShimSignInput(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def file(self, text, name="in.json"):
        path = self.rig.home / name
        path.write_text(text)
        return str(path)

    def refused(self, done, calls):
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertTrue(done.stderr.startswith("gh: refused:"), done.stderr)
        self.assertEqual(calls, [])

    def test_s3_input_file_is_signed_once_and_the_temp_file_removed(self):
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "--input", self.file('{"body":"x"}'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(calls), 1)
        temp = calls[0]["argv"][-1]
        data = json.loads(calls[0]["files"][temp])
        self.assertEqual(data["body"].count(SIGNED), 1)
        self.assertNotEqual(temp, str(self.rig.home / "in.json"))
        self.assertFalse(os.path.exists(temp))

    def test_s3b_input_equals_spelling(self):
        original = self.file('{"body":"x"}')
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "--input=" + original)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["argv"][-1].startswith("--input="))
        self.assertNotEqual(calls[0]["argv"][-1], "--input=" + original)   # the recorder cannot read an =path value: the swap to a temp file is what is checked

    def test_s4_input_stdin_is_signed_and_event_kept(self):
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", "-", stdin='{"body":"x","event":"COMMENT"}')
        data = json.loads(calls[0]["stdin"])
        self.assertEqual(data["body"].count(SIGNED), 1)
        self.assertEqual(data["event"], "COMMENT")

    def test_s5_a_broken_input_is_refused(self):
        cases = [(self.file("{nope"), None), (self.file('{"body":5}'), None), (str(self.rig.home / "missing.json"), None)]
        for value, stdin in cases:
            with self.subTest(value=value):
                self.refused(*self.rig.run("api", "repos/o/r/issues/1/comments", "--input", value))
        with self.subTest(value="stdin"):
            self.refused(*self.rig.run("api", "repos/o/r/issues/1/comments", "--input", "-", stdin="{nope"))

    def test_an_input_without_a_body_passes_unchanged(self):
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", "-", stdin='{"event":"APPROVE"}')
        self.assertEqual(calls[0]["stdin"], '{"event":"APPROVE"}')
        path = self.file('{"event":"APPROVE"}')
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", path)
        self.assertEqual(calls[0]["argv"][-1], path)

    def test_s19_review_comment_endpoints_are_signed(self):
        for endpoint, extra in (("repos/o/r/pulls/comments/5", ("-X", "PATCH")), ("repos/o/r/pulls/1/comments/5/replies", ())):
            with self.subTest(endpoint=endpoint):
                done, calls = self.rig.run("api", endpoint, *extra, "-f", "body=x")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["argv"][-1].count(SIGNED), 1)

    def test_s20_review_comment_endpoints_with_input(self):
        done, calls = self.rig.run("api", "repos/o/r/pulls/comments/5", "-X", "PATCH", "--input", self.file('{"body":"x"}'))
        temp = calls[0]["argv"][-1]
        self.assertEqual(json.loads(calls[0]["files"][temp])["body"].count(SIGNED), 1)
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/comments/5/replies", "--input", "-", stdin='{"body":"x"}')
        self.assertEqual(json.loads(calls[0]["stdin"])["body"].count(SIGNED), 1)
        self.refused(*self.rig.run("api", "repos/o/r/pulls/1/comments/5/replies", "--input", "-", stdin="[["))

    def test_s7_each_graphql_body_mutation_is_refused(self):
        for name in MUTATIONS:
            with self.subTest(name=name):
                done, calls = self.rig.run("api", "graphql", "-f", "query=" + gql(name))
                self.refused(done, calls)
                self.assertIn(name, done.stderr)

    def test_s8_other_ways_to_name_a_mutation(self):
        q = gql("addComment")
        forms = {
            "file": ("-F", "query=@" + self.file(q, "q.graphql"), None),
            "upper": ("-f", "query=" + q.upper(), None),
            "alias": ("-f", "query=mutation{a: addComment(input:{body:\"x\"}){clientMutationId}}", None),
            "json": ("--input", self.file(json.dumps({"query": q}), "q.json"), None),
            "stdin": ("--input", "-", json.dumps({"query": q})),
        }
        for label, (flag, value, stdin) in forms.items():
            with self.subTest(form=label):
                self.refused(*self.rig.run("api", "graphql", flag, value, stdin=stdin))
        with self.subTest(form="unreadable"):
            self.refused(*self.rig.run("api", "graphql", "-F", "query=@" + str(self.rig.home / "none.graphql")))

    def test_u2_reads_and_other_calls_are_untouched(self):
        rig = hermetic_rig()   # no merge gate config in this rig's home: the merge mutation below is not gated
        self.addCleanup(rig.close)
        for args in (("api", "repos/o/r/issues/1/comments"), ("api", "graphql", "-f", "query=query { viewer { login } }"),
                     ("api", "repos/o/r/issues/1/labels", "-f", "body=x"), ("api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "body=x"),
                     ("api", "graphql", "-f", "query=mutation{mergePullRequest(input:{pullRequestId:\"X\"}){clientMutationId}}")):
            with self.subTest(args=args):
                done, calls = rig.run(*args)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["argv"], list(args))

    def test_u3_a_graphql_merge_mutation_with_and_without_a_gate(self):
        merge = ("api", "graphql", "-f", "query=mutation{mergePullRequest(input:{pullRequestId:\"X\"}){clientMutationId}}")
        free = hermetic_rig()
        self.addCleanup(free.close)
        done, calls = free.run(*merge)
        self.assertEqual(len(calls), 1, done.stderr)   # no gate: the call goes through untouched
        self.assertEqual(calls[0]["argv"], list(merge))
        gated = hermetic_rig("o/r")
        self.addCleanup(gated.close)
        done, calls = gated.run(*merge)
        self.assertEqual(calls, [])                    # a gated repository: refused with one line, nothing reaches the real gh
        self.assertEqual(done.returncode, 10)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)

    def test_u6_input_on_a_get_or_another_endpoint_is_untouched(self):
        path = self.file('{"body":"x"}')
        done, calls = self.rig.run("api", "repos/o/r/issues/1/labels", "--input", path)
        self.assertEqual(calls[0]["argv"][-1], path)

    def test_h1_fifo_input_is_refused_before_reading(self):
        fifo = self.rig.home / "fifo.json"
        os.mkfifo(str(fifo))
        for spelling in (("--input", str(fifo)), ("--input=" + str(fifo),)):
            with self.subTest(spelling=spelling):
                done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", *spelling)
                self.refused(done, calls)
                self.assertEqual(done.stderr.strip(), "gh: refused: the --input file %s is not a regular file, so it cannot be signed and nothing was posted" % fifo)
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", str(self.rig.home))   # a directory
        self.refused(done, calls)
        self.assertIn("is not a regular file", done.stderr)
        control = self.file('{"event":"COMMENT"}')   # control: a regular file of the same shape passes
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", control)
        self.assertEqual(len(calls), 1, done.stderr)

    def test_h1_no_body_input_is_passed_as_a_temp_copy_of_the_bytes_read(self):
        text = '{"event":"COMMENT",\n "n": "\u00e9"}'
        original = self.file(text)
        for spelling in (lambda p: ("--input", p), lambda p: ("--input=" + p,)):
            with self.subTest(spelling=spelling(original)):
                done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", *spelling(original))
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(len(calls), 1)
                last = calls[0]["argv"][-1]
                temp = last[len("--input="):] if last.startswith("--input=") else last
                self.assertNotEqual(temp, original)
                if not last.startswith("--input="):   # the recorder reads only a bare path
                    self.assertEqual(calls[0]["files"][temp], text)
                self.assertFalse(os.path.exists(temp))
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", "-", stdin='{"event":"COMMENT"}')   # stdin: read once, passed as that text
        self.assertEqual(calls[0]["stdin"], '{"event":"COMMENT"}')

    def test_h2_every_spelling_of_graphql_is_refused_like_the_bare_word(self):
        bare, _calls = self.rig.run("api", "graphql", "-f", "query=" + gql("addComment"))
        self.assertEqual(bare.stderr.strip(), "gh: refused: the GraphQL mutation addComment writes a body that cannot be signed and nothing was posted; use gh pr comment, gh issue comment or POST repos/OWNER/REPO/issues/N/comments")   # control
        for spelling in SPELLINGS + ("HTTPS://API.GITHUB.COM/graphql/#x",):
            with self.subTest(spelling=spelling):
                done, calls = self.rig.run("api", spelling, "-f", "query=" + gql("addComment"))
                self.refused(done, calls)
                self.assertEqual(done.stderr, bare.stderr)

    def test_h2_a_read_query_in_every_spelling_passes_untouched(self):
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        for spelling in ("graphql",) + SPELLINGS:
            with self.subTest(spelling=spelling):
                args = ("api", spelling, "-f", "query=query { viewer { login } }")
                done, calls = rig.run(*args)
                self.assertEqual(len(calls), 1, done.stderr)
                self.assertEqual(calls[0]["argv"], list(args))

    def test_h3_a_full_api_github_com_url_is_signed(self):
        for flag in ("-f", "-F"):
            with self.subTest(flag=flag):
                done, calls = self.rig.run("api", URL_COMMENTS, flag, "body=x")
                self.assertEqual(len(calls), 1, done.stderr)
                self.assertEqual(calls[0]["argv"][-1].count(SIGNED), 1)
                self.assertEqual(calls[0]["argv"][1], URL_COMMENTS)   # the URL itself is passed on as written
        done, calls = self.rig.run("api", URL_COMMENTS, "--input", self.file('{"body":"x"}'))
        temp = calls[0]["argv"][-1]
        self.assertEqual(json.loads(calls[0]["files"][temp])["body"].count(SIGNED), 1)
        done, calls = self.rig.run("api", "https://API.github.com/repos/o/r/pulls/1/reviews", "--input", "-", stdin='{"body":"x"}')
        self.assertEqual(json.loads(calls[0]["stdin"])["body"].count(SIGNED), 1)

    def test_h3_another_host_is_not_touched(self):
        for url in ("https://example.com/repos/o/r/issues/1/comments", "https://api.github.com.evil.test/repos/o/r/issues/1/comments"):
            with self.subTest(url=url):
                args = ("api", url, "-f", "body=x")
                done, calls = self.rig.run(*args)
                self.assertEqual(calls[0]["argv"], list(args))

    def test_s10_completeness_and_count(self):
        self.assertEqual(forms_missing(self.source), [])
        self.assertEqual(count_forms(self.source), 21)
        self.assertEqual(old_forms_without_an_example(self.source), [])

    def test_s11_a_planted_name_is_reported(self):
        planted = self.source + "\nGRAPHQL_BODY_MUTATIONS = {**GRAPHQL_BODY_MUTATIONS, 'addPlanted': 'x'}\n"
        self.assertEqual(forms_missing(planted), [("graphql", "addPlanted")])

    def test_the_battery_passes_for_the_real_shim(self):
        self.assertEqual(battery(self.rig), [])

    def mutant(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old)
        rig = Rig(self.source.replace(old, new, 1))
        self.addCleanup(rig.close)
        return battery(rig)

    def test_s6_mutant_input_left_unsigned_is_seen(self):
        seen = self.mutant("        return input_call(args, namer(), found)", "        return None")
        self.assertIn("--input file not signed", seen)
        self.assertIn("--input stdin not signed", seen)

    def test_s9_mutant_graphql_let_through_is_seen(self):
        seen = self.mutant('        return graphql_call(args)', "        return None")
        self.assertEqual(len([s for s in seen if s.startswith("graphql let through")]), len(MUTATIONS))

    def test_mutant_a_left_out_signed_api_entry_is_seen(self):
        seen = self.mutant('("pr-review-reply", r"pulls/[0-9]+/comments/[0-9]+/replies")', '("pr-review-reply", r"pulls/[0-9]+/comments/[0-9]+/repliesX")')
        self.assertIn("endpoint not signed: pr-review-reply", seen)
        seen = self.mutant('("pr-review-comment", r"pulls/comments/[0-9]+")', '("pr-review-comment", r"pulls/comments/[0-9]+X")')
        self.assertIn("endpoint not signed: pr-review-comment", seen)

    def test_h1_mutants_are_seen(self):
        seen = self.mutant("        return passed_copy(args, index, inline, value, text)", "        return list(args), (text if value == \"-\" else None), None")
        self.assertEqual(seen, ["no-body --input passed by path, not by temp copy"])
        seen = self.mutant("        if not stat.S_ISREG(os.fstat(fd).st_mode):", "        if False:")
        self.assertEqual(seen, ["a FIFO --input was not refused"])

    def test_h2_h3_mutants_are_seen(self):
        seen = self.mutant("    return [api_endpoint(p) for p in positionals], method, fields", "    return positionals, method, fields")
        self.assertEqual(sorted(seen), sorted(["graphql spelling let through: " + p for p in SPELLINGS] + ["URL form not signed: -f", "URL form not signed: -F", "URL form --input not signed"]))
        seen = self.mutant('    return "graphql" if bare == "graphql" else short', '    return text if bare == "graphql" else short')
        self.assertEqual(sorted(seen), sorted("graphql spelling let through: " + p for p in SPELLINGS if p != "graphql"))
        seen = self.mutant('    short = API_HOST_RE.sub("", text, count=1)', "    short = text")
        self.assertEqual(sorted(seen), sorted(["URL form not signed: -f", "URL form not signed: -F", "URL form --input not signed"]
                                              + ["graphql spelling let through: https://api.github.com/graphql"]))


if __name__ == "__main__":
    unittest.main()
