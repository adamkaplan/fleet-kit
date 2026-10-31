"""#1665: the end hook keeps git's refusal lines, bounded and scrubbed.

The error of a failed git call carries the last EIGHT lines of git's standard error, at most 600 characters, joined on one line with ' | ',
each line scrubbed (key-shaped runs, an address with a credential and control characters become a fixed word), the last line always last.
A FAKE git (a small script, written here) prints the lines the case needs; the hook runs as a program with its git path pointed at the fake.
Mutants (scratch copies of the hook OUTSIDE the repository) run this file as a child through stub_support.start.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "worker", "tests"))
import stub_support  # noqa: E402
import test_token_at_limit as tl  # noqa: E402  (its harness: a running worker, the fake cluster, a scripted end hook)

HOOK_ENV = "CW_TEST_END_HOOK"
INNER_ENV = "CW_TEST_INNER_RUN"
SUMMARY = "error: failed to push some refs to 'https://github.com/acme-org/widget-service.git'"
REJECTED = " ! [rejected]        HEAD -> coder7/wip (non-fast-forward)"
KEY = "ghp_" + "Zk9Wp3Qx7Lm2" * 3
HEXRUN = "0123456789abcdef" * 3
CRED = "https://deploy:hunter2pass@example.invalid/acme.git"
ESC = "\x1b[31m"

# The fake git: every call succeeds and prints nothing, except `log` (one unpushed commit unless FAKE_MODE is nothing) and the push, whose
# standard error is the file FAKE_LINES (FAKE_MODE refuse), or which succeeds (FAKE_MODE ok / nothing).
FAKE_GIT = (
    "#!/usr/bin/env python3\n"
    "import os, sys\n"
    "args = sys.argv[1:]\n"
    "verb = next((a for a in args if a in ('push', 'log')), '')\n"
    "mode = os.environ.get('FAKE_MODE', '')\n"
    "if verb == 'log' and mode != 'nothing':\n"
    "    print('0123456789abcdef0123456789abcdef01234567')\n"
    "if verb == 'push' and mode == 'refuse':\n"
    "    sys.stderr.write(open(os.environ['FAKE_LINES']).read())\n"
    "    sys.exit(1)\n")
DRIVER = (
    "import importlib.util, sys\n"
    "sp = importlib.util.spec_from_file_location('h', sys.argv[1]); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)\n"
    "m.GIT_BIN = sys.argv[2]\n"
    "sys.exit(m.main())\n")


def read_hook():
    with open(os.path.join(LAUNCHER, "end_hook.py")) as f:
        return f.read()


def write_copy(directory, old, new):
    src = read_hook()
    assert src.count(old) == 1, "the mutation did not apply"
    path = os.path.join(directory, "end_hook.py")
    with open(path, "w") as f:
        f.write(src.replace(old, new))
    return path


def last_line_copy(directory):
    """A build that keeps only the last line (the base)."""
    return write_copy(directory, "raise GitFailed(refusal_text(lines), lines[-1])", "raise GitFailed(lines[-1], lines[-1])")


def no_bound_copy(directory):
    """A build with no bound."""
    return write_copy(directory, 'REFUSAL_LINES, REFUSAL_CHARS, REMOVED = 8, 600, "<removed>"', 'REFUSAL_LINES, REFUSAL_CHARS, REMOVED = 1000, 10 ** 6, "<removed>"')


def no_scrub_copy(directory):
    """A build that does not scrub."""
    return write_copy(directory, "take = scrub_lines(lines)[-REFUSAL_LINES:]", "take = list(lines[-REFUSAL_LINES:])")


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = self.tmp.name
        self.fake = os.path.join(self.t, "git")
        with open(self.fake, "w") as f:
            f.write(FAKE_GIT)
        os.chmod(self.fake, 0o755)
        self.work = os.path.join(self.t, "work")
        os.makedirs(os.path.join(self.work, ".git"))
        self.driver = os.path.join(self.t, "driver.py")
        with open(self.driver, "w") as f:
            f.write(DRIVER)

    def hook(self, lines=None, mode="refuse", path=None, **env):
        listing = os.path.join(self.t, "lines")
        with open(listing, "w") as f:
            f.write("".join(l + "\n" for l in (lines or [])))
        e = dict(os.environ, CW_WORKDIR=self.work, CW_WORKER="coder7", FAKE_MODE=mode, FAKE_LINES=listing)
        e.update(env)
        path = path or os.environ.get(HOOK_ENV) or os.path.join(LAUNCHER, "end_hook.py")
        e.pop(HOOK_ENV, None)
        p = subprocess.run([sys.executable, self.driver, path, self.fake], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])

    @staticmethod
    def refusal(n_noise=2, width=0):
        noise = ["remote: noise line %02d %s" % (i, "word " * (width // 5)) for i in range(n_noise)]
        return ["To https://github.com/acme-org/widget-service.git", REJECTED] + noise + [SUMMARY]


def segments(error):
    return error.count(" | ") + 1


class S2Refused(Scratch):
    def test_the_error_holds_the_rejected_line_and_ends_in_the_summary(self):
        code, ans = self.hook(self.refusal(0))
        err = ans["error"]
        self.assertEqual(code, 1)
        self.assertTrue("rejected" in err and "non-fast-forward" in err)
        self.assertTrue(err.endswith(SUMMARY))
        self.assertEqual(err.count("\n"), 0)
        print("S2: error holds rejected and non-fast-forward 1 of 1, ends in the summary 1 of 1, newlines %d" % err.count("\n"))

    def test_a_refusal_with_the_remotes_own_message_keeps_it(self):
        code, ans = self.hook(["remote: Permission to acme-org/widget-service.git denied", "fatal: unable to access the repository", SUMMARY])
        self.assertIn("remote: Permission to acme-org/widget-service.git denied", ans["error"])
        self.assertTrue(ans["error"].endswith(SUMMARY))


class S3Bound(Scratch):
    def test_thirty_lines_are_cut_to_eight_lines(self):
        code, ans = self.hook(["remote: line %02d of the answer with some words in it" % i for i in range(29)] + [SUMMARY])
        err = ans["error"]
        self.assertTrue(segments(err) <= 8 and len(err) <= 600 and err.endswith(SUMMARY))
        self.assertEqual(segments(err), 8)
        print("S3: lines %d (at most 8), characters %d (at most 600)" % (segments(err), len(err)))

    def test_long_lines_are_cut_to_the_character_bound(self):
        code, ans = self.hook(["remote: " + ("word " * 40) + str(i) for i in range(29)] + [SUMMARY])
        self.assertTrue(len(ans["error"]) <= 600 and ans["error"].endswith(SUMMARY))


class S4Scrubbed(Scratch):
    def test_no_key_no_credential_address_no_control_character_is_kept(self):
        lines = ["remote: %s here %s %s %s%s" % (KEY, HEXRUN, CRED, ESC, "text") for _ in range(7)] + [SUMMARY + " " + KEY]
        code, ans = self.hook(lines)
        err = ans["error"]
        keys = err.count(KEY) + err.count(HEXRUN) + err.count("Zk9Wp3Qx7Lm2")
        creds = err.count("hunter2pass") + err.count("deploy:")
        escapes = err.count("\x1b")
        self.assertEqual((keys, creds, escapes), (0, 0, 0))
        self.assertEqual(code, 1)
        print("S4: key-shaped runs %d, credential addresses %d, escape characters %d" % (keys, creds, escapes))


class S5Unchanged(Scratch):
    def test_refresh_needed_and_the_other_answers(self):
        code, ans = self.hook(["fatal: Authentication failed for 'https://github.com/acme-org/widget-service.git/'"])
        self.assertEqual((code, ans.get("refresh"), ans["error"]), (1, "needed", "the git token had ended"))
        base = last_line_copy(self.t)
        same = 0
        for mode in ("ok", "nothing"):
            a, b = self.hook(mode=mode), self.hook(mode=mode, path=base)
            self.assertEqual(a[0], 0)
            same += a == b
        self.assertEqual(same, 2)
        print("S5: refresh needed answers 1, other answers equal the base %d of 2" % same)


class S6Launcher(tl.Stop):
    def test_the_audit_line_and_the_run_record_show_the_longer_error_scrubbed_again(self):
        s = Scratch("setUp")
        s.t = self.tmp.name
        s.work = os.path.join(s.t, "w")
        os.makedirs(os.path.join(s.work, ".git"))
        s.fake, s.driver = os.path.join(s.t, "git"), os.path.join(s.t, "driver.py")
        for p, text in ((s.fake, FAKE_GIT), (s.driver, DRIVER)):
            with open(p, "w") as f:
                f.write(text)
        os.chmod(s.fake, 0o755)
        code, ans = s.hook(["To https://github.com/acme-org/widget-service.git", REJECTED, SUMMARY])
        planted = "ghp_" + "Qx7Lm2Zk9Wp3" * 3
        ans = dict(ans, error=ans["error"] + " | later " + planted)   # a key-shaped run the hook missed: the launcher scrubs again
        res, error = self.run_stop([ans])
        self.assertIsNotNone(error)
        reason = self.stop_audit()["reason"]
        record = json.loads(self.record_text())["end_hook"]["error"]
        shown = int("non-fast-forward" in reason and "rejected" in reason) , int("non-fast-forward" in record and "rejected" in record)
        planted_count = self.everywhere().count(planted) + self.everywhere().count("Qx7Lm2Zk9Wp3Qx7Lm2Zk9Wp3")
        self.assertEqual((shown, planted_count), ((1, 1), 0))
        print("S6: audit lines with the longer error 1, run records 1, planted text occurrences %d" % planted_count)


class U2Equal(Scratch):
    def test_success_nothing_to_push_and_a_non_git_error_answer_as_the_base(self):
        base = last_line_copy(self.t)
        same = 0
        for mode, env in (("ok", {}), ("nothing", {}), ("ok", {"CW_WORKER": ""})):
            same += self.hook(mode=mode, **env) == self.hook(mode=mode, path=base, **env)
        self.assertEqual(same, 3)
        print("U2: answers equal the base %d of 3" % same)


class U3Bound(Scratch):
    def test_eight_lines_of_eighty_characters_and_the_scrubbed_answer(self):
        eighty = ["remote: " + "word " * 14 + "ab" for _ in range(7)] + ["error: " + "word " * 14 + "abc"]
        self.assertTrue(all(len(l) == 80 for l in eighty))
        answers = [self.hook(eighty)[1]["error"], self.hook(["a %s b%s c" % (KEY, ESC)] * 8)[1]["error"]]
        controls = sum(len(re.findall(r"[\x00-\x1f\x7f]", a)) for a in answers)
        self.assertEqual(controls, 0)
        self.assertTrue(all(segments(a) <= 8 and len(a) <= 600 for a in answers))
        print("U3: control characters %d, lines %d, characters %d" % (controls, max(segments(a) for a in answers), max(len(a) for a in answers)))


class S7Mutants(Scratch):
    TARGETS = ((last_line_copy, "S2Refused"), (no_bound_copy, "S3Bound"), (no_scrub_copy, "S4Scrubbed"))

    @unittest.skipIf(os.environ.get(INNER_ENV), "inside a mutant's child run")
    def test_each_mutant_fails_its_test(self):
        caught = applied = 0
        for copy, cls in self.TARGETS:
            scratch = tempfile.mkdtemp(prefix="cw-mutant-")   # outside the repository
            self.addCleanup(shutil.rmtree, scratch, True)
            path = copy(scratch)
            applied += os.path.isfile(path)
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "-q", "test_end_hook_refusal_lines." + cls], cwd=HERE,
                                   env=dict(os.environ, **{HOOK_ENV: path, INNER_ENV: "1"}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out, _ = p.communicate(timeout=90)
            self.assertEqual(p.returncode, 1, out)
            caught += 1
        self.assertEqual((caught, applied), (3, 3))
        print("S7: mutants caught %d of 3, mutations applied %d of 3" % (caught, applied))


class S8Docs(Scratch):
    def test_the_documentation_states_the_bound(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = f.read().lower()
        found = sum(p in text for p in ("eight lines", "600 characters", "a new image"))
        self.assertEqual(found, 3)
        print("S8: phrases found %d of 3" % found)


class S9Prestop(Scratch):
    """The pre-stop line keeps git's own LAST line (cut to 80 characters, a URL replaced), none of the earlier refusal lines."""
    PRESTOP_DRIVER = (
        "import importlib.util, sys\n"
        "sp = importlib.util.spec_from_file_location('h', sys.argv[1]); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)\n"
        "m.GIT_BIN = sys.argv[2]\n"
        "m.HELPER_DIR = sys.argv[3]\n"
        "sys.argv = [sys.argv[0], '--prestop']\n"
        "sys.exit(m.main())\n")
    OLD = 'msg = " ".join(str(getattr(e, "summary", e)).split())'
    NEW = 'msg = " ".join(str(e).split())'

    def prestop_line(self, lines, path=None):
        listing = os.path.join(self.t, "lines")
        with open(listing, "w") as f:
            f.write("".join(l + "\n" for l in lines))
        # the pre-stop hook scrubs the environment: this fake reads its lines from a file beside it, not from FAKE_LINES
        fake = os.path.join(self.t, "git-prestop")
        with open(fake, "w") as f:
            f.write(FAKE_GIT.replace("os.environ.get('FAKE_MODE', '')", "'refuse'").replace("os.environ['FAKE_LINES']", "%r" % listing))
        os.chmod(fake, 0o755)
        helpers = os.path.join(self.t, "helpers")
        os.makedirs(helpers, exist_ok=True)
        helper = os.path.join(helpers, "git-credential-cw")
        with open(helper, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(helper, 0o755)
        driver = os.path.join(self.t, "prestop_driver.py")
        with open(driver, "w") as f:
            f.write(self.PRESTOP_DRIVER)
        e = dict(os.environ, CW_WORKDIR=self.work, CW_WORKER="coder7", CW_REPO="acme-org/widget-service", FAKE_MODE="refuse", FAKE_LINES=listing)
        path = path or os.environ.get(HOOK_ENV) or os.path.join(LAUNCHER, "end_hook.py")
        e.pop(HOOK_ENV, None)
        p = subprocess.run([sys.executable, driver, path, fake, helpers], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        out = [l for l in p.stdout.splitlines() if l.startswith("cw-prestop: ")]
        return p.returncode, out

    def test_the_prestop_line_keeps_the_last_line_only(self):
        lines = self.refusal()
        rc, out = self.prestop_line(lines)
        self.assertEqual((rc, len(out)), (0, 1), out)
        last = " ".join(SUMMARY.split())
        last = re.sub(r"https?://\S+", "<url>", last)[:80]
        self.assertIn("not pushed (%s)" % last, out[0])
        earlier = sum(l.strip() in out[0] for l in lines[:-1])
        self.assertEqual(earlier, 0)
        self.assertNotIn("non-fast-forward", out[0])
        # the same refusal through the end hook answers the bounded lines
        error = self.hook(lines)[1]["error"]
        self.assertIn("non-fast-forward", error)
        self.assertTrue(error.endswith(SUMMARY))
        print("S9 part 1: prestop line equals git last line 1 of 1, earlier refusal lines in it %d" % earlier)

    @unittest.skipIf(os.environ.get(INNER_ENV), "inside a mutant's child run")
    def test_a_prestop_that_reads_the_whole_text_fails_it(self):
        scratch = tempfile.mkdtemp(prefix="cw-mutant-")   # outside the repository
        self.addCleanup(shutil.rmtree, scratch, True)
        path = write_copy(scratch, self.OLD, self.NEW)
        applied = os.path.isfile(path)
        p = stub_support.start(self, [sys.executable, "-m", "unittest", "-q", "test_end_hook_refusal_lines.S9Prestop.test_the_prestop_line_keeps_the_last_line_only"],
                               cwd=HERE, env=dict(os.environ, **{HOOK_ENV: path, INNER_ENV: "1"}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=90)
        self.assertEqual(p.returncode, 1, out)
        self.assertTrue(applied)
        print("S9: prestop line equals git last line 1 of 1, earlier refusal lines in it 0, mutant fails 1 of 1, mutation applied %s" % applied)


# (value planted, the part that must not survive): one class per line (#1665, constraint PC-3)
PLANTED = (
    ("key broken by a slash", "Qm8xT2vLp4Zr/Wn6Yb3Kd9Hc5Fa", "Wn6Yb3Kd9Hc5Fa"),
    ("key broken by a dot", "Hb7yR4nKs2Vc.Xt9Lw3Pq6Jd8Mz", "Xt9Lw3Pq6Jd8Mz"),
    ("20-character AKIA key", "AKIA" + "IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    ("sk- key", "sk-a1b2c3d4", "a1b2c3d4"),
    ("xox- key", "xoxb-12345-abcde", "12345-abcde"),
    ("gh prefix with one character", "ghp_Z", "ghp_Z"),
    ("JWT", "eyJhbGci.0123456789ab", "0123456789ab"),
    ("fullwidth gh token", "\uff47\uff48\uff50\uff3fAb3dE5", "Ab3dE5"),
)
LOWER_PATH = "/work/repo/cloud/launcher/tests/test_x.py"
MIXED_PATH = "src/module_one/handler_v2/a.py"
MIXED_REF = "refs/heads/Feature/login-page-x"
SHORT_WORD = "Qm8xT2vLp4Zr6Wn3Yb9K"
SEP = "! [hint] see the remote"   # a line that starts outside the run class, so that the next line is not joined into a planted run
HALF_A, HALF_B = "ghp_Ab3dE5fG7hJ9", "Kk2Lm4Np6Qr8St0Uv1Wx9"


class S10Classes(Scratch):
    def test_no_planted_value_of_any_class_is_kept(self):
        kept = planted = 0
        for name, value, marker in PLANTED:
            code, ans = self.hook(["remote: " + value, SEP, SUMMARY])
            planted += 1
            self.assertEqual(code, 1)
            self.assertTrue(ans["error"].endswith(SUMMARY) and "remote: " in ans["error"], name)
            kept += (marker in ans["error"]) + (value in ans["error"] and marker not in value)
        self.assertEqual((kept, planted), (0, 8))
        print("S10: planted values in the error %d of 8, classes planted %d of 8" % (kept, planted))


GLUED = (("xsk- key glued to a letter", "x", "sk-a1b2c3d4"), ("KEYsk- key glued to upper-case letters", "KEY", "sk-a1b2c3d4"))


class S10bGlued(Scratch):
    def test_a_key_glued_to_letters_is_removed(self):
        removed = 0
        for name, glue, key in GLUED:
            code, ans = self.hook(["remote: " + glue + key, SEP, SUMMARY])
            self.assertTrue(ans["error"].endswith(SUMMARY) and "remote: " in ans["error"], name)
            removed += key not in ans["error"] and key[3:] not in ans["error"]
        self.assertEqual(removed, 2)
        print("S10b: glued keys removed %d of 2" % removed)


class S11Split(Scratch):
    def test_a_token_split_over_two_lines_replaces_both_lines(self):
        code, ans = self.hook(["remote: " + HALF_A, HALF_B + " more", SUMMARY])
        err = ans["error"]
        halves = (HALF_A[4:] in err) + (HALF_B in err)
        self.assertEqual(halves, 0)
        self.assertTrue(err and err.endswith(SUMMARY))
        print("S11: halves in the error %d of 2, errors not empty %d of 1" % (halves, bool(err)))


class S12bKeyBeforeSummary(Scratch):
    def test_a_key_that_ends_the_line_before_the_summary_keeps_the_earlier_reason(self):
        code, ans = self.hook(["remote: rejected: protected branch", "remote: token " + KEY, SUMMARY])
        err = ans["error"]
        halves = (KEY[:8] in err) + (KEY[-8:] in err)
        ends = err.endswith("<removed>")
        self.assertEqual((code, ends, halves), (1, True, 0))
        self.assertIn("rejected", err)
        print("S12b: key before the summary: ends in the fixed word %d of 1, halves in the error %d of 2, earlier reason line kept %d of 1" % (ends, halves, "rejected" in err))


class S12Limits(Scratch):
    def test_long_names_and_short_words_and_the_readme(self):
        self.assertEqual((len(LOWER_PATH), len(MIXED_PATH), len(MIXED_REF), len(SHORT_WORD)), (41, 30, 31, 20))
        kept = replaced = short = 0
        kept += LOWER_PATH in self.hook(["remote: " + LOWER_PATH, SEP, SUMMARY])[1]["error"]
        for name in (MIXED_PATH, MIXED_REF):
            replaced += name not in self.hook(["remote: " + name, SEP, SUMMARY])[1]["error"]
        short += SHORT_WORD in self.hook(["remote: " + SHORT_WORD, SEP, SUMMARY])[1]["error"]
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = f.read().lower()
        phrases = sum(x in text for x in ("a prefixed token", "24 or more characters", "an address with a credential", "a control character",
                                          "a key of 23 characters or fewer", "made only of lower-case letters"))
        self.assertEqual((kept, replaced, short, phrases), (1, 2, 1, 6))
        print("S12: lower-case paths kept %d of 1, mixed long names replaced %d of 2, short words kept %d of 1, README phrases %d of 6" % (kept, replaced, short, phrases))


def _mut(old, new):
    return lambda directory: write_copy(directory, old, new)


class S13ClassMutants(Scratch):
    TARGETS = (
        (_mut("import unicodedata\n", "class unicodedata:\n    normalize = staticmethod(lambda form, text: text)\n"), "S10Classes"),
        (_mut("[A-Za-z0-9+/_=.-]{24,}", "[A-Za-z0-9+_=-]{24,}"), "S10Classes"),
        (_mut("if _MIXED_RE.search(m.group())]", "if True]"), "S12Limits"),
        (_mut(r"|(?:sk-|xox[a-z]-)[A-Za-z0-9_-]+|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]{10,}", ""), "S10Classes"),
        (_mut("if len(touched) > 1:", "if False:"), "S11Split"),
    )

    @unittest.skipIf(os.environ.get(INNER_ENV), "inside a mutant's child run")
    def test_each_mutant_fails_the_test_of_its_class(self):
        caught = applied = 0
        for copy, cls in self.TARGETS:
            scratch = tempfile.mkdtemp(prefix="cw-mutant-")   # outside the repository
            self.addCleanup(shutil.rmtree, scratch, True)
            path = copy(scratch)
            applied += os.path.isfile(path)
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "-q", "test_end_hook_refusal_lines." + cls], cwd=HERE,
                                   env=dict(os.environ, **{HOOK_ENV: path, INNER_ENV: "1"}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out, _ = p.communicate(timeout=90)
            self.assertEqual(p.returncode, 1, out)
            self.assertIn("FAILED (failures=1)", out)
            caught += 1
        self.assertEqual((caught, applied), (5, 5))
        print("S13: mutants failing their test %d of 5, mutations applied %d of 5" % (caught, applied))

    @unittest.skipIf(os.environ.get(INNER_ENV), "inside a mutant's child run")
    def test_the_boundary_mutant_fails_the_glued_key_test(self):
        scratch = tempfile.mkdtemp(prefix="cw-mutant-")   # outside the repository
        self.addCleanup(shutil.rmtree, scratch, True)
        path = _mut("|(?:sk-|xox[a-z]-)", "|(?<![A-Za-z0-9])(?:sk-|xox[a-z]-)")(scratch)
        applied = os.path.isfile(path)
        p = stub_support.start(self, [sys.executable, "-m", "unittest", "-q", "test_end_hook_refusal_lines.S10bGlued"], cwd=HERE,
                               env=dict(os.environ, **{HOOK_ENV: path, INNER_ENV: "1"}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=90)
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("FAILED (failures=1)", out)
        self.assertTrue(applied)
        print("S13b: boundary mutant fails 1 of 1, mutation applied %s" % applied)


class S10c(Scratch):
    def test_password_with_an_at_and_format_characters_in_a_key(self):
        pw = "https://user:pa@ss@host/x"
        code, ans = self.hook(["remote: " + pw, SEP, SUMMARY])
        err = ans["error"]
        self.assertTrue(err.endswith(SUMMARY))
        pw_removed = int("ss@host" not in err and "pa@" not in err and "https://<removed>@host/x" in err)
        tail = "Zk9Wp3Qx7Lm2Yt4Rv"[:16]
        keys = ("gh" + chr(0x200b) + "p_" + tail, "AKIA" + chr(0x200b) + "ABCD1234EFGH5678", "gh" + chr(0xad) + "p_" + tail)
        removed = 0
        for k in keys:
            code, ans = self.hook(["remote: " + k, SEP, SUMMARY])
            bare = k.replace(chr(0x200b), "").replace(chr(0xad), "")
            self.assertTrue(ans["error"].endswith(SUMMARY))
            removed += k not in ans["error"] and bare not in ans["error"] and bare[4:] not in ans["error"]
        kept = 0
        for a in ("https://github.com/acme/widget.git", "https://host/path@ref"):
            kept += a in self.hook(["remote: " + a, SEP, SUMMARY])[1]["error"]
        self.assertEqual((pw_removed, removed, kept), (1, 3, 2))
        print("S10c: password address removed %d of 1, format-character keys removed %d of 3, ordinary addresses kept %d of 2" % (pw_removed, removed, kept))


def _unmutated(directory):
    path = os.path.join(directory, "end_hook.py")
    shutil.copy(os.path.join(LAUNCHER, "end_hook.py"), path)
    return path


class S13cMutants(Scratch):
    TARGETS = (
        (_mut(r"(?<=://)[^/\s]*@", r"(?<=://)[^/\s@]*@"), "S10c"),
        (_mut("if _category(c) != \"Cf\")", "if True)"), "S10c"),
    )

    def _child(self, path):
        p = stub_support.start(self, [sys.executable, "-m", "unittest", "-q", "test_end_hook_refusal_lines.S10c"], cwd=HERE,
                               env=dict(os.environ, **{HOOK_ENV: path, INNER_ENV: "1"}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=90)
        return p.returncode, out

    @unittest.skipIf(os.environ.get(INNER_ENV), "inside a mutant's child run")
    def test_mutants_fail_and_controls_pass(self):
        caught = applied = passed = 0
        for copy, cls in self.TARGETS:
            scratch = tempfile.mkdtemp(prefix="cw-mutant-")   # outside the repository
            self.addCleanup(shutil.rmtree, scratch, True)
            path = copy(scratch)
            applied += os.path.isfile(path)
            rc, out = self._child(path)
            self.assertEqual(rc, 1, out)
            self.assertIn("FAILED (failures=1)", out)
            caught += 1
            control = _unmutated(tempfile.mkdtemp(prefix="cw-control-"))
            self.addCleanup(shutil.rmtree, os.path.dirname(control), True)
            rc, out = self._child(control)
            self.assertEqual(rc, 0, out)
            passed += 1
        self.assertEqual((caught, applied, passed), (2, 2, 2))
        print("S13c: mutants failing their test %d of 2, mutations applied %d of 2" % (caught, applied))
        print("S13c control: unmutated copies pass %d of 2" % passed)


if __name__ == "__main__":
    unittest.main()
