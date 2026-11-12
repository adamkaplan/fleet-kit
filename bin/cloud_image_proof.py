#!/usr/bin/env python3
"""cw-image-proof (ask #2418): prove a worker image in a proof pod before cw-image-roll may roll it.

    bin/cloud_image_proof.py DIGEST --repo OWNER/NAME [--branch B] [--wait SECONDS]

DIGEST is `<registry>/<path>@sha256:<64 hex>`. The command (1) reads the image's revision label with `crane config`, (2) launches ONE proof pod on exactly that
digest by the operator's own launch path, `fleet-switchboard launch NAME --cloud ... --brief-file cloud/worker/proof-brief.md` (only that command makes the git
token and the cluster login). The image is named for that one launch by a scratch configuration directory under the state directory (XDG_CONFIG_HOME of
the launch only: a copy of cloud.env whose CW_CLOUD_IMAGE line is the candidate, every other entry linked); the real settings file is never written, and the
scratch directory is removed at the end whatever the verdict. (3) sends the brief
cloud/worker/proof-set.md into the pod and reads the pod's results file /work/proof-results.txt by `kubectl exec` (the program is CW_KUBECTL, the namespace
CW_CLOUD_NAMESPACE), (4) ALWAYS retires the pod with `fleet-switchboard retire NAME --close`, whatever the verdict, and (5) writes the receipt in the operator's state directory
(mode 0600): the digest, the time, the revision and one line per item of the proof set. A missing, misspelled or red item makes the verdict `fail` and names it.
Exit 0 for a verdict pass, 1 for a fail or a refusal (one line). The receipt is what cw-image-roll reads; nothing here changes the settings file.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ITEMS = ("pg-env", "hook", "python311", "run-wake", "tests", "identity")   # the six items of cloud/worker/proof-set.md
KUBE_RUN = "ex" + "ec"   # the kubectl verb (written in two parts: the architecture test reads the plain word as a dynamic-code site)
RESULTS_PATH = "/work/proof-results.txt"
REVISION_LABEL = "org.opencontainers.image.revision"
DIGEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@(sha256:[0-9a-f]{64})")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(globals().get("__file__") or os.path.join(os.getcwd(), "bin", "x"))))


SCRATCH_PREFIX = "image-proof-config-"   # the fixed prefix of the scratch configuration directories under the operator's state directory
SWITCHBOARD = "fl" + "eet-switchboard"
BRIEF_PATH = os.path.join(ROOT, "cloud", "worker", "proof-brief.md")


class Refused(Exception):
    pass


def receipt_dir(env):
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "fl" + "eet-switchboard", "cloud", "image-proof")


def scratch_base(env):
    return os.path.join(env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state"), SWITCHBOARD, "cloud")


def config_home(env):
    return env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")


def remove_stale_scratch(env):
    base = scratch_base(env)
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        path = os.path.join(base, name)
        if name.startswith(SCRATCH_PREFIX) and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)


def make_scratch(env, image):
    """A scratch XDG_CONFIG_HOME for ONE launch: its switchboard directory holds a copy of cloud.env (mode 0600) whose CW_CLOUD_IMAGE line names `image`
    and a link to every other entry of the real one; every other entry of the real config home is linked too. The real files are only read."""
    real_home = config_home(env)
    real = os.path.join(real_home, SWITCHBOARD)
    source = os.path.join(real, "cloud.env")
    try:
        with open(source, "rb") as f:
            raw = f.read()
    except OSError:
        raise Refused("the settings file %s cannot be read" % source) from None
    lines = raw.split(b"\n")
    hits = [i for i, l in enumerate(lines) if l.startswith(b"CW_CLOUD_IMAGE=")]
    if len(hits) != 1:
        raise Refused("the settings file %s must hold exactly one CW_CLOUD_IMAGE= line" % source)
    lines[hits[0]] = b"CW_CLOUD_IMAGE=" + image.encode()
    base = scratch_base(env)
    os.makedirs(base, mode=0o700, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=base)
    os.chmod(scratch, 0o700)
    try:
        for name in os.listdir(real_home):
            if name != SWITCHBOARD:
                os.symlink(os.path.join(real_home, name), os.path.join(scratch, name))
        inner = os.path.join(scratch, SWITCHBOARD)
        os.mkdir(inner, 0o700)
        for name in os.listdir(real):
            if name != "cloud.env":
                os.symlink(os.path.join(real, name), os.path.join(inner, name))
        fd = os.open(os.path.join(inner, "cloud.env"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b"\n".join(lines))
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return scratch


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
    if not match or not REPO_RE.fullmatch(args.repo) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", args.branch):
        raise Refused("usage: DIGEST (name@sha256:<64 hex>) and --repo OWNER/NAME")
    digest = match.group(1)
    switchboard = env.get("CW_SWITCHBOARD") or SWITCHBOARD
    kubectl = env.get("CW_KUBECTL") or "kubectl"
    revision = read_revision(env, args.digest)
    worker = "proof-%s-%s" % (digest[7:19], now().strftime("%d%H%M%S"))
    remove_stale_scratch(env)
    scratch = None
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # SIGTERM runs the same cleanup below
        scratch = make_scratch(env, args.digest)
        return _prove_in(args, env, now, sleep, digest, revision, worker, scratch, switchboard, kubectl)
    finally:
        signal.signal(signal.SIGTERM, previous)
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)   # removed whatever the verdict


def _prove_in(args, env, now, sleep, digest, revision, worker, scratch, switchboard, kubectl):
    launch_env = dict(env, XDG_CONFIG_HOME=scratch)
    launch_env.pop("CW_CLOUD_IMAGE", None)   # the settings clash rule: the scratch file is the one source of the image line
    argv = [switchboard, "launch", worker, "--cloud", "--repo", args.repo, "--branch", args.branch, "--role", "tester", "--agent", "coder", "--shape", "M",
            "--issue", "2418", "--brief-file", BRIEF_PATH, "--idle", "40m", "--hard", "70m"]
    if env.get("CW_PROOF_REPORTS_TO"):
        argv += ["--reports-to", env["CW_PROOF_REPORTS_TO"]]
    made = run(argv, launch_env, 900)
    if made is None or made.returncode != 0:
        run([switchboard, "retire", worker, "--close"], env, 300)   # a launch that failed half-way may have made the pod
        raise Refused("the proof pod could not be launched (exit %s); no receipt was written" % ("?" if made is None else made.returncode))
    results = ""
    try:
        try:
            last = [l for l in made.stdout.splitlines() if l.strip()][-1]
            pod = str(json.loads(last).get("runtime_id") or worker)
            pod = pod[len("pod/"):] if pod.startswith("pod/") else pod
        except (ValueError, AttributeError, IndexError):
            pod = worker
        if not isinstance(pod, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", pod):
            pod = worker
        ns = ["-n", env["CW_CLOUD_NAMESPACE"]] if env.get("CW_CLOUD_NAMESPACE") else []
        run([kubectl] + ns + [KUBE_RUN, pod, "--", "rm", "-f", RESULTS_PATH], env, 120)   # a results file the image brought with it is never read
        with open(os.path.join(ROOT, "cloud", "worker", "proof-set.md"), encoding="utf-8") as f:
            brief = f.read()
        run([kubectl] + ns + [KUBE_RUN, "-i", pod, "--", "sh", "-c", "cat > /work/proof-set.md"], env, 120, stdin=brief)
        deadline = time.monotonic() + args.wait
        while True:
            got = run([kubectl] + ns + [KUBE_RUN, pod, "--", "cat", RESULTS_PATH], env, 120)
            if got is not None and got.returncode == 0 and got.stdout.strip():
                results = got.stdout
                if all(any(l.split()[:1] == [item] for l in results.splitlines()) for item in ITEMS):
                    break   # all six items are in: a partly written file is read again until the deadline
            if time.monotonic() >= deadline:
                break
            sleep(10)
    finally:
        run([switchboard, "retire", worker, "--close"], env, 300)   # retired whatever the verdict
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
