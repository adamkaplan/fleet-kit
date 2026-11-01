# ---------------------------------------------------------------- `scratch` (#1456)
#
# The caller's own scratch directory under the state directory, for the helper scripts and the rule texts that must outlive a turn (the system temp area is
# swept by the system and by the temp janitor; the state directory is the fleet's own). The directory is made by `agent_scratch_dir`, which calls the one
# private-directory helper `job_ensure_root` (0700, ours, lstat, so a symlink is refused): this part makes no directory itself. The name is the caller's own,
# found by `identify_caller`; the command takes no argument, so no path or name can point elsewhere. No fleet command removes or empties the directory
# (`disk_janitor.holds_scratch` is the janitor's check; the others never reach the state directory). A size bound only warns, it never deletes or refuses.
# The daemon's look is `ScratchBoundFacts`, one more source of `fact_sources` (the one registration point of the entry).

SCRATCH_BOUND = 100 * 1024 * 1024   # bytes: 100 MB
SCRATCH_WALK_MAX = 50000            # entries counted at most (a walk of more says "over" when the bytes seen are already over the bound, else stops)
SCRATCH_BOUND_FILE = "scratch-bound.json"   # {"day": "YYYY-MM-DD"}: the day the over-bound audit line was last written
SCRATCH_BOUND_EVERY = 600           # seconds between two looks of the daemon at the size


def scratch_bytes(path):
    """The bytes held under `path` (lstat sizes, no link followed; stops early once over the bound or past SCRATCH_WALK_MAX entries). Reads only."""
    total, seen, todo = 0, 0, [str(path)]
    while todo and total <= SCRATCH_BOUND and seen < SCRATCH_WALK_MAX:
        try:
            with os.scandir(todo.pop()) as entries:
                for entry in entries:
                    seen += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += info.st_size
                    if stat.S_ISDIR(info.st_mode):
                        todo.append(entry.path)
        except OSError:
            continue
    return total


def scratch_warning(path, size):
    return "%s: warning: %s holds %d MB, over the %d MB bound (nothing is deleted; remove what you no longer need)" % (PROG, path, size >> 20, SCRATCH_BOUND >> 20)


def cmd_scratch(args, runner, now):
    """`scratch`: print the path of your own scratch directory (made private, 0700, when absent). Takes no argument: the name is the caller's own."""
    engine, _, _ = build_engine(runner, now, "cli", writable=False)
    name = identify_caller(engine.herdr, engine.v2, os.environ)
    try:
        path = agent_scratch_dir(engine, name)
    except LaunchError as err:
        sys.stderr.write("%s: scratch: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_USAGE
    print(path)
    size = scratch_bytes(path)
    if size > SCRATCH_BOUND:
        sys.stderr.write(scratch_warning(path, size) + "\n")
    return EXIT_OK


def add_scratch_parsers(sub):
    sub.add_parser("scratch", help="Print your own scratch directory under the state directory (private, 0700, never swept; not a place for a secret or a token)")


class ScratchBoundFacts:
    """The daemon's look at the scratch directories: one audit line `scratch.over_bound` a day for each directory that stays over the bound. It only counts and
    records: nothing is removed or refused. Returns no facts. The day is kept in `scratch-bound.json` (a name -> day map)."""

    __name__ = "scratch_bound_facts"

    def __init__(self):
        self.engine, self.last = None, None

    def bind(self, engine):
        self.engine = engine

    def __call__(self):
        engine = self.engine
        if engine is None or engine.state_dir is None:
            return []
        now = engine.now()
        if self.last is not None and now - self.last < SCRATCH_BOUND_EVERY:
            return []
        self.last = now
        base = Path(engine.state_dir) / "scratch"
        try:
            names = sorted(n for n in os.listdir(str(base)) if NAME_RE.fullmatch(n))
        except OSError:
            return []
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        try:
            with open(str(Path(engine.state_dir) / SCRATCH_BOUND_FILE)) as handle:
                seen = json.loads(handle.read(65536))
        except (OSError, ValueError):
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        changed = False
        for name in names:
            path = base / name
            if path.is_symlink() or seen.get(name) == day:
                continue
            size = scratch_bytes(path)
            if size > SCRATCH_BOUND:
                engine.audit.append(engine.actor, "scratch.over_bound", name, {"bytes": size, "bound": SCRATCH_BOUND, "day": day})
                seen[name] = day
                changed = True
        if changed:
            try:
                _write_small(engine.state_dir, SCRATCH_BOUND_FILE, seen)
            except OSError:
                pass
        return []

