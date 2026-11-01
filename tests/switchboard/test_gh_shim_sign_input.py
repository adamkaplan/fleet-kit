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
        done, calls = rig.run("api", endpoint, "-X", "POST" if label == "pr-review-reply" else "PATCH", "-f", "body=x")   # r4: a method the shape does not have is refused
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
        if calls or RAW_LINE not in gh_err(done):   # r5 (OLD: refused with the mutation name): a userinfo, port, dot or upper-case host is not accepted for a write
            bad.append("graphql mutation let through: " + host)
        done, calls = rig.run("api", host + "/repos/o/r/issues/1/comments", "-f", "body=x")
        if calls or RAW_LINE not in gh_err(done):   # r5 (OLD: signed once)
            bad.append("REST comment not signed: " + host)
    for host in ("https://api.github.com.evil", "https://api.github.com:8443", "http://api.github.com:8080"):   # #1696 r3: plain http to api.github.com is GitHub's (refused/signed like https): see TestRoot
        args = ("api", host + "/graphql", "-f", "query=" + gql("addComment"))
        done, calls = rig.run(*args)
        if calls or not gh_err(done).startswith("gh: refused:"):   # r4 (OLD: passed untouched): another host's path is not on the allow-list: a body is refused
            bad.append("not-GitHub host touched (graphql): " + host)
        args = ("api", host + "/repos/o/r/issues/1/comments", "-f", "body=x")
        done, calls = rig.run(*args)
        if calls or not gh_err(done).startswith("gh: refused:"):
            bad.append("not-GitHub host touched (REST): " + host)
    return bad


# ---- #1696 round 3: the endpoint is decided by its MEANING (canonical path, classified), not by a list of spellings.
R3_GRAPHQL = ("./graphql", "repos/x/../graphql", "/repos/x/../graphql", "https://api.github.com/x/../graphql", "graphql/.", "graphql/x/..", "graphql%2f",
              "%2fgraphql", "gra%70hql", "gra%2570hql", "graphql;x", "graphql\t", "graphql\n", "\tgraphql", "graphql ", "//api.github.com/graphql",
              "https://api.github.com:443./graphql", "graphql\\", "\\graphql", "http://api.github.com/graphql", "https://api.github.com:443/graphql")
R3_COMMENTS = ("./repos/o/r/issues/1/comments", "repos/o/x/../r/issues/1/comments", "/repos/o/r/issues/1/comments", "repos/o/r/issues/1/comments/.",
               "repos%2fo%2fr%2fissues%2f1%2fcomments", "repos/o/r/issues/1/comm%65nts", "repos/o/r/issues/1/comments;x", "repos/o/r/issues/1/comments?a=b%20c",
               "https://api.github.com/./repos/o/r/issues/1/comments", "http://api.github.com/repos/o/r/issues/1/comments",
               "//api.github.com/repos/o/r/issues/1/comments", "repos\\o\\r\\issues\\1\\comments", "Repos/o/r/issues/1/comments", "repos/o/r/issues/1/comm%2565nts")
R3_UNKNOWN = ("foo/bar", "%zz/graphql", "repos%", "x%e9", "weird")
R3_TEXT = "gh: refused: the GraphQL mutation addComment writes a body that cannot be signed and nothing was posted; use gh pr comment, gh issue comment or POST repos/OWNER/REPO/issues/N/comments"
R3_READ = "query { viewer { login } }"
RAW_LINE = ("the endpoint would change if cleaned: the shim matches the raw path only; write it plainly as repos/OWNER/REPO/... "
            "(one optional leading slash, no backslash, %-escape, dot segment, semicolon, space, #, double or trailing slash)")
R5_RAW_OK_GRAPHQL = ("//api.github.com/graphql", "http://api.github.com/graphql")   # r5 (OLD: all 21 spellings reached the graphql parser): only these two are raw-valid
R5_RAW_OK_COMMENTS = ("/repos/o/r/issues/1/comments", "repos/o/r/issues/1/comments?a=b%20c", "http://api.github.com/repos/o/r/issues/1/comments", "//api.github.com/repos/o/r/issues/1/comments")
R5_RAW_OK_UNKNOWN = ("foo/bar", "weird")
R3_NOT_GRAPHQL = ("repos/x/../graphql", "/repos/x/../graphql", "graphql\t", "graphql\n", "\tgraphql", "graphql ", "https://api.github.com:443./graphql")   # r4: their meaning is not the graphql endpoint (or cannot be read): a body is refused


def battery3(rig):
    """#1696 round 3: names every spelling that is decided wrongly."""
    bad = []
    mutation = "query=" + gql("addComment")
    for spelling in R3_GRAPHQL:
        done, calls = rig.run("api", spelling, "-f", mutation)
        want_text = spelling in R5_RAW_OK_GRAPHQL   # r5 (OLD: every spelling got the mutation line): an unclean spelling is refused by the raw rule
        if calls or done.returncode != 1 or (gh_err(done) != R3_TEXT if want_text else RAW_LINE not in gh_err(done)):
            bad.append("mutation not refused: %r" % spelling)
        args = ("api", spelling, "-f", "query=" + R3_READ)
        done, calls = rig.run(*args)
        if spelling not in R5_RAW_OK_GRAPHQL:   # r5 (OLD: read query passed for the graphql meanings)
            if calls or not gh_err(done).startswith("gh: refused:"):
                bad.append("read query with a body to a non-graphql meaning not refused: %r" % spelling)
        elif len(calls) != 1 or calls[0]["argv"] != list(args):
            bad.append("read query touched: %r" % spelling)
        args = ("api", spelling)   # body-less GET: untouched in every spelling
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"] != list(args):
            bad.append("body-less GET touched: %r" % spelling)
    for spelling in R3_COMMENTS:
        done, calls = rig.run("api", spelling, "-f", "body=x")
        if spelling in R5_RAW_OK_COMMENTS:   # r5 (OLD: every spelling signed once): only the raw-valid ones are signed; the rest are refused by the raw rule
            if len(calls) != 1 or calls[0]["argv"][-1].count(SIGNED) != 1 or calls[0]["argv"][1] != spelling:
                bad.append("comment not signed once: %r" % spelling)
        elif calls or not gh_err(done).startswith("gh: refused:"):
            bad.append("comment not signed once: %r" % spelling)
    for spelling in R3_UNKNOWN:   # class (iii): refused only when the request text names a body mutation
        done, calls = rig.run("api", spelling, "-f", mutation)
        if calls or (gh_err(done) != R3_TEXT if spelling in R5_RAW_OK_UNKNOWN else RAW_LINE not in gh_err(done)):
            bad.append("unknown shape with a mutation not refused: %r" % spelling)
        args = ("api", spelling, "-f", "query=" + R3_READ)
        done, calls = rig.run(*args)
        if calls or not gh_err(done).startswith("gh: refused: a gh api POST"):   # r4 (OLD: passed): unknown + body is refused
            bad.append("unknown shape with a read and a body not refused: %r" % spelling)
        args = ("api", spelling)
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"] != list(args):
            bad.append("unknown shape with no text touched: %r" % spelling)
    done, calls = rig.run("api", "foo/bar", "-F", "query=@/nonexistent/q.gql")
    if calls or not gh_err(done).startswith("gh: refused:"):
        bad.append("unknown shape with an unreadable query not refused")
    done, calls = rig.run("api", "foo/bar", "--input", "-", stdin=json.dumps({"query": gql("updateIssueComment")}))
    if calls or not gh_err(done).startswith("gh: refused:"):   # r4: no inline text to name the mutation: the generic allow-list refusal
        bad.append("unknown shape with a --input mutation not refused")
    done, calls = rig.run("api", "weird", "-F", "query=@-", stdin=gql("addPullRequestReview"))
    if calls or not gh_err(done).startswith("gh: refused:"):
        bad.append("unknown shape with a stdin mutation not refused")
    query = rig.home / "r3.gql"
    query.write_text(gql("addComment"))
    done, calls = rig.run("api", "weird", "-F", "query=@%s" % query)
    if calls or not gh_err(done).startswith("gh: refused:"):
        bad.append("unknown shape with a query file mutation not refused")
    for endpoint in ("search/issues",):   # r4: off the allow-list, with a mutation text: refused with the named mutation (an allow-listed REST path is a REST write)
        done, calls = rig.run("api", endpoint, "-f", mutation)
        if calls or gh_err(done) != R3_TEXT:
            bad.append("REST shape with a mutation not refused: " + endpoint)
    for args in (("api", "repos/o/r/issues/1/labels", "-f", "labels[]=x"), ("api", "repos/o/r/pulls/1/merge", "-X", "PUT", "-f", "merge_method=squash"),
                 ("api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "state=closed"), ("api", "search/issues", "-X", "GET", "-f", "query=" + R3_READ),
                 ("api", "https://api.github.com.evil/graphql"), ("api", "https://example.com/x/../graphql", "-X", "GET")):
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"] != list(args):   # never an ordinary REST write; another host is the user's own gh (kept as before)
            bad.append("untouched call changed: %r" % (args[1:3],))
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
                if spelling == "graphql?x=1":   # r5 round 5 (OLD: the mutation line): any '?' on graphql is refused first
                    self.assertTrue(gh_err(done).startswith(Q_ANCHOR), gh_err(done))
                elif spelling in R5_RAW_OK_GRAPHQL + ("/graphql", "https://api.github.com/graphql", "GraphQL"):
                    self.assertEqual([l for l in done.stderr.splitlines() if l.startswith("gh:")], bare.stderr.splitlines())
                else:   # r5 (OLD: the same line as the bare word): graphql/ and the #x form change if cleaned
                    self.assertIn(RAW_LINE, gh_err(done))

    def test_h2_a_read_query_in_every_spelling_passes_untouched(self):
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        for spelling in ("graphql",) + SPELLINGS:
            with self.subTest(spelling=spelling):
                args = ("api", spelling, "-f", "query=query { viewer { login } }")
                done, calls = rig.run(*args)
                if spelling == "graphql?x=1":   # round 5 (OLD: passed): any '?' on graphql is refused
                    self.assertEqual(calls, [])
                    self.assertTrue(gh_err(done).startswith(Q_ANCHOR), gh_err(done))
                    continue
                if spelling in ("GraphQL", "graphql/"):   # r5 (OLD: passed): a write on a spelling that would change if cleaned
                    self.refused(done, calls)
                    continue
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
        self.refused(done, calls)   # r5 (OLD: signed once): the host is compared exactly, lower-case
        self.assertIn(RAW_LINE, gh_err(done))

    def test_h3_another_host_is_not_touched(self):
        for url in ("https://example.com/repos/o/r/issues/1/comments", "https://api.github.com.evil.test/repos/o/r/issues/1/comments"):
            with self.subTest(url=url):
                args = ("api", url, "-f", "body=x")
                done, calls = self.rig.run(*args)
                self.refused(done, calls)   # r4 (OLD: passed untouched): a body to any host but api.github.com is off the allow-list
                self.assertTrue(gh_err(done).startswith("gh: refused: a gh api POST"), gh_err(done))

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
        self.assertEqual(sorted(seen), sorted(["graphql spelling let through: " + p for p in ("/graphql", "https://api.github.com/graphql")] + ["URL form not signed: -f", "URL form not signed: -F", "URL form --input not signed"]))   # r5 (OLD: 5 spellings, now the 3 raw-valid ones: GraphQL and graphql/ are refused by the raw rule or the unknown rule first)
        seen = self.mutant('    if first == "graphql":\n        return "graphql"', '    if first == "graphql":\n        return text')
        self.assertEqual(sorted(seen), sorted(["graphql spelling let through: " + p for p in ("/graphql", "graphql?x=1", "https://api.github.com/graphql")]))   # r5 (OLD: []): the 3 raw-valid spellings now reach the canonical form
        seen = self.mutant('host.rstrip(".") != "api.github.com"', "True")
        self.assertEqual(sorted(seen), sorted(["graphql spelling let through: https://api.github.com/graphql", "URL form not signed: -f", "URL form not signed: -F", "URL form --input not signed"]))   # r4 (OLD: + one graphql spelling)

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
        seen = self.mutant2('host.rstrip(".") != "api.github.com"', "True")
        self.assertEqual(seen, [], seen)   # r5 (OLD: 2 per host): equivalent now, every such host form is refused by the raw rule before the host test   # r3: the one canonical function has no old rule behind it: every host form is seen

    def test_r3_each_spelling_is_decided_by_its_meaning(self):   # CONTROL of the r3 mutants
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        self.assertEqual(battery3(rig), [])

    def test_r3_each_graphql_spelling_is_refused_with_the_one_line(self):
        for spelling in R3_GRAPHQL:
            for name in MUTATIONS:
                with self.subTest(spelling=spelling, mutation=name):
                    done, calls = self.rig.run("api", spelling, "-f", "query=" + gql(name))
                    self.refused(done, calls)
                    if spelling in R5_RAW_OK_GRAPHQL:
                        self.assertTrue(gh_err(done).startswith("gh: refused: the GraphQL mutation %s writes a body" % name), gh_err(done))
                    else:   # r5 (OLD: the mutation line for every spelling): refused by the raw rule
                        self.assertIn(RAW_LINE, gh_err(done))

    def test_r3_read_queries_and_signed_comments_in_every_spelling(self):
        for spelling in R3_GRAPHQL:
            with self.subTest(read=spelling):
                args = ("api", spelling, "-f", "query=" + R3_READ)
                done, calls = self.rig.run(*args)
                if spelling not in R5_RAW_OK_GRAPHQL:   # r5 (OLD: passed unless in R3_NOT_GRAPHQL): an unclean spelling with a body is refused
                    self.refused(done, calls)
                else:
                    self.assertEqual([c["argv"] for c in calls], [list(args)], done.stderr)
                args = ("api", spelling)
                done, calls = self.rig.run(*args)
                self.assertEqual([c["argv"] for c in calls], [list(args)], done.stderr)
        for spelling in R3_COMMENTS:
            with self.subTest(signed=spelling):
                done, calls = self.rig.run("api", spelling, "-f", "body=x")
                if spelling in R5_RAW_OK_COMMENTS:   # r5 (OLD: all signed once): only raw-valid spellings
                    self.assertEqual(len(calls), 1, done.stderr)
                    self.assertEqual(calls[0]["argv"][-1].count(SIGNED), 1)
                else:
                    self.refused(done, calls)

    def test_r3_the_three_classes(self):
        bad = battery3(self.rig)
        self.assertEqual(bad, [])
        # Another host (https://api.github.com.evil/graphql) goes to the user's own gh with that host: the call is untouched, whatever the query says
        # (kept as before; the backstop is endpoint-agnostic only for api.github.com and for text that cannot be classified).

    def mutant3(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old)
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        shim = rig.shims / "gh"
        shim.write_text(shim.read_text().replace(old, new, 1))
        return battery3(rig)

    def test_r3_mutant_canonicalisation_skipped_is_seen(self):
        seen = self.mutant3("    return [api_endpoint(p) for p in positionals], method, fields", "    return positionals, method, fields")
        print("R3 mutant 1 seen:", len(seen))   # r5 (OLD: 48 seen, './graphql' among them): only the two raw-valid scheme-relative / http spellings reach the canonicaliser now
        self.assertEqual(len(seen), 4, seen)
        self.assertIn("mutation not refused: '//api.github.com/graphql'", seen)

    def test_r3_mutant_percent_decode_skipped_is_seen(self):
        seen = self.mutant3('decoded = unquote(text, errors="strict")', "decoded = text")
        self.assertEqual(seen, [])   # r5 (OLD: 3 failures): equivalent now, the decision no longer decodes; the raw rule refuses a %-escape before any decoder is reached

    def test_r3_mutant_dot_segments_not_resolved_is_seen(self):
        seen = self.mutant3('posixpath.normpath("/" + path).lstrip("/")', '("/" + path).lstrip("/")')
        self.assertEqual(seen, [])   # r5 (OLD: 4 failures): equivalent now, the raw rule refuses a dot segment itself



# ---- #1696 round 4: DECIDE BY EFFECT (a body or a method other than GET: only on the allow-list; graphql: only one query, read by a real parser).
UNKNOWN_PATHS = ("foo/bar", "weird", "user/repos", "search/issues", "repos/o/r/git/refs", "https://example.com/repos/o/r/issues/1/comments")
METHOD_FORMS = (("-X", "POST"), ("-X", "post"), ("--method=POST",), ("-XPOST",), ("--method", "POST"), ("-X", "PUT"), ("-X", "delete"), ("-XPATCH",), ("--method=patch",),
                ("-iXPOST",))
BODY_FORMS = (("-f", "a=b"), ("-F", "a=b"), ("--field", "a=b"), ("--raw-field", "a=b"), ("--field=a=b",), ("-fa=b",), ("-ifa=b",), ("--input", "-"))
R4_SPELLINGS = R3_GRAPHQL + ("graphql", "/graphql", "GraphQL", "graphql/", "https://api.github.com/graphql", "HTTPS://API.GITHUB.COM/graphql")
R4_MACOS = ("https://user@api.github.com", "https://api.github.com:443", "https://api.github.com.", "https://API.GitHub.com", "https://u:p@api.github.com.:443")
R4_MUTATION = "query=" + gql("addComment")


def refused_line(done, calls):
    return not calls and gh_err(done).startswith("gh: refused:") and len(gh_err(done).splitlines()) == 1


def effect_battery(rig):
    """#1696 r4: names every call decided wrongly by the effect rule."""
    bad = []
    for path in UNKNOWN_PATHS:
        for form in METHOD_FORMS:
            done, calls = rig.run("api", path, *form)
            if not refused_line(done, calls):
                bad.append("unknown+method passed: %s %s" % (path, " ".join(form)))
        for form in BODY_FORMS:
            done, calls = rig.run("api", path, *form, stdin="{}")
            if not refused_line(done, calls):
                bad.append("unknown+body passed: %s %s" % (path, " ".join(form)))
        for args in (("api", path), ("api", path, "-X", "GET"), ("api", path, "--method=get"), ("api", path, "-XGET", "-f", "a=b"), ("api", path, "--method", "GET", "-F", "a=b"),
                     ("api", path, "-X", "POST", "-X", "GET")):
            done, calls = rig.run(*args)
            if [c["argv"] for c in calls] != [list(args)]:
                bad.append("unknown GET without a body not passed: %s %s" % (path, " ".join(args[2:])))
    done, calls = rig.run("api", "foo/bar", "--method", "GET", "--input", "-", stdin="{}")   # GET with --input: a body
    if not refused_line(done, calls):
        bad.append("unknown GET with --input passed")
    for args in (("api", "foo/bar", "-X", "GET", "-X", "POST"), ("api", "foo/bar", "-X"), ("api", "foo/bar", "--method"), ("api", "foo/bar", "-X", "HEAD")):
        done, calls = rig.run(*args)
        if not refused_line(done, calls):
            bad.append("last or missing method passed: " + " ".join(args[2:]))
    for args in (("api", "repos/o/r/pulls/1/merge", "-X", "PUT"), ("api", "repos/o/r/issues/1/labels", "-f", "labels[]=x"), ("api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "state=closed"),
                 ("api", "repos/o/r/issues/1/labels/x", "-X", "DELETE"), ("api", "repos/o/r/issues", "-f", "title=t"), ("api", "repos/o/r/issues/1/sub_issues", "-F", "sub_issue_id=3")):
        done, calls = rig.run(*args)
        if [c["argv"] for c in calls] != [list(args)]:
            bad.append("allow-listed REST write not passed: " + args[1])
    done, calls = rig.run("api", "repos/o/r/issues/1/labels", "-X", "PATCH", "-f", "a=b")   # an allow-listed path with a method it does not have
    if not refused_line(done, calls):
        bad.append("allow-listed path with a wrong method passed")
    done, calls = rig.run("api", "repos/o/r/issues/1/comments", "-f", "body=x")   # signed as before
    if len(calls) != 1 or calls[0]["argv"][-1].count(SIGNED) != 1:
        bad.append("signed comment not signed")
    for spelling in R4_SPELLINGS:
        for tail in (("-f", R4_MUTATION), ("-f", "body=x")):
            done, calls = rig.run("api", spelling, *tail)
            if not refused_line(done, calls) and spelling != "graphql" or (spelling == "graphql" and tail[1] == "body=x" and not refused_line(done, calls)):
                if not (spelling not in R3_GRAPHQL and tail[1].startswith("body") and False):
                    bad.append("round-3 spelling with %s passed: %r" % (tail[1][:5], spelling))
    for host in R4_MACOS:
        for tail in (("graphql", ("-f", R4_MUTATION)), ("repos/o/r/git/refs", ("-f", "ref=x"))):
            done, calls = rig.run("api", host + "/" + tail[0], *tail[1])
            if not refused_line(done, calls):
                bad.append("macOS form passed: %s %s" % (host, tail[0]))
        args = ("api", host + "/graphql", "-f", "query=" + R3_READ)
        done, calls = rig.run(*args)
        if not refused_line(done, calls):   # r5 (OLD: passed): not accepted for a write
            bad.append("macOS form read query not refused: " + host)
    for spelling in ("graphql", "/graphql", "https://api.github.com/graphql"):
        args = ("api", spelling, "-f", "query=" + R3_READ)
        done, calls = rig.run(*args)
        if [c["argv"] for c in calls] != [list(args)]:
            bad.append("read query not passed: " + spelling)
    return bad


def load_parser(source):
    """The shim's GraphQL tokenizer and reader (gql_tokens .. graphql_verdict), executed alone."""
    start, stop = source.index("class GqlError"), source.index("def api_refuse_unknown")
    space = {"re": re, "GRAPHQL_BODY_MUTATIONS": names_dict(source), "MUTATION_ALLOW": frozenset({"mergePullRequest"})}
    exec(source[start:stop], space)
    return space


def names_dict(source):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "GRAPHQL_BODY_MUTATIONS" for t in node.targets):
            return ast.literal_eval(node.value)


GQL_ACCEPTED = ("{ viewer { login } }", "query { viewer { login } }", "query Q { viewer { login } }", "query Q($n: Int!, $s: [String!] = [\"a\"]) { repository(owner: \"o\", name: \"r\") { issues(first: $n) { nodes { number } } } }",
                "query { a: viewer { l: login } }", "query { x(a: {b: [1, 2.5e3, -3], c: ENUM, d: true, e: null}) { y } }", "# mutation here\nquery { viewer { login } }",
                "query { x(s: \"a mutation \\\" \\u0041\") { y } }", 'query { x(s: """a mutation { b } \\""" c""") { y } }', "query { a, b, c }", "\ufeffquery{a}")
GQL_REJECTED = ("mutation { addComment(input: {}) { clientMutationId } }", "subscription { a }", "fragment F on User { login } query { ...F }", "query { ... on User { login } }",
                "query @skip(if: true) { a }", "query { a @include(if: true) }", "query { a } query { b }", "query A { a } mutation B { b }", 'query { x(s: "abc) { y } }', 'query { x(s: """abc) { y } }',
                "query { a { b }", "query { a } }", "", "   ", "# only a comment", "garbage", "query", "query {", "query { }", "query { a(: 1) }", "query { a(b: ) }", "query { a(b: 1x) }", "query { a } trailing",
                "query { a(b: \"\\q\") }", "{ a } { b }", "query ($x) { a }", "query { 1a }", "query { a ; }")


class TestGhShimEffectRule(unittest.TestCase):
    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def test_r4_the_battery_passes_for_the_real_shim(self):   # CONTROL of every mutant below
        self.assertEqual(effect_battery(self.rig), [])

    def test_r4_every_method_form_on_an_unknown_path_is_refused_with_one_line(self):
        for path in ("foo/bar", "https://example.com/repos/o/r/issues/1/comments"):
            for form in METHOD_FORMS + BODY_FORMS:
                with self.subTest(path=path, form=form):
                    done, calls = self.rig.run("api", path, *form, stdin="{}")
                    self.assertEqual(calls, [])
                    self.assertEqual(len(gh_err(done).splitlines()), 1, done.stderr)
                    self.assertTrue(gh_err(done).startswith("gh: refused: a gh api "), gh_err(done))
                    self.assertIn("allow-list in shims/gh", gh_err(done))

    def test_r4_the_same_unknown_paths_pass_as_a_get_without_a_body(self):
        for path in UNKNOWN_PATHS:
            for extra in ((), ("-X", "GET"), ("--method=GET", "-f", "a=b")):
                with self.subTest(path=path, extra=extra):
                    args = ("api", path) + extra
                    done, calls = self.rig.run(*args)
                    self.assertEqual([c["argv"] for c in calls], [list(args)], done.stderr)

    def test_r4_round_three_spellings_are_refused_with_a_mutation_and_with_a_body(self):
        for spelling in R3_GRAPHQL:
            for tail in (("-f", R4_MUTATION), ("-f", "body=x"), ("-X", "DELETE")):
                with self.subTest(spelling=spelling, tail=tail[-1][:8]):
                    done, calls = self.rig.run("api", spelling, *tail)
                    if spelling not in R3_NOT_GRAPHQL and spelling not in ("./graphql", "graphql/.", "graphql/x/..", "graphql%2f", "%2fgraphql", "gra%70hql",
                                                                           "gra%2570hql", "graphql;x", "//api.github.com/graphql", "graphql\\", "\\graphql",
                                                                           "http://api.github.com/graphql", "https://api.github.com:443/graphql",
                                                                           "https://api.github.com/x/../graphql") or tail[0] == "-f" and tail[1] == R4_MUTATION:
                        self.assertTrue(refused_line(done, calls), (spelling, tail, done.stderr))
                    # a canonical-graphql spelling with a body that is no query text, or no query: refused by the parser step too
                    if tail[1:] == ("body=x",) or tail == ("-X", "DELETE"):
                        self.assertTrue(refused_line(done, calls), (spelling, tail, done.stderr))

    def test_r4_macos_host_forms(self):
        for host in R4_MACOS:
            with self.subTest(host=host):
                done, calls = self.rig.run("api", host + "/graphql", "-f", R4_MUTATION)
                self.assertTrue(refused_line(done, calls), done.stderr)
                self.assertIn(RAW_LINE, gh_err(done))   # r5 (OLD: the mutation name): the host form is not accepted for a write
                done, calls = self.rig.run("api", host + "/repos/o/r/git/refs", "-f", "ref=x")
                self.assertTrue(refused_line(done, calls), done.stderr)
                args = ("api", host + "/graphql", "-f", "query=" + R3_READ)
                done, calls = self.rig.run(*args)
                self.assertTrue(refused_line(done, calls), done.stderr)   # r5 (OLD: passed untouched): a read query WITH a field is a POST write on that host form
                self.assertIn(RAW_LINE, gh_err(done))
                args = ("api", host + "/graphql")
                done, calls = self.rig.run(*args)   # a GET with no body passes
                self.assertEqual([c["argv"] for c in calls], [list(args)], done.stderr)
                args = ("api", host + "/repos/o/r/git/refs")
                done, calls = self.rig.run(*args)
                self.assertEqual([c["argv"] for c in calls], [list(args)], done.stderr)

    def test_r4_the_parser_accepts_only_what_it_reads(self):
        space = load_parser(self.source)
        for text in GQL_ACCEPTED:
            with self.subTest(accepted=text):
                self.assertEqual(space["graphql_verdict"](text), None)
                self.assertEqual(space["gql_operation"](text)[0], "query")
        for text in GQL_REJECTED:
            with self.subTest(rejected=text):
                self.assertTrue(space["graphql_verdict"](text), text)

    def test_r4_a_mutation_names_its_root_fields_and_only_the_merge_passes(self):
        space = load_parser(self.source)
        self.assertEqual(space["gql_operation"]("mutation { a: addComment(input: {}) { x } b: mergePullRequest(input: {}) { y } }"), ("mutation", ["addComment", "mergePullRequest"]))
        self.assertIsNone(space["graphql_verdict"]('mutation M($i: ID!) { mergePullRequest(input: {pullRequestId: $i}) { clientMutationId } }'))
        self.assertIn("deleteRepository", space["graphql_verdict"]("mutation { deleteRepository(input: {}) { x } }"))
        self.assertIn("addComment", space["graphql_verdict"]("mutation { mergePullRequest(input: {}) { x } addComment(input: {}) { x } }"))
        self.assertEqual(re.findall(r"^MUTATION_ALLOW = frozenset\(\{[^}]*\}\)", self.source, re.M), ['MUTATION_ALLOW = frozenset({"mergePullRequest"})'])
        done, calls = self.rig.run("api", "graphql", "-f", "query=mutation{mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}")
        self.assertEqual(len(calls), 1, done.stderr)
        for query in ("mutation{deleteRepository(input:{}){x}}", "subscription{a}", "query{a} query{b}", "fragment F on U {a} query{...F}"):
            with self.subTest(query=query):
                done, calls = self.rig.run("api", "graphql", "-f", "query=" + query)
                self.assertTrue(refused_line(done, calls), done.stderr)

    def mutant4(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old)
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        shim = rig.shims / "gh"
        shim.write_text(shim.read_text().replace(old, new, 1))
        return effect_battery(rig)

    def test_r4_mutant_1_the_allow_list_check_skipped_is_seen(self):
        seen = self.mutant4("        found = None\n        for label, pattern in SIGNED_API + SIGNED_API_MORE:", "        found = (\"x\", (\"POST\", \"PUT\", \"PATCH\", \"DELETE\"), False)\n        for label, pattern in SIGNED_API + SIGNED_API_MORE:")
        print("R4 mutant 1 seen:", len(seen))
        self.assertTrue(any(s.startswith("unknown+method passed") for s in seen) and any(s.startswith("unknown+body passed") for s in seen), seen[:5])

    def test_r4_mutant_2_implicit_post_treated_as_get_is_seen(self):
        seen = self.mutant4('("POST" if has_body else "GET")', '"GET"')
        print("R4 mutant 2 seen:", len(seen))
        self.assertTrue(any(s.startswith("unknown+body passed: foo/bar -f") for s in seen), seen[:5])
        self.assertFalse(any(s.startswith("unknown+method passed") and "-X" in s for s in seen), seen[:5])   # an explicit method is still seen

    def test_r4_mutant_3_the_parser_accepts_a_mutation_is_seen(self):
        seen = self.mutant4("        op = word\n", '        op = "query"\n')
        print("R4 mutant 3 seen:", len(seen))
        self.assertEqual(len(seen), 4, seen[:5])   # r5 (OLD: 24, the 19 round-3 spellings and the 5 macOS forms): the raw rule now refuses the unclean ones first; only the 4 raw-valid graphql spellings reach the parser
        self.assertTrue(all(s.startswith("round-3 spelling with query passed") for s in seen), seen[:5])
        # S9 per-name bar: under the mutant EVERY refused mutation name reaches the real gh (exactly 1 failure per name), on the real shim 0
        mrig = hermetic_rig()
        self.addCleanup(mrig.close)
        mshim = mrig.shims / "gh"
        mshim.write_text(mshim.read_text().replace("        op = word\n", '        op = "query"\n', 1))
        let_through = []
        for name in MUTATIONS:
            done, calls = mrig.run("api", "graphql", "-f", "query=" + gql(name))
            if calls:
                let_through.append(name)
        self.assertEqual(let_through, list(MUTATIONS), "each mutation name let through exactly once under the mutant: %s" % let_through)
        self.assertEqual(len(let_through), 8)
        for name in MUTATIONS:   # the control: the real shim refuses every name, 0 failures, the line names it
            done, calls = self.rig.run("api", "graphql", "-f", "query=" + gql(name))
            self.assertEqual(calls, [], name)
            self.assertIn(name, gh_err(done), name)
        space = load_parser(self.source.replace("        op = word\n", '        op = "query"\n', 1))
        self.assertIsNone(space["graphql_verdict"]("mutation { addComment(input: {}) { x } }"))   # the mutant reads a mutation as a query
        self.assertTrue(load_parser(self.source)["graphql_verdict"]("mutation { addComment(input: {}) { x } }"))   # control

    def test_r4_mutant_4_the_unknown_body_refusal_removed_is_seen(self):
        seen = self.mutant4("        if found is None:\n            for text in (f.partition", "        if found is None:\n            continue\n            for text in (f.partition")
        print("R4 mutant 4 seen:", len(seen))
        self.assertTrue(any(s.startswith("unknown+body passed") for s in seen) and any(s.startswith("unknown+method passed") for s in seen), seen[:5])

    def test_r4_mutant_5_canonicalisation_skipped_is_seen(self):
        seen = self.mutant4('    if rest.startswith("/"):\n        rest = rest[1:]', '    if False:\n        rest = rest[1:]')   # r5 (OLD: canonicalisation skipped): the one leading slash is no longer accepted
        print("R4 mutant 5 seen:", len(seen))
        self.assertIn("read query not passed: /graphql", seen)
        self.assertIn("read query not passed: https://api.github.com/graphql", seen)


class TestGhShimMutationRoots(unittest.TestCase):
    """#1696 r4 addendum: the root fields of the single mutation are judged by the FIELD name (never an alias), each one."""
    MERGE = 'mergePullRequest(input:{pullRequestId:"x"}){clientMutationId}'

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def verdicts(self, source):
        space = load_parser(source)
        return [space["graphql_verdict"]("mutation{%s}" % body) for body in (
            "a: " + self.MERGE, "a: %s b: %s" % (self.MERGE, self.MERGE), "m: addComment(input:{}){x}", "mergePullRequest: addComment(input:{}){x}",
            "%s addComment(input:{}){x}" % self.MERGE, "addComment(input:{}){x} %s" % self.MERGE, "x: %s y: deleteRepository(input:{}){z}" % self.MERGE)]

    def test_alias_and_several_roots_are_judged_by_each_field(self):
        got = self.verdicts(self.source)
        self.assertIsNone(got[0])
        self.assertIsNone(got[1])
        for i in (2, 3, 4, 5):
            self.assertIn("addComment", got[i] or "", i)
        self.assertIn("deleteRepository", got[6] or "")
        self.assertNotIn("mergePullRequest is refused", got[6] or "")

    def test_the_same_through_the_shim(self):
        for body, passes in (("a: " + self.MERGE, True), ("m: addComment(input:{}){x}", False), ("mergePullRequest: addComment(input:{}){x}", False),
                             ("a: %s b: deleteRepository(input:{}){x}" % self.MERGE, False), ("deleteRepository(input:{}){x} %s" % self.MERGE, False)):
            with self.subTest(body=body):
                done, calls = self.rig.run("api", "graphql", "-f", "query=mutation{%s}" % body)
                if passes:
                    self.assertEqual(len(calls), 1, done.stderr)
                else:
                    self.assertTrue(refused_line(done, calls), done.stderr)

    def mutated(self, old, new):
        self.assertEqual(self.source.count(old), 1, old)
        return self.verdicts(self.source.replace(old, new, 1))

    def test_mutant_alias_judged_by_the_alias_is_seen(self):
        got = self.mutated('                take()\n                name = take("name")[1]\n            roots.append(name)', '                take()\n                take("name")\n            roots.append(name)')
        self.assertEqual([i for i, v in enumerate(got) if v != self.verdicts(self.source)[i]], [0, 1, 2, 3, 6])   # the alias is judged: a: merge refused, m: addComment passes, mergePullRequest: addComment passes

    def test_mutant_only_the_first_root_judged_is_seen(self):
        got = self.mutated("    return op, roots\n", "    return op, roots[:1]\n")
        self.assertEqual([i for i, v in enumerate(got) if v != self.verdicts(self.source)[i]], [4, 6])


if __name__ == "__main__":
    unittest.main()


# ---- #1696 round 5: the shim matches the RAW path for a write (it never normalises toward a path the server will not use).
READER_FORMS = ("repos/o/r/contents/x\\..\\..\\pulls/1/merge", "repos/o/r/contents/x%5c..%5c..%5cpulls/1/merge", "repos/o/r/contents/x%252f..%252f..%252fpulls/1/merge")
CLEANING_CLASSES = {"backslash": "repos/o/r/pulls/1/merge\\", "percent": "repos/o/r/pulls/1/%6derge", "dot": "repos/o/r/pulls/./1/merge", "dotdot": "repos/o/r/x/../pulls/1/merge",
                    "semicolon": "repos/o/r/pulls/1/merge;x", "tab": "repos/o/r/pulls/1/merge\t", "newline": "repos/o/r/pulls/1/merge\n", "space": "repos/o/r/pulls/1/merge ",
                    "hash": "repos/o/r/pulls/1/merge#x", "double slash": "repos/o/r//pulls/1/merge", "trailing slash": "repos/o/r/pulls/1/merge/",
                    "userinfo host": "https://u@api.github.com/repos/o/r/pulls/1/merge", "port host": "https://api.github.com:443/repos/o/r/pulls/1/merge",
                    "dot host": "https://api.github.com./repos/o/r/pulls/1/merge", "upper host": "https://API.github.com/repos/o/r/pulls/1/merge"}
PLAIN_WRITES = ("repos/o/r/pulls/1/merge", "/repos/o/r/pulls/1/merge", "https://api.github.com/repos/o/r/pulls/1/merge", "http://api.github.com/repos/o/r/pulls/1/merge",
                "//api.github.com/repos/o/r/pulls/1/merge", "repos/o/r/pulls/1/merge?x=1%20y")


def raw_line(done, calls, method="PUT"):
    """The one raw-rule refusal: exactly one gh: line, anchored, and no call made."""
    text = gh_err(done)
    return not calls and len(text.splitlines()) == 1 and text.startswith("gh: refused: a gh api %s with a body" % method) and text.endswith("): " + RAW_LINE)


class TestGhShimRawPath(unittest.TestCase):
    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def reader_failures(self, rig):
        return [form for form in READER_FORMS if not raw_line(*rig.run("api", "-X", "PUT", form, "-f", "message=m"))]

    def mutant_rig(self, *pairs):
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        shim = rig.shims / "gh"
        text = shim.read_text()
        for old, new in pairs:
            self.assertEqual(text.count(old), 1, "the mutant's anchor is gone: " + old)
            text = text.replace(old, new, 1)
        shim.write_text(text)
        return rig

    def test_the_three_reader_forms_are_refused_with_no_call(self):
        for form in READER_FORMS:
            with self.subTest(form=form):
                done, calls = self.rig.run("api", "-X", "PUT", form, "-f", "message=m")
                self.assertTrue(raw_line(done, calls), (gh_err(done), calls))
        self.assertEqual(self.reader_failures(self.rig), [])   # control: 0

    def test_every_cleaning_class_is_refused_for_a_write(self):
        for name, path in CLEANING_CLASSES.items():
            with self.subTest(cls=name):
                done, calls = self.rig.run("api", "-X", "PUT", path, "-f", "merge_method=squash")
                self.assertTrue(raw_line(done, calls), (gh_err(done), calls))

    def test_the_plain_raw_paths_still_pass(self):
        for path in PLAIN_WRITES:
            with self.subTest(path=path):
                args = ("api", "-X", "PUT", path, "-f", "merge_method=squash")
                done, calls = self.rig.run(*args)
                self.assertEqual([c["argv"] for c in calls], [list(args)], gh_err(done))
        for path in ("repos/o/r/issues/1/comments", "/repos/o/r/issues/1/comments", "https://api.github.com/repos/o/r/issues/1/comments"):
            with self.subTest(signed=path):
                done, calls = self.rig.run("api", path, "-f", "body=x")
                self.assertEqual(len(calls), 1, gh_err(done))
                self.assertEqual(calls[0]["argv"][-1].count(SIGNED), 1)

    def test_a_get_without_a_body_passes_on_any_path(self):
        for path in list(CLEANING_CLASSES.values()) + list(READER_FORMS):
            for extra in ((), ("-X", "GET"), ("--method=get",)):
                with self.subTest(path=path, extra=extra):
                    args = ("api", path) + extra
                    done, calls = self.rig.run(*args)
                    self.assertEqual([c["argv"] for c in calls], [list(args)], gh_err(done))

    def test_get_graphql_with_a_field_is_refused_and_without_passes(self):
        query = "query=query { viewer { login } }"
        forms = (("-X", "GET", "-f", query), ("--method=get", "-f", query), ("--method", "get", "-F", query), ("-XGET", "--field=" + query), ("-X", "GET", "--raw-field", query),
                 ("-X", "GET", "--input", self.input_file('{"query":"query{a}"}')))
        for form in forms:
            with self.subTest(form=form):
                done, calls = self.rig.run("api", "graphql", *form)
                self.assertEqual(calls, [])
                self.assertEqual(len(gh_err(done).splitlines()), 1, done.stderr)
                self.assertTrue(gh_err(done).startswith("gh: refused: a GET to graphql that carries a field or --input is refused"), gh_err(done))
        for form in ((), ("-X", "GET"), ("--method=get",), ("-X", "get")):
            with self.subTest(plain=form):
                args = ("api", "graphql") + form
                done, calls = self.rig.run(*args)
                self.assertEqual([c["argv"] for c in calls], [list(args)], gh_err(done))

    def input_file(self, text):
        path = self.rig.home / ("in%d.json" % len(list(self.rig.home.glob("in*.json"))))
        path.write_text(text)
        return str(path)

    def test_a_non_object_input_on_graphql_is_refused(self):
        for text in ('[{"query":"mutation{a}"}]', "[]", '"x"', "3", "null"):
            with self.subTest(text=text):
                done, calls = self.rig.run("api", "graphql", "--input", self.input_file(text))
                self.assertEqual(calls, [])
                self.assertEqual(gh_err(done), "gh: refused: the GraphQL --input is not a JSON object, so it is refused; only one query object passes")
        done, calls = self.rig.run("api", "graphql", "--input", self.input_file('{"query":"query{a}"}'))   # control: an object passes
        self.assertEqual(len(calls), 1, gh_err(done))

    def override_forms(self):
        for name in ("X-HTTP-Method-Override", "X-HTTP-Method", "X-Method-Override"):
            for shown in (name, name.lower(), name.upper(), name.swapcase()):
                yield name, ("-H", shown + ": DELETE")
                yield name, ("-H" + shown + ":PUT",)
                yield name, ("--header", shown + ":PATCH")
                yield name, ("--header=" + shown + ": x",)

    def override_failures(self, rig):
        text = "gh: refused: a method-override header (X-HTTP-Method-Override, X-HTTP-Method, X-Method-Override) is refused: it can turn a read into a write, so the shim cannot tell the real method"
        bad = []
        for name, form in self.override_forms():
            for extra in ((), ("-f", "a=b"), ("-X", "POST", "-f", "a=b")):   # a GET and a POST
                done, calls = rig.run("api", "repos/o/r/issues", *form, *extra)
                if calls or gh_err(done) != text:
                    bad.append((form, extra))
        return bad

    def test_the_method_override_headers_are_refused(self):
        self.assertEqual(self.override_failures(self.rig), [])
        args = ("api", "repos/o/r/issues", "-H", "Accept: application/json")   # control
        done, calls = self.rig.run(*args)
        self.assertEqual([c["argv"] for c in calls], [list(args)], gh_err(done))

    def test_mutant_override_check_skipped_is_seen(self):
        rig = self.mutant_rig(("in OVERRIDE_HEADERS:", "in ():"))
        seen = self.override_failures(rig)
        print("RAW mutant override check skipped: %d seen" % len(seen))
        self.assertEqual(len(seen), len(list(self.override_forms())) * 3)

    def test_the_graphql_hint_names_a_form_the_shim_accepts(self):
        done, calls = self.rig.run("api", "graphql", "-f", "query=" + gql("updatePullRequestReview"))
        self.assertEqual(calls, [])
        self.assertEqual(gh_err(done), "gh: refused: the GraphQL mutation updatePullRequestReview writes a body that cannot be signed and nothing was posted; use "
                                       "gh pr comment, gh issue comment or POST repos/OWNER/REPO/issues/N/comments")
        self.assertNotIn("PUT repos/OWNER/REPO/pulls", gh_err(done))
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "-X", "POST", "-f", "body=x")   # the hint's own form is accepted (and signed)
        self.assertEqual(len(calls), 1)

    def test_mutant_canonicaliser_in_the_decision_fails_the_three_reader_forms(self):
        rig = self.mutant_rig(("        path = api_raw_path(raw)\n", "        path = api_endpoint(raw)\n"))
        failing = self.reader_failures(rig)
        print("RAW mutant canonicaliser: %d of %d reader forms fail under the mutant; control %d" % (len(failing), len(READER_FORMS), len(self.reader_failures(self.rig))))
        self.assertEqual(failing, list(READER_FORMS))   # 3 of 3, each named
        for form in READER_FORMS:
            done, calls = rig.run("api", "-X", "PUT", form, "-f", "message=m")
            self.assertEqual(len(calls), 1, form)   # the mutant lets the call through
        self.assertEqual(self.reader_failures(self.rig), [])   # control 0

    def test_mutant_raw_rule_skipped_for_writes_is_seen(self):
        rig = self.mutant_rig(('if "#" in rest or', 'if False or'), ('if "//" in path or', 'if False or'), ('path.endswith("/")', 'False'),
                              ('or not path or any(seg in (".", "..") for seg in path.split("/"))', 'or not path'), ('for c in "\\\\%;"', 'for c in ""'),
                              ('any(ord(c) <= 0x20 or ord(c) == 0x7f for c in path)', 'False'))
        seen = [name for name, path in CLEANING_CLASSES.items() if not raw_line(*rig.run("api", "-X", "PUT", path, "-f", "merge_method=squash"))]
        print("RAW mutant rule skipped: %d of %d classes seen" % (len(seen), len(CLEANING_CLASSES)))
        for name in ("backslash", "percent", "dot", "dotdot", "semicolon", "tab", "newline", "space", "hash", "double slash", "trailing slash"):
            self.assertIn(name, seen)

    def test_mutant_get_graphql_with_a_field_passes_is_seen(self):
        rig = self.mutant_rig(('        if has_body and any(api_endpoint(p) == "graphql" for p in raw_positionals):', '        if False and any(api_endpoint(p) == "graphql" for p in raw_positionals):'))
        done, calls = rig.run("api", "graphql", "-X", "GET", "-f", "query=query{a}")
        self.assertEqual(len(calls), 1)   # the mutant lets it through; the real shim (test above) refuses
        done, calls = self.rig.run("api", "graphql", "-X", "GET", "-f", "query=query{a}")
        self.assertEqual(calls, [])


EQ_ANCHOR = "gh: refused: the short option "
Q_ANCHOR = "gh: refused: a '?' (URL query string) on the graphql endpoint is refused"
COMMENTS = "repos/o/r/issues/1/comments"
SHORT_EQ_FORMS = {   # name -> argv after api
    "-f=": (COMMENTS, "-f=body=x"), "-F=": (COMMENTS, "-F=body=@/dev/null"), "-X=": (COMMENTS, "-X=POST", "-f", "body=x"),
    "-H=": (COMMENTS, "-H=Accept:x", "-f", "body=x"), "-H= override": ("repos/o/r/issues/1", "-X", "POST", "-H=X-HTTP-Method-Override: PATCH", "-f", "title=x"),
    "-i=": (COMMENTS, "-i=", "-f", "body=x"), "-R=": (COMMENTS, "-R=o/r", "-f", "body=x"),
    "-p= (value-taking)": (COMMENTS, "-p=x"), "-q= (value-taking)": (COMMENTS, "-q=x"), "-t= (value-taking)": (COMMENTS, "-t=x"),
    "-i= (boolean)": ("repos/o/r/issues", "-i="), "-h= (boolean)": ("repos/o/r/issues", "-h="),
}
QUERY_FORMS = {
    "mutation in url query, POST": ("graphql?query=mutation{x}", "-X", "POST", "-f", "query={viewer{login}}"),
    "mutation in url query, bare GET": ("graphql?query=mutation{x}",),
    "any ? with a read query": ("graphql?x=1", "-f", "query=query{viewer{login}}"),
}


class TestGhShimRound5(unittest.TestCase):
    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)

    def mutant_rig(self, old, new):
        rig = hermetic_rig()
        self.addCleanup(rig.close)
        shim = rig.shims / "gh"
        text = shim.read_text()
        self.assertEqual(text.count(old), 1, "the mutant's anchor is gone: " + old)
        shim.write_text(text.replace(old, new, 1))
        return rig

    def failing(self, rig, forms, anchor):
        bad = []
        for name, argv in forms.items():
            done, calls = rig.run("api", *argv)
            if calls or not gh_err(done).startswith(anchor):
                bad.append(name)
        return bad

    def test_short_options_with_an_attached_equals_are_refused(self):
        self.assertEqual(self.failing(self.rig, SHORT_EQ_FORMS, EQ_ANCHOR), [])
        done, calls = self.rig.run("api", COMMENTS, "-i=")
        self.assertEqual(gh_err(done).count("\n"), 0)
        self.assertIn("-i=", gh_err(done))

    def test_the_long_forms_and_plain_forms_still_work(self):
        done, calls = self.rig.run("api", COMMENTS, "--field=body=x")
        self.assertEqual(len(calls), 1, gh_err(done))
        self.assertEqual(calls[0]["argv"][-1].count(SIGNED), 1)
        done, calls = self.rig.run("api", COMMENTS, "-f", "body=x")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"][-1].count(SIGNED), 1)
        done, calls = self.rig.run("api", COMMENTS, "--method=POST", "-f", "body=x")
        self.assertEqual(len(calls), 1, gh_err(done))
        done, calls = self.rig.run("api", "repos/o/r/issues", "--header=Accept:x")
        self.assertEqual(len(calls), 1, gh_err(done))

    def test_graphql_with_any_question_mark_is_refused(self):
        self.assertEqual(self.failing(self.rig, QUERY_FORMS, Q_ANCHOR), [])

    def test_plain_graphql_read_passes(self):
        done, calls = self.rig.run("api", "graphql", "-f", "query=query{viewer{login}}")
        self.assertEqual(len(calls), 1, gh_err(done))

    def test_mutant_short_equals_refusal_removed_is_seen(self):
        rig = self.mutant_rig('SHORT_EQ_RE = re.compile(r"^-[A-Za-z]=")', 'SHORT_EQ_RE = re.compile(r"^\\0$")')
        seen = self.failing(rig, SHORT_EQ_FORMS, EQ_ANCHOR)
        print("R5 mutant short '=' refusal removed: %d of %d forms fail: %s; control %d" % (len(seen), len(SHORT_EQ_FORMS), seen, len(self.failing(self.rig, SHORT_EQ_FORMS, EQ_ANCHOR))))
        self.assertEqual(seen, list(SHORT_EQ_FORMS))
        self.assertEqual(self.failing(self.rig, SHORT_EQ_FORMS, EQ_ANCHOR), [])

    def test_mutant_graphql_question_mark_refusal_removed_is_seen(self):
        rig = self.mutant_rig('if "?" in raw and api_endpoint(raw) == "graphql":', 'if False:')
        seen = self.failing(rig, QUERY_FORMS, Q_ANCHOR)
        print("R5 mutant graphql '?' refusal removed: %d of %d forms fail: %s" % (len(seen), len(QUERY_FORMS), seen))
        self.assertEqual(seen, list(QUERY_FORMS))
        self.assertEqual(self.failing(self.rig, QUERY_FORMS, Q_ANCHOR), [])
