#!/usr/bin/env python3
"""Narration for the presentation: text to speech through OpenRouter, one request per sentence.

    OPENROUTER_API_KEY=... docs/presentation/build/tts.py

Reads narration.json, writes:
  audio/slide-NN.mp3      each slide's narration with its lead-in, gaps and tail (if ffmpeg is found)
  build/work/slide-NN.wav the same, uncompressed, for the video build
  build/work/narration.wav every slide in order: the video's whole audio track
  timing.js               when each sentence starts and each slide ends, for the deck's animation cues

Sentences are synthesised separately so the animation cues come from measured durations, not
estimates. Each result is cached by its text, voice and model under build/cache, so changing one
sentence re-synthesises one sentence. The key is read from the environment and never written down.
Python 3.9, standard library only.
"""
import array
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
URL = "https://openrouter.ai/api/v1/audio/speech"
SILENCE_THRESHOLD = 350   # |sample| below this is silence (16-bit)
KEEP_MS = 90              # silence kept at each end of a sentence after trimming


def synth(text, voice, key, attempts=4):
    body = json.dumps({"model": voice["model"], "voice": voice["voice"], "input": text,
                       "response_format": voice["format"]}).encode()
    request = urllib.request.Request(URL, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=180) as reply:
                data = reply.read()
            if len(data) < 2000:
                raise RuntimeError("audio too short (%d bytes) for %r" % (len(data), text))
            return data
        except urllib.error.HTTPError as err:
            detail = err.read()[:200].decode("utf-8", "replace")
            if err.code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("speech request failed: HTTP %d %s" % (err.code, detail)) from None
        except (urllib.error.URLError, TimeoutError, RuntimeError) as err:
            if attempt < attempts:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("speech request failed: %s" % err) from None


def trim(pcm, rate):
    """Drop leading and trailing silence, keeping KEEP_MS at each end."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    loud = [i for i, s in enumerate(samples) if abs(s) > SILENCE_THRESHOLD]
    if not loud:
        return pcm
    keep = int(rate * KEEP_MS / 1000)
    start, end = max(0, loud[0] - keep), min(len(samples), loud[-1] + keep)
    out = array.array("h", samples[start:end])
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def silence(seconds, rate):
    return b"\x00\x00" * int(round(seconds * rate))


def write_wav(path, pcm, rate):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)


def find_ffmpeg():
    candidate = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not candidate:
        return None
    try:
        done = subprocess.run([candidate, "-version"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return candidate if done.returncode == 0 else None


def main():
    plan = json.loads((ROOT / "narration.json").read_text())
    voice, timing = plan["voice"], plan["timing"]
    rate = voice["sample_rate"]
    key = os.environ.get("OPENROUTER_API_KEY")
    cache = HERE / "cache"
    work = HERE / "work"
    cache.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    (ROOT / "audio").mkdir(exist_ok=True)

    jobs = {}
    for slide in plan["slides"]:
        for seg in slide["segments"]:
            spoken = seg.get("say") or seg["text"]
            name = hashlib.sha1(("%s|%s|%s" % (voice["model"], voice["voice"], spoken)).encode()).hexdigest()[:20]
            jobs[spoken] = cache / (name + ".pcm")
    missing = {text: path for text, path in jobs.items() if not path.exists()}
    if missing and not key:
        sys.exit("tts: OPENROUTER_API_KEY is not set, and %d sentence(s) are not cached" % len(missing))
    if missing:
        print("synthesising %d of %d sentence(s) with %s, voice %s" % (len(missing), len(jobs), voice["model"], voice["voice"]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(synth, text, voice, key): (text, path) for text, path in missing.items()}
            for future in concurrent.futures.as_completed(futures):
                text, path = futures[future]
                path.write_bytes(trim(future.result(), rate))
                print("  ok: %s" % text[:70])

    ffmpeg = find_ffmpeg()
    track, clock, entries = b"", 0.0, []
    for number, slide in enumerate(plan["slides"], 1):
        pcm = silence(timing["lead_seconds"], rate)
        starts = []
        for index, seg in enumerate(slide["segments"]):
            if index:
                pcm += silence(timing["gap_seconds"], rate)
            starts.append(len(pcm) / 2 / rate)
            audio = jobs[seg.get("say") or seg["text"]].read_bytes()
            pcm += audio
            seg["_end"] = len(pcm) / 2 / rate
        pcm += silence(timing["tail_seconds"], rate)
        duration = len(pcm) / 2 / rate
        write_wav(work / ("slide-%02d.wav" % number), pcm, rate)
        if ffmpeg:
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(work / ("slide-%02d.wav" % number)),
                            "-codec:a", "libmp3lame", "-q:a", "5", str(ROOT / "audio" / ("slide-%02d.mp3" % number))], check=True)
        entries.append({"id": slide["id"], "start": round(clock, 3), "duration": round(duration, 3),
                        "segments": [{"start": round(s, 3), "end": round(seg["_end"], 3), "text": seg["text"]}
                                     for s, seg in zip(starts, slide["segments"])]})
        track += pcm
        clock += duration
    write_wav(work / "narration.wav", track, rate)
    (ROOT / "timing.js").write_text(
        "// Generated by build/tts.py: when each sentence starts, and how long each slide lasts.\n"
        "window.PRESENTATION_TIMING = %s;\n" % json.dumps({"total": round(clock, 3), "slides": entries}, indent=1))
    print("narration: %.1f s over %d slides; %s" % (clock, len(entries), "mp3 written" if ffmpeg else "no ffmpeg found: wav only"))


if __name__ == "__main__":
    main()
