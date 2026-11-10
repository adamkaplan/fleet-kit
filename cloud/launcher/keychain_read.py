"""The one read of a launcher secret from the macOS login keychain (setting CW_KEYCHAIN_SERVICE).

The program is the fixed absolute /usr/bin/security (`find-generic-password -s SERVICE -a ACCOUNT -w`), run without a shell, with a 20 second
timeout (a locked keychain or an access prompt makes it hang), a minimal environment and its output capped. The ACCOUNT is the vault's own
secret name. STORAGE FORMAT: `security -w` prints multi-line data as hex, so an item holds ONE printable line: the secret base64-encoded; a
secret whose base64 would pass 3900 characters is stored as `gz:` + base64(zlib.compress(secret)). The reader decodes base64 and, for the `gz:`
prefix, decompresses (capped); text that is not valid base64/zlib, not UTF-8 or empty is refused. A refusal carries a kind and a one-line
reason, never a value; the value is never in an argument or a log line."""
import base64
import binascii
import os
import pwd
import re
import subprocess
import zlib

PROGRAM = "/usr/bin/security"
TIMEOUT_SECONDS = 20
SERVICE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")   # letters, digits, dot and dash, at most 64; never a leading dash (an option)
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
LOCKED_LINE = "the login keychain is locked or asks for access"


class KeychainError(Exception):
    """`kind` is 'keychain-unavailable' (timeout) or 'vault other'; `line` is one clear line without any value."""

    def __init__(self, kind, line):
        super().__init__(line)
        self.kind, self.line = kind, line


def service_ok(value):
    return bool(SERVICE_PATTERN.fullmatch(value or ""))


def _child_env():
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir   # the account's own home: a scrubbed caller's HOME (empty, private) would hide the login keychain
    except KeyError:
        home = os.environ.get("HOME") or "/"
    return {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8"}


def decode(text, limit):
    """The secret from the stored line, or KeychainError('vault other'). Never echoes the text."""
    bad = KeychainError("vault other", "the keychain item is not valid stored text (base64, or gz: and base64 of zlib)")
    zipped = text.startswith("gz:")
    try:
        raw = base64.b64decode(text[3:] if zipped else text, validate=True)
        if zipped:
            inflater = zlib.decompressobj()
            raw = inflater.decompress(raw, limit + 1)
            if not inflater.eof and not inflater.unconsumed_tail:
                raise zlib.error("truncated")   # (more data than the cap is caught by the caller's size check)
        return raw.decode("utf-8")
    except (binascii.Error, ValueError, zlib.error):
        raise bad from None


def read(service, account, limit=1000000, trim=True, run=subprocess.run):
    """The secret `account` of keychain service `service`. trim=False keeps the value as stored (the App key is not trimmed)."""
    if not service_ok(service):
        raise KeychainError("vault other", "CW_KEYCHAIN_SERVICE is not a valid service name")
    if not ACCOUNT_PATTERN.fullmatch(account or ""):
        raise KeychainError("vault other", "the keychain item name is not valid")
    argv = [PROGRAM, "find-generic-password", "-s", service, "-a", account, "-w"]
    try:
        done = run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, env=_child_env())
    except subprocess.TimeoutExpired:
        raise KeychainError("keychain-unavailable", LOCKED_LINE) from None
    except (OSError, ValueError):
        raise KeychainError("vault other", "the keychain program could not be run") from None
    stored = done.stdout if isinstance(done.stdout, str) else ""
    if done.returncode != 0:
        raise KeychainError("vault other", "the keychain item %s was not found or could not be read" % account)
    if len(stored) > 2 * limit + 16:
        raise KeychainError("vault other", "the keychain item %s is over the %d byte cap" % (account, limit))
    stored = stored.strip()
    if not stored:
        raise KeychainError("vault other", "the keychain item %s is empty" % account)
    value = decode(stored, limit)
    if len(value) > limit:
        raise KeychainError("vault other", "the keychain item %s is over the %d byte cap" % (account, limit))
    if not value.strip():
        raise KeychainError("vault other", "the keychain item %s is empty" % account)
    return value.strip() if trim else value
