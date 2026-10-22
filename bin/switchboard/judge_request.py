# ============================================================================
# PR 9: the tool-call policy judge
#
# One section, so the stack rebases cleanly. docs/switchboard.md, "Tool-call
# policy (PR 9)" and invariant 9. `fleet-switchboard judge-tool` is what the
# `fleet-hooks` plugin runs, once per tool call, with one JSON object on stdin;
# it prints one JSON line and exits 0 whatever happens, because the judge only
# tightens: when it cannot answer, the configured outcome stands.
#
# The daemon, not v2, holds the OpenRouter key: `judge-tool` runs in v2's
# environment (every agent's shell sees it), so it never reads the key and
# never calls Jev. It asks the daemon over $STATE/judge.sock (see "the judge
# socket" below), and the daemon runs the judgement.
#
# Registered in six places: DEFAULTS (here), `validate_config`
# (`validate_policy_config`), `build_parser` (`add_policy_parsers`), `main`
# (the `judge-tool` branch), `cmd_run` (`start_judge_server`) and the fault
# list (`no-policy-judge`).
#
# This section reads GitHub (the ask's Intent, the charter's standing
# authority) through IntentReader, which can only GET, and reads and writes
# nothing else but its two cache files and the audit log. It never touches
# herdr and never writes to GitHub.
# ============================================================================
POLICY_TOOLS = ("shell", "edit", "webfetch", "subagent", "read", "glob", "grep", "skill", "other")
POLICY_PASS_THROUGH = ("read", "glob", "grep", "skill")  # never judged, never sent to Jev
POLICY_REQUEST_OUTCOMES = ("allow", "ask", "deny")       # what the plugin can configure and be told
POLICY_DEFAULTS = {
    "enabled": False,       # off until the plugin is installed and a `jev` object is configured
    "budget_ms": 1500,      # the whole judgement; no answer in time keeps the configured outcome. Jev alone ran to ~1 s in the trial
    "thresholds": POLICY_DEFAULT_THRESHOLDS,  # question id -> probability at which it fires; a configured entry wins per question
    "cache_seconds": 300,   # how long a verdict or a resolved ask is reused; 0 turns both caches off
    "cache_entries": 200,   # entries kept in each cache file
    "routine_filter": True, # issue 87: a read or a local reversible step needs no model (routine_local); false asks the model about every call
    "shadow": False,        # record what the judge would do (audit policy.judge, `shadow`/`verdict`), enforce nothing
}
POLICY_STDIN_LIMIT = 65536  # bytes of request read from stdin
POLICY_ARG_LIMIT = 4000     # characters of a tool's arguments kept
POLICY_WARM_SECONDS = 8     # after replying, how long a late lookup may go on filling the caches
POLICY_CACHE_BYTES = 1 << 20
POLICY_VERDICT_FILE = "policy-verdicts.json"  # hashes and probabilities only
POLICY_CONTEXT_FILE = "policy-context.json"   # resolved asks and sessions: a disposable copy of what GitHub and v2 say
POLICY_SESSION_RE = re.compile(r"^ses_[A-Za-z0-9]{1,80}$")
NO_STANDING_AUTHORITY = ("none stated: nothing is pre-authorised, so treat any merge, deploy, publish, "
                         "message to others or spend as not covered")
POLICY_FIRED_TEXT = {"hard_to_reverse": "hard to reverse",
                     "speaks_for_you": "speaks for you, which the standing authority does not cover",
                     "outside_scope": "outside this agent's assignment"}
POLICY_ACTOR = "judge-tool"


def validate_policy_config(config, source):
    """Check and complete `policy`: unknown subkeys are refused, every value is checked."""
    raw = config.get("policy")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'policy' must be an object, got %r" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - set(POLICY_DEFAULTS))
    if unknown:
        warn_unknown_config("%s: 'policy' contains key(s) this tool does not read: %s (known: %s)"
                          % (source, ", ".join(unknown), ", ".join(POLICY_DEFAULTS)), 'policy', unknown, list(POLICY_DEFAULTS))
    settings = dict(POLICY_DEFAULTS, **raw)
    if not isinstance(settings["enabled"], bool):
        raise ConfigError("%s: 'policy.enabled' must be true or false, got %r" % (source, settings["enabled"]))
    for name in ("budget_ms", "cache_seconds"):
        value = settings[name]
        if not _is_number(value) or value < 0 or (name == "budget_ms" and value <= 0):
            raise ConfigError("%s: 'policy.%s' must be a %s number, got %r"
                              % (source, name, "positive" if name == "budget_ms" else "non-negative", value))
    if not isinstance(settings["shadow"], bool):
        raise ConfigError("%s: 'policy.shadow' must be true or false, got %r" % (source, settings["shadow"]))
    entries = settings["cache_entries"]
    if not (isinstance(entries, int) and not isinstance(entries, bool) and 1 <= entries <= 5000):
        raise ConfigError("%s: 'policy.cache_entries' must be an integer from 1 to 5000, got %r" % (source, entries))
    if not isinstance(settings["routine_filter"], bool):
        raise ConfigError("%s: 'policy.routine_filter' must be true or false, got %r" % (source, settings["routine_filter"]))
    settings["thresholds"] = dict(POLICY_DEFAULT_THRESHOLDS, **_checked_policy_thresholds(settings["thresholds"], source))
    if settings["enabled"] and not config.get("jev"):
        raise ConfigError("%s: 'policy.enabled' needs a 'jev' object: there is no decision model to ask" % source)
    config["policy"] = settings


def _checked_policy_thresholds(thresholds, source):
    if not isinstance(thresholds, dict):
        raise ConfigError("%s: 'policy.thresholds' must be an object of question id -> probability" % source)
    for name, value in thresholds.items():
        if name not in POLICY_QUESTIONS:
            raise ConfigError("%s: 'policy.thresholds' names no question %r (known: %s)"
                              % (source, name, ", ".join(POLICY_QUESTIONS)))
        if not _is_number(value) or not 0 < value <= 1:
            raise ConfigError("%s: 'policy.thresholds.%s' must be a number above 0 and at most 1, got %r"
                              % (source, name, value))
    return dict(thresholds)


# ------------------------------------------------------------- the request

class PolicyRequestError(ValueError):
    """What `judge-tool` was given on stdin is not a usable request."""


def parse_policy_request(text):
    """{"session", "tool", "arguments", "configured"} from the plugin's JSON, or PolicyRequestError."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise PolicyRequestError("the request is not JSON") from None
    if not isinstance(data, dict):
        raise PolicyRequestError("the request must be a JSON object")
    session = data.get("sessionID")
    if not isinstance(session, str) or not POLICY_SESSION_RE.match(session):
        raise PolicyRequestError("sessionID must be a v2 session id (ses_...)")
    if data.get("tool") not in POLICY_TOOLS:
        raise PolicyRequestError("tool must be one of %s" % ", ".join(POLICY_TOOLS))
    if data.get("outcome") not in POLICY_REQUEST_OUTCOMES:
        raise PolicyRequestError("outcome must be one of %s" % ", ".join(POLICY_REQUEST_OUTCOMES))
    arguments = data.get("arguments", "")
    if arguments is None:
        arguments = ""
    elif not isinstance(arguments, str):
        arguments = _canonical(arguments)
    return {"session": session, "tool": data["tool"], "arguments": arguments[:POLICY_ARG_LIMIT],
            "configured": data["outcome"]}


def _guess_configured(text):
    """The configured outcome in a request we could not use, or `allow`, which no plugin applies."""
    try:
        outcome = json.loads(text).get("outcome")
    except (TypeError, ValueError, AttributeError):
        return "allow"
    return outcome if outcome in POLICY_REQUEST_OUTCOMES else "allow"


_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|credential|auth)[A-Za-z0-9_.-]*)"
                r"(\s*[=:]\s*|\s+)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2[redacted]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{8,})"), "[redacted]"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1[redacted]@"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*"), "[redacted]"),   # a JWT (#773)
)


# The sanctioned channel between agents. What a message *says* is not an action: the trial judged
# `fleet-switchboard send cos "... run gh workflow run deploy.yml ..."` as a deploy. The receiving agent
# judges what it then does. Only these read or message; `launch`, `handoff` and `import-v1` act, and stay judged.
POLICY_MESSAGE_COMMANDS = ("send", "intent", "intents", "pending", "status", "remind")


def _plain_words(text):
    """The words of ONE simple command whose arguments cannot run anything: no substitution, no operator, no
    redirect outside quotes, no unbalanced quote. None for anything this is unsure about."""
    if not isinstance(text, str) or not text.strip():
        return None
    quote, words, word, any_word = None, [], [], False
    for ch in text.strip():
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                word.append(ch)
        elif quote == '"':
            if ch in "`$\\":
                return None  # substitution or an escape inside double quotes
            if ch == '"':
                quote = None
            else:
                word.append(ch)
        elif ch in "'\"":
            quote, any_word = ch, True
        elif ch in "`$\\;&|<>(){}*?[]~#!\n\r":
            return None  # an operator, expansion, glob, comment or a second line outside any quote
        elif ch in " \t":
            if word or any_word:
                words.append("".join(word))
                word, any_word = [], False
        else:
            word.append(ch)
    if quote is not None:
        return None
    if word or any_word:
        words.append("".join(word))
    return words


# ---- the routine-local pre-filter (issue 87). The judge compared every step of an agent to the ask's Done-when and
# asked "outside the intent?" of a `git grep`, a test run or a report to its boss: 77 of 80 would-deny/ask verdicts in a
# labelled sample were wrong. A plainly local, read-only or reversible step needs no model: the code answers "nothing at
# its threshold" for it. It only ever REMOVES the judge's own extra strictness for a call it can read with certainty; the
# configured outcome is combined as always (never loosened: invariant 9).
#
# FAIL CLOSED, a strict allowlist (review of PR #90): a command is routine only when it is ONE simple command, with no
# environment prefix, no redirect, no chaining, pipe or substitution, no wrapper (`nice`, `timeout`), no `..`, no absolute or home
# path and no dotfile or credential-looking name, whose command word is in the table below and whose EVERY flag is listed for
# it (an unknown flag, a flag with an unlisted value, or a missing required argument is not routine). Anything else, including
# every `sed`, `sort`, `rg`, `find`, `gh`, `cp`, `rm`, `curl`, shell, interpreter script or heredoc, goes to the model as before.
# A symlink inside the worktree that points outside it cannot be seen from the text: the model still judges what such a link
# is for only when a path is unusual; this is a known limit, not a proof.
_ROUTINE_NAME = re.compile(r"^[A-Za-z0-9._/@^+=-]{1,200}$")
_ROUTINE_SECRETISH = re.compile(r"(?i)(secret|credential|token|passw|\.pem$|\.key$|\.p12$|id_rsa|id_ed25519|netrc|npmrc|pypirc|keystore|private)")
_ROUTINE_NUMBER = re.compile(r"^[0-9]{1,6}$")


def _routine_path(word):
    """A path or revision that stays inside the worktree: relative, no `..`, no dotfile or credential-looking part, plain characters."""
    if not _ROUTINE_NAME.match(word) or word.startswith(("/", "-", "=")) or _ROUTINE_SECRETISH.search(word):
        return False
    if ".." in word:
        return False
    for part in word.split("/"):
        if part.startswith(".") and part not in (".", ""):
            return False
    return "=" not in word


# command -> (flags without a value, flags with a value that is a number, flags with a free text value, minimum paths, kind of positionals)
F = lambda text: set(text.split())  # the flags of a command, one string each: the allowlist is data
_ROUTINE_SPEC = {
    ("ls",): (F("-l -a -1 -h -t -la -al -lh -lah -lt -ltr -lR -R -d"), set(), set(), 0, "paths"),
    ("cat",): (F("-n"), set(), set(), 1, "paths"),
    ("head",): (set(), F("-n"), set(), 1, "paths"),
    ("tail",): (set(), F("-n"), set(), 1, "paths"),
    ("wc",): (F("-l -w -c"), set(), set(), 1, "paths"),
    ("grep",): (F("-n -i -l -c -w -F -E -r -I -v -h -H -s -q"), set(), set(), 2, "pattern"),
    ("git", "grep"): (F("-n -i -l -c -w -F -E -I -v -h --no-color --cached"), set(), set(), 0, "pattern"),
    ("git", "status"): (F("-s --short -b --branch --porcelain --no-color"), set(), set(), 0, "paths"),
    ("git", "diff"): (F("--stat --name-only --name-status --cached --staged --no-color --numstat --shortstat --"), set(), set(), 0, "paths"),
    ("git", "log"): (F("--oneline --graph --decorate --stat --name-only --no-color --"), F("-n"), set(), 0, "paths"),
    ("git", "show"): (F("--stat --name-only --oneline --no-color --"), set(), set(), 0, "paths"),
    ("git", "rev-parse"): (F("--short --abbrev-ref --verify"), set(), set(), 1, "paths"),
    ("git", "add"): (F("-A --all -u --update --"), set(), set(), 0, "paths"),
    ("git", "commit"): (F("-a --amend --no-edit --allow-empty -q"), set(), F("-m"), 0, "none"),
}
_ROUTINE_NUMERIC_FLAG = re.compile(r"^-[0-9]{1,4}$")  # git log -5, head -5, tail -5
_ROUTINE_ISSUE_REF = re.compile(r"^(?:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#?[1-9][0-9]{0,8}$")
_ROUTINE_PR_REF = re.compile(r"^(?:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9][0-9]{0,8}|https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/[1-9][0-9]{0,8})$")
_ROUTINE_NAME_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")
_ROUTINE_WHEN = re.compile(r"^(?:[0-9]{1,5}[smhd]|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{5,12}(?:Z|[+-][0-9]{2}:[0-9]{2}))$")


# A message that talks of deploying, merging, publishing, approving, spending, deleting or credentials may BE the authorisation (a
# labelled sample had a "Hold lifted: deploy ..." send that asking was right for): such a message is never routine, so the model reads it.
_ROUTINE_CONSEQUENTIAL = re.compile(
    r"(?i)(deploy|merg|publish|releas|ship|approv|go[- ]ahead|green[- ]?light|prod\b|production|spend|spent|purchas|pay(?:ment|ing|s)?\b|delet|drop\b|"
    r"force|roll(?:out|back)|credential|secret|token|passw|key\b|authori|permission|lifted|unblock|proceed|sign[- ]?off|confirm)")


def _routine_text_after(rest, issue_flag=True):
    """(ok, words): the message words of a send/report/remind, with an optional `--issue REF` anywhere; no other flag, and no
    message word that looks like a flag."""
    text, i = [], 0
    while i < len(rest):
        w = rest[i]
        if w == "--issue" and issue_flag:
            if i + 1 >= len(rest) or not _ROUTINE_ISSUE_REF.match(rest[i + 1]):
                return False, []
            i += 1
        elif w.startswith("-") or not w:
            return False, []
        elif _ROUTINE_CONSEQUENTIAL.search(w):
            return False, []
        else:
            text.append(w)
        i += 1
    return bool(text), text


def _routine_fleet(rest):
    """What the fleet says UP to its boss, and its plain reads: report, notice pr, intent, orders list, decisions list. NOT send or remind
    (they reach other agents, so free text in them is the model's to read), and never decisions answer/resolve/escalate/supersede/batch,
    orders add/remove, launch, handoff."""
    if not rest:
        return False
    action, args = rest[0], rest[1:]
    if action == "report":
        return len(args) >= 2 and args[0] in _ROUTINE_REPORT_STATES and _routine_text_after(args[1:])[0]
    if action == "notice":
        return len(args) >= 2 and args[0] == "pr" and bool(_ROUTINE_PR_REF.match(args[1])) and (len(args) == 2 or _routine_text_after(args[2:], issue_flag=False)[0])
    if action == "intent":
        return len(args) <= 1 and all(_ROUTINE_ISSUE_REF.match(w) for w in args)
    if action == "orders":
        return (args == ["--cos"]) or (args[:1] == ["list"] and args[1:3] and args[1] == "--charter" and len(args) >= 3
                                       and bool(_ROUTINE_NUMBER.match(args[2])) and args[3:] in ([], ["--json"]))
    if action == "decisions":
        return args in ([], ["list"])
    return False


# gh READS only: a pr or issue view/list/checks/diff and a GET of a repo's issues, pulls or comments. Never merge, review, close,
# edit, create, comment, `gh repo`, `gh auth`, `gh api` with any flag that writes, or anything that opens a browser.
_ROUTINE_GH_READ = {"pr": ("view", "list", "checks", "diff"), "issue": ("view", "list")}
_ROUTINE_GH_FLAG_VALUE = {"--repo": re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"), "-R": re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"),
                          "--json": re.compile(r"^[A-Za-z][A-Za-z,]{0,300}$"), "--state": re.compile(r"^(?:open|closed|merged|all)$"),
                          "--limit": _ROUTINE_NUMBER, "-L": _ROUTINE_NUMBER,
                          "--label": re.compile(r"^[A-Za-z0-9._:-]{1,60}$"), "--author": re.compile(r"^(?:@me|[A-Za-z0-9-]{1,39})$")}
_ROUTINE_GH_BARE_FLAGS = frozenset("--comments --name-only --patch --stat".split())
_ROUTINE_GH_API_PATH = re.compile(r"^repos/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(?:issues(?:/comment[s]|/[1-9][0-9]{0,8}(?:/comment[s])?)?|pulls(?:/comment[s]|/[1-9][0-9]{0,8}(?:/(?:comment[s]|reviews|files|commits))?)?|comment[s])(?:\?[A-Za-z0-9_=&.%+:-]{0,200})?$")


def _routine_gh(rest):
    if len(rest) >= 2 and rest[0] in _ROUTINE_GH_READ and rest[1] in _ROUTINE_GH_READ[rest[0]]:
        i, seen = 2, 0
        while i < len(rest):
            w = rest[i]
            if w in _ROUTINE_GH_FLAG_VALUE:
                if i + 1 >= len(rest) or not _ROUTINE_GH_FLAG_VALUE[w].match(rest[i + 1]):
                    return False
                i += 1
            elif w in _ROUTINE_GH_BARE_FLAGS:
                pass
            elif _ROUTINE_NUMBER.match(w) and i == 2:
                pass  # the number of the pr or issue
            else:
                return False
            i += 1
        return True
    if rest[:1] == ["api"] and len(rest) >= 2 and _ROUTINE_GH_API_PATH.match(rest[1]):
        return all(w == "--paginate" for w in rest[2:])  # no --jq/-q (it can read the environment), and no other flag
    return False


_ROUTINE_NO_ARG = {("pwd",), ("uptime",), ("true",)}
_ROUTINE_REPORT_STATES = ("done", "failed", "blocked", "question", "working", "paused", "withdrawn")


def _routine_mid_word_hash(text):
    """`text` with each unquoted `#` that sits inside a word (`owner/repo#7`: the shell starts a comment only at the start of a word)
    replaced by a placeholder, so `_plain_words`, which refuses every unquoted `#`, still refuses a real comment."""
    out, quote, prev = [], None, " "
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and not prev.isspace() and prev not in ";&|<>(){}":
            ch = "\x1f"
        out.append(ch)
        prev = ch
    return "".join(out)


def routine_local(text):
    """True only for ONE simple command that is a read or a local reversible step inside the worktree and is on the strict
    allowlist above, flags included. False for anything this is unsure about."""
    if not isinstance(text, str) or not text.strip() or len(text) > POLICY_ARG_LIMIT or "\r" in text or "\x00" in text:
        return False
    words = _plain_words(_routine_mid_word_hash(text))  # no operator, redirect, substitution, glob, comment, `~`, heredoc or unbalanced quote (a newline only inside quotes)
    words = [w.replace("\x1f", "#") for w in words] if words else words
    if not words or "=" in words[0] or "/" in words[0]:
        return False
    if words[0] == "timeout" and len(words) > 2 and _ROUTINE_NUMBER.match(words[1]) and words[2] in (PROG, "gh"):
        words = words[2:]  # `timeout 60 fleet-switchboard ...`: the same command under a time limit, for the two command words that need it
    if words[0] not in (PROG, "gh") and (len(text) > 1000 or "\n" in text):
        return False  # only a message may be long or span lines (inside its quotes)
    head = words[0]
    if head == PROG:
        return _routine_fleet(words[1:])
    if head == "gh":
        return _routine_gh(words[1:])
    if head == "sleep":
        return len(words) == 2 and bool(_ROUTINE_NUMBER.match(words[1]))
    if head == "node":  # node --test <paths>: the agent's own tests
        return words[1:2] == ["--test"] and len(words) > 2 and all(_routine_path(w) for w in words[2:])
    if head in ("python3", "python") and len(words) >= 2 and re.match(r"^bin/test-[A-Za-z0-9_-]+$", words[1]):
        return all(re.match(r"^[A-Za-z0-9_.]+$", w) for w in words[2:])  # `python3 bin/test-x TestClass.test_name`
    key = (head, words[1]) if head == "git" and len(words) > 1 else (head,)
    if key in _ROUTINE_NO_ARG:
        return len(words) == 1
    spec = _ROUTINE_SPEC.get(key)
    if spec is None:
        return False
    flags, numeric, texty, need, kind = spec
    rest = words[len(key):]
    paths, patterns, i, only_paths = [], [], 0, False
    while i < len(rest):
        w = rest[i]
        if only_paths or not w.startswith("-"):
            if kind == "pattern" and not patterns and not only_paths:
                if not w or w.startswith("-"):
                    return False
                patterns.append(w)
            elif kind == "none" or not _routine_path(w):
                return False
            else:
                paths.append(w)
        elif w == "--" and "--" in flags:
            only_paths = True
        elif w in flags or (re.match(r"^-[A-Za-z]{2,}$", w) and all("-" + c in flags for c in w[1:])):
            pass  # a listed flag, or a cluster of listed single-letter flags (`-rn`)
        elif w in numeric:
            if i + 1 >= len(rest) or not _ROUTINE_NUMBER.match(rest[i + 1]):
                return False
            i += 1
        elif w in texty:
            if i + 1 >= len(rest) or not rest[i + 1] or rest[i + 1].startswith("-"):
                return False
            i += 1
        elif _ROUTINE_NUMERIC_FLAG.match(w) and (numeric or key == ("git", "log")):
            pass
        else:
            return False  # an unknown flag: not routine
        i += 1
    if kind == "pattern":
        return bool(patterns) and (key[0] == "git" or bool(paths))
    if key == ("git", "add") and not paths and not any(w in ("-A", "--all", "-u", "--update") for w in rest):
        return False  # `git add` of nothing
    return len(paths) >= need and (key != ("git", "commit") or "-m" in rest or "--amend" in rest or "--no-edit" in rest)


def _decisions_subcommand(rest):
    """The subcommand word of `decisions ...` (after its flags), or None for a plain list."""
    skip = False
    for word in rest:
        if skip:
            skip = False
        elif word in ("--repo", "--tier", "--from"):
            skip = True
        elif not word.startswith("-"):
            return word
    return None


def plain_switchboard_message(text):
    """True only for ONE simple `fleet-switchboard <send|intent|...> ...` command whose arguments cannot run
    anything (see _plain_words). Anything this is unsure about is judged like any other shell command.

    PR 18: `decisions` (a read, `list`, or `escalate`) and `orders --cos` (a read) are plain too. `decisions
    resolve`, `decisions answer` and `decisions supersede` are NOT: they act for the owner, so the judge reads them against the Chief of
    Staff's standing orders (the `answer-unjudged` fault lets them through). `decisions batch` writes an issue to GitHub,
    so it is judged too, however its quoted rows are written (it was plain: a quoted pipe is text)."""
    words = _plain_words(text)
    if not words or len(words) < 2 or os.path.basename(words[0]) != PROG:
        return False
    if words[1] in POLICY_MESSAGE_COMMANDS:
        return True
    if words[1] == "decisions":
        return _decisions_subcommand(words[2:]) not in ("resolve", "answer", "supersede", "batch") or fault("answer-unjudged")
    return words[1] == "orders" and words[2:] == ["--cos"]


def decision_gloss(state_dir, command):
    """For a judged `decisions answer|resolve <id>`: one line saying what that decision is, read from the file the
    daemon keeps, so the judge's question sees the request and not just its id. Empty when it cannot say."""
    words = _plain_words(command)
    if not words or len(words) < 4 or os.path.basename(words[0]) != PROG or words[1] != "decisions":
        return ""
    rest = words[2:]
    sub = _decisions_subcommand(rest)
    if sub not in ("resolve", "answer", "supersede") or rest.index(sub) + 1 >= len(rest):
        return ""
    ident = rest[rest.index(sub) + 1]
    body = read_decisions_file(Path(state_dir) / DECISIONS_FILE)
    for decision in (body or {}).get("decisions", []):
        if isinstance(decision, dict) and decision.get("id") in (ident, "#" + ident.lstrip("#")):
            return " [the decision it acts on: %s by %s, %s tier: %s]" % (
                decision.get("kind"), decision.get("agent"), decision.get("tier"), _one_line(decision.get("title"), 300))
    return ""


def redact_command_text(text, extra=()):
    """`text` with credential-shaped parts replaced, and any value in `extra` (a known key) removed.

    A tool's arguments go to a third-party model and to the audit log; neither
    may see a credential. This is a net, not a promise: it catches the usual shapes.
    """
    text = "" if text is None else str(text)
    for secret in extra:
        if secret:
            text = text.replace(secret, "[redacted]")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ------------------------------------------------------- standing authority

_STANDING_AUTHORITY = re.compile(r"^(?:[-*]\s+)?\**standing authority\**\s*:\s*\**\s*(.*?)\**\s*$", re.IGNORECASE)

# PR 17: standing orders. The charter issue's `## Standing orders` section, one order per bullet,
# `- <text> (ordered by <name>, <YYYY-MM-DD>)`; the one-line `Standing authority:` stays valid and
# counts as one more order. `parse_orders` is the one parser.
ORDERS_HEADING = "## Standing orders"
ORDER_MAX_CHARS = 300    # characters of one order's text
_ORDERS_HEADING = re.compile(r"^#{1,6}\s+standing orders\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,6}\s")
_ORDER_BULLET = re.compile(r"^[-*]\s+(\S.*?)\s*$")


class OrdersError(Exception):
    """An order that cannot be recorded or removed, and why."""


def _legacy_authority(body):
    """The charter's one-line `Standing authority:` text, or None when it states none.

    It lives in the charter issue's prose, below the yaml block; fenced code is
    ignored, the first such line wins, and "none" counts as none stated.
    """
    fenced = False
    for line in (body or "").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        match = None if fenced else _STANDING_AUTHORITY.match(line.strip())
        if match:
            text = match.group(1).strip()
            return None if not text or text.lower().rstrip(".") in ("none", "none stated", "n/a") else text
    return None


def _orders_section(body):
    """The first `## Standing orders` section outside a fenced block: None, or a dict with the character
    offsets `start` (its heading), `head_end`, `end` (the next heading, or the end of the body) and
    `bullets`, a list of (start, end, text), one per order, in the body's own bytes."""
    body = body or ""
    fenced, offset, found = False, 0, None
    for line in body.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if found is None:
            if _ORDERS_HEADING.match(stripped):
                found = {"start": start, "head_end": offset, "end": len(body), "bullets": []}
            continue
        if _ANY_HEADING.match(stripped):
            found["end"] = start
            break
        match = _ORDER_BULLET.match(stripped)
        if match:
            found["bullets"].append((start, offset, match.group(1)))
    return found


def section_orders(body):
    """The orders of the `## Standing orders` section alone, in order."""
    found = _orders_section(body)
    return [text for _, _, text in found["bullets"]] if found else []


def parse_orders(body):
    """Every standing order the charter body states: the section's bullets in order, then the legacy
    `Standing authority:` line when there is one. [] when there are none."""
    legacy = _legacy_authority(body)
    return section_orders(body) + ([legacy] if legacy else [])


def parse_standing_authority(body):
    """The orders joined into one line, or None when the charter states none (the old one-line form,
    kept for its callers: it answers through `parse_orders`)."""
    orders = parse_orders(body)
    return "; ".join(orders) if orders else None


def _eol_of(body):
    return "\r\n" if "\r\n" in body else "\n"


def add_order_to_body(body, text, by, date):
    """`body` with one more order, `- <text> (ordered by <by>, <date>)`, added to the section (made at the
    end of the body when there is none). Every byte outside the section is kept. OrdersError for a text
    that is empty, longer than ORDER_MAX_CHARS or on more than one line."""
    body = body or ""
    text = (text or "").strip()
    by = " ".join(str(by or "").split())[:60]
    if not text:
        raise OrdersError("an order needs some text")
    if "\n" in text or "\r" in text:
        raise OrdersError("an order is one line")
    if len(text) > ORDER_MAX_CHARS:
        raise OrdersError("an order is at most %d characters; this one has %d" % (ORDER_MAX_CHARS, len(text)))
    if not by:
        raise OrdersError("an order records who ordered it (--by)")
    eol = _eol_of(body)
    bullet = "- %s (ordered by %s, %s)" % (text, by, date)
    found = _orders_section(body)
    if found is not None:
        if found["bullets"]:
            at = found["bullets"][-1][1]
            lead = "" if body[:at].endswith(("\n", "\r")) else eol
            return body[:at] + lead + bullet + eol + body[at:]
        at = found["head_end"]
        lead = "" if body[:at].endswith(("\n", "\r")) else eol
        return body[:at] + lead + eol + bullet + eol + body[at:]
    if not body:
        sep = ""
    elif body.endswith(eol + eol):
        sep = ""
    elif body.endswith(eol):
        sep = eol
    else:
        sep = eol + eol
    return body + sep + ORDERS_HEADING + eol + eol + bullet + eol


def remove_order_from_body(body, number):
    """`body` without order `number` (as `orders list` numbers them). Only the section changes; when the last
    order goes the section goes with it. The legacy `Standing authority:` line is numbered after the section's
    orders but lies outside the section, so it is refused: edit it on GitHub."""
    body = body or ""
    found = _orders_section(body)
    bullets = found["bullets"] if found else []
    total = len(parse_orders(body))
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= total:
        raise OrdersError("no order %s: this charter has %d" % (number, total))
    if number > len(bullets):
        raise OrdersError("order %d is the legacy `Standing authority:` line, outside the %s section: edit or "
                          "delete that line on GitHub" % (number, ORDERS_HEADING))
    start, end, _ = bullets[number - 1]
    kept = [line for line in body[found["start"]:found["end"]].splitlines()
            if line.strip() and not _ORDERS_HEADING.match(line.strip())]
    if len(bullets) == 1 and len(kept) == 1:  # nothing but this order: the section goes
        start, end = found["start"], found["end"]
        out = body[:start] + body[end:]
        eol = _eol_of(body)
        if end == len(body) and out.endswith(eol + eol):
            out = out[:-len(eol)]  # the blank line that set the section off
        return out
    return body[:start] + body[end:]


def os_user():
    """The operating-system user, the default for `orders add --by`."""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def fetch_charter_body(reader, charter, repo):
    """The charter issue's body (or None) through the reader's GET-only `gh api`: the one read of it."""
    data = reader._api("repos/%s/issues/%d" % (repo, charter), repo)
    return data.get("body") if isinstance(data, dict) and isinstance(data.get("body"), str) else None


def fetch_orders(reader, charter, repo):
    """The charter issue's standing orders (a list) through the reader's GET-only `gh api`."""
    return parse_orders(fetch_charter_body(reader, charter, repo))


def fetch_standing_authority(reader, charter, repo):
    """The orders joined into one line, or None (kept for the callers that want a single string)."""
    orders = fetch_orders(reader, charter, repo)
    return "; ".join(orders) if orders else None


def resolve_policy_context(reader, fleet):
    """{"issue", "intent", "done_when", "authority"} for the agent's ask, from GitHub (IntentError on failure).

    The ask is the agent's issue, or its parent when the issue is a work item;
    an orchestrator with only a charter has no ask (`issue` None). The
    authority comes from the charter above the ask, or the charter itself.
    """
    repo = fleet.get("repo") or reader.repo
    issue, charter = fleet.get("issue"), fleet.get("charter")
    ask = intent = done_when = None
    if _is_issue_number(issue) and reader.issue(issue, repo).charter:
        issue, charter = None, issue
    if _is_issue_number(issue):
        ask, work = reader.ask_of(issue, repo)
        found = reader.issue(ask, repo)
        intent, done_when = found.intent, found.done_when
        if work is not None and reader.issue(work, repo).intent:
            intent = "%s (work item: %s)" % (intent or NO_INTENT, reader.issue(work, repo).intent)
        charter = reader.parent(ask, repo)
    orders = authority = None
    if _is_issue_number(charter):
        body = fetch_charter_body(reader, charter, repo)
        orders = parse_orders(body)
        # PR 17: the model's question sees every order; `orders-never-judged` shows it only the old one-line form
        seen = [_legacy_authority(body)] if fault("orders-never-judged") else orders
        seen = [text for text in seen if text]
        authority = "; ".join(seen) if seen else None
    return {"issue": ask if intent else None, "intent": intent, "done_when": done_when, "authority": authority,
            "charter": charter if _is_issue_number(charter) else None, "charter_repo": repo, "orders": orders or []}


# PR 17: an agent must not edit the charter it works under: that would grant itself authority. This is a
# deterministic rule beside the model's questions. It is a guard, not a proof: a shell can build the call
# out of pieces this text never shows (a variable, a script, an alias, another tool), and the rule sees only
# the agent's own charter. It reads the command's words, as the redaction does.
_GH_ISSUE_EDIT = re.compile(r"\bgh(?:\s+(?:-R|--repo)[ =]\S+)?\s+issue\s+edit\b([^;&|\n]*)", re.IGNORECASE)
_GH_API = re.compile(r"\bgh(?:\s+(?:-R|--repo)[ =]\S+)?\s+api\b([^;&|\n]*)", re.IGNORECASE)
_GH_REPO_FLAG = re.compile(r"\bgh\s+(?:-R|--repo)[ =]['\"]?([\w.-]+/[\w.-]+)", re.IGNORECASE)
_REPO_FLAG = re.compile(r"(?:^|\s)(?:-R|--repo)(?:\s+|=)['\"]?([\w.-]+/[\w.-]+)", re.IGNORECASE)
_API_WRITE_METHOD = re.compile(r"(?:-X|--method)(?:\s+|=)?['\"]?(?:PATCH|PUT|DELETE)\b", re.IGNORECASE)
_API_ISSUE_PATH = re.compile(r"repos/([\w.-]+/[\w.-]+)/issues/(\d+)(?![\w/])")
_ISSUE_WORD = re.compile(r"^['\"]?(?:(?:([\w.-]+/[\w.-]+)#|#)(\d+)|(\d+)|\S*?(?:([\w.-]+/[\w.-]+)/issues/)(\d+))['\"]?$")


def charter_edit_target(command, charters):
    """The number of the charter that a shell command rewrites with gh's issue subcommand or `gh api` PATCH, PUT
    or DELETE of `repos/<repo>/issues/<n>`, or None. `charters` holds what the agent works under: a bare number
    (any repo) or a `(repo, number)` pair, which a command that names another repo (`--repo`, `-R`, a URL, an
    `owner/repo#N` word or the api path) does not hit: issue 1 of another repo is not this charter."""
    text = command if isinstance(command, str) else ""

    def owns(repo, number):
        return any(number == (c[1] if isinstance(c, tuple) else c) and (repo is None or not isinstance(c, tuple)
                                                                        or c[0] is None or c[0].lower() == repo.lower())
                   for c in charters)

    for found in _GH_ISSUE_EDIT.finditer(text):
        rest, repo = found.group(1), None
        flagged = _REPO_FLAG.search(" " + rest) or _GH_REPO_FLAG.search(found.group(0))
        if flagged:
            repo = flagged.group(1)
        for word in rest.split():
            match = _ISSUE_WORD.match(word)
            if not match:
                continue
            named = match.group(1) or match.group(4)
            number = int(match.group(2) or match.group(3) or match.group(5))
            if owns(named or repo, number):
                return number
    for found in _GH_API.finditer(text):
        rest = found.group(1)
        path = _API_ISSUE_PATH.search(rest)
        if path and _API_WRITE_METHOD.search(rest) and owns(path.group(1), int(path.group(2))):
            return int(path.group(2))
    return None


# ------------------------------------------------------------------ the caches

class BoundedFileCache:
    """A small JSON file of {key: {at, value}}: bounded, expiring, private, replaceable.

    It is a cache and nothing else: deleting it loses only speed (invariant 8).
    Written atomically (a temp file, mode 0600, then rename); a missing,
    oversized or corrupt file reads as empty; a write that fails is not an error.
    A ttl of 0 turns it off.
    """

    def __init__(self, path, now, ttl, limit):
        self.path, self.now, self.ttl, self.limit = Path(path), now, ttl, limit

    def _fresh(self):
        if not self.ttl:
            return {}
        try:
            if self.path.stat().st_size > POLICY_CACHE_BYTES:
                return {}
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {key: entry for key, entry in entries.items()
                if isinstance(entry, dict) and _is_number(entry.get("at")) and self.now() - entry["at"] < self.ttl
                and self.now() >= entry["at"]}

    def get(self, key):
        entry = self._fresh().get(key)
        return entry["value"] if entry else None

    def put(self, key, value):
        """Store one entry, dropping the oldest beyond the limit. True when it was written."""
        if not self.ttl:
            return False
        entries = self._fresh()
        held = entries.get(key)
        if held is not None and held.get("value") == value and self.now() - held["at"] < self.ttl / 2:
            return True   # the same answer, still young: rewriting the whole file to move its time would only touch the disk
        entries[key] = {"at": self.now(), "value": value}
        for old in sorted(entries, key=lambda k: entries[k]["at"])[:max(0, len(entries) - self.limit)]:
            del entries[old]
        temp = self.path.with_name("%s.%d.tmp" % (self.path.name, os.getpid()))
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"entries": entries}, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(self.path))
            return True
        except OSError:
            try:
                os.unlink(str(temp))
            except OSError:
                pass
            return False


# ------------------------------------------------------------------- the reply

def policy_reply(outcome, reason, judged=False, cached=False, latency_ms=0):
    """The one JSON line `judge-tool` prints."""
    return {"outcome": outcome, "reason": _one_line(reason, 600), "judged": bool(judged),
            "cached": bool(cached), "latency_ms": int(latency_ms)}


def policy_unjudged_reason(config, request):
    """Why this call is not judged at all (a sentence), or None when it is to be judged."""
    if request["configured"] == "deny":
        return "a configured deny is final: the judge was not consulted"
    if request["tool"] in POLICY_PASS_THROUGH:
        return "not judged: a %s passes straight through" % request["tool"]
    if not config["policy"]["enabled"]:
        return "the policy judge is not enabled"
    if fault("no-policy-judge"):
        return "no-policy-judge fault: the judge is off"
    return None


def _what_text(request):
    return "%s: %s" % (request["tool"], _one_line(request["arguments"], 100))


def escalation_reason(fleet, ask, request):
    """The denial text for a call outside the ask's Intent: escalate, never argue."""
    where = " (ask #%d)" % ask if ask else ""
    route = ("Send the Chief of Staff the proposal with `%s send <chief-of-staff> --issue <n> \"...\"`" % PROG)
    if (fleet or {}).get("role") == "orchestrator":
        route += ", or label your charter awaiting-user with a Decision required section"
    return ("Denied: this call is outside the ask's Intent%s (%s). Do not retry it or work around it. Escalate: "
            "%s. Only the person you report to widens the ask." % (where, _what_text(request), route))


def tightened_reason(combined, fleet, ask, request):
    """The sentence for the outcome the judge reached, for the agent or for you."""
    fired = combined["fired"]
    if "outside_intent" in fired and combined["outcome"] in ("deny", "hard-deny"):
        return escalation_reason(fleet, ask, request)
    if fired and combined["outcome"] != "allow":
        return "Needs your yes: %s (%s)." % ("; ".join(POLICY_FIRED_TEXT[n] for n in fired if n in POLICY_FIRED_TEXT),
                                            _what_text(request))
    return "judge: nothing at its threshold; the configured outcome stands"


def policy_verdict_key(request, fleet_name, context, role):
    """A hash of everything the verdict depends on, for the cache: no text is kept."""
    parts = {"session": request["session"], "agent": fleet_name, "role": role, "ask": context["issue"],
             "intent": context["intent"], "done_when": context["done_when"], "authority": context["authority"],
             "tool": request["tool"], "arguments": request["arguments"]}
    return hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()[:32]


def _valid_answers(value):
    answers = value.get("answers") if isinstance(value, dict) else None
    ok = isinstance(answers, dict) and set(answers) == set(POLICY_QUESTIONS) \
        and all(_is_probability(p) for p in answers.values())
    return answers if ok else None


def _valid_session_fleet(value):
    fleet = value.get("fleet") if isinstance(value, dict) else None
    return fleet if isinstance(fleet, dict) and isinstance(fleet.get("name"), str) else None


def _valid_context(value):
    keys = ("issue", "intent", "done_when", "authority")
    ok = isinstance(value, dict) and all(k in value for k in keys) \
        and all(value[k] is None or isinstance(value[k], str) for k in keys[1:]) \
        and (value["issue"] is None or _is_issue_number(value["issue"]))
    if not ok:
        return None
    context = {k: value[k] for k in keys}
    # PR 17: the charter and its orders ride along (an older cache entry has neither: they read as none)
    orders = value.get("orders")
    context["charter"] = value.get("charter") if _is_issue_number(value.get("charter")) else None
    context["charter_repo"] = value["charter_repo"] if isinstance(value.get("charter_repo"), str) \
        and REPO_RE.match(value["charter_repo"]) else None
    context["orders"] = [t for t in orders if isinstance(t, str)] if isinstance(orders, list) else []
    return context
