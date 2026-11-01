#!/usr/bin/env python3
"""Offline tests: `scrub` makes one pass over the text (ask #1776, PC-1, PC-2). The old body is the frozen reference of every check.
Scenarios S2 S3 S4 S5 S8 S11 S13 and U1. Run directly: python3 cloud/launcher/tests/test_scrub_linear.py
Only counts, lengths and names are printed, never a fixture value."""
import contextlib
import importlib.util
import inspect
import io
import itertools
import linecache
import os
import random
import re
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
_spec = importlib.util.spec_from_file_location("scrub_linear_cw", str(ROOT / "cloud" / "launcher" / "cloud_worker.py"))
cw = importlib.util.module_from_spec(_spec)
sys.modules["scrub_linear_cw"] = cw
_spec.loader.exec_module(cw)
SEED = 1776

# THE FROZEN REFERENCE: the tip's body of `scrub`, verbatim, with its one loop line taking an explicit list.
_OLD_LOOP = "    for sec in sorted({v for v in secrets_known if v and len(v.strip()) >= MIN_FRAGMENT}, key=len, reverse=True):\n"
_NEW_LOOP = "    for sec in _ORDER:\n"
_FROZEN = '''def old_body(text, secrets_known=(), limit=300):
    text = str(text)[:SCRUB_INPUT_MAX]
''' + _OLD_LOOP + '''        for v in {sec, sec.strip()}:
            text = text.replace(v, "[redacted]")
        out, i = [], 0
        while i < len(text):  # any run of 8+ characters that is a piece of the secret
            head = text[i:i + MIN_FRAGMENT]
            if len(head) == MIN_FRAGMENT and head in sec:
                j = i + MIN_FRAGMENT
                while j < len(text) and text[i:j + 1] in sec:
                    j += 1
                out.append("[redacted]")
                i = j
            else:
                out.append(text[i])
                i += 1
        text = "".join(out)
    for pat, repl in CREDENTIAL_SHAPES:
        text = pat.sub(repl, text)
    for pat in TOKEN_SHAPES:
        text = pat.sub("[redacted]", text)
    return re.sub(r"[^\\x20-\\x7e]", "?", text)[:limit]
'''
assert _FROZEN.count(_OLD_LOOP) == 1
_SRC = _FROZEN.replace(_OLD_LOOP, _NEW_LOOP)
assert _SRC != _FROZEN and _SRC.count(_NEW_LOOP) == 1
_NS = {k: getattr(cw, k) for k in ("MIN_FRAGMENT", "SCRUB_INPUT_MAX", "CREDENTIAL_SHAPES", "TOKEN_SHAPES")}
_NS["re"] = re
_FILE = "<frozen-old-scrub>"
linecache.cache[_FILE] = (len(_SRC), None, _SRC.splitlines(True), _FILE)
exec(compile(_SRC, _FILE, "exec"), _NS)
_old_body = _NS["old_body"]


def forms_of(secs):
    return [v for v in set(secs) if v and len(v.strip()) >= cw.MIN_FRAGMENT]


def ref(text, secs, limit=300, order=None):
    """The reference: the frozen body over the explicit order, by default longest first then by the text."""
    if order is None:
        order = sorted(forms_of(secs), key=lambda v: (-len(v), v))
    _NS["_ORDER"] = order
    return _old_body(text, secs, limit)


def rnd(r, n, alpha):
    return "".join(r.choice(alpha) for _ in range(n))


def same(fn, text, secs, name, limit=10 ** 7):
    a, b = ref(text, secs, limit), fn(text, secs, limit)
    if a != b:  # counts only: never the strings
        raise AssertionError("the new scrub differs from the old one: %s (reference length %d, output length %d)" % (name, len(a), len(b)))
    return len(b)


def count_lines(fn, needle, *args):
    """Operation count by line events: how many times a line holding `needle` ran. No clock."""
    n = [0]
    code = fn.__code__

    def tracer(frame, event, arg):
        if frame.f_code is code:
            return local
        return None

    def local(frame, event, arg):
        if event == "line" and needle in linecache.getline(frame.f_code.co_filename, frame.f_lineno):
            n[0] += 1
        return local
    sys.settrace(tracer)
    try:
        fn(*args)
    finally:
        sys.settrace(None)
    return n[0]


def case_sets(r):
    """The 309 cases of S13: 5 fixed, 200 equal-length random, 100 mixed random, the 4 plain kinds."""
    cases = [("zz abcdefgh12xy zz", ["abcdefgh12", "cdefgh12xy"]),
             ("xx abcdefgh1 [ab]cdefgh yy", ["abcdefgh1", "[ab]cdefgh", "cdefgh1234", "bcdefgh12]"]),
             ("a[abcdefgh]b[redacted]c", ["[abcdefgh", "abcdefgh]", "[redacted]x", "edacted]xy"]),
             ("12345678901234567890", ["1234567890", "0987654321", "4567890123", "7890123456"]),
             ("aaaaaaaaaaaaaaaaaaaa", ["aaaaaaaaa", "aaaaaaaab", "baaaaaaaa", "aaaaaaaaaa"])]
    for _ in range(200):
        n = r.randint(8, 12)
        secs = [rnd(r, n, "abcd[]") for _ in range(r.randint(4, 6))]
        cases.append(("".join(r.choice(secs)[r.randint(0, 3):r.randint(6, n)] + r.choice(["", "x", " "]) for _ in range(8)), secs))
    for _ in range(100):
        secs = [rnd(r, r.randint(8, 16), "abcde[]") for _ in range(r.randint(1, 5))]
        cases.append(("".join(r.choice(secs)[r.randint(0, 4):r.randint(8, 16)] + rnd(r, 2, "abcde") for _ in range(8)), secs))
    p = "p" + "Qw3rT7yU1iO5pA9sD2fG4hJ6kL8"
    cases += [("a " + p + " b", [p]), ("a  spaced one  b spaced", ["  spaced one  "]), ("b64 " + "QUJD" * 8, ["QUJDQUJDQUJD"]), ("nothing here", [])]
    return cases


class S2Differential(unittest.TestCase):
    def test_S2_random_cases(self):
        r = random.Random(SEED)
        bad = 0
        for _ in range(2000):
            secs = [rnd(r, r.randint(0, 14), "ab[]redact d") for _ in range(r.randint(0, 5))]
            text = rnd(r, r.randint(0, 60), "ab[]redact d")
            bad += cw.scrub(text, set(secs), 5000) != ref(text, set(secs), 5000)
        for _ in range(200):
            secs = [rnd(r, r.randint(8, 12), "abcd") for _ in range(r.randint(1, 40))]
            text = rnd(r, 300, "abcd")
            bad += cw.scrub(text, set(secs), 5000) != ref(text, set(secs), 5000)
        print("S2 seed %d cases 2200 differing %d" % (SEED, bad))
        self.assertEqual(bad, 0, "the new scrub differs from the old one")

    def test_S2_worst_case(self):
        r = random.Random(5)
        secs = {rnd(r, 20, "abcdefgh") for _ in range(50)}
        text = ("".join(s[2:14] + " " for s in secs) * 30)[:20000]
        self.assertEqual(cw.scrub(text, secs, 30000), ref(text, secs, 30000))
        new = count_lines(cw.scrub, "found = index.get(", text, secs, 30000)
        _NS["_ORDER"] = sorted(forms_of(secs), key=lambda v: (-len(v), v))
        old = count_lines(_old_body, "head = text[i:i + MIN_FRAGMENT]", text, secs, 30000)
        print("S2 worst case equal 1 of 1, new count %d, old count %d" % (new, old))
        self.assertLessEqual(new, old)


class S3Fixtures(unittest.TestCase):
    def test_S3_fixtures(self):
        p = "ghp_" + "Qw3rT7yU1iO5pA9sD2fG4hJ6kL8zX0cV"
        many = {"secret%03dvalue" % i for i in range(60)}
        fx = {
            "no secret": ("hello world " * 20, set()),
            "one secret": ("x " + p + " y", {p}),
            "many secrets": (" ".join(sorted(many)), many),
            "prefix": ("aaaaaaaabbbbbbbbcccc aaaaaaaabbbbbbbb", {"aaaaaaaabbbbbbbb", "aaaaaaaabbbbbbbbcccc"}),
            "twice": (p + " " + p, {p}),
            "overlap": ("abcdefghijklmnopqrst", {"abcdefghijkl", "ghijklmnopqrst"}),
            "regex chars": ("a.*+?(b)[c]{d}|^$ and a.*+?(b)[c]{d}|^$", {"a.*+?(b)[c]{d}|^$"}),
            "empty": ("some text here", {"", "   "}),
            "5MiB": (("lorem ipsum " + p + " ") * 200000, {p}),
            "shapes": ("Authorization: Bearer abc123def456ghi789 https://user:pw@host/x " + "A" * 60 + " " + "0123456789abcdef" * 4
                       + " eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl", set()),
            "spaces ends": ("a  spaced secret  b", {"  spaced secret  "}),
            "equal length": ("abcdefgh12345678", {"abcdefgh", "12345678"}),
        }
        builds = (list, lambda s: list(reversed(sorted(s))), set, frozenset)
        for name, (text, secs) in fx.items():
            outs = set()
            for build in builds:
                n = same(cw.scrub, text, build(secs), name)
                outs.add(cw.scrub(text, build(secs), 10 ** 7))
            self.assertEqual(len(outs), 1, "the output depends on how the set is built: " + name)
            print("S3 %s: input %d output %d, 4 builds equal" % (name, len(text), n))

    def test_U1_signature(self):
        self.assertEqual(str(inspect.signature(cw.scrub)), "(text, secrets_known=(), limit=300)")
        print("U1 signature 1 of 1")


class S4Counts(unittest.TestCase):
    def test_S4_ratios(self):
        r = random.Random(4)
        s50 = {rnd(r, 30, "ABCDEFGHIJKLMNOP") for _ in range(50)}
        s100 = s50 | {rnd(r, 30, "ABCDEFGHIJKLMNOP") for _ in range(50)}
        t1, t2 = "z" * 20000, "z" * 40000
        cases = {"1": (t1, s50), "2": (t2, s50), "f": (t1, s100)}
        n = {k: count_lines(cw.scrub, "found = index.get(", t, s, 10 ** 6) for k, (t, s) in cases.items()}
        o = {}
        for k, (t, s) in cases.items():
            _NS["_ORDER"] = sorted(forms_of(s), key=lambda v: (-len(v), v))
            o[k] = count_lines(_old_body, "head = text[i:i + MIN_FRAGMENT]", t, s, 10 ** 6)
        print("S4 new counts %s old counts %s" % (n, o))
        print("S4 new size x%.3f forms x%.3f; old size x%.3f forms x%.3f" % (n["2"] / n["1"], n["f"] / n["1"], o["2"] / o["1"], o["f"] / o["1"]))
        self.assertTrue(1.9 <= n["2"] / n["1"] <= 2.1, "new count not in step with the text")
        self.assertTrue(1.0 <= n["f"] / n["1"] <= 1.1, "new count not flat in the forms")
        self.assertEqual((o["2"] / o["1"], o["f"] / o["1"]), (2.0, 2.0))


class S5Mutants(unittest.TestCase):
    def _run(self, fn):
        r = random.Random(7)
        for _ in range(500):
            secs = {rnd(r, r.randint(8, 14), "abcd") for _ in range(r.randint(1, 4))}
            text = rnd(r, 80, "abcd")
            if fn(text, secs, 5000) != ref(text, secs, 5000):
                raise AssertionError("the new scrub differs from the old one")

    def test_S5_control_and_mutants(self):
        self._run(cw.scrub)
        one_less = mutant("for n in sorted(hit):", "for n in sorted(hit)[1:]:")
        cut = mutant("j = i + MIN_FRAGMENT\n", "j = i + MIN_FRAGMENT + 1\n")
        for m in (one_less, cut):
            with self.assertRaisesRegex(AssertionError, "the new scrub differs from the old one"):
                self._run(m)
        print("S5 control 1 of 1, mutants failing 2 of 2")


def mutant(old, new):
    src = inspect.getsource(cw.scrub)
    assert src.count(old) == 1, "the mutation text must occur once"
    assert old != new
    ns = dict(vars(cw))
    exec(src.replace(old, new), ns)
    return ns["scrub"]


class S13Contract(unittest.TestCase):
    def _check(self, fn, quiet=False):
        r = random.Random(SEED)
        cases = case_sets(r)
        surv_new = surv_ref = perm_cases = perm_in = 0
        for text, secs in cases:
            a, b = ref(text, secs, 10 ** 6), fn(text, secs, 10 ** 6)
            if a != b:
                raise AssertionError("the output is not the reference output")
            outs = {fn(text, build(secs), 10 ** 6) for build in (list, lambda s: list(reversed(s)), set, frozenset)}
            if len(outs) != 1:
                raise AssertionError("the output depends on how the set is built")
            f = forms_of(secs)
            surv_new += any(s in b for s in f)
            surv_ref += any(s in a for s in f)
            groups = [sorted(g) for _, g in itertools.groupby(sorted(f, key=lambda v: (-len(v), v)), key=len)]
            if all(len(g) <= 4 for g in groups):
                perm_cases += 1
                prod = itertools.product(*[itertools.permutations(g) for g in groups])
                allowed = {ref(text, secs, 10 ** 6, order=[s for g in o for s in g]) for o in prod}
                perm_in += b in allowed
        if not quiet:
            print("S13 seed %d cases %d byte-equal %d; permutation cases %d, in set %d; survivors new %d reference %d"
                  % (SEED, len(cases), len(cases), perm_cases, perm_in, surv_new, surv_ref))
        self.assertEqual(perm_in, perm_cases)
        self.assertLessEqual(surv_new, surv_ref)

    def test_S13_contract_and_mutant(self):
        self._check(cw.scrub)
        m = mutant("key=lambda v: (-len(v), v)", "key=lambda v: (-len(v), [-ord(c) for c in v])")
        with self.assertRaisesRegex(AssertionError, "the output is not the reference output"):
            self._check(m, quiet=True)
        print("S13 control 1 of 1, mutant fails 1 of 1")


class Hooked(set):
    calls = 0

    def __iter__(self):
        Hooked.calls += 1
        it = super().__iter__()
        yield next(it)
        self.add("late-form-added-%d" % Hooked.calls)
        yield from it


def tip_scrub(text, secrets_known, limit=300):
    """The tip's loop line over the live set (the cause of the crash), only for the third case of S8."""
    for sec in sorted({v for v in secrets_known if v and len(v.strip()) >= 8}, key=len, reverse=True):
        text = text.replace(sec, "[redacted]")
    return text


class S8Snapshot(unittest.TestCase):
    def test_S8_next_call_honours_added(self):
        known, text = {"firstsecret1"}, "a firstsecret1 b secondsecret2 c"
        self.assertIn("secondsecret2", cw.scrub(text, known))
        known.add("secondsecret2")
        out = cw.scrub(text, known)
        self.assertNotIn("secondsecret2", out)
        self.assertEqual(out, ref(text, known))
        print("S8 redacted after add 1 of 1, equal to the reference 1 of 1")

    def test_S8_thread_200_rounds(self):
        base = {"base-secret-%02d-x" % i for i in range(30)}
        text = " ".join(sorted(base)) * 3
        raises = ok = 0
        for rd in range(200):
            known, stop = set(base), threading.Event()

            def adder():
                k = 0
                while not stop.is_set():
                    known.add("added-%d-%d-form" % (rd, k))
                    k += 1
            th = threading.Thread(target=adder, daemon=True)
            th.start()
            try:
                out = cw.scrub(text, known, 10 ** 6)
                ok += all(b not in out for b in base)
            except RuntimeError:
                raises += 1
            finally:
                stop.set()
                th.join()
        print("S8 thread rounds 200, raises %d, every start form redacted %d" % (raises, ok))
        self.assertEqual((raises, ok), (0, 200))

    def test_S8_subclass_case_and_mutant(self):
        base = ["hooksecret-%d-xx" % i for i in range(5)]
        text = " ".join(base)
        with self.assertRaisesRegex(RuntimeError, "Set changed size during iteration"):
            tip_scrub(text, Hooked(base))
        Hooked.calls = 0
        out = cw.scrub(text, Hooked(base), 5000)
        self.assertTrue(all(b not in out for b in base))
        self.assertEqual(Hooked.calls, 0)
        print("S8 tip raises 1 of 1; new raises 0, redacted 1 of 1, hook calls %d" % Hooked.calls)
        m = mutant("snap = frozenset(secrets_known)", "snap = frozenset(secrets_known)\n    for _ in secrets_known:\n        pass")

        def run(fn):
            try:
                fn(text, Hooked(base), 5000)
            except RuntimeError:
                raise AssertionError("the set was iterated while it changed")
        run(cw.scrub)
        with self.assertRaisesRegex(AssertionError, "the set was iterated while it changed"):
            run(m)
        print("S8 live-iteration mutant fails 1 of 1, control passes first")


class S11NoValues(unittest.TestCase):
    def test_S11_outputs_hold_no_fixture_value(self):
        secs = ["fixture-secret-value-1", "fixture-secret-value-2"]
        text = " ".join(secs)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            same(cw.scrub, text, secs, "pass")
            try:
                same(mutant("for n in sorted(hit):", "for n in sorted(hit)[1:]:"), text, secs, "forced failure")
            except AssertionError as e:
                print(str(e))
                self.assertIn("the new scrub differs from the old one", str(e))
        found = sum(buf.getvalue().count(s) for s in secs)
        print("S11 fixture values in 2 of 2 outputs: %d" % found)
        self.assertEqual(found, 0)


if __name__ == "__main__":
    unittest.main()
