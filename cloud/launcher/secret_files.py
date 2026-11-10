"""The one read of a launcher secret from a private directory of files (setting CW_SECRET_DIR).

CW_SECRET_DIR is an absolute path of a directory under the user's home: a real directory (not a link), owned by the running user, mode 0700.
It holds one regular file per secret, named exactly as the vault's secret (cw-app-id, cw-app-key, cw-minter-kubeconfig, fk-cloud-workers-admin-kubeconfig,
cw-stack-model-key; and acr-token for the image publish: `username:password` on one line), each mode 0600, owned by the running user, not a link, at
most 64 KiB, holding the raw secret text. No program is run. A wrong mode, owner, link, size, a missing file or text that is not UTF-8 or is empty is
refused with a one-line reason that names the item, never a value; the value is never in an argument or a log line."""
import os
import pwd
import re
import stat

LIMIT = 65536
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
HOME = None   # a test hook: None means the running user's home from the password database


class SecretError(Exception):
    """`kind` is the launcher's vault error kind ('vault other'); `line` is one clear line without any value."""

    def __init__(self, line, kind="vault other"):
        super().__init__(line)
        self.kind, self.line = kind, line


def home_dir():
    if HOME is not None:
        return HOME
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return ""


def dir_ok(value):
    """True for an absolute path with no .. segment, no control character, under the user's home (a name check only: the reader checks the directory)."""
    home = home_dir().rstrip("/")
    if not isinstance(value, str) or not home or not os.path.isabs(value) or ".." in value.split("/") or re.search(r"[\x00-\x1f\x7f]", value):
        return False
    value = os.path.normpath(value)
    return value.startswith(home + "/") and value != home


def read(directory, name, limit=LIMIT, trim=True):
    """The secret file `name` of `directory`. trim=False keeps the text as stored (the App key is not trimmed)."""
    limit = min(limit, LIMIT)
    if not dir_ok(directory):
        raise SecretError("CW_SECRET_DIR is not an absolute directory under the home directory")
    if not NAME_PATTERN.fullmatch(name or ""):
        raise SecretError("the secret file name is not valid")
    me = os.geteuid()
    dfd = fd = None
    try:
        try:
            dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise SecretError("the secret directory is missing, is a link or cannot be opened") from None
        info = os.fstat(dfd)
        if info.st_uid != me or stat.S_IMODE(info.st_mode) != 0o700:
            raise SecretError("the secret directory must be owned by the running user with mode 0700")
        try:
            real, home = os.path.realpath(directory), os.path.realpath(home_dir())
            if not real.startswith(home.rstrip("/") + "/"):
                raise SecretError("the secret directory is not under the home directory")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError:
            raise SecretError("the secret file %s is missing or is a link" % name) from None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretError("the secret file %s is not a regular file" % name)
        if info.st_uid != me or stat.S_IMODE(info.st_mode) != 0o600:
            raise SecretError("the secret file %s must be owned by the running user with mode 0600" % name)
        if info.st_size > limit:
            raise SecretError("the secret file %s is over the %d byte cap" % (name, limit))
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, limit + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > limit:
            raise SecretError("the secret file %s is over the %d byte cap" % (name, limit))
    finally:
        for f in (fd, dfd):
            if f is not None:
                os.close(f)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretError("the secret file %s is not UTF-8 text" % name) from None
    if not text.strip():
        raise SecretError("the secret file %s is empty" % name)
    return text.strip() if trim else text
