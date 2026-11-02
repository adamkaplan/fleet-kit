"""`fleet-switchboard preflight-pr --body PR`: the description against the plan's cause-first reviewer scenario (ask of 28 Oct, plan S1-S10, U1, U2).

The three recorded shapes are SYNTHETIC (neutral names): the why block first, places without line numbers, fewer than N places.
The headings are read from the page's own rendering (`body_html`): the S11 fixtures are REAL renderings; the other tests build theirs with `render`."""
import contextlib
import html as htmllib
import json
import importlib.machinery
import importlib.util
import io
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
ENTRY = REPO / "bin" / "fleet-switchboard"
PART = REPO / "bin" / "switchboard" / "body_check.py"


def load_entry(path=ENTRY, name="sb_body_check_t"):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


SB = load_entry()


def plan(rule, text="Given the description, when read, then it names the cause first in the first section", evaluator="reviewer", claim="pr"):
    return ("## Plan (scenarios): demo\n\n```yaml\nscenarios:\n  - id: S1\n    covers: DW-1\n    text: %s\n    proof: screenshot\n    pass: %s\n"
            "    discriminates: a description that opens with the change fails\n    evaluator: %s\n    claim: %s\n```\n" % (text, rule, evaluator, claim))


def render(raw):
    """The page's rendering of the few constructs the tests use, in the shape of the real ones: `## x` h2, `**x**` p/strong, `- x` ul/li (a backticked
    text is code), a fenced block pre/code, an HTML comment nothing, any other paragraph p."""
    def inline(text):
        return re.sub(r"`([^`]*)`", r'<code class="notranslate">\1</code>', htmllib.escape(text, quote=False))
    out, para, items, fence = [], [], [], None
    def flush():
        nonlocal para, items
        if para:
            out.append('<p dir="auto">%s</p>' % inline(" ".join(para)))
        if items:
            out.append('<ul dir="auto">\n%s\n</ul>' % "\n".join("<li>%s</li>" % inline(i) for i in items))
        para, items = [], []
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    for line in raw.splitlines():
        if fence is not None:
            if line.startswith("```"):
                out.append('<pre class="notranslate"><code class="notranslate">%s\n</code></pre>' % htmllib.escape("\n".join(fence), quote=False))
                fence = None
            else:
                fence.append(line)
        elif line.startswith("```"):
            flush()
            fence = []
        elif line.startswith("## "):
            flush()
            out.append('<h2 dir="auto">%s</h2>' % inline(line[3:]))
        elif line.startswith("- "):
            if para:
                flush()
            items.append(line[2:])
        elif line.startswith("**") and line.endswith("**"):
            flush()
            out.append('<p dir="auto"><strong>%s</strong></p>' % inline(line[2:-2]))
        elif line.strip():
            if items:
                flush()
            para.append(line)
        else:
            flush()
    flush()
    return "\n".join(out)


# The REAL renderings of eight descriptions, captured 2 Nov from GitHub's markdown renderer (repo adamkaplan/fleet-kit): the raw text and `body_html` of each
REAL = {
    "d1": (
        '<!-- pr-explainer:begin -->\n\n**Why (before and after)**\n\n```text\nbefore: x\nafter:  y\n```\n\n<!-- pr-explainer:end -->\n\n```\n## Cause (DW-1): the case\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n```\n\n## Why this needs you\n\ntext\n',
        '\n<p dir="auto"><strong>Why (before and after)</strong></p>\n<pre lang="text" class="notranslate"><code class="notranslate">before: x\nafter:  y\n</code></pre>\n\n<pre class="notranslate"><code class="notranslate">## Cause (DW-1): the case\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n</code></pre>\n<h2 dir="auto">Why this needs you</h2>\n<p dir="auto">text</p>'),
    "d2": (
        'MERGE: captain\n\n## Cause (DW-1): the case\n\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n\n```\n## Why this needs you\n```\n\n## Why this needs you\n\ntext\n',
        '<p dir="auto">MERGE: captain</p>\n<h2 dir="auto">Cause (DW-1): the case</h2>\n<ul dir="auto">\n<li><code class="notranslate">bin/a.py:10</code>, <code class="notranslate">bin/b.py:20</code>, <code class="notranslate">bin/c.py:30</code> and <code class="notranslate">bin/d.py:40</code></li>\n</ul>\n<pre class="notranslate"><code class="notranslate">## Why this needs you\n</code></pre>\n<h2 dir="auto">Why this needs you</h2>\n<p dir="auto">text</p>'),
    "d3": (
        '<!-- pr-explainer:begin -->\n\n**Why (before and after)**\n\n```text\nbefore: x\nafter:  y\n```\n\n<!-- pr-explainer:end -->\n\n~~~\n## Cause (DW-1): the case\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n~~~\n\n## Why this needs you\n\ntext\n',
        '\n<p dir="auto"><strong>Why (before and after)</strong></p>\n<pre lang="text" class="notranslate"><code class="notranslate">before: x\nafter:  y\n</code></pre>\n\n<pre class="notranslate"><code class="notranslate">## Cause (DW-1): the case\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n</code></pre>\n<h2 dir="auto">Why this needs you</h2>\n<p dir="auto">text</p>'),
    "d4": (
        '<!-- pr-explainer:begin -->\n\n**Why (before and after)**\n\n```text\nbefore: x\nafter:  y\n```\n\n<!-- pr-explainer:end -->\n\n```\n## Cause (DW-1): the case\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n\n## Why this needs you\n\ntext\n',
        '\n<p dir="auto"><strong>Why (before and after)</strong></p>\n<pre lang="text" class="notranslate"><code class="notranslate">before: x\nafter:  y\n</code></pre>\n\n<pre class="notranslate"><code class="notranslate">## Cause (DW-1): the case\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n\n## Why this needs you\n\ntext\n</code></pre>'),
    "d5": (
        'MERGE: captain\n\n```\n## Why this needs you\n```\n\n## Cause (DW-1): the case\n\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n\n## Why this needs you\n\ntext\n',
        '<p dir="auto">MERGE: captain</p>\n<pre class="notranslate"><code class="notranslate">## Why this needs you\n</code></pre>\n<h2 dir="auto">Cause (DW-1): the case</h2>\n<ul dir="auto">\n<li><code class="notranslate">bin/a.py:10</code>, <code class="notranslate">bin/b.py:20</code>, <code class="notranslate">bin/c.py:30</code> and <code class="notranslate">bin/d.py:40</code></li>\n</ul>\n<h2 dir="auto">Why this needs you</h2>\n<p dir="auto">text</p>'),
    "d6": (
        'MERGE: captain\n\n<!--\n## Cause (DW-1): the case\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n-->\n\n## Why this needs you\n\ntext\n',
        '<p dir="auto">MERGE: captain</p>\n\n<h2 dir="auto">Why this needs you</h2>\n<p dir="auto">text</p>'),
    "d7": (
        'MERGE: captain\n\n<!--\n## Why this needs you\n-->\n\n## Cause (DW-1): the case\n\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n\n## Why this needs you\n\ntext\n',
        '<p dir="auto">MERGE: captain</p>\n\n<h2 dir="auto">Cause (DW-1): the case</h2>\n<ul dir="auto">\n<li><code class="notranslate">bin/a.py:10</code>, <code class="notranslate">bin/b.py:20</code>, <code class="notranslate">bin/c.py:30</code> and <code class="notranslate">bin/d.py:40</code></li>\n</ul>\n<h2 dir="auto">Why this needs you</h2>\n<p dir="auto">text</p>'),
    "d8": (
        'MERGE: captain\n\nCause (DW-1): the case\n------------------------\n\n- `bin/a.py:10`, `bin/b.py:20`, `bin/c.py:30` and `bin/d.py:40`\n\n## Why this needs you\n\ntext\n',
        '<p dir="auto">MERGE: captain</p>\n<h2 dir="auto">Cause (DW-1): the case</h2>\n<ul dir="auto">\n<li><code class="notranslate">bin/a.py:10</code>, <code class="notranslate">bin/b.py:20</code>, <code class="notranslate">bin/c.py:30</code> and <code class="notranslate">bin/d.py:40</code></li>\n</ul>\n<h2 dir="auto">Why this needs you</h2>\n<p dir="auto">text</p>'),
}


PLACES = "\n".join("- `src/mod_%d.py:%d` the place" % (i, 10 + i) for i in range(1, 7))
GOOD = "PLAN: acme-org/demo#7\n\n## The cause\n\nThe places:\n%s\n\n## Why\n\nbefore and after\n\n## The change\n\nwe edit it\n" % PLACES
WHY_FIRST = "PLAN: acme-org/demo#7\n\n<!-- pr-explainer:begin -->\n\n**Why (before and after)**\n\n<!-- pr-explainer:end -->\n\n## The cause\n\n%s\n" % PLACES
HEADING_ORDER = "PLAN: acme-org/demo#7\n\n## Why\n\nbefore and after\n\n## The cause\n\n%s\n" % PLACES
NO_LINES = "PLAN: acme-org/demo#7\n\n## The cause\n\n- `src/mod_a.py` the place\n- `src/mod_b.py` the place\n- `src/mod_c.py` the place\n"
FEW = "PLAN: acme-org/demo#7\n\n## The cause\n\n- `src/mod_a.py:12` the place\n- `src/mod_b.py:30` the place\n\n## The change\n\nedit\n"
LINE_RULE = "the first section names at least 4 places with function and line"


class Run:
    """The command with its readers faked; every call a fake sees is kept, so a non-GET call or a write can be counted."""

    def __init__(self, body, plan_text, fail=None, sb=None, arg="acme-org/demo#9", html=None):
        self.arg = arg
        self.sb = sb or SB
        self.calls, self.fail = [], fail
        self.body, self.plan_text = body, plan_text
        self.html = render(body) if html is None else html   # html=False: the field is absent

    def __call__(self):
        SB = self.sb
        def read_pr_json(engine, repo, number, extra_fields=""):
            self.calls.append("GET pr")
            if self.fail == "pr":
                raise SB.NoticeError("gh pr view failed: boom")
            return {"body": self.body}

        def fetch_plan(reader, repo, number, comment_id=None):
            self.calls.append("GET plan")
            if self.fail == "plan":
                raise SB.PlanInputError("cannot read plan")
            return self.plan_text, "comment", "", []

        outer = self

        class Accounts:
            """The account reader: its `read` answers the pull request's JSON (the rendering) and keeps the call and its header."""
            def __init__(self, *a, **k):
                pass

            def read(self, runner, path, repo, timeout, extra_flags=(), **kw):
                outer.calls.append("GET html %s %s" % (path, " ".join(extra_flags)))
                if outer.fail == "html":
                    return subprocess.CompletedProcess([], 1, "", "boom")
                doc = {"body": outer.body} if outer.html is False else {"body": outer.body, "body_html": outer.html}
                return subprocess.CompletedProcess([], 0, json.dumps(doc), "")

        args = SB.argparse.Namespace(body=self.arg, merged=False)
        out = io.StringIO()
        cfg = {"gh": "gh", "call_timeout_seconds": 5, "intent_repo": "acme-org/demo"}
        with mock.patch.object(SB, "read_pr_json", read_pr_json), mock.patch.object(SB, "fetch_plan", fetch_plan), \
                mock.patch.object(SB, "build_engine", lambda *a, **k: (object(), None, None)), mock.patch.object(SB, "load_config", lambda: cfg), \
                mock.patch.object(SB, "IntentReader", lambda *a, **k: object()), mock.patch.object(SB, "GhAccounts", Accounts), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("no process may run")), contextlib.redirect_stdout(out):
            code = SB.cmd_preflight_pr(args, lambda *a, **k: (_ for _ in ()).throw(AssertionError("no runner call")), lambda: 0)
        return code, out.getvalue()


def run(body, rule=LINE_RULE, **kw):
    return Run(body, plan(rule, **kw.pop("plan_kw", {})), **kw)()


S12_RULE_PLAN = plan("the first section names at least 2 named items", text="Given the line of the description, when read, then the cause is first")
TWO_ITEMS = "PLAN: acme-org/demo#7\n\n## The cause\n\nSee #101 and #102.\n"
BC_NOT_FIRST = "the cause section is not first"
S3_MSG = "places with a file and line: found 0 of at least 4"


def check_s3(sb=None):
    """The S3 check: the description with 3 places without a line is refused with the counts. Raises AssertionError anchored on its own message."""
    code, out = Run(NO_LINES, plan(LINE_RULE), sb=sb)()
    if S3_MSG not in out or code != 1:
        raise AssertionError("S3 check: '%s' missing (exit %s): %s" % (S3_MSG, code, out.strip()))


def real(name):
    """(raw, html) of a real rendering, with the PLAN: line the command reads added to both (the captured descriptions carry none)."""
    raw, html = REAL[name]
    return "PLAN: acme-org/demo#7\n\n" + raw, '<p dir="auto">PLAN: acme-org/demo#7</p>\n' + html


def check_s11(sb=None):
    """The eight real descriptions: d1, d3, d4, d6 refused with the item, d2, d5, d7, d8 pass. Raises AssertionError anchored on its own message."""
    wrong = []
    for name in sorted(REAL):
        raw, html = real(name)
        code, out = Run(raw, plan(LINE_RULE), sb=sb, html=html)()
        if name in ("d1", "d3", "d4", "d6"):
            ok = code == 1 and out.count(BC_NOT_FIRST) == 1
        else:
            ok = code == 0 and "PASS" in out
        if not ok:
            wrong.append(name)
    if wrong:
        raise AssertionError("S11 check: wrong answers for %s" % ",".join(wrong))


def check_s12_form(sb=None):
    code, out = Run(TWO_ITEMS, S12_RULE_PLAN, sb=sb)()
    if code != 0 or "PASS" not in out:
        raise AssertionError("S12 form check: the pass field says items, expected PASS (exit %s): %s" % (code, out.strip()))


def check_s12_empty(sb=None):
    try:
        code, out = Run(GOOD, plan(LINE_RULE), sb=sb, arg="")()
    except Exception as err:   # the old path ran (a runner or process call)
        raise AssertionError("S12 empty check: expected 'not checked', the old path ran: %r" % (err,))
    if code != 1 or out.count("not checked") != 1 or "PASS" in out or "FAIL" in out:
        raise AssertionError("S12 empty check: expected one 'not checked' line and exit 1, got exit %s: %s" % (code, out.strip()))


class TestBodyCheck(unittest.TestCase):
    def source_copy(self, tmp, edits=()):
        """A copy of bin/ in tmp with each (file under bin/, old, new) edit applied exactly once; returns the loaded entry."""
        import shutil
        for src in [p for p in (REPO / "bin").iterdir() if p.name != "test-switchboard" and (p.is_dir() or p.suffix in ("", ".py", ".parts"))]:
            dst = Path(tmp) / "bin" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst) if src.is_dir() else shutil.copy(src, dst)
        for rel, old, new in edits:
            target = Path(tmp) / "bin" / rel
            source = target.read_text()
            assert source.count(old) == 1, "mutation not applied exactly once in %s: %d" % (rel, source.count(old))
            target.write_text(source.replace(old, new))
        return load_entry(Path(tmp) / "bin" / "fleet-switchboard", "sb_bc_%s" % abs(hash((str(tmp), str(edits)))))

    def mutant_fails(self, check, edits):
        """Control first (the unmutated copy passes the check), then the mutant copy must fail it; returns the failure text."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            check(self.source_copy(Path(tmp) / "control"))
            with tempfile.TemporaryDirectory() as tmp2:
                mutant = self.source_copy(Path(tmp2), edits)
                with self.assertRaises(AssertionError) as caught:
                    check(mutant)
        return str(caught.exception)

    def test_s8_the_documentation_sentence_and_the_unit_row_each_once(self):
        doc = (REPO / "docs" / "switchboard.md").read_text()
        sentences = [l for l in doc.splitlines() if "preflight-pr --body" in l]
        self.assertEqual(len(sentences), 1, "docs/switchboard.md: the sentence naming 'preflight-pr --body' occurs %d times, expected 1" % len(sentences))
        arch = (REPO / "docs" / "architecture.md").read_text()
        head, _, known = arch.partition("\n## Known violations")
        unit = [l for l in head.splitlines() if l.startswith("| `switchboard/body_check.py` |")]
        self.assertEqual(len(unit), 1, "docs/architecture.md: the unit-table row of switchboard/body_check.py occurs %d times, expected 1" % len(unit))
        rows = [l for l in known.splitlines() if l.startswith("|") and "switchboard/body_check.py" in l]
        self.assertEqual(len(rows), 0, "docs/architecture.md: %d Known-violations rows name switchboard/body_check.py, expected 0" % len(rows))

    def test_s2_the_why_block_first_is_refused_by_name(self):
        for shape in (WHY_FIRST, HEADING_ORDER):
            code, out = run(shape)
            self.assertEqual(code, 1)
            self.assertEqual(out.count("the cause section is not first"), 1, out)

    def test_s3_places_without_a_line_are_refused_with_the_counts(self):
        code, out = run(NO_LINES)
        self.assertEqual(code, 1)
        self.assertIn("places with a file and line: found 0 of at least 4", out)
        self.assertNotIn("the cause section is not first", out)

    def test_fewer_than_n_places_is_refused(self):
        code, out = run(FEW)
        self.assertEqual(code, 1)
        self.assertIn("places with a file and line: found 2 of at least 4", out)

    def test_s5_good_passes_and_each_bad_shape_has_its_own_item(self):
        self.assertEqual(run(GOOD)[0], 0)
        items = set()
        for bad in (WHY_FIRST, NO_LINES, FEW):
            code, out = run(bad)
            self.assertEqual(code, 1)
            items.add(re.findall(r"REFUSED: (.*)", out)[0])
        self.assertEqual(len(items), 3)

    def test_s6_a_build_that_skips_the_count_check_fails_the_s3_check(self):
        old = "    if (got < count) if form == \"at least\" else (got != count):\n"
        text = self.mutant_fails(check_s3, [("switchboard/body_check.py", old, "    if False:\n")])
        self.assertIn("S3 check: '%s' missing" % S3_MSG, text)

    def test_s3_the_check_function_passes_on_the_real_build(self):
        check_s3()

    def test_s11_the_real_renderings_are_read_as_the_page_shows_them(self):
        check_s11()
        for name in ("d1", "d3", "d4", "d6"):
            raw, html = real(name)
            self.assertEqual(Run(raw, plan(LINE_RULE), html=html)()[1].count(BC_NOT_FIRST), 1, name)
        for name in ("d2", "d5", "d7", "d8"):
            raw, html = real(name)
            self.assertEqual(Run(raw, plan(LINE_RULE), html=html)()[0], 0, name)

    def test_s11_a_rendering_absent_or_empty_is_not_checked(self):
        raw, html = real("d2")
        for shown in (False, ""):
            code, out = Run(raw, plan(LINE_RULE), html=shown)()
            self.assertEqual(code, 1)
            self.assertEqual(out.count("not checked"), 1, out)
        code, out = Run(raw, plan(LINE_RULE), fail="html")()
        self.assertEqual((code, out.count("not checked")), (1, 1))

    def test_s11_the_rendering_is_one_get_with_the_html_header(self):
        runner = Run(GOOD, plan(LINE_RULE))
        runner()
        gets = [c for c in runner.calls if c.startswith("GET html")]
        self.assertEqual(gets, ["GET html repos/acme-org/demo/pulls/9 -H Accept: application/vnd.github.html+json"])

    def test_s11_a_mutant_that_reads_the_headings_from_the_raw_text_fails(self):
        old = "    heading, text, before, found, why = body_rendered(html)\n"
        new = ("    first = _body_re(r\"(?m)^##[ \\t]+(.*)$\").search(html)\n"
               "    heading, text, before, found, why = (first.group(1) if first else \"\"), html[first.end():] if first else \"\", html[:first.start()] if first else \"\", bool(first), "
               "bool(first and \"Why (before and after)\" in html[:first.start()])\n")
        feed = ("r, u = body_check_problems(html, scenario)", "r, u = body_check_problems(body, scenario)")   # the old rule: the check is given the RAW text
        text = self.mutant_fails(check_s11, [("switchboard/body_check.py", feed[0], feed[1]), ("switchboard/body_check.py", old, new)])
        self.assertIn("S11 check: wrong answers for", text)
        for name in ("d5", "d7", "d6"):
            self.assertIn(name, text.split("wrong answers for")[1].replace(" ", "").split(","))

    def test_s12_the_form_is_read_from_the_pass_field_only(self):
        check_s12_form()

    def test_s12_an_empty_body_argument_is_not_checked(self):
        check_s12_empty()

    def test_s12_a_mutant_reading_the_form_from_the_text_fails(self):
        old = '    if _body_re(r"\\bline\\b", "I").search(str(scenario.get("pass", ""))):\n'
        new = '    if _body_re(r"\\bline\\b", "I").search(str(scenario.get("pass", "")) + " " + str(scenario.get("text", ""))):\n'
        text = self.mutant_fails(check_s12_form, [("switchboard/body_check.py", old, new)])
        self.assertIn("S12 form check: the pass field says items, expected PASS", text)

    def test_s12_a_mutant_testing_the_argument_for_truth_fails(self):
        old = 'getattr(args, "body", None) is not None:'
        text = self.mutant_fails(check_s12_empty, [("fleet-switchboard", old, 'getattr(args, "body", None):')])
        self.assertIn("S12 empty check: expected", text)

    def test_s4_the_count_forms_and_the_no_count_rule(self):
        self.assertEqual(SB.body_rule_count("on 28 Oct the section names at least 4 places"), (4, "at least"))
        self.assertEqual(SB.body_rule_count("the first section names the 3 cases (count equals 3)"), (3, "count equals"))
        self.assertEqual(SB.body_rule_count("the first section names the cause, as on 28 Oct"), (None, None))
        code, out = run(GOOD, rule="the first section is the cause, seen on 28 Oct")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("not checked"), 1)
        self.assertIn("not checked: the pass rule has no count", out)
        code, out = run(WHY_FIRST, rule="the first section is the cause, seen on 28 Oct")
        self.assertIn("the cause section is not first", out)

    def test_count_equals_is_exact_and_items_count_without_lines(self):
        rule = "the first section names the cases (count equals 3)"
        three = "PLAN: acme-org/demo#7\n\n## The cause\n\nSee #101, #102 and #103.\n"
        self.assertEqual(run(three, rule=rule)[0], 0)
        code, out = run(three + "and #104\n", rule=rule)
        self.assertEqual(code, 1)
        self.assertIn("named items: found 4 of count equals 3", out)

    def test_s7_a_failed_read_is_not_checked_and_writes_nothing(self):
        for where in ("pr", "plan"):
            runner = Run(GOOD, plan(LINE_RULE), fail=where)
            code, out = runner()
            self.assertNotEqual(code, 0)
            self.assertEqual(out.count("not checked"), 1, out)
            self.assertEqual([c for c in runner.calls if not c.startswith("GET")], [])

    def test_s9_a_plan_with_no_cause_first_scenario_is_not_checked(self):
        for other in (plan("the table lists 2 rows", text="Given x, then y"), plan(LINE_RULE, evaluator="ci"), plan(LINE_RULE, claim="epic")):
            code, out = Run(GOOD, other)()
            self.assertNotEqual(code, 0)
            self.assertEqual(out.count("not checked"), 1)
            self.assertIn("no cause-first scenario", out)

    def test_u2_only_gets_are_made(self):
        runner = Run(GOOD, plan(LINE_RULE))
        self.assertEqual(runner()[0], 0)
        self.assertEqual([c.split(" repos")[0] for c in runner.calls], ["GET pr", "GET html", "GET plan"])

    def test_u1_without_body_the_command_is_unchanged(self):
        args = SB.argparse.Namespace(body=None, merged=False)
        with mock.patch.object(SB, "cmd_body_check", side_effect=AssertionError("must not run")), contextlib.redirect_stderr(io.StringIO()):
            code = SB.cmd_preflight_pr(args, lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""), lambda: 0)
        self.assertEqual(code, SB.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
