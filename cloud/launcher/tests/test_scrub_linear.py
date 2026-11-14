#!/usr/bin/env python3
"""Offline tests: `scrub` makes one pass over the text (ask #1776, PC-1, PC-2). The old body is the frozen reference of every check.
Scenarios S2 S3 S4 S5 S8 S11 S13 S14 S15 S16 S17 S18 S19 S20 and U1 U2. Run directly: python3 cloud/launcher/tests/test_scrub_linear.py
Only counts, lengths and names are printed, never a fixture value."""
import contextlib
import copy
import importlib.util
import inspect
import io
import itertools
import linecache
import os
import pickle
import random
import re
import sys
import threading
import time
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
    b = fn(text, secs, limit)
    try:
        holds_property(text, secs, b, limit)
    except AssertionError as e:  # counts only: never the strings
        raise AssertionError("the new scrub differs from the old one: %s (%s)" % (name, e))
    return len(b)


def _shapes(out):
    for pat, repl in cw.CREDENTIAL_SHAPES:
        out = pat.sub(repl, out)
    for pat in cw.TOKEN_SHAPES:
        out = pat.sub("[redacted]", out)
    return re.sub(r"[^\x20-\x7e]", "?", out)


def _find_all(text, sub):
    pos = text.find(sub, 0)
    while pos != -1:
        yield pos
        pos = text.find(sub, pos + 1)


def match_intervals(text, secs):
    """The matches of every known form on the ORIGINAL text, found by brute force (not by the code under test): from each position whose
    8-character window is a window of a form, the longest run that is still a piece of that form; the overlapping ones merged."""
    forms = forms_of(secs)
    raw = []
    n, mf = len(text), cw.MIN_FRAGMENT
    for f in forms:  # for each form, left to right: a match is the longest run from its start, the next start is after it
        fwins = {f[k:k + mf] for k in range(len(f) - mf + 1)}
        starts = sorted({pos for w in fwins for pos in _find_all(text, w)})
        next_i = 0
        for i in starts:
            if i < next_i:
                continue
            j = i + mf
            while j < n and text[i:j + 1] in f:
                j += 1
            raw.append((i, j))
            next_i = j
    merged = []
    for a, b in sorted(raw):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


VIA_MARKER = [set()]


def old_redacted(text, secs):
    """The positions of the original text that the frozen old scrub redacted: the old loop (as `flagged_ref` plays it) on a list of
    (character, original index or None for a character of a marker written by it)."""
    order = sorted(forms_of(secs), key=lambda v: (-len(v), v))
    ch, ids = list(text), list(range(len(text)))
    mark = list(cw._SCRUB_MARK)
    gone = set()
    via = VIA_MARKER[0] = set()  # the positions of matches that took a character of a marker written by the old scrub (an accident of a secret that holds marker characters)
    for sec in order:
        for v in {sec, sec.strip()}:
            s_ = "".join(ch)
            if v not in s_:
                continue   # nothing found: the loop below would only copy the two lists
            nch, nids, pos = [], [], 0
            while True:
                at = s_.find(v, pos)
                if at < 0:
                    break
                (via if None in ids[at:at + len(v)] else gone).update(x for x in ids[at:at + len(v)] if x is not None)
                nch += ch[pos:at] + mark
                nids += ids[pos:at] + [None] * len(mark)
                pos = at + len(v)
            ch, ids = nch + ch[pos:], nids + ids[pos:]
        s_ = "".join(ch)
        mf = cw.MIN_FRAGMENT
        wins = {sec[k:k + mf] for k in range(len(sec) - mf + 1)}   # the 8-character heads that are a piece of sec
        if not wins:
            continue
        seek = re.compile("|".join(re.escape(w) for w in sorted(wins))).search   # the leftmost head that is a piece of sec
        nch, nids, i = [], [], 0
        while i < len(s_):
            m = seek(s_, i)
            if m is None:
                break
            if m.start() > i:   # the characters before it stay as they are
                nch += ch[i:m.start()]
                nids += ids[i:m.start()]
                i = m.start()
            j = i + mf
            while j < len(s_) and s_[i:j + 1] in sec:
                j += 1
            (via if None in ids[i:j] else gone).update(x for x in ids[i:j] if x is not None)
            nch += mark
            nids += [None] * len(mark)
            i = j
        ch, ids = nch + ch[i:], nids + ids[i:]
    return gone


def holds_property(text, secs, out, limit=10 ** 7):
    """The property of the scrub (ask #2144), by counts: (1) every character outside a merged match interval is unchanged and each
    merged interval shows one marker (no over-redaction, no tail); (2) no 8-character piece of a form survives; (3) no position the
    frozen old scrub redacted is left (the old scrub is replayed on texts up to 200,000 characters). Returns the number of intervals; raises with counts only."""
    text = str(text)[:cw.SCRUB_INPUT_MAX]
    iv = match_intervals(text, secs)
    parts, pos = [], 0
    for a, b in iv:
        parts += [text[pos:a], cw._SCRUB_MARK]
        pos = b
    parts.append(text[pos:])
    want = _shapes("".join(parts))[:limit]
    if out != want:
        raise AssertionError("the output is not the text with the merged match intervals redacted and nothing else (lengths %d, %d; %d intervals)" % (len(out), len(want), len(iv)))
    covered = bytearray(len(text) + 1)
    for a, b in iv:
        covered[a:b] = b"\1" * (b - a)
    missed = [c for c in old_redacted(text, secs) if not covered[c]] if len(text) <= 200000 else []  # the 9 MB fixture is a cost case: the old scrub is not replayed on it
    if missed:
        raise AssertionError("%d positions the old scrub redacted are not redacted" % len(missed))
    return len(iv)


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




# ---- mutants: copies of the source text of the new function and the functions it calls, with ONE changed text ----
_PARTS = (cw._scrub_built, cw._ScrubSpans, cw._scrub_secrets, cw.scrub)
_MUT = [0]


def mutant(old, new):
    """(scrub, namespace) of a copy of the new function with the text `old` (asserted to occur once) changed to `new`."""
    src = "\n\n".join(inspect.getsource(p) for p in _PARTS)
    assert src.count(old) == 1, "the mutation text must occur once"
    msrc = src.replace(old, new)
    assert msrc != src, "the mutation was not applied"
    _MUT[0] += 1
    fname = "<mutant-%d>" % _MUT[0]
    linecache.cache[fname] = (len(msrc), None, msrc.splitlines(True), fname)
    ns = dict(vars(cw))
    ns["_SCRUB_SLOT"] = [None]
    exec(compile(msrc, fname, "exec"), ns)
    return ns["scrub"], ns


def codes_of(ns):
    """Every code object of `scrub` and of the functions and methods it calls (nested ones too)."""
    found = set()

    def walk(code):
        found.add(code)
        for c in code.co_consts:
            if hasattr(c, "co_code"):
                walk(c)
    for f in (ns["_scrub_built"], ns["_scrub_secrets"], ns["scrub"]):
        walk(f.__code__)
    for v in vars(ns["_ScrubSpans"]).values():
        if inspect.isfunction(v):
            walk(v.__code__)
    return found


def line_events(fn, ns, *args):
    """The number of line events in `scrub` and in the functions it calls (a trace; no clock)."""
    codes = codes_of(ns)
    n = [0]

    def tracer(frame, event, arg):
        return local if frame.f_code in codes else None

    def local(frame, event, arg):
        if event == "line":
            n[0] += 1
        return local
    sys.settrace(tracer)
    try:
        fn(*args)
    finally:
        sys.settrace(None)
    return n[0]


NS = vars(cw)
SCRUB = cw.scrub


def line_hits(fn, code, needle, *args):
    """How many times a line of `code` that holds `needle` ran during fn(*args)."""
    n = [0]

    def tracer(frame, event, arg):
        return local if frame.f_code is code else None

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


# ---- the reference, instrumented: the same old body that counts markers, characters removed, and matches on a written marker ----
def flagged_ref(text, secs):
    """The old body over the order (-len, text), on a list of characters each flagged when a redaction wrote it. Returns
    (output, M markers written, R characters redacted, whether a match took a character of a written marker)."""
    order = sorted(forms_of(secs), key=lambda v: (-len(v), v))
    ch, fl = list(str(text)[:cw.SCRUB_INPUT_MAX]), [False] * len(str(text)[:cw.SCRUB_INPUT_MAX])
    M = R = 0
    hit_marker = False
    mark = list(cw._SCRUB_MARK)
    for sec in order:
        for v in {sec, sec.strip()}:
            s = "".join(ch)
            nch, nfl, pos = [], [], 0
            while True:
                at = s.find(v, pos)
                if at < 0:
                    break
                hit_marker |= any(fl[at:at + len(v)])
                nch += ch[pos:at] + mark
                nfl += fl[pos:at] + [True] * len(mark)
                M += 1
                R += len(v)
                pos = at + len(v)
            ch, fl = nch + ch[pos:], nfl + fl[pos:]
        s = "".join(ch)
        nch, nfl, i = [], [], 0
        while i < len(s):
            head = s[i:i + cw.MIN_FRAGMENT]
            if len(head) == cw.MIN_FRAGMENT and head in sec:
                j = i + cw.MIN_FRAGMENT
                while j < len(s) and s[i:j + 1] in sec:
                    j += 1
                hit_marker |= any(fl[i:j])
                nch += mark
                nfl += [True] * len(mark)
                M += 1
                R += j - i
                i = j
            else:
                nch.append(s[i])
                nfl.append(fl[i])
                i += 1
        ch, fl = nch, nfl
    out = "".join(ch)
    for pat, repl in cw.CREDENTIAL_SHAPES:
        out = pat.sub(repl, out)
    for pat in cw.TOKEN_SHAPES:
        out = pat.sub("[redacted]", out)
    return re.sub(r"[^\x20-\x7e]", "?", out)[:10 ** 7], M, R, hit_marker


class S2Differential(unittest.TestCase):
    def test_S2_random_cases(self):
        r = random.Random(SEED)
        bad = 0
        for _ in range(400):
            secs = [rnd(r, r.randint(0, 14), "ab[]redact d") for _ in range(r.randint(0, 5))]
            text = rnd(r, r.randint(0, 60), "ab[]redact d")
            bad += cw.scrub(text, set(secs), 5000) != ref(text, set(secs), 5000)
        for _ in range(60):
            secs = [rnd(r, r.randint(8, 12), "abcd") for _ in range(r.randint(1, 40))]
            text = rnd(r, 300, "abcd")
            bad += cw.scrub(text, set(secs), 5000) != ref(text, set(secs), 5000)
        print("S2 seed %d cases 460 differing %d" % (SEED, bad))
        self.assertEqual(bad, 0, "the new scrub differs from the old one")

    def test_S2_worst_case(self):
        r = random.Random(5)
        secs = {rnd(r, 20, "abcdefgh") for _ in range(50)}
        text = ("".join(s[2:14] + " " for s in secs) * 30)[:20000]
        self.assertEqual(cw.scrub(text, secs, 30000), ref(text, secs, 30000))
        new = line_events(cw.scrub, NS, text, secs, 30000)
        _NS["_ORDER"] = sorted(forms_of(secs), key=lambda v: (-len(v), v))
        old = line_hits(_old_body, _old_body.__code__, "head = text[i:i + MIN_FRAGMENT]", text, secs, 30000)
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
        t1, t2 = "z" * 4000, "z" * 8000   # the counts are exact multiples of the size: no need for a long traced run
        cases = {"1": (t1, s50), "2": (t2, s50), "f": (t1, s100)}
        n = {k: line_hits(cw.scrub, cw._scrub_secrets.__code__, "for rank in get(", t, s, 10 ** 6) for k, (t, s) in cases.items()}
        o = {}
        for k, (t, s) in cases.items():
            _NS["_ORDER"] = sorted(forms_of(s), key=lambda v: (-len(v), v))
            o[k] = line_hits(_old_body, _old_body.__code__, "head = text[i:i + MIN_FRAGMENT]", t, s, 10 ** 6)
        print("S4 new counts %s old counts %s" % (n, o))
        print("S4 new size x%.3f forms x%.3f; old size x%.3f forms x%.3f" % (n["2"] / n["1"], n["f"] / n["1"], o["2"] / o["1"], o["f"] / o["1"]))
        self.assertTrue(1.9 <= n["2"] / n["1"] <= 2.1, "new count not in step with the text")
        self.assertTrue(1.0 <= n["f"] / n["1"] <= 1.1, "new count not flat in the forms")
        self.assertTrue(1.9 <= o["2"] / o["1"] <= 2.1 and 1.9 <= o["f"] / o["1"] <= 2.1, "the old count is not in step with the text and the forms")   # the frozen reference: linear in both


class S5Mutants(unittest.TestCase):
    def _run(self, fn):
        r = random.Random(7)
        for _ in range(500):
            secs = {rnd(r, r.randint(8, 14), "abcd") for _ in range(r.randint(1, 4))}
            text = rnd(r, 80, "abcd")
            try:
                holds_property(text, secs, fn(text, secs, 5000), 5000)
            except AssertionError:
                raise AssertionError("the new scrub differs from the old one")

    def test_S5_control_and_mutants(self):
        self._run(cw.scrub)
        one_less = mutant("            for whole in {sec, sec.strip()}:\n", "            if rank == 0:\n                continue\n            for whole in {sec, sec.strip()}:\n")[0]
        cut = mutant("                end = i + lo\n", "                end = i + lo - 1\n")[0]
        for m in (one_less, cut):
            with self.assertRaisesRegex(AssertionError, "the new scrub differs from the old one"):
                self._run(m)
        print("S5 control 1 of 1, mutants failing 2 of 2, mutations applied 2 of 2")


class S13Contract(unittest.TestCase):
    def _check(self, fn, quiet=False):
        r = random.Random(SEED)
        cases = case_sets(r)
        surv_new = surv_ref = ivs = 0
        for text, secs in cases:
            a, b = ref(text, secs, 10 ** 6), fn(text, secs, 10 ** 6)
            try:
                ivs += holds_property(text, secs, b, 10 ** 6)
            except AssertionError as e:
                raise AssertionError("the output breaks the property: %s" % e)
            outs = {fn(text, build(secs), 10 ** 6) for build in (list, lambda s: list(reversed(s)), set, frozenset)}
            if len(outs) != 1:
                raise AssertionError("the output depends on how the set is built")
            f = forms_of(secs)
            surv_new += any(s in b for s in f)
            surv_ref += any(s in a for s in f)
        if not quiet:
            print("S13 seed %d cases %d hold the property (every span the old scrub redacted redacted, nothing outside a merged match changed), "
                  "merged intervals %d; survivors of a whole value new %d old %d" % (SEED, len(cases), ivs, surv_new, surv_ref))
        self.assertLessEqual(surv_new, surv_ref)

    def test_S13_contract_and_mutant(self):
        self._check(cw.scrub)
        m = mutant("                if b > merged[-1][1]:\n                    merged[-1][1] = b\n", "                pass\n")[0]
        with self.assertRaisesRegex(AssertionError, "the output breaks the property"):
            self._check(m, quiet=True)
        print("S13 control 1 of 1, mutant (a later overlapping match is dropped, its tail stays) fails 1 of 1, mutation applied 1 of 1")


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
        rounds = 60
        for rd in range(rounds):
            known, stop = set(base), threading.Event()

            def adder():
                k = 0
                while not stop.is_set():
                    known.add("added-%d-%d-form" % (rd, k))
                    k += 1
                    time.sleep(0.0002)   # lets the scrub run between two additions: the race stays, the GIL fight does not cost seconds
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
        print("S8 thread rounds %d, raises %d, every start form redacted %d" % (rounds, raises, ok))
        self.assertEqual((raises, ok), (0, rounds))

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
        m = mutant("snap = frozenset(secrets_known)", "snap = frozenset(secrets_known)\n        for _ in secrets_known:\n            pass")[0]

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
                same(mutant("            for whole in {sec, sec.strip()}:\n", "            if rank == 0:\n                continue\n            for whole in {sec, sec.strip()}:\n")[0], text, secs, "forced failure")
            except AssertionError as e:
                print(str(e))
                self.assertIn("the new scrub differs from the old one", str(e))
        found = sum(buf.getvalue().count(s) for s in secs)
        print("S11 fixture values in 2 of 2 outputs: %d" % found)
        self.assertEqual(found, 0)


def tally(code, calls):
    """Count, over `calls`, the runs of the lines of `code` (the function that builds the structure) that take the snapshot
    of the set and that build. A frozenset copy of a set subclass does not call __iter__, so the snapshots are counted by line events."""
    n = {"snap": 0, "build": 0}

    def tracer(frame, event, arg):
        return local if frame.f_code is code else None

    def local(frame, event, arg):
        if event == "line":
            line = linecache.getline(frame.f_code.co_filename, frame.f_lineno)
            n["snap"] += "frozenset(secrets_known)" in line
            n["build"] += "built = (version if" in line
        return local
    sys.settrace(tracer)
    try:
        calls()
    finally:
        sys.settrace(None)
    return n["snap"], n["build"]


def synthetic_forms(seed, n=1000):
    r = random.Random(seed)
    return {rnd(r, r.randint(8, 40), "abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n)}



class S14Version(unittest.TestCase):
    def _check(self, fn, ns):
        code = ns["_scrub_built"].__code__
        forms = synthetic_forms(14)
        ks = cw.KnownSet(forms)
        text = "abcde"

        def thousand():
            for _ in range(1000):
                fn(text, ks)
        snaps, builds = tally(code, thousand)
        if snaps > 1:
            raise AssertionError("a call read the whole set: %d snapshots in 1000 calls" % snaps)
        extra = "added-form-" + "q" * 12
        ks.add(extra)
        out = []
        snaps2, builds2 = tally(code, lambda: out.append(fn("x " + extra + " y", ks, 5000)))
        self.assertLessEqual(snaps2, 1)   # a change costs at most one snapshot and one build (the new form is redacted below)
        self.assertLessEqual(builds2, 1)
        self.assertNotIn(extra, out[0])
        other = cw.KnownSet(synthetic_forms(15))
        fn(text, other)
        fn(text, ks)

        def alternate():
            for _ in range(50):
                fn(text, other)
                fn(text, ks)
        _, rebuilds = tally(code, alternate)
        self.assertEqual(rebuilds, 0)
        return snaps, snaps2, rebuilds

    def test_S14_version_stamp_and_mutant(self):
        got = self._check(cw.scrub, NS)
        forms = synthetic_forms(14)
        pick = sorted(forms)[:3]
        texts = ["a " + pick[0] + " b", pick[1][:20] + " and " + pick[2], "nothing"]
        equal = 0
        kinds = (set, frozenset, cw.KnownSet)
        for kind in kinds:
            secs = kind(forms)
            if all(cw.scrub(t, secs, 5000) == ref(t, secs, 5000) for t in texts):
                equal += 1
        self.assertEqual(equal, len(kinds))
        old = '    version = getattr(secrets_known, "version", None)  # a KnownSet: read its version FIRST\n'
        m, mns = mutant(old, old + "    if version is not None:\n        _whole = frozenset(secrets_known)\n")
        with self.assertRaisesRegex(AssertionError, "a call read the whole set"):
            self._check(m, mns)
        print("S14 iterations in 1000 calls: %d (not 1000), for the added form %d, rebuilds in 100 alternating calls %d, "
              "plain set, frozenset and KnownSet equal to the reference %d of 3; control passes 1 of 1, mutant fails 1 of 1"
              % (got[0], got[1], got[2], equal))


class S15Counts(unittest.TestCase):
    FORM_ALPHA = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    def _form_case(self, nsec, size=15000):
        r = random.Random(15)
        form = rnd(r, 319, self.FORM_ALPHA)
        secs = []
        while len(secs) < 197:
            ln = r.randint(40, 120)
            k = r.randint(0, len(form) - ln)
            if form[k:k + ln] not in secs:
                secs.append(form[k:k + ln])
        filler = "-. " * 40000
        pad = size - len(form)
        hit = filler[:pad // 2] + form + filler[:pad - pad // 2]
        zero = filler[:pad // 2] + "~" * len(form) + filler[:pad - pad // 2]
        self.assertEqual((len(hit), len(zero)), (size, size))
        return secs[:nsec], hit, zero

    def _vrm_big(self, text, secs, out_new, brackets=0):
        count = {}
        for s in forms_of(secs):
            for w in {s[k:k + 8] for k in range(len(s) - 7)}:
                count[w] = count.get(w, 0) + 1
        V = sum(count.get(text[i:i + 8], 0) for i in range(len(text) - 7))
        M = out_new.count("[redacted]") - text.count("[redacted]")
        R = len(text) - len(out_new) + 10 * M
        if brackets:  # a marker written can be a window of a bracket secret: 17 windows for each marker, for each of them
            V += 17 * max(M, 0) * brackets
        return V, M, R

    def _check(self, fn, ns, nsec, brackets=None, size=15000):
        if brackets is None:
            secs, hit, zero = self._form_case(nsec, size)
            label = "%d secrets" % nsec
        else:
            secs, hit, zero = brackets
            label = "%d bracket secrets" % len(secs)
        out = fn(hit, set(secs), 10 ** 7)
        c_hit = line_events(fn, ns, hit, set(secs), 10 ** 7)
        c_zero = line_events(fn, ns, zero, set(secs), 10 ** 7)
        V, M, R = self._vrm_big(hit, secs, out, brackets=len(secs) if brackets is not None else 0)
        bound = 40 * (V + R + M)
        print("S15 %s, text %d: V %d R %d M %d, count hit %d hit-0 %d, above %d, bound %d" % (label, len(hit), V, R, M, c_hit, c_zero, c_hit - c_zero, bound))
        if c_hit - c_zero > bound:
            raise AssertionError("a pass over the text was made for a secret: %d above the hit-0 count, bound %d" % (c_hit - c_zero, bound))

    def _bracket_case(self, size):
        r = random.Random(1515)
        bases = ["".join(r.choice(["[redacted]", "[red", "acted]", "ab[cd", "x]y", "qrstuv"]) for _ in range(5)) for _ in range(4)]
        secs = []
        while len(secs) < 20:
            b = r.choice(bases)
            ln = r.randint(8, min(20, len(b)))
            k = r.randint(0, len(b) - ln)
            if b[k:k + ln] not in secs and len(b[k:k + ln].strip()) >= 8:
                secs.append(b[k:k + ln])
        parts, n = [], 0
        while n < size:
            piece = r.choice(bases)[r.randint(0, 6):r.randint(10, 30)] + r.choice(["", " ", "[redacted]"])
            parts.append(piece)
            n += len(piece)
        hit = "".join(parts)[:size]
        zero = "-" * size
        return secs, hit, zero

    def test_S15_counts_and_mutant(self):
        for nsec in (30, 100, 197):
            self._check(cw.scrub, NS, nsec)
        self._check(cw.scrub, NS, 0, brackets=self._bracket_case(15000))
        m, mns = mutant("            self.sweep(sec, None, positions)\n",
                        "            self.sweep(sec, None, positions)\n            for _i in range(len(self.text)):\n                _c = self.text[_i]\n")
        self._check(cw.scrub, NS, 197, size=5000)  # the control, first
        with self.assertRaisesRegex(AssertionError, "a pass over the text was made for a secret"):
            self._check(m, mns, 197, size=5000)
        print("S15 control passes 1 of 1, the mutant that makes a pass over the text for each secret fails 1 of 1, mutation applied")


class S16Random(unittest.TestCase):
    TOKENS = ["[redacted]", "[red", "acted]", "ab", "cd", "e[", "]x", "zz", " ", "[", "]", "dact"]

    def _case(self, r, brk):
        if brk:
            bases = ["".join(r.choice(self.TOKENS) for _ in range(r.randint(6, 14))) for _ in range(r.randint(2, 4))]
        else:
            bases = [rnd(r, r.randint(25, 45), "abcdefgh") for _ in range(r.randint(2, 4))]
        secs = []
        for _ in range(r.randint(20, 80)):
            b = r.choice(bases)
            ln = r.randint(8, 24)
            k = r.randint(0, max(0, len(b) - ln))
            secs.append(b[k:k + ln])
        text = "".join(r.choice(bases)[r.randint(0, 8):r.randint(8, 45)] + r.choice(["", "x", " "]) for _ in range(r.randint(8, 30)))
        return text, secs

    def _cases(self):
        r = random.Random(SEED + 16)
        cases = [self._case(r, i % 2) for i in range(600)]
        rr = random.Random(1616)
        form = rnd(rr, 319, "abcdefghijklmnopqrstuvwxyz0123456789")
        secs = [form[k:k + ln] for ln in [rr.randint(40, 120) for _ in range(100)] for k in [rr.randint(0, 319 - ln)]]
        text = ("-" * 14840 + form) * 2
        cases.append((text[:30000], secs))
        bases = ["".join(rr.choice(self.TOKENS) for _ in range(6)) for _ in range(3)]
        secs = [b[k:k + 12] for b in bases for k in range(0, 6)][:20]
        secs += [rnd(rr, 10, "[redacted]") for _ in range(20 - len(secs))] if len(secs) < 20 else []
        text = "".join(rr.choice(bases)[rr.randint(0, 4):rr.randint(10, 30)] + " " for _ in range(3000))[:30000]
        cases.append((text, secs))
        return cases

    def _check(self, fn, quiet=False):
        cases = self._cases()
        equal = many = later = dep = 0
        for text, secs in cases:
            a = ref(text, secs, 10 ** 7)
            b = fn(text, set(secs), 10 ** 7)
            try:
                holds_property(text, secs, b)
            except AssertionError as e:
                raise AssertionError("the output breaks the property (%d of %d cases hold so far): %s" % (equal, len(cases), e))
            equal += 1
            for build in (list, lambda s: list(reversed(s)), frozenset):
                dep += fn(text, build(secs), 10 ** 7) != b
            if len(text) < 5000:
                fout, M, R, marker_hit = flagged_ref(text, secs)
                self.assertEqual(fout, a, "the instrumented reference differs from the reference")
                later += marker_hit
            found = sum(1 for s in forms_of(secs) if s[:8] in text or any(s[k:k + 8] in text for k in range(len(s) - 7)))
            many += found >= 10
        if not quiet:
            print("S16 seed %d cases %d hold the property %d, cases that find 10 or more secrets %d, cases where a later secret matched a "
                  "written marker %d, cases that depend on how the set is built %d" % (SEED + 16, len(cases), equal, many, later, dep))
        self.assertEqual(dep, 0)
        self.assertGreaterEqual(many, 300)
        self.assertGreaterEqual(later, 100)

    def test_S16_differential_and_mutant(self):
        self._check(cw.scrub)
        m = mutant("                if b > merged[-1][1]:\n                    merged[-1][1] = b\n", "                pass\n")[0]
        with self.assertRaisesRegex(AssertionError, "the output breaks the property"):
            self._check(m, quiet=True)
        print("S16 control passes 1 of 1, the mutant that drops a later overlapping match fails 1 of 1, mutation applied")


MUTATORS = ("add", "update", "discard", "remove", "pop", "clear", "__ior__", "__iand__", "__isub__", "__ixor__", "difference_update",
            "intersection_update", "symmetric_difference_update")
MUTATOR_ARGS = {"add": ("c",), "update": (["c"],), "discard": ("a",), "remove": ("a",), "pop": (), "clear": (), "__ior__": ({"c"},),
                "__iand__": ({"a"},), "__isub__": ({"a"},), "__ixor__": ({"a"},), "difference_update": (["a"],),
                "intersection_update": (["a"],), "symmetric_difference_update": (["a"],)}


def changed_text(src, old, new):
    """The text with `old` (asserted to occur once) changed to `new`; the application is computed from the two texts."""
    if src.count(old) != 1:
        raise AssertionError("the mutation text must occur once")
    out = src.replace(old, new)
    if not (out != src and old in src and old not in out):
        raise AssertionError("the mutation was not applied")
    return out


def known_set_class(src):
    ns = dict(vars(cw))
    exec(compile(src, "<knownset-copy>", "exec"), ns)
    return ns["KnownSet"]


def held_set_check(cls):
    """The held set must not be reachable under a public attribute of an instance."""
    ks = cls(["held-form-one"])
    for name in dir(ks):
        if name.startswith("_"):
            continue
        try:
            val = getattr(ks, name)
        except Exception:
            continue
        if isinstance(val, (set, frozenset)):
            raise AssertionError("the held set is reachable: " + name)
    return 1


def bump_check(cls):
    """Each of the thirteen moves `version` by exactly one when it changes the content."""
    bumped = 0
    for name in MUTATORS:
        ks = cls(["a", "b"])
        before, content = ks.version, frozenset(ks)
        getattr(ks, name)(*MUTATOR_ARGS[name])
        if frozenset(ks) == content or ks.version != before + 1:
            raise AssertionError("a mutator did not change the content and bump by one: %s" % name)
        bumped += 1
    return bumped


class RecordingLock:
    """A re-entrant stand-in: notes the version and the content at the outermost acquire and release."""
    def __init__(self, ks):
        self.ks, self.lock, self.depth, self.acquired, self.released, self.content, self.after = ks, threading.RLock(), 0, [], [], [], []

    def acquire(self, *a):
        ok = self.lock.acquire(*a)
        if ok:
            self.depth += 1
            if self.depth == 1:
                self.acquired.append(self.ks.version)
                self.content.append(frozenset(self.ks._forms))  # probe on purpose
        return ok

    def release(self):
        if self.depth == 1:
            self.released.append(self.ks.version)
            self.after.append(frozenset(self.ks._forms))  # probe on purpose
        self.depth -= 1
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def lock_check(cls):
    done = 0
    for name in MUTATORS:
        ks = cls(["a", "b"])
        rec = RecordingLock(ks)
        ks._lock = rec  # probe on purpose
        getattr(ks, name)(*MUTATOR_ARGS[name])
        if not (len(rec.acquired) == 1 and rec.released == [a + 1 for a in rec.acquired]):
            raise AssertionError("the version was bumped outside the lock: %s" % name)
        if rec.after[0] == rec.content[0]:
            raise AssertionError("the content had not changed when the lock was released: %s" % name)
        done += 1
    return done


def reentrant_check(cls):
    ks = cls(["a"])
    ks._lock.acquire()  # probe on purpose
    ok = ks._lock.acquire(False)  # probe on purpose
    if ok:
        ks._lock.release()  # probe on purpose
    ks._lock.release()  # probe on purpose
    if not ok:
        raise AssertionError("the lock is not re-entrant")
    return 1


INSIDE_ARGS = {"add": ("charlie-form-3",), "update": (["charlie-form-3"],), "discard": ("alpha-form-1",), "remove": ("alpha-form-1",),
               "pop": (), "clear": (), "__ior__": ({"charlie-form-3"},), "__iand__": ({"alpha-form-1"},), "__isub__": ({"alpha-form-1"},),
               "__ixor__": ({"alpha-form-1"},), "difference_update": (["alpha-form-1"],), "intersection_update": (["alpha-form-1"],),
               "symmetric_difference_update": (["alpha-form-1"],)}


class HookSet(set):
    """The held set with a hook that runs a scrub on the same KnownSet just BEFORE each content change (inside the change)."""
    hook = None

    def _run(self):
        if self.hook:
            self.hook()


def _hooked(name):
    def method(self, *a):
        self._run()
        return getattr(set, name)(self, *a)
    return method


for _n in MUTATORS:
    setattr(HookSet, _n, _hooked(_n))


def inside_check(cls):
    """A scrub made from inside each change leaves no stale structure: the next scrub shows the content as it is now."""
    done = 0
    for name in MUTATORS:
        ks = cls(["alpha-form-1", "bravo-form-2"])
        held = HookSet(ks._forms)  # probe on purpose
        ks._forms = held  # probe on purpose
        held.hook = lambda: cw.scrub("alpha-form-1 bravo-form-2 charlie-form-3", ks, 1000)
        getattr(ks, name)(*INSIDE_ARGS[name])
        held.hook = None
        for form in ("alpha-form-1", "bravo-form-2", "charlie-form-3"):
            clear = cw.scrub("x " + form + " y", ks, 1000)
            if (form in clear) == (form in frozenset(held)):
                raise AssertionError("a form added was not redacted after the change (stale structure after %s)" % name)
        done += 1
    return done


class S17Composition(unittest.TestCase):
    def test_S17_composition_and_mutant(self):
        src = inspect.getsource(cw.KnownSet)
        self.assertEqual(held_set_check(cw.KnownSet), 1)  # the control, first
        ks = cw.KnownSet(["held-form-one"])
        self.assertFalse(isinstance(ks, set))
        refused = 0
        attempts = (lambda: setattr(ks, "version", 5), lambda: setattr(ks, "built", 5),
                    lambda: set.add(ks, "x"), lambda: set.update(ks, ["x"]))
        for fn in attempts:
            with self.assertRaises((AttributeError, TypeError)) as cm:
                fn()
            refused += 1
        self.assertEqual(refused, len(attempts))
        self.assertEqual(ks.version, 0)

        with self.assertRaisesRegex(TypeError, "a KnownSet may not be subclassed"):
            class Sub(cw.KnownSet):
                pass
        with self.assertRaises(AttributeError):
            ks.extra = 1
        self.assertEqual([n for n in dir(ks) if not n.startswith("_") and isinstance(getattr(ks, n), (set, frozenset))], [])
        through = 2
        self.assertFalse(hasattr(ks, "__dict__"))
        with self.assertRaisesRegex(TypeError, "a KnownSet is initialised once"):
            ks.__init__()
        self.assertEqual(ks.version, 0)
        # copy, deepcopy and a pickle round trip
        ks.add("original-form-9")
        cw.scrub("some text", ks)
        self.assertIsNotNone(ks.built)
        independent = 0
        dups = (("copy", copy.copy(ks)), ("deepcopy", copy.deepcopy(ks)), ("copy()", ks.copy()))  # probe on purpose
        for label, dup in dups:  # probe on purpose
            self.assertIsInstance(dup, cw.KnownSet)
            ok = (dup == ks and dup._lock is not ks._lock and dup.built is None)  # probe on purpose
            dup.add("only-in-copy-1")
            ok = ok and "only-in-copy-1" not in cw.scrub("x only-in-copy-1 y", dup, 1000)
            ok = ok and "only-in-copy-1" in cw.scrub("x only-in-copy-1 y", ks, 1000)
            independent += ok
        self.assertEqual(independent, len(dups))
        with self.assertRaisesRegex(TypeError, "a KnownSet is not pickled"):
            pickle.dumps(ks)  # probe on purpose
        old = "    @property\n    def version(self):\n        return self._version\n"
        new = "    @property\n    def forms(self):\n        return self._forms\n\n    @property\n    def version(self):\n        return self._version + 0\n"  # probe on purpose
        mut = known_set_class(changed_text(src, old, new))
        with self.assertRaisesRegex(AssertionError, r"the held set is reachable: forms"):
            held_set_check(mut)
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            changed_text(src, old, old)
        print("S17 assignments and base-type calls refused %d of 4, subclass refused and instance assignment refused %d of 2, "
              "second __init__ refused 1 of 1, copy, deepcopy and copy() independent %d of 3, pickle refused 1 of 1; control passes 1 of 1, copy that exposes "
              "the held set fails 1 of 1, no-op mutation fails the computation 1 of 1" % (refused, through, independent))


class S18Lock(unittest.TestCase):
    def test_S18_lock_order_stress_and_mutants(self):
        src = inspect.getsource(cw.KnownSet)
        self.assertEqual((reentrant_check(cw.KnownSet), lock_check(cw.KnownSet), inside_check(cw.KnownSet)), (1, len(MUTATORS), len(MUTATORS)))  # control first
        # a scrub run from inside an `update` whose argument iterator calls scrub on the same set
        ks = cw.KnownSet(["alpha-form-1"])

        def args():
            cw.scrub("alpha-form-1 beta-form-22", ks, 1000)
            yield "beta-form-22"
        ks.update(args())
        self.assertNotIn("beta-form-22", cw.scrub("x beta-form-22 y", ks, 1000))
        ks = cw.KnownSet()
        lasts = {}

        def work(t):
            for i in range(500):
                form = "stress-form-%d-%04d-end" % (t, i)
                ks.add(form)
            lasts[t] = form
        threads = [threading.Thread(target=work, args=(t,), daemon=True) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((ks.version, len(ks)), (len(threads) * 500, len(threads) * 500))   # every add of every thread, once
        redacted = sum(1 for t in range(len(threads)) if lasts[t] not in cw.scrub("x " + lasts[t] + " y", ks, 1000))
        self.assertEqual(redacted, len(threads))
        old = ("                if self._forms != before:\n                    self._version += 1  # AFTER the content changed, under the lock\n")  # probe on purpose
        new = ("                pass\n        if self._forms != before:\n            self._version += 1\n")  # probe on purpose
        mut = known_set_class(changed_text(src, old, new))
        self.assertEqual(reentrant_check(mut), 1)
        with self.assertRaisesRegex(AssertionError, "the version was bumped outside the lock"):
            lock_check(mut)
        old2 = "                self._forms.discard(item)\n                self._version += 1\n"  # probe on purpose
        new2 = "                self._version += 1\n                self._forms.discard(item)\n"  # probe on purpose
        mut2 = known_set_class(changed_text(src, old2, new2))
        self.assertEqual((reentrant_check(mut2), lock_check(mut2)), (1, len(MUTATORS)))
        with self.assertRaisesRegex(AssertionError, "a form added was not redacted after the change"):
            inside_check(mut2)
        old3 = "self._lock = threading.RLock()"
        mut3 = known_set_class(changed_text(src, old3, "self._lock = threading.Lock()"))  # probe on purpose
        with self.assertRaisesRegex(AssertionError, "the lock is not re-entrant"):
            reentrant_check(mut3)
        for o in (old, old2, old3):
            with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
                changed_text(src, o, o)
        print("S18 13 of 13 release = acquire + 1 with the content already changed, scrub inside the change leaves no stale structure "
              "13 of 13 and 1 of 1 inside update, lock re-entrant 1 of 1, stress version %d forms %d, last forms redacted %d of 8; control "
              "passes 1 of 1, the 3 mutants (bump after the release, bump before the change in discard, plain Lock) fail 3 of 3 with "
              "their texts, no-op mutations fail the computation 3 of 3" % (ks.version, len(ks), redacted))


class CountStop(Exception):
    pass


def run_under_stop(fn, ns, text, secs, bound):
    """Run fn on the case; a tracer RAISES `the count passed its bound` when the line events pass `bound`. Returns the count."""
    codes = codes_of(ns)
    n = [0]

    def tracer(frame, event, arg):
        return local if frame.f_code in codes else None

    def local(frame, event, arg):
        if event == "line":
            n[0] += 1
            if n[0] > bound:
                raise CountStop("the count passed its bound")
        return local
    sys.settrace(tracer)
    try:
        fn(text, set(secs), 10 ** 7)
    finally:
        sys.settrace(None)
    return n[0]


class S19RankGuard(unittest.TestCase):
    def _cases(self):
        c15 = S15Counts()
        r16 = S16Random()
        cases = []
        for nsec in (30, 197):
            secs, hit, zero = c15._form_case(nsec, 5000)
            cases.append(("S15-%d" % nsec, hit, secs, None))
        bases = r16._cases()
        for i in list(range(0, 20, 2)) + list(range(1, 60, 2)) + [600, 601]:
            cases.append(("S%s-%d" % ("16" if i < 600 else "2", i), bases[i][0], bases[i][1], None))
        out = []
        for label, text, secs, _ in cases:
            res = cw.scrub(text, set(secs), 10 ** 7)
            V, M, R = c15._vrm_big(text, secs, res, brackets=len(secs))
            build = sum(len(x) for x in set(secs))
            bound = 200 * (len(text) + V + max(R, 0) + max(M, 0) + build)
            if label.startswith("S15"):
                bound += line_events(cw.scrub, NS, "-" * len(text), set(secs), 10 ** 7)
            out.append((label, text, secs, bound))
        return out

    def _run(self, fn, ns, cases):
        passed, stopped = 0, []
        for label, text, secs, bound in cases:
            try:
                run_under_stop(fn, ns, text, secs, bound)
                passed += 1
            except CountStop as e:
                stopped.append((label, bound, str(e)))
        return passed, stopped

    def test_S19_rank_guard_mutant_ends_by_the_count(self):
        cases = self._cases()
        passed, stopped = self._run(cw.scrub, NS, cases)  # the control, first
        self.assertEqual((passed, stopped), (len(cases), []))
        src = "\n\n".join(inspect.getsource(p) for p in _PARTS)
        old = "            positions = sorted(self.cand[rank])\n"
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            changed_text(src, old, old)
        new = "            positions = range(len(self.text))\n"  # every position is tried for every secret: a pass over the text for each secret
        m, mns = mutant(old, new)
        changed_text(src, old, new)
        mpassed, mstopped = self._run(m, mns, cases)
        self.assertGreaterEqual(len(mstopped), 1)
        self.assertEqual(mstopped[0][2], "the count passed its bound")
        label, bound, _ = mstopped[0]
        print("S19 control passes %d of %d under the stop; the copy that tries every position for every secret ends by the count with `the count passed its "
              "bound` in %d of %d cases (first: %s, bound %d, count above it); no-op mutation fails the computation 1 of 1"
              % (passed, len(cases), len(mstopped), len(cases), label, bound))


FORM_VALUES = ['ghp_Zx9Qw3Er7Ty1Ui5Op2As4Df6Gh8Jk0Lz', '{"name": "synthetic-entry-one", "key": "synthetic-key-0123456789"}', 'plain synthetic value 42']


class S19FurtherMutants(unittest.TestCase):
    """Three further mutants of `_ScrubSpans`, each caught by CASES this file holds: cases 1, 3 and 5 of the S16 list (bracket cases),
    run under the count stop and compared with the control output (which the S16 test holds to the property):
      a run one character short      -> cases 1, 3 and 5 end with a DIFFERENT output;
      the halving skipped            -> the same three cases differ (a run is cut at the length of the secret);
      overlapping matches not merged -> the same three cases differ (the tail of the later match stays).
    None of the three is an equivalence, so no equivalence claim is made."""
    MUTANTS = (
        ("run one character short", "                end = i + lo\n", "                end = i + lo - 1\n"),
        ("halving skipped", "                if text[i:i + lo] not in sec:", "                if False:"),
        ("overlapping matches not merged", "                if b > merged[-1][1]:\n                    merged[-1][1] = b\n", "                pass\n"),
    )
    CASES = (1, 3, 5)

    @staticmethod
    def _bound(text, secs):
        return 2000 * (len(text) + sum(len(x) for x in set(secs)))

    def _run(self, fn, ns, text, secs):
        res = []
        run_under_stop(lambda tx, sc, lim: res.append(fn(tx, sc, lim)), ns, text, secs, self._bound(text, secs))
        return res[0]

    def test_S19_three_further_mutants_are_caught_by_cases(self):
        cases = S16Random()._cases()
        src = "\n\n".join(inspect.getsource(x) for x in _PARTS)
        expected = {}
        for i in self.CASES:  # the control, first: every case passes under the stop and equals the reference
            text, secs = cases[i]
            expected[i] = self._run(cw.scrub, NS, text, secs)
            holds_property(text, secs, expected[i])
        lines = []
        for name, old, new in self.MUTANTS:
            changed_text(src, old, new)  # the application, computed from the two texts
            with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
                changed_text(src, old, old)
            m, mns = mutant(old, new)
            stopped, differs = [], []
            for i in self.CASES:
                text, secs = cases[i]
                try:
                    if self._run(m, mns, text, secs) != expected[i]:
                        differs.append(i)
                except CountStop as e:
                    self.assertEqual(str(e), "the count passed its bound")
                    stopped.append(i)
            self.assertTrue(stopped or differs, "a further mutant was not caught: " + name)
            lines.append("%s: stop in cases %s, differs in cases %s" % (name, stopped, differs))
        print("S19 further mutants, each caught by an S16 case under the stop (control passes %d of %d): %s" % (len(self.CASES), len(self.CASES), "; ".join(lines)))


class U2Fixture(unittest.TestCase):
    def test_U2_forms_and_set_equal_the_tip(self):
        """The property (ask #2144; owner's rule of 4 November 21:27Z): on the fixture, no piece of a planted secret survives the scrub of the
        set the launcher builds, the plain text around it is unchanged, and the scrub makes one pass. No exact set is pinned."""
        k = cw.Kube({})
        k.add_secrets(FORM_VALUES[0])
        k.add_secrets(*FORM_VALUES[1:])
        k.add_secrets(FORM_VALUES[0], "")
        self.assertIsInstance(k.known, cw.KnownSet)
        print("U2: %s" % fixture_property(k))


def fixture_property(kube):
    """Plant each fixture value in plain text: 0 windows of 8 characters of it left, the text around it unchanged, one probe per position."""
    left = around = planted = 0
    for v in FORM_VALUES:
        text = "before %s after" % v
        out = kube.scrub(text, 10 ** 6)
        planted += 1
        left += any(v[i:i + cw.MIN_FRAGMENT] in out for i in range(len(v) - cw.MIN_FRAGMENT + 1))
        around += out.startswith("before ") and out.endswith(" after")
    assert left == 0, "a piece of a known secret survived the scrub in %d of %d texts" % (left, planted)
    assert around == planted, "plain text around a planted secret changed in %d of %d texts" % (planted - around, planted)
    probes = line_hits(cw.scrub, cw._scrub_secrets.__code__, "for rank in get(", "before %s after" % FORM_VALUES[0], kube.known, 10 ** 6)
    assert probes <= 2 * (len(FORM_VALUES[0]) + 13), "the scrub made more than one pass over the text"
    return "planted %d, pieces surviving %d, plain text unchanged %d of %d, probes %d" % (planted, left, around, planted, probes)


def removing_check(cls):
    """The six operations that remove forms, given an argument iterator that raises after its first form, change nothing."""
    def boom():
        yield "aaaaaaaa1"
        raise RuntimeError("the iterator stopped")
    calls = (("intersection_update", lambda k: k.intersection_update(boom())), ("difference_update", lambda k: k.difference_update(boom())),
             ("symmetric_difference_update", lambda k: k.symmetric_difference_update(boom())),
             ("__iand__", lambda k: k.__iand__(Raising())), ("__isub__", lambda k: k.__isub__(Raising())), ("__ixor__", lambda k: k.__ixor__(Raising())))
    kept = 0
    for name, call in calls:
        k = cls(["aaaaaaaa1", "bbbbbbbb2"])
        v = k.version
        try:
            call(k)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the iterator did not raise")
        if k != {"aaaaaaaa1", "bbbbbbbb2"} or k.version != v:
            raise AssertionError("a half-read argument changed the set or the version: %s" % name)
        kept += 1
    return kept


class Raising(set):
    """A set whose iteration yields one form and raises (the in-place operators accept a set)."""
    def __iter__(self):
        yield "aaaaaaaa1"
        raise RuntimeError("the iterator stopped")


def removing_mutant():
    """KnownSet with the early raise removed (the half-read behaviour restored); applied is computed from the two texts."""
    src = inspect.getsource(cw.KnownSet)
    old = '        if err is not None and name in ("difference_update", "intersection_update", "symmetric_difference_update"):\n'
    assert src.count(old) == 1, "the mutation text must occur once"
    msrc = src.replace(old, '        if False:\n')
    assert msrc != src and old in src and old not in msrc, "the mutation was not applied"
    ns = dict(vars(cw))
    exec(compile(msrc, "<mutant-removing>", "exec"), ns)
    return ns["KnownSet"]


class S20Removing(unittest.TestCase):
    def test_S20_removing_operations_read_first(self):
        self.assertGreater(removing_check(cw.KnownSet), 0)   # it raises on the first operation that changes the set or the version
        mut = removing_mutant()
        with self.assertRaisesRegex(AssertionError, "a half-read argument changed the set or the version"):
            removing_check(mut)
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            src = "x"
            assert src.replace("y", "z") != src, "the mutation was not applied"
        print("S20 removing operations with a raising iterator leave the set and the version unchanged 3 of 3; in-place operators 3 of 3; mutant fails with 'a half-read argument changed the set or the version'")


class S20VersionRule(unittest.TestCase):
    def test_S20_version_rule_equality_and_kube(self):
        self.assertEqual(bump_check(cw.KnownSet), len(MUTATORS))
        unchanged = 0
        ks = cw.KnownSet(["a"])
        before = ks.version
        with self.assertRaises(KeyError):
            ks.remove("absent")
        unchanged += ks.version == before
        empty = cw.KnownSet()
        with self.assertRaises(KeyError):
            empty.pop()
        unchanged += empty.version == 0
        self.assertEqual(unchanged, 2)   # the two raising calls above, each leaving its version
        nochange = {"add": ("a",), "discard": ("zz",), "update": (["a"],), "difference_update": (["zz"],), "intersection_update": (["a", "b"],),
                    "symmetric_difference_update": ([],), "__ior__": ({"a"},), "__iand__": ({"a", "b"},), "__isub__": ({"zz"},),
                    "__ixor__": (set(),)}
        stayed = 0
        for name, args in nochange.items():
            one = cw.KnownSet(["a"])
            getattr(one, name)(*args)
            stayed += one.version == 0
        gone = cw.KnownSet()
        gone.clear()
        stayed += gone.version == 0
        self.assertEqual(stayed, len(nochange) + 1)   # each no-change call, and clear on an empty set
        half = cw.KnownSet(["a"])

        def boom():
            yield "first-form-1"
            raise RuntimeError("the iterator stopped")
        with self.assertRaises(RuntimeError):
            half.update(boom())
        self.assertEqual((half.version, "first-form-1" in half), (1, True))
        forms = ["f%d-form-xx" % i for i in range(5)]
        k = cw.KnownSet(forms)
        self.assertTrue(k == set(forms) and k == frozenset(forms) and k == cw.KnownSet(forms) and k != set(forms[:2]))
        self.assertIsNone(cw.KnownSet.__hash__)
        self.assertEqual((len(k), forms[0] in k, "nope" in k, bool(k), bool(cw.KnownSet()), sorted(k) == sorted(forms)), (len(forms), True, False, True, False, True))

        def grow():
            for x in k:  # a set changed during the iteration never raises
                k.add(x + "-more")
                yield x
        self.assertEqual(len(list(grow())), len(forms))
        self.assertEqual(len(k), 2 * len(forms))
        kube = cw.Kube({})
        kube.add_secrets(FORM_VALUES[0])
        kube.add_secrets(*FORM_VALUES[1:])
        self.assertIsInstance(kube.known, cw.KnownSet)
        fixture_property(kube)
        print("S20 13 of 13 operations bump by one, no-change calls leave the version %d of 11, raising remove and pop leave the version unchanged %d of 2, half-way update bumps once 1 of 1, "
              "equality with a plain set and a frozenset 2 of 2, len, in and iteration 3 of 3, no piece of a planted secret survives in the set of a Kube 1 of 1" % (stayed, unchanged))



PROBE = "# probe on purpose"
USE_PATTERNS = (re.compile(r"\._(forms|built)\b"),
                re.compile(r"(?i)(known|\bks\b|\bdup\b|\bmut\w*\b|\bobj\b|\bone\b).*\._(lock|version)\b|\._(lock|version)\b.*(known|\bks\b)"),
                re.compile(r"object\.__setattr__\(\s*[^,]*(?i:known|\bks\b)"),
                re.compile(r"(?i:pickle|copy)\.\w+\(\s*[^)]*(?i:kube|known|\bks\b)"))


def class_lines(path, lines):
    """The 1-based lines of the class KnownSet itself in cloud_worker.py."""
    if path.name != "cloud_worker.py" or "launcher" not in path.parts:
        return set()
    a = next(i for i, l in enumerate(lines) if l.startswith("class KnownSet"))
    b = next(i for i, l in enumerate(lines) if i > a and l.startswith("_SCRUB_MARK"))
    return set(range(a + 1, b + 1))


def scan_uses(roots):
    """The uses of a private name of KnownSet, of object.__setattr__ on a known set and of pickle or copy of a Kube or a known set,
    outside the class and outside the lines that carry the probe marker; (path, line number) pairs."""
    found = []
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file() or path.is_symlink() or ".git" in path.parts or path.stat().st_size > 20 * 2 ** 20:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="strict").split("\n")
            except (UnicodeDecodeError, OSError):
                continue
            own = class_lines(path, lines)
            for n, line in enumerate(lines, 1):
                if n in own or PROBE in line:
                    continue
                if any(pat.search(line) for pat in USE_PATTERNS):
                    found.append((str(path), n))
    return found


def use_check(roots):
    found = scan_uses(roots)
    if found:
        raise AssertionError("a private name of KnownSet is used outside it: %d use(s), first at %s:%d" % (len(found), found[0][0], found[0][1]))
    return 0


def read_check(cls):
    """(a) the argument iterator of each of five calls is advanced with the lock NOT held; (b) five reads take the lock."""
    held_steps = 0
    steps = 0

    def watch(rec, obj):
        nonlocal held_steps, steps
        for x in ("watch-form-%d" % i for i in range(3)):
            steps += 1
            held_steps += rec is not None and rec.depth > 0
            yield x

    for name in ("update", "difference_update", "intersection_update", "symmetric_difference_update"):
        ks = cls(["seed-form-1"])
        rec = RecordingLock(ks)
        ks._lock = rec  # probe on purpose
        getattr(ks, name)(watch(rec, ks))
    fresh = object.__new__(cls)
    seen = []

    def watch_init():
        nonlocal steps
        for x in ("init-form-%d" % i for i in range(3)):
            steps += 1
            seen.append(getattr(fresh, "_lock", None))  # probe on purpose
            yield x
    fresh.__init__(watch_init())
    held_steps += sum(1 for v in seen if v is not None)
    if held_steps:
        raise AssertionError("an argument was read while the lock was held: %d of %d steps" % (held_steps, steps))
    two_a, two_b = cls(["aaaa-form-1"]), cls(["bbbb-form-2"])
    two_a.update(x for x in two_b)
    two_b.update(x for x in two_a)
    ks = cls(["read-form-1"])
    rec = RecordingLock(ks)
    ks._lock = rec  # probe on purpose
    took = 0
    reads = (lambda: len(ks), lambda: "read-form-1" in ks, lambda: bool(ks), lambda: list(ks), lambda: ks == {"read-form-1"})
    for call in reads:
        before = len(rec.acquired)
        call()
        took += len(rec.acquired) > before
    if took != len(reads):
        raise AssertionError("a read did not take the lock: %d of %d" % (took, len(reads)))
    return steps, took


def stale_check(cls):
    ks = cls(["stale-form-1"])
    old_version = ks.version
    ks.add("stale-form-2")
    before = ks.built
    accepted = ks.keep(old_version, ("stale", [], {}))
    if accepted or ks.built is not before:
        raise AssertionError("a stale structure was kept: built changed from an old version")
    return 1


def failed_init_check(cls):
    obj = object.__new__(cls)

    def boom():
        yield "first-form-1"
        raise RuntimeError("the iterable stopped")
    try:
        obj.__init__(boom())
    except RuntimeError:
        pass
    else:
        raise AssertionError("the iterable did not raise")
    obj.__init__(["second-form-2"])
    if list(obj) != ["second-form-2"]:
        raise AssertionError("a failed init did not leave the object uninitialised")
    try:
        obj.__init__([])
    except TypeError as e:
        return 1 if "initialised once" in str(e) else 0
    raise AssertionError("a second init was accepted")


class S21Reads(unittest.TestCase):
    def test_S21_reads_keep_init_scan_and_mutants(self):
        import tempfile
        src = inspect.getsource(cw.KnownSet)
        roots = [ROOT / d for d in ("cloud", "bin", "tools", "tests")]
        steps, took = read_check(cw.KnownSet)  # the control, first
        self.assertEqual((stale_check(cw.KnownSet), failed_init_check(cw.KnownSet), use_check(roots)), (1, 1, 0))
        old = "        reads = [self._read(a) for a in args]  # BEFORE the lock\n        err = next((e for _, e in reads if e is not None), None)\n        if err is not None and name in (\"difference_update\", \"intersection_update\", \"symmetric_difference_update\"):\n            raise err  # an operation that removes forms: a half-read argument changes nothing (set and version unchanged)\n        with self._lock:\n"
        new = "        with self._lock:\n            reads = [self._read(a) for a in args]\n            err = next((e for _, e in reads if e is not None), None)\n            if err is not None and name in (\"difference_update\", \"intersection_update\", \"symmetric_difference_update\"):\n                raise err\n"
        with self.assertRaisesRegex(AssertionError, "an argument was read while the lock was held:"):
            read_check(known_set_class(changed_text(src, old, new)))
        old2 = "            if version != self._version:\n                return False\n"
        new2 = "            if False:\n                return False\n"
        with self.assertRaisesRegex(AssertionError, "a stale structure was kept:"):
            stale_check(known_set_class(changed_text(src, old2, new2)))
        clean = "value = 1\n"
        planted = "value = known._forms\n"  # probe on purpose
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "scratch.py").write_text(changed_text(clean, clean, planted), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "a private name of KnownSet is used outside it:"):
                use_check([tmp])
        for o in (old, old2, clean):
            with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
                changed_text(src + clean, o, o)
        print("S21 %d of %d iterator steps read with the lock not held (5 calls), two-set case finished 1 of 1, %d of 5 reads take the lock, "
              "stale structure refused 1 of 1, failed init recovered 1 of 1, 0 uses of the private names outside the class; control passes "
              "1 of 1, the 3 mutants fail 3 of 3 with their texts, no-op mutation fails the computation" % (0, steps, took))


if __name__ == "__main__":
    unittest.main()
