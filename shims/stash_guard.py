"""Refuse the `git stash` forms that change the stash stack, for the fleet's `git` shim (ask #341).

A repository has ONE stash stack, shared by every worktree: a `git stash pop` in one agent's worktree can apply files another agent stashed. Agents
commit to a scratch branch instead. The shim calls `refusal(args, real_git)` with the arguments after `git`; it returns the message to print (and the
shim exits 1 without running git) or None (run git as before). Only `stash list` and `stash show` are let through: every other form, including the bare
`git stash` (which pushes), `git stash -- path`, `-p`, `-m`, an unknown word or an odd spelling, is refused. A guardrail against forgetting, not a security
boundary: a caller that runs git by its full path is not stopped (and `--autostash` is out of scope).

What is resolved before the verb is judged, because git itself resolves it:
- global options (`-C dir`, `-c k=v`, `--git-dir x`, `--no-pager`, ...) are skipped;
- an ALIAS, from `-c alias.x=...`, `--config-env`, the environment (`GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`) or any config file, is looked up by asking
  the real git (`config --get-all alias.<verb>`, same global options), and followed, with a depth limit and loop detection that refuse. A shell alias (`!...`)
  that names stash is refused; one that runs `git ...` reaches this shim again through PATH only when git's exec path holds no `git` of its own (git puts that directory first on PATH for shell aliases), so an obfuscated shell alias can get past;
- a verb git does not know, when `help.autocorrect` would run a near miss, is refused (it could become `stash`); a verb git does not know, when the caller moved
  the exec path (`GIT_EXEC_PATH`, `--exec-path=`), is refused (a `git-<verb>` there could be anything).
A lookup the real git cannot answer cleanly (it times out or cannot start) refuses; a lookup that exits with an error other than "not set" is taken as no alias
(git would fail on the same broken configuration).
"""
import os
import re
import shlex
import subprocess

MESSAGE = (
    "git stash is not available in the fleet's agent shells: a repository has ONE stash stack shared by every worktree, so a pop or apply can carry another agent's "
    "unpushed files into your work.\nCommit to a scratch branch instead:\n"
    "    git switch -c scratch/<name> && git add -A && git commit -m 'wip: <what>'\n"
    "(`git stash list` and `git stash show` still work.)\n"
)
UNSURE = (
    "git: refused: `%s` is not a git command this shim can place, and it could run `git stash` (an alias it cannot read, git's autocorrect, or a moved exec path). "
    "The fleet's agent shells do not use the shared stash stack: commit to a scratch branch instead (git switch -c scratch/<name> && git commit -am 'wip').\n"
)
VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--attr-source"}
READ_ONLY = {"list", "show"}
MAX_ALIAS_DEPTH = 10
LOOKUP_SECONDS = 10
AUTOCORRECT_ON = {"immediate", "prompt", "true", "yes", "on"}
# Git's builtin commands: these can never be an alias and never run an external `git-<verb>`, so a verb in this set needs no lookup.
BUILTINS = set("""add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore check-mailmap check-ref-format checkout checkout-index cherry
cherry-pick clean clone column commit commit-graph commit-tree config count-objects credential describe diff diff-files diff-index diff-tree difftool fast-export
fast-import fetch fetch-pack fmt-merge-msg for-each-ref for-each-repo format-patch fsck gc get-tar-commit-id grep hash-object help hook index-pack init interpret-trailers
log ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base merge-file merge-index merge-tree mktag mktree multi-pack-index mv name-rev notes
pack-objects pack-redundant pack-refs patch-id prune prune-packed pull push range-diff read-tree rebase receive-pack reflog refs remote repack replace replay rerere reset
restore rev-list rev-parse revert rm send-pack shortlog show show-branch show-index show-ref sparse-checkout stage status stripspace submodule switch symbolic-ref tag
unpack-file unpack-objects update-index update-ref update-server-info upload-archive upload-pack var verify-commit verify-pack verify-tag version whatchanged worktree
write-tree""".split())


class Unsure(Exception):
    """The verb cannot be placed (a loop, a timeout): refuse."""


def parse(args):
    """(index of the verb or None, the global options before it as given, the -c/--config-env settings). Only git's own global options are skipped."""
    settings, i = [], 0
    while i < len(args):
        a = args[i]
        if a in VALUE_OPTIONS:
            if i + 1 >= len(args):
                return None, [], settings
            if a in ("-c", "--config-env"):
                settings.append(args[i + 1])
            i += 2
        elif a.startswith("--config-env="):
            settings.append(a.split("=", 1)[1])
            i += 1
        elif a.startswith("-"):
            i += 1
        else:
            return i, args[:i], settings
    return None, [], settings


def is_stash(word):
    return word.strip().lower() == "stash"


def exec_path_moved(globals_):
    return bool(os.environ.get("GIT_EXEC_PATH")) or any(g == "--exec-path" or g.startswith("--exec-path=") for g in globals_)


def real(git, globals_, *rest, drop_exec_path=False):
    """Run the real git read-only; (exit status, stdout) or raise Unsure when it times out or cannot start."""
    env = dict(os.environ)
    if drop_exec_path:
        env.pop("GIT_EXEC_PATH", None)
    try:
        done = subprocess.run([git] + list(globals_) + list(rest), capture_output=True, text=True, timeout=LOOKUP_SECONDS, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise Unsure()
    return done.returncode, done.stdout


def aliases(git, globals_, verb):
    """The alias definitions for verb (all values), [] when none."""
    status, out = real(git, globals_, "config", "--get-all", "alias." + verb)
    return out.splitlines() if status == 0 else []


def autocorrect_active(git, globals_):
    status, out = real(git, globals_, "config", "--get", "help.autocorrect")
    value = out.strip().lower() if status == 0 else ""
    return value in AUTOCORRECT_ON or (value.isdigit() and int(value) > 0)


def known_commands(git, globals_):
    status, out = real(git, [g for g in globals_ if not g.startswith("--exec-path")], "--list-cmds=builtins,main,others", drop_exec_path=True)
    if status != 0:
        raise Unsure()
    return set(out.split())


def stash_forms(rest):
    """Message when `git stash <rest>` is not a plain read, else None."""
    if rest and rest[0] in READ_ONLY:
        return None
    if rest in (["-h"], ["--help"]):
        return None
    return MESSAGE


def judge(args, git, globals_seed=(), depth=0, seen=()):
    """The refusal text for args (the words after `git`), or None."""
    verb_at, globals_, settings = parse(args)
    if verb_at is None:
        return None
    globals_ = list(globals_seed) + globals_
    verb, rest = args[verb_at], args[verb_at + 1:]
    if is_stash(verb):
        return MESSAGE if verb != "stash" else stash_forms(rest)
    if verb in BUILTINS:
        return None
    if depth >= MAX_ALIAS_DEPTH or verb in seen:
        raise Unsure()
    values = aliases(git, globals_, verb)
    if values:
        for value in values:
            if value.startswith("!"):
                if re.search(r"stash", value, re.I):
                    return MESSAGE
                continue   # runs through sh; a `git ...` inside reaches this shim again through PATH
            try:
                words = shlex.split(value)
            except ValueError:
                raise Unsure()
            found = judge(words + rest, git, globals_, depth + 1, seen + (verb,))
            if found:
                return found
        return None
    if exec_path_moved(globals_) or autocorrect_active(git, globals_):
        if verb not in known_commands(git, globals_):
            raise Unsure()
    return None


def refusal(args, git):
    """The message to print when the shim must not run these arguments, else None. Never raises: when the check itself fails, an argument that mentions stash
    in any case is refused and anything else runs."""
    try:
        return judge(list(args), git)
    except Unsure:
        verb_at, _, _ = parse(list(args))
        return UNSURE % re.sub(r"[^\x20-\x7e]", "?", args[verb_at] if verb_at is not None else "?")
    except Exception:
        return MESSAGE if any("stash" in a.lower() for a in args) else None
