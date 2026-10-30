#!/usr/bin/env python3
"""Scenario S11 of ask #1616: the read of a retired worker's last message, on the CURRENT code of bin/cloud_retired.py.

The fixtures are those of bin/test-cloud-retired (loaded from there, nothing copied). The read of a worker that was never launched says so.
"""
import importlib.machinery
import importlib.util
import os
import sys
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[3] / "bin"
_loader = importlib.machinery.SourceFileLoader("cw_tests_of_the_retired", str(BIN / "test-cloud-retired"))
rt = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw_tests_of_the_retired", _loader))
sys.modules["cw_tests_of_the_retired"] = rt
_loader.exec_module(rt)

RESULTS = []


class ReadRetiredLastMessage(rt.Retired):
    def test_the_last_message_of_a_retired_worker_is_read_under_its_header(self):
        self.retire()
        self.run_record("coder7", rt.stamp(-5), [rt.msg("assistant", "first"), rt.msg("user", "q"), rt.msg("assistant", "THE LAST MESSAGE")])
        code, out, err = self.as_caller(rt.KIND_BOSS, "read", "coder7")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertIn("record of a retired run", lines[0])
        self.assertEqual(lines[1:], ["THE LAST MESSAGE"])
        RESULTS.append("read")
        RESULTS.append("header")

    def test_a_name_never_launched_says_it_is_not_a_fleet_agent(self):
        code, out, err = self.as_caller(rt.KIND_BOSS, "read", "neverlaunched")
        self.assertNotEqual(code, 0)
        self.assertIn("is not a fleet agent this switchboard has seen", out + err)
        RESULTS.append("read")


def tearDownModule():
    print("S11: reads as stated %d of 2, header found %d" % (RESULTS.count("read"), RESULTS.count("header")), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
