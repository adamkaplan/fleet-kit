"""#1642: `launch --model PROVIDER/MODEL --reasoning LEVEL`. build(host) gives the tests the fixtures of bin/test-switchboard."""
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
from unittest import mock

from support.loader import captured, sb

MODEL_LIST = [{"providerID": "acme-provider", "id": "model-a", "variants": [{"id": "low"}, {"id": "medium"}, {"id": "high"}]},
              {"providerID": "acme-provider", "id": "model-b", "variants": []}]
OPTIONS = ("--model", "acme-provider/model-a", "--reasoning", "medium")


def build():
    host = HOST   # set by bin/test-switchboard: the fixtures of that file
    class TestLaunchModel(host.LedgerBase):
        """S2-S7, U1-U3, U7: the choice is checked against the local list before anything exists, then carried on the session and the record."""

        def setUp(self):
            super().setUp()
            self.work = self.home / "work"
            self.work.mkdir()
            self.listing, self.reads, self.bodies = list(MODEL_LIST), [], []
            self.before = len(self.world.v2.sessions)
            v2 = self.world.v2
            real = v2.op_session_create

            def model_list(params, body):
                self.reads.append(1)
                if self.listing is None:
                    raise host.FakeHTTP(500, "Boom", "no list")
                return self.listing

            def create(params, body):
                self.bodies.append(body)
                return real(params, body)
            v2.op_model_list, v2.op_session_create = model_list, create

        def launch(self, *more, mode="--terminal"):
            host.os.environ["HERDR_WORKSPACE_ID"] = "w1"
            host.os.environ.pop("HERDR_PANE_ID", None)
            argv = ["launch", "newcoder", "--agent", "coder", "--dir", str(self.work), "--role", "coder", "--reports-to", "own",
                    "--issue", "7", "--brief-file", "-", mode, *more]
            with captured() as (out, err), mock.patch.object(sys, "stdin", io.StringIO("Do it.\n")):
                code = sb.main(argv, runner=self.route, now=self.world.clock.now, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def record(self):
            (session,) = [s for s in self.world.v2.sessions.values() if s["title"] == "newcoder"]
            return session["metadata"]["fleet"]

        def nothing_started(self, err):
            self.assertEqual(self.bodies, [], "no session was created")
            self.assertEqual(len(self.world.v2.sessions), self.before)
            self.assertEqual(len([line for line in err.splitlines() if line.strip()]), 1, err)

        def test_s2_the_session_is_created_with_the_model_and_the_variant(self):
            code, out, err = self.launch(*OPTIONS)
            self.assertEqual(code, 0, err)
            (body,) = self.bodies
            self.assertEqual(body["model"], {"providerID": "acme-provider", "id": "model-a", "variant": "medium"})
            self.assertEqual(body["agent"], "coder")
            self.assertTrue(json.loads(out)["pane"])

        def test_s3_a_headless_launch_carries_the_same_model(self):
            code, out, err = self.launch(*OPTIONS, mode="--headless")
            self.assertEqual(code, 0, err)
            (body,) = self.bodies
            self.assertEqual(body["model"], {"providerID": "acme-provider", "id": "model-a", "variant": "medium"})
            self.assertEqual(self.record()["model"], "acme-provider/model-a")

        def test_s4_the_record_holds_the_choice_and_the_status_row_shows_it(self):
            self.assertEqual(self.launch(*OPTIONS)[0], 0)
            fleet = self.record()
            self.assertEqual((fleet["model"], fleet["reasoning"]), ("acme-provider/model-a", "medium"))
            code, out, err = self.run_as(None, "status")
            self.assertIn("model acme-provider/model-a reasoning medium", out)

        def test_s5_each_refusal_is_one_line_and_starts_nothing(self):
            cases = [("--model", "acme-provider/model-z"), ("--model", "acme-provider/model-a", "--reasoning", "max"),
                     ("--model", "acme-provider/model-b", "--reasoning", "low"), ("--model", "nonsense"),
                     ("--reasoning", "low"), ("--resume", "ses_x", "--model", "acme-provider/model-a"), ("--resume", "ses_x", "--reasoning", "low")]
            for case in cases:
                with self.subTest(case=case):
                    code, out, err = self.launch(*case)
                    self.assertNotEqual(code, 0)
                    self.nothing_started(err)
                    self.assertEqual(out, "")

        def test_s6_a_list_that_cannot_be_read_refuses_with_one_line(self):
            self.listing = None
            code, out, err = self.launch(*OPTIONS)
            self.assertNotEqual(code, 0)
            self.nothing_started(err)
            self.assertIn("could not be verified", err)

        def test_s7_u1_neither_option_is_the_session_call_it_was(self):
            code, out, err = self.launch()
            self.assertEqual(code, 0, err)
            (body,) = self.bodies
            self.assertNotIn("model", body)
            self.assertEqual(self.reads, [])
            fleet = self.record()
            self.assertFalse({"model", "reasoning"} & set(fleet))
            self.assertNotIn("model ", self.run_as(None, "status")[1].split("newcoder")[-1].split("\n")[0])

        def test_u2_one_read_of_the_list_before_the_session_and_nothing_else(self):
            self.assertEqual(self.launch(*OPTIONS)[0], 0)
            self.assertEqual(self.reads, [1])
            calls = [c for c in self.world.v2.calls if c[2:3] in (["model.list"], ["session.create"])]
            self.assertEqual([c[2] for c in calls], ["model.list", "session.create"])

        def test_u3_a_model_alone_sets_no_variant(self):
            self.assertEqual(self.launch("--model", "acme-provider/model-a")[0], 0)
            (body,) = self.bodies
            self.assertEqual(body["model"], {"providerID": "acme-provider", "id": "model-a"})
            self.assertNotIn("reasoning", self.record())

        def test_u7_a_malformed_value_reaches_nothing(self):
            bad = [("--model", "acme-provider/" + "m" * 108), ("--model", "acme provider/model-a"), ("--model", "acme-provider/model-a\nx"),
                   ("--model", "acme-provider/model-a;x"), ("--model", "acme-provider/model-a", "--reasoning", "m" * 25),
                   ("--model", "acme-provider/model-a", "--reasoning", "Medium")]
            for case in bad:
                with self.subTest(case=case):
                    code, out, err = self.launch(*case)
                    self.assertNotEqual(code, 0)
                    self.nothing_started(err)
                    self.assertEqual(self.reads, [])
                    seen = err + json.dumps(self.world.audit())
                    for raw in case[1::2]:
                        self.assertNotIn(raw, seen)

        def test_the_choice_is_checked_by_pure_functions_too(self):
            self.assertEqual(sb.launch_model_kwargs({}), {})
            self.assertEqual(sb.launch_model_kwargs({"model": "a/b/c"}), {"model": {"providerID": "a", "id": "b/c"}})
            self.assertEqual(sb.launch_model_note({}), "")

        # S9: four mutants of the code, each run through the same launch and shown to break an expectation the tests above hold.
        def mutant(self, name):
            real_choice, real_record = sb.launch_model_choice, sb.fleet_record

            def unchecked(args, engine):   # the level (and the model) is never checked against the list
                return sb.launch_model_syntax(args.model, args.reasoning) if args.model else {}
            patches = {"ignored": mock.patch.object(sb, "launch_model_kwargs", lambda choice: {}),
                       "unchecked_level": mock.patch.object(sb, "launch_model_choice", unchecked),
                       "record_not_written": mock.patch.object(sb, "fleet_record", lambda *a, **k: real_record(*a[:8])),
                       "check_after_session": mock.patch.object(sb, "launch_model_choice", unchecked)}
            return patches[name]

        def test_s9_mutant_the_options_are_ignored(self):
            with self.mutant("ignored"):
                self.assertEqual(self.launch(*OPTIONS)[0], 0)
            self.assertNotIn("model", self.bodies[0])   # test_s2 expects the model: it fails

        def test_s9_mutant_the_level_is_not_checked_against_the_models_variants(self):
            with self.mutant("unchecked_level"):
                code, out, err = self.launch("--model", "acme-provider/model-b", "--reasoning", "low")
            self.assertEqual(code, 0)   # test_s5 expects a refusal: it fails
            self.assertEqual(self.bodies[0]["model"]["variant"], "low")

        def test_s9_mutant_the_record_is_not_written(self):
            with self.mutant("record_not_written"):
                self.assertEqual(self.launch(*OPTIONS)[0], 0)
            self.assertNotIn("model", self.record())   # test_s4 expects the record keys: it fails

        def test_s9_mutant_the_check_is_made_after_the_session_is_created(self):
            with self.mutant("check_after_session"):
                code, out, err = self.launch("--model", "acme-provider/model-z")
            self.assertEqual(len(self.bodies), 1)   # test_s5 expects 0 sessions: it fails
            self.assertEqual(self.reads, [])

    return TestLaunchModel


def build_cloud():
    """S8, U8: the cloud route, on the fixtures of bin/test-cloud-worker (a fake pod service and a stub launcher); only the class below is exposed."""
    path = Path(__file__).resolve().parents[2] / "bin" / "test-cloud-worker"
    loader = importlib.machinery.SourceFileLoader("cw_for_launch_model", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw_for_launch_model", loader))
    sys.modules["cw_for_launch_model"] = module
    loader.exec_module(module)

    class TestLaunchModelCloud(module.Base):
        """The pod's session carries the model; another provider is refused before any token or cluster call; the pod's refusal of a level is reported."""

        def session_bodies(self):
            return [body for method, path, body in self.remote.bodies if method == "POST" and path == "/api/session"]

        def test_s8_the_pod_session_carries_the_model_and_a_launch_without_options_does_not(self):
            code, out, err = self.launch_with("--cloud", "--model", "github-copilot/model-a", "--reasoning", "medium")
            self.assertEqual(code, 0, err)
            (body,) = self.session_bodies()
            self.assertEqual(body["model"], {"providerID": "github-copilot", "id": "model-a", "variant": "medium"})
            self.assertEqual(body["agent"], "cloud-worker" if "cloud-worker" in body["agent"] else body["agent"])
            record = body["metadata"]["fleet"]
            self.assertEqual((record["model"], record["reasoning"]), ("github-copilot/model-a", "medium"))

        def test_s8_a_launch_with_neither_option_has_no_model_key_and_no_record_keys(self):
            code, out, err = self.launch_with("--cloud")
            self.assertEqual(code, 0, err)
            (body,) = self.session_bodies()
            self.assertNotIn("model", body)
            self.assertFalse({"model", "reasoning"} & set(body["metadata"]["fleet"]))

        def test_u8_another_providers_model_is_refused_before_any_token_or_cluster_call(self):
            code, out, err = self.launch_with("--cloud", "--model", "acme-provider/model-a")
            self.assertNotEqual(code, 0)
            lines = [line for line in err.splitlines() if "--model" in line]
            self.assertEqual(len(lines), 1, err)
            self.assertIn("a cloud worker holds credentials for the provider github-copilot only", lines[0])
            self.assertIn("#1085", lines[0])
            self.assertEqual(self.stub_calls(), [])
            self.assertEqual(self.remote.requests, [])

        def test_u8_a_level_the_pod_does_not_list_is_the_pods_refusal_in_one_line(self):
            handler = self.remote.server.RequestHandlerClass
            real = handler.do_POST

            def post(h):
                if h.path == "/api/session":
                    length = int(h.headers.get("Content-Length") or 0)
                    body = json.loads(h.rfile.read(length))
                    self.remote.bodies.append(("POST", h.path, body))
                    if (body.get("model") or {}).get("variant") == "xhigh":
                        return h._answer(400, {"message": "variant xhigh is not listed for model-a"})
                    return h._answer(200, {"data": {"id": "ses_remote1"}})
                return real(h)
            handler.do_POST = post
            code, out, err = self.launch_with("--cloud", "--model", "github-copilot/model-a", "--reasoning", "xhigh")
            self.assertNotEqual(code, 0)
            lines = [line for line in err.splitlines() if "xhigh" in line]
            self.assertEqual(len(lines), 1, err)
            self.assertIn("variant xhigh is not listed for model-a", lines[0])
            self.assertFalse((self.state / "cloud" / "coder7.json").exists(), "the launch was undone")

    return TestLaunchModelCloud
