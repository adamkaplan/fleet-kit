"""Scenarios of ask #1783 (S2, S3, S5-lite, S8, U1): synthetic values only, counts printed, no value printed."""
import inspect, os, sys, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cloud_worker as cw

TOK = "ghs_" + "A1b2C3d4" * 4 + "wxyz"
LOGIN = "eyJ" + "Qr7Zk9" * 20
PW = "pw" + "Xy_-9" * 8
MODEL = '{"provider":"p","key":"sk-' + "k" * 20 + '","base":"http://m.example"}'

import json as _json, random as _random, string as _string
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
        k.sweep(3661, set()); self.assertFalse(forms["pw"] & set(k) - mf)
        self.assertTrue(mf <= set(k), "a model-entry form was dropped")

    def test_s3_running_pod_kept(self):
        k = self.kf(); fs = set(cw.secret_forms(TOK))
        k.add_forms(fs, ("until", 10.0, "p1"))
        self.assertEqual(k.sweep(99999, {"p1"}), 0)
        self.assertNotIn(TOK, cw.scrub("x " + TOK, k), "a form of a running pod was dropped")
        n = k.sweep(99999, set()); self.assertEqual(n, len(fs)); self.assertEqual(len(k), 0)

    def test_s4_scrub_same_for_live(self):
        k = self.kf(); k.add_forms(cw.secret_forms(PW), ("pod", "p1")); k.add_forms(cw.secret_forms(TOK), ("pod", "p2"))
        k.sweep(0, {"p1"})
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
        self.assertLess(peak, 40 * 7 + 12 + 34 + 200, "the known forms grew past the bound")

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
        ts = [threading.Thread(target=f) for f in (adder, sweeper, scr)]
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
        self.assertNotIn(G34, out, "a form of a running pod was dropped"); self.assertNotIn(L34, out, "a form of a running pod was dropped")
        kept = len(gf)
        gone = k.sweep(99999, set())
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
        self.assertEqual(k.sweep(0, {"p1", "p2"}), 0, "a form was dropped with no event")
        self.assertEqual(len(k), total, "a form was dropped with no event")
        for now, pods, kind in ((2400, {"p1", "p2"}, "login"), (3660, {"p1", "p2"}, "git"), (3660, {"p2"}, "pw"), (3660, set(), "hub")):
            if kind in ("login", "git"):
                before = now - 1
                self.assertEqual(k.sweep(before, pods), 0, "a form was dropped before its event (" + kind + ")")
            elif kind == "pw":
                self.assertEqual(k.sweep(now, pods | {"p1"}), 0, "a form was dropped before its event (" + kind + ")")
            else:
                self.assertEqual(k.sweep(now, pods | {"p2"}), 0, "a form was dropped before its event (" + kind + ")")
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
            ("drops a pod's forms while it runs", 'l[1] not in pods)', 'True)', "a form was dropped with no event"),
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


class Mutants(unittest.TestCase):
    """S3 and S2 mutants: a scratch copy of the sweep, text asserted once, mutation asserted applied."""

    SRC = open(cw.__file__).read()

    def mutant(self, old, new):
        self.assertEqual(self.SRC.count(old), 1)
        src = self.SRC.replace(old, new)
        self.assertNotEqual(src, self.SRC)
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
        bad = self.mutant('l[0] == "until" and now >= l[1] and l[2] not in pods', 'l[0] == "until" and now >= l[1]')
        with self.assertRaises(AssertionError) as cm:
            self.check_running(bad)
        self.assertIn("a form of a running pod was dropped", str(cm.exception))

    def check_model(self, cls):
        k = cls(); mf = set(cw.secret_forms(MODEL))
        k.add_forms(mf, None); k.add_forms(mf, ("pod", "p1"))
        k.sweep(1e12, set())
        self.assertTrue(mf <= set(k), "a model-entry form was dropped")
        return len(mf)

    def test_s2_model_control_and_mutant(self):
        print("model forms kept", self.check_model(cw.KnownForms))
        bad = self.mutant("                if lives is None:\n                    continue\n", "                if lives is None:\n                    lives = set()\n")
        with self.assertRaises(AssertionError) as cm:
            self.check_model(bad)
        self.assertIn("a model-entry form was dropped", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
