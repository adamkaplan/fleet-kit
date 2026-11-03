"""#2074: an orchestrator can run with no terminal: its charter says `pane: headless:<name>` and the doctor reads it from the service
(`fleet-switchboard pending NAME --json`, a read): alive while the service shows the session, dead only when it does not, unknown when unreadable."""
import importlib.machinery
import importlib.util
import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "bin"


def _doctor_module():
    loader = importlib.machinery.SourceFileLoader("fd_tests_2074", str(BIN / "test-fleet-doctor"))
    spec = importlib.util.spec_from_loader("fd_tests_2074", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


FD = _doctor_module()


class DoctorHeadlessCharter(unittest.TestCase):
    """S2: the doctor on a `headless:<name>` charter (a fake service answers `pending`)."""

    def charter_run(self, answer):
        tmp = tempfile.mkdtemp(prefix="test-headless-orch.")
        self.addCleanup(shutil.rmtree, tmp, True)
        stub = Path(tmp) / "sb-stub"
        stub.write_text("#!/bin/sh\n[ \"$1\" = pending ] && { %s; }\nexit 0\n" % answer)
        stub.chmod(0o755)
        run = FD.DoctorRun(tmp, {"herdr": "  echo '{\"result\": {\"panes\": []}}'", "gh": "  true"})
        (run.path_dir / "herdr").write_text("#!/bin/sh\n[ \"$1\" = \"--version\" ] && echo 'herdr 1.0.0' && exit 0\necho '{\"result\": {\"panes\": []}}'\n")
        (run.path_dir / "gh").write_text("#!/bin/sh\ncase \"$*\" in\n  *'issue list'*'--label'*) cat \"%s\";;\n  *'api repos'*) echo true;;\n  *) echo 1;;\nesac\nexit 0\n" % (Path(tmp) / "issues.json"))
        body = "```yaml\nstatus: active\npane: headless:orch-hl\n```"
        (Path(tmp) / "issues.json").write_text(json.dumps([{"number": 7, "title": "charter", "body": body}]))
        state = Path(tmp) / "state"
        state.mkdir()
        sock = Path(tmp) / "h.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(sock))
        self.addCleanup(listener.close)
        run.extra_env.update({"HERDR_SOCKET_PATH": str(sock), "FLEET_REPO": "acme-org/widget-service", "FLEET_REGISTRY_DIR": str(state), "FLEET_SWITCHBOARD_BIN": str(stub)})
        run.run()
        return run

    def text(self, run):
        return "\n".join(run.lines("charters"))

    def test_a_headless_orchestrator_is_alive_while_the_service_shows_it(self):
        run = self.charter_run("echo '{\"observation\": {\"name\": \"orch-hl\", \"status\": \"idle\", \"headless\": true}}'")
        text = self.text(run)
        self.assertIn("1 headless charter(s) alive", text, "a headless orchestrator is alive while the service shows it\n" + run.output)
        self.assertNotIn("dead pane", text)

    def test_a_headless_orchestrator_the_service_does_not_show_is_dead(self):
        run = self.charter_run("echo '{\"observation\": null}'")
        self.assertIn("#7->headless:orch-hl", self.text(run), run.output)

    def test_an_unreadable_answer_is_unknown_never_dead(self):
        run = self.charter_run("echo 'not json'")
        text = self.text(run)
        self.assertIn("unknown", text, run.output)
        self.assertNotIn("dead pane", text)


if __name__ == "__main__":
    unittest.main()
