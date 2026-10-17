"""One way to match a repository against the `gh_users` map, for the fleet's shims (gh, and git through gh's credential route).

The rules are the launch guard's (`GhAccounts.normal_repo` and `GhAccounts.account_for` in bin/fleet-switchboard): surrounding whitespace, trailing
`/` and one trailing `.git` come off and the text is lower-cased (GitHub names are not case sensitive); an exact entry wins over an `owner/*` entry.

Where a shim differs it is only STRICTER, because a shim guards a credential and fails CLOSED: a spelling that cannot be read as `owner/name`
(empty, only whitespace, a non-ASCII character such as a case-folding lookalike, a path like `owner/../x`, a second slash, `.git.git`
leaving a name that is not a name) is unmatched, never defaulted. A map whose two spellings of one repository name different accounts matches nothing.
Standard library only; loaded by path, never imported as a package.
"""
import re

NAME_RE = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+")


def normal_repo(repo):
    """`owner/name` in lower case, or None when `repo` cannot be read as one (the caller treats None as unmatched and refuses)."""
    if not isinstance(repo, str) or not repo.isascii():
        return None
    text = repo.strip().rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    text = text.lower()
    if not NAME_RE.fullmatch(text) or text.endswith(".git"):   # a name still ending in .git (`.git.git`) is not one GitHub allows
        return None
    return None if any(part in (".", "..") for part in text.split("/")) else text


def normal_key(key):
    """A `gh_users` key the same way: `owner/name` or `owner/*`, else None."""
    if isinstance(key, str) and key.strip().endswith("/*"):
        owner = normal_repo(key.strip()[:-2] + "/x")
        return owner[:-1] + "*" if owner else None
    return normal_repo(key)


def account_for(users, repo):
    """The account `users` maps `repo` to (exact entry first, then `owner/*`), or None: no map, an unreadable repo, no entry, a conflicting map."""
    want = normal_repo(repo)
    if not want or not isinstance(users, dict):
        return None
    table, clash = {}, set()
    for key, account in users.items():
        norm = normal_key(key)
        if norm is None:
            continue
        if norm in table and table[norm] != account:
            clash.add(norm)
        table[norm] = account
    wild = "%s/*" % want.split("/")[0]
    for name in (want, wild):
        if name in clash:
            return None
        account = table.get(name)
        if isinstance(account, str) and account:
            return account
    return None
