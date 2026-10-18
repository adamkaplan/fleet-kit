#!/usr/bin/env python3
"""File scanner for scan-secrets.sh (stdlib only). Usage: scan-files.py [--allow <allowlist> --prefix <dir>] <patterns-file> <root>...

The patterns file holds one "name@@regex" per line. Every regular file under the roots is read as BYTES
(binary files are scanned, never skipped) in streamed chunks with an overlap, so a token that straddles a chunk edge
is found and memory stays bounded; there is no file-size limit. Each file is searched in two forms: as it is, and with
every NUL byte removed (which turns UTF-16LE, UTF-16BE and NUL-padded text into plain ASCII). Base64, compressed
or otherwise encoded forms are NOT searched. A "$" also matches before a CR, so a CRLF line end does not hide a key.
Prints "HIT\tpath: name" per file and pattern, never the matched value; a hit the allowlist names (path AND sha256 of the
file AND pattern, all three) is printed as "ALLOW\tpath: name" instead and is not a hit. The reported path is
"/" + the path under --prefix. With SCAN_TRIPLES=1 each hit also prints "TRIPLE\t<path> <sha256> <pattern>" (no value). Exit 0 = no hit, 1 = hits, 2 = something could not
be read (never a pass).
"""
import hashlib
import os
import re
import sys

CHUNK = 4 * 1024 * 1024
OVERLAP = 64 * 1024


def load(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            name, _, rx = line.partition("@@")
            rx = rx.replace("$", r"\r?$") if rx.endswith("$") else rx
            out.append((name, re.compile(rx.encode(), re.M)))
    return out


class AllowlistError(Exception):
    pass


def load_allow(path):
    """Triples (path-in-image, sha256, pattern) from the allowlist file: "<path> <sha256> <pattern> <reason...>" per line
    (# comments and blank lines ignored). A missing, unreadable or malformed file raises AllowlistError (never "no
    allowlist = everything passes"); a line with no reason is malformed."""
    triples = set()
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise AllowlistError(f"cannot read the allowlist {path}") from e
    for i, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = line.split(None, 3)
        if len(f) < 4 or not f[0].startswith("/") or not re.fullmatch(r"[0-9a-f]{64}", f[1]) \
                or not re.fullmatch(r"[A-Za-z0-9-]+", f[2]):
            raise AllowlistError(f"malformed allowlist line {i} in {path}")
        triples.add((f[0], f[1], f[2]))
    return triples


def esc(p):
    """A path for display: control characters (an ESC, a newline) become \\xNN, so a hostile name cannot forge output lines."""
    return re.sub(r"[\x00-\x1f\x7f]", lambda m: "\\x%02x" % ord(m.group(0)), p)


def classify(path, found, sha, allow, pats, emit):
    """Print each pattern hit of one file; an allowlisted triple is printed as ALLOW. Returns (hits, allowed)."""
    hits = allowed = 0
    order = [n for n, _ in pats] + sorted(found - {n for n, _ in pats})  # pattern names, then e.g. credential-file
    for name in order:
        if name not in found:
            continue
        if (path, sha, name) in allow:
            emit("ALLOW", esc(path), name)
            allowed += 1
        else:
            emit("HIT", esc(path), name)
            hits += 1
            if os.environ.get("SCAN_TRIPLES") == "1":
                print(f"TRIPLE\t{esc(path)} {sha} {name}")
    return hits, allowed


def scan_fileobj(fh, pats, found, hasher=None):
    """Search a binary stream chunk by chunk (as it is and NUL-stripped); add the names of the patterns that hit."""
    tail = b""
    while True:
        data = fh.read(CHUNK)
        if hasher is not None:
            hasher.update(data)
        if not data and tail:
            break
        buf = tail + data
        for name, rx in pats:
            if name in found:
                continue
            if rx.search(buf) or rx.search(buf.replace(b"\x00", b"")):
                found.add(name)
        if not data:
            break
        tail = buf[-OVERLAP:]


def scan_file(path, pats, found, hasher=None):
    with open(path, "rb") as fh:
        scan_fileobj(fh, pats, found, hasher)


def main(argv):
    argv = list(argv)
    allow = set()
    prefix = ""
    while len(argv) > 2 and argv[1] in ("--allow", "--prefix"):
        if argv[1] == "--allow":
            try:
                allow = load_allow(argv[2])
            except AllowlistError as e:
                print(f"scan-secrets: {e}: not scanned", file=sys.stderr)
                return 2
        else:
            prefix = argv[2].rstrip("/") + "/"
        del argv[1:3]
    if len(argv) < 3:
        print("usage: scan-files.py [--allow <allowlist> --prefix <dir>] <patterns-file> <root>...", file=sys.stderr)
        return 2
    pats = load(argv[1])
    hits = 0
    bad = []

    def emit(kind, p, name):
        print(f"{kind}\t{p}: {name}")

    def one(p):
        nonlocal hits
        found = set()
        h = hashlib.sha256()
        try:
            scan_file(p, pats, found, h)
        except OSError:
            bad.append(p)
            return
        shown = "/" + p[len(prefix):] if prefix and p.startswith(prefix) else p
        hits += classify(shown, found, h.hexdigest(), allow, pats, emit)[0]

    def onerr(e):
        bad.append(e)

    for root in argv[2:]:
        if os.path.islink(root):
            continue
        if os.path.isfile(root):
            one(root)
            continue
        for d, _dirs, files in os.walk(root, onerror=onerr):
            for f in files:
                p = os.path.join(d, f)
                if os.path.islink(p) or not os.path.isfile(p):
                    continue
                one(p)
    if bad:
        print("scan-secrets: could not read some files: not scanned", file=sys.stderr)
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
