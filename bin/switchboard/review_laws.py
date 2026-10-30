# ---------------------------------------------------------------- `review record` and `review laws` (#179, slice L2)

REVIEW_LAWS_FILES = ("REVIEW_LAWS.md", "CODING_STANDARDS.md", "SECURITY_STANDARDS.md", "TEST_STANDARDS.md")
REVIEW_LAW_FILES = REVIEW_LAWS_FILES + ("REVIEW_OVERLAY.md",)   # the laws a reviewer loads, and the repository's own overlay
REVIEW_FILE_MAX = 30000      # bytes of one law file a reviewer is shown
REVIEW_TOTAL_MAX = 90000     # bytes of all of them together


def _review_pr(args, engine):
    """(repo, number, ref, raw) of the PR in `args`, read with the PR's own account; raises ReviewError."""
    bare = args.review_cmd == "laws" and _BARE_PR.match((args.pr or "").strip())
    try:
        repo, number = parse_bare_pr_ref(args.pr) if bare else parse_pr_ref(args.pr)
    except NoticeError as err:
        raise ReviewError(str(err)) from None
    try:
        raw = read_pr_json(engine, repo, number, "baseRefOid,baseRefName,files")
    except NoticeError as err:
        if bare and isinstance(err, NoticeNotFound):
            err = "%s: %s does not name a pull request: pass owner/repo#N or the PR's URL" % (err, _one_line(args.pr, 40))
        raise ReviewError(str(err)) from None
    return repo, number, "%s#%d" % (repo, number), raw


def review_laws_text(engine, config, runner, now, repo, base_sha):
    """The review laws of `repo` AS THEY STAND AT `base_sha` (the PR's base commit, never its head: a PR cannot rewrite the law it is judged
    by), then the repository's overlay and a local overlay kept outside the repository. Returns (text, missing files)."""
    reader = IntentReader(config["gh"], repo, runner, now, config["call_timeout_seconds"], accounts=GhAccounts(config, runner, now))
    parts, missing, total = [], [], 0
    for name in REVIEW_LAW_FILES:
        try:
            got = reader._api("repos/%s/contents/%s?ref=%s" % (repo, name, base_sha), repo)
        except IntentError:
            missing.append(name)
            continue
        raw = got.get("content") if isinstance(got, dict) else None
        if not isinstance(raw, str) or got.get("encoding") != "base64":
            missing.append(name)
            continue
        try:
            text = base64.b64decode(raw).decode("utf-8", "replace")
        except ValueError:
            missing.append(name)
            continue
        if len(text.encode("utf-8")) > REVIEW_FILE_MAX:
            text = text.encode("utf-8")[:REVIEW_FILE_MAX].decode("utf-8", "ignore") + "\n[cut at %d bytes]" % REVIEW_FILE_MAX
        total += len(text)
        if total > REVIEW_TOTAL_MAX:
            missing.append(name + " (over the total size limit)")
            continue
        parts.append("===== %s (from %s at the base commit %s) =====\n%s" % (name, repo, base_sha[:12], text))
    local = local_review_overlay(repo)
    if local:
        parts.append("===== local overlay for %s (kept outside the repository) =====\n%s" % (repo, local))
    return "\n\n".join(parts), missing


def review_law_files(config, runner, now, repo, base_sha):
    """The review law files of `repo` at `base_sha`, one by one, for `review checkout`: ({name: bytes}, missing names). `review_laws_text` is not changed."""
    reader = IntentReader(config["gh"], repo, runner, now, config["call_timeout_seconds"], accounts=GhAccounts(config, runner, now))
    files, missing = {}, []
    for name in REVIEW_LAW_FILES:
        try:
            got = reader._api("repos/%s/contents/%s?ref=%s" % (repo, name, base_sha), repo)
            files[name] = base64.b64decode(got["content"]) if got.get("encoding") == "base64" else None
        except (IntentError, AttributeError, KeyError, TypeError, ValueError):
            files[name] = None
        if files[name] is None:
            del files[name]
            missing.append(name)
    return files, missing


def local_review_overlay(repo):
    """The operator's own overlay for `repo`, if one exists: $XDG_CONFIG_HOME/fleet-switchboard/review-overlay/<owner>__<repo>.md (capped)."""
    path = config_path().parent / "review-overlay" / (repo.replace("/", "__").lower() + ".md")
    try:
        return path.read_text(encoding="utf-8")[:REVIEW_FILE_MAX]
    except (OSError, UnicodeError):
        return ""


def parse_review_block(text):
    """(fields, why not) from the verdict block a reviewer RETURNS as text (the reviewers hold no write command: the coder session records what
    they returned). Lines: `verdict: ...`, `head: <sha>`, optional `impact-action: ...`, `findings:` and one `- <rule> | block|note | ...` per finding. Counts
    come from the findings, so a block cannot claim a count it does not list. A block may sit inside a code fence or prose; the first one found is read."""
    if not isinstance(text, str) or not text.strip():
        return None, "no verdict block"
    lines = [l.strip() for l in text.splitlines()]
    fields = {"verdict": None, "head": None, "impact_action": None, "impact_class": None, "impact_severity": None, "impact_probability": None, "impact_reason": None, "blocks": 0, "notes": 0, "findings": 0, "files_read": [], "files_problem": None, "block_count": 0}
    started, section = False, "findings"
    for line in lines:
        if started and line.startswith("verdict:"):
            fields["block_count"] += 1   # #1126: a second `verdict:` line before the block ends opens another block (`--block` keeps reading the first verdict and the last rating)
        if line.startswith("verdict:") and not started:
            fields["verdict"], started = line.split(":", 1)[1].strip(), True
        elif started and line.startswith("head:"):
            fields["head"] = line.split(":", 1)[1].strip()
        elif started and line.startswith("impact-action:"):
            fields["impact_action"] = line.split(":", 1)[1].strip()
        elif started and line.startswith("impact-class:"):
            fields["impact_class"] = line.split(":", 1)[1].strip().lower()
        elif started and line.startswith("impact-severity:"):
            fields["impact_severity"] = line.split(":", 1)[1].strip()
        elif started and line.startswith("impact-probability:"):
            fields["impact_probability"] = line.split(":", 1)[1].strip()
        elif started and line.startswith("impact-reason:"):
            fields["impact_reason"] = line.split(":", 1)[1].strip()
        elif started and line.startswith("files-read:"):
            section = "files"
        elif started and line.startswith("findings:"):
            section = "findings"
        elif started and section == "files" and line.startswith("- "):
            row = parse_files_read_row(line[2:]) if len(fields["files_read"]) < FILES_READ_ROWS_MAX else None
            if row is None:
                fields["files_problem"] = fields["files_problem"] or "a `files-read:` row is not `- <path> | <lines read> | <lines in its diff>` (numbers): %r" % line[:80]
            else:
                fields["files_read"].append(row)
        elif started and line.startswith("- "):
            parts = [p.strip() for p in line[2:].split("|")]
            if len(parts) >= 2 and parts[1] in ("block", "note"):
                fields["findings"] += 1
                if parts[1] == "block":
                    if not re.fullmatch(r"R-[A-Z]+-[0-9]+", parts[0]) and not re.fullmatch(r"[A-Za-z0-9_.-]+-[0-9]+", parts[0]):
                        fields["notes"] += 1   # a blocking finding that cites no rule is counted as a note; the verdict itself is recorded as given and marked uncited (#1047)
                    else:
                        fields["blocks"] += 1
                else:
                    fields["notes"] += 1
        elif started and line.startswith("```"):
            break
    if not started or fields["verdict"] not in plan_ledger.REVIEW_VERDICTS:
        return None, "the block has no `verdict:` line with one of %s" % ", ".join(plan_ledger.REVIEW_VERDICTS)
    if not fields["head"] or not re.fullmatch(r"[0-9a-f]{7,40}", fields["head"]):
        return None, "the block has no `head: <sha>` line: say which commit was judged"
    if fields["impact_action"] not in (None, "hold", "second-review", "none"):
        return None, "`impact-action:` must be hold, second-review or none"
    if fields["impact_class"] is not None and not IMPACT_CLASS_RE.fullmatch(fields["impact_class"]):
        return None, "`impact-class:` must be one lower-case word naming the class of the impact (see the matrix file's classes)"
    return fields, None


def pr_changed_paths(raw):
    """(paths, None) of the pull request's changed files from GitHub's own list, or (None, why) when that list is not whole: the same test `pr_floor_report` makes (`changedFiles` against the files listed)."""
    files = raw.get("files")
    if not isinstance(files, list):
        return None, "GitHub's list of changed files could not be read"
    paths = [f.get("path") for f in files if isinstance(f, dict) and isinstance(f.get("path"), str)]
    total = raw.get("changedFiles") if isinstance(raw.get("changedFiles"), int) else len(paths)
    if len(paths) < total or raw.get("truncated"):
        return None, "GitHub listed %d of %d changed files" % (len(paths), total)
    return paths, None


def parse_files_read_row(text):
    """(path, lines read, lines in its diff) from `<path> | <lines read> | <lines in its diff>`, or None when it is not that shape."""
    parts = [p.strip() for p in text.rsplit("|", 2)]
    if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or not parts[2].isdigit() or len(parts[1]) > 9 or len(parts[2]) > 9:
        return None
    return parts[0], int(parts[1]), int(parts[2])


FILES_READ_ROWS_MAX = 5000   # the most `files-read:` rows a block may list (the checkout's own file limit)
RATER_CHUNK_LINES = 200   # #1047: the ONE chunk size (diff lines) a rater reads at a time, under the ~280 lines where recorded reads were cut; the agent definition and the refusal text render it from here


def pr_changed_bounds(raw):
    """{path: additions + deletions} from GitHub's own per-file list: a diff has at least that many lines, so no honest count of one is lower (a file with no counts, or none given, is 0)."""
    out = {}
    for f in raw.get("files") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            out[f["path"]] = sum(f[k] for k in ("additions", "deletions") if isinstance(f.get(k), int) and not isinstance(f.get(k), bool) and f[k] > 0)
    return out


def whole_read_problem(files_read, listed, bounds=None):
    """Why a rater's files-read rows are not a whole read of the pull request's changed files `listed` (GitHub's list: paths), or None. Names the missing files and the first unread line.
    `bounds` ({path: additions + deletions} from GitHub's list) is the least a diff can hold: a row whose total or lines read is below it is refused, whatever the rater's own total says."""
    rows = {}
    for path, read, total in files_read:
        rows[path] = (max(read, rows[path][0]) if path in rows else read, max(total, rows[path][1]) if path in rows else total)
    missing = [p for p in listed if p not in rows]
    if missing:
        return "the files-read list is shorter than the pull request's changed files: not read: %s (first unread line: 1 of each) (each row's path is a repo path as in files.txt, e.g. bin/widget-api/app.py)" % ", ".join(_printable_path(p) for p in missing[:10])
    for path in listed:
        read, total = rows[path]
        bound = (bounds or {}).get(path, 0)
        if total < bound or read < bound:
            return "%s: the row says %d lines read of %d in its diff, but GitHub counts %d added and deleted lines, so its diff has at least %d (the bound): read it again to its last line and count them" % (_printable_path(path), read, total, bound, bound)
        if read < total:
            return "%s was read to line %d of %d: the first unread line is line %d (chunks of %d lines; read every file to its last line; a row is `- <path> | <lines read> | <lines in its diff>`, and the third field is the line count of that file's diff, not the changed file's length)" % (_printable_path(path), read, total, read + 1, RATER_CHUNK_LINES)
    return None


IMPACT_CLASS_RE = re.compile(r"[a-z][a-z-]{0,31}")
IMPACT_RATING_FIELDS = ("impact_severity", "impact_probability", "impact_reason")   # the record's rating fields: the code's one table (parse, record, flags)
# #895: `review record --block`: the ONE table of which flags go with it. The refusal, the `--block` help and the impact reviewer's agent definition all say BLOCK_TABLE_TEXT (rendered here, never retyped).
BLOCK_FLAG_TABLE = (   # (flag, dest, mode): `refused` (the block carries it: giving it is an error), `accepted` (goes with --block), `wins` (accepted, and the flag wins over the block's own line)
    ("--reviewer", "reviewer", "accepted"), ("--summary", "summary", "accepted"),
    ("--verdict", "verdict", "refused"), ("--head", "head", "refused"), ("--blocks", "blocks", "refused"), ("--notes", "notes", "refused"), ("--impact-action", "impact_action", "refused"),
    ("--impact-class", "impact_class", "wins"), ("--impact-severity", "impact_severity", "wins"), ("--impact-probability", "impact_probability", "wins"), ("--impact-reason", "impact_reason", "wins"),
    ("--confirms-carry", "confirms_carry", "accepted"),   # (#815) a flag with no value: the reviewer's own confirmation, never a line of the block
)


def block_table_text():
    """The table as one sentence: what `review record --block` refuses and what it accepts."""
    pick = lambda *modes: ", ".join(flag for flag, _, mode in BLOCK_FLAG_TABLE if mode in modes)
    return ("With --block, refused (the block carries them): %s; accepted: %s, and of these %s win over the block's own lines; "
            "the score comes from the block's impact-severity:, impact-probability: and impact-reason: lines, so a block without the first two is recorded WITHOUT a score"
            % (pick("refused"), pick("accepted", "wins"), pick("wins")))


BLOCK_TABLE_TEXT = block_table_text()
NO_SCORE_TEXT = ("recorded WITHOUT a score (no impact severity and probability): it clears nothing by score; add --impact-severity and --impact-probability, or `impact-severity:` and `impact-probability:` lines in the block")
RATING_INPUTS = IMPACT_RATING_FIELDS + ("impact_action", "impact_class", "block", "block_file")   # every input of `review record` that decides the rating: read ONLY by `rating_inputs`, from the raw argv


CARRY_FLAG_INPUTS = ("confirms_carry",)   # (#815) a flag with no value: whether a record confirms a carry
DECISION_INPUTS = ("reviewer", "verdict")   # (#815) what the carry and the score read from a record besides the rating: the reviewer role it is for, and its verdict (a confirmation is an approve)
PRIVATE_INPUTS = RATING_INPUTS + DECISION_INPUTS + CARRY_FLAG_INPUTS   # every input of `review record` that can change what the clear, the carry or the score reads: read ONLY from the raw argv
REVIEW_RECORD_PUBLIC_INPUTS = {   # the inputs of `review record` read from the shared namespace, each with the reason it cannot change what the clear, the carry or the score reads
    "review_cmd": "selects the subcommand; the record branch is the one that writes",
    "pr": "required positional: the record binds to the pull request GitHub resolves from it (`ref` in the audit line); argparse refuses its absence and no value of it reaches a rating, a verdict or a confirmation",
    "head": "only checked against the pull request's head (a different one is refused); the head recorded is GitHub's, never this value",
    "blocks": "a count that can only refuse (an approve with blocking findings); neither the carry nor the score reads it",
    "notes": "a count; neither the carry nor the score reads it",
    "summary": "free text, redacted and clipped; neither the carry nor the score reads it",
}


def _private_parse(raw_argv, values, flags):
    """{dest: value} of the inputs of `values` (each takes a value) and `flags` (each takes none: True) GIVEN in `raw_argv` (a tuple of strings: the command line `main` captured before it built the shared
    parser), parsed by a PRIVATE minimal parser: no abbreviation, no default (an absent input is absent), nothing shared with `build_parser`. A registrar's `set_defaults`, a replaced or re-parsed subparser, a
    same-dest argument or a wrapper on the namespace cannot change what this returns. A raw argv that is not a tuple or list of strings gives nothing."""
    if not isinstance(raw_argv, (tuple, list)) or not all(isinstance(x, str) for x in raw_argv):
        return {}
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, argument_default=argparse.SUPPRESS, exit_on_error=False)
    for name in values:
        parser.add_argument("--" + name.replace("_", "-"), dest=name)
    for name in flags:
        parser.add_argument("--" + name.replace("_", "-"), dest=name, action="store_true", default=argparse.SUPPRESS)
    try:
        parsed, _ = parser.parse_known_args(list(raw_argv))
    except (argparse.ArgumentError, SystemExit):
        return {}
    return dict(vars(parsed))


def rating_inputs(raw_argv):
    """{dest: value} of the rating inputs GIVEN in `raw_argv`: the six flags of RATING_INPUTS, through `_private_parse`. A raw argv that is not a tuple or list of strings gives nothing (no rating). #835."""
    return _private_parse(raw_argv, RATING_INPUTS, ())


def record_inputs(raw_argv):
    """{dest: value} of DECISION_INPUTS and CARRY_FLAG_INPUTS GIVEN in `raw_argv`, through `_private_parse` (#815): the reviewer and the verdict of a record, and whether it confirms a carry. Nothing given
    (or a raw argv that is missing) gives nothing: no reviewer, no verdict, no confirmation."""
    return _private_parse(raw_argv, DECISION_INPUTS, CARRY_FLAG_INPUTS)
