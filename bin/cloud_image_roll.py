#!/usr/bin/env python3
"""cw-image-roll (ask #2391): copy a worker image to the cluster registry and point the operator's settings file at it, only when the copy is proven.

    bin/cloud_image_roll.py SOURCE_BY_DIGEST DEST_REGISTRY TAG [--dry-run] [--settings PATH]

SOURCE_BY_DIGEST is `<registry>/<path>@sha256:<64 hex>`; DEST_REGISTRY is `<registry>/<path>` (no tag, no digest). It runs `crane copy SOURCE DEST:TAG`
and `crane digest DEST:TAG` (the program is `crane`, or the path in the CRANE variable) and compares the digests. The command never logs in: it relies
on the credential store the operator's `crane` already uses, and it prints nothing the tool wrote on its error stream.

It stops with ONE line on the error stream and exit status 1, changing nothing, when the settings file is missing, is not a regular file, is not owned by
the user, has a mode wider than 0600 or has no `CW_CLOUD_IMAGE=` line (checked before any copy), when the copy fails, or when the destination digest is empty or
differs from the source's. Only then: a backup of the file (same directory, the time in its name, mode 0600), the one `CW_CLOUD_IMAGE=` line replaced through a
temporary file in the same directory and a rename (mode 0600), the file read back, and the old and new digests (12 characters each) and the backup's path printed.
Every other line stays byte for byte. `--dry-run` runs the same checks and the copy, prints what it would change and touches no file.
Exit 0 on success or dry run, 1 on a refusal, 2 on bad arguments.
"""
import argparse
import datetime
import os
import re
import stat
import subprocess
import sys
import tempfile

SETTING = b"CW_CLOUD_IMAGE="
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@(sha256:[0-9a-f]{64})")
DEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
TOOL_SECONDS = 600


class Refused(Exception):
    pass


def _euid():
    return os.geteuid()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def default_settings():
    """The operator settings path, asked of the launcher's own function (cloud/launcher/cloud_worker.py), so the two never differ."""
    launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cloud", "launcher")
    code = "import sys; sys.path.insert(0, sys.argv[1]); import cloud_worker; print(cloud_worker.settings_path())"
    done = subprocess.run([sys.executable, "-c", code, launcher], capture_output=True, text=True, timeout=60)
    path = done.stdout.strip()
    if done.returncode != 0 or not path:
        raise Refused("cannot find the settings path: pass --settings PATH")
    return path


def check_settings(path):
    """Refuse (before any copy) a file that is missing, not regular, not ours, wider than 0600, or without the one image line. Returns its bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Refused("refused: the settings file %s is missing; nothing was changed" % path) from None
    except OSError as err:
        raise Refused("refused: the settings file %s cannot be opened (%s); nothing was changed" % (path, type(err).__name__)) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("refused: the settings file %s is not a regular file; nothing was changed" % path)
        if st.st_uid != _euid():
            raise Refused("refused: the settings file %s is not owned by you; nothing was changed" % path)
        if stat.S_IMODE(st.st_mode) & 0o177:
            raise Refused("refused: the settings file %s has mode %04o, wider than 0600; nothing was changed" % (path, stat.S_IMODE(st.st_mode)))
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(1 << 20)
    finally:
        os.close(fd)
    if sum(1 for line in data.splitlines() if line.startswith(SETTING)) != 1:
        raise Refused("refused: the settings file %s must hold exactly one CW_CLOUD_IMAGE= line; nothing was changed" % path)
    return data


def run_tool(argv):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TOOL_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None


def copy_and_compare(crane, source, want, dest_ref):
    """Copy, read the destination's digest and compare. Nothing the tool printed is shown."""
    copy = run_tool([crane, "copy", source, dest_ref])
    if copy is None or copy.returncode != 0:
        raise Refused("refused: the copy failed (exit %s; the tool's output is not shown); nothing was changed" % ("?" if copy is None else copy.returncode))
    got = run_tool([crane, "digest", dest_ref])
    digest = (got.stdout.strip() if got is not None and got.returncode == 0 else "")
    if not DIGEST.fullmatch(digest):
        raise Refused("refused: the destination digest of %s is empty or unreadable; nothing was changed" % dest_ref)
    if digest != want:
        raise Refused("refused: the destination digest %s differs from the source's %s; nothing was changed" % (digest[:19], want[:19]))
    return digest


def old_digest(data):
    for line in data.splitlines():
        if line.startswith(SETTING):
            found = re.search(rb"sha256:([0-9a-f]{12})", line)
            return found.group(1).decode() if found else "none"
    return "none"


def write_backup(path, data):
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    for n in range(100):
        name = "%s.bak-%s%s" % (path, stamp, "" if n == 0 else "-%d" % n)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(name, 0o600)
        return name
    raise Refused("refused: no backup name was free; nothing was changed")


def replace_line(data, new_value):
    out = []
    for line in data.splitlines(keepends=True):
        if line.startswith(SETTING):
            ending = line[len(line.rstrip(b"\r\n")):]
            line = SETTING + new_value.encode() + ending
        out.append(line)
    return b"".join(out)


def write_replace(path, new_bytes):
    fd, tmp = tempfile.mkstemp(prefix=".cw-image-roll-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(new_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def roll(args, environ, out):
    path = args.settings or default_settings()
    data = check_settings(path)
    source = args.source
    want = SOURCE.fullmatch(source).group(1)
    dest_ref = "%s:%s" % (args.registry, args.tag)
    copy_and_compare(environ.get("CRANE") or "crane", source, want, dest_ref)
    new_value = "%s@%s" % (args.registry, want)
    old = old_digest(data)
    if args.dry_run:
        print("dry run: would change CW_CLOUD_IMAGE from sha256:%s to sha256:%s in %s (a backup would be made); nothing was changed" % (old, want[7:19], path), file=out)
        return 0
    backup = write_backup(path, data)
    write_replace(path, replace_line(data, new_value))
    after = check_settings(path)
    if after != replace_line(data, new_value):
        raise Refused("refused: the settings file read back differently; restore it from %s" % backup)
    print("rolled: CW_CLOUD_IMAGE sha256:%s -> sha256:%s; backup %s" % (old, want[7:19], backup), file=out)
    return 0


def main(argv=None, out=None, err=None, environ=None):
    out = out or sys.stdout
    err = err or sys.stderr
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="cloud_image_roll", description="copy a worker image by digest and point the settings file at it")
    parser.add_argument("source", help="the source reference by digest: <registry>/<path>@sha256:<64 hex>")
    parser.add_argument("registry", help="the destination registry and path, no tag")
    parser.add_argument("tag")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--settings", default=None, help="the settings file (default: the launcher's operator settings path)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    if not SOURCE.fullmatch(args.source) or not DEST.fullmatch(args.registry) or "@" in args.registry or not TAG.fullmatch(args.tag):
        print("refused: usage: SOURCE@sha256:<64 hex> DEST_REGISTRY TAG; nothing was changed", file=err)
        return 2
    try:
        return roll(args, environ, out)
    except Refused as why:
        print(str(why), file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
