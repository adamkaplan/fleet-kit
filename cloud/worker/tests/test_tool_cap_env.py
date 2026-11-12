"""Ask #2447: second layer. The image sets OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS to a value in 1..CAP_MS (CAP_MS read from the plugin)."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.dirname(HERE)
NAME = "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS"


def cap():
    with open(os.path.join(W, "plugins", "cw-tool-cap", "index.js")) as f:
        return int(re.search(r"export const CAP_MS = (\d+);", f.read()).group(1))


def problems(text, bound):
    vals = re.findall(r"(?m)^ENV %s=(\S+)\s*$" % NAME, text)
    if len(vals) != 1:
        return ["the default shell timeout variable must be set exactly once"]
    if not re.fullmatch(r"[1-9][0-9]*", vals[0]) or not 1 <= int(vals[0]) <= min(bound, 2**31 - 1):
        return ["the default shell timeout variable is outside 1..CAP_MS"]
    return []


class T(unittest.TestCase):
    def read(self):
        with open(os.path.join(W, "Dockerfile")) as f:
            return f.read()

    def test_control_then_mutant(self):
        t = self.read()
        self.assertEqual(problems(t, cap()), [])
        m = re.sub(r"(?m)^(ENV %s=)\d+" % NAME, r"\g<1>600000", t)
        self.assertNotEqual(m, t)
        self.assertEqual(problems(m, cap()), ["the default shell timeout variable is outside 1..CAP_MS"])
        self.assertEqual(problems(t.replace("ENV " + NAME, "#"), cap()), ["the default shell timeout variable must be set exactly once"])


if __name__ == "__main__":
    unittest.main()
