"""The Spotlight guard (ask #438): is each guarded folder still kept out of the Spotlight index?

The exclusion list itself is root-readable only, so it cannot be read. What can be read cheaply, with no privilege, is whether the index holds anything under
a folder: `mdfind -onlyin <folder> -count -name a` answers an integer in about 50 ms. A folder that is excluded has its entries purged, so a nonzero count
means it is being indexed again. A purge after a fresh exclusion can lag, so ONE nonzero answer is not yet an alarm: it takes two in a row.

  - Read-only. It changes no setting and writes only its own state file `spotlight-guard.json` (and only when the answer changed).
  - Once an hour, on a background thread, with a timeout per folder; a normal pass starts no process.
  - Pure of the switchboard: the caller hands it the state directory, the runner and the clock.

Per folder the answer is one of `excluded` (count 0), `indexed` (count above 0), `not checked` (the folder is missing or empty: there is nothing to tell) and
`unknown` (the tool is missing, timed out or printed something that is not a count: never excluded, never a warning)."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FILE = "spotlight-guard.json"
CHECK_SECONDS = 3600     # at most one check an hour
TOOL_SECONDS = 5         # the timeout of one `mdfind`
CONFIRM = 2              # nonzero counts in a row before the alarm
MAX_FOLDERS = 20
STATE_MAX = 65536        # bytes of the state file read
EXCLUDED, INDEXED, NOT_CHECKED, UNKNOWN = "excluded", "indexed", "not checked", "unknown"


def default_folders():
    """The code folder and the opencode temp folder (the one the temp janitor keeps clean)."""
    folders = [str(Path.home() / "Code")]
    try:
        import disk_janitor
        folders.append(str(disk_janitor.default_dir()))
    except Exception:
        folders.append(str(Path(tempfile.gettempdir()) / "opencode"))
    return folders


def validate(raw, source):
    """`spotlight_guard`: absent or null = the defaults; false = off; else {"folders": [absolute or ~ paths without a parent part], "enabled": bool}.
    Raises ValueError with a message naming the key."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict) or set(raw) - {"folders", "enabled"}:
        raise ValueError("%s: 'spotlight_guard' must be false or an object with only folders and enabled, got %r" % (source, raw))
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError("%s: spotlight_guard.enabled must be true or false" % source)
    folders = raw.get("folders")
    if folders is None:
        return
    if not isinstance(folders, list) or len(folders) > MAX_FOLDERS or not all(isinstance(f, str) and f.strip() and "\x00" not in f for f in folders):
        raise ValueError("%s: spotlight_guard.folders must be a list of at most %d non-empty path strings" % (source, MAX_FOLDERS))
    for folder in folders:
        if not (os.path.isabs(folder) or folder == "~" or folder.startswith("~/")):
            raise ValueError("%s: spotlight_guard.folders must be absolute paths (or start with ~/), got %r" % (source, folder))
        if ".." in Path(folder).parts:
            raise ValueError("%s: spotlight_guard.folders must not hold a parent part (..), got %r" % (source, folder))


def settings(raw):
    """None when the guard is off; else {"folders": [expanded absolute paths]}. The folders come from the configuration only, never from input.
    Spotlight is a macOS service: with no configuration at all the guard is off elsewhere (a configured one is honoured)."""
    if raw is False or (isinstance(raw, dict) and raw.get("enabled") is False):
        return None
    if raw is None and sys.platform != "darwin":
        return None
    raw = raw if isinstance(raw, dict) else {}
    folders = raw.get("folders")
    if folders is None:
        folders = default_folders()
    return {"folders": [os.path.abspath(os.path.expanduser(f)) for f in folders]}


def mdfind_argv(folder):
    return ["mdfind", "-onlyin", folder, "-count", "-name", "a"]


def parse_count(stdout):
    """The count from the tool's stdout: the last non-empty line, and only when it is a whole number. Anything else is None (unknown)."""
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines or not re.fullmatch(r"[0-9]{1,12}", lines[-1]):
        return None
    return int(lines[-1])


def holds_files(folder):
    """True when the folder exists and holds at least one entry."""
    try:
        with os.scandir(folder) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def check(folder, runner=None):
    """(state, count) for one folder. Never raises. FAIL OPEN: a failed or unreadable answer is `unknown`, never excluded and never an alarm."""
    if not holds_files(folder):
        return NOT_CHECKED, None
    try:
        done = (runner or subprocess.run)(mdfind_argv(folder), capture_output=True, text=True, timeout=TOOL_SECONDS, stdin=subprocess.DEVNULL)
    except Exception:
        return UNKNOWN, None
    if getattr(done, "returncode", 1) != 0:
        return UNKNOWN, None
    count = parse_count(done.stdout)
    if count is None:
        return UNKNOWN, None
    return (EXCLUDED, 0) if count == 0 else (INDEXED, count)


def check_all(folders, runner=None):
    return {folder: check(folder, runner) for folder in folders}


class Tracker:
    """Per folder: how many nonzero counts in a row, and the episode (the time the alarm began, None when none is open).

    An episode begins at the CONFIRM-th nonzero count in a row and ends at a zero count. `unknown` and `not checked` change nothing about the alarm: an answer that
    cannot be read neither opens nor closes an episode, and a not-checked folder drops the streak (there is nothing to count)."""

    def __init__(self, saved=None):
        saved = saved if isinstance(saved, dict) else {}
        self.streak = {}
        self.episode = {}
        for folder, entry in saved.items():
            if isinstance(entry, dict):
                streak, episode = entry.get("streak"), entry.get("episode")
                self.streak[folder] = streak if isinstance(streak, int) and not isinstance(streak, bool) and 0 <= streak < 1000 else 0
                self.episode[folder] = float(episode) if isinstance(episode, (int, float)) and not isinstance(episode, bool) else None

    def step(self, folder, state, now):
        """The event of this check: "begin", "end" or None."""
        if state == INDEXED:
            self.streak[folder] = self.streak.get(folder, 0) + 1
            if self.streak[folder] >= CONFIRM and self.episode.get(folder) is None:
                self.episode[folder] = float(now)
                return "begin"
        elif state == EXCLUDED:
            self.streak[folder] = 0
            if self.episode.get(folder) is not None:
                self.episode[folder] = None
                return "end"
        elif state == NOT_CHECKED:
            self.streak[folder] = 0
        return None


def body(results, tracker, now):
    """The file's content: per folder the state, the count, the streak and the episode."""
    folders = {}
    for folder, (state, count) in results.items():
        folders[folder] = {"state": state, "count": count, "streak": tracker.streak.get(folder, 0), "episode": tracker.episode.get(folder)}
    return {"folders": folders, "checked_at": float(now)}


def stable(content):
    """The content without the time: the file is rewritten only when this changes."""
    return {k: v for k, v in content.items() if k != "checked_at"}


def save(state_dir, content):
    """Atomic, 0600. Raises OSError."""
    path = os.path.join(str(state_dir), FILE)
    fd, temp = tempfile.mkstemp(prefix=FILE + ".", suffix=".tmp", dir=str(state_dir))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(content, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def load(state_dir):
    """The file's content as written, or None (missing, unreadable, or of the wrong shape)."""
    try:
        with open(os.path.join(str(state_dir), FILE)) as handle:
            raw = json.loads(handle.read(STATE_MAX))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("folders"), dict):
        return None
    return raw


def _plain(text):
    """A folder name for a line or a wake: control characters (an escape sequence) are dropped."""
    return "".join(ch for ch in str(text) if ch.isprintable())


def status_lines(cfg, saved, now):
    """One line per guarded folder, from the file the background check wrote (no process): excluded, a WARN naming the folder and the count, not checked or unknown."""
    lines = []
    folders = (saved or {}).get("folders") or {}
    for folder in cfg["folders"]:
        entry = folders.get(folder)
        shown = _plain(folder)
        if not isinstance(entry, dict):
            lines.append("%s: not checked yet" % shown)
            continue
        state, count = entry.get("state"), entry.get("count")
        if state == EXCLUDED:
            lines.append("%s: excluded from Spotlight" % shown)
        elif state == INDEXED and entry.get("episode") is not None:
            lines.append("WARN %s: Spotlight is INDEXING it (%s entries); add it back to the Spotlight privacy list" % (shown, count))
        elif state == INDEXED:
            lines.append("%s: Spotlight shows %s entries (confirming on the next check)" % (shown, count))
        elif state == NOT_CHECKED:
            lines.append("%s: not checked (missing or empty)" % shown)
        else:
            lines.append("%s: unknown (the check could not be read)" % shown)
    return lines


def wake_text(folder, count, prog):
    return ("spotlight: %s is being indexed again (%s entries in the index on two checks in a row). The exclusion was lost: add the folder back under System Settings, "
            "Spotlight, Search Privacy. `%s status` shows the line." % (_plain(folder), count, prog))
