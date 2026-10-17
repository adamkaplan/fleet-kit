# Fleet Switchboard presentation

A three-minute animated deck, with a narrated video you build from it. Ten
slides: the problem, the idea, delivery, intent, the foreground, the judge,
GitHub as memory, the proof, and a close.

| File | What it is |
| --- | --- |
| `index.html` | The deck. One file, no dependencies, plain CSS animation |
| `fleet-switchboard.mp4` | The narrated video (1080p, 24 fps, captions burned in). Not committed: build it, see Rebuild |
| `narration.json` | The script, one entry per slide, split into sentences |
| `audio/slide-NN.mp3` | The narration for each slide, used by autoplay |
| `timing.js` | When each sentence starts; generated, drives the animation cues |
| `build/` | The scripts that regenerate the audio and the video |

## Watch

Open `index.html` in a browser.

| Key | Does |
| --- | --- |
| Right arrow, Space, click | Next slide |
| Left arrow | Previous slide |
| `a` | Autoplay from this slide, with the narration |
| `c` | Toggle captions |
| `f` | Full screen |

Browsers refuse audio until you press a key, so use `a` rather than a link.
Adding `?captions=1` to the address starts with captions on.

The animation is timed to the narration: each element appears on the sentence
that talks about it (`--c0` .. `--c5` are the sentence start times from
`timing.js`).

## Rebuild

Needs Python 3 (standard library only), Node 18 or later, and an `ffmpeg` with
libx264, aac and libmp3lame.

```sh
# 1. Narration. Writes audio/, timing.js and build/work/narration.wav.
#    Calls a text-to-speech model through OpenRouter; sentences are cached
#    by hash in build/cache, so re-running only pays for what changed.
export OPENROUTER_API_KEY=...        # your own key; it is never written to a file
FFMPEG=/path/to/ffmpeg python3 build/tts.py

# 2. Video. Parks every animation at each frame time, screenshots it and
#    pipes the frames to ffmpeg together with the narration.
npm ci --prefix build
FFMPEG=/path/to/ffmpeg node build/render.mjs            # --fps 24 --crf 23 --out file.mp4
```

`render.mjs` drives Chromium through `playwright-core`. It finds an installed
Chromium; pass `--chrome /path/to/browser` if it does not. A full render takes
about six minutes and leaves `fleet-switchboard.mp4` (about 8 MB, git-ignored)
next to `index.html`. Step 2 needs the narration from step 1, so run both on a
fresh checkout. The sentence cache is not committed, so step 1 then needs a key.

## Voice

Synthesised with `google/gemini-3.8-flash-tts`, voice `Kore`, as raw 24 kHz mono
PCM. Words a speech model mispronounces carry a spoken override in
`narration.json` (`say`), for example "A P I". Change the voice or model under
`voice` in `narration.json`. The cache is keyed on text, model and voice.

## Editing

Change the words in `narration.json` and the visuals in `index.html`, then
rebuild. A slide lasts as long as its narration, so a longer script makes a
longer slide; keep each slide's animation inside its own sentence cues. Keep the
bottom 130 px free: captions sit there.
