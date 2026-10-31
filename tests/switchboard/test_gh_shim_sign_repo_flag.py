"""#1639: the signed forms of #1638 are signed also when `-R` or `--repo` stands before the command words (`gh -R o/r issue comment 1 --body x`).

Runs a COPY of the shim in front of a fake real `gh` (the rig of test_gh_shim_sign.py). The forms are read from shims/gh with `ast`, never a pattern.
Closed rule: only a flag at the very start (`-R v`, `--repo v`, `--repo=v`, `-Rv`) is skipped, with its value, so a value that looks like a verb is a value;
a call that is not a signed form, or whose `-R` is inside a field value, is passed through untouched. Known limits: other global flags (`--hostname`).
"""
import json
import unittest

from test_gh_shim_sign import API_EXAMPLES, MARK, SHIM_DIR, Rig, pairs

FLAG_FORMS = {"separate": ["-R", "o/r"], "long": ["--repo", "o/r"], "joined": ["--repo=o/r"], "short joined": ["-Ro/r"]}


def forms(source):
    """Every signed form as an argv tail with its body at the end of the tail: (label, argv, index of the signed text in argv)."""
    out = []
    for group, verb in sorted(pairs(source, "SIGNED_COMMANDS")):
        out.append(("%s %s" % (group, verb), [group, verb, "1", "--body", "x"], -1))
    for group, verb in sorted(pairs(source, "CLOSE_COMMANDS")):
        out.append(("%s %s" % (group, verb), [group, verb, "1", "--comment", "x"], -1))
    for label, _pattern in pairs(source, "SIGNED_API"):
        out.append(("api " + label, ["api", API_EXAMPLES[label], "-f", "body=x"], -1))
    return out


def battery(rig):
    """The failures seen over a short run of every flag form (empty for the real shim): used to judge the mutants."""
    bad = []
    for flag in FLAG_FORMS.values():
        done, calls = rig.run(*flag, "issue", "comment", "1", "--body", "x")
        if len(calls) != 1 or calls[0]["argv"][-1].count(MARK % "alice-bot") != 1:
            bad.append("unsigned with %s" % flag)
    for args in (("-R", "issue", "view", "1"), ("-R", "o/r", "issue", "view", "1"), ("-R", "o/r", "pr", "list"),
                 ("-R", "issue", "comment", "1", "--body", "x"), ("api", "repos/o/r/issues/1", "-f", "title=-R", "-f", "body=-R")):
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"] != list(args):
            bad.append("an unrelated call changed: %s" % " ".join(args))
    return bad


class TestGhShimSignRepoFlag(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def test_every_form_with_every_flag_form_is_signed_and_the_flag_is_kept(self):
        listed = forms(self.source)
        self.assertEqual(len(listed), 11)
        for label, argv, at in listed:
            for flag_name, flag in FLAG_FORMS.items():
                with self.subTest(form=label, flag=flag_name):
                    done, calls = self.rig.run(*flag, *argv)
                    self.assertEqual(len(calls), 1, done.stderr)
                    sent = calls[0]["argv"]
                    self.assertEqual(sent[:len(flag)], flag)
                    self.assertEqual(sent[len(flag):len(flag) + 1], argv[:1])
                    self.assertEqual(sent[at].count(MARK % "alice-bot"), 1)
                    self.assertEqual(json.dumps(sent).count("fleet-switchboard:from="), 1)

    def test_a_body_file_and_stdin_are_signed_behind_a_flag(self):
        path = self.rig.home / "b.md"
        path.write_text("from a file")
        done, calls = self.rig.run("-R", "o/r", "pr", "comment", "1", "--body-file", str(path))
        temp = calls[0]["argv"][-1]
        self.assertIn(MARK % "alice-bot", calls[0]["files"][temp])
        done, calls = self.rig.run("--repo=o/r", "issue", "comment", "1", "-F", "-", stdin="piped")
        self.assertIn(MARK % "alice-bot", calls[0]["stdin"])

    def test_an_unreadable_body_is_refused_behind_a_flag_too(self):
        done, calls = self.rig.run("-R", "o/r", "issue", "comment", "1", "--body-file", "/nonexistent/x.md")
        self.assertEqual((calls, done.returncode), ([], 1))

    def test_a_flag_value_that_looks_like_a_verb_is_a_value_and_never_the_verb(self):
        done, calls = self.rig.run("-R", "issue/comment", "pr", "comment", "1", "--body", "x")
        self.assertIn(MARK % "alice-bot", calls[0]["argv"][-1])
        for args in (("-R", "issue", "comment", "1", "--body", "x"), ("--repo", "issue", "comment", "x", "--body", "x")):
            with self.subTest(args=args):   # here the value is the word `issue`, so the verb `comment` is not a signed pair
                done, calls = self.rig.run(*args)
                self.assertEqual(calls[0]["argv"], list(args))

    def test_unrelated_calls_with_a_flag_are_passed_byte_for_byte(self):
        for args in (("-R", "o/r", "issue", "view", "1"), ("--repo=o/r", "pr", "list"), ("-Ro/r", "issue", "close", "1"),
                     ("-R", "o/r", "issue", "comment", "1", "--editor"), ("-R", "o/r", "issue", "close", "1", "--comment", ""),
                     ("-R", "o/r", "api", "repos/o/r/issues/1", "-f", "body=x"), ("-R",), ("-R", "o/r"),
                     ("api", "repos/o/r/issues/1", "-f", "title=-R"), ("api", "repos/o/r/issues/1/comments", "-X", "GET", "-f", "q=-R"),
                     ("issue", "view", "1", "-R", "o/r")):
            with self.subTest(args=args):
                done, calls = self.rig.run(*args)
                self.assertEqual([c["argv"] for c in calls], [list(args)])

    def test_the_flag_forms_are_signed_as_agent_when_whoami_fails(self):
        for flag in FLAG_FORMS.values():
            done, calls = self.rig.run(*flag, "pr", "close", "1", "-c", "x", no_whoami=True)
            self.assertIn(MARK % "agent", calls[0]["argv"][-1])

    def test_the_battery_passes_for_the_real_shim(self):
        self.assertEqual(battery(self.rig), [])

    def mutant(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old)
        rig = Rig(self.source.replace(old, new, 1))
        self.addCleanup(rig.close)
        return battery(rig)

    def test_mutant_a_flag_form_left_unsigned_is_seen(self):
        self.assertTrue(self.mutant('arg.startswith("--repo=") or (arg.startswith("-R") and len(arg) > 2 and not arg.startswith("--"))', "False"))

    def test_mutant_an_unrelated_call_changed_is_seen(self):
        self.assertTrue(self.mutant("    core = args[lead:]\n", "    core = args[lead:] if lead == 0 else [a for a in args if not a.startswith('-')]\n"))


if __name__ == "__main__":
    unittest.main()
