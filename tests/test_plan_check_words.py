"""#1552: the `review-for-code` defect names the matched keywords on its own line. Run: python3 -m unittest tests.test_plan_check_words"""
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import plan_check  # noqa: E402

BASE = "14b26650"   # the integration head before this change: the "before" of S4
DW = "(1) a (2) b"
TAIL = " (matched: "


def plan(scenarios):
    out = ["## Intent", "x", "Done when: (1) a (2) b", "", "```yaml", "scenarios:"]
    for s in scenarios:
        out.append("  - id: %s" % s["id"])
        for k in ("covers", "text", "proof", "pass", "discriminates", "evaluator"):
            out.append("    %s: %s" % (k, s.get(k, {"covers": "DW-1", "proof": "review", "evaluator": "reviewer", "discriminates": "a build that leaves the header blank shows an empty header",
                                                    "pass": "the verdict quotes 3 words from the text it read"}.get(k, ""))))
        for k in ("claim", "slice"):
            if k in s:
                out.append("    %s: %s" % (k, s[k]))
    out.append("```")
    return "\n".join(out) + "\n"


def review(text, pass_="the verdict quotes 3 words from the text it read", sid="S1"):
    return {"id": sid, "text": text, "pass": pass_}


def lines(text):
    got = plan_check.check(text, DW)
    return got, plan_check.render(got, "t")


def words_of(got):
    d = [d for d in got["defects"] if d["code"] == "review-for-code"]
    found = [re.search(r"\(matched: (.*)\)$", x["message"]) for x in d]
    return [m.group(1) if m else None for m in found]


class Words(unittest.TestCase):
    def test_s2_names_exactly_the_matched_words_not_a_marker(self):   # S2
        text = plan([review("the command returns the text and never changes it", "the reviewer refuses zzmarkerq")])
        got, out = lines(text)
        self.assertEqual(words_of(got), ["returns, never, refuses"])
        mine = [l for l in out if "reviewer's read of text" in l]
        self.assertEqual(len(mine), 1)
        self.assertNotIn("zzmarkerq", mine[0])
        self.assertTrue(mine[0].endswith("use test (matched: returns, never, refuses)"))
        self.assertEqual(len(out), 14)   # 0 extra lines: the same count as before (S4 compares with the base too)

    def test_s3_each_once_in_order_at_most_8(self):   # S3
        a = review("it returns and never", sid="S1")
        b = review("it prints and blocks", sid="S2")
        ten = "returns raises throws function endpoint stdout stderr prints logs exits"
        c = review("returns returns returns returns " + ten, sid="S3")
        got, out = lines(plan([a, b, c]))
        self.assertEqual(words_of(got), ["returns, never", "prints, blocks",
                                         "returns, raises, throws, function, endpoint, stdout, stderr, prints and 2 more"])
        self.assertEqual(sum(1 for l in out if "matched:" in l), 3)

    def test_u1_secret_and_prose_not_echoed(self):   # U1
        secret = "ghp_" + "A1b2C3d4" * 4
        got, out = lines(plan([review("a prose sentence about nothing returns " + secret, "the verdict quotes 3 words from sentence " + secret)]))
        mine = [d["message"] for d in got["defects"] if d["code"] == "review-for-code"]
        self.assertEqual(len(mine), 1)
        self.assertNotIn(secret, mine[0])
        self.assertNotIn("prose", mine[0])
        self.assertTrue(mine[0].endswith("(matched: returns)"))
        self.assertEqual(plan_check.REVIEW_CODE_RE.pattern.count("|"), 56)

    def test_s5_mutants_are_caught(self):   # S5: the S2 assertion fails for each wrong build
        text = plan([review("the command returns the text and never changes it", "the reviewer refuses zzmarkerq")])
        real = plan_check.check(text, DW)
        for name, mutant in (("no words", lambda m: m.split(TAIL)[0]),
                             ("a word the scenario lacks", lambda m: m.replace("(matched: ", "(matched: prints, ")),
                             ("the marker word", lambda m: m.replace("refuses)", "refuses, zzmarkerq)"))):
            bad = json.loads(json.dumps(real))
            for d in bad["defects"]:
                if d["code"] == "review-for-code":
                    d["message"] = mutant(d["message"])
            with self.assertRaises(AssertionError, msg=name):
                self.assertEqual(words_of(bad), ["returns, never, refuses"])


def old_module():
    try:
        src = subprocess.run(["git", "show", "%s:bin/plan_check.py" % BASE], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except Exception:
        return None
    spec = importlib.util.spec_from_loader("plan_check_old", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src, "plan_check_old", "exec"), mod.__dict__)
    return mod


def mk(sid="S1", **kw):
    d = {"id": sid}
    d.update(kw)
    return d


# S4: the corpus is a TABLE of 20 plans (name, plan text); it covers every defect code the checker can raise plus passing plans.
GOOD = dict(text="Given the chat window, when the reply arrives, then the header shows the agent state", pass_="the header shows the word Ready after the reply")
CORPUS = [
    ("pass-1", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("pass-2", plan([mk(text=GOOD["text"], covers="DW-2", proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("pass-3", plan([mk(text="Given a form, when it opens, then the title shows Intake", proof="review", **{"pass": "the verdict quotes the title Intake from the text it read"})])),
    ("review-1", plan([review("the command returns exit status 0")])),
    ("review-2", plan([review("it never sends and always logs", "the daemon refuses")])),
    ("review-3", plan([review("a" * 0 + "returns raises throws function endpoint stdout stderr prints logs exits")])),
    ("bad-covers", plan([mk(text=GOOD["text"], covers="X-9", proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("covers-unknown-clause", plan([mk(text=GOOD["text"], covers="DW-9", proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("bad-id", plan([mk(sid="Q1", text=GOOD["text"], proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("duplicate-id", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]}), mk(text=GOOD["text"], proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("short-text", plan([mk(text="short", proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("bad-proof", plan([mk(text=GOOD["text"], proof="vibes", evaluator="reviewer", **{"pass": GOOD["pass_"]})])),
    ("bad-evaluator", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="wizard", **{"pass": GOOD["pass_"]})])),
    ("evaluator-mismatch", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="ci", **{"pass": GOOD["pass_"]})])),
    ("bad-claim", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="reviewer", claim="moon", **{"pass": GOOD["pass_"]})])),
    ("vague-pass", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="reviewer", **{"pass": "it works"})])),
    ("no-discriminator", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="reviewer", discriminates="n/a", **{"pass": GOOD["pass_"]})])),
    ("evaluator-alias", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="adam", **{"pass": GOOD["pass_"]})])),
    ("live-no-claim", plan([mk(text=GOOD["text"], proof="live", evaluator="tester", **{"pass": GOOD["pass_"]})])),
    ("unknown-key", plan([mk(text=GOOD["text"], proof="screenshot", evaluator="reviewer", **{"pass": GOOD["pass_"]})]).replace("    evaluator: reviewer", "    evaluator: reviewer\n    bogus: 1")),
]


class Corpus(unittest.TestCase):
    def test_s4_u2_only_the_review_message_changes(self):   # S4 and U2
        old = old_module()
        if old is None:
            self.skipTest("the base commit is not in this checkout")
        self.assertEqual(len(CORPUS), 20)
        codes_seen = set()
        for name, text in CORPUS:
            a, b = old.check(text, DW), plan_check.check(text, DW)
            self.assertEqual((a["result"], a["plan_hash"], a["scenario_hash"], [d["code"] for d in a["defects"]]),
                             (b["result"], b["plan_hash"], b["scenario_hash"], [d["code"] for d in b["defects"]]), name)
            self.assertEqual(a["warnings"], b["warnings"], name)
            for k in set(a) - {"defects"}:
                self.assertEqual(a[k], b[k], name)
            for x, y in zip(a["defects"], b["defects"]):
                codes_seen.add(x["code"])
                if x["code"] == "review-for-code":
                    self.assertTrue(y["message"].startswith(x["message"] + TAIL), name)
                    self.assertEqual({k: v for k, v in x.items() if k != "message"}, {k: v for k, v in y.items() if k != "message"})
                else:
                    self.assertEqual(x, y, name)
            ra, rb = old.render(a, "t"), plan_check.render(b, "t")
            self.assertEqual(len(ra), len(rb), name)
            for x, y in zip(ra, rb):
                self.assertTrue(y == x or (y.startswith(x + TAIL) and "review-for-code" in "".join(ra)), (name, x, y))
        self.assertTrue({"review-for-code", "bad-covers", "covers-unknown-clause", "bad-id", "duplicate-id", "short-text", "bad-proof", "bad-evaluator",
                         "evaluator-mismatch", "bad-claim", "vague-pass", "no-discriminator", "unknown-key"} <= codes_seen, codes_seen)


if __name__ == "__main__":
    unittest.main()
