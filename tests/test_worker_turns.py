"""#1102: a worker's turns are read as tool turns and errors, not blank lines. Run: python3 -m unittest tests.test_worker_turns"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import worker_turns as wt  # noqa: E402

spec = importlib.util.spec_from_file_location("launcher_worker", ROOT / "cloud" / "launcher" / "cloud_worker.py")
launcher = importlib.util.module_from_spec(spec)
sys.modules["launcher_worker"] = launcher
spec.loader.exec_module(launcher)
SCRUB = launcher.scrub
CANARY = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def tool(name, **args):
    return {"type": "tool", "tool": name, "state": {"input": args}}


class TestTurns(unittest.TestCase):
    def test_tool_only_turn_shows_tool_name_and_summary(self):
        out = wt.turn_text({"type": "assistant", "parts": [tool("bash", command="ls -la"), tool("read", path="a.txt")]}, SCRUB)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("tool bash", out)
        self.assertIn("ls -la", out)
        self.assertIn("tool read", out)

    def test_text_turn_shows_its_text(self):
        self.assertEqual(wt.turn_text({"type": "assistant", "text": "hello"}, SCRUB), "hello")

    def test_failed_turn_shows_error_name_and_status(self):
        out = wt.turn_text({"type": "assistant", "error": {"name": "APIError", "data": {"statusCode": 429}}}, SCRUB)
        self.assertIn("APIError", out)
        self.assertIn("429", out)

    def test_mixed_turn_shows_text_only_when_present(self):
        out = wt.turn_text({"type": "assistant", "parts": [{"type": "text", "text": "doing it"}, tool("bash", command="x")]}, SCRUB)
        self.assertIn("doing it", out)

    def test_empty_turn_is_empty(self):
        self.assertEqual(wt.turn_text({"type": "assistant", "parts": []}, SCRUB), "")

    def test_argument_summary_is_bounded(self):
        out = wt.turn_text({"parts": [tool("bash", command="word " * 500)]}, SCRUB)
        self.assertLessEqual(len(out), len("tool bash ") + wt.SUMMARY_MAX + 1)

    def test_canary_token_in_arguments_is_not_printed(self):
        out = wt.turn_text({"parts": [tool("bash", command="curl -H 'Authorization: Bearer %s' x" % CANARY)]}, SCRUB)
        self.assertNotIn(CANARY, out)
        self.assertNotIn(CANARY[:12], out)

    def test_an_unscrubbed_printer_would_fail_the_canary(self):
        out = wt.turn_text({"parts": [tool("bash", command=CANARY)]}, lambda text, known, limit: text[:limit])   # the mutant: no scrub
        self.assertIn(CANARY, out)

    def test_summary_line(self):
        messages = [{"type": "assistant", "parts": [tool("bash", command="a")], "time": {"created": 2}},
                    {"type": "assistant", "error": {"name": "APIError", "data": {"statusCode": 429}}, "time": {"created": 3}},
                    {"type": "user", "text": "go", "time": {"created": 1}}]
        line = wt.summary_line(messages, SCRUB)
        for part in ("turns=2", "tools=1", "last_tool=bash", "last_error=APIError/429", "last_activity=3"):
            self.assertIn(part, line)
