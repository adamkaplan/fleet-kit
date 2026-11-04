#!/usr/bin/env python3
"""Offline tests: a piece of a structured known secret is scrubbed like the whole value (ask #2144; S1 S2 S3 U1).
Run directly: python3 cloud/launcher/tests/test_scrub_pieces.py. Every value below is SYNTHETIC; only counts are printed.

THE CAUSE (S1). `Kube.add_secrets` (cloud_worker.py) calls `secret_forms(v)` for each whole value. `secret_forms` builds the forms of THAT
text only: itself, base64 at three alignments, hex, percent-encoding, JSON-escaping. `scrub` redacts a form and any 8-character window of a
form (`_scrub_built`, ask #1776). A plain piece of 8+ characters that is a substring of the whole value is therefore caught, but a piece that
is NOT a substring is not: the password of a URL that holds it percent-encoded (a client prints it decoded), and the decoded user:password.
Under 8 characters a piece (`u:pw@`) is never redacted: the plan keeps that floor (SECRET_PIECE_MIN = 8) so common words stay.
The fix: `_secret_pieces` names the pieces and `secret_forms` adds the forms of each, inserted before its return."""
import base64
import importlib.util
import os
import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CW = ROOT / "cloud" / "launcher" / "cloud_worker.py"
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
ANCHOR = "a piece of a known secret survived the scrub"


def load(name, source=None):
    spec = importlib.util.spec_from_file_location(name, str(CW))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    if source is None:
        spec.loader.exec_module(mod)
    else:
        mod.__file__ = str(CW)
        exec(compile(source, str(CW), "exec"), mod.__dict__)
    return mod


b64 = lambda t: base64.b64encode(t.encode()).decode()
USER, PW = "appuser", "Zx9!s3cr@t"  # the password as a client uses it; the URL holds it percent-encoded
URL = "postgres://%s:%s@db.internal:5432/app" % (USER, urllib.parse.quote(PW, safe=""))
TOKEN_BODY = "A1b2C3d4E5f6G7h8I9j0K1l2"
TOKEN = "ghp_" + TOKEN_BODY
KEY_LINES = ["QUJDREVGR0hJSktMTU5PUA1234", "WFlaMDEyMzQ1Njc4OTBhYmNk5678"]
KEY = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(KEY_LINES) + "\n-----END PRIVATE KEY-----"
MODEL = "mk-7Hq2xZ"
VALUES = (URL, TOKEN, KEY, MODEL)
# (shape, planted text): each whole value and each piece a reader could use
PLANTED = [("url", URL), ("url", urllib.parse.quote(PW, safe="")), ("url", PW), ("url", USER + ":" + PW), ("url", b64(USER + ":" + PW)),
           ("url", b64(PW)), ("url", urllib.parse.quote(PW, safe="")), ("url", PW.encode().hex()), ("url", urllib.parse.quote(USER + ":" + PW, safe="")),
           ("token", TOKEN), ("token", TOKEN_BODY), ("token", b64(TOKEN_BODY)),
           ("key", KEY), ("key", KEY_LINES[0]), ("key", KEY_LINES[1]), ("key", b64(KEY_LINES[0])),
           ("model", MODEL), ("model", b64(MODEL))]
AROUND = {"url": ("connect with", "failed twice"), "token": ("header was", "and then 401"), "key": ("loaded the", "from disk"), "model": ("model", "answered")}


def known_set(mod):
    """The launcher's way: Kube.add_secrets of each Secret value, on an object that holds a KnownSet."""
    holder = type("Holder", (), {})()
    holder.known = mod.KnownSet()
    mod.Kube.add_secrets(holder, *VALUES)
    return holder.known


def survivors(mod):
    """Planted texts of which an 8-character window is left in the scrubbed text; plain text around each that changed."""
    known, left, changed = known_set(mod), [], []
    for shape, text in PLANTED:
        pre, post = AROUND[shape]
        out = mod.scrub("%s %s %s" % (pre, text, post), known, 2000)
        if any(text[i:i + 8] in out for i in range(len(text) - 7)):
            left.append(shape)
        if not (out.startswith(pre + " ") and out.endswith(" " + post)):
            changed.append(shape)
    return left, changed


def check(mod):
    left, changed = survivors(mod)
    assert not left, "%s: %d of %d planted values and pieces" % (ANCHOR, len(left), len(PLANTED))
    assert not changed, "plain text around a planted value changed in %d texts" % len(changed)


class ScrubPieces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cw = load("scrub_pieces_cw")
        cls.src = CW.read_text()

    def test_S2_no_value_or_piece_survives(self):
        left, changed = survivors(self.cw)
        self.assertEqual(left, [])
        self.assertEqual(changed, [])
        print("S2 planted %d, surviving 0, plain texts unchanged %d of %d" % (len(PLANTED), len(PLANTED), len(PLANTED)))

    def test_S2_short_words_and_pieces_are_not_redacted(self):
        known = known_set(self.cw)
        for word in ("the", "user", "password", "secret", "common word", "BEGIN KEY"):
            self.assertEqual(self.cw.scrub("a %s here" % word, known), "a %s here" % word)
        short = self.cw.secret_forms("postgres://u:pw@db.internal/app")  # a password under 8: no piece is added
        self.assertFalse({"pw", "u:pw"} & short)
        self.assertTrue(all(len(p) >= 8 for p in self.cw._secret_pieces(URL) | self.cw._secret_pieces(TOKEN) | self.cw._secret_pieces(KEY)))
        print("S2 short words unchanged 7 of 7, pieces under 8: 0")

    def test_S1_the_whole_value_alone_leaves_the_decoded_password(self):
        """The cause, shown on a synthetic URL: forms of the whole value only. The URL holds the password percent-encoded; a client
        prints it decoded (`Zx9!s3cr@t`), which is no window of any form of the whole URL, so it survives."""
        mutant = load("scrub_pieces_whole", self._mutant_source())
        whole = mutant.KnownSet()
        whole.update(mutant.secret_forms(URL))
        self.assertIn(PW, mutant.scrub("password " + PW + " rejected", whole))
        fixed = self.cw.KnownSet()
        fixed.update(self.cw.secret_forms(URL))
        self.assertNotIn(PW, self.cw.scrub("password " + PW + " rejected", fixed))
        print("S1 surviving piece named: the decoded password of a percent-encoded URL, 1 of 1; line that adds the forms: Kube.add_secrets -> secret_forms, 1 of 1")

    def _mutant_source(self):
        old = "    for piece in _secret_pieces(value):\n        forms.update(secret_forms(piece))  # a piece is a form of the same value, with its own encodings\n"
        self.assertEqual(self.src.count(old), 1)
        new = self.src.replace(old, "")
        self.assertNotEqual(new, self.src)
        return new

    def test_S3_control_then_mutant(self):
        check(self.cw)  # the unmutated control first: it passes
        mutant = load("scrub_pieces_mutant", self._mutant_source())  # whole values only
        with self.assertRaises(AssertionError) as caught:
            check(mutant)
        self.assertIn(ANCHOR, str(caught.exception))
        print("S3 control 1 of 1, mutant fails 1 of 1 with the anchored text, mutation applied 1 of 1")


if __name__ == "__main__":
    unittest.main()
