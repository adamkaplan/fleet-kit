"""The live model credential, read from OpenCode's OWN store (#1088): the fixed command `opencode auth export`, run in a scrubbed environment, its
output taken through a pipe and kept in memory only (never a file of its own, never printed), parsed as a JSON array and cut down to the ONE entry
of the provider the launcher is told to copy. The result is that one entry as a one-element JSON array, the form `opencode auth import` reads.

The legacy auth file is NOT read: the server ignores it (it reads its own database), so a copy of it gave a pod with no subscription models.

Every refusal is a ModelCredentialError whose text names the provider and the reason, never a value. Stdlib only; shared by the driver
(bin/cloud_worker.py) and the run-task launcher (cloud/launcher/run_task.py)."""
import json
import os
import shutil
import signal
import subprocess

DEFAULT_BINARY = "opencode"
EXPORT_ARGV = ("auth", "export")   # the whole argument list: no other argument, no shell
TIMEOUT_SECONDS = 30
OUTPUT_LIMIT = 1000000
ENV_KEEP = ("PATH", "HOME", "LANG")   # and every XDG_* variable: nothing of the cluster, a token or CW_*
ENTRY_TYPE = "oauth"


class ModelCredentialError(Exception):
    pass


def scrubbed_env(env):
    return {k: v for k, v in env.items() if k in ENV_KEEP or k.startswith("XDG_")}


def _refuse(provider, reason):
    raise ModelCredentialError("the model credential for %s could not be read from OpenCode's own store (%s); nothing was created" % (provider, reason))


def read_provider_entry(provider, env=None):
    """The one-entry export array (JSON text) for `provider`. `env` is the caller's environment (default os.environ): CW_OPENCODE_BIN names the
    binary, else `opencode` on its PATH. Raises ModelCredentialError."""
    env = os.environ if env is None else env
    child_env = scrubbed_env(env)
    binary = (env.get("CW_OPENCODE_BIN") or "").strip() or DEFAULT_BINARY
    found = shutil.which(binary, path=child_env.get("PATH") or os.defpath)
    if not found:
        _refuse(provider, "the command %s was not found; install OpenCode or set CW_OPENCODE_BIN" % os.path.basename(binary))
    try:
        child = subprocess.Popen([found, *EXPORT_ARGV], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=child_env, start_new_session=True)
    except OSError:
        _refuse(provider, "the export command could not be run")
    try:
        out, _ = child.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        child.communicate()
        _refuse(provider, "the export command timed out")
    if child.returncode != 0:
        _refuse(provider, "the export command failed with exit status %d" % child.returncode)
    if len(out) > OUTPUT_LIMIT:
        _refuse(provider, "the export output is over %d bytes" % OUTPUT_LIMIT)
    try:
        entries = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        entries = None
    del out
    if not isinstance(entries, list):
        _refuse(provider, "the export output is not a JSON array")
    mine = [e for e in entries if isinstance(e, dict) and e.get("integrationID") == provider]
    if not mine:
        _refuse(provider, "the export has no entry for this provider; sign in with OpenCode first")
    if len(mine) > 1:
        _refuse(provider, "the export has %d entries for this provider; expected one" % len(mine))
    entry = mine[0]
    if entry.get("active") is not True:
        _refuse(provider, "the entry is not active")
    value = entry.get("value")
    if not isinstance(value, dict) or value.get("type") != ENTRY_TYPE:
        _refuse(provider, "the entry is not of type %s" % ENTRY_TYPE)
    return json.dumps([entry])
