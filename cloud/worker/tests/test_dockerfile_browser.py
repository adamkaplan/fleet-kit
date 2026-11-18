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


CHROMIUM_VERSION = "158.0.8127.36"
CHROMIUM_SHA = "2d3a23017323c54fd457a678e4351f895c4076d8050e39d5aa6247f93448b73a"
CHROMIUM_CHECK = ' && echo "${CHROMIUM_SHA256}  /tmp/chromium.zip" | sha256sum -c - \\\n'
UNCHECKED = "the Chromium archive was not checked"
README = os.path.join(os.path.dirname(os.path.dirname(HERE)), "README.md")


def chromium_run(t):
    b = t.find("ARG CHROMIUM_SHA256=")
    e = t.find("# ---- end Chromium ----", b)
    if b < 0 or e < 0:
        return ""
    blk = t[b:e]
    return " ".join(x.strip() for x in blk.replace("\\\n", " ").split("\n") if x.strip() and not x.startswith(("#", "ARG ", "ENV ")))


def chromium_problems(t):
    out = []
    if len(re.findall(r"^ARG CHROMIUM_VERSION=%s$" % re.escape(CHROMIUM_VERSION), t, re.M)) != 1:
        out.append("CHROMIUM_VERSION must be pinned once to " + CHROMIUM_VERSION)
    if len(re.findall(r"^ARG CHROMIUM_SHA256=%s$" % CHROMIUM_SHA, t, re.M)) != 1:
        out.append("CHROMIUM_SHA256 must be pinned once to the stated hash")
    run = chromium_run(t)
    if not run.startswith("RUN "):
        out.append("no Chromium RUN")
    chk = run.find('echo "${CHROMIUM_SHA256}  /tmp/chromium.zip" | sha256sum -c -')
    unz = run.find("python3 -m zipfile -e /tmp/chromium.zip /tmp/chromium")
    if chk < 0 or unz < 0 or chk > unz:
        out.append(UNCHECKED)
    if ("curl -fsSL https://storage.googleapis.com/chrome-for-testing-public/${CHROMIUM_VERSION}"
            "/linux64/chrome-headless-shell-linux64.zip -o /tmp/chromium.zip") not in run:
        out.append("Chromium archive not downloaded with curl -fsSL from the pinned version")
    for need in ("chmod 0755 /opt/chromium/chrome-headless-shell", "chown -R root:root /opt/chromium",
                 "chmod -R a+rX,go-w /opt/chromium"):
        if need not in run:
            out.append("missing: " + need)
    for n in ("chromium", "chromium-browser", "google-chrome-stable"):
        if not re.search(r"ln -s /opt/chromium/chrome-headless-shell /usr/bin/%s(?= |$)" % re.escape(n), run):
            out.append(n + " not linked")
    if not re.search(r"&& rm -rf /tmp/chromium\.zip /tmp/chromium$", run):
        out.append("archive not removed at the end of the RUN")
    if "||" in run or re.search(r"sha256sum[^&]*;", run):
        out.append("check or chain masked")
    if len(re.findall(r"^ENV CHROME_PATH=/usr/bin/chromium$", t, re.M)) != 1 or len(re.findall(r"^ENV[^\n]*CHROME_PATH", t, re.M)) != 1:
        out.append("CHROME_PATH=/usr/bin/chromium must be set once")
    return out


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

    def _cm(self, old, new):
        t = read(DF)
        self.assertEqual(t.count(old), 1, old)
        m = t.replace(old, new)
        self.assertNotEqual(m, t)
        return m

    def test_chromium_real_file_clean(self):
        self.assertEqual(chromium_problems(read(DF)), [])

    def test_chromium_mutant_check_dropped(self):
        self.assertEqual(chromium_problems(read(DF)), [])
        self.assertIn(UNCHECKED, chromium_problems(self._cm(CHROMIUM_CHECK, "")))

    def test_chromium_mutant_check_after_unpack(self):
        self.assertEqual(chromium_problems(read(DF)), [])
        unz = " && python3 -m zipfile -e /tmp/chromium.zip /tmp/chromium \\\n"
        m = self._cm(CHROMIUM_CHECK + unz, unz + CHROMIUM_CHECK)
        self.assertIn(UNCHECKED, chromium_problems(m))

    def test_chromium_mutant_masked(self):
        m = self._cm("sha256sum -c - \\\n && python3 -m zipfile", "sha256sum -c - || true \\\n && python3 -m zipfile")
        self.assertIn("check or chain masked", chromium_problems(m))

    def test_chromium_mutants_other(self):
        self.assertEqual(chromium_problems(read(DF)), [])
        for old, new in (
            ("ARG CHROMIUM_VERSION=" + CHROMIUM_VERSION, "ARG CHROMIUM_VERSION=158.0.0.0"),
            ("ARG CHROMIUM_SHA256=" + CHROMIUM_SHA, "ARG CHROMIUM_SHA256=" + "0" * 64),
            ("chown -R root:root /opt/chromium && ", ""),
            ("chmod 0755 /opt/chromium/chrome-headless-shell /opt/chromium/*.so* \\\n && ", ""),
            (" && ln -s /opt/chromium/chrome-headless-shell /usr/bin/chromium \\\n", ""),
            (" && ln -s /opt/chromium/chrome-headless-shell /usr/bin/google-chrome-stable \\\n", ""),
            ("rm -rf /tmp/chromium.zip /tmp/chromium", "rm -rf /tmp/chromium"),
            ("ENV CHROME_PATH=/usr/bin/chromium\n", ""),
        ):
            with self.subTest(old=old):
                self.assertNotEqual(chromium_problems(self._cm(old, new)), [])

    def test_chromium_docs(self):
        r = read(README)
        for s in ("CHROMIUM_VERSION", "CHROMIUM_SHA256", "CHROME_PATH=/usr/bin/chromium"):
            self.assertIn(s, r)

    def test_noop_mutation_fails_the_computation(self):
        real = read(DF)
        mutant = real.replace("zz-absent-zz", "", 1)
        applied = len(real) - len(mutant) == len("libglib2.0-0t64") + 1
        self.assertFalse(applied)


if __name__ == "__main__":
    unittest.main()
