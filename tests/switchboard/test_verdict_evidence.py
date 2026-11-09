"""#2054: a failed verdict says why where a lane reads it: in the `gates` missing line and in the `plan.verdict` audit line.
build(Base) makes the class on the LedgerBase of bin/test-switchboard (the ledger world lives there)."""
import os
from unittest import mock

OLD, NEW = "OLDER-REASON " + "o" * 200, "NEWEST-REASON " + "n" * 200
SECRET = "ghp_" + "A1b2C3d4E5" * 4


def build(Base):
    class TestVerdictEvidence(Base):
        def fail(self, evidence, who="tst", sid="S2"):
            return self.run_as(who, "verdict", sid, "--pr", self.PR, "--as", "tester", "--result", "fail", "--evidence", evidence)

        def lines(self):
            _, report, _ = self.gates()
            return [m for m in self.row(report, "G1")["missing"] if "verdict says fail" in m] if report else []

        def test_S2_gates_prints_the_evidence_of_the_newest_fail_who_and_when(self):
            self.assertEqual(self.prove("coder", "S2")[0], 0)
            self.assertEqual(self.fail(OLD)[0], 0)
            self.assertEqual(self.verdict("tst", "S2", "tester")[0], 0)   # a same-result record is not repeated: a pass between makes the next fail a new entry
            self.assertEqual(self.fail(NEW)[0], 0)
            lines = self.lines()
            newest = [l for l in lines if NEW[:160] in l and "tst" in l]
            self.assertEqual(len(newest), 1, "the evidence of the newest verdict: %r" % lines)
            self.assertEqual([l for l in lines if OLD[:30] in l], [])
            self.assertNotIn(NEW[:161], newest[0])
            self.assertIn("only the tester may record a new one", newest[0])
            ts = self.ledger()[-1]["ts"]
            self.assertTrue(ts, "the verdict carries a time")
            self.assertIn("[by tst at %s]" % ts, newest[0], "who and when: the raw ts of the verdict is on the line")

        LONG = "saw " + SECRET + " fail " + "x" * 300

        def check_S3(self):
            self.assertEqual(self.prove("coder", "S2")[0], 0)
            self.assertEqual(self.fail(self.LONG)[0], 0)
            why = "the audit line carries the redacted clip"
            details = [e["detail"] for e in self.world.audit() if e["event"] == "plan.verdict"]
            self.assertEqual(len(details), 1, why)
            self.assertTrue(details[0].get("evidence", "").startswith("saw "), "%s: %r" % (why, details[0]))
            self.assertNotIn(SECRET, str(self.world.audit()), why)
            full = self.ledger()[-1]["evidence"]
            self.assertTrue(full.startswith(details[0]["evidence"]), why)   # the audit line carries a clip of the ledger's text
            self.assertLess(len(details[0]["evidence"]), len(full), why)    # and it is a clip, not the whole text

        def check_S4(self):
            self.assertEqual(self.prove("coder", "S2")[0], 0)
            before = len(self.ledger())
            refusals = 0
            for ev in ("", "   "):
                code, _, err = self.fail(ev)
                self.assertNotEqual(code, 0, "a blank evidence is refused")
                refusals += err.count("--evidence is required")
            self.assertEqual(refusals, 2, "a blank evidence is refused")
            self.assertEqual(len(self.ledger()), before, "a blank evidence is refused")

        def test_S3_the_audit_line_carries_the_redacted_clip(self):
            self.check_S3()

        def test_S4_an_empty_or_blank_evidence_stays_refused(self):
            self.check_S4()

        def mutated(self, old, new):
            """A COPY of bin/fleet-switchboard with one textual replacement, loaded as its own module; the caller's run_as uses it."""
            import importlib.machinery
            import importlib.util
            import pathlib
            import sys
            import tempfile
            src = pathlib.Path(__file__).resolve().parents[2] / "bin" / "fleet-switchboard"
            text = src.read_text()
            self.assertEqual(text.count(old), 1, "the line to mutate is not found exactly once: %r" % old)
            tmp = tempfile.mkdtemp()
            self.addCleanup(__import__("shutil").rmtree, tmp, True)
            for sibling in src.parent.iterdir():   # the copy finds its sibling modules beside it
                if sibling.name != src.name:
                    os.symlink(str(sibling), os.path.join(tmp, sibling.name))
            path = os.path.join(tmp, src.name)
            with open(path, "w") as handle:
                handle.write(text.replace(old, new))
            loader = importlib.machinery.SourceFileLoader("sb_mutant", path)
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("sb_mutant", loader))
            sys.modules["sb_mutant"] = module
            self.addCleanup(sys.modules.pop, "sb_mutant", None)
            loader.exec_module(module)
            return module

        def caught(self, check, anchor, old, new):
            module = self.mutated(old, new)
            with mock.patch.dict(Base.run_as.__globals__, {"sb": module}):
                with self.assertRaises(AssertionError) as ctx:
                    check()
            self.assertIn(anchor, str(ctx.exception))

        def test_S3_mutants_are_caught(self):
            anchor = "the audit line carries the redacted clip"
            self.check_S3()   # the unmutated control first
            clip = '"chain": one["hash"], "evidence": entry["evidence"][:160]}'
            self.caught(self.check_S3, anchor, clip, '"chain": one["hash"], "evidence": entry["evidence"]}')       # no clip
            self.caught(self.check_S3, anchor, clip, '"chain": one["hash"], "evidence": args.evidence[:160]}')     # the raw text

        def test_S4_mutant_is_caught(self):
            anchor = "a blank evidence is refused"
            self.check_S4()   # the unmutated control first
            old = "    text = plan_ledger.bounded_text(redact_command_text(args.evidence), plan_ledger.EVIDENCE_LIMIT, \"evidence\")\n    if not text:\n"
            self.caught(self.check_S4, anchor, old, old.replace("if not text:", "if not args.evidence:"))

    return TestVerdictEvidence
