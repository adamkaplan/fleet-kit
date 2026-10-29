"""Paginated account reads that go through the counting layer (#1515).

`gh api --paginate` hides its pages: the daemon counted one call however many pages it cost and could not send a stored validator.
`read_pages` reads the pages itself (`gh api -i`, per_page=100, page=1,2,...): the stored ETag goes on page 1 only (a 304 there returns the
stored pages and costs nothing against the budget), and ONE request is counted per page request made. The pages are kept per path.
Loaded lazily by path (`load_module("paged_reads")`), outside the clear closure. It keeps no header and no token: only the ETag
(already held by the layer) and the bodies, as the single-page conditional read does."""
import re
import subprocess

PER_PAGE = 100
PAGE_CAP = 100                      # a safety stop: 10000 records
_NEXT = re.compile(r'<[^>]*>\s*;\s*rel="next"')


def page_path(path, number):
    """`path` with per_page=100 and page=<number> (a value the path already holds is kept)."""
    out = path
    for key, value in (("per_page", PER_PAGE), ("page", number)):
        if re.search(r"[?&]%s=" % key, out) is None:
            out += ("&" if "?" in out else "?") + "%s=%d" % (key, value)
    return out


def _body_join(a, b):
    return a + ("" if not a or a.endswith("\n") else "\n") + b


def read_pages(accounts, runner, path, repo, timeout, split_http, endpoint_of, etag_max, body_max, bytes_max, **run_kwargs):
    """A CompletedProcess whose stdout is the concatenated page bodies (what `--paginate` printed). `accounts` is the account layer."""
    account = accounts.describe(repo)
    key = (account, "paged:" + path)
    with accounts.lock:
        cached = accounts.etags.get(key)
    env = accounts.env_for(repo) if repo else None
    if env is not None:
        run_kwargs["env"] = env
    text = run_kwargs.pop("text", True)
    bodies, argv, first_etag = [], None, None
    for number in range(1, PAGE_CAP + 1):
        flags = ["-i"] + (["-H", "If-None-Match: " + cached[0]] if (number == 1 and cached) else [])
        argv = [accounts.config["gh"], "api"] + flags + [page_path(path, number)]
        now = accounts.now()
        done = runner(argv, capture_output=True, text=text, timeout=timeout, **run_kwargs)
        status, headers, body = split_http(done.stdout)
        with accounts.lock:
            accounts.calls.append((now, account, endpoint_of(path), status == 304))   # one per page request made
            while accounts.calls and accounts.calls[0][0] < now - 600:
                accounts.calls.popleft()
        if status is None:   # no header block (a fake, a failure): as it is; a first page alone is the whole answer
            if number == 1:
                if done.returncode != 0 and accounts.says_rate_limited(done.stderr, done.stdout):
                    accounts.pause_default(account, now)
                return done
            return subprocess.CompletedProcess(argv, done.returncode or 1, "", done.stderr or "page %d: no header block" % number)
        if status == 304 and number == 1 and cached is not None:
            with accounts.lock:
                accounts.etags.move_to_end(key)
            return subprocess.CompletedProcess(argv, 0, cached[1], "")
        if not 200 <= status < 300:
            if status in (403, 429):
                accounts.pause_for(account, status, headers, body, now)
            return subprocess.CompletedProcess(argv, done.returncode or 1, "", done.stderr or ("HTTP %d" % status))
        bodies.append(body)
        if number == 1:
            first_etag = headers.get("etag")
        if _NEXT.search(headers.get("link", "")) is None:
            break
    joined = ""
    for one in bodies:
        joined = _body_join(joined, one)
    if first_etag and len(joined) <= body_max and "since=" not in path:
        with accounts.lock:
            accounts.etags[key] = (first_etag, joined)
            accounts.etags.move_to_end(key)
            while len(accounts.etags) > etag_max or sum(len(v[1]) for v in accounts.etags.values()) > bytes_max:
                accounts.etags.popitem(last=False)
    return subprocess.CompletedProcess(argv, 0, joined, "")
