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

DPKG_INFO = "/var/lib/dpkg/info"
DPKG_MAX = 64 * 1024 * 1024  # a package list or md5sums file larger than this is malformed (fail closed)

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


class DpkgError(Exception):
    """The dpkg database cannot be read or is malformed: the skip set is then EMPTY (fail closed)."""


class Multi:
    """update() feeds several hashers (sha256 for the allowlist, md5 for the dpkg comparison)."""

    def __init__(self, *hs):
        self.hs = hs

    def update(self, data):
        for h in self.hs:
            h.update(data)


def md5():
    return hashlib.md5(usedforsecurity=False)


def _parts(path):
    return [c for c in path.split("/") if c not in ("", ".")]


def resolve(path, readlink):
    """The real path of an absolute path inside a root, following symlinks of the directories (readlink(p) -> target or
    None). A '..' that would climb above the root, and a loop give None (not resolvable: never skipped). The caller checks the last component itself. Absolute targets are read from the root, as in a chroot."""
    todo = list(reversed(_parts(path)))
    cur = []
    hops = 0
    while todo:
        c = todo.pop()
        if c == "..":
            if not cur:
                return None
            cur.pop()
            continue
        cur.append(c)
        t = readlink("/" + "/".join(cur))
        if t is None:
            continue
        hops += 1
        if hops > 40:
            return None
        cur.pop()
        if t.startswith("/"):
            cur = []
        todo.extend(reversed(_parts(t)))
    return "/" + "/".join(cur)


MD5LINE = re.compile(r"([0-9a-f]{32})  (.+)")


def build_skipmap(lists, sums, readlink):
    """The unmodified files of installed distro packages: {real path: {md5 hex}}. lists / sums: {package: bytes} of
    /var/lib/dpkg/info/<package>.list and .md5sums (None = unreadable). A file counts only when it is in a package's
    .list AND in the same package's .md5sums; the caller still compares the md5 of the file itself. A path with a '..',
    an entry that does not resolve inside the root, or a symlink as the file is dropped. A malformed list or md5sums
    (or an unreadable one, or no list at all) raises DpkgError: everything is scanned then."""
    if not lists:
        raise DpkgError("no package list in the dpkg database")
    out = {}
    for pkg, lraw in lists.items():
        sraw = sums.get(pkg)
        if lraw is None:
            raise DpkgError(f"cannot read the list of {pkg}")
        listed = set()
        for line in lraw.decode("utf-8", "surrogateescape").split("\n"):
            if not line:
                continue
            if not line.startswith("/") or "\x00" in line:
                raise DpkgError(f"malformed list of {pkg}")
            listed.add(line)
        if sraw is None:
            if pkg in sums:
                raise DpkgError(f"cannot read the md5sums of {pkg}")
            continue
        for line in sraw.decode("utf-8", "surrogateescape").split("\n"):
            if not line:
                continue
            m = MD5LINE.fullmatch(line)
            if not m or "\x00" in line:
                raise DpkgError(f"malformed md5sums of {pkg}")
            path = "/" + m.group(2)
            if path not in listed or ".." in path.split("/") or m.group(2).startswith("\\"):
                continue
            parent, _, base = path.rpartition("/")
            rp = resolve(parent or "/", readlink)
            if rp is None:
                continue
            real = rp.rstrip("/") + "/" + base
            if readlink(real) is not None:
                continue
            out.setdefault(real, set()).add(m.group(1))
    # a name that is only in md5sums without a list is never a skip (loop above is driven by the lists)
    return out


def dpkg_from_dir(root):
    """Read the dpkg database of the tree at root (as a chroot: symlinks are read inside it). Returns the skip map or
    raises DpkgError."""
    root = root.rstrip("/")

    def readlink(p):
        q = root + p
        try:
            return os.readlink(q) if os.path.islink(q) else None
        except OSError:
            return None

    info = resolve(DPKG_INFO, readlink)
    if info is None:
        raise DpkgError("the dpkg database path does not resolve inside the root")
    d = root + info
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        raise DpkgError(f"cannot read {DPKG_INFO} ({type(e).__name__})")
    lists, sums = {}, {}
    for n in names:
        for ext, dst in ((".list", lists), (".md5sums", sums)):
            if n.endswith(ext) and len(n) > len(ext):
                f = os.path.join(d, n)
                try:
                    if os.path.islink(f) or not os.path.isfile(f) or os.path.getsize(f) > DPKG_MAX:
                        dst[n[:-len(ext)]] = None
                        continue
                    with open(f, "rb") as fh:
                        dst[n[:-len(ext)]] = fh.read()
                except OSError:
                    dst[n[:-len(ext)]] = None
    return build_skipmap(lists, sums, readlink)


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
    dpkg_root = None
    while len(argv) > 2 and argv[1] in ("--allow", "--prefix", "--dpkg"):
        if argv[1] == "--dpkg":
            dpkg_root = argv[2]
        elif argv[1] == "--allow":
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
    skipped = 0
    bad = []
    skipmap = {}
    if dpkg_root is not None:
        try:
            skipmap = dpkg_from_dir(dpkg_root)
            print(f"scan-secrets: dpkg database read: {len(skipmap)} package files can be skipped when unmodified", file=sys.stderr)
        except DpkgError as e:
            skipmap = {}
            print(f"scan-secrets: dpkg database not usable ({e}): skip set EMPTY, every file is scanned", file=sys.stderr)

    def emit(kind, p, name):
        print(f"{kind}\t{p}: {name}")

    def one(p):
        nonlocal hits, skipped
        found = set()
        h = hashlib.sha256()
        shown = "/" + p[len(prefix):] if prefix and p.startswith(prefix) else p
        m5 = md5() if shown in skipmap else None
        try:
            scan_file(p, pats, found, h if m5 is None else Multi(h, m5))
        except OSError:
            bad.append(p)
            return
        if m5 is not None and m5.hexdigest() in skipmap[shown]:
            skipped += 1  # an unmodified file of an installed distro package
            return
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
    if dpkg_root is not None:
        print(f"SKIPPED\t{skipped}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
