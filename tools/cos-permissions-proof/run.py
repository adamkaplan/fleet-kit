#!/usr/bin/env python3
"""Replay a corpus of shell commands through the REAL permission rules of agents/opencode/chief-of-staff.md.

A throwaway `opencode run --standalone` (private server, its own HOME and XDG directories under a scratch directory: never the live background
service, never ~/.config) talks to a scripted mock provider (mock_provider.py), so no model is involved. Every command word of the corpus is a SHIM
that only logs "RAN <name> <args>", so even a rule that wrongly allows something runs nothing real. A spy plugin logs each permission evaluation
(action, scanner resources, effect). Per command the proof says: RAN (a shim logged an execution), ASKED (a rule asked; the non-interactive run rejected it),
DENIED (nothing ran and nothing asked: a hard denial; the evaluate hook does not log denials), or ERROR. Exit status 1 when any HOSTILE command RAN (other than the corpus's listed `harmless` read-only ones) or any command was ASKED:
the chief of staff's rules only allow or deny, they never ask.

  run.py [--corpus corpus.json] [--work DIR] [--batch 20] [--jobs 3] [--out result.json] [--only allowed|hostile] [--limit N]
Needs: `opencode` v2 on PATH, python3, nice. Takes about a minute per hundred commands per job.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DEFINITION = os.path.join(REPO, "agents", "opencode", "chief-of-staff.md")
SPY = r'''import fs from "node:fs";
const LOG = process.env.PROOF_LOG;
export default {
  id: "proof.spy",
  async setup(api) {
    api.permission.hook("evaluate", async (input) => {
      try { fs.appendFileSync(LOG, "EVAL " + JSON.stringify({ action: input.action, resources: input.resources, effect: input.effect }) + "\n"); } catch {}
    });
  },
};
'''
BUILTINS = {"cd", "echo", "eval", "exec", "source", ".", "alias", "kill", "pwd", "time", "command", "true", "false", "test", "[", "export", "set", "unset", "read", "type", "wait", "exit", "trap", "umask", "ulimit", "builtin", "printf", "fg", "bg", "jobs", "local", "return", "shift", "history", "hash", "let", "getopts", "typeset", "declare", "readonly", "nohup", "coproc", "noglob", "nocorrect", "rehash", "whence", "where", "which", "functions", "unalias", "setopt", "unsetopt", "emulate", "print", "pushd", "popd", "dirs", "disown", "enable", "compgen", "complete", "bind", "caller", "mapfile", "readarray", "logout", "suspend", "times", "ulimit", "fc", "if", "then", "else", "fi", "for", "while", "do", "done", "case", "esac", "function", "select", "until", "in", "{", "}", "!", "[[", "((", "env"}
SAFE_SUBSTITUTIONS = [(r"^kill\b", "kill 99999999")]


def words_of(command):
    return {w for w in re.findall(r"(?:^|[;&|(\n]|&&|\|\||\$\(|`)\s*(?:\w+=\S*\s+)*([A-Za-z_][A-Za-z0-9_.+-]*)", command)}


def sanitize(command):
    for pattern, repl in SAFE_SUBSTITUTIONS:
        command = re.sub(pattern, repl, command)
    return command


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare(work, name, definition_text, port):
    root = os.path.join(work, name)
    for d in ("home/.config/opencode/plugins", "home/.local/share", "home/.local/state", "home/.cache", "proj/.opencode/agents", "shims"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    open(os.path.join(root, "home/.config/opencode/plugins/spy.js"), "w").write(SPY)
    open(os.path.join(root, "proj/.opencode/agents/chief-of-staff.md"), "w").write(definition_text)
    config = {"$schema": "https://opencode.ai/config.json", "model": "mock/mock", "default_agent": "chief-of-staff",
              "providers": {"mock": {"name": "Mock", "package": "@opencode/ai/providers/openai-compatible",
                                     "settings": {"baseURL": "http://127.0.0.1:%d/v1" % port, "apiKey": "x"},
                                     "models": {"mock": {"name": "Mock", "capabilities": {"tools": True}}}}}}
    json.dump(config, open(os.path.join(root, "proj/opencode.jsonc"), "w"), indent=1)
    return root


def make_shims(root, names, log):
    for name in sorted(names):
        path = os.path.join(root, "shims", name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nprintf 'RAN %%s\\n' \"%s $*\" >> \"%s\"\necho shim-%s-ran\n" % (name, log, name))
        os.chmod(path, 0o755)


def run_batch(work, name, commands, definition_text, timeout):
    port = free_port()
    root = prepare(work, name, definition_text, port)
    log = os.path.join(root, "events.log")
    open(log, "w").close()
    names = set()
    for c in commands:
        names |= words_of(c)
    names -= BUILTINS
    make_shims(root, names | {"gh", "git", "curl", "az", "fleet-switchboard", "ls", "cat", "head", "tail", "wc", "grep"}, log)
    cmds = os.path.join(root, "commands.json")
    json.dump([sanitize(c) for c in commands], open(cmds, "w"))
    mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_provider.py"), str(port), cmds, log], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        proj = os.path.join(root, "proj")
        env = {"PATH": os.path.join(root, "shims") + ":/usr/bin:/bin", "HOME": os.path.join(root, "home"), "PWD": proj, "PROOF_LOG": log,
               "XDG_CONFIG_HOME": os.path.join(root, "home/.config"), "XDG_DATA_HOME": os.path.join(root, "home/.local/share"),
               "XDG_STATE_HOME": os.path.join(root, "home/.local/state"), "XDG_CACHE_HOME": os.path.join(root, "home/.cache"), "TERM": "dumb",
               "OPENCODE_BIN_DIR": os.path.dirname(shutil.which("opencode") or "")}
        argv = ["/usr/bin/nice", "-n", "10", shutil.which("opencode"), "run", "--standalone", "--agent", "chief-of-staff", "--model", "mock/mock", "--format", "json", "go"]
        try:
            subprocess.run(argv, cwd=proj, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
    finally:
        mock.kill()
    cases, current = [], None
    for line in open(log).read().splitlines():
        if line.startswith("MOCK "):
            current = {"n": int(line.split()[1]), "evals": [], "ran": []}
            cases.append(current)
        elif current is not None and line.startswith("EVAL "):
            current["evals"].append(json.loads(line[5:]))
        elif current is not None and line.startswith("RAN "):
            current["ran"].append(line[4:])
    results = []
    for i, command in enumerate(commands):
        case = next((c for c in cases if c["n"] == i), None)
        if case is None:
            results.append({"command": command, "verdict": "NOT-SERVED"})
            continue
        effects = [(e["action"], e["effect"]) for e in case["evals"]]
        verdict = "RAN" if case["ran"] else "ASKED" if any(e == "ask" for _, e in effects) else "DENIED"
        results.append({"command": command, "verdict": verdict, "ran": case["ran"], "evals": case["evals"]})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", default=os.path.join(HERE, "corpus.json"))
    parser.add_argument("--definition", default=DEFINITION)
    parser.add_argument("--work")
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--out")
    parser.add_argument("--only", choices=("allowed", "hostile"))
    parser.add_argument("--limit", type=int)
    a = parser.parse_args()
    if not shutil.which("opencode"):
        sys.exit("run.py: opencode is not on PATH")
    corpus = json.load(open(a.corpus))
    text = open(a.definition).read().replace("__PROVIDER__/__MODEL_ID__", "mock/mock")
    work = a.work or tempfile.mkdtemp(prefix="cos-permissions-proof-")
    jobs = []
    for kind in ("allowed", "hostile"):
        if a.only and a.only != kind:
            continue
        commands = corpus[kind][:a.limit] if a.limit else corpus[kind]
        for i in range(0, len(commands), a.batch):
            jobs.append((kind, i, commands[i:i + a.batch]))
    results = {"allowed": [], "hostile": []}
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        futures = [(kind, pool.submit(run_batch, work, "%s-%d" % (kind, i), batch, text, a.timeout)) for kind, i, batch in jobs]
        for kind, future in futures:
            results[kind].extend(future.result())
    count = lambda kind, verdict: sum(1 for r in results[kind] if r["verdict"] == verdict)
    for kind in ("allowed", "hostile"):
        if results[kind]:
            print("%s: %d commands: RAN %d, ASKED %d, DENIED %d, ERROR %d, NOT-SERVED %d" % (kind, len(results[kind]), count(kind, "RAN"), count(kind, "ASKED"),
                  count(kind, "DENIED"), count(kind, "ERROR"), count(kind, "NOT-SERVED")))
    harmless = set(corpus.get("harmless", []))
    bad = [r for r in results["hostile"] if r["verdict"] == "RAN" and r["command"] not in harmless]
    asked = [r for kind in results for r in results[kind] if r["verdict"] == "ASKED"]
    for r in asked:
        print("ASKED (the chief of staff must never be asked):", json.dumps(r["command"]))
    for r in bad:
        print("HOSTILE RAN:", json.dumps(r["command"]), r["ran"])
    for r in results["allowed"]:
        if r["verdict"] != "RAN":
            print("allowed but %s: %s" % (r["verdict"], json.dumps(r["command"])))
    for kind in results:
        for r in results[kind]:
            if r["verdict"] in ("ERROR", "NOT-SERVED"):
                print("check by hand (%s): %s %s" % (kind, r["verdict"], json.dumps(r["command"])))
    if a.out:
        json.dump(results, open(a.out, "w"), indent=1)
    if not a.work:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(1 if bad or asked else 0)


if __name__ == "__main__":
    main()
