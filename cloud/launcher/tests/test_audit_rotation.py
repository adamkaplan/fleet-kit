"""Ask #1743 (pull request 2): the audit rotation, the rotation record and the two readers across rotations (scenarios S6 S7 S10 S11 S12 S13 S14 S17).
Temporary directories only. Mutants are copies of a module's text made in memory: each text is asserted to occur once and the mutation to apply."""
import contextlib
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
sys.path.insert(0, os.path.join(ROOT, "cloud", "launcher"))
import record_json  # noqa: E402

PARSE = record_json.parse_bounded
SOURCES = {"cw": os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py"), "ea": os.path.join(ROOT, "bin", "cloud_end_alert.py"),
           "lw": os.path.join(ROOT, "bin", "lost_worker.py")}
WORDS = "an audit archive needed to read older stop lines was missing or unreadable"


def load(kind, old=None, new=None):
    """The module of `kind`, or a copy with `old` replaced by `new` (asserted to occur once, the mutation asserted applied)."""
    with open(SOURCES[kind]) as handle:
        text = handle.read()
    if old is not None:
        assert text.count(old) == 1, "the text to mutate does not occur exactly once: %r" % old
        mutated = text.replace(old, new)
        assert mutated != text, "the mutation was not applied"
        text = mutated
    module = types.ModuleType("rotation_%s_under_test" % kind)
    module.__dict__["__file__"] = SOURCES[kind]
    exec(compile(text, SOURCES[kind], "exec"), module.__dict__)
    return module


CW, EA, LW = load("cw"), load("ea"), load("lw")


def stop(n, pad=0):
    return json.dumps({"ts": "2026-01-01T10:00:00Z", "action": "stop", "worker": "w%d" % n, "ok": True, "cause": "idle", "n": n,
                       "reason": "idle; run-id=r%d%s" % (n, "x" * pad), "runtime_id": "pod/w%d" % n}) + "\n"


def lines(first, last, pad=0):
    return "".join(stop(n, pad) for n in range(first, last))


def snapshot(directory):
    out = {}
    for base, _dirs, names in os.walk(directory):
        for name in names:
            with open(os.path.join(base, name), "rb") as handle:
                out[os.path.join(base, name)] = hashlib.sha256(handle.read()).hexdigest()
    return out


class EndReader:
    def __init__(self, directory, audit, module=EA):
        self.audit, self.state, self.module = audit, os.path.join(directory, "cloud-end-alert.json"), module
        self.cloud = os.path.join(directory, "cloud")
        os.makedirs(self.cloud, exist_ok=True)
        self.sent = []

    def once(self):
        before = len(self.sent)
        self.module.scan_audit(self.audit, self.state, self.cloud, {}, lambda to, text: self.sent.append(text), PARSE)
        return self.sent[before:]

    def read_all(self):
        got = []
        for _ in range(60):
            new = self.once()
            if not new:
                break
            got += new
        return [int(m.group(1)) for t in got for m in [re.search(r"\] w(\d+) was ended", t)] if m]

    def said(self):
        return sum(WORDS in t for t in self.sent)

    def offset(self):
        with open(self.state) as handle:
            return json.load(handle)["offset"]


class LostReader:
    def __init__(self, audit, module=LW):
        self.audit, self.state, self.module = audit, {}, module
        self.sb = types.SimpleNamespace(parse_bounded=PARSE)

    def once(self):
        return [item["n"] for item, _text in self.module._read_new(self.sb, self.audit, self.state, "launcher")]

    def read_all(self):
        got = []
        for _ in range(60):
            new = self.once()
            if not new:
                break
            got += new
        return got

    def offset(self):
        return self.state["offsets"]["launcher"]


def both(directory, audit, ea=EA, lw=LW):
    return EndReader(directory, audit, ea), LostReader(audit, lw)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="audit-rotation-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.audit = os.path.join(self.dir, "audit.jsonl")

    def put(self, text, mode="w"):
        with open(self.audit, mode) as handle:
            handle.write(text)

    def big(self, count=6000, pad=700):
        self.put(lines(0, count, pad))
        return count

    def rotate(self, cw=CW, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = cw.rotate_audit(self.audit, **kw)
        return result, err.getvalue()

    def live_ids(self):
        with open(self.audit) as handle:
            out = []
            for text in handle:
                try:
                    out.append(json.loads(text)["n"])
                except ValueError:
                    pass
            return out


# ---- S6 and S11: the readers across rotations ----
class TestReadersAcrossRotations(Base):
    def s6(self, ea=EA, lw=LW):
        count = self.big()
        end, lost = both(self.dir, self.audit, ea, lw)
        end.once(); lost.read_all()   # first sight: each takes the end
        self.assertEqual(lost.once(), [])
        self.assertEqual(end.once(), [])
        result, _ = self.rotate()
        self.assertTrue(result)
        self.put(lines(count, count + 5), "a")
        got_end, got_lost = end.read_all(), lost.read_all()
        want = list(range(count, count + 5))
        if got_end != want or got_lost != want:
            raise AssertionError("a reader did not read the 5 new lines once: end alert %s, lost worker %s" % (got_end, got_lost))
        return len(got_end) + len(got_lost), len([n for n in got_end if n < count])

    def test_s6(self):
        reads, old = self.s6()
        print("S6: 5 of 5 new lines read once by each of the 2 readers (%d of 10 reads), %d alerts for lines older than the rotation" % (reads, old))
        self.assertEqual((reads, old), (10, 0))

    def s11(self, ea=EA, lw=LW):
        count = self.big(7000, 700)
        end, lost = both(self.dir, self.audit, ea, lw)
        end.once(), lost.read_all()
        self.assertTrue(self.rotate(now=CW.dt.datetime(2026, 1, 1, tzinfo=CW.dt.timezone.utc))[0])
        self.put(lines(count, count + 3), "a")
        with mock.patch.object(CW, "AUDIT_ROTATE_BYTES", 512 << 10):
            second, _ = self.rotate(now=CW.dt.datetime(2026, 1, 2, tzinfo=CW.dt.timezone.utc))
        self.assertTrue(second)
        self.put(lines(count + 3, count + 7), "a")
        want = list(range(count, count + 7))
        got = (end.read_all(), lost.read_all())
        if got != (want, want):
            raise AssertionError("a cut was applied twice (or a line was skipped or replayed): end alert %s, lost worker %s" % got)
        return sum(len(g) for g in got)

    def test_s11(self):
        self.assertEqual(self.s11(), 14)
        pending_moved = 0
        for kind in ("ea", "lw"):
            directory = tempfile.mkdtemp(prefix="audit-pending-")
            self.addCleanup(shutil.rmtree, directory, True)
            audit = os.path.join(directory, "audit.jsonl")
            with open(audit, "w") as handle:
                handle.write(lines(0, 40, 50))
            end, lost = both(directory, audit)
            end.once(), lost.once()
            end.once(), lost.once()
            size = os.path.getsize(audit)
            with open(audit + ".rotations", "w") as handle:
                json.dump({"done": 0, "cuts": [], "pending": {"cut": 1000, "old_size": size}}, handle)
            before = (end.offset(), lost.offset())
            reader = end if kind == "ea" else lost
            self.assertEqual(reader.read_all(), [])
            pending_moved += (end.offset(), lost.offset()) != before
        self.assertEqual(pending_moved, 0)
        self.setUp()
        mutant_ea, mutant_lw = load("ea", "live -= cut\n", "live -= cut; live -= cut\n"), load("lw", "live -= cut\n", "live -= cut; live -= cut\n")
        self.setUp()
        with self.assertRaises(AssertionError) as caught:
            self.s11(mutant_ea, mutant_lw)
        self.assertIn("a cut was applied twice", str(caught.exception))
        print("S11: 7 of 7 new lines read once by each of the 2 readers (14 of 14 reads), 0 replays, 0 alerts for lines already read, %d offset change in the "
              "pending case for 2 of 2 readers, the control passes 1 of 1, the copy that applies a cut twice fails 1 of 1 with the anchored text, the mutation applied" % pending_moved)

    def test_s6_discriminates(self):
        mutant_ea, mutant_lw = load("ea", "live -= cut\n", "live -= cut; live -= cut\n"), load("lw", "live -= cut\n", "live -= cut; live -= cut\n")
        with self.assertRaises(AssertionError):
            self.s6(mutant_ea, mutant_lw)


# ---- S7, S13, S14: the rotation itself ----
class TestRotation(Base):
    def archives(self):
        return sorted(n for n in os.listdir(self.dir) if n.startswith("audit-") and n.endswith(".jsonl.gz"))

    def check_whole(self, cw=CW):
        original = lines(0, 3000, 1500)
        self.put(original)
        result, _ = self.rotate(cw)
        names = self.archives()
        if not result or len(names) != 1:
            raise AssertionError("a line was lost: no archive was made")
        with gzip.open(os.path.join(self.dir, names[0]), "rb") as handle:
            old = handle.read().decode()
        with open(self.audit) as handle:
            live = handle.read()
        got = (old + live).count("\n")
        if old + live != original:
            raise AssertionError("a line was lost: %d of 3000 lines present" % got)
        return got

    def test_s7(self):
        self.assertEqual(self.check_whole(), 3000)
        self.setUp()
        original = lines(0, 3000, 1500)
        self.put(original)

        def fail():
            raise OSError("injected")
        result, err = self.rotate(_before_swap=fail)
        with open(self.audit) as handle:
            kept = handle.read()
        record = CW.read_rotation_record(self.audit)
        self.assertIsNone(result)
        self.assertEqual(kept, original)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual((self.archives(), record["pending"], record["done"]), ([], None, 0))
        self.setUp()
        mutant = load("cw", "packed.write(old)", "packed.write(old[:len(old) // 2])")
        with self.assertRaises(AssertionError) as caught:
            self.check_whole(mutant)
        self.assertIn("a line was lost", str(caught.exception))
        print("S7: 3000 of 3000 lines present once in order, the failed swap leaves %d of 3000 lines in the live file and %d line of explanation, the control passes 1 of 1, "
              "the truncating copy fails 1 of 1 with the anchored text, the mutation applied" % (kept.count("\n"), len(err.strip().splitlines())))

    def test_s13(self):
        self.big()
        before = snapshot(self.dir)
        result, err = self.rotate(dry_run=True)
        self.assertEqual((snapshot(self.dir) == before, err), (True, ""))
        self.assertTrue(result["lines"] > 0 and result["bytes"] > 0)
        real, err = self.rotate()
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertLess(os.path.getsize(self.audit), 1 << 20 | 1)
        self.assertEqual(real["lines"], result["lines"])
        print("S13: dry run lists 1 rotation (%d lines, %d bytes) and leaves %d differences in the tree; the rotation makes 1 archive, replaces the live file by its newest lines "
              "and prints 1 line" % (result["lines"], result["bytes"], 0))

    def check_newest(self, cw=CW):
        count = self.big()
        self.rotate(cw)
        present = set(self.live_ids())
        newest = [n for n in range(count - 1000, count) if n in present]
        if len(newest) != 1000:
            raise AssertionError("the newest lines were not kept: %d of the newest 1000 in the live file" % len(newest))
        return len(newest)

    def test_s14(self):
        self.assertEqual(self.check_newest(), 1000)
        self.setUp()
        mutant = load("cw", "out.write(data[cut:] + late)", "out.write(data[:len(data) - cut] + late)")
        with self.assertRaises(AssertionError) as caught:
            self.check_newest(mutant)
        self.assertIn("the newest lines were not kept", str(caught.exception))
        print("S14: the newest 1000 lines of the fixture are present in the live file 1000 of 1000, the control passes 1 of 1, the copy that keeps the oldest fails 1 of 1 with "
              "the anchored text (0 of 1000), the mutation applied")

    def test_the_record_is_written_in_order(self):
        self.big()
        self.rotate()
        record = CW.read_rotation_record(self.audit)
        path = self.audit + ".rotations"
        self.assertEqual((record["done"], len(record["cuts"]), record["pending"]), (1, 1, None))
        self.assertEqual(record["cuts"][0]["archive"], self.archives()[0])
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertLessEqual(os.path.getsize(path), 4096)
        with gzip.open(os.path.join(self.dir, record["archive"] if "archive" in record else record["cuts"][0]["archive"])) as handle:
            self.assertEqual(len(handle.read()), record["cuts"][0]["cut"])


# ---- S10: a damaged record leaves the readers as they are ----
class TestDamagedRecord(Base):
    INPUTS = {"missing": None, "empty": "", "not JSON": "{nope", "wrong shape": json.dumps({"done": "x", "cuts": 5, "pending": 7}),
              "larger than 4096": json.dumps({"done": 0, "cuts": [], "pending": None, "pad": "x" * 5000}),
              "valid (the control)": json.dumps({"done": 0, "cuts": [], "pending": None})}

    def run_one(self, text, ea=EA, lw=LW):
        directory = tempfile.mkdtemp(prefix="audit-damaged-")
        self.addCleanup(shutil.rmtree, directory, True)
        audit = os.path.join(directory, "audit.jsonl")
        with open(audit, "w") as handle:
            handle.write(lines(0, 20, 50))
        end, lost = both(directory, audit, ea, lw)
        end.once(), lost.once()
        end.once(), lost.once()
        if text is not None:
            with open(audit + ".rotations", "w") as handle:
                handle.write(text)
        size = os.path.getsize(audit)
        with open(audit, "w") as handle:   # replaced by a file shorter than the offset
            handle.write(lines(100, 119, 0))
            gap = size - 1 - handle.tell()
            handle.write(lines(119, 120, gap - len(lines(119, 120, 0))))   # one line more, so that the new file is exactly one byte shorter than the offset
        assert os.path.getsize(audit) == size - 1
        before = set(os.listdir(directory))
        got_end, got_lost = end.read_all(), lost.read_all()
        if got_end != [] or got_lost != list(range(100, 120)) or lost.offset() != os.path.getsize(audit) or end.offset() != os.path.getsize(audit):
            raise AssertionError("the damaged record was trusted: end alert %s, lost worker %s" % (got_end, got_lost))
        extra = set(os.listdir(directory)) - before - {"cloud-end-alert.json", "cloud-end-alert.json.rotations", "cloud"}
        return len(extra)

    def test_s10(self):
        outside = sum(self.run_one(text) for text in self.INPUTS.values())
        trusting = load("lw", "    except (ValueError, UnicodeDecodeError):\n        return None\n    if not isinstance(body, dict):\n        return None\n",
                    "    except (ValueError, UnicodeDecodeError):\n        return 1, [(1, 'audit-x.jsonl.gz')]\n    if not isinstance(body, dict):\n        return None\n")
        trusted = 0
        try:
            self.run_one("{nope", lw=trusting)
        except AssertionError as e:
            trusted = 1
            self.assertIn("the damaged record was trusted", str(e))
        self.assertEqual((outside, trusted), (0, 1))
        print("S10: 5 inputs x 2 readers = 10 of 10 behave as before the change, 0 exceptions, %d files written outside the state files, the control with a valid record "
              "passes 1 of 1, and a copy of a reader that trusts a damaged record fails 1 of 1 with the anchored text, the mutation applied" % outside)


# ---- S17: the archives are read ----
class TestArchiveRead(Base):
    def build(self, damage=None):
        names = ["audit-2026-01-0%d.jsonl.gz" % i for i in (1, 2, 3)]
        parts = [lines(0, 100, 20), lines(100, 200, 20), lines(200, 300, 20)]
        cuts = []
        for index, (name, text) in enumerate(zip(names, parts)):
            cuts.append({"cut": len(text.encode()), "archive": name})
            payload = text.encode()
            path = os.path.join(self.dir, name)
            if damage == "removed" and index == 1:
                continue
            if damage == "not gzip" and index == 1:
                payload, packed = b"this is not gzip", payload
                with open(path, "wb") as handle:
                    handle.write(payload)
                continue
            if damage == "shorter" and index == 1:
                payload = payload[:len(payload) // 2]
            with gzip.open(path, "wb") as handle:
                handle.write(payload)
        with open(self.audit + ".rotations", "w") as handle:
            json.dump({"done": 3, "cuts": cuts, "pending": None}, handle)
        self.put(lines(300, 330, 20))
        return cuts[0]["cut"] - len(lines(60, 100, 20).encode())

    def readers(self, offset, ea=EA, lw=LW):
        end, lost = both(self.dir, self.audit, ea, lw)
        with open(end.state, "w") as handle:
            json.dump({"offset": offset, "sent": []}, handle)
        with open(end.state + ".rotations", "w") as handle:
            json.dump({"applied": 0, "chain": None}, handle)
        lost.state = {"offsets": {"launcher": offset}, "rotations": {"launcher": {"applied": 0, "chain": None}}}
        return end, lost

    def run_both(self, damage=None, ea=EA, lw=LW):
        offset = self.build(damage)
        end, lost = self.readers(offset, ea, lw)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = (end.read_all(), lost.read_all())
        return got, end.said(), err.getvalue().count(WORDS)

    def check(self, ea=EA, lw=LW):
        want = list(range(60, 330))
        got, _said, _logged = self.run_both(None, ea, lw)
        for reader, ids in zip(("end alert", "lost worker"), got):
            if ids != want:
                raise AssertionError("the archive lines were skipped: %s delivered %d of 270 lines, %d missing" % (reader, len(ids), 270 - len(ids)))
        return len(got[0]), len(got[1])

    def test_s17(self):
        self.assertEqual(self.check(), (270, 270))
        said = 0
        for damage in ("removed", "not gzip", "shorter"):
            self.setUp()
            got, said_end, said_lost = self.run_both(damage)
            self.assertEqual(got, (list(range(300, 330)),) * 2)
            self.assertEqual((said_end, said_lost), (1, 1))
            said += said_end + said_lost
        self.setUp()
        skipper_ea, skipper_lw = load("ea", "        if not segs:\n", "        if True:\n"), load("lw", "        if not segs:\n", "        if True:\n")
        with self.assertRaises(AssertionError) as caught:
            self.check(skipper_ea, skipper_lw)
        self.assertIn("the archive lines were skipped", str(caught.exception))
        print("S17: 2 readers x 270 of 270 lines delivered once and in order (40 + 100 + 100 + 30), 0 delivered twice, %d of 6 failure inputs (3 inputs x 2 readers) say the words "
              "once and deliver the 30 live lines, the control passes 1 of 1, the copy that skips the archives fails 1 of 1 with the anchored text (%s), the mutation applied"
              % (said, str(caught.exception).split(":")[1].strip()[:60]))


# ---- S18 to S21: the lock, two rotators, the pending record and the lows (constraint PC-8) ----
class TestLockAndRecord(Base):
    def all_text(self):
        """The archives (in the order of the record's cuts) and the live file, as one text, and the number of archives."""
        record = CW.read_rotation_record(self.audit)
        names = [c["archive"] for c in record["cuts"]]
        names += sorted(n for n in os.listdir(self.dir) if n.startswith("audit-") and n.endswith(".jsonl.gz") and n not in names)
        text = ""
        for name in names:
            with gzip.open(os.path.join(self.dir, name), "rb") as handle:
                text += handle.read().decode()
        with open(self.audit) as handle:
            return text + handle.read(), len(names)

    def ids(self, text):
        return [json.loads(x)["n"] for x in text.splitlines()]

    def check_s18(self, cw=CW):
        self.put(lines(0, 3000, 1500))
        seen = []
        threads = []

        def hook():
            def appender():
                cw.write_audit(self.audit, {"action": "stop", "n": 3000})
            thread = threading.Thread(target=appender, daemon=True)
            thread.start()
            threads.append(thread)
            time.sleep(0.3)
            with open(self.audit) as handle:
                seen.append(sum('"n": 3000' in x for x in handle))
        self.rotate(cw, _before_swap=hook)
        threads[0].join()
        text, _ = self.all_text()
        got = self.ids(text)
        if got != list(range(3001)):
            raise AssertionError("an appended line was lost: %d of 3001 lines present" % len(got))
        return seen[0], len(got)

    def test_s18(self):
        during, total = self.check_s18()
        self.assertEqual((during, total), (0, 3001))
        self.setUp()
        mutant = load("cw", "lock = _audit_lock(path, fcntl.LOCK_SH)", "lock = None")
        with self.assertRaises(AssertionError) as caught:
            self.check_s18(mutant)
        self.assertIn("an appended line was lost", str(caught.exception))
        print("S18: the control: %d lines of the appender present while the hook waits, %d of 3001 lines present once and in order; the copy without the lock fails 1 of 1 "
              "with the anchored text (%s), the mutation applied" % (during, total, str(caught.exception).split(": ")[1]))

    def check_s19(self, cw=CW):
        original = lines(0, 3000, 1500)
        self.put(original)
        second = []

        def inner():
            second.append(cw.rotate_audit(self.audit))
        err = io.StringIO()
        real = cw._record_update
        fired = []

        def wrapped(path, change):   # the second rotation runs to its end before the first writes its pending: the first then finds the file is not the one it read
            if not fired:
                fired.append(1)
                inner()
            return real(path, change)
        with contextlib.redirect_stderr(err), mock.patch.object(cw, "_record_update", wrapped):
            cw.rotate_audit(self.audit)
        text, count = self.all_text()
        record = CW.read_rotation_record(self.audit)
        got = self.ids(text)
        if got != list(range(3000)):
            raise AssertionError("a line was duplicated: %d lines for 3000" % len(got))
        temps = [n for n in os.listdir(self.dir) if n.endswith(".tmp") or n.startswith(".audit.")]
        return count, len(record["cuts"]), len(got), len(temps)

    def test_s19(self):
        self.assertEqual(self.check_s19(), (1, 1, 3000, 0))
        self.setUp()
        mutant = load("cw", "if (now_seen.st_dev, now_seen.st_ino) != (seen.st_dev, seen.st_ino) or now_seen.st_size < len(data):", "if False:")
        with self.assertRaises(AssertionError) as caught:
            self.check_s19(mutant)
        self.assertIn("a line was duplicated", str(caught.exception))
        print("S19: 1 archive, 1 cut, 3000 of 3000 lines once and in order, 0 temporary files, the control passes 1 of 1, the copy without the check fails 1 of 1 with the "
              "anchored text (%s), the mutation applied" % str(caught.exception).split(": ")[1])

    def check_s20(self, cw=CW):
        self.put(lines(0, 3000, 1500))
        original = cw.write_rotation_record

        def failing(path, rec):
            if rec.get("pending") is None and rec.get("done") == 1:
                raise OSError(5, "injected")
            return original(path, rec)
        with mock.patch.object(cw, "write_rotation_record", failing):
            self.rotate(cw)
        first = cw.read_rotation_record(self.audit)
        self.assertEqual((first["done"], first["pending"] is not None), (0, True))
        self.put(lines(3000, 5000, 1500), "a")
        self.rotate(cw)
        record = cw.read_rotation_record(self.audit)
        if record["done"] != 2 or len(record["cuts"]) != 2 or record["pending"] is not None:
            raise AssertionError("the cut was never recorded: done %d of 2, %d cuts" % (record["done"], len(record["cuts"])))
        text, _ = self.all_text()
        self.assertEqual(self.ids(text), list(range(5000)))
        return first["done"], record["done"], len(record["cuts"]), record["pending"]

    def test_s20(self):
        self.assertEqual(self.check_s20(), (0, 2, 2, None))
        self.setUp()
        mutant = load("cw", "            _complete_pending(path, now.timestamp())\n", "            pass\n")
        with self.assertRaises(AssertionError) as caught:
            self.check_s20(mutant)
        self.assertIn("the cut was never recorded", str(caught.exception))
        print("S20: after the injected failure pending is set and done is 0; after the next rotation done is 2 with 2 cuts recorded and pending null, 5000 lines once and in order; "
              "the control passes 1 of 1, the copy fails 1 of 1 with the anchored text (%s), the mutation applied" % str(caught.exception).split(": ")[1])

    def test_s21(self):
        import datetime
        import threading as _t
        # (a) the failure line holds the class and the errno and never the text of the error
        self.big()

        def fail():
            raise OSError(13, "denied /planted/secret/path")
        result, err = self.rotate(_before_swap=fail)
        self.assertIsNone(result)
        self.assertIn("PermissionError errno=13", err)
        planted = err.count("planted")
        self.assertEqual(planted, 0)
        # (b) a symbolic-link audit path is refused and says so
        self.setUp()
        target = os.path.join(self.dir, "real.jsonl")
        with open(target, "w") as handle:
            handle.write(lines(0, 6000, 700))
        os.symlink(target, self.audit)
        before = snapshot(self.dir)
        result, err = self.rotate()
        self.assertIsNone(result)
        self.assertIn("symbolic link", err)
        self.assertTrue(os.path.islink(self.audit) and os.readlink(self.audit) == target)
        self.assertEqual(snapshot(self.dir), before)
        # (c) archives are pruned by the date in their name, not by their modification time
        self.setUp()
        self.big()
        old_name, young_name = "audit-2026-09-01.jsonl.gz", "audit-2026-10-25.jsonl.gz"
        for name in (old_name, young_name):
            with open(os.path.join(self.dir, name), "wb") as handle:
                handle.write(b"x")
        os.utime(os.path.join(self.dir, young_name), (1e9, 1e9))
        result, _ = self.rotate(now=datetime.datetime(2026, 11, 1, 12, tzinfo=datetime.timezone.utc))
        self.assertEqual(result["pruned"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, old_name)))
        self.assertTrue(os.path.exists(os.path.join(self.dir, young_name)))
        # (d) the end-alert reader's count file is never seen empty
        count_file = os.path.join(self.dir, "count.json")
        EA._write_count(count_file, {"applied": 0, "chain": None})
        stop_flag = []

        def writer():
            n = 0
            while not stop_flag:
                n += 1
                EA._write_count(count_file, {"applied": n, "chain": None})
        thread = _t.Thread(target=writer, daemon=True)
        thread.start()
        empty = 0
        try:
            for _ in range(200):
                with open(count_file) as handle:
                    if not handle.read():
                        empty += 1
        finally:
            stop_flag.append(1)
            thread.join()
        self.assertEqual(empty, 0)
        print("S21: 4 of 4 checks pass, %d occurrences of the planted path in the failure line, 1 link refused with link and target unchanged, 1 old archive removed and "
              "1 young archive kept, %d empty reads of the count file in 200 polls" % (planted, empty))


# ---- S25 and S26: the record lock and the archive of pending (constraint PC-8, pull request 2 continuation) ----
class TestRecordLockAndPending(Base):
    def check_s25(self, cw=CW):
        self.put(lines(0, 3000, 3000))
        original = cw.read_rotation_record
        armed, reached, b_done = threading.Event(), threading.Event(), threading.Event()
        owner = []

        def reading(path):   # the first read of the final write of the first rotator holds, so the second rotator runs inside it
            rec = original(path)
            if armed.is_set() and threading.current_thread() is owner[0]:
                armed.clear()
                reached.set()
                b_done.wait(1.5)
            return rec
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(cw, "read_rotation_record", reading):
            first = threading.Thread(target=lambda: cw.rotate_audit(self.audit, _before_swap=armed.set), daemon=True)
            owner.append(first)
            first.start()
            if not reached.wait(30):
                raise AssertionError("the first rotation never reached its final write")
            self.put(lines(3000, 6000, 3000), "a")   # the live file is over the size again
            cw.rotate_audit(self.audit)
            b_done.set()
            first.join(30)
        record = original(self.audit)
        names = sorted(n for n in os.listdir(self.dir) if n.startswith("audit-") and n.endswith(".jsonl.gz"))
        sizes = {}
        for name in names:
            with gzip.open(os.path.join(self.dir, name), "rb") as handle:
                sizes[name] = len(handle.read())
        cut_names = [c["archive"] for c in record["cuts"]]
        text, _ = TestLockAndRecord.all_text(self)
        got = TestLockAndRecord.ids(self, text)
        if record["done"] != len(names) or sorted(cut_names) != names or sum(c["cut"] for c in record["cuts"]) != sum(sizes.values()):
            raise AssertionError("a cut was lost: done %d and %d archives, %d cuts" % (record["done"], len(names), len(record["cuts"])))
        pend = record["pending"]
        if pend is not None and pend.get("archive") not in names:
            raise AssertionError("a cut was lost: pending names no archive")
        if got != list(range(6000)):
            raise AssertionError("a cut was lost: %d of 6000 lines present" % len(got))
        return record["done"], len(names), len(record["cuts"]), len(got)

    def test_s25(self):
        done, archives, cuts, total = self.check_s25()
        self.assertEqual((done, archives, cuts, total), (2, 2, 2, 6000))
        self.setUp()
        mutant = load("cw", 'lock = _audit_lock(path, fcntl.LOCK_EX, ".record.lock")', 'lock = open(os.devnull, "rb")')
        with self.assertRaises(AssertionError) as caught:
            self.check_s25(mutant)
        self.assertIn("a cut was lost", str(caught.exception))
        print("S25: done %d equals the %d archives and %d cuts in the control, cut bytes equal archive bytes, %d of 6000 lines once and in order, the control passes 1 of 1 "
              "and the copy without the record lock fails 1 of 1 with the anchored text (%s), the mutation applied" % (done, archives, cuts, total, str(caught.exception)))

    def build(self):
        name = "audit-2026-01-01.jsonl.gz"
        old = lines(0, 100, 20).encode()
        with gzip.open(os.path.join(self.dir, name), "wb") as handle:
            handle.write(old)
        live = lines(100, 130, 20)
        self.put(live)
        with open(self.audit + ".rotations", "w") as handle:
            json.dump({"done": 0, "cuts": [], "pending": {"cut": len(old), "old_size": len(old) + len(live.encode()) + 5000, "archive": name,
                                                           "ts": time.time(), "new_id": [os.stat(self.audit).st_dev, os.stat(self.audit).st_ino]}}, handle)
        return len(old) - len(lines(60, 100, 20).encode())

    def check_s26(self, ea=EA, lw=LW):
        offset = self.build()
        end, lost = TestArchiveRead.readers(self, offset, ea, lw)
        with contextlib.redirect_stderr(io.StringIO()):
            got = (end.read_all(), lost.read_all())
        for reader, ids in zip(("end alert", "lost worker"), got):
            if ids != list(range(60, 130)):
                raise AssertionError("a line was skipped across a pending rotation: %s delivered %d of 70 lines" % (reader, len(ids)))
        return len(got[0]), len(got[1])

    def test_s26(self):
        self.assertEqual(self.check_s26(), (70, 70))
        self.setUp()
        old_ea, old_lw = 'entries + [(pending["cut"], pending.get("archive"))]', 'entries + [(pend["cut"], pend.get("archive"))]'
        mutant_ea = load("ea", old_ea, 'entries + [(pending["cut"], None)]')
        mutant_lw = load("lw", old_lw, 'entries + [(pend["cut"], None)]')
        with self.assertRaises(AssertionError) as caught:
            self.check_s26(mutant_ea, mutant_lw)
        self.assertIn("a line was skipped across a pending rotation", str(caught.exception))
        self.setUp()
        with self.assertRaises(AssertionError) as caught2:
            self.check_s26(EA, mutant_lw)
        self.assertIn("lost worker delivered 30 of 70", str(caught2.exception))
        print("S26: 2 readers x 70 of 70 lines (40 of the archive named by pending, 30 live) read once and in order in the control, the control passes 1 of 1 and the copies fail "
              "with the anchored text (%s; %s), the mutation applied" % (str(caught.exception), str(caught2.exception)))


# ---- S28 to S31: the idempotent final write, the ownership of pending, the stale age, the failed final write ----
class Killed(BaseException):
    """A rotator killed after its swap: nothing after the swap runs."""


def fresh(rotate_bytes=None, keep_bytes=None):
    cw = load("cw")
    if rotate_bytes:
        cw.AUDIT_ROTATE_BYTES, cw.AUDIT_KEEP_BYTES = rotate_bytes, keep_bytes
    return cw


class TestIdempotentAndOwned(Base):
    def reader_ids(self, cw):
        lost = LostReader(self.audit, LW)
        lost.state = {"offsets": {"launcher": 0}, "rotations": {"launcher": {"applied": 0, "chain": None}}}
        with contextlib.redirect_stderr(io.StringIO()):
            return lost.read_all()

    def archives(self):
        return sorted(n for n in os.listdir(self.dir) if n.startswith("audit-") and n.endswith(".jsonl.gz"))

    def check_s28(self, cw=None):
        cw = cw or CW
        self.put(lines(0, 30000, 0))
        real = cw._record_update
        armed, reached, b_done, owner = threading.Event(), threading.Event(), threading.Event(), []

        def wrapped(path, change):   # the first rotator's final write holds until the second has completed its pending record
            if armed.is_set() and threading.current_thread() is owner[0] and change.__name__ == "final":
                armed.clear()
                reached.set()
                b_done.wait(5)
            return real(path, change)
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(cw, "_record_update", wrapped):
            first = threading.Thread(target=lambda: cw.rotate_audit(self.audit, _before_swap=armed.set), daemon=True)
            owner.append(first)
            first.start()
            if not reached.wait(30):
                raise AssertionError("the first rotation never reached its final write")
            cw.rotate_audit(self.audit)   # completes the first's pending record in the window
            b_done.set()
            first.join(30)
        record = cw.read_rotation_record(self.audit)
        names = self.archives()
        cuts = [c["archive"] for c in record["cuts"]]
        if len(cuts) != len(set(cuts)) or record["done"] != len(names) or record["pending"] is not None:
            raise AssertionError("a cut was recorded twice: %d archive, done %d, %d cuts for %d archive, pending %r" % (len(names), record["done"], len(cuts), len(set(cuts)), record["pending"]))
        got = self.reader_ids(cw)
        if got != list(range(30000)):
            raise AssertionError("a cut was recorded twice: the reader read %d of 30000 lines" % len(got))
        return len(names), record["done"], cuts.count(names[0]), record["pending"], len(got)

    def test_s28(self):
        self.assertEqual(self.check_s28(), (1, 1, 1, None, 30000))
        self.setUp()
        mutant = load("cw", 'if not any(isinstance(c, dict) and c.get("archive") == mine for c in record["cuts"]):', "if True:")
        with self.assertRaises(AssertionError) as caught:
            self.check_s28(mutant)
        self.assertIn("a cut was recorded twice", str(caught.exception))
        print("S28: 1 archive, done 1, the archive named once in the cuts and pending null in the control, 30000 of 30000 lines once and in order, the control passes 1 of 1 and the "
              "copy that appends without the check fails 1 of 1 with the anchored text (%s), the mutation applied" % str(caught.exception))

    # ---- S29 ----
    def schedule(self, cw, seed):
        import random
        rng = random.Random(seed)
        self.put(lines(0, 3000, 0))
        cond = threading.Condition()
        parked, go, finished = {}, {}, set()
        kill_name = "C" if seed % 2 == 0 else None
        overwritten = []
        real_update, real_complete, real_write = cw._record_update, cw._complete_pending, cw.write_rotation_record

        def gate(step):
            me = threading.current_thread().name
            with cond:
                parked[me] = step
                cond.notify_all()
                cond.wait_for(lambda: go.get(me))
                go[me] = False
                del parked[me]
            if step == "final" and me == kill_name:
                raise Killed()

        def update(path, change):
            if change.__name__ in ("put_pending", "final") and threading.current_thread().name in go:
                gate("pending" if change.__name__ == "put_pending" else "final")
            return real_update(path, change)

        def complete(path, now_ts=None):
            if threading.current_thread().name in go:
                gate("start")
            return real_complete(path, now_ts)

        def write(path, rec):
            if rec.get("pending") is not None:
                held = cw.read_rotation_record(path)["pending"]
                if held is not None and held != rec["pending"]:
                    overwritten.append(1)
            return real_write(path, rec)

        def body(name):
            def run():
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        cw.rotate_audit(self.audit, _before_lock=lambda: gate("lock"), _before_swap=lambda: gate("swap"))
                except Killed:
                    pass
                finally:
                    with cond:
                        finished.add(name)
                        cond.notify_all()
            return run
        names = ["A", "B", "C"]
        threads = [threading.Thread(target=body(n), name=n, daemon=True) for n in names]
        for n in names:
            go[n] = False
        with mock.patch.object(cw, "_record_update", update), mock.patch.object(cw, "_complete_pending", complete), mock.patch.object(cw, "write_rotation_record", write):
            for t in threads:
                t.start()
            deadline = time.time() + 60
            while len(finished) < 3:
                with cond:
                    cond.wait_for(lambda: parked or len(finished) == 3, timeout=1)
                    ready = sorted(parked)
                if not ready:
                    if time.time() > deadline:
                        raise AssertionError("the schedule deadlocked (seed %d)" % seed)
                    continue
                pick = rng.choice(ready)
                with cond:
                    go[pick] = True
                    cond.notify_all()
                    cond.wait_for(lambda: pick in parked or pick in finished, timeout=0.05)
            for t in threads:
                t.join(10)
        if overwritten:
            raise AssertionError("a pending was overwritten (seed %d)" % seed)
        self.put(lines(3000, 3500, 0), "a")
        with contextlib.redirect_stderr(io.StringIO()):
            cw.rotate_audit(self.audit)   # the next rotation records the killed rotator's cut
        record = cw.read_rotation_record(self.audit)
        names_on_disk = self.archives()
        cuts = [c["archive"] for c in record["cuts"]]
        sizes = 0
        for name in names_on_disk:
            with gzip.open(os.path.join(self.dir, name), "rb") as handle:
                sizes += len(handle.read())
        if sorted(cuts) != names_on_disk or record["done"] != len(names_on_disk) or record["pending"] is not None or sum(c["cut"] for c in record["cuts"]) != sizes:
            raise AssertionError("a pending was overwritten: schedule %d done %d, %d archives, %d cuts, pending %r" % (seed, record["done"], len(names_on_disk), len(cuts), record["pending"]))
        text, _ = TestLockAndRecord.all_text(self)
        got = TestLockAndRecord.ids(self, text)
        if got != list(range(3500)):
            raise AssertionError("a pending was overwritten: schedule %d holds %d of 3500 lines" % (seed, len(got)))
        return len(names_on_disk)

    def run_schedules(self, cw, seed, count=200, stop_at_first=False):
        failed, rotations = [], 0
        for i in range(count):
            self.setUp()
            try:
                rotations += self.schedule(cw, seed * 1000 + i)
            except AssertionError as e:
                failed.append(str(e))
                if stop_at_first:
                    break
        return failed, rotations

    def test_s29(self):
        seed = 29
        print("S29: seed %d" % seed)
        cw = fresh(300000, 100000)
        failed, rotations = self.run_schedules(cw, seed)
        self.assertEqual(failed, [])
        self.setUp()
        mutant = load("cw", 'if record["pending"] is not None:\n                return None\n            wrote.append(1)', "wrote.append(1)")
        mutant.AUDIT_ROTATE_BYTES, mutant.AUDIT_KEEP_BYTES = 300000, 100000
        failed2, _ = self.run_schedules(mutant, seed, 200, True)
        self.assertTrue(failed2 and "a pending was overwritten" in failed2[0], failed2)
        print("S29: seed %d, 200 of 200 schedules pass in the control (%d archives in all), the copy that writes pending whatever the record holds fails at least 1 of the 200 with the anchored "
              "text (%s), the mutation applied" % (seed, rotations, failed2[0]))

    # ---- S30 ----
    def check_s30(self, cw=None):
        import datetime
        cw = cw or fresh(300000, 100000)
        T = 1_800_000_000
        now = lambda extra: datetime.datetime.fromtimestamp(T + extra, datetime.timezone.utc)
        self.put(lines(0, 3000, 0))
        name = "audit-2026-01-01.jsonl.gz"
        orphan = os.path.join(self.dir, name)
        with gzip.open(orphan, "wb") as handle:
            handle.write(b"x\n")
        pend = {"cut": 2, "old_size": 99, "archive": name, "ts": T, "new_id": [0, 0]}
        with open(self.audit + ".rotations", "w") as handle:
            json.dump({"done": 0, "cuts": [], "pending": pend}, handle)
        lost = LostReader(self.audit, LW)
        with contextlib.redirect_stderr(io.StringIO()):
            lost.once()   # first sight
        def tree():
            return {k: v for k, v in snapshot(self.dir).items() if not k.endswith(".lock")}
        before = tree()
        result, _ = self.rotate(cw, now=now(10))
        record = cw.read_rotation_record(self.audit)
        if record["pending"] != pend or result is not None or tree() != before:
            raise AssertionError("a live rotator's pending was cleared: pending %r, rotation %r" % (record["pending"], result))
        self.put(lines(3000, 3003, 0), "a")
        with contextlib.redirect_stderr(io.StringIO()):
            polled = lost.once()
        if polled != [3000, 3001, 3002]:
            raise AssertionError("the reader polling before the swap read %r" % polled)
        shutil_size = os.path.getsize(self.audit)
        cw.AUDIT_ROTATE_BYTES = shutil_size + 1_000_000   # the stale case clears and does not rotate
        self.rotate(cw, now=now(121))
        record = cw.read_rotation_record(self.audit)
        if record["pending"] is not None or record["cuts"] or not os.path.exists(orphan):
            raise AssertionError("the stale pending was not cleared with its archive left: %r" % (record,))
        return 1, 1, len(polled)

    def test_s30(self):
        self.assertEqual(self.check_s30(), (1, 1, 3))
        self.setUp()
        mutant = load("cw", 'if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts > now_ts or now_ts - ts > AUDIT_PENDING_STALE:', "if True:")
        mutant.AUDIT_ROTATE_BYTES, mutant.AUDIT_KEEP_BYTES = 300000, 100000
        with self.assertRaises(AssertionError) as caught:
            self.check_s30(mutant)
        self.assertIn("a live rotator's pending was cleared", str(caught.exception))
        print("S30: the young pending is left untouched in 1 of 1 case and no rotation is made, the stale one (121 s on a fake clock) is cleared in 1 of 1 case with its archive left, the reader "
              "reads 3 of 3 new lines, the control passes 1 of 1 and the copy that clears an unswapped pending fails 1 of 1 with the anchored text (%s), the mutation applied" % str(caught.exception).split(":")[0])

    # ---- S31 ----
    def check_s31(self, cw=None):
        cw = cw or fresh(300000, 100000)
        self.put(lines(0, 3000, 0))
        original = cw.write_rotation_record

        def failing(path, rec):
            if rec.get("pending") is None and rec.get("done") == 1:
                raise OSError(5, "injected")
            return original(path, rec)
        with mock.patch.object(cw, "write_rotation_record", failing):
            self.rotate(cw)
        first = cw.read_rotation_record(self.audit)
        if first["pending"] is None:
            raise AssertionError("the cut of a failed final write was lost: pending was cleared by the failed write")
        old_size = first["pending"].get("old_size")
        self.put(lines(3000, 9500, 0), "a")
        grown = os.path.getsize(self.audit)
        if not old_size or grown < 2 * old_size:
            raise AssertionError("the fixture did not regrow: %r %r" % (old_size, grown))
        cw._complete_pending(self.audit)
        record = cw.read_rotation_record(self.audit)
        names = self.archives()
        if record["done"] != 1 or [c["archive"] for c in record["cuts"]] != names or record["pending"] is not None:
            raise AssertionError("the cut of a failed final write was lost: done %d, %d cuts, %d archives, pending %r (old_size %d, live %d)"
                                 % (record["done"], len(record["cuts"]), len(names), record["pending"], old_size, grown))
        got = self.reader_ids(cw)
        if got != list(range(9500)):
            raise AssertionError("the cut of a failed final write was lost: the reader read %d of 9500 lines" % len(got))
        return old_size, grown, record["done"], len(got)

    def test_s31(self):
        old_size, grown, done, total = self.check_s31()
        self.assertEqual((done, total), (1, 9500))
        texts = []
        for old, new in (("                if attempt == 2:\n", "                if attempt == 2:\n                    _record_update(path, lambda r: dict(r, pending=None))\n"),
                         ('swapped = list(pend.get("new_id") or []) == [st.st_dev, st.st_ino]', 'swapped = st.st_size < pend.get("old_size", 0)')):
            self.setUp()
            mutant = load("cw", old, new)
            mutant.AUDIT_ROTATE_BYTES, mutant.AUDIT_KEEP_BYTES = 300000, 100000
            with self.assertRaises(AssertionError) as caught:
                self.check_s31(mutant)
            self.assertIn("the cut of a failed final write was lost", str(caught.exception))
            texts.append(str(caught.exception))
        print("S31: the cut recorded once in the control with the live file grown to %d bytes for an old_size of %d, %d of 9500 lines once and in order, the control passes 1 of 1 and each of the "
              "2 copies fails 1 of 1 with the anchored text (%s | %s), the mutations applied" % (grown, old_size, total, texts[0], texts[1]))


# ---- S12: the README ----
class TestGiveUpAndBadTs(Base):
    """S32: a give-up leaks no descriptor; a pending dated after now, without ts or with a word for ts is stale."""

    def fds(self):
        return len(os.listdir("/proc/self/fd"))

    def check_give_ups(self, cw=CW, count=200):
        self.put(lines(0, 3000, 1500))
        before = self.fds()
        for _ in range(count):
            def replace():   # another rotation replaces the file between the read and the lock
                tmp = self.audit + ".other"
                with open(self.audit, "rb") as src, open(tmp, "wb") as dst:
                    dst.write(src.read())
                os.replace(tmp, self.audit)
            self.rotate(cw, _before_lock=replace)
        after = self.fds()
        temps = [n for n in os.listdir(self.dir) if n.endswith(".tmp") or n.startswith(".audit.") or n.endswith(".other")]
        if after != before:
            raise AssertionError("a descriptor leaked per give-up: %d more descriptors" % (after - before))
        return before, after, len(temps)

    def test_give_ups(self):
        before, after, temps = self.check_give_ups()
        self.assertEqual((before, temps), (after, 0))
        self.setUp()
        mutant = load("cw", "        if keep_fd is not None:   # a give-up or a failure closes the file opened for the replacement, once\n",
                      "        if False:\n")
        with self.assertRaises(AssertionError) as caught:
            self.check_give_ups(mutant)
        self.assertIn("a descriptor leaked per give-up", str(caught.exception))
        self.leak = str(caught.exception).split(": ")[1]
        print("S32: the descriptor count is equal before and after 200 give-ups (%d and %d) in the control, %d temporary files; the copy that does not close its file fails 1 of 1 "
              "with the anchored text (%s), the mutation applied" % (before, after, temps, self.leak))

    def check_bad_ts(self, cw=CW):
        cleared = 0
        for label, ts in (("10 minutes after the clock", 1000 + 600), ("missing", None), ("the word x", "x"), ("the integer 10**400", 10**400)):
            self.setUp()
            self.put(lines(0, 3000, 1500))
            pend = {"cut": 5, "old_size": 9, "archive": "audit-2020-01-01.jsonl.gz", "new_id": [0, 0]}
            if ts is not None:
                pend["ts"] = ts
            cw.write_rotation_record(self.audit, {"done": 0, "cuts": [], "pending": pend})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result, more = self.rotate(cw, now=dt.datetime.fromtimestamp(1000, dt.timezone.utc))
            record = cw.read_rotation_record(self.audit)
            bad = [x for x in (more + err.getvalue()).splitlines() if "was rotated" not in x]
            if bad or record["done"] != 1:
                raise AssertionError("a huge ts stopped the rotation: %s, done %s, errors %r" % (label, record["done"], "; ".join(bad)[:60]))
            self.assertEqual((record["pending"], len(record["cuts"])), (None, 1))
            cleared += 1
        return cleared

    def test_bad_ts(self):
        for label, ts in (("10 minutes after the clock", 1000 + 600), ("missing", None), ("the word x", "x"), ("the integer 10**400", 10**400)):
            with self.subTest(label):
                self.setUp()
                self.put(lines(0, 3000, 1500))
                pend = {"cut": 5, "old_size": 9, "archive": "audit-2020-01-01.jsonl.gz", "new_id": [0, 0]}
                if ts is not None:
                    pend["ts"] = ts
                CW.write_rotation_record(self.audit, {"done": 0, "cuts": [], "pending": pend})
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    CW._complete_pending(self.audit, 1000)
                self.assertEqual(err.getvalue(), "")
                record = CW.read_rotation_record(self.audit)
                self.assertEqual((record["pending"], record["cuts"], record["done"]), (None, [], 0))
                result, _ = self.rotate(now=dt.datetime.fromtimestamp(1000, dt.timezone.utc))
                self.assertEqual(CW.read_rotation_record(self.audit)["done"], 1)
        print("S32: 4 of 4 stale pendings cleared (10 minutes after the clock, missing ts, ts x, ts 10**400) with no cut naming them, 0 errors printed, and the rotation ran after each (done 1)")

    def test_huge_ts_without_the_try(self):
        self.assertEqual(self.check_bad_ts(), 4)
        mutant = load("cw", "            except (OverflowError, ValueError):\n", "            except ZeroDivisionError:\n")
        mutant.AUDIT_ROTATE_BYTES, mutant.AUDIT_KEEP_BYTES = CW.AUDIT_ROTATE_BYTES, CW.AUDIT_KEEP_BYTES
        with self.assertRaises(AssertionError) as caught:
            self.check_bad_ts(mutant)
        self.assertIn("a huge ts stopped the rotation", str(caught.exception))
        print("S32: the copy of the stale test without the try fails 1 of 1 with the anchored text (%s), control first, mutation applied" % str(caught.exception)[:60])


class TestTakenArchiveName(Base):
    """S33: both archive names taken is an expected give-up that prints nothing; a real failure still prints one line."""
    NOW = dt.datetime(2026, 1, 5, 10, 0, 0, tzinfo=dt.timezone.utc)

    def check_taken(self, cw=CW):
        self.big()
        for name in ("audit-2026-01-05.jsonl.gz", "audit-2026-01-05T100000.jsonl.gz"):
            with open(os.path.join(self.dir, name), "wb") as handle:
                handle.write(b"x")
        with open(self.audit, "rb") as handle:
            before = handle.read()
        result, printed = self.rotate(cw, now=self.NOW)
        with open(self.audit, "rb") as handle:
            same = handle.read() == before
        names = sorted(os.listdir(self.dir))
        record = cw.read_rotation_record(self.audit)
        pending = record.get("pending") if isinstance(record, dict) else None
        count = len(printed.splitlines())
        if count:
            raise AssertionError("an expected give-up printed an error line: %d printed line" % count)
        self.assertIsNone(result)
        temps = [n for n in names if n not in ("audit.jsonl", "audit-2026-01-05.jsonl.gz", "audit-2026-01-05T100000.jsonl.gz")]
        self.assertEqual(temps, [x for x in temps if x.startswith("audit.jsonl.") and not x.endswith(".tmp")])
        self.assertFalse([n for n in temps if n.endswith(".tmp") or n.startswith(".audit.")])
        self.assertTrue(same)
        self.assertIsNone(pending)
        return count, len([n for n in names if n.endswith(".tmp") or n.startswith(".audit.")]), same, pending

    def test_taken(self):
        count, temps, same, pending = self.check_taken()
        self.assertEqual((count, temps, same, pending), (0, 0, True, None))
        self.setUp()
        self.big()
        def full(*a, **k):
            raise OSError(28, "No space left on device")
        with mock.patch.object(gzip, "GzipFile", full):
            result, printed = self.rotate(now=self.NOW)
        real = len(printed.splitlines())
        self.assertEqual(real, 1)
        self.assertIn("OSError", printed)
        self.setUp()
        mutant = load("cw", '            raise _GaveUp()\n        temps.append(archive)', '            raise FileExistsError(0, "no free archive name")\n        temps.append(archive)')
        with self.assertRaises(AssertionError) as caught:
            self.check_taken(mutant)
        self.assertIn("an expected give-up printed an error line", str(caught.exception))
        shown = str(caught.exception).split(": ")[1]
        print("S33: both names taken: returned None, %d lines printed, %d temp files, live file equal %s, no pending; real failure printed %d line; control 1 of 1, "
              "mutant 1 of 1 with the text (%s), mutation applied True" % (count, temps, same, real, shown))


class TestReadme(unittest.TestCase):
    @staticmethod
    def check(text):
        match = re.search(r"## Retention of the audit.*?(?=\n## )", text, re.S)
        if not match:
            raise AssertionError("the audit rotation line is missing")
        section = match.group(0)
        numbers = [n for n in ("4 MiB", "1 MiB", "30 days") if n in section]
        if len(numbers) != 3 or "rotation record" not in section or "archive" not in section \
                or "belongs to the rotation whose archive name it carries" not in section \
                or "older than 120 seconds or dated after now is stale" not in section:
            raise AssertionError("the audit rotation line is incomplete: %d of 3 numbers" % len(numbers))
        return len(numbers)

    def test_s12(self):
        with open(os.path.join(ROOT, "cloud", "README.md")) as handle:
            text = handle.read()
        self.assertEqual(self.check(text), 3)
        without = re.sub(r"## Retention of the audit.*?(?=\n## )", "", text, flags=re.S)
        self.assertNotEqual(without, text)
        with self.assertRaises(AssertionError) as caught:
            self.check(without)
        self.assertIn("audit rotation", str(caught.exception))
        print("S12: 1 of 1 audit line present with its 3 numbers (4 MiB, 1 MiB, 30 days) and the sentence naming the rotation record and the archive, the copy without the line "
              "fails 1 of 1 with the anchored text, control passes 1 of 1")


if __name__ == "__main__":
    unittest.main()
