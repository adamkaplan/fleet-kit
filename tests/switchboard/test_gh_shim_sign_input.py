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
               "repos%2fo%2fr%2fissues%2f1%2fcomments", "repos/o/r/issues/1/comm%65nts", "repos/o/r/issues/1/comments;x", "repos/o/r/issues/1/comments?path=b%20c",
               "https://api.github.com/./repos/o/r/issues/1/comments", "http://api.github.com/repos/o/r/issues/1/comments",
               "//api.github.com/repos/o/r/issues/1/comments", "repos\\o\\r\\issues\\1\\comments", "Repos/o/r/issues/1/comments", "repos/o/r/issues/1/comm%2565nts")
R3_UNKNOWN = ("foo/bar", "%zz/graphql", "repos%", "x%e9", "weird")
R3_TEXT = "gh: refused: the GraphQL mutation addComment writes a body that cannot be signed and nothing was posted; use gh pr comment, gh issue comment or POST repos/OWNER/REPO/issues/N/comments"
R3_READ = "query { viewer { login } }"
RAW_LINE = ("the endpoint would change if cleaned: the shim matches the raw path only; write it plainly as repos/OWNER/REPO/... "
            "(one optional leading slash, no backslash, %-escape, dot segment, semicolon, space, #, double or trailing slash)")
R5_RAW_OK_GRAPHQL = ("//api.github.com/graphql", "http://api.github.com/graphql")   # r5 (OLD: all 21 spellings reached the graphql parser): only these two are raw-valid
R5_RAW_OK_COMMENTS = ("/repos/o/r/issues/1/comments", "repos/o/r/issues/1/comments?path=b%20c", "http://api.github.com/repos/o/r/issues/1/comments", "//api.github.com/repos/o/r/issues/1/comments")
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
        text = '{"event":"COMMENT",\n "side": "\u00e9"}'
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


Q_ANCHOR = "gh: refused: a '?' (URL query string) on the graphql endpoint is refused"
COMMENTS = "repos/o/r/issues/1/comments"
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


    def test_mutant_graphql_question_mark_refusal_removed_is_seen(self):
        rig = self.mutant_rig('if "?" in raw and api_endpoint(raw) == "graphql":', 'if False:')
        seen = self.failing(rig, QUERY_FORMS, Q_ANCHOR)
        print("R5 mutant graphql '?' refusal removed: %d of %d forms fail: %s" % (len(seen), len(QUERY_FORMS), seen))
        self.assertEqual(seen, list(QUERY_FORMS))
        self.assertEqual(self.failing(self.rig, QUERY_FORMS, Q_ANCHOR), [])






# ---- #1696 round 6: the DIFFERENTIAL TABLE of the one parser of gh api's argv (pflag grammar, gh 2.103.3).
PARSE_ANCHOR = "gh: refused: "
COMMENTS = "repos/o/r/issues/1/comments"
OVERRIDE_NAMES = ("X-HTTP-Method-Override", "X-HTTP-Method", "X-Method-Override")


def load_argv_parser(source):
    """The shim's flag table and parse_api_argv, executed alone."""
    start, stop = source.index("GH_API_PINNED = "), source.index("def api_parse(")
    class Refused(Exception):
        pass
    space = {"Refused": Refused, "_clean_reason": lambda text: text}
    exec(source[start:stop], space)
    return space


def old_scan(args):
    """The OLD reading of the argv (api_scan of ea4298a4, before the parser): (method, field texts, positionals); used as the mutant."""
    method, fields, positionals, i = None, [], [], 1
    longs = {"--method": "m", "--raw-field": "f", "--field": "f", "--input": "i", "--header": "x", "--jq": "x", "--template": "x", "--hostname": "x", "--cache": "x", "--preview": "x"}
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            positionals.extend(args[i:])
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            kind = longs.get(name)
            if kind is None:
                continue
            if not eq:
                value = args[i] if i < len(args) else None
                i += 1
            if kind == "m":
                method = "" if value is None else value.upper()
            elif kind == "f" and value is not None:
                fields.append(value)
        elif arg.startswith("-") and len(arg) > 1:
            for pos, ch in enumerate(arg[1:], 1):
                if ch in "XHfFqtp":
                    value = arg[pos + 1:] or None
                    if value is None:
                        value = args[i] if i < len(args) else None
                        i += 1
                    if ch == "X":
                        method = "" if value is None else value.upper()
                    elif ch in "fF" and value is not None:
                        fields.append(value)
                    break
        else:
            positionals.append(arg)
    return method, fields, positionals


def view_new(space, tail):
    """(method, [field texts], [header lines], [positionals]) from the shim's parser, or None when it refuses."""
    try:
        got = space["parse_api_argv"](["api"] + list(tail))
    except space["Refused"]:
        return None
    return got.method, [f[3] for f in got.fields], ["%s:%s" % h for h in got.headers], [p for _i, p in got.positionals]


def view_old(tail):
    method, fields, positionals = old_scan(["api"] + list(tail))
    return method, fields, None, positionals


def table_cases():
    """(label, argv after api, expected (method, field texts, headers, positionals) or None when refused, effect: signed / pass / refused)."""
    c = []
    def add(label, tail, method, fields, headers, pos, effect):
        c.append((label, tuple(tail), None if method == "refused" else (method, fields, headers, pos), effect))
    for form in ("-f=body=x", "-ifbody=x", "-fbody=x"):
        add(form, (COMMENTS, form), None, ["body=x"], [], [COMMENTS], "signed")
    add("-if=body=x", (COMMENTS, "-if=body=x"), None, ["body=x"], [], [COMMENTS], "signed")
    add("-f body=x", (COMMENTS, "-f", "body=x"), None, ["body=x"], [], [COMMENTS], "signed")
    add("-f= (value '=')", (COMMENTS, "-f=", "-f", "body=x"), None, ["=", "body=x"], [], [COMMENTS], "refused")
    add("-F=body=@file", (COMMENTS, "-F=body=@{FILE}"), None, ["body=@{FILE}"], [], [COMMENTS], "signed")
    for form in ("-iX=POST", "-XPOST", "-X=POST"):
        add(form, (COMMENTS, form), "POST", [], [], [COMMENTS], "pass")
    add("-X POST", (COMMENTS, "-X", "POST"), "POST", [], [], [COMMENTS], "pass")
    add("--method=POST", (COMMENTS, "--method=POST"), "POST", [], [], [COMMENTS], "pass")
    add("--method POST", (COMMENTS, "--method", "POST"), "POST", [], [], [COMMENTS], "pass")
    add("--field=body=x", (COMMENTS, "--field=body=x"), None, ["body=x"], [], [COMMENTS], "signed")
    add("--field body=x", (COMMENTS, "--field", "body=x"), None, ["body=x"], [], [COMMENTS], "signed")
    add("--raw-field=body=x", (COMMENTS, "--raw-field=body=x"), None, ["body=x"], [], [COMMENTS], "signed")
    add("--raw-field body=x", (COMMENTS, "--raw-field", "body=x"), None, ["body=x"], [], [COMMENTS], "signed")
    add("options after the endpoint", (COMMENTS, "-X", "POST", "-f", "body=x"), "POST", ["body=x"], [], [COMMENTS], "signed")
    add("options before the endpoint", ("-f", "body=x", COMMENTS), None, ["body=x"], [], [COMMENTS], "signed")
    add("-X twice, last wins (POST)", (COMMENTS, "-X", "GET", "-X", "POST"), "POST", [], [], [COMMENTS], "pass")
    add("-X twice, last wins (GET)", (COMMENTS, "-X", "POST", "-X", "GET", "-f", "body=x"), "GET", ["body=x"], [], [COMMENTS], "pass")
    add("-f twice (stringArray)", (COMMENTS, "-f", "body=a", "-f", "body=b"), None, ["body=a", "body=b"], [], [COMMENTS], "signed")
    add("-- then a flag-looking positional", ("--", "-f"), None, [], [], ["-f"], "pass")
    add("-- then the endpoint", ("--", COMMENTS, ), None, [], [], [COMMENTS], "pass")
    add("-- before a field-looking word", (COMMENTS, "--", "-f"), "refused", None, None, None, "refused")
    add("lone - is a positional", ("-",), None, [], [], ["-"], "pass")
    add("-i then -h booleans", (COMMENTS, "-ih"), None, [], [], [COMMENTS], "pass")
    add("--include=true", (COMMENTS, "--include=true"), None, [], [], [COMMENTS], "pass")
    add("--include=false", (COMMENTS, "--include=false"), None, [], [], [COMMENTS], "pass")
    add("-i=true", (COMMENTS, "-i=true"), None, [], [], [COMMENTS], "pass")
    add("a boolean with a non-bool value", (COMMENTS, "--include=maybe"), "refused", None, None, None, "refused")
    add("a short boolean with a non-bool value", (COMMENTS, "-i=maybe"), "refused", None, None, None, "refused")
    add("a missing value (-f)", (COMMENTS, "-f"), "refused", None, None, None, "refused")
    add("a missing value (--method)", (COMMENTS, "--method"), "refused", None, None, None, "refused")
    add("an unknown flag", (COMMENTS, "--bogus"), "refused", None, None, None, "refused")
    add("a long-name abbreviation", (COMMENTS, "--meth=POST"), "refused", None, None, None, "refused")
    add("an unknown short in a cluster", (COMMENTS, "-iz"), "refused", None, None, None, "refused")
    add("-R after api", (COMMENTS, "-R", "o/r"), "refused", None, None, None, "refused")
    add("--repo after api", (COMMENTS, "--repo=o/r"), "refused", None, None, None, "refused")
    add("two positionals", (COMMENTS, "repos/o/r/issues"), "refused", None, None, None, "refused")
    for name in OVERRIDE_NAMES:
        low = name.lower()
        for label, tail in (("-H joined", ("-H" + name + ":PATCH",)), ("-H separate", ("-H", name + ": PATCH")), ("--header=", ("--header=" + name + ":PATCH",)),
                            ("--header separate", ("--header", name + ":PATCH")), ("-H=", ("-H=" + name + ":PATCH",)), ("-iH joined", ("-iH" + name + ":PATCH",)),
                            ("-iH=", ("-iH=" + name + ":PATCH",))):
            add("%s %s" % (label, name), (COMMENTS,) + tail + ("-f", "body=x"), None, ["body=x"], ["%s:PATCH" % low], [COMMENTS], "refused")
    return c


def decide_effect(rig, tail, tmp):
    """'signed' | 'pass' | 'refused' for the real shim (and the refusal's anchored one line); 'bad' otherwise."""
    args = ("api",) + tuple(tail)
    done, calls = rig.run(*args, stdin="")
    if not calls:
        return "refused" if gh_err(done).startswith(PARSE_ANCHOR) and len(gh_err(done).splitlines()) == 1 else "bad: " + gh_err(done)[:80]
    if len(calls) != 1:
        return "bad: %d calls" % len(calls)
    argv = calls[0]["argv"]
    if argv == list(args):
        return "pass"
    words = [w for w in argv if SIGNED in w]
    if not words and any("body=@" in w for w in argv) and argv[:2] == list(args[:2]):   # -F body=@file: signed text goes to a temp file
        return "signed"
    return "signed" if words and all(w.count(SIGNED) == 1 for w in words) else "bad: " + " ".join(argv)[:80]


OLD_MISREADS = ('-- before a field-looking word', '--repo after api', '-F=body=@file', '-R after api', '-X=POST', '-f=body=x', '-iX=POST', '-if=body=x', 'a boolean with a non-bool value',
                'a long-name abbreviation', 'a missing value (--method)', 'a missing value (-f)', 'a short boolean with a non-bool value', 'an unknown flag', 'an unknown short in a cluster', 'two positionals')


class TestGhShimArgvParser(unittest.TestCase):
    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()
        self.cases = table_cases()
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="argvtable-")
        self.file = os.path.join(self.dir, "b.txt")
        with open(self.file, "w") as fh:
            fh.write("from a file")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def fill(self, tail):
        return tuple(t.replace("{FILE}", self.file) for t in tail)

    def parse_failures(self, view, cases=None):
        bad = []
        for label, tail, expected, _effect in (cases or self.cases):
            tail = self.fill(tail)
            want = None if expected is None else (expected[0], [x.replace("{FILE}", self.file) for x in expected[1]], expected[2], [x.replace("{FILE}", self.file) for x in expected[3]])
            got = view(tail)
            if got is not None and want is not None and got[2] is None:   # the old reading has no header list: compare the other three
                ok = (got[0], got[1], got[3]) == (want[0], want[1], want[3])
            else:
                ok = (got is None) == (want is None) and (want is None or tuple(got) == want)
            if not ok:
                bad.append("%s: parsed %r, expected %r" % (label, got, want))
        return bad

    def test_control_the_parser_reads_every_form_as_expected(self):
        space = load_argv_parser(self.source)
        self.assertEqual(self.parse_failures(lambda tail: view_new(space, tail)), [])

    def test_control_the_shim_decides_every_form_by_effect(self):
        bad = []
        for label, tail, _expected, effect in self.cases:
            got = decide_effect(self.rig, self.fill(tail), self.dir)
            if got != effect:
                bad.append("%s: %s, expected %s" % (label, got, effect))
        self.assertEqual(bad, [])

    def test_the_flag_table_is_the_pinned_one(self):
        space = load_argv_parser(self.source)
        self.assertEqual(space["GH_API_PINNED"], "2.103.3")
        self.assertEqual(sorted(n for n, _s, _v in space["GH_API_FLAGS"]), sorted(
            "allow-escape-sequences cache field header hostname include input jq method paginate preview raw-field silent slurp template verbose help".split()))
        self.assertEqual({n: (s, v) for n, s, v in space["GH_API_FLAGS"] if s}, {"field": ("F", True), "header": ("H", True), "include": ("i", False), "jq": ("q", True), "method": ("X", True),
                                                                                    "preview": ("p", True), "raw-field": ("f", True), "template": ("t", True), "help": ("h", False)})

    def test_mutant_the_old_reading_fails_the_forms_it_misreads(self):
        seen = self.parse_failures(view_old)
        labels = sorted(s.split(": parsed")[0] for s in seen)
        print("R6 old-reading mutant: %d forms fail: %s" % (len(labels), labels))
        for s in seen:
            self.assertIn(": parsed ", s)
        self.assertEqual(labels, sorted(OLD_MISREADS))
        self.assertEqual(self.parse_failures(lambda tail: view_new(load_argv_parser(self.source), tail)), [])   # CONTROL: 0 failures

    def mutant_space(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old)
        return load_argv_parser(self.source.replace(old, new))

    def check_mutant(self, name, space, must):
        seen = self.parse_failures(lambda tail: view_new(space, tail))
        print("R6 mutant %s: %d forms fail" % (name, len(seen)))
        self.assertTrue(seen, name)
        for label in must:
            self.assertTrue(any(s.startswith(label + ": parsed") for s in seen), (name, label))

    def test_mutant_f_marked_boolean_is_seen(self):
        self.check_mutant("-f boolean", self.mutant_space('("raw-field", "f", True)', '("raw-field", "f", False)'), ["-f body=x", "-fbody=x"])

    def test_mutant_double_dash_rule_skipped_is_seen(self):
        old = '        if arg == "--":\n            out.positionals.extend((j, args[j]) for j in range(i + 1, len(args)))\n            break\n'
        self.check_mutant("-- skipped", self.mutant_space(old, ""), ["-- then the endpoint", "-- then a flag-looking positional"])

    def test_mutant_unknown_flags_passed_is_seen(self):
        old = '                _api_refuse(arg, "unknown flag --%s" % name)\n'
        self.check_mutant("unknown flags passed", self.mutant_space(old, "                i += 1\n                continue\n"), ["an unknown flag"])



# ---- the older readers of gh api argv are views of parse_api_argv (#1696 round 7) ----

MERGE = "repos/o/r/pulls/5/merge"
ISSUE = "repos/o/r/issues/1"
GRAPHQL_MERGE = "mutation{mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}"
API_READER_FORMS = {
    # label: (argv, accepted)
    "-X PUT": (["api", "-X", "PUT", MERGE], True),
    "-XPUT": (["api", "-XPUT", MERGE], True),
    "--method=PUT": (["api", "--method=PUT", MERGE], True),
    "--method put": (["api", "--method", "put", MERGE], True),
    "-fsha=1": (["api", "-fsha=1", MERGE], True),
    "-fa=b with -Xput": (["api", "-fa=b", "-Xput", MERGE], True),
    "-i -XPUT": (["api", "-i", "-XPUT", MERGE], True),
    "-ifoo -XPUT": (["api", "-ifoo", "-XPUT", MERGE], True),
    "-pXPUT": (["api", "-pXPUT", MERGE], True),
    "--cache 1h": (["api", "--cache", "1h", MERGE], True),
    "--cache=1h --input=x": (["api", "--cache=1h", "--input=x", MERGE], True),
    "-- then a positional": (["api", "--", MERGE], True),
    "--silent --field": (["api", "--silent", "--field", "a=b", MERGE], True),
    "-X GET -f a=b": (["api", "-X", "GET", "-f", "a=b", MERGE], True),
    "-X PUT then -X GET": (["api", "-X", "PUT", "-X", "GET", MERGE], True),
    "-X GET then -X PUT": (["api", "-X", "GET", "-X", "PUT", MERGE], True),
    "plain read": (["api", MERGE], True),
    "graphql -f": (["api", "graphql", "-f", "query=" + GRAPHQL_MERGE], True),
    "graphql -F": (["api", "graphql", "-F", "query=" + GRAPHQL_MERGE], True),
    "graphql --raw-field": (["api", "graphql", "--raw-field", "query=" + GRAPHQL_MERGE], True),
    "graphql --input": (["api", "graphql", "--input", "q.json"], True),
    "-R api api": (["-R", "api", "api", ISSUE], True),
    "--repo api api": (["--repo", "api", "api", ISSUE], True),
    "-R api/x api": (["-R", "api/x", "api", ISSUE], True),
    "-R api/x api merge PUT": (["-R", "api/x", "api", MERGE, "-X", "PUT"], True),
    "abbreviated --fiel": (["api", "--fiel", "a=b", MERGE], False),
    "abbreviated --inp": (["api", "--inp", "x", MERGE], False),
    "abbreviated --meth": (["api", "--meth", "PUT", MERGE], False),
    "-- then two positionals": (["api", "--", MERGE, ISSUE], False),
    "header override": (["api", "-H", "X-HTTP-Method-Override: PUT", MERGE], None),   # None: decided by the parser itself, whichever way
}
REFUSED_START = "the argv of that gh api call cannot be classified"


class Lookup:
    def identity(self):
        return {"read_repo": "o/x", "repo": "o/r", "own": True, "name": "w"}


def load_shim(source):
    space = {"__name__": "ghshim_under_test", "__file__": str(SHIM_DIR / "gh")}
    exec(compile(source, "shims/gh", "exec"), space)
    return space


def parser_verdict(space, argv):
    """(None, parsed) when the parser accepts argv, else (message, None). argv is the whole command line."""
    try:
        return None, space["api_parsed"](argv)
    except space["Refused"] as err:
        return str(err), None


def readers(space, argv):
    """What each of the four readers decides for argv."""
    os.environ.setdefault("XDG_CONFIG_HOME", "/nonexistent-xdg")
    space["load_hold"] = lambda: None
    guard = space["hold_probe"](argv, None, None, False)[0]
    scope = space["read_scope"](argv, Lookup(), space["is_write"](argv))
    return {"is_write": space["is_write"](argv), "merge_route": space["merge_route"](argv), "named_repos": space["named_repos"](argv),
            "guard": guard, "scope": scope}


def reader_failures(source):
    """Every difference between a reader and the parser over API_READER_FORMS, as anchored one-line texts."""
    space = load_shim(source)
    failures = []
    for label, (argv, _accepted) in API_READER_FORMS.items():
        why, parsed = parser_verdict(space, argv)
        got = readers(space, argv)
        if why is not None and _accepted is True:
            failures.append("%s: the parser refused an argv that is accepted: %s" % (label, why[:60]))
        if why is not None:
            if got["is_write"] is not True:
                failures.append("%s: is_write read a refused argv as %r, not a write" % (label, got["is_write"]))
            route = got["merge_route"]
            if not (route and route[0] == "api-unknown" and route[1].startswith(REFUSED_START)):
                failures.append("%s: merge_route did not refuse the argv: %r" % (label, route))
            if got["named_repos"][1] is not True:
                failures.append("%s: named_repos did not refuse the argv: %r" % (label, got["named_repos"]))
            if got["guard"] != "gh: refused: " + why:
                failures.append("%s: the hold guard did not refuse the argv: %r" % (label, got["guard"]))
            if not (got["scope"] and got["scope"][0] == "refuse" and got["scope"][1].startswith("gh: refused:")):
                failures.append("%s: the read-scope gate did not refuse with 'gh: refused:': %r" % (label, got["scope"]))
            try:
                space["api_parse"](argv[space["api_start"](argv):])
                failures.append("%s: api_parse accepted a refused argv" % label)
            except space["Refused"]:
                pass
            except Exception as err:
                failures.append("%s: api_parse raised %s, not Refused" % (label, type(err).__name__))
            continue
        method, write = parsed.method or "GET", bool(parsed.fields or parsed.input)
        try:
            old_positionals, old_method, old_fields = space["api_parse"](argv[space["api_start"](argv):])
        except Exception as err:
            failures.append("%s: api_parse raised %s on an accepted argv" % (label, type(err).__name__))
        else:
            flagmap = {"field": "-F", "raw-field": "-f"}
            want = ([space["api_endpoint"](p) for _i, p in parsed.positionals], parsed.method, [(i, flagmap[n], j, v) for i, n, j, v in parsed.fields])
            if (old_positionals, old_method, old_fields) != want:
                failures.append("%s: api_parse %r differs from the parser's %r" % (label, (old_positionals, old_method, old_fields), want))
        if got["is_write"] != (parsed.effective_method != "GET"):
            failures.append("%s: is_write %r differs from the parser's method %s" % (label, got["is_write"], parsed.effective_method))
        route = got["merge_route"]
        graphql = any(p == "graphql" for _i, p in parsed.positionals)
        if graphql:
            expected = ("graphql", None)
        elif method == "GET" and not write:
            expected = None
        else:
            expected = ("api", ("o/r", 5)) if parsed.positionals and parsed.positionals[0][1] == MERGE else None
        if route != expected:
            failures.append("%s: merge_route %r differs from the parser's %r" % (label, route, expected))
        if got["guard"] is not None:
            failures.append("%s: the hold guard refused an argv the parser accepts: %r" % (label, got["guard"]))
        found, bad = got["named_repos"]
        if parsed.positionals and parsed.positionals[0][1] in (MERGE, ISSUE):
            if bad or "o/r" not in found:
                failures.append("%s: named_repos read %r (bad %r), not o/r" % (label, found, bad))
    return failures


class TestApiReadersAreViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (SHIM_DIR / "gh").read_text()   # at run time, not at import: a copy of the tests without shims/ must still import

    def test_every_reader_agrees_with_the_parser(self):
        failures = reader_failures(self.source)
        print("R7 readers: %d forms, %d differences" % (len(API_READER_FORMS), len(failures)))
        self.assertEqual(failures, [])

    def test_table_forms_are_accepted_or_refused_as_labelled(self):
        space = load_shim(self.source)
        for label, (argv, accepted) in API_READER_FORMS.items():
            why, _parsed = parser_verdict(space, argv)
            if accepted is not None:
                self.assertEqual(why is None, accepted, label)
        self.assertIsNotNone(parser_verdict(space, API_READER_FORMS["abbreviated --inp"][0])[0])

    def test_option_value_named_api_is_not_the_verb(self):
        space = load_shim(self.source)
        for argv in (["-R", "api", "api", ISSUE], ["--repo", "api", "api", ISSUE], ["-R", "api/x", "api", ISSUE]):
            got = readers(space, argv)
            self.assertFalse(got["is_write"], argv)
            self.assertIn("o/r", got["named_repos"][0], argv)
            self.assertFalse(got["named_repos"][1], argv)
            self.assertIsNone(got["guard"], argv)
            _why, parsed = parser_verdict(space, argv)
            self.assertEqual([p for _i, p in parsed.positionals], [ISSUE], argv)

    def test_merge_put_behind_option_value_named_api_is_the_merge_route(self):
        space = load_shim(self.source)
        argv = ["-R", "api/x", "api", MERGE, "-X", "PUT"]
        self.assertEqual(space["merge_route"](argv), ("api", ("o/r", 5)))
        self.assertTrue(space["is_write"](argv))

    def test_header_override_is_decided_as_the_parser_decides(self):
        space = load_shim(self.source)
        argv = API_READER_FORMS["header override"][0]
        why, parsed = parser_verdict(space, argv)
        got = readers(space, argv)
        if why is not None:
            self.assertTrue(got["is_write"])
            self.assertEqual(got["guard"], why)
        else:
            self.assertEqual(got["is_write"], parsed.effective_method != "GET")

    def mutant(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old[:60])
        return self.source.replace(old, new)

    def test_control_unmutated_passes(self):
        self.assertEqual(reader_failures(self.source), [])

    def test_mutant_old_is_write_branch_fails(self):
        old = '''            try:
                return api_parsed(args).effective_method != "GET"   # gh's own rule: an explicit last method, else POST with a field or --input
            except Refused:
                return True   # an argv the parser refuses is a write: fail closed
'''
        new = '''            method = None
            for i, a in enumerate(args):
                if a in ("-X", "--method") and i + 1 < len(args):
                    method = args[i + 1].upper()
                elif a.startswith("--method="):
                    method = a.split("=", 1)[1].upper()
                elif a.startswith("-X") and len(a) > 2:
                    method = a[2:].upper()
            if method is not None:
                return method != "GET"
            return any(a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=", "--input=", "-f", "-F"))
                       for a in args[1:] if a.startswith("-") and not a.startswith("--jq") and not a.startswith("--paginate") and not a.startswith("--slurp"))
'''
        seen = reader_failures(self.mutant(old, new))
        print("R7 mutant old is_write: %d failures" % len(seen))
        self.assertTrue(any(s.startswith("abbreviated --inp: is_write read a refused argv as False") for s in seen), seen)

    def test_mutant_args_index_api_fails(self):
        old = '''    words = command_word_positions(args)
    return words[0][0] if words and words[0][1] == "api" else None
'''
        new = '''    return args.index("api") if "api" in args else None
'''
        seen = reader_failures(self.mutant(old, new))
        print("R7 mutant args.index: %d failures" % len(seen))
        self.assertTrue(any(s.startswith("-R api api: the parser refused an argv that is accepted") for s in seen), seen)
        self.assertTrue(any(s.startswith("--repo api api:") for s in seen), seen)

    def test_mutant_api_parse_scanning_words_fails_and_control_passes(self):
        old = '    parsed = parse_api_argv(args)\n    flagmap = {"field": "-F", "raw-field": "-f"}\n'
        new = '    at = args.index("api")\n    return [a for a in args[at + 1:] if not a.startswith("-")], None, []\n' + old
        seen = [t for t in reader_failures(self.mutant(old, new)) if ": api_parse " in t]
        print("S22 mutant api_parse: %d failures" % len(seen))
        self.assertTrue(any(t.startswith("-X PUT: api_parse ") and "differs from the parser's" in t for t in seen), seen)
        self.assertEqual([t for t in reader_failures(self.source) if ": api_parse " in t], [])


HOLD_FORMS = (["-R", "api/x", "api", ISSUE], ["-R", "api", "api", ISSUE])


def hold_endpoint_failures(hold_source):
    """Anchored one-line texts: every HOLD_FORMS argv must be accepted by the hold probe and read by gh_hold's parse_api as the one endpoint ISSUE.
    gh_hold is loaded from hold_source and handed the shim's parser the way load_hold hands it."""
    import types
    space = load_shim((SHIM_DIR / "gh").read_text())
    module = types.ModuleType("gh_hold_under_test")
    exec(compile(hold_source, "shims/gh_hold.py", "exec"), module.__dict__)
    module.API_PARSER[0] = space["api_parsed"]
    failures = []
    for argv in HOLD_FORMS:
        try:
            got = module.parse_api(argv)
        except Exception as err:
            failures.append("hold %s: parse_api raised %s" % (argv[:2], type(err).__name__))
            continue
        if got[0] != [ISSUE]:
            failures.append("hold %s: parse_api endpoints %r, not %r" % (argv[:2], got[0], [ISSUE]))
    for label, (argv, accepted) in API_READER_FORMS.items():
        why, parsed = parser_verdict(space, argv)
        if why is not None:
            try:
                module.parse_api(argv)
                failures.append("hold form %s: parse_api accepted a refused argv" % label)
            except Exception as err:
                if type(err) is not space["Refused"]:
                    failures.append("hold form %s: parse_api raised %s, not Refused" % (label, type(err).__name__))
            continue
        try:
            got = module.parse_api(argv)
        except Exception as err:
            failures.append("hold form %s: parse_api raised %s on an accepted argv" % (label, type(err).__name__))
            continue
        want = ([p for _i, p in parsed.positionals], parsed.method.upper() if parsed.method else None, len(parsed.fields), parsed.input is not None)
        have = (got[0], got[1].upper() if got[1] else None, len(got[2]), got[3])
        if have != want:
            failures.append("hold form %s: parse_api %r differs from the parser's %r" % (label, have, want))
    space["load_hold"] = lambda: module
    os.environ.setdefault("XDG_CONFIG_HOME", "/nonexistent-xdg")
    for argv in HOLD_FORMS:
        guard = space["hold_probe"](argv, Lookup(), None, False)[0]
        if guard is not None:
            failures.append("hold %s: the hold probe refused: %r" % (argv[:2], guard))
    return failures


class TestGhHoldIsAView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (SHIM_DIR / "gh_hold.py").read_text()

    def test_table_forms_through_the_hold_probe(self):
        self.assertEqual(hold_endpoint_failures(self.source), [])

    def test_real_load_hold_hands_the_parser_over(self):
        space = load_shim((SHIM_DIR / "gh").read_text())
        module = space["load_hold"]()
        self.assertIsNotNone(module)
        self.assertEqual(module.parse_api(["-R", "api/x", "api", ISSUE])[0], [ISSUE])

    def test_mutant_private_reader_fails_and_control_passes(self):
        anchor = "    parser = API_PARSER[0]\n"
        self.assertEqual(self.source.count(anchor), 1, "the mutant's anchor is gone")
        mutant = self.source.replace(anchor, "    at = args.index(\"api\")\n    return [a for a in args[at + 1:] if not a.startswith(\"-\")][:1], None, [], False\n" + anchor)
        seen = hold_endpoint_failures(mutant)
        self.assertTrue(any(t.startswith("hold ['-R', 'api']: parse_api endpoints ['api'], not ") for t in seen), seen)
        self.assertEqual(hold_endpoint_failures(self.source), [])

    def test_mutant_private_reader_over_every_form_fails_and_control_passes(self):
        anchor = "    parser = API_PARSER[0]\n"
        self.assertEqual(self.source.count(anchor), 1, "the mutant's anchor is gone")
        mutant = self.source.replace(anchor, "    at = args.index(\"api\")\n    return [a for a in args[at + 1:] if not a.startswith(\"-\")], None, [], False\n" + anchor)
        seen = hold_endpoint_failures(mutant)
        print("S22 mutant gh_hold: %d failures" % len(seen))
        self.assertTrue(any(t.startswith("hold form -X PUT: parse_api ") and "differs from the parser's" in t for t in seen), seen)
        self.assertEqual(hold_endpoint_failures(self.source), [])


def nested_failures(rig):
    """#1696 S23/S26: the failures seen for inline review comment bodies (empty when each is signed or refused)."""
    bad = []
    reviews = "repos/o/r/pulls/1/reviews"
    for flag in ("-f", "-F", "--raw-field"):
        done, calls = rig.run("api", reviews, flag, "event=COMMENT", flag, "comments[][path]=a", flag, "comments[][body]=x", flag, "comments[][position]=1")
        lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        if calls or done.returncode != 1 or len(lines) != 1 or not lines[0].startswith("gh: refused:"):
            bad.append("a nested review comment body went out unsigned")
    payload = json.dumps({"event": "COMMENT", "comments": [{"path": "a", "body": "x", "position": 1}, {"path": "b", "body": "y", "position": 2}]})
    path = rig.home / "nested.json"
    path.write_text(payload)
    done, calls = rig.run("api", reviews, "--input", str(path))
    if len(calls) != 1:
        bad.append("a nested review comment body went out unsigned")
    else:
        data = json.loads(calls[0]["files"][calls[0]["argv"][-1]])
        if [c["body"].count(SIGNED) for c in data["comments"]] != [1, 1] or data["event"] != "COMMENT" or [c["path"] for c in data["comments"]] != ["a", "b"]:
            bad.append("a nested review comment body went out unsigned")
    done, calls = rig.run("api", reviews, "--input", "-", stdin=payload)
    if len(calls) != 1:
        bad.append("a nested review comment body went out unsigned")
    else:
        data = json.loads(calls[0]["stdin"])
        if [c["body"].count(SIGNED) for c in data["comments"]] != [1, 1] or data["event"] != "COMMENT":
            bad.append("a nested review comment body went out unsigned")
    return bad


class TestGhShimNestedAndQueryBodies(unittest.TestCase):
    """#1696 PC-7: inline review comment bodies (M1), a body in the URL query string (M2), a body key in any case and a non-object --input (L1)."""

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def file(self, text, name="in.json"):
        path = self.rig.home / name
        path.write_text(text)
        return str(path)

    def refused(self, done, calls):
        self.assertEqual(done.returncode, 1)
        gh_lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        self.assertEqual(len(gh_lines), 1, done.stderr)
        self.assertTrue(gh_lines[0].startswith("gh: refused:"), done.stderr)
        self.assertEqual(calls, [])

    def test_s23_inline_comment_fields_are_refused(self):
        reviews = "repos/o/r/pulls/1/reviews"
        refused = 0
        for flag in ("-f", "-F", "--raw-field"):
            with self.subTest(flag=flag):
                self.refused(*self.rig.run("api", reviews, flag, "comments[][path]=a", flag, "comments[][body]=x", flag, "comments[][position]=1"))
                refused += 1
        self.assertEqual(refused, 3)

    def test_s23_inline_comment_input_file_and_stdin_are_signed_once_each(self):
        payload = json.dumps({"event": "COMMENT", "comments": [{"path": "a", "body": "x", "position": 1}, {"path": "b", "body": "y", "position": 2}]})
        reviews = "repos/o/r/pulls/1/reviews"
        done, calls = self.rig.run("api", reviews, "--input", self.file(payload))
        self.assertEqual(len(calls), 1, done.stderr)
        data = json.loads(calls[0]["files"][calls[0]["argv"][-1]])
        self.assertEqual([c["body"].count(SIGNED) for c in data["comments"]], [1, 1])
        self.assertEqual(data["event"], "COMMENT")
        self.assertEqual([c["path"] for c in data["comments"]], ["a", "b"])
        done, calls = self.rig.run("api", reviews, "--input", "-", stdin=payload)
        self.assertEqual(len(calls), 1, done.stderr)
        data = json.loads(calls[0]["stdin"])
        self.assertEqual([c["body"].count(SIGNED) for c in data["comments"]], [1, 1])
        self.assertEqual(data["event"], "COMMENT")
        self.assertEqual([c["path"] for c in data["comments"]], ["a", "b"])

    def test_s23_control_no_inline_comments_passes_unchanged(self):
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "-f", "event=COMMENT", "-f", "body=x")
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertIn("event=COMMENT", calls[0]["argv"])
        self.assertEqual(sum(a.count(SIGNED) for a in calls[0]["argv"]), 1)
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", "-", stdin='{"event":"APPROVE"}')
        self.assertEqual(calls[0]["stdin"], '{"event":"APPROVE"}')

    def test_s24_a_query_string_body_on_a_write_is_refused(self):
        refused = 0
        for key in ("body", "Body"):
            with self.subTest(key=key):
                self.refused(*self.rig.run("api", "-X", "POST", "repos/o/r/issues/1/comments?%s=zz" % key))
                refused += 1
        self.assertEqual(refused, 2)

    def test_s24_a_get_with_the_same_query_is_unchanged(self):
        argv = ("api", "repos/o/r/issues/1/comments?body=zz")
        done, calls = self.rig.run(*argv)
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(calls[0]["argv"], list(argv))

    def test_s25_a_Body_key_is_signed_under_its_own_key_once(self):
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "--input", self.file('{"Body":"x"}'))
        self.assertEqual(len(calls), 1, done.stderr)
        data = json.loads(calls[0]["files"][calls[0]["argv"][-1]])
        self.assertEqual(list(data), ["Body"])
        self.assertEqual(data["Body"].count(SIGNED), 1)

    def test_s25_an_array_and_a_scalar_are_refused(self):
        for text in ('[{"body":"x"}]', '"x"'):
            with self.subTest(text=text):
                self.refused(*self.rig.run("api", "repos/o/r/issues/1/comments", "--input", self.file(text)))

    def test_s25_a_nested_non_string_body_is_refused(self):
        self.refused(*self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", self.file('{"comments":[{"path":"a","body":5}]}')))

    def test_s26_control_the_unmutated_shim_passes(self):
        self.assertEqual(nested_failures(self.rig), [])

    def test_s26_a_mutant_that_ignores_nested_bodies_is_seen(self):
        self.assertEqual(nested_failures(self.rig), [])   # control first
        old = "        for item in data[key]:\n"
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone (the nested handling of input_call)")
        rig = Rig(self.source.replace(old, "        for item in []:\n", 1))
        self.addCleanup(rig.close)
        seen = nested_failures(rig)
        self.assertIn("a nested review comment body went out unsigned", seen)


def encoded_query_failures(rig):
    """#1696 S27: the failures seen for a body written in the query string with an encoded or `;`-separated key (empty when each is refused)."""
    bad = []
    for path in ("repos/o/r/issues/1/comments?%62ody=zz", "repos/o/r/pulls/1/reviews?comments%5B%5D%5Bbody%5D=zz", "repos/o/r/issues/1/comments?x=1;body=zz"):
        done, calls = rig.run("api", "-X", "POST", path)
        lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        if calls or done.returncode != 1 or len(lines) != 1 or not lines[0].startswith("gh: refused:"):
            bad.append("an encoded query key went out unsigned")
    return bad


class TestGhShimEncodedQueryAndLabelDelete(unittest.TestCase):
    """#1696 PC-8: encoded and `;`-separated query keys (S27) and the one allow-listed label delete (S29)."""

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def refused(self, done, calls):
        self.assertEqual(done.returncode, 1)
        gh_lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        self.assertEqual(len(gh_lines), 1, done.stderr)
        self.assertTrue(gh_lines[0].startswith("gh: refused:"), done.stderr)
        self.assertEqual(calls, [])

    def test_s27_encoded_and_semicolon_query_keys_are_refused(self):
        refused = 0
        for path in ("repos/o/r/issues/1/comments?%62ody=zz", "repos/o/r/pulls/1/reviews?comments%5B%5D%5Bbody%5D=zz", "repos/o/r/issues/1/comments?x=1;body=zz"):
            with self.subTest(path=path):
                self.refused(*self.rig.run("api", "-X", "POST", path))
                refused += 1
        self.assertEqual(refused, 3)

    def test_s27_the_plain_forms_are_still_refused(self):
        for path in ("repos/o/r/issues/1/comments?body=zz", "repos/o/r/issues/1/comments?x=1&body=zz"):
            with self.subTest(path=path):
                self.refused(*self.rig.run("api", "-X", "POST", path))

    def test_s27_a_query_without_a_body_key_passes_signed_once(self):
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments?line=1", "-f", "body=hi")
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(sum(a.count(SIGNED) for a in calls[0]["argv"]), 1)

    def test_s27_a_get_with_the_same_query_is_unchanged(self):
        for path in ("repos/o/r/issues/1/comments?%62ody=zz", "repos/o/r/issues/1/comments?x=1;body=zz"):
            with self.subTest(path=path):
                done, calls = self.rig.run("api", path)
                self.assertEqual(len(calls), 1, done.stderr)
                self.assertEqual(calls[0]["argv"], ["api", path])

    def test_s27_control_the_unmutated_shim_passes(self):
        self.assertEqual(encoded_query_failures(self.rig), [])

    def test_s27_a_mutant_that_compares_the_raw_key_is_seen(self):
        self.assertEqual(encoded_query_failures(self.rig), [])   # control first
        old = 'urllib.parse.unquote(part.partition("=")[0]) for part in re.split(r"[&;]", query)'
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone (the query keys of api_decide)")
        check = '"%" in key or not key_known(key) or (key.lower() == "body" and key in query_keys)'
        self.assertEqual(self.source.count(check), 1, "the mutant's anchor is gone (the key check of api_decide)")
        rig = Rig(self.source.replace(old, 'part.partition("=")[0] for part in query.split("&")', 1).replace(check, '(key != "body" or key in query_keys) and re.search(r"(?i)(^|\\[)body(\\]|\\[\\])*$", key)', 1))
        self.addCleanup(rig.close)
        self.assertIn("an encoded query key went out unsigned", encoded_query_failures(rig))

    def test_s29_the_delete_of_a_repository_label_passes_unchanged(self):
        argv = ("api", "-X", "DELETE", "repos/o/r/labels/NAME")
        done, calls = self.rig.run(*argv)
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(calls[0]["argv"], list(argv))

    def test_s29_its_neighbours_are_refused(self):
        refused = 0
        for method, path in (("PATCH", "repos/o/r/labels/NAME"), ("DELETE", "repos/o/r/labels"), ("DELETE", "repos/o/r/labels/a/b"), ("PUT", "repos/o/r/labels/NAME")):
            with self.subTest(method=method, path=path):
                self.refused(*self.rig.run("api", "-X", method, path))
                refused += 1
        self.assertEqual(refused, 4)


def body_key_failures(rig):
    """#1696 S31: the failures seen for an --input JSON whose key is written Body (empty when it is signed once under that key)."""
    bad = []
    path = rig.home / "bodykey.json"
    path.write_text('{"Body":"x"}')
    done, calls = rig.run("api", "repos/o/r/issues/1/comments", "--input", str(path))
    if len(calls) != 1:
        return ["a body key written Body went out unsigned"]
    data = json.loads(calls[0]["files"][calls[0]["argv"][-1]])
    if list(data) != ["Body"] or data["Body"].count(SIGNED) != 1:
        bad.append("a body key written Body went out unsigned")
    return bad


class TestGhShimDeepBodiesAndInputForms(unittest.TestCase):
    """#1696 PC-9: bodies below the signed level are refused (S30), the Body key mutant is seen (S31), --input=FILE and repeated -f body (S32)."""

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def file(self, text, name="in.json"):
        path = self.rig.home / name
        path.write_text(text)
        return str(path)

    def refused(self, done, calls):
        self.assertEqual(done.returncode, 1)
        gh_lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        self.assertEqual(len(gh_lines), 1, done.stderr)
        self.assertTrue(gh_lines[0].startswith("gh: refused:"), done.stderr)
        self.assertEqual(calls, [])

    def test_s30_control_first_a_flat_comment_is_signed_once(self):
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", self.file('{"comments":[{"body":"x","path":"a","line":3}]}'))
        self.assertEqual(len(calls), 1, done.stderr)
        data = json.loads(calls[0]["files"][calls[0]["argv"][-1]])
        self.assertEqual(data["comments"][0]["body"].count(SIGNED), 1)

    def test_s30_a_body_below_the_signed_level_is_refused(self):
        deep = {"body": "n"}
        for _ in range(25):
            deep = {"n": deep}
        refused = 0
        for text in ('{"comments":[{"body":"x","replies":[{"body":"n2"}]}]}', '{"body":"x","threads":[{"Body":"n2"}]}', json.dumps({"body": "x", "deep": deep})):
            with self.subTest(text=text[:40]):
                self.refused(*self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", self.file(text)))
                refused += 1
        self.assertEqual(refused, 3)

    def test_s31_control_the_unmutated_shim_passes(self):
        self.assertEqual(body_key_failures(self.rig), [])

    def test_s31_a_mutant_comparing_the_key_in_lower_case_only_is_seen(self):
        self.assertEqual(body_key_failures(self.rig), [])   # control first
        old = 'for key in [k for k in data if k.lower() == "body"]:'
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone (the first-level body of input_call)")
        rig = Rig(self.source.replace(old, 'for key in [k for k in data if k == "body"]:', 1))
        self.addCleanup(rig.close)
        self.assertIn("a body key written Body went out unsigned", body_key_failures(rig))

    def test_s32_input_equals_file_is_signed_once_and_keeps_its_spelling(self):
        reader = (self.rig.bin / "gh")
        reader.write_text(reader.read_text().replace("(a, a.split(", "(a, a.split(\"=\", 1)[-1], a.split(", 1))
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "--input=" + self.file('{"body":"x"}'))
        self.assertEqual(len(calls), 1, done.stderr)
        arg = calls[0]["argv"][-1]
        self.assertTrue(arg.startswith("--input="), arg)
        temp = arg[len("--input="):]
        self.assertNotEqual(temp, str(self.rig.home / "in.json"))
        self.assertEqual(json.loads(calls[0]["files"][temp])["body"].count(SIGNED), 1)

    def test_s32_two_body_fields_are_signed_once_each(self):
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "-f", "body=a", "-f", "body=b")
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(sum(a.count(SIGNED) for a in calls[0]["argv"]), 2)


def input_body_field_failures(rig):
    """#1696 S33: the failures seen for a -f or -F body field beside --input (empty when the control passes and each form is refused)."""
    bad = []
    reviews = "repos/o/r/pulls/1/reviews"
    path = rig.home / "beside.json"
    path.write_text('{"event":"COMMENT"}')
    other = rig.home / "control.json"
    other.write_text('{"body":"x"}')
    done, calls = rig.run("api", reviews, "-f", "event=COMMENT", "--input", str(other))
    if len(calls) != 1 or json.loads(calls[0]["files"][calls[0]["argv"][-1]])["body"].count(SIGNED) != 1:
        return ["the control of S33 did not pass"]
    for form in (("-f", "body=a"), ("-F", "Body=a"), ("-f", "comments[][body]=a")):
        done, calls = rig.run("api", reviews, *form, "--input", str(path))
        lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        if calls or done.returncode != 1 or len(lines) != 1 or not lines[0].startswith("gh: refused:"):
            bad.append("a body field beside --input went out unsigned")
    return bad


class TestGhShimBodyFieldBesideInput(unittest.TestCase):
    """#1696 PC-10: a -f or -F body field beside --input is refused (S33), and a mutant that skips that check is seen (S34)."""

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def test_s33_control_first_a_non_body_field_beside_input_is_signed_once(self):
        path = self.rig.home / "c.json"
        path.write_text('{"body":"x"}')
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "-f", "event=COMMENT", "--input", str(path))
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(json.loads(calls[0]["files"][calls[0]["argv"][-1]])["body"].count(SIGNED), 1)

    def test_s33_the_label_delete_with_a_field_is_unchanged(self):
        done, calls = self.rig.run("api", "-X", "DELETE", "repos/o/r/labels/n", "-f", "body=x")
        self.assertEqual(len(calls), 1, done.stderr)

    def test_s33_a_body_field_beside_input_is_refused(self):
        self.assertEqual(input_body_field_failures(self.rig), [])

    def test_s34_a_mutant_skipping_the_check_when_input_is_present_is_seen(self):
        self.assertEqual(input_body_field_failures(self.rig), [])   # control first
        old = "        if found[2] and has_input and any("
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone (the body field check of api_decide)")
        rig = Rig(self.source.replace(old, "        if found[2] and has_input and not has_input and any(", 1))
        self.addCleanup(rig.close)
        self.assertIn("a body field beside --input went out unsigned", input_body_field_failures(rig))


def bracket_body_failures(rig):
    """#1696 S35: the failures seen for body keys with trailing brackets and a percent-bearing query key (empty when the control passes and all 4 are refused)."""
    bad = []
    comments = "repos/o/r/issues/1/comments"
    path = rig.home / "bracket.json"
    path.write_text('{"event":"COMMENT"}')
    done, calls = rig.run("api", comments, "-f", "body=a")
    if len(calls) != 1 or sum(a.count(SIGNED) for a in calls[0]["argv"]) != 1:
        return ["the control of S35 did not pass"]
    forms = (
        (comments, "-f", "body[]=a"),
        (comments, "-F", "comments[][body][]=a"),
        (comments, "-f", "body[]=a", "--input", str(path)),
        (comments + "?%2562ody=zz", "-X", "POST"),
    )
    for form in forms:
        done, calls = rig.run("api", *form)
        lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        if calls or done.returncode != 1 or len(lines) != 1 or not lines[0].startswith("gh: refused:"):
            bad.append("a body[] field went out unsigned" if "?" not in form[0] else "a key with a percent sign went out unsigned")
    return bad


class TestGhShimBracketBodyKeys(unittest.TestCase):
    """#1696 PC-11: body keys with trailing brackets and percent-bearing query keys are refused (S35), and two mutants are seen (S36)."""

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def test_s35_control_first_a_plain_body_field_is_signed_once(self):
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "-f", "body=a")
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(sum(a.count(SIGNED) for a in calls[0]["argv"]), 1)

    def test_s35_bracket_and_percent_keys_are_refused(self):
        self.assertEqual(bracket_body_failures(self.rig), [])

    def test_s36_a_mutant_with_the_old_key_pattern_is_seen(self):
        self.assertEqual(bracket_body_failures(self.rig), [])   # control first
        new = '    return key.lower() == "body" or key in ALLOWED_KEYS'
        old = '    return key.lower() == "body" or not re.search(r"(?i)(^|\[)body\]?$", key)'
        self.assertEqual(self.source.count(new), 1, "the mutant's anchor is gone (the allowlist check of the shim)")
        rig = Rig(self.source.replace(new, old))
        self.addCleanup(rig.close)
        self.assertIn("a body[] field went out unsigned", bracket_body_failures(rig), "a body[] field went out unsigned")

    def test_s36_a_mutant_dropping_the_percent_check_is_seen(self):
        self.assertEqual(bracket_body_failures(self.rig), [])   # control first
        old = '"%" in key or not key_known(key)'
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone (the percent check of api_decide)")
        rig = Rig(self.source.replace(old, 'not (key_known(key) or "%" in key)', 1))
        self.addCleanup(rig.close)
        self.assertIn("a key with a percent sign went out unsigned", bracket_body_failures(rig), "a key with a percent sign went out unsigned")


def allowlist_failures(rig):
    """#1696 S37: the failures seen for keys outside the fixed allowlist on a comment or review endpoint (empty when both controls pass and all 9 forms are refused)."""
    bad = []
    comments, reviews = "repos/o/r/issues/1/comments", "repos/o/r/pulls/1/reviews"
    good = rig.home / "known.json"
    good.write_text('{"body":"x","event":"COMMENT","comments":[{"path":"a","line":3,"body":"y"}]}')
    done, calls = rig.run("api", comments, "-f", "body=a", "-f", "event=COMMENT")
    if len(calls) != 1 or sum(a.count(SIGNED) for a in calls[0]["argv"]) != 1:
        return ["the first control of S37 did not pass"]
    done, calls = rig.run("api", reviews, "--input", str(good))
    if len(calls) != 1:
        return ["the second control of S37 did not pass"]
    text = calls[0]["files"][calls[0]["argv"][-1]]
    if text.count("from=alice-bot") != 2:
        return ["the second control of S37 did not pass"]
    unknown = rig.home / "unknown.json"
    unknown.write_text('{"body":"x","threads":[]}')
    unknown_comment = rig.home / "unknown-comment.json"
    unknown_comment.write_text('{"comments":[{"path":"a","body":"y","threads":1}]}')
    forms = (
        (comments, "-f", "body[0]=a"), (comments, "-f", "body[x]=a"), (comments, "-f", "body][=a"), (comments, "-f", "body =a"),
        (comments, "-f", "xbody=a"), (comments, "-f", "threads=a"),
        (reviews, "--input", str(unknown)), (reviews, "--input", str(unknown_comment)),
        (comments + "?labels=a", "-X", "POST"),
    )
    for form in forms:
        done, calls = rig.run("api", *form)
        lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        if calls or done.returncode != 1 or len(lines) != 1 or not lines[0].startswith("gh: refused:"):
            bad.append("a key outside the allowlist went out unsigned")
    return bad


class TestGhShimAllowlist(unittest.TestCase):
    """#1696 PC-12: only a fixed allowlist of keys passes on a comment or review endpoint (S37); a mutant with the old deny pattern is seen on a scratch copy (S38)."""

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def test_s37_control_first_known_keys_are_signed_once_per_body(self):
        done, calls = self.rig.run("api", "repos/o/r/issues/1/comments", "-f", "body=a", "-f", "event=COMMENT")
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(sum(a.count(SIGNED) for a in calls[0]["argv"]), 1)
        path = self.rig.home / "k.json"
        path.write_text('{"body":"x","event":"COMMENT","comments":[{"path":"a","line":3,"body":"y"}]}')
        done, calls = self.rig.run("api", "repos/o/r/pulls/1/reviews", "--input", str(path))
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(calls[0]["files"][calls[0]["argv"][-1]].count("from=alice-bot"), 2)

    def test_s37_keys_outside_the_allowlist_are_refused(self):
        self.assertEqual(allowlist_failures(self.rig), [])

    def test_s38_a_mutant_with_the_old_deny_pattern_is_seen(self):
        self.assertEqual(allowlist_failures(self.rig), [])   # control first: exit 0
        old = '    return key.lower() == "body" or key in ALLOWED_KEYS'
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone (the allowlist check of the shim)")
        rig = Rig(self.source.replace(old, '    return key.lower() == "body" or not re.search(r"(?i)(^|\\[)body(\\]|\\[\\])*$", key)', 1))   # a scratch copy outside the repo
        self.addCleanup(rig.close)
        self.assertNotEqual(rig.home.resolve(), REPO.resolve())
        self.assertIn("a key outside the allowlist went out unsigned", allowlist_failures(rig), "a key outside the allowlist went out unsigned")


def early_flag_failures(rig):
    """#1696 S39: the failures seen for a method flag written before the api word (empty when the control passes and both forms are refused)."""
    bad = []
    endpoint = "repos/o/r/issues/comments/1"
    done, calls = rig.run("api", "-X", "PATCH", endpoint, "-f", "body=a")
    if len(calls) != 1 or sum(a.count(SIGNED) for a in calls[0]["argv"]) != 1:
        return ["the control of S39 did not pass"]
    for form in (("-X", "PATCH", "api", endpoint, "-f", "body=a"), ("--method", "PATCH", "api", endpoint, "-f", "body=a")):
        done, calls = rig.run(*form)
        lines = [l for l in done.stderr.splitlines() if l.startswith("gh:")]
        if calls or done.returncode != 1 or len(lines) != 1 or not lines[0].startswith("gh: refused:"):
            bad.append("a method flag before api went out unsigned")
    return bad


class TestGhShimMethodFlagBeforeApi(unittest.TestCase):
    """#1696 PC-12: a method flag before the api word is refused (S39), and an anchored mutant is seen."""

    def setUp(self):
        self.rig = hermetic_rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def test_s39_control_first_the_flag_after_the_api_word_is_signed_once(self):
        done, calls = self.rig.run("api", "-X", "PATCH", "repos/o/r/issues/comments/1", "-f", "body=a")
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(sum(a.count(SIGNED) for a in calls[0]["argv"]), 1)

    def test_s39_a_method_flag_before_api_is_refused(self):
        self.assertEqual(early_flag_failures(self.rig), [])

    def test_s39_a_mutant_ignoring_the_early_flag_is_seen(self):
        self.assertEqual(early_flag_failures(self.rig), [])   # control first
        old = "    if early_method_flag(args[:at]):"
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone (the early flag refusal of api_parsed)")
        rig = Rig(self.source.replace(old, "    if False:", 1))
        self.addCleanup(rig.close)
        self.assertIn("a method flag before api went out unsigned", early_flag_failures(rig), "a method flag before api went out unsigned")


if __name__ == "__main__":
    unittest.main()
