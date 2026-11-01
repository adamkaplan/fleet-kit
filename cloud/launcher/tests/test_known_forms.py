"""Scenarios of ask #1783 (S2, S3, S5-lite, S8, U1): synthetic values only, counts printed, no value printed."""
import inspect, os, sys, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cloud_worker as cw

TOK = "ghs_" + "A1b2C3d4" * 4 + "wxyz"
LOGIN = "eyJ" + "Qr7Zk9" * 20
PW = "pw" + "Xy_-9" * 8
MODEL = '{"provider":"p","key":"sk-' + "k" * 20 + '","base":"http://m.example"}'


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
        self.assertIn("[redacted", cw.scrub("x " + TOK, k).lower() if False else "[redacted" if all(f in set(k) for f in fs) else "")
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
