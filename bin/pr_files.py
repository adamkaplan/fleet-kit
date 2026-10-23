"""The ONE reader of a pull request's changed-file list (#1098). Every reader (the impact floor, the delegated clear, the clear's carry, the who-can-clear line) reads through `read`.

GitHub lists a pull request's files 100 to a page and stops at 3000 (its stated maximum). `read` follows the pages to the last. It fails closed (files None, a reason) only when
GitHub itself says there are more files than it will list, or when a page cannot be read: a failed read is never an empty list. A pull request of 100 files or fewer costs one request.
Python 3.9 standard library only.
"""
PAGE = 100
MAX_FILES = 3000          # the pull request files API lists no more than this
MAX_PAGES = MAX_FILES // PAGE


def read(get, repo, number, total=None):
    """(files, None) with every changed file, or (None, why). `get(path)` is a parsed GET that raises on failure. `total` is the pull request's own count of changed files when known
    (it ends the read at exactly a full page, and tells a list GitHub cut from a whole one)."""
    known = isinstance(total, int) and not isinstance(total, bool) and total >= 0
    if known and total > MAX_FILES:
        return None, "GitHub lists at most %d of the %d changed files (its stated maximum)" % (MAX_FILES, total)
    files = []
    for page in range(1, MAX_PAGES + 1):
        try:
            batch = get("repos/%s/pulls/%d/files?per_page=%d&page=%d" % (repo, number, PAGE, page))
        except Exception as err:
            return None, "the changed files could not be read (%s)" % str(err)[:80]
        if not isinstance(batch, list):
            return None, "the changed files were not a list"
        files.extend(f for f in batch if isinstance(f, dict))
        if len(batch) < PAGE or (known and len(files) >= total):
            break
    else:
        if not known:
            return None, "GitHub lists at most %d changed files (its stated maximum) and this is a full list of that size" % MAX_FILES
    if known and len(files) != total:
        return None, "GitHub listed %d of %d changed files" % (len(files), total)
    return files, None
