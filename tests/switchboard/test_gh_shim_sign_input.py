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


def gh_err(done):
    """#1696: the shim's own gh: lines of a call's stderr (an interpreter warning on another platform is not the shim's output)."""
    return "\n".join(l for l in done.stderr.splitlines() if l.startswith("gh:"))


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


H4_REFUSAL = "gh: refused: the query file %s is not a regular file, so it cannot be signed and nothing was posted"
M1_HOSTS = ("https://user@api.github.com", "https://api.github.com:443", "https://api.github.com.", "https://API.GitHub.com", "https://u:p@api.github.com.:443")
READ_QUERY = "query { viewer { login } }"


def battery2(rig):
    """#1696 round 2: H4 (graphql query files: refused when not regular, passed as temp copies) and M1 (host normalisation). Names the failures."""
    bad = []
    home = rig.home
    query = home / "q.graphql"
    query.write_text(READ_QUERY)
    for form in (("-F", "query=@%s" % query), ("--field=query=@%s" % query,)):
        done, calls = rig.run("api", "graphql", *form)
        last = calls[0]["argv"][-1] if calls else ""
        temp = last.split("=@", 1)[-1]
        if len(calls) != 1 or temp == str(query) or calls[0]["files"].get(temp) != READ_QUERY or os.path.exists(temp):
            bad.append("clean query not passed as a temp copy: " + form[0])
    path = home / "in.json"
    path.write_text(json.dumps({"query": READ_QUERY}))
    done, calls = rig.run("api", "graphql", "--input", str(path))
    if len(calls) != 1 or calls[0]["argv"][-1] == str(path) or calls[0]["files"].get(calls[0]["argv"][-1]) != path.read_text():
        bad.append("clean --input not passed as a temp copy")
    done, calls = rig.run("api", "graphql", "-F", "query=@-", stdin=READ_QUERY)
    if len(calls) != 1 or calls[0]["stdin"] != READ_QUERY:
        bad.append("stdin query not passed")
    done, calls = rig.run("api", "graphql", "--input", "-", stdin=json.dumps({"query": READ_QUERY}))
    if len(calls) != 1 or json.loads(calls[0]["stdin"])["query"] != READ_QUERY:
        bad.append("stdin --input not passed")
    fifo = home / "q.fifo"
    os.mkfifo(str(fifo))
    for form in (("-F", "query=@%s" % fifo), ("--input", str(fifo)), ("--input=" + str(fifo),)):
        done, calls = rig.run("api", "graphql", *form)
        if calls or not gh_err(done).startswith("gh: refused:") or "is not a regular file" not in done.stderr:
            bad.append("FIFO query not refused: " + form[0])
    done, calls = rig.run("api", "graphql", "-F", "query=@%s" % fifo)
    if gh_err(done) != H4_REFUSAL % fifo:
        bad.append("FIFO refusal text differs")
    for host in M1_HOSTS:
        done, calls = rig.run("api", host + "/graphql", "-f", "query=" + gql("addComment"))
        if calls or not gh_err(done).startswith("gh: refused: the GraphQL mutation addComment"):
            bad.append("graphql mutation let through: " + host)
        done, calls = rig.run("api", host + "/repos/o/r/issues/1/comments", "-f", "body=x")
        if len(calls) != 1 or calls[0]["argv"][-1].count(SIGNED) != 1:
            bad.append("REST comment not signed: " + host)
    for host in ("https://api.github.com.evil", "https://api.github.com:8443", "http://api.github.com"):
        args = ("api", host + "/graphql", "-f", "query=" + gql("addComment"))
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"] != list(args):
            bad.append("not-GitHub host touched (graphql): " + host)
        args = ("api", host + "/repos/o/r/issues/1/comments", "-f", "body=x")
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"] != list(args):
            bad.append("not-GitHub host touched (REST): " + host)
    return bad


class TestGhShimSignInput(unittest.TestCase):
    def setUp(self):
        self.rig = hermetic_rig()   # #1696: the shim's merge-gate home is the rig's own: a machine's gate config cannot change a result
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def file(self, text, name="in.json"):
        path = self.rig.home / name
        path.write_text(text)
        return str(path)

    def refused(self, done, calls):
        self.assertEqual(done.returncode, 1)
        gh_lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        self.assertEqual([l for l in gh_lines if l.startswith("gh: refused:")], gh_lines, done.stderr)
        self.assertEqual(len(gh_lines), 1, done.stderr)
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
        self.assertNotEqual(calls[0]["argv"][-1], path)
        self.assertEqual(calls[0]["files"][calls[0]["argv"][-1]], '{"event":"APPROVE"}')

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
        self.assertEqual(len([l for l in done.stderr.splitlines() if l.startswith("gh:")]), 1, done.stderr)

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
                self.assertEqual(gh_err(done), "gh: refused: the --input file %s is not a regular file, so it cannot be signed and nothing was posted" % fifo)
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
        self.assertEqual(gh_err(bare), "gh: refused: the GraphQL mutation addComment writes a body that cannot be signed and nothing was posted; use gh pr comment, gh issue comment or POST repos/OWNER/REPO/issues/N/comments")   # control
        for spelling in SPELLINGS + ("HTTPS://API.GITHUB.COM/graphql/#x",):
            with self.subTest(spelling=spelling):
                done, calls = self.rig.run("api", spelling, "-f", "query=" + gql("addComment"))
                self.refused(done, calls)
                self.assertEqual([l for l in done.stderr.splitlines() if l.startswith("gh:")], bare.stderr.splitlines())

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

    def test_h4_the_older_hold_and_merge_gate_refusals_come_first(self):
        merge = ("api", "graphql", "-f", "query=mutation{mergePullRequest(input:{pullRequestId:\"X\"}){clientMutationId}}")
        unreadable = (("api", "graphql", "--input", "query.json"), ("api", "graphql", "-F", "query=@query.graphql"))
        comment = ("api", "graphql", "-f", "query=" + gql("addComment"))
        free = hermetic_rig()   # control: with no gate and no hold, the comment mutation and the unreadable queries are refused by the NEW line, exit 1
        self.addCleanup(free.close)
        done, calls = free.run(*comment)
        self.assertEqual((done.returncode, calls), (1, []))
        self.assertIn("writes a body that cannot be signed", done.stderr)
        for args in unreadable:
            done, calls = free.run(*args)
            self.assertEqual((done.returncode, calls), (1, []))
            self.assertIn("cannot be signed and nothing was posted", done.stderr)
        gated = hermetic_rig("o/r")
        self.addCleanup(gated.close)
        for args in (merge,) + unreadable:
            with self.subTest(args=args):
                done, calls = gated.run(*args)
                self.assertEqual((done.returncode, calls), (10, []))
                self.assertEqual(len([l for l in done.stderr.splitlines() if l.startswith("gh:")]), 1, done.stderr)
                self.assertTrue(gh_err(done).startswith("gh: "), done.stderr)
                self.assertNotIn("cannot be signed", done.stderr)
        held = hermetic_rig()
        self.addCleanup(held.close)
        config = held.home / ".config" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"hold_labels": {"o/r": {"label": "hold", "message": "m"}}}))
        done, calls = held.run(*comment)
        self.assertEqual((done.returncode, calls), (1, []))
        self.assertEqual(gh_err(done), "gh: graphql mutation cannot be tied to a pull request, so it is refused while a hold label is configured: use the gh pr verbs")

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

    def test_h4_mutant_the_new_refusal_before_the_gates_is_seen(self):
        source = self.source.replace("        return late\n", "        raise\n", 1)
        self.assertNotEqual(source, self.source)
        rig = hermetic_rig("o/r")
        self.addCleanup(rig.close)
        shim = rig.shims / "gh"
        shim.write_text(shim.read_text().replace("        return late\n", "        raise\n", 1))
        done, calls = rig.run("api", "graphql", "--input", "query.json")
        self.assertEqual(done.returncode, 1)   # the mutant refuses first (exit 1), the real shim exit 10 (test_h4_the_older...)
        self.assertIn("cannot be signed", done.stderr)

    def test_h4_m1_round_two_battery_passes_for_the_real_shim(self):   # the CONTROL of every mutant below
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        self.assertEqual(battery2(rig), [])

    def mutant2(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old)
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        shim = rig.shims / "gh"
        shim.write_text(shim.read_text().replace(old, new, 1))
        return battery2(rig)

    def test_h4_mutant_original_path_passed_is_seen(self):
        seen = self.mutant2("    return plan[0], plan[1], temps or None", "    return list(args), plan[1], None")
        self.assertEqual(len(seen), 3, seen)
        self.assertTrue(all(x.startswith("clean ") for x in seen), seen)

    def test_h4_mutant_fifo_not_refused_is_seen(self):
        seen = self.mutant2('        if not stat.S_ISREG(mode):\n            raise Unsignable("the query file', '        if False:\n            raise Unsignable("the query file')
        self.assertEqual(len(seen), 4, seen)
        self.assertTrue(all("FIFO" in x for x in seen), seen)

    def test_m1_mutant_host_not_normalised_is_seen(self):   # 8 failures: the case-only host still matches the old rule
        seen = self.mutant2('if parts.scheme.lower() == "https" and host and', 'if False and host and')
        self.assertTrue(all(s.startswith(("graphql mutation let through", "REST comment not signed")) for s in seen), seen)
        self.assertEqual(len(seen), 2 * len(M1_HOSTS) - 2, seen)   # the plain-case host still matches the old rule


if __name__ == "__main__":
    unittest.main()
