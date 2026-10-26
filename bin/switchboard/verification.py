# ------------------------------------------------------------- verification

def sign_body(secret, body):
    """The `X-Hub-Signature-256` value for `body` under `secret`."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_matches(secret, body, header):
    """True when `header` is the signature of the raw `body`. Constant time."""
    if not isinstance(header, str) or not secret:
        return False
    return hmac.compare_digest(sign_body(secret, body).encode("ascii"), header.encode("utf-8", "replace"))


def _scrub(text, *secrets_):
    """`text` with every secret in `secrets_` replaced; for anything read back from a child."""
    for secret in secrets_:
        if secret:
            text = text.replace(secret, REDACTED)
    return text
