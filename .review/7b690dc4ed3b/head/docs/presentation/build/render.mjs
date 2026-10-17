// Renders index.html to an MP4: every slide is parked at each frame time through
// window.deck.seek, screenshotted, and piped straight into ffmpeg together with
// work/narration.wav.
//
//   FFMPEG=/path/to/ffmpeg node build/render.mjs [--fps 24] [--crf 23] [--out file.mp4] [--chrome /path/to/browser]
//
// Needs a Chromium that playwright-core can launch (it finds a system install,
// or pass --chrome).
import { chromium } from "playwright-core";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const arg = (name, dflt) => {
  const i = process.argv.indexOf("--" + name);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const fps = Number(arg("fps", 24));
const crf = String(arg("crf", 23));
const out = path.resolve(arg("out", path.join(root, "fleet-switchboard.mp4")));
const ffmpeg = process.env.FFMPEG || "ffmpeg";
const audio = path.join(here, "work", "narration.wav");

const browser = await chromium.launch(arg("chrome", null) ? { executablePath: arg("chrome") } : {});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
await page.goto(pathToFileURL(path.join(root, "index.html")).href + "?captions=1");
await page.waitForFunction(() => window.deck);
const durations = await page.evaluate(() => window.deck.durations);

const enc = spawn(
  ffmpeg,
  [
    "-y", "-loglevel", "error",
    "-f", "image2pipe", "-framerate", String(fps), "-c:v", "mjpeg", "-i", "-",
    "-i", audio,
    "-c:v", "libx264", "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p", "-r", String(fps),
    "-c:a", "aac", "-b:a", "128k",
    "-movflags", "+faststart", "-shortest",
    out,
  ],
  { stdio: ["pipe", "inherit", "inherit"] },
);
const done = new Promise((res, rej) => {
  enc.on("exit", (c) => (c === 0 ? res() : rej(new Error("ffmpeg exited " + c))));
  enc.on("error", rej);
});
enc.stdin.on("error", () => {});

let start = 0;
let frames = 0;
for (let i = 0; i < durations.length; i++) {
  const end = start + durations[i];
  // whole frames per slide from the running total, so audio and video never drift
  const n = Math.round(end * fps) - Math.round(start * fps);
  for (let k = 0; k < n; k++) {
    await page.evaluate(([s, t]) => window.deck.seek(s, t), [i, k / fps]);
    const jpg = await page.screenshot({ type: "jpeg", quality: 92 });
    if (!enc.stdin.write(jpg)) await new Promise((r) => enc.stdin.once("drain", r));
    frames++;
  }
  console.log(`slide ${i + 1}/${durations.length}: ${n} frames`);
  start = end;
}
enc.stdin.end();
await done;
await browser.close();
console.log(`wrote ${out} (${frames} frames at ${fps} fps)`);
if (errors.length) console.log("page errors:", errors);
