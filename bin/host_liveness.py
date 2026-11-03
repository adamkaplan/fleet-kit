"""host_liveness — is the agent a charter names (`host:<hostname>/<agent>`) on that host's list of agents? (ask #1984, scenario S7)

Three answers: alive (the agent is on the host's list), dead (the host answered and the agent is absent) and unreachable (the host could not
be asked: neither alive nor dead). `check` never raises.

The default read: on THIS host the registry read of bin/registry.py (read-only, no lock): the agents that hold a session. On another host ssh
(5-second limit, BatchMode, no password, no credential read or written here) runs the switchboard's read-only `status --json` there and takes
the agent names of its overview. Any failure is unreachable.
"""
import json
import os
import socket
import subprocess
import sys

SSH_SECONDS = 5
NAME = "[A-Za-z0-9][A-Za-z0-9._-]{0,63}"


def _local(state_dir=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import registry
    state = state_dir or os.environ.get("FLEET_REGISTRY_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "fleet-switchboard")
    got = registry.read(state)
    if not got.get("whole"):
        raise RuntimeError("the registry was not read whole")
    return [name for name, row in got["agents"].items() if isinstance(row, dict) and row.get("session")]


def _remote(host, runner=subprocess.run):
    done = runner(["ssh", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no", "-o", "ConnectTimeout=%d" % SSH_SECONDS,
                   "-o", "NumberOfPasswordPrompts=0", "--", host, "fleet-switchboard", "status", "--json"],
                  capture_output=True, text=True, timeout=SSH_SECONDS)
    if done.returncode != 0:
        raise RuntimeError("ssh exited with status %s" % done.returncode)
    report = json.loads(done.stdout)
    agents = report["agents"]
    # a live agent has an `observation` only when the host's discovery worked: with problems (herdr or v2 unreachable there) or a report that is not ok, the list proves nothing
    if report.get("ok") is not True or agents.get("problems") != []:
        raise RuntimeError("the host's overview says its discovery had problems")
    entries = agents["list"]
    # an entry with no `observation` is only the recipient of a pending message: it has no session, so it is not alive
    return [e.get("name") for e in entries if isinstance(e, dict) and e.get("name") and e.get("observation")]


def default_ask(host, agent, this_host=None, remote=_remote, local=_local):
    if host == (this_host or socket.gethostname()):
        return local()
    return remote(host)


def check(host, agent, ask=None, this_host=None):
    try:
        import re
        if not (re.fullmatch(NAME, str(host)) and re.fullmatch(NAME, str(agent))):
            return {"verdict": "unreachable", "why": "the host or the agent name is not well formed"}
        names = ask(host, agent) if ask else default_ask(host, agent, this_host)
        if not isinstance(names, (list, tuple, set)):
            return {"verdict": "unreachable", "why": "the host's answer was not a list"}
        if agent in names:
            return {"verdict": "alive", "why": "the agent is on the host's list"}
        return {"verdict": "dead", "why": "the host answered and the agent is not on its list"}
    except BaseException as err:   # never raises
        return {"verdict": "unreachable", "why": "the host could not be asked: %s" % type(err).__name__}
