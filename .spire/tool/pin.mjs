#!/usr/bin/env node
// The pin of a vendored tool copy (.spire/tool/): reading it, computing the folder checksum, verifying it.
// One implementation, used by the installer (bin/spire-land.mjs) and shipped inside the tool, so that anything
// running from a copy can check the copy before it does its work:   node .spire/tool/pin.mjs verify
//
// The PIN covers two folders the installer owns: .spire/tool/ (`checksum`) and the guide, the skill folder .agents/skills/spire-land/ of the project (`skill`).
// The checksum rule is LAYOUT.md's: the sha256 of the text `sha256sum` prints for every file under the folder
// EXCEPT the top-level PIN, as "<hash>  <path relative to the folder>" lines sorted bytewise (LC_ALL=C).
import { createHash } from 'node:crypto';
import { lstatSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const sha256 = (data) => createHash('sha256').update(data).digest('hex');

// Every file under dir as relative paths with '/' separators, sorted bytewise, PIN (top level) excluded.
// Throws on anything that is not a plain file or directory (symlinks, sockets) and on names sha256sum would escape.
export function listFiles(dir) {
  const out = [];
  const walk = (abs, rel) => {
    for (const name of readdirSync(abs)) {
      if (/[\\\n\r]/.test(name)) throw new Error(`unsupported file name in ${rel || '.'}: ${JSON.stringify(name)}`);
      const r = rel ? `${rel}/${name}` : name;
      const st = lstatSync(join(abs, name));
      if (st.isDirectory()) walk(join(abs, name), r);
      else if (st.isFile()) { if (r !== 'PIN') out.push(r); }
      else throw new Error(`not a plain file or directory: ${r}`);
    }
  };
  walk(dir, '');
  return out.sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
}

// The checksum of a set of files given as { relativePath: contentHash }.
export function checksumOf(hashes) {
  const lines = Object.entries(hashes).map(([p, h]) => Buffer.from(`${h}  ${p}\n`));
  lines.sort(Buffer.compare);
  return sha256(Buffer.concat(lines));
}

export const hashFile = (path) => sha256(readFileSync(path));

// The ONE checksum of a tool folder on disk.
export function folderChecksum(dir) {
  const hashes = {};
  for (const p of listFiles(dir)) hashes[p] = hashFile(join(dir, p));
  return checksumOf(hashes);
}

export function parsePin(text) {
  const pin = {};
  for (const line of text.split('\n')) {
    const m = /^([a-z]+)=(.*)$/.exec(line);
    if (m) pin[m[1]] = m[2];
  }
  return pin;
}

export const formatPin = ({ source, version, commit, checksum, skill }) =>
  `source=${source}\nversion=${version}\ncommit=${commit}\nchecksum=${checksum}\n${skill ? `skill=${skill}\n` : ''}`;

// The skill folder (the one guide) of the project whose tool folder this is: <project>/.agents/skills/spire-land.
export const skillDirOf = (toolDir) => join(toolDir, '..', '..', '.agents', 'skills', 'spire-land');

export function readPin(dir) {
  let text;
  try { text = readFileSync(join(dir, 'PIN'), 'utf8'); } catch { return null; }
  return parsePin(text);
}

// { ok, pin, actual, actualSkill, reason } for a tool folder and, when the PIN names one, its skill folder.
export function verify(dir) {
  const pin = readPin(dir);
  if (!pin) return { ok: false, reason: `no PIN in ${dir}` };
  if (!/^[0-9a-f]{64}$/.test(pin.checksum || '')) return { ok: false, pin, reason: `PIN has no valid checksum in ${dir}` };
  let actual;
  try { actual = folderChecksum(dir); } catch (e) { return { ok: false, pin, reason: e.message }; }
  if (actual !== pin.checksum) {
    return { ok: false, pin, actual, reason: `checksum differs from PIN (pinned ${pin.checksum.slice(0, 12)}, actual ${actual.slice(0, 12)}): ${dir} was edited, or an update was interrupted` };
  }
  let actualSkill;
  if (pin.skill) {
    const sdir = skillDirOf(dir);
    try { actualSkill = folderChecksum(sdir); } catch (e) { return { ok: false, pin, actual, reason: `the guide ${sdir} is missing or unreadable (${e.code || e.message}): update --discard-edits restores it` }; }
    if (actualSkill !== pin.skill) {
      return { ok: false, pin, actual, actualSkill, reason: `skill checksum differs from PIN (pinned ${pin.skill.slice(0, 12)}, actual ${actualSkill.slice(0, 12)}): ${sdir} was edited, or an update was interrupted` };
    }
  }
  return { ok: true, pin, actual, actualSkill };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const [cmd, arg] = process.argv.slice(2);
  const dir = arg || dirname(fileURLToPath(import.meta.url));
  if (cmd === 'verify') {
    const r = verify(dir);
    if (r.ok) { console.log(`tool ok: ${r.pin.version} ${r.pin.commit.slice(0, 12)} ${r.actual.slice(0, 12)}`); process.exit(0); }
    console.error(`tool DRIFTED: ${r.reason}`);
    process.exit(1);
  }
  console.error('usage: pin.mjs verify [tool-dir]');
  process.exit(2);
}
