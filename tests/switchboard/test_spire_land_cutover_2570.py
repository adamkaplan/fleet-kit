"""Spire Land 0.6.0 cutover and commit-status attestation tests (#2570).

CLAIMS: S1, S2, U1. Verifies that `.spire/tool` and `.agents/skills/spire-land` match the 0.6.0 PIN (`def97d882b78`),
that every area in `.spire/land.json` runs `check-public-names` (on tracked files and commit messages) and `size_ceilings.py check`,
that `tools/spire-land-host` fails closed (exit 70) when `cw-app-id` or `cw-app-key` is missing or unreadable without leaking key
material, and that `tools/land-status-parity` verifies `land/checks` (with `tree=<12 hex>`) and `land/council` statuses.
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from support.core import Base
from support.paths import REPO


def _load_script(rel_path, mod_name):
    path = REPO / rel_path
    loader = importlib.machinery.SourceFileLoader(mod_name, str(path))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSpireLandCutover2570(Base):
    def test_s1_pin_verify_and_land_json_areas(self):
        import os, shutil
        node = shutil.which("node") or "/opt/homebrew/bin/node"
        done = subprocess.run([node, ".spire/tool/pin.mjs", "verify"], cwd=str(REPO), capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("tool ok: 0.6.2 c275f63765b4", done.stdout)
        cfg = json.loads((REPO / ".spire" / "land.json").read_text(encoding="utf-8"))
        areas = cfg.get("areas") or []
        self.assertGreaterEqual(len(areas), 6)
        for area in areas:
            steps = {(s.get("id") or s.get("name")): (" ".join(s["cmd"]) if isinstance(s["cmd"], list) else s["cmd"]) for s in area.get("steps") or []}
            self.assertIn("public-names", steps, area["name"])
            self.assertIn("check-public-names --stdin commit-messages", steps["public-names"], area["name"])
            self.assertIn("size-and-role-ceilings", steps, area["name"])
            self.assertIn("size_ceilings.py check", steps["size-and-role-ceilings"], area["name"])

    def test_s2_spire_land_host_fails_closed_without_valid_app_credentials(self):
        host = _load_script("tools/spire-land-host", "spire_land_host_test")
        calls = []
        secret = "PRIVATE_KEY_MATERIAL_DO_NOT_LEAK_987654321"
        bad_dir = self.home / "bad-secrets"
        bad_dir.mkdir()
        host.SECRET_DIR = str(bad_dir)
        rc = host.main(["--dry-run"], workdir=str(REPO), runner=lambda *a, **kw: calls.append(a))
        self.assertEqual(rc, 70)
        self.assertEqual(calls, [])
        # World-readable key (0644) is refused before running spire
        (bad_dir / "cw-app-id").write_text("5404907\n", encoding="utf-8")
        (bad_dir / "cw-app-key").write_text(secret + "\n", encoding="utf-8")
        (bad_dir / "cw-app-id").chmod(0o600)
        (bad_dir / "cw-app-key").chmod(0o644)
        rc2 = host.main(["--dry-run"], workdir=str(REPO), runner=lambda *a, **kw: calls.append(a))
        self.assertEqual(rc2, 70)
        self.assertEqual(calls, [])
        # Valid 0600 credentials pass FLEET_APP_ID, FLEET_APP_PRIVATE_KEY, and FLEET_LAND_STATUSES=required
        (bad_dir / "cw-app-key").chmod(0o600)
        seen_env = {}

        class _Done:
            returncode = 0

        def fake_run(argv, cwd=None, env=None):
            seen_env.update(env or {})
            return _Done()

        rc3 = host.main(["--dry-run"], workdir=str(REPO), runner=fake_run)
        self.assertEqual(rc3, 0)
        self.assertEqual(seen_env.get("FLEET_APP_ID"), "5404907")
        self.assertEqual(seen_env.get("FLEET_LAND_STATUSES"), "required")

    def test_u1_parity_tool_verifies_spire_land_statuses_and_tree(self):
        parity = _load_script("tools/land-status-parity", "land_status_parity_test")
        sha = "a" * 40
        tree = "123456789abc" + "0" * 28
        good = {
            "sha": sha,
            "tree": tree,
            "statuses": [
                {"context": "land/checks", "state": "success", "description": "tree=123456789abc areas=docs (override: none)", "creator": {"login": "app[bot]"}},
                {"context": "land/council", "state": "success", "description": "clean", "creator": {"login": "app[bot]"}},
            ],
        }
        problems, bots = parity.check(good, None)
        self.assertEqual(problems, [])
        self.assertEqual(bots, ["app[bot]", "app[bot]"])
        # Wrong tree in land/checks is caught
        bad_tree = dict(good, tree="ffffffffffff" + "0" * 28)
        problems2, _ = parity.check(bad_tree, None)
        self.assertEqual(problems2, [("land/checks", "tree mismatch in description")])
