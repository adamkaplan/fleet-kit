#!/usr/bin/env python3
"""cw-image-proof (ask #2418): prove a worker image in a proof pod before cw-image-roll may roll it.

    bin/cloud_image_proof.py DIGEST --repo OWNER/NAME [--branch B] [--wait SECONDS]

DIGEST is `<registry>/<path>@sha256:<64 hex>`. The command (1) reads the image's revision label with `crane config`, (2) launches ONE proof pod on exactly that
digest through the launcher's own launch path, `cloud-worker create --image DIGEST --no-commit` (`--image` is an argument of that launch only: the settings file
and its CW_CLOUD_IMAGE line are never read for the image and never written, so the settings clash rule does not apply), (3) sends the brief
cloud/worker/proof-set.md into the pod and reads the pod's results file /work/proof-results.txt by `kubectl exec` (the program is CW_KUBECTL, the namespace
CW_CLOUD_NAMESPACE), (4) ALWAYS retires the pod with `cloud-worker stop`, whatever the verdict, and (5) writes the receipt in the operator's state directory
(mode 0600): the digest, the time, the revision and one line per item of the proof set. A missing, misspelled or red item makes the verdict `fail` and names it.
Exit 0 for a verdict pass, 1 for a fail or a refusal (one line). The receipt is what cw-image-roll reads; nothing here changes the settings file.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

ITEMS = ("pg-env", "hook", "python311", "run-wake", "tests", "identity")   # the six items of cloud/worker/proof-set.md
RESULTS_PATH = "/work/proof-results.txt"
REVISION_LABEL = "org.opencontainers.image.revision"
DIGEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@(sha256:[0-9a-f]{64})")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(globals().get("__file__") or os.path.join(os.getcwd(), "bin", "x"))))


class Refused(Exception):
    pass


def receipt_dir(env):
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "fl" + "eet-switchboard", "cloud", "image-proof")


def judge(results_text):
    """(verdict, [(item, pass|fail)]) from the pod's results file: an item is `pass` only on exactly the line `<item> pass`; missing, doubled or red is `fail`."""
    seen = {}
    for line in (results_text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ITEMS:
            seen.setdefault(parts[0], []).append(parts[1])
    rows = [(item, "pass" if seen.get(item) == ["pass"] else "fail") for item in ITEMS]
    return ("pass" if all(r == "pass" for _, r in rows) else "fail"), rows


def receipt_text(digest, revision, now, verdict, rows):
    lines = ["digest: %s" % digest, "time: %s" % now.strftime("%Y-%m-%dT%H:%M:%SZ"), "revision: %s" % revision, "verdict: %s" % verdict]
    lines += ["item %s %s" % row for row in rows]
    red = [item for item, result in rows if result != "pass"]
    if red:
        lines.append("failed: %s" % ", ".join(red))
    return "\n".join(lines) + "\n"


def write_receipt(directory, digest, text, now):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    name = "%s-%s.receipt" % (digest[7:19], now.strftime("%Y%m%dT%H%M%SZ"))
    path = os.path.join(directory, name)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def run(argv, env, timeout, stdin=None):
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def read_revision(env, source):
    done = run([env.get("CRANE") or "crane", "config", source], env, 120)
    try:
        labels = (json.loads(done.stdout).get("config") or {}).get("Labels") or {} if done is not None and done.returncode == 0 else {}
    except (ValueError, AttributeError):
        labels = {}
    value = labels.get(REVISION_LABEL) if isinstance(labels, dict) else ""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else ""


def prove(args, env, now=None, sleep=time.sleep):
    now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
    match = DIGEST_RE.fullmatch(args.digest)
    if not match or not REPO_RE.fullmatch(args.repo):
        raise Refused("usage: DIGEST (name@sha256:<64 hex>) and --repo OWNER/NAME")
    digest = match.group(1)
    launcher = env.get("CW_CLOUD_LAUNCHER") or os.path.join(ROOT, "cloud", "launcher", "cloud-worker")
    kubectl = env.get("CW_KUBECTL") or "kubectl"
    revision = read_revision(env, args.digest)
    worker = "proof-%s" % digest[7:19]
    made = run([launcher, "create", worker, "--repo", args.repo, "--branch", args.branch, "--image", args.digest, "--no-commit", "--json"], env, 900)
    if made is None or made.returncode != 0:
        raise Refused("the proof pod could not be launched (exit %s); no receipt was written" % ("?" if made is None else made.returncode))
    results = ""
    try:
        try:
            pod = json.loads(made.stdout).get("pod") or worker
        except (ValueError, AttributeError):
            pod = worker
        ns = ["-n", env["CW_CLOUD_NAMESPACE"]] if env.get("CW_CLOUD_NAMESPACE") else []
        with open(os.path.join(ROOT, "cloud", "worker", "proof-set.md"), encoding="utf-8") as f:
            brief = f.read()
        run([kubectl] + ns + ["exec", "-i", pod, "--", "sh", "-c", "cat > /work/proof-set.md"], env, 120, stdin=brief)
        deadline = time.monotonic() + args.wait
        while True:
            got = run([kubectl] + ns + ["exec", pod, "--", "cat", RESULTS_PATH], env, 120)
            if got is not None and got.returncode == 0 and got.stdout.strip():
                results = got.stdout
                break
            if time.monotonic() >= deadline:
                break
            sleep(10)
    finally:
        run([launcher, "stop", worker, "--reason", "image proof finished"], env, 300)   # retired whatever the verdict
    verdict, rows = judge(results)
    moment = now()
    path = write_receipt(receipt_dir(env), digest, receipt_text(digest, revision, moment, verdict, rows), moment)
    red = [item for item, result in rows if result != "pass"]
    return verdict, "proof %s for %s: verdict %s%s; receipt %s" % (digest[7:19], revision[:12] or "no revision", verdict, " (failed: %s)" % ", ".join(red) if red else "", path)


def main(argv=None, env=None, sleep=time.sleep):
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="cw-image-proof", description=__doc__.split("\n")[0])
    ap.add_argument("digest")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="integration")
    ap.add_argument("--wait", type=int, default=1800)
    args = ap.parse_args(argv)
    try:
        verdict, line = prove(args, env, sleep=sleep)
    except Refused as why:
        print("cw-image-proof: refused: %s" % why, file=sys.stderr)
        return 1
    print(line)
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
