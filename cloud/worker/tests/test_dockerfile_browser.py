"""#2047: the worker image installs the system libraries of Playwright's headless Chromium shell.
Offline: reads the Dockerfile only. Scenario S4."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DF = os.path.join(os.path.dirname(HERE), "Dockerfile")
PACKAGES = (
    "libasound2t64", "libatk-bridge2.0-0t64", "libatk1.0-0t64", "libatspi2.0-0t64", "libcairo2",
    "libcups2t64", "libdbus-1-3", "libdrm2", "libfontconfig1", "libfreetype6", "libgbm1",
    "libglib2.0-0t64", "libnspr4", "libnss3", "libpango-1.0-0", "libx11-6", "libxcb1",
    "libxcomposite1", "libxdamage1", "libxext6", "libxfixes3", "libxkbcommon0", "libxrandr2",
    "fonts-liberation", "fonts-noto-color-emoji", "fonts-unifont", "fonts-freefont-ttf",
)


def read(p):
    with open(p) as f:
        return f.read()


def check(text):
    """Fail with the anchored text when a package is absent from the #2047 install line."""
    m = re.search(r"^RUN apt-get update && apt-get install -y --no-install-recommends ([^\\\n]*)\\\n && rm -rf /var/lib/apt/lists/\*$",
                  text.split("# #2047:")[-1] if "# #2047:" in text else "", re.M)
    have = set(m.group(1).split()) if m else set()
    missing = [p for p in PACKAGES if p not in have]
    if missing:
        raise AssertionError("a browser library package is missing from the image: " + " ".join(missing))


class TestDockerfileBrowser(unittest.TestCase):
    def test_real_dockerfile_has_every_package(self):
        check(read(DF))

    def test_mutant_without_one_package_fails(self):
        real = read(DF)
        check(real)  # control first, passing
        name = "libglib2.0-0t64"
        self.assertEqual(real.count(name), 1)
        mutant = real.replace(" " + name, "", 1)
        applied = len(real) - len(mutant) == len(name) + 1 and name not in mutant
        self.assertTrue(applied, "the mutation was not applied")
        with self.assertRaises(AssertionError) as cm:
            check(mutant)
        self.assertIn("a browser library package is missing from the image: " + name, str(cm.exception))

    def test_noop_mutation_fails_the_computation(self):
        real = read(DF)
        mutant = real.replace("zz-absent-zz", "", 1)
        applied = len(real) - len(mutant) == len("libglib2.0-0t64") + 1
        self.assertFalse(applied)


if __name__ == "__main__":
    unittest.main()
