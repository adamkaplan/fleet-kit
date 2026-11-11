#!/usr/bin/env python3
"""cw-image-roll (ask #2391): copy the worker image to the operator's registry and point the settings file at it, only when the copy is proven.

    bin/cloud_image_roll.py SOURCE@sha256:DIGEST DEST_REGISTRY TAG [--dry-run] [--settings PATH]

Runs on the operator's machine, never in the image. Steps, in this order, each refusing with ONE line on standard error and a
non-zero status, with nothing changed:
  1. the settings file must exist, be a regular file owned by you, have no mode bit beyond 0600, and hold one `CW_CLOUD_IMAGE=` line;
  2. `crane copy SOURCE DEST_REGISTRY:TAG` must exit 0;
  3. `crane digest DEST_REGISTRY:TAG` must print exactly the source digest (an empty or a different digest refuses).
Before any copy the proof gate of ask #2418 (check_gate) refuses an image whose revision label is empty or not reachable from origin/integration, or that has no
receipt of cw-image-proof with verdict pass for exactly its digest younger than 6 hours; there is no flag that skips it.
Then (not with --dry-run) the file is copied to a backup in the same directory (name with the time, mode 0600), the one
`CW_CLOUD_IMAGE=` line is replaced through a temporary file in the same directory and os.replace (mode 0600), the file is read back, and the old and
new 12-character digests and the backup path are printed. Every other line is byte for byte unchanged.
The command never logs in: the registry login is the credential store `crane` already uses. The output of `crane` is never shown
(it may hold a credential); only its exit status is. The crane tool is $CRANE, or `crane` on PATH.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys

SOURCE_RE = re.compile(r"^[^\s@\x00-\x1f\x7f-]\S*@sha256:([0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
REGISTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
SETTING = b"CW_CLOUD_IMAGE="


class Refused(Exception):
    pass


def current_uid():
    return os.getuid()


def default_settings(env):
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(base, "fl" + "eet-switchboard", "cloud.env")   # the same place as cloud_worker.settings_path


def check_settings(path):
    """The file's bytes, or Refused: missing, not a regular file, not owned by the user, mode wider than 0600, linked twice, or no single image line."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)   # one open: the checks and the read are of the same file
    except OSError:
        raise Refused("the settings file %s is missing or is a link" % path)
    with os.fdopen(fd, "rb") as f:
        return _checked(path, f)


def _checked(path, f):
    st = os.fstat(f.fileno())
    if not stat.S_ISREG(st.st_mode):
        raise Refused("the settings file %s is not a regular file" % path)
    if st.st_uid != current_uid():
        raise Refused("the settings file %s is not owned by you" % path)
    if stat.S_IMODE(st.st_mode) & 0o177:
        raise Refused("the settings file %s has a mode wider than 0600" % path)
    if st.st_nlink != 1:
        raise Refused("the settings file %s has more than one hard link" % path)
    try:
        data = f.read()
    except OSError:
        raise Refused("the settings file %s cannot be read" % path)
    if sum(1 for line in data.split(b"\n") if line.startswith(SETTING)) != 1:
        raise Refused("the settings file %s must hold exactly one CW_CLOUD_IMAGE= line" % path)
    return data


def crane_tool(env):
    tool = env.get("CRANE") or shutil.which("crane", path=env.get("PATH"))
    if not tool:
        raise Refused("crane was not found (set CRANE or put it on PATH)")
    return tool


def run_crane(tool, args, env, what, timeout):
    try:
        p = subprocess.run([tool] + args, stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("%s could not run or timed out; nothing was changed" % what)
    return p


def old_digest(data):
    for line in data.split(b"\n"):
        if line.startswith(SETTING):
            m = re.search(rb"sha256:([0-9a-f]{12})", line)
            return m.group(1).decode() if m else "none"


def replace_line(data, new_value):
    out = []
    for line in data.splitlines(keepends=True):
        if line.startswith(SETTING):
            tail = line[len(line.rstrip(b"\r\n")):]
            line = SETTING + new_value.encode() + tail
        out.append(line)
    return b"".join(out)


def apply(path, data, new_bytes, now):
    """Backup, then temporary file and rename in the same directory; returns the backup's path."""
    directory = os.path.dirname(os.path.abspath(path))
    backup = "%s.bak-%s" % (os.path.abspath(path), now.strftime("%Y%m%dT%H%M%S%f"))
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp = "%s.tmp-%d" % (os.path.abspath(path), os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    with open(path, "rb") as f:
        if f.read() != new_bytes:
            raise Refused("the settings file did not read back as written (it was replaced); restore it from the backup %s" % backup)
    return backup


REVISION_LABEL = "org.opencontainers.image.revision"
INTEGRATION_REF = "origin/integration"
RECEIPT_MAX_AGE = datetime.timedelta(hours=6)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(globals().get("__file__") or os.path.join(os.getcwd(), "bin", "x"))))   # the checkout that holds this file


def receipt_dir(env):
    """Where cw-image-proof writes its receipts (ask #2418): the operator's state directory, the launcher's own `cloud` folder."""
    base = env.get("XDG_STATE_HOME") or os.environ.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "fl" + "eet-switchboard", "cloud", "image-proof")


def read_receipts(directory, want):
    """The receipts of exactly this digest: a list of dicts (digest, time, revision, verdict). A file that cannot be read is skipped."""
    found = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".receipt"):
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
                text = f.read(65536)
        except (OSError, UnicodeDecodeError):
            continue
        fields = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line and not line.startswith("item "))
        if fields.get("digest") == want:
            found.append(fields)
    return found


def check_gate(tool, source, want, env, now):
    """Ask #2418: refuse, before any copy, an image with no revision label, a revision that is not reachable from the integration branch's remote tip,
    or without a receipt of verdict pass for exactly this digest younger than six hours. There is no flag that skips it."""
    got = run_crane(tool, ["config", source], env, "crane config", 120)
    try:
        labels = (json.loads(got.stdout.decode("utf-8", "replace")).get("config") or {}).get("Labels") or {} if got.returncode == 0 else {}
    except (ValueError, AttributeError):
        labels = {}
    revision = labels.get(REVISION_LABEL) if isinstance(labels, dict) else ""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise Refused("the image has no revision label (%s); nothing was changed" % REVISION_LABEL)
    near = run_crane(env.get("GIT") or "git", ["-C", REPO_ROOT, "merge-base", "--is-ancestor", revision, INTEGRATION_REF], env, "git merge-base", 60)
    if near.returncode != 0:
        raise Refused("the image's revision %s is not a commit reachable from %s; nothing was changed" % (revision[:12], INTEGRATION_REF))
    receipts = read_receipts(receipt_dir(env), want)
    if not receipts:
        raise Refused("no proof receipt for %s; run cw-image-proof first; nothing was changed" % want[:19])
    latest = max(receipts, key=lambda r: r.get("time", ""))
    if latest.get("verdict") != "pass":
        raise Refused("the proof receipt for %s has verdict %s, not pass; nothing was changed" % (want[:19], latest.get("verdict", "none")))
    try:
        when = datetime.datetime.strptime(latest.get("time", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise Refused("the proof receipt for %s has no readable time; nothing was changed" % want[:19])
    if not datetime.timedelta(0) <= now - when < RECEIPT_MAX_AGE:
        raise Refused("the proof receipt for %s is not younger than 6 hours; nothing was changed" % want[:19])
    if latest.get("revision") != revision:
        raise Refused("the proof receipt's revision differs from the image's label; nothing was changed")


def roll(args, env, now=None):
    m = SOURCE_RE.fullmatch(args.source)
    if not m:
        raise Refused("the source must be a reference by digest, NAME@sha256:<64 lowercase hex>")
    if not REGISTRY_RE.fullmatch(args.registry) or "@" in args.registry:
        raise Refused("the destination registry is not a plain registry path")
    if not TAG_RE.fullmatch(args.tag):
        raise Refused("the tag is not a valid image tag")
    want = "sha256:" + m.group(1)
    path = args.settings or default_settings(env)
    data = check_settings(path)
    tool = crane_tool(env)
    check_gate(tool, args.source, want, env, datetime.datetime.now(datetime.timezone.utc))
    dest = "%s:%s" % (args.registry, args.tag)
    copy = run_crane(tool, ["copy", args.source, dest], env, "crane copy", 900)
    if copy.returncode != 0:
        raise Refused("crane copy failed with status %d; nothing was changed" % copy.returncode)
    got = run_crane(tool, ["digest", dest], env, "crane digest", 120)
    if got.returncode != 0:
        raise Refused("crane digest failed with status %d; nothing was changed" % got.returncode)
    seen = got.stdout.decode("utf-8", "replace").strip()
    if not seen:
        raise Refused("the destination digest is empty; nothing was changed")
    if not DIGEST_RE.fullmatch(seen) or seen != want:
        raise Refused("the destination digest differs from the source digest; nothing was changed")
    new_value = "%s@%s" % (args.registry, want)
    old, new = old_digest(data), want[len("sha256:"):][:12]
    if args.dry_run:
        return ["dry run: would change CW_CLOUD_IMAGE in %s from %s to %s (new setting %s); nothing was changed" % (path, old, new, new_value)]
    if check_settings(path) != data:   # the copy can take minutes: an edit made meanwhile is not overwritten
        raise Refused("the settings file %s changed during the copy; nothing was changed" % path)
    backup = apply(path, data, replace_line(data, new_value), now or datetime.datetime.now())
    return ["CW_CLOUD_IMAGE changed from %s to %s" % (old, new), "backup: %s" % backup]


def main(argv=None, env=None):
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="cw-image-roll", description=__doc__.split("\n")[0])
    ap.add_argument("source")
    ap.add_argument("registry")
    ap.add_argument("tag")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--settings", default=None)
    args = ap.parse_args(argv)
    try:
        lines = roll(args, env)
    except Refused as e:
        print("cw-image-roll: refused: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("cw-image-roll: refused: %s" % (e.strerror or "an I/O error"), file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
