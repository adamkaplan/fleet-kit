"""Ask #1743 (pull request 2): the audit rotation, the rotation record and the two readers across rotations (scenarios S6 S7 S10 S11 S12 S13 S14 S17).
Temporary directories only. Mutants are copies of a module's text made in memory: each text is asserted to occur once and the mutation to apply."""
import contextlib
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
        with contextlib.redirect_stderr(err):
            cw.rotate_audit(self.audit, _before_lock=inner)
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
        mutant = load("cw", "            _complete_pending(path)\n", "            pass\n")
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


# ---- S12: the README ----
class TestReadme(unittest.TestCase):
    @staticmethod
    def check(text):
        match = re.search(r"## Retention of the audit.*?(?=\n## )", text, re.S)
        if not match:
            raise AssertionError("the audit rotation line is missing")
        section = match.group(0)
        numbers = [n for n in ("4 MiB", "1 MiB", "30 days") if n in section]
        if len(numbers) != 3 or "rotation record" not in section or "archive" not in section:
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
