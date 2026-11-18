"""A plain absolute file path with a long random segment is not a token (the temporary directory of a developer machine)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cloud_worker as cw  # noqa: E402


class ScrubKeepsPaths(unittest.TestCase):
    def test_path_with_random_segment_stays(self):
        p = "/var/folders/zz/Ab3dE5gH7jK9mN1pQ3sT5vX7yZ0/T/kubectl"
        self.assertEqual(cw.scrub("%s delete secret x" % p, limit=500), "%s delete secret x" % p)

    def test_token_shapes_still_redacted(self):
        for tok in ("ghp_abcdef123456", "github_pat_abcdef123456", "eyJhbGciOiJIUzI1NiJ9.abc.def",
                    "A" * 44, "Ab3+" * 12 + "==", "ab-_" * 12, "0123456789abcdef" * 3):
            self.assertIn("[redacted]", cw.scrub("x %s y" % tok), tok)
            self.assertNotIn(tok[:12], cw.scrub("x %s y" % tok))


if __name__ == "__main__":
    unittest.main()
