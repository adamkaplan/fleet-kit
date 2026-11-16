# ---- ask #2363: where the fleet makes a worktree. One config key, `worktree_root` (default ~/Code/.fleet; layout <root>/<repo>/<name>).
# The root must itself be inside ~/Code (below it, never ~/Code itself) or inside the temp directory. A NEW local worker whose worktree would be a new
# top-level entry of ~/Code (its parent is ~/Code) is refused with a text that names the root. A path under the root, one in the temp directory and
# any deeper path are accepted. Pure: it reads the configuration, the home directory and the temp directory name, and touches no file.
import os
import tempfile

WORKTREE_ROOT_KEY = "worktree_root"
WORKTREE_ROOT_DEFAULT = "~/Code/.fleet"
CODE_DIR = "~/Code"


def _real(path):
    return os.path.realpath(os.path.expanduser(path))


def _inside(path, base):
    """True when `path` is strictly below `base` (both already real paths)."""
    return path != base and path.startswith(base.rstrip("/") + "/")


def worktree_root(config):
    """The configured root as a text with `~` kept (the default when the key is absent or null)."""
    raw = config.get(WORKTREE_ROOT_KEY) if isinstance(config, dict) else None
    return raw if isinstance(raw, str) and raw else WORKTREE_ROOT_DEFAULT


def _root_problem(raw):
    if not isinstance(raw, str) or not raw.strip() or raw != raw.strip() or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return "must be a path on one line"
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return "must be an absolute path (or start with ~/)"
    real = os.path.realpath(expanded)
    if not (_inside(real, _real(CODE_DIR)) or _inside(real, os.path.realpath(tempfile.gettempdir()))):
        return "must be inside %s or the temp directory (and not %s itself)" % (CODE_DIR, CODE_DIR)
    return None


def validate_worktree_root(config, source):
    """`worktree_root`: null or absent is the default; else a path inside ~/Code (below it) or the temp directory. A bad value is a ConfigError naming the key."""
    raw = config.get(WORKTREE_ROOT_KEY)
    if raw is None:
        config.pop(WORKTREE_ROOT_KEY, None)
        return
    problem = _root_problem(raw)
    if problem:
        raise ConfigError("%s: 'worktree_root' %s, got %r" % (source, problem, raw))


def check_worktree_path(config, path):
    """None when a NEW worktree may be made at `path`, else the refusal text (it names the root). Refused: a new top-level entry of ~/Code (its parent is
    ~/Code itself), and anything that is not a path. Accepted: a path under the root, one in the temp directory, any other path not directly in ~/Code."""
    root = worktree_root(config)
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return "the worktree path must be absolute; make worktrees under %s as <root>/<repo>/<name>" % root
    code = _real(CODE_DIR)
    for parent in (os.path.dirname(os.path.realpath(path)), os.path.realpath(os.path.dirname(os.path.abspath(path)))):   # the resolved path, and the path as named (a symlink at the last component is still an entry of ~/Code)
        if parent == code:
            return "%s would be a new top-level entry of %s: make the worktree under the worktree root %s as <repo>/<name> (config key worktree_root)" % (path.replace("\n", " ")[:160], CODE_DIR, root)
    return None
