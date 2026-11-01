"""Ask #1768: the pod's state and last events reach the run record (`pod_state` of run.json) before the pod is deleted.
Offline: a fake cluster object (no kubectl); every file is written in a temporary directory. Prints S lines (counts and names only)."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
import run_record  # noqa: E402


def say(line):
    print(line, file=sys.stderr)


class Mask(unittest.TestCase):
    def test_the_address_mask(self):
        cases = {"dial 10.1.2.3 failed": "dial [address] failed", "to 10.1.2.3:443 refused": "to [address] refused",
                 "get registry.example.org:5000 timeout": "get [address] timeout",
                 "node aks-pool-0001 not ready": "node aks-pool-0001 not ready", "at 10:30 it ended": "at 10:30 it ended"}
        bad = [k for k, v in cases.items() if cw.mask_addresses(k) != v]
        say("S5 mask: %d of %d cases masked as asked" % (len(cases) - len(bad), len(cases)))
        self.assertEqual(bad, [])


class Writer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)
        with open(os.path.join(self.tmp.name, "run.json"), "w") as f:
            json.dump({"worker": "w1", "exit_reason": "stop"}, f)

    def read(self):
        with open(os.path.join(self.tmp.name, "run.json")) as f:
            return json.load(f)

    def test_the_field_is_added_and_the_earlier_fields_stay(self):
        run_record.Recorder.add_pod_state(self.tmp.name, {"phase": "Failed", "events": [], "events_read": "yes"})
        got = self.read()
        say("S2 writer: fields %s" % ",".join(sorted(got)))
        self.assertEqual(got["worker"], "w1")
        self.assertEqual(got["pod_state"]["phase"], "Failed")

    def test_a_state_that_cannot_be_written_is_the_one_word(self):
        run_record.Recorder.add_pod_state(self.tmp.name, {"bad": object()})
        self.assertEqual(self.read()["pod_state"], "unreadable")
        say("S4 writer: unwritable state gives 1 word")

    def test_a_missing_record_never_raises(self):
        run_record.Recorder.add_pod_state(os.path.join(self.tmp.name, "absent"), {"phase": "Failed"})
        say("S4 writer: a missing record raised 0 times")


if __name__ == "__main__":
    unittest.main()
