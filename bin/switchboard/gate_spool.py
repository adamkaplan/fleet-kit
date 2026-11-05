# ---------------------------------------------------------------- the gate spool reader (ask #2216)
#
# The shim writes `gate-failopen.jsonl` in the state directory (the shadow gate probe, #107): a line for every gate that failed open (`fail_open`), was
# refused (`refused`) or whose merge went over a blocking review (`over_review`). Nothing read it, so it filled to its limit (262,144 bytes) and from then
# on every `--over-review` was refused. This part reads it each pass: the file is untrusted text (any local process may write it), so every line is parsed
# as data and every field clipped. The read position (inode and offset) is kept in `gate-spool-state.json`, a line is never read twice, and the file is
# renamed to `.1` once it is read to the end and holds a quarter of its limit, so the shim never finds it full. A rotation by someone else (the shim) is
# followed: the unread rest of `.1` is read first when its inode is the saved one. The sweep (kind S11) reads the counts; the decisions carry the over-review lines.
GATE_SPOOL_NAME = "gate-failopen.jsonl"
GATE_SPOOL_STATE = "gate-spool-state.json"
GATE_SPOOL_CAP = 262144            # the shim's own limit of the file
GATE_SPOOL_ROTATE_AT = GATE_SPOOL_CAP * 3 // 4
GATE_SPOOL_WINDOW = 3600           # seconds: "an hour" of fail-opens
GATE_SPOOL_EVENTS_KEPT = 2000
GATE_SPOOL_OVER_PRS = 200
GATE_SPOOL_OVER_PER_PR = 5
GATE_FAILOPEN_PER_HOUR = 20        # default of `sweep.failopen_per_hour`: a repository with MORE fail-opens than this in an hour is one finding


def _spool_chunk(path, offset):
    """(complete lines as text, bytes consumed): from `offset`, only whole lines (a line the shim is still writing is left for the next pass)."""
    with open(str(path), "rb") as handle:
        handle.seek(offset)
        data = handle.read(GATE_SPOOL_CAP + 4096)
    cut = data.rfind(b"\n") + 1
    return data[:cut].decode("utf-8", "replace").splitlines(), cut


def _spool_line(text, now):
    """One spool line as clipped data: (event, repo, number, reason, ts), or None for a line that is not a record."""
    try:
        record = json.loads(text[:4000])
    except ValueError:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("event"), str):
        return None
    ts = record.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 < ts < now + 86400 else now
    pr = record.get("pr") if isinstance(record.get("pr"), str) else "unknown#0"
    repo, _, number = pr[:120].rpartition("#")
    return (_plain(record["event"], 40), _plain(repo or "unknown", 100), int(number) if number.isdigit() and len(number) < 9 else 0,
            _plain(record.get("detail") if isinstance(record.get("detail"), str) else "", 200), ts)


class GateSpoolFacts:
    """The spool step of the pass list: reads the new lines, keeps the position, rotates. Returns no facts. `engine.gate_spool` is this object: the sweep
    asks it `busy` (repositories over the rate) and `over_reviews` (by pull request)."""

    __name__ = "gate_spool_facts"

    def __init__(self):
        self.engine, self.saved, self.events, self.over, self.said, self.loaded = None, {"ino": None, "off": 0}, [], {}, set(), False

    def bind(self, engine):
        self.engine = engine
        engine.gate_spool = self

    def _say(self, key, text):
        if key not in self.said:   # an unreadable spool is said once, never every pass
            self.said.add(key)
            self.engine._note("gate_spool.unreadable", None, {"error": _plain(text, 160)})

    def _load(self, state_dir):
        self.loaded = True
        try:
            raw = json.loads((Path(state_dir) / GATE_SPOOL_STATE).read_text()[:400000])
            self.saved = {"ino": raw["ino"] if isinstance(raw["ino"], int) else None, "off": raw["off"] if isinstance(raw["off"], int) and raw["off"] >= 0 else 0}
            self.events = [tuple(e) for e in raw.get("events", []) if isinstance(e, list) and len(e) == 3][-GATE_SPOOL_EVENTS_KEPT:]
            self.over = {k: [str(x)[:200] for x in v][-GATE_SPOOL_OVER_PER_PR:] for k, v in raw.get("over", {}).items() if isinstance(v, list)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _save(self, state_dir):
        path = Path(state_dir) / GATE_SPOOL_STATE
        temp = path.with_name("%s.%d.tmp" % (GATE_SPOOL_STATE, os.getpid()))
        body = json.dumps({"ino": self.saved["ino"], "off": self.saved["off"], "events": [list(e) for e in self.events], "over": self.over}, sort_keys=True)
        with open(str(temp), "w") as handle:
            handle.write(body)
        os.replace(str(temp), str(path))

    def _take(self, lines, now):
        for text in lines:
            row = _spool_line(text, now)
            if row is None:
                continue
            event, repo, number, reason, ts = row
            if event == "fail_open":
                self.events.append((ts, repo, reason))
            elif event == "over_review" and number:
                seen = self.over.setdefault("%s#%d" % (repo, number), [])
                seen.append(reason)
                del seen[:-GATE_SPOOL_OVER_PER_PR]
        self.events = [e for e in self.events if now - e[0] <= 2 * GATE_SPOOL_WINDOW][-GATE_SPOOL_EVENTS_KEPT:]
        while len(self.over) > GATE_SPOOL_OVER_PRS:
            del self.over[next(iter(self.over))]

    def read_once(self, state_dir, now):
        """One pass over the spool; raises OSError when it cannot be read or rotated."""
        path = Path(state_dir) / GATE_SPOOL_NAME
        old = path.with_name(GATE_SPOOL_NAME + ".1")
        try:
            stat = os.stat(str(path))
        except FileNotFoundError:
            stat = None
        if self.saved["ino"] is not None and (stat is None or stat.st_ino != self.saved["ino"]):   # rotated away by the shim: the unread rest of `.1` first
            try:
                if os.stat(str(old)).st_ino == self.saved["ino"]:
                    lines, used = _spool_chunk(old, self.saved["off"])
                    self._take(lines, now)
            except FileNotFoundError:
                pass
            self.saved = {"ino": None, "off": 0}
        if stat is None:
            return
        offset = self.saved["off"] if stat.st_ino == self.saved["ino"] and stat.st_size >= self.saved["off"] else 0   # a size reset is a new file
        lines, used = _spool_chunk(path, offset)
        self._take(lines, now)
        self.saved = {"ino": stat.st_ino, "off": offset + used}
        if self.saved["off"] >= GATE_SPOOL_ROTATE_AT and self.saved["off"] >= os.stat(str(path)).st_size:
            os.replace(str(path), str(old))
            rest, used = _spool_chunk(old, self.saved["off"])   # what the shim wrote between the read and the rename
            self._take(rest, now)
            self.saved = {"ino": None, "off": 0}

    def __call__(self):
        engine = self.engine
        if engine is None or engine.state_dir is None:
            return []
        now = engine.now()
        if not self.loaded:
            self._load(engine.state_dir)
        try:
            self.read_once(engine.state_dir, now)
            self._save(engine.state_dir)
            self.said.clear()
        except (OSError, ValueError) as err:
            self._say("spool", "%s: %s" % (type(err).__name__, err))
        except Exception as err:   # a reader never stops the pass
            self._say("spool", "%s: %s" % (type(err).__name__, err))
        return []

    def busy(self, now, rate):
        """{repo: (count, commonest reason)} for each repository with MORE than `rate` fail-opens in the last hour."""
        counts = {}
        for ts, repo, reason in self.events:
            if 0 <= now - ts <= GATE_SPOOL_WINDOW:
                counts.setdefault(repo, {}).setdefault(reason, 0)
                counts[repo][reason] += 1
        return {repo: (sum(r.values()), sorted(r.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]) for repo, r in counts.items() if sum(r.values()) > rate}

    def over_reviews(self):
        """{"repo#n": [clipped lines]} of the merges that went over a blocking review."""
        return {k: list(v) for k, v in self.over.items()}
