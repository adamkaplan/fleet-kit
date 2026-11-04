"""Scenarios of ask #1783 (S2, S3, S5-lite, S8, U1): synthetic values only, counts printed, no value printed."""
import inspect, os, sys, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cloud_worker as cw

TOK = "ghs_" + "A1b2C3d4" * 4 + "wxyz"
LOGIN = "eyJ" + "Qr7Zk9" * 20
PW = "pw" + "Xy_-9" * 8
MODEL = '{"provider":"p","key":"sk-' + "k" * 20 + '","base":"http://m.example"}'

import re, json as _json, random as _random, string as _string
import pathlib
_R = _random.Random(1783)  # the fixed seed of the fixtures
_AL = _string.ascii_letters + _string.digits + "-_"
G34 = "ghs_" + "".join(_R.choice(_string.ascii_letters + _string.digits) for _ in range(36))
L34 = "eyJ" + "".join(_R.choice(_AL) for _ in range(717))
P34 = "".join(_R.choice(_AL) for _ in range(43))
H34 = "".join(_R.choice("0123456789abcdef") for _ in range(64))
SHORT_KEY = "sk-" + "".join(_R.choice(_string.ascii_lowercase) for _ in range(20))
M34 = _json.dumps({"provider": "openai", "key": SHORT_KEY, "base": "http://m.example"})
FIXTURES = (G34, L34, P34, H34, SHORT_KEY, M34)


class S(unittest.TestCase):
    def kf(self):
        return cw.KnownForms()

    def test_s2_events(self):
        k = self.kf()
        forms = {}
        for name, v, life in (("tok", TOK, ("until", 3660.0, None)), ("login", LOGIN, ("until", 2400.0, None)), ("pw", PW, ("pod", "p1"))):
            fs = set(cw.secret_forms(v)); forms[name] = fs
            k.add_forms(fs, life)
        mf = set(cw.secret_forms(MODEL)); k.add_forms(mf, None)
        total = len(k)
        self.assertEqual(k.sweep(0, {"p1"}), 0)
        self.assertEqual(len(k), total)
        k.sweep(3659, {"p1"}); self.assertTrue(forms["tok"] <= set(k))
        k.sweep(3660, {"p1"}); self.assertFalse(forms["tok"] & set(k) - forms["login"] - forms["pw"] - mf)
        k.sweep(3661, {"x"}); self.assertTrue(forms["pw"] <= set(k)); k.sweep(3661, {"x"}); self.assertFalse(forms["pw"] & set(k) - mf)
        self.assertTrue(mf <= set(k), "a model-entry form was dropped")

    def test_s3_running_pod_kept(self):
        k = self.kf(); fs = set(cw.secret_forms(TOK))
        k.add_forms(fs, ("until", 10.0, "p1"))
        self.assertEqual(k.sweep(99999, {"p1"}), 0)
        self.assertTrue(TOK not in cw.scrub("x " + TOK, k), "a form of a running pod was dropped")
        self.assertEqual(k.sweep(99999, {"x"}), 0); self.assertTrue(fs <= set(k))
        n = k.sweep(99999, {"x"}); self.assertEqual(n, len(fs)); self.assertEqual(len(k), 0)

    def test_s4_scrub_same_for_live(self):
        k = self.kf(); k.add_forms(cw.secret_forms(PW), ("pod", "p1")); k.add_forms(cw.secret_forms(TOK), ("pod", "p2"))
        k.sweep(0, {"p1"}); k.sweep(0, {"p1"})
        live = set(cw.secret_forms(PW)); text = "a %s b %s" % (PW, TOK)
        self.assertEqual(cw.scrub(text, k), cw.scrub(text, live))

    def test_s5_bound(self):
        k = self.kf(); peak = 0
        for m in range(0, 7 * 1440, 10):
            t = m * 60.0
            pods = {"p%d" % i for i in range(max(0, m // 10 - 6), m // 10 + 1)}
            k.add_forms(cw.secret_forms("t%d" % m + TOK), ("until", t + 3660, "p%d" % (m // 10)))
            k.add_forms(cw.secret_forms("l%d" % m + LOGIN), ("until", t + 3660, None))
            k.sweep(t, pods); peak = max(peak, len(k))
        print("peak forms", peak)
        self.assertLessEqual(peak, 40 * 7 + 12 + 34, "the known forms grew past the bound")

    def test_s8_threads(self):
        k = self.kf(); errs = []
        def adder():
            for i in range(200):
                k.add_forms(cw.secret_forms("v%d" % i + TOK), ("pod", "live"))
        def sweeper():
            for i in range(200): k.sweep(0, {"live"})
        def scr():
            try:
                for i in range(200): cw.scrub("text", k)
            except Exception as e: errs.append(type(e).__name__)
        ts = [threading.Thread(target=f, daemon=True) for f in (adder, sweeper, scr)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])

    def test_u1_signature(self):
        self.assertEqual(str(inspect.signature(cw.scrub)), "(text, secrets_known=(), limit=300)")

    def test_no_life_never_leaves(self):
        k = self.kf(); k.add_forms(cw.secret_forms(MODEL)); k.add_forms(cw.secret_forms(MODEL), ("pod", "x"))
        k.sweep(1e12, set()); self.assertEqual(len(k), len(set(cw.secret_forms(MODEL))))


class S3More(unittest.TestCase):
    """S3 as the plan states it: git token and login token past their hour, the pod in the list, then out of it."""

    def test_s3_running_then_left(self):
        k = cw.KnownForms(); gf, lf = set(cw.secret_forms(G34)), set(cw.secret_forms(L34))
        k.add_forms(gf, ("until", 3660.0, "p1")); k.add_forms(lf, ("until", 2400.0, "p1"))
        self.assertEqual(k.sweep(99999, {"p1"}), 0)
        self.assertTrue((gf | lf) <= set(k), "a form of a running pod was dropped")
        out = cw.scrub("x " + G34 + " y " + L34, k)
        self.assertTrue(G34 not in out and L34 not in out, "a form of a running pod was dropped")
        kept = len(gf)
        self.assertEqual(k.sweep(99999, {"x"}), 0); self.assertTrue((gf | lf) <= set(k), "a form of a pod missing from one list was dropped")
        gone = k.sweep(99999, {"x"})
        print("S3 kept %d of %d, redacted 1 of 1, dropped after leaving %d of %d" % (kept, len(gf), gone, len(gf | lf)))
        self.assertEqual(gone, len(gf | lf)); self.assertEqual(len(k), 0)
        self.assertEqual((len(gf), len(lf)), (8, 6))


class S2Kinds(unittest.TestCase):
    """S2 as the plan states it: each kind gone exactly at its event; a mutant for each drop event by kind."""

    def world(self, cls):
        k = cls()
        self.F = {n: set(cw.secret_forms(v)) for n, v in (("git", G34), ("login", L34), ("pw", P34), ("hub", H34), ("model", M34))}
        k.add_forms(self.F["git"], ("until", 3660.0, None))
        k.add_forms(self.F["login"], ("until", 2400.0, None))
        k.add_forms(self.F["pw"], ("pod", "p1"))
        k.add_forms(self.F["hub"], ("pod", "p2"))
        k.add_forms(self.F["model"], None); k.add_forms(self.F["model"], ("pod", "p1"))
        return k

    def check(self, cls):
        k = self.world(cls); total = len(k); removed = []
        self.assertEqual(sum(len(v) for v in self.F.values()), total, "the fixtures overlap")
        # control: no event, 0 of 63 dropped
        self.assertEqual(k.sweep(0, {"p1", "p2"}) + k.sweep(0, {"p1", "p2"}), 0, "a form was dropped with no event")
        self.assertEqual(len(k), total, "a form was dropped with no event")
        for now, pods, kind in ((2400, {"p1", "p2"}, "login"), (3660, {"p1", "p2"}, "git"), (3660, {"p2"}, "pw"), (3660, {"x"}, "hub")):
            if kind in ("login", "git"):
                before = now - 1
                self.assertEqual(k.sweep(before, pods), 0, "a form was dropped before its event (" + kind + ")")
            elif kind == "pw":
                pre = k.sweep(now, pods | {"p1"})
                self.assertTrue(self.F["model"] <= set(k), "a model-entry form was dropped")
                self.assertEqual(pre, 0, "a form was dropped before its event (" + kind + ")")
            else:
                self.assertEqual(k.sweep(now, pods | {"p2"}), 0, "a form was dropped before its event (" + kind + ")")
            if kind in ("pw", "hub"):
                first = k.sweep(now, pods)
                self.assertTrue(self.F["model"] <= set(k), "a model-entry form was dropped")
                self.assertEqual(first, 0, "a form was dropped before its event (" + kind + ")")
            n = k.sweep(now, pods)
            self.assertTrue(self.F["model"] <= set(k), "a model-entry form was dropped")
            self.assertEqual(n, len(self.F[kind]), "a %s form was not dropped at its event" % kind)
            self.assertFalse(self.F[kind] & set(k), "a %s form was not dropped at its event" % kind)
            removed.append(n)
        self.assertTrue(self.F["model"] <= set(k), "a model-entry form was dropped")
        self.assertEqual(len(k), len(self.F["model"]), "a model-entry form was dropped")
        return total, removed, len(self.F["model"])

    def test_control_counts(self):
        total, removed, m = self.check(cw.KnownForms)
        print("S2 forms before %d, removed in turn %s, model kept %d of %d, control dropped 0 of %d" % (total, removed, m, m, total))
        self.assertEqual((total, removed, m), (63, [6, 8, 8, 7], 34))

    def mutant(self, old, new):
        src = Mutants.SRC
        self.assertEqual(src.count(old), 1)
        src = src.replace(old, new)
        self.assertNotEqual(src, Mutants.SRC)
        ns = {"__name__": "mut", "__file__": cw.__file__}
        exec(compile(src, "mut", "exec"), ns)
        return ns["KnownForms"]

    def test_mutants_by_kind(self):
        until = '(l[0] == "until" and now >= l[1] and l[2] not in pods)'
        pod = '(l[0] == "pod" and l[1] not in pods)'
        cases = (
            ("never drops by instant", until, "False", "was not dropped at its event"),
            ("never drops by pod", pod, "False", "was not dropped at its event"),
            ("drops one step early", 'now >= l[1] and l[2] not in pods', 'now >= l[1] - 1 and l[2] not in pods', "a form was dropped before its event"),
            ("drops at the first absence", ' and (l[1] if l[0] == "pod" else l[2]) in before}', '}', "a form was dropped before its event"),
            ("drops every kind by one fixed age", 'if lives is None:\n                    continue\n', 'if False:\n                    continue\n', None),
        )
        failed = 0
        for name, old, new, text in cases:
            if name.startswith("drops every kind"):
                new = 'if lives is None and now < 3660:\n                    continue\n                lives = lives or {("pod", "x")}\n'
                text = "a model-entry form was dropped"
            bad = self.mutant(old, new)
            with self.assertRaises(AssertionError, msg=name) as cm:
                self.check(bad)
            self.assertIn(text, str(cm.exception), name)
            failed += 1
        print("S2 mutants failed %d of %d with their anchored text" % (failed, len(cases)))
        self.assertEqual(failed, len(cases))


def OLD_SCRUB(text, secrets_known=(), limit=300):
    """The frozen copy of `scrub` as it stood at the integration tip ec5eabf1, kept verbatim as the reference (ask #1783)."""
    text = str(text)[:cw.SCRUB_INPUT_MAX]
    for sec in sorted({v for v in secrets_known if v and len(v.strip()) >= cw.MIN_FRAGMENT}, key=lambda v: (-len(v), v)):
        for v in {sec, sec.strip()}:
            text = text.replace(v, "[redacted]")
        out, i = [], 0
        while i < len(text):  # any run of 8+ characters that is a piece of the secret
            head = text[i:i + cw.MIN_FRAGMENT]
            if len(head) == cw.MIN_FRAGMENT and head in sec:
                j = i + cw.MIN_FRAGMENT
                while j < len(text) and text[i:j + 1] in sec:
                    j += 1
                out.append("[redacted]")
                i = j
            else:
                out.append(text[i])
                i += 1
        text = "".join(out)
    for pat, repl in cw.CREDENTIAL_SHAPES:
        text = pat.sub(repl, text)
    for pat in cw.TOKEN_SHAPES:
        text = pat.sub("[redacted]", text)
    return re.sub(r"[^\x20-\x7e]", "?", text)[:limit]


class S4Same(unittest.TestCase):
    SEED = 1783

    def test_s4_two_thousand_texts(self):
        extra = ["ghs_" + "".join(_random.Random(i).choice(_string.ascii_letters) for _ in range(36)) for i in range(5)]
        k = cw.KnownForms()
        for i, t in enumerate(extra):
            k.add_forms(cw.secret_forms(t), ("until", 100.0 + i, None))
        k.add_forms(cw.secret_forms(G34), ("until", 5000.0, "p1")); k.add_forms(cw.secret_forms(P34), ("pod", "p1"))
        k.add_forms(cw.secret_forms(M34), None); k.add_forms(cw.secret_forms(H34), ("pod", "p9"))
        k.sweep(1000.0, {"p1", "p9"}); k.sweep(1000.0, {"p1", "p9"})  # the five extra tokens are dropped, the rest is live
        live = set(cw.secret_forms(G34)) | set(cw.secret_forms(P34)) | set(cw.secret_forms(M34)) | set(cw.secret_forms(H34))
        self.assertTrue(set(k) == live, "the live set differs from the expected one: %d held, %d expected" % (len(set(k)), len(live)))
        pool = sorted(live)
        ordered = list(k)  # the live forms in the order the new scrub reads them (equal-length forms are tied in the sort, so the order is part of the output)
        rnd = _random.Random(self.SEED); same = 0
        for n in range(2000):
            parts = []
            for _ in range(rnd.randint(1, 5)):
                parts.append("".join(rnd.choice(_string.ascii_letters + " :=/\n") for _ in range(rnd.randint(0, 20))))
                if rnd.random() < 0.8:
                    f = rnd.choice(pool)
                    parts.append(f if rnd.random() < 0.7 else f[rnd.randint(0, len(f) // 2):])
            text = "".join(parts)
            same += cw.scrub(text, k, 100000) == OLD_SCRUB(text, ordered, 100000)
        print("S4 seed %d: %d of 2000 outputs byte-identical" % (self.SEED, same))
        self.assertEqual(same, 2000, "an output differs from the frozen old function's")

    def test_s4_before_and_after_the_drop(self):
        kinds = (("git", G34, ("until", 3660.0, None)), ("login", L34, ("until", 2400.0, None)), ("pw", P34, ("pod", "p1")), ("hub", H34, ("pod", "p2")))
        k = cw.KnownForms(); nforms = shaped = before = after = 0
        for _, v, life in kinds:
            k.add_forms(cw.secret_forms(v), life)
        k.add_forms(cw.secret_forms(M34), ("pod", "p1")); k.add_forms(cw.secret_forms(M34), None)
        for _, v, _ in kinds:
            before += v not in cw.scrub("x " + v + " y", k, 5000)
        k.sweep(99999, {"x"}); k.sweep(99999, {"x"})
        for name, v, _ in kinds:
            after += v not in cw.scrub("x " + v + " y", k, 5000)
            for f in cw.secret_forms(v):
                if len(f) >= 8:
                    nforms += 1
                    shaped += f not in cw.scrub("x " + f + " y", (), 5000)
        self.assertEqual(len(k), len(set(cw.secret_forms(M34))))
        self.assertTrue(SHORT_KEY in cw.scrub("x " + SHORT_KEY, (), 300), "the shapes were expected not to catch a short key")
        short = SHORT_KEY not in cw.scrub("x " + SHORT_KEY + " y", k, 5000)
        print("S4 redacted before the drop %d of 4, after it by the shapes %d of 4 with %d of %d forms, short model key kept %d of 1" % (before, after, shaped, nforms, short))
        self.assertEqual((before, after, shaped, nforms, short), (4, 4, 29, 29, 1))


def fake_world(cls, seed=1783):
    """Seven simulated days, one pass a simulated minute: 300 pods launched a day living 5 to 180 minutes, a renewal 30 minutes after each mint of a running pod, a login refresh every 40 minutes, each pod's stop learning its password; one model entry. Returns (count after every pass, the most pods running at once)."""
    R = _random.Random(seed); k = cls()
    k.add_forms(cw.secret_forms(M34), None)
    pods, counts, peak_run, n, last_login, li = {}, [], 0, 0, -99, 0
    launch = sorted(R.uniform(0, 7 * 1440) for _ in range(300 * 7))
    for minute in range(7 * 1440):
        for name in [p for p, v in pods.items() if v[1] <= minute]:  # the stop learns the password, the pod leaves the list
            k.add_forms(cw.secret_forms("pw%s%s" % (name, "x" * 38)), ("pod", name)); del pods[name]
        while li < len(launch) and launch[li] <= minute:
            n += 1; nm = "p%d" % n; pods[nm] = [minute, minute + R.randint(5, 180), minute + 30]; li += 1
            k.add_forms(cw.secret_forms("ghs_%s%s" % (nm, "T" * (32 - len(nm)))), ("until", minute * 60 + 3660.0, nm))
        for nm, v in pods.items():
            if minute >= v[2]:
                v[2] += 30
                k.add_forms(cw.secret_forms("ghs_%s_%d%s" % (nm, minute, "U" * 20)), ("until", minute * 60 + 3660.0, nm))
        if minute - last_login >= 40:
            last_login = minute
            k.add_forms(cw.secret_forms("eyJ%d%s" % (minute, "L" * 100)), ("until", minute * 60 + 3660.0, None))
        k.sweep(minute * 60.0, set(pods))
        counts.append(len(k)); peak_run = max(peak_run, len(pods))
    return counts, peak_run


class S5Week(unittest.TestCase):
    def check(self, cls):
        counts, peak = fake_world(cls)
        bound = 40 * peak + 34 + 12
        self.assertEqual(len(counts), 10080)
        self.assertLessEqual(max(counts), bound, "the known forms grew past the bound")
        return max(counts), bound, counts[-1]

    def test_control_and_mutant(self):
        top, bound, last = self.check(cw.KnownForms)
        print("S5 control: largest count %d, bound %d, %d passes, last %d" % (top, bound, 10080, last))
        src = Mutants.SRC; old = "        if not pods:\n            with self._lock:\n                self._absent = set()\n            return 0\n"
        self.assertEqual(src.count(old), 1)
        mut = src.replace(old, "        if True:\n            return 0\n")
        self.assertNotEqual(mut, src)
        ns = {"__name__": "mut", "__file__": cw.__file__}
        exec(compile(mut, "mut", "exec"), ns)
        with self.assertRaises(AssertionError) as cm:
            self.check(ns["KnownForms"])
        self.assertIn("the known forms grew past the bound", str(cm.exception))
        counts, _ = fake_world(ns["KnownForms"])
        print("S5 mutant that never drops: ends the week at %d forms, above the bound %d, fails 1 of 1" % (counts[-1], bound))
        self.assertGreater(counts[-1], bound)


class S8Rounds(unittest.TestCase):
    def test_200_rounds(self):
        class Rec(cw.KnownForms):
            def __iter__(self):
                self.snap = list(cw.KnownForms.__iter__(self))
                return iter(self.snap)
        raises = kept = same = 0
        for r in range(200):
            k = Rec(); k.snap = []
            for i in range(6):  # forms that the sweep really removes
                k.add_forms(cw.secret_forms("old%d_%d" % (r, i) + P34[:20]), ("until", 0.0, None))
            k.add_forms(cw.secret_forms(G34), ("pod", "live"))
            added = set(cw.secret_forms("new%d" % r + H34)); errs = []; out = []
            text = "a %s b %s c new%d%s" % (G34, P34[:20], r, H34)
            go = threading.Barrier(3)
            def adder():
                try: go.wait(); k.add_forms(added, ("pod", "live"))
                except Exception as e: errs.append(type(e).__name__)
            def sweeper():
                try: go.wait(); k.sweep(1.0, {"live"}); k.sweep(1.0, {"live"})
                except Exception as e: errs.append(type(e).__name__)
            def scr():
                try:
                    go.wait(); o = cw.scrub(text, k, 100000); out.append((o, list(k.snap)))
                except Exception as e: errs.append(type(e).__name__)
            ts = [threading.Thread(target=f, daemon=True) for f in (adder, sweeper, scr)]
            [t.start() for t in ts]; [t.join() for t in ts]
            raises += bool(errs); kept += added <= set(k)
            if out:
                same += out[0][0] == OLD_SCRUB(text, out[0][1], 100000)
        print("S8 rounds 200: %d raised, %d of 200 added forms kept, %d of 200 outputs equal to the frozen old function's on their own snapshot" % (raises, kept, same))
        self.assertEqual((raises, kept, same), (0, 200, 200))


class S6NoValueInOutput(unittest.TestCase):
    """S6: every output of the checks, in a pass and in a forced failure, holds no fixture value."""
    RUNNING = False

    def run_suites(self):
        import io, contextlib
        buf = io.StringIO()
        suites = unittest.TestSuite()
        for c in (S2Kinds, S3More, S4Same, S5Week, S8Rounds, Mutants, S14TwoLists):
            suites.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(c))
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            res = unittest.TextTestRunner(stream=buf, verbosity=2).run(suites)
        return buf.getvalue(), res

    def scan(self, text):
        return sum(text.count(v) for v in FIXTURES + (TOK, LOGIN, PW, MODEL, G34[4:], L34[3:])) + sum(text.count(f) for f in cw.secret_forms(SHORT_KEY) if len(f) >= 8)

    def test_no_value_in_a_pass_or_a_forced_failure(self):
        if S6NoValueInOutput.RUNNING:
            return
        S6NoValueInOutput.RUNNING = True
        try:
            out_pass, r1 = self.run_suites()
            self.assertTrue(r1.wasSuccessful(), "the suites do not pass")
            def drop_all(self, now, pods):
                with self._lock:
                    n = len(self); self._lives.clear(); set.clear(self); return n
            def never(self, now, pods):
                return 0
            outs = []
            for name, fn in (("drop-all", drop_all), ("never-drop", never)):
                old = cw.KnownForms.sweep; cw.KnownForms.sweep = fn
                try:
                    o, r = self.run_suites()
                finally:
                    cw.KnownForms.sweep = old
                self.assertFalse(r.wasSuccessful(), "the forced failure did not fail (" + name + ")")
                outs.append((name, o, len(r.failures) + len(r.errors)))
        finally:
            S6NoValueInOutput.RUNNING = False
        hits = self.scan(out_pass) + sum(self.scan(o) for _, o, _ in outs)
        print("S6 outputs scanned: 1 pass (%d chars) and %d forced failures (%s): %d occurrences of a fixture value" % (len(out_pass), len(outs), ", ".join("%s %d failing checks" % (n, f) for n, _, f in outs), hits))
        self.assertEqual(hits, 0, "a fixture value appears in an output")


class Mutants(unittest.TestCase):
    """S3 and S2 mutants: a scratch copy of the sweep, text asserted once, mutation asserted applied."""

    SRC = pathlib.Path(cw.__file__).read_text()

    def mutant(self, old, new):
        self.assertEqual(self.SRC.count(old), 1)
        src = self.SRC.replace(old, new)
        self.assertNotEqual(src, self.SRC)
        self.last_src = src
        ns = {"__name__": "mut", "__file__": cw.__file__}
        exec(compile(src, "mut", "exec"), ns)
        return ns["KnownForms"]

    def check_running(self, cls):
        k = cls(); fs = set(cw.secret_forms(TOK))
        k.add_forms(fs, ("until", 10.0, "p1"))
        k.sweep(99999, {"p1"})
        self.assertTrue(fs <= set(k), "a form of a running pod was dropped")
        self.assertNotIn(TOK, cw.scrub("x " + TOK, k))
        return len(fs)

    def test_s3_control_and_mutant(self):
        self.assertEqual(self.check_running(cw.KnownForms), 8)
        bad = self.mutant('(l[0] == "until" and now >= l[1] and l[2] not in pods)) and (l[1] if l[0] == "pod" else l[2]) in before}', '(l[0] == "until" and now >= l[1]))}')
        with self.assertRaises(AssertionError) as cm:
            self.check_running(bad)
        self.assertIn("a form of a running pod was dropped", str(cm.exception))

    def check_model(self, cls):
        k = cls(); mf = set(cw.secret_forms(MODEL))
        k.add_forms(mf, None); k.add_forms(mf, ("pod", "p1"))
        k.sweep(1e12, {"x"}); k.sweep(1e12, {"x"})
        self.assertTrue(mf <= set(k), "a model-entry form was dropped")
        return len(mf)

    def test_s2_model_control_and_mutant(self):
        print("model forms kept", self.check_model(cw.KnownForms))
        bad = self.mutant("                if lives is None:\n                    continue\n", "                if lives is None:\n                    lives = set()\n")
        with self.assertRaises(AssertionError) as cm:
            self.check_model(bad)
        self.assertIn("a model-entry form was dropped", str(cm.exception))


class S2KindMutants(unittest.TestCase):
    """S2 (ask #1783): one mutant per dropping kind that keeps that kind for ever, beside the model-entry mutant."""

    KEEP = {}
    NAMES = (("git", "a git-token form was not dropped"), ("login", "a login-token form was not dropped"),
             ("pw", "a pod-password form was not dropped"), ("hub", "a hub-secret form was not dropped"))

    def mutant(self, kind):
        old = "            for f, lives in list(self._lives.items()):\n"
        src = Mutants.SRC
        self.assertEqual(src.count(old), 1)
        new = old + "                if f in KEEP:\n                    continue\n"
        mut = src.replace(old, new)
        self.assertNotEqual(mut, src)
        ns = {"__name__": "mut", "__file__": cw.__file__, "KEEP": self.KEEP[kind]}
        exec(compile(mut, "mut", "exec"), ns)
        return ns["KnownForms"]

    def run_check(self, cls, kind, text):
        k = S2Kinds("test_control_counts").world(cls)
        F = {"git": set(cw.secret_forms(G34)), "login": set(cw.secret_forms(L34)), "pw": set(cw.secret_forms(P34)), "hub": set(cw.secret_forms(H34))}
        k.sweep(10 ** 9, {"x"}); k.sweep(10 ** 9, {"x"})
        self.assertFalse(F[kind] & set(k), text)
        return len(F[kind])

    def test_one_mutant_per_kind(self):
        for kind, v in (("git", G34), ("login", L34), ("pw", P34), ("hub", H34)):
            self.KEEP[kind] = set(cw.secret_forms(v))
        failed = 0; removed = []
        for kind, text in self.NAMES:
            removed.append(self.run_check(cw.KnownForms, kind, text))   # the control first
            with self.assertRaises(AssertionError) as cm:
                self.run_check(self.mutant(kind), kind, text)
            self.assertIn(text, str(cm.exception))
            failed += 1
        print("S2 per-kind controls removed %s, mutants failed %d of 4 with their own anchored text" % (removed, failed))
        self.assertEqual((removed, failed), ([8, 6, 8, 7], 4))


class _Launch:
    """Fakes for the reaper's two own paths (S11), S12 and S13."""

    @staticmethod
    def fresh_known():
        k = cw.KnownForms()
        return k


class S11ReaperPaths(unittest.TestCase):
    import base64 as _b64

    def renewal(self, mod):
        import datetime as dt
        k = mod["KnownForms"]()
        kube = mod["Kube"].__new__(mod["Kube"]); kube.known = k
        secret = {"metadata": {"labels": {"cw/pod": "p1", "cw/run-id": "r"}, "uid": "u", "resourceVersion": "1"},
                  "data": {"git-token": self._b64.b64encode(G34.encode()).decode(), "model-auth": self._b64.b64encode(M34.encode()).decode()}}
        kube.get_run_secret = lambda name: secret
        la = mod["Launcher"].__new__(mod["Launcher"]); la.kube = kube; la.env = {}
        pod = {"metadata": {"name": "p1", "annotations": {"cw/worker": "w1", "cw/run-id": "r"}}, "status": {"phase": "Running"}}
        try:
            la._renew_git_token(pod, dt.datetime.now(dt.timezone.utc), force=True)
        except Exception:
            pass   # the path after the add is not under test
        return k

    def record(self, mod):
        k = mod["KnownForms"]()
        kube = type("K", (), {})(); kube.known = k
        kube.add_secrets = lambda *v, life=None: k.add_forms([f for x in v for f in cw.secret_forms(x)], life)
        files = {"git-token": G34, "model-auth": M34}
        kube.exec = lambda pod, *cmd, **kw: "\n".join(files) if cmd[0] == "ls" else files[cmd[-1].split("/")[-1]]
        rr = mod["rr"].Recorder(kube, {})
        rr._learn_secrets("p1")
        return k

    def check(self, path, build):
        k = build()
        self.assertEqual(k.sweep(0, {"p1"}), 0, "a form was dropped while its pod ran")
        self.assertEqual(k.sweep(1, {"other"}), 0, "a form of a pod missing from one list was dropped")
        gone = k.sweep(1, {"other"})
        mf = set(cw.secret_forms(M34))
        self.assertTrue(mf <= set(k), "a model-entry form was dropped on the reaper's path")
        self.assertFalse(set(cw.secret_forms(G34)) & set(k), "a git-token form was kept")
        self.assertNotIn(SHORT_KEY, cw.scrub("x " + SHORT_KEY, k), "a model-entry form was dropped on the reaper's path")
        return gone, len(mf & set(k)), len(SHORT_KEY)

    def load(self, edits):
        import importlib.util
        rr_path = os.path.join(os.path.dirname(cw.__file__), "run_record.py")
        sources = {"cw": pathlib.Path(cw.__file__).read_text(), "rr": pathlib.Path(rr_path).read_text()}
        for key, old, new in edits:
            self.assertEqual(sources[key].count(old), 1)
            mutated = sources[key].replace(old, new)
            self.assertNotEqual(mutated, sources[key])
            sources[key] = mutated
        ns = {"__name__": "mut", "__file__": cw.__file__}
        exec(compile(sources["cw"], "mut", "exec"), ns)
        rrns = {"__name__": "run_record_mut", "__file__": rr_path}
        exec(compile(sources["rr"], "mut_rr", "exec"), rrns)
        import types
        ns["rr"] = types.SimpleNamespace(**rrns)
        return ns

    def test_s11(self):
        life = '], life=("pod", (pod.get("metadata") or {}).get("name")))'
        paths = (("renewal", self.renewal, ("cw", 'and k == "model-auth"])', 'and k == "model-auth"' + life)),
                 ("record", self.record, ("rr", 'self.kube.add_secrets(text.strip())   # the model entry', 'self.kube.add_secrets(text.strip(), life=("pod", pod))   # the model entry')))
        kept = failed = 0
        for name, fn, edit in paths:
            gone, n, ln = self.check(name, lambda: fn(self.load([])))
            self.assertEqual((gone, n), (8, 34), name)
            kept += 1
            with self.assertRaises(AssertionError, msg=name) as cm:
                self.check(name, lambda: fn(self.load([edit])))
            self.assertIn("a model-entry form was dropped on the reaper's path", str(cm.exception))
            failed += 1
        print("S11 git-token forms gone 8 of 8 and model forms kept 34 of 34 on %d of 2 paths (short key of %d characters redacted), copies failed %d of 2" % (kept, ln, failed))


class S12S13Sweep(unittest.TestCase):
    def launcher(self, mod, pods, known):
        import tempfile
        d = tempfile.mkdtemp()
        kube = type("K", (), {})()
        kube.known = known
        kube.list_pods = lambda: pods
        kube.scrub = lambda t, limit=300: str(t)[:limit]
        kube.add_secrets = lambda *a, **k: None
        la = mod["Launcher"].__new__(mod["Launcher"])
        import datetime as dt
        la.kube, la.env, la.clock = kube, {"CW_CLOUD_AUDIT": os.path.join(d, "a.jsonl")}, (lambda: dt.datetime.now(dt.timezone.utc))
        la.audit_path = la.env["CW_CLOUD_AUDIT"]
        la.renew_lock = threading.RLock(); la.relogin = None
        la.reap_results = {}; la.last_hook = None
        return la

    def lines(self, la):
        try:
            return [l for l in pathlib.Path(la.audit_path).read_text().splitlines() if l]
        except OSError:
            return []

    def pass_world(self, mod, pods):
        k = mod["KnownForms"]()
        k.add_forms(cw.secret_forms(G34), ("pod", "p1")); k.add_forms(cw.secret_forms(P34), ("pod", "p2"))
        k.add_forms(cw.secret_forms(M34), None); k.add_forms(cw.secret_forms(H34), ("pod", "p2"))
        k.add_forms(cw.secret_forms(L34), ("pod", "p1"))
        la = self.launcher(mod, pods, k)
        return la, k

    def mod(self, old=None, new=None, *more):
        src = Mutants.SRC
        for old, new in ((((old, new),) if old else ()) + tuple(zip(more[::2], more[1::2]))):
            self.assertEqual(src.count(old), 1)
            m = src.replace(old, new)
            self.assertNotEqual(m, src)
            src = m
        ns = {"__name__": "mut", "__file__": cw.__file__}
        exec(compile(src, "mut", "exec"), ns)
        return ns

    def check12(self, mod):
        class Once(dict):   # a fault injected into the sweep only: the first read of the entry raises
            hit = False
            def get(self, *a):
                if not Once.hit:
                    Once.hit = True
                    raise TypeError("injected")
                return dict.get(self, *a)
        la, k = self.pass_world(mod, [Once({"metadata": {"name": "p1"}})]); total = len(k)
        try:
            la.reap()
        except Exception:
            raise AssertionError("the sweep raised into the pass") from None
        ln = self.lines(la)
        self.assertEqual(len(k), total, "a form was dropped")
        return total, len(ln), ln

    def test_s12(self):
        total, n, ln = self.check12(self.mod())
        self.assertEqual((total, n), (63, 1))
        self.assertIn("known-forms.sweep-failed", ln[0]); self.assertIn("TypeError", ln[0])
        bad = self.mod('            except Exception as e:   # the sweep never raises', '            except KeyboardInterrupt as e:   # the sweep never raises')
        with self.assertRaises(AssertionError) as cm:
            self.check12(bad)
        self.assertIn("the sweep raised into the pass", str(cm.exception))
        print("S12 the pass ended, %d audit line, %d of 63 forms kept; the copy outside the try failed 1 of 1" % (n, total))

    def check13(self, mod):
        la, k = self.pass_world(mod, [])
        total = len(k)
        k.sweep(0, {"p9"})   # the two pods are absent once
        try:
            la.reap()   # the empty list
        except Exception:
            pass
        self.assertEqual(len(k), total, "an empty list dropped forms")
        ln = self.lines(la)
        k.sweep(0, {"p9"})   # absent once again, not twice in a row
        self.assertEqual(len(k), total, "a form of a pod missing from one list was dropped")
        k.sweep(0, {"p9"})
        return total, ln, len(k)

    def test_s13(self):
        total, ln, after = self.check13(self.mod())
        self.assertEqual(len(ln), 1); self.assertIn("known-forms.sweep-skipped", ln[0]); self.assertIn("empty pod list", ln[0])
        self.assertEqual(after, 34)
        bad = self.mod('                if not pods:   # an empty', '                if False:   # an empty', '        if not pods:\n            with self._lock:\n                self._absent = set()\n            return 0\n', '        pods = set(pods or ())\n')
        with self.assertRaises(AssertionError) as cm:
            self.check13(bad)
        self.assertIn("an empty list dropped forms", str(cm.exception))
        print("S13 0 of %d forms dropped on the empty list, 1 audit line, the next non-empty pass left %d (the model entry); the copy failed 1 of 1" % (total, after))


class S14TwoLists(unittest.TestCase):
    """S14: a form is released only when its pod was absent from two sweeps in a row whose lists were not empty."""
    MSG = "a form of a pod missing from one list was dropped"

    def fresh(self, cls):
        k = cls(); fs = set(cw.secret_forms(TOK)); k.add_forms(fs, ("pod", "p1")); return k, fs

    def check(self, cls):
        k, fs = self.fresh(cls)
        k.sweep(1.0, {"p0", "p1"}); k.sweep(2.0, {"p0"})
        kept1 = len(fs & set(k))
        self.assertEqual(kept1, len(fs), self.MSG + " (kept %d of %d)" % (kept1, len(fs)))
        self.assertNotIn(TOK, cw.scrub("x " + TOK, k), self.MSG)
        k.sweep(3.0, {"p0"}); gone = len(fs) - len(fs & set(k))
        k2, _ = self.fresh(cls); kept = []
        for pods in ({"p0"}, {"p0", "p1"}, {"p0"}):
            k2.sweep(1.0, pods); kept.append(len(fs & set(k2)))
        self.assertEqual(kept, [len(fs)] * 3, self.MSG)
        k2.sweep(1.0, {"p0"}); gone2 = len(fs) - len(fs & set(k2))
        k3, _ = self.fresh(cls)
        k3.sweep(1.0, {"p0"}); k3.sweep(1.0, set()); k3.sweep(1.0, {"p0"})
        across = len(fs & set(k3))
        self.assertEqual(across, len(fs), self.MSG)
        return kept1, gone, kept, gone2, across, len(fs)

    @staticmethod
    def is_applied(orig, mutated, cut):
        return mutated != orig and cut in orig and cut not in mutated

    def test_s14(self):
        r = self.check(cw.KnownForms)   # the control first
        self.assertEqual(r, (8, 8, [8, 8, 8], 8, 8, 8))
        cut = ' and (l[1] if l[0] == "pod" else l[2]) in before}'
        mu = Mutants("test_s3_control_and_mutant")
        bad = mu.mutant(cut, '}')
        applied = self.is_applied(Mutants.SRC, mu.last_src, cut)
        self.assertTrue(applied, "the mutation of S14 was not applied")
        with self.assertRaises(AssertionError) as nm:   # control of the check: a no-op mutation
            self.assertTrue(self.is_applied(Mutants.SRC, Mutants.SRC.replace(cut, cut), cut), "the mutation of S14 was not applied")
        self.assertIn("the mutation of S14 was not applied", str(nm.exception))
        with self.assertRaises(AssertionError) as cm:
            self.check(bad)
        ok = self.MSG in str(cm.exception)
        self.assertTrue(ok)
        print("S14: kept %d of %d after one absence, gone %d of %d after two in a row, kept through absent-present-absent and across a skipped pass; control 1 of 1, mutant 1 of 1 with the text, mutation applied %s" % (r[0], r[5], r[1], r[5], applied))


if __name__ == "__main__":
    unittest.main()
