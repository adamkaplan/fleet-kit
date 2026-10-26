# ---------------------------------------------------------------- the judge's rule

_GH_SEG = re.compile(r"(?:^|[\s;&|(`'\"/])gh\s+([^;&|\n`]*)")
_API_REPOS = re.compile(r"(?:^|[\s'\"])/?(?:orgs/[^/\s'\"]+|user)/repos/?(?=$|[\s'\"?])")
_POST_FLAG = re.compile(r"(?:^|\s)(?:-X\s*|--method[=\s]\s*)POST\b", re.I)
_FIELD_FLAG = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)\b")


def repo_commissioning_rule(command):
    """The sentence naming what a shell command does to a repo's existence or visibility, or None.

    `gh repo create`, `gh repo delete`, `gh repo edit --visibility` and a POST to `/orgs/*/repos` are
    `hard_to_reverse` whatever the thresholds say: the Chief of Staff commissions a worker to do these, after
    a decision, and no agent does one quietly. Deterministic, so it needs no model; judge machinery only
    ever tightens."""
    if not isinstance(command, str):
        return None
    for found in _GH_SEG.finditer(command):
        words = found.group(1)
        positional = [w for w in words.split() if not w.startswith("-")]
        if positional[:2] in (["repo", "create"], ["repo", "delete"]):
            return "gh repo %s" % positional[1]
        if positional[:2] == ["repo", "edit"] and re.search(r"(?:^|\s)--visibility\b", words):
            return "gh repo edit --visibility"
        if positional[:1] == ["api"] and _API_REPOS.search(" " + words) \
                and (_POST_FLAG.search(words) or (_FIELD_FLAG.search(words) and not re.search(r"(?:-X|--method)", words))):
            return "gh api POST to a repo-creating endpoint"
    return None
