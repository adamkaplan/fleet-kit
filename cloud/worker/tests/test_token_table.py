"""#1031: the token table (cloud/token-table.txt) is ONE data table. Each of its 18 actions has one test row here that fails when the
row changes (action, token, deciding permission or status), the README and the worker prompt carry exactly the rendering of the file,
and the docs say plainly what is a boundary and what is a convenience. Regenerate the two renderings with:
    python3 cloud/worker/tests/test_token_table.py --write
Offline: reads files only. No probe, no credential."""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(os.path.dirname(HERE))
ROOT = os.path.dirname(CLOUD)
TABLE = os.path.join(CLOUD, "token-table.txt")
README = os.path.join(CLOUD, "README.md")
WORKER_MD = os.path.join(CLOUD, "worker", "agents", "worker.md")
BEGIN, END = "<!-- token-table:begin -->", "<!-- token-table:end -->"
FIELDS = ("action", "token", "decided by", "status")

# THE PIN: one row per action, written out. A change to the file's row (or a new or removed row) fails the row's own test.
PINNED = [
    ("PR create, edit title/body/base, close, reopen", "allowed", "pull requests: write", "documented"),
    ("PR comment, review comment, reply", "allowed", "pull requests: write", "documented"),
    ("Issue comment on a PR, labels on a PR, assignees", "allowed", "pull requests: write (the shared endpoints accept issues or pull requests)", "documented, labels to be probed live"),
    ("Request or remove reviewers", "allowed", "pull requests: write", "documented"),
    ("Ready for review, convert to draft", "allowed", "pull requests: write (GraphQL, not in the REST table)", "to be probed live"),
    ("Submit review, approve, request changes, dismiss", "allowed", "pull requests: write (GitHub refuses a self-approval; whether a bot approval counts is the ruleset's)", "documented, counting to be probed live"),
    ("Update PR branch", "allowed", "pull requests: write", "documented"),
    ("Merge a PR (REST)", "allowed", "contents: write (bounded only by the ruleset)", "documented"),
    ("Enable or disable auto-merge", "allowed", "pull requests: write (GraphQL; only if the repository allows auto-merge)", "to be probed live"),
    ("Merge queue enqueue and dequeue", "unknown", "not in the documented table", "to be probed live"),
    ("Branch create, push, delete (non-default)", "allowed", "contents: write", "documented"),
    ("Push or delete the default branch, force-push", "allowed", "contents: write (refused only by the ruleset)", "documented"),
    ("Tag create and delete", "allowed", "contents: write", "documented"),
    ("Releases and release assets", "allowed", "contents: write", "documented"),
    ("Workflow file changes", "refused", "no workflows permission", "documented"),
    ("Repository settings, rulesets, branch protection, webhooks, secrets", "refused", "administration, secrets and webhooks not granted", "documented"),
    ("Issue and issue-comment reads (view, list), and the comment (create)", "allowed", "issues: write (the owner's App step; the one permission covers reads and the comment)", "documented, to be probed live"),
    ("Issue edit, create, close, reopen, lock, delete, transfer, labels, assignees, comment edit and delete", "refused", "the pod's tool policy, not the token: issues: write would allow them, so the guard table denies each (the token cannot split the comment from the rest)", "documented, to be probed live"),
]


def rows(path=TABLE):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                out.append(tuple(x.strip() for x in line.rstrip("\n").split(" | ")))
    return out


def render_readme(rs):
    lines = ["| Action | Token | Decided by | Status |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % r for r in rs]
    return "\n".join(lines)


def render_prompt(rs):
    return "\n".join("- %s: %s (%s; %s)" % r for r in rs)


def block(text, name):
    m = re.search(re.escape(BEGIN) + r"\n(.*?)\n" + re.escape(END), text, re.S)
    if not m:
        raise AssertionError("%s has no token-table markers" % name)
    return m.group(1)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write_renderings():
    rs = rows()
    for path, fn in ((README, render_readme), (WORKER_MD, render_prompt)):
        text = read(path)
        new = re.sub(re.escape(BEGIN) + r"\n(?:.*?\n)?" + re.escape(END), lambda m: BEGIN + "\n" + fn(rs) + "\n" + END, text, flags=re.S)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)


class TokenTable(unittest.TestCase):
    def test_S2_exactly_eighteen_rows_each_with_all_four_fields(self):
        rs = rows()
        self.assertEqual(len(rs), 18)
        for r in rs:
            self.assertEqual(len(r), 4, r)
            self.assertTrue(all(x for x in r), r)
        self.assertEqual(len({r[0] for r in rs}), 18)

    def test_values_are_in_their_vocabulary(self):
        for r in rows():
            self.assertIn(r[1], ("allowed", "refused", "unknown"), r)
            self.assertTrue(r[3] == "documented" or "to be probed live" in r[3], r)

    def test_the_pin_covers_exactly_the_file(self):
        self.assertEqual([r[0] for r in PINNED], [r[0] for r in rows()])

    def test_the_readme_and_the_prompt_carry_exactly_the_rendering(self):
        rs = rows()
        self.assertEqual(block(read(README), "README"), render_readme(rs))
        self.assertEqual(block(read(WORKER_MD), "worker.md"), render_prompt(rs))

    def test_a_changed_cell_in_the_file_fails_its_row_test_and_the_rendering_check(self):   # a REAL mutant: a changed copy of the file, parsed by rows()
        import tempfile
        original = read(TABLE).splitlines(True)
        data = [i for i, l in enumerate(original) if l.strip() and not l.startswith("#")]
        for n, line_no in enumerate(data):
            for j in range(4):
                cells = original[line_no].rstrip("\n").split(" | ")
                cells[j] += " x"
                mutant = list(original)
                mutant[line_no] = " | ".join(cells) + "\n"
                with tempfile.TemporaryDirectory() as d:
                    path = os.path.join(d, "t.txt")
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(mutant)
                    got = rows(path)
                self.assertNotEqual(got[n], PINNED[n], (n, j))
                self.assertNotEqual(render_readme(got), block(read(README), "README"), (n, j))
                self.assertNotEqual(render_prompt(got), block(read(WORKER_MD), "worker.md"), (n, j))


def _row_test(i):
    def t(self):
        self.assertEqual(rows()[i], PINNED[i])
    return t


for _i, _p in enumerate(PINNED):
    setattr(TokenTable, "test_S2_row_%02d_%s" % (_i + 1, re.sub(r"\W+", "_", _p[0]).strip("_").lower()[:40]), _row_test(_i))


class BoundaryText(unittest.TestCase):
    """S1, S6, S8: the cause, the boundaries, the conveniences and the owner's choices are said in the README and the worker prompt."""

    def test_S1_exactly_one_root_cause_paragraph_names_the_real_binary_the_wrapper_and_the_token(self):
        text = read(README)
        m = re.search(r"^## The root cause of the recorded case\n(.*?)(?=^## )", text, re.S | re.M)
        self.assertTrue(m, "the README has no root-cause section")
        paras = [p for p in m.group(1).split("\n\n") if p.strip()]
        hits = [p for p in paras if "real `gh` binary" in p and "wrapper" in p and "token" in p and "never a boundary" in p]
        self.assertEqual(len(hits), 1, paras)

    def test_S8_the_boundaries_section_names_two_boundaries_the_conveniences_and_the_owner_choices(self):
        text = read(README)
        m = re.search(r"^## Where the boundary is\n(.*?)(?=^## )", text, re.S | re.M)
        self.assertTrue(m)
        sec = m.group(1)
        self.assertIn("the token's scope", sec)
        self.assertIn("the repository's rules", sec)
        self.assertRegex(sec, r"wrappers? and the client are conveniences")
        owner = sec.split("Owner decisions", 1)[1] if "Owner decisions" in sec else ""
        self.assertGreaterEqual(len(re.findall(r"^- ", owner, re.M)), 1)

    def test_S6_the_prompt_says_a_convenience_and_lists_every_action(self):
        text = read(WORKER_MD)
        self.assertIn("a convenience", text)
        self.assertEqual([r[0] for r in rows() if r[0] not in text], [])
        self.assertIn("never a boundary", text)

    def test_the_removed_verbs_are_listed_in_the_readme(self):
        text = read(README)
        sec = re.search(r"^### Verbs that no longer work\n(.*?)(?=^#)", text, re.S | re.M)
        self.assertTrue(sec)
        for verb in ("gh issue comment", "gh issue create", "gh issue edit", "gh issue close", "gh pr merge", "gh pr review",
                     "gh pr edit", "gh pr close", "gh label", "gh release", "gh workflow", "gh extension", "gh alias", "gh auth",
                     "gh repo edit", "gh secret", "gh run rerun"):
            self.assertIn(verb, sec.group(1), verb)


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_renderings()
        print("renderings written")
    else:
        unittest.main()
