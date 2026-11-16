// Stand-ins for the "herdr" module, for running keeper.js under Node (tests/keeper/run.mjs).
// Everything reads and records through globalThis, so a case sets up a world and checks what
// the keeper did:
//   __spaces, __panes, __terminals, __machines, __focused   what the app shows
//   __shared                                                shared state (setShared writes here)
//   __files   { "<machine or ''>:<path>": text }            files readTextFile sees (null/absent: none)
//   __offline [machine names]                               readTextFile/exec on them throw
//   __fail    ["<machine>:<path>"]                          readTextFile of these throws (a failed read)
//   __runs    [[name, args]]                                every run() call
//   __notes   [notification]                                every notify() call
//   __reads   ["<machine>:<path>"]                          every readTextFile() call
//   __writes  ["<machine>:<path>"]                          every file write
//   __now                                                   Date.now()
const g = globalThis;
const realNow = Date.now.bind(Date);
Date.now = () => (typeof g.__now === "number" ? g.__now : realNow());

export const spaces = () => g.__spaces ?? [];
export const panes = () => g.__panes ?? [];
export const terminals = () => g.__terminals ?? [];
export const machines = () => g.__machines ?? [{ name: null, layout: "ready" }];
export const focused = () => g.__focused ?? null;
export const home = () => "/home/u";
export const shared = (k) => (g.__shared ?? {})[k];
export const setShared = (k, v) => { (g.__shared ??= {})[k] = JSON.parse(JSON.stringify(v)); return null; };
export const run = (name, args) => { (g.__runs ??= []).push([name, args ?? {}]); return null; };
export const notify = (n) => { (g.__notes ??= []).push(n); return "n"; };
export const openOverlay = (o) => { (g.__overlays ??= []).push(o); return "o"; };

const key = (path, on) => `${on ?? ""}:${path}`;
const offline = (on) => on && (g.__offline ?? []).includes(on);

export const readTextFile = async (path, options = {}) => {
  const on = options?.on ?? null;
  (g.__reads ??= []).push(key(path, on));
  if (offline(on)) throw new Error(`machine ${on} is unreachable`);
  if ((g.__fail ?? []).includes(key(path, on))) throw new Error("read failed");
  const v = (g.__files ?? {})[key(path, on)];
  return v === undefined ? null : v;
};

export const writeTextFile = async (path, text, expected) => {
  const files = (g.__files ??= {}), k = key(path, null), current = files[k] ?? null;
  if (current !== expected) throw new Error("stale snapshot: the file changed");
  files[k] = text;
  (g.__writes ??= []).push(k);
  return null;
};

// store.js writes remote files with `exec("python3", ["-c", script, path, expected, text], { on })`.
export const exec = async (command, args = [], options = {}) => {
  const on = options?.on ?? null;
  if (offline(on)) throw new Error(`machine ${on} is unreachable`);
  if (command === "python3" && args[0] === "-c" && args.length === 5) {
    const [, , path, expected, text] = args, files = (g.__files ??= {}), k = key(path, on);
    if ((files[k] ?? "") !== expected) return { code: 3, stdout: "", stderr: "" };
    files[k] = text;
    (g.__writes ??= []).push(k);
    return { code: 0, stdout: "", stderr: "" };
  }
  (g.__execs ??= []).push([command, args, on]);
  return { code: 0, stdout: "", stderr: "" };
};
export const hc = async () => ({ code: 0, stdout: "", stderr: "" });

// A cx whose spawned work can be awaited: flush() settles everything spawned so far.
const pending = [];
export const cx = {
  spawn(f) { const p = Promise.resolve().then(() => f(cx)); pending.push(p); return p; },
  timer: { every() {}, after() {} },
  notify() {},
};
export async function flush() {
  for (let i = 0; i < 20; i++) {
    const batch = pending.splice(0);
    if (!batch.length) { await new Promise((r) => setTimeout(r, 0)); if (!pending.length) return; continue; }
    await Promise.allSettled(batch);
  }
}
