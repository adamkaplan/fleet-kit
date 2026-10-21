"""retire_token: the fresh repository token the launcher hands to the end hook at retire time (never stored, never printed).

`configured(env)` says whether this launcher can mint at all (the mint command or the vault/App settings are present); a
launcher with none behaves as before the change. `mint(env, repo)` runs the same command the launch uses (CW_GIT_TOKEN_CMD,
default app_token.py OWNER/NAME) and returns the token held in memory, or None when it gave nothing usable. The reason is
never returned: a failed mint says only that it failed.
"""
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MINT_SETTINGS = ("CW_GIT_TOKEN_CMD", "CW_VAULT_NAME", "CW_APP_ID_CMD")
MAX_TOKEN = 4096

# The step names a retire failure message uses, and the read-only route out of a kept pod (docs: three files carry it).
STEP_MINT, STEP_PUSH = "mint", "push"
RECOVERY = ("the pod and its work are kept; copy the work out read-only by running "
            "`git -C /work/repo bundle create - --all` in the pod (git bundle; the docs' retire-path section has the full route), "
            "then `retire` again or `delete --force` once it is safe")


def configured(env):
    return any((env.get(k) or "").strip() for k in MINT_SETTINGS)


def mint(env, repo):
    raw = (env.get("CW_GIT_TOKEN_CMD") or "").strip()
    try:
        cmd = shlex.split(raw) if raw else [sys.executable, os.path.join(HERE, "app_token.py"), repo]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env=dict(os.environ, **dict(env, GH_REPO=repo)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    token = (p.stdout or "").strip() if p.returncode == 0 else ""
    return token if token and len(token) <= MAX_TOKEN and "\n" not in token else None
