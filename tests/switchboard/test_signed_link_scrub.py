"""The #1576 signed-link scrub tests, moved out of bin/test-switchboard (its line ceiling). build() is called by bin/test-switchboard, which holds Base and GithubRig."""
import json
from support.loader import sb
from support.paths import REPO


LINK_SCRUB_NAMES = ("token", "access_token", "id_token", "refresh_token", "sig", "signature", "key", "api_key", "apikey", "secret",
                    "client_secret", "password", "passwd", "code", "expires", "expiry", "x-amz-signature", "x-amz-credential",
                    "x-amz-security-token", "auth", "jwt", "sid", "ticket", "sas", "pwd", "credential")


def _planted(i):
    return "PLANTEDvalue%03dZq" % i


def build(Base, GithubRig):
    class TestSignedLinkScrub(Base):
        def test_the_stated_list_is_the_one_the_docs_name(self):
            self.assertEqual(tuple(sb.SIGNED_NAMES), LINK_SCRUB_NAMES)
            text = (REPO / "docs" / "switchboard.md").read_text()
            for word in ("[query removed]", "x-amz-signature", "after the scrub"):
                self.assertIn(word, text)

        def test_s2_every_listed_name_in_the_query_is_scrubbed_and_the_path_stays(self):
            for i, name in enumerate(LINK_SCRUB_NAMES):
                with self.subTest(name=name):
                    out = sb.scrub_signed_links("see https://files.example.com/a/b.png?page=2&%s=%s&z=1 now" % (name, _planted(i)))
                    self.assertEqual(out, "see https://files.example.com/a/b.png[query removed] now")

        def test_s3_every_listed_name_in_the_fragment_is_scrubbed(self):
            for i, name in enumerate(LINK_SCRUB_NAMES):
                with self.subTest(name=name):
                    out = sb.scrub_signed_links("https://x.example.com/cb#%s=%s;state=1 end" % (name, _planted(i)))
                    self.assertEqual(out, "https://x.example.com/cb[query removed] end")

        def test_s4_case_percent_encoding_and_encoded_links(self):
            for text in ("https://h.example/p?TOKEN=%s" % _planted(1), "https://h.example/p?ToKeN=%s" % _planted(1),
                         "https://h.example/p?%%74oken=%s" % _planted(1), "https://h.example/p?X-Amz-Signature=%s" % _planted(1),
                         "https://h.example/p?my_Access_TOKEN_v2=%s" % _planted(1),
                         "https%%3A%%2F%%2Fh.example%%2Fp%%3Ftoken%%3D%s" % _planted(1)):
                with self.subTest(text=text):
                    out = sb.scrub_signed_links("a " + text)
                    self.assertNotIn(_planted(1), out)
                    self.assertIn("[query removed]", out)
                    self.assertIn("h.example", out)

        def test_s5_a_link_without_a_signed_name_and_text_without_a_link_are_unchanged(self):
            for text in ("see https://x.example.com/a?page=2&q=hello#top ok", "https://x.example.com/plain/path", "no link; 100% sure? #12 key=v",
                         "", "token=abc in prose"):
                with self.subTest(text=text):
                    self.assertEqual(sb.scrub_signed_links(text), text)

        def test_r1_review_fix_scrubs_bracket(self):
            out = sb.scrub_signed_links("see https://h/p?a[0]=1&token=x ok")
            self.assertNotIn("token=x", out)
            self.assertNotIn("=x ", out)
            self.assertIn("https://h", out)
            self.assertIn("[query removed]", out)

        def test_r2_review_fix_scrubs_brace(self):
            out = sb.scrub_signed_links("see https://h/p?a={1}&token=x ok")
            self.assertNotIn("token=x", out)
            self.assertNotIn("=x ", out)
            self.assertIn("https://h", out)
            self.assertIn("[query removed]", out)

        def test_r3_review_fix_scrubs_paren(self):
            out = sb.scrub_signed_links("see https://h/p?a=(1)&token=x ok")
            self.assertNotIn("token=x", out)
            self.assertNotIn("=x ", out)
            self.assertIn("https://h", out)
            self.assertIn("[query removed]", out)

        def test_r4_review_fix_scrubs_pipe(self):
            out = sb.scrub_signed_links("see https://h/p?q=a|b&token=x ok")
            self.assertNotIn("token=x", out)
            self.assertNotIn("=x ", out)
            self.assertIn("https://h", out)
            self.assertIn("[query removed]", out)

        def test_r5_review_fix_scrubs_path_paren(self):
            out = sb.scrub_signed_links("see https://a.com/a(b)?token=abc ok")
            self.assertNotIn("token=abc", out)
            self.assertNotIn("=abc ", out)
            self.assertIn("https://a.com", out)
            self.assertIn("[query removed]", out)

        def test_r6_review_fix_scrubs_enc_q(self):
            out = sb.scrub_signed_links("see https://a.com/p%3Ftoken=abc ok")
            self.assertNotIn("token=abc", out)
            self.assertNotIn("=abc ", out)
            self.assertIn("https://a.com", out)
            self.assertIn("[query removed]", out)

        def test_r7_review_fix_scrubs_double_enc(self):
            out = sb.scrub_signed_links("see https://a.com/p%253Ftoken=abc ok")
            self.assertNotIn("token=abc", out)
            self.assertNotIn("=abc ", out)
            self.assertIn("https://a.com", out)
            self.assertIn("[query removed]", out)

        def test_r8_review_fix_scrubs_enc_name(self):
            out = sb.scrub_signed_links("see https://a.com/p?a=1&%2574oken=abc ok")
            self.assertNotIn("token=abc", out)
            self.assertNotIn("=abc ", out)
            self.assertIn("https://a.com", out)
            self.assertIn("[query removed]", out)

        def test_r9_review_fix_scrubs_enc_amp(self):
            out = sb.scrub_signed_links("see https://a.com/p?a=%26token=abc ok")
            self.assertNotIn("token=abc", out)
            self.assertNotIn("=abc ", out)
            self.assertIn("https://a.com", out)
            self.assertIn("[query removed]", out)

        def test_r10_review_fix_scrubs_fullwidth_q(self):
            out = sb.scrub_signed_links("see https://a.com/p\uFF1Ftoken=abc ok")
            self.assertNotIn("token=abc", out)
            self.assertNotIn("=abc ", out)
            self.assertIn("https://a.com", out)
            self.assertIn("[query removed]", out)

        def _kinds(self, body):
            issue = {"number": 7, "title": "T " + body, "state": "open"}
            comment = {"id": 1, "body": body, "user": {"login": "octocat"}}
            review = {"id": 2, "state": "commented", "body": body, "user": {"login": "octocat"}}
            return (sb._comment_item("acme/api", comment, issue, 0).summary,
                    sb._review_item("acme/api", review, {"number": 7}, 0).summary)

        def test_s6_the_comment_kinds_are_scrubbed_and_a_title_is_not(self):
            body = "look https://h.example/p?token=%s" % _planted(2)
            summaries = self._kinds(body)
            self.assertEqual(sum(_planted(2) in s for s in summaries), 0)
            title = sb._one_line("T " + body, 400)
            self.assertIn(_planted(2), title)

        def test_s6_a_review_comment_line_is_scrubbed_in_the_login_and_signer(self):
            body = "Review of %s\n\nAPPROVE" % ("a" * 40)
            issue = {"number": 7, "state": "open", "pull_request": {"url": "u"}}
            comment = {"id": 3, "body": body, "user": {"login": "https://h.example/p?token=%s" % _planted(3)}}
            item = sb._review_comment_item("acme/api", comment, issue, 7, 3, 0)
            self.assertIsNotNone(item)
            self.assertNotIn(_planted(3), item.summary)

        def test_s7_the_cut_is_made_after_the_scrub(self):
            value = _planted(4)
            for pad in (370, 380, 390, 395):
                with self.subTest(pad=pad):
                    body = "x" * pad + " https://h.example/p?token=" + value + "&more=1"
                    for summary in self._kinds(body):
                        self.assertNotIn(value[:6], summary)
                    comment_summary = self._kinds(body)[0]
                    self.assertLessEqual(len(comment_summary) - len("@octocat commented on #7: "), 400)

        def test_s8_nothing_planted_reaches_the_message_the_audit_or_the_state_directory(self):
            rig = GithubRig(self, agents={"platform": {"role": "orchestrator", "charter": 1}})
            rig.gh.issue(1)
            rig.gh.issue(12, parent=1)
            rig.hub.tick()
            values = []
            for i, name in enumerate(LINK_SCRUB_NAMES):
                values.append(_planted(i))
                rig.gh.comment(12, "link https://h.example/p?%s=%s" % (name.upper() if i % 2 else name, _planted(i)), 7000 + i)
            rig.gh.comment(12, "x" * 380 + " https://h.example/p?token=" + _planted(90), 7100)
            values.append(_planted(90))
            rig.pump(60)
            rig.hub.tick()
            everything = rig.state_text() + json.dumps(rig.world.admissions("platform")) + json.dumps(rig.world.audit())
            self.assertEqual(sum("[query removed]" in i.summary for i in rig.hub.queue.values()), 26)   # the 27th is cut inside the words
            self.assertEqual(sum(everything.count(v) for v in values), 0)
            self.assertEqual(everything.count(_planted(90)[:6]), 0)

        def test_c1_a_long_run_with_the_signed_pair_after_2100_characters_never_reaches_a_message_or_the_audit(self):
            value = _planted(5)
            url = "https://h.example/" + "a" * 2100 + "?token=" + value
            for body in (url, "x" * 350 + " " + url):
                with self.subTest(start=body.index("https")):
                    for summary in self._kinds(body):
                        self.assertNotIn(value[:6], summary)
            rig = GithubRig(self, agents={"platform": {"role": "orchestrator", "charter": 1}})
            rig.gh.issue(1)
            rig.gh.issue(12, parent=1)
            rig.hub.tick()
            rig.gh.comment(12, "x" * 350 + " " + url, 7200)
            rig.pump(60)
            rig.hub.tick()
            everything = rig.state_text() + json.dumps(rig.world.admissions("platform")) + json.dumps(rig.world.audit())
            self.assertEqual(everything.count(value[:6]), 0)

        def test_c2_the_2000_character_cap_of_one_run(self):
            value = _planted(6)
            tail = "?token=" + value
            inside = "https://h.example/" + "a" * (1999 - len("https://h.example/") - len(tail)) + tail
            self.assertEqual(len(inside), 1999)
            out = sb.scrub_signed_links(inside)
            self.assertNotIn(value[:6], out)
            self.assertTrue(out.endswith("[query removed]"))
            beyond = "https://h.example/" + "a" * (2000 - len("https://h.example/")) + tail
            self.assertEqual(beyond.index("?"), 2000)
            self.assertEqual(sb.scrub_signed_links(beyond), beyond)   # the scrub leaves it; the summary cap cuts it
            for summary in self._kinds(beyond):
                self.assertNotIn(value[:6], summary)

        def test_c3_a_100000_character_encoded_run_is_scrubbed_in_under_a_second(self):
            import time
            value = _planted(7)
            text = "https://a.com/" + "%25" * 100000 + "?token=" + value
            start = time.monotonic()
            out = sb.scrub_signed_links(text)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 1.0)
            self.assertEqual(out, text)   # no mark in the first 2,000 characters: the scrub leaves the run, the summary cap cuts it
            for summary in self._kinds(text):
                self.assertFalse(value[:6] in summary)

    return TestSignedLinkScrub
