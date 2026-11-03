"""#2054: a failed verdict says why where a lane reads it: in the `gates` missing line and in the `plan.verdict` audit line.
build(Base) makes the class on the LedgerBase of bin/test-switchboard (the ledger world lives there)."""
OLD, NEW = "OLDER-REASON " + "o" * 200, "NEWEST-REASON " + "n" * 200
SECRET = "ghp_" + "A1b2C3d4E5" * 4


def build(Base):
    class TestVerdictEvidence(Base):
        def fail(self, evidence, who="tst", sid="S2"):
            return self.run_as(who, "verdict", sid, "--pr", self.PR, "--as", "tester", "--result", "fail", "--evidence", evidence)

        def lines(self, sid="S2"):
            code, report, err = self.gates()
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

        def test_S3_the_audit_line_carries_the_redacted_clip(self):
            self.assertEqual(self.prove("coder", "S2")[0], 0)
            self.assertEqual(self.fail("saw %s fail" % SECRET)[0], 0)
            details = [e["detail"] for e in self.world.audit() if e["event"] == "plan.verdict"]
            self.assertEqual(len(details), 1)
            self.assertTrue(details[0]["evidence"].startswith("saw "), details[0])
            self.assertNotIn(SECRET, str(self.world.audit()))
            self.assertEqual(details[0]["evidence"], self.ledger()[-1]["evidence"][:160])

        def test_S4_an_empty_or_blank_evidence_stays_refused(self):
            self.assertEqual(self.prove("coder", "S2")[0], 0)
            before = len(self.ledger())
            refusals = 0
            for ev in ("", "   "):
                code, _, err = self.fail(ev)
                self.assertNotEqual(code, 0)
                refusals += err.count("--evidence is required")
            self.assertEqual(refusals, 2)
            self.assertEqual(len(self.ledger()), before)

    return TestVerdictEvidence
