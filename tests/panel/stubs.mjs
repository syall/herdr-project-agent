// Stand-ins for "herdr", "gpui-kit" and "gpui-base", for rendering mods/project-agent/main.js
// under Node (tests/panel/run.mjs). The "herdr" part is tests/keeper/stubs.mjs (copied next to
// this file as keeper-stubs.mjs) plus what the panel also uses:
//   __agents   agents()          __props   props()
// Elements are plain objects; render() returns a tree that run.mjs walks for its text and for
// the click handlers of elements with an id.
//   __layout      layout()            (default {}: the panel is not in it, so it counts as shown)
// A slow remote (t-012), on the same __now clock as __delay:
//   __remoteDelay  ms                every readTextFile and exec on another machine settles once __now has moved on by ms
//   __execHang     ["<machine>:<kind>"]  exec of that kind never settles (kind: batch, evidence, stale, ls, or the command)
//   __batchFail    { "<machine>": { code, stdout } }  what the batched card read returns instead
//   __issued       [{ at, on, kind, path? }]  every readTextFile and exec, when it was called
//   __execOut      { "<machine>:<kind>": { code?, stdout } }  what an exec of that kind (evidence, stale) prints
//   __sleepHang    true: cx.sleep never settles (a read that ignores its 10 s limit)
//   __execFail     ["<machine>:<kind>"]  exec of that kind throws (a failed call)
//   __execFn       (command, args, on, options) => reply | undefined  a case's own answer for an exec (t-007b: the
//                  pa-push-main and pa-push-allow stand-ins; a reply may be a promise); recorded in __execs with its options
// The batched card read (`sh -c <script with "for f in *.json"> sh <dir>`) answers from __ls and __files:
// each listed name the card-name filter takes that has a file, in byte order, as "name\0text\0". No __ls entry: exit 3 (no folder).
// cx / makeCx(): the keeper stubs' spawn (flush() awaits it) plus sleep(ms) on the __now clock and a timer that
// records every(ms, fn) in __timers ([{ ms, fn }]) and after(ms, fn) in __afters, so a case can fire them;
// write_to_clipboard(text) appends text to __clipboard (t-026); notify() counts into __notifies (t-028).
export * from "./keeper-stubs.mjs";
import { readTextFile as baseRead, exec as baseExec, cx as baseCx } from "./keeper-stubs.mjs";
const g = globalThis;

const waitFor = async (ms) => {
  const until = g.__now + ms, gen = g.__gen;
  while (g.__now < until) { if (g.__gen !== gen) throw new Error("an old world"); await new Promise((r) => setTimeout(r, 1)); }
};
export const execKind = (command, args = []) => {
  if (command !== "sh") return command === "ls" ? "ls" : command;
  const script = String(args[1] ?? "");
  return /for f in \*\.json/.test(script) ? "batch" : /session\.log/.test(script) ? "stale" : /ls -t/.test(script) ? "evidence" : "sh";
};
// __draining: set between cases; every call fails at once, so work left over from a case ends there.
const drained = () => { if (g.__draining) throw new Error("an old world"); };
export const readTextFile = async (path, options = {}) => {
  drained();
  const on = options?.on ?? null;
  (g.__issued ??= []).push({ at: g.__now, on, kind: "read", path });
  if (on && g.__remoteDelay && !(g.__offline ?? []).includes(on)) await waitFor(g.__remoteDelay);
  return baseRead(path, options);
};
export const exec = async (command, args = [], options = {}) => {
  drained();
  const on = options?.on ?? null, kind = execKind(command, args);
  if (!(command === "python3" && args[0] === "-c")) (g.__issued ??= []).push({ at: g.__now, on, kind });
  if ((g.__execFail ?? []).includes(`${on ?? ""}:${kind}`)) throw new Error("exec failed");
  if ((g.__execHang ?? []).includes(`${on ?? ""}:${kind}`)) { (g.__execs ??= []).push([command, args, on]); return new Promise(() => {}); }
  if (on && g.__remoteDelay && !(g.__offline ?? []).includes(on)) await waitFor(g.__remoteDelay);
  const own = g.__execFn ? g.__execFn(command, args, on, options) : undefined;
  if (own !== undefined) { (g.__execs ??= []).push([command, args, on, options]); return own; }
  const canned = (g.__execOut ?? {})[`${on ?? ""}:${kind}`];
  if (canned && kind !== "batch") { if ((g.__offline ?? []).includes(on)) throw new Error(`machine ${on} is unreachable`); return { code: 0, stderr: "", ...canned }; }
  if (kind !== "batch") return baseExec(command, args, options);
  if ((g.__offline ?? []).includes(on)) throw new Error(`machine ${on} is unreachable`);
  (g.__execs ??= []).push([command, args, on]);
  const forced = (g.__batchFail ?? {})[on ?? ""];
  if (forced) return { code: forced.code ?? 1, stdout: forced.stdout ?? "", stderr: forced.stderr ?? "" };
  const dir = args[3], names = (g.__ls ?? {})[`${on ?? ""}:${dir}`];
  if (!names) return { code: 3, stdout: "", stderr: "" };
  let out = "";
  for (const n of [...names].filter((n) => /^[A-Za-z0-9_.-]+\.json$/.test(n) && !n.startsWith(".")).sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)))) {
    const text = (g.__files ?? {})[`${on ?? ""}:${dir}/${n}`];
    if (text === undefined || text === null) continue; // gone between the glob and the read
    out += `${n}\0${text}\0`;
  }
  return { code: 0, stdout: out, stderr: "" };
};
// makeCx(): a cx for one world. Its spawns run only while that world lasts (__gen unchanged), so a
// view mounted by an earlier case cannot start work in a later one.
export function makeCx(extra = {}) {
  const gen = g.__gen;
  const c = {
    spawn(f) { if (gen !== undefined && g.__gen !== gen) return Promise.resolve(); return baseCx.spawn(() => f(c)); },
    sleep: (ms) => (g.__sleepHang ? new Promise(() => {}) : waitFor(ms ?? 0)),
    timer: { every(ms, fn) { if (gen === undefined || g.__gen === gen) (g.__timers ??= []).push({ ms, fn }); }, after(ms, fn) { if (gen === undefined || g.__gen === gen) (g.__afters ??= []).push({ ms, fn }); } },
    notify() { g.__notifies = (g.__notifies ?? 0) + 1; }, // counted (t-028 A4): a redraw asked for
    write_to_clipboard(text) { (g.__clipboard ??= []).push(text); },
    ...extra,
  };
  return c;
}
export const cx = makeCx();

export const agents = () => g.__agents ?? [];
export const props = () => g.__props ?? {};
export const theme = () => new Proxy({}, { get: () => "#808080" });
export const window = () => ({ platform: "macos" });
export const layout = () => g.__layout ?? {};
export const watchFolder = () => new Promise(() => {}); // never changes
export const closeOverlay = (o) => { (g.__closed ??= []).push(o); return null; };
export const pickFolder = async () => null;

// ---- gpui-kit / gpui-base ----
class El {
  constructor(kind) { this.kind = kind; this.kids = []; this.key = null; this.click = null; }
  id(x) { this.key = x; return this; }
  on_click(fn) { this.click = fn; return this; }
  child(x) { if (x !== undefined && x !== null && x !== false) this.kids.push(x); return this; }
  children(list) { for (const x of list ?? []) this.child(x); return this; }
  when(cond, fn) { return cond ? fn(this) ?? this : this; }
}
// Any other builder method (styling, hover, scroll, ...) returns the element itself; its last
// arguments are kept in `style` (for example style.pl is [12] after .pl(12)).
const make = (kind) => new Proxy(new El(kind), {
  get(target, name, recv) {
    if (name in target) return Reflect.get(target, name, recv);
    if (typeof name === "symbol") return undefined;
    return (...args) => { (target.style ??= {})[name] = args; return recv; };
  },
});

export class View {}
export const div = () => make("div");
export const svg = () => make("svg");
export const image = () => make("image");
export const v_flex = () => make("v_flex");
export const h_flex = () => make("h_flex");
// on(event, fn) keeps the handler; fire(event) calls them as the host would, with a cx (t-028 A2: "focus").
export const InputState = { new: (o = {}) => {
  let v = o.value ?? ""; const on = {};
  return { value: () => v, set: (x) => { v = x; }, release() {}, on(event, fn) { (on[event] ??= []).push(fn); return true; },
    fire(event) { for (const fn of on[event] ?? []) fn({}, cx); }, handlers: (event) => (on[event] ?? []).length };
} };
export const Input = { new: () => make("input") };
export const ScrollHandle = { new: () => ({ scroll_to_item() {} }) };
// TextView (t-026): selectable text. Its html is kept, its text is the html without the one <span> and with
// & < > " decoded, and .selectable(v) is recorded as style.selectable. inspect() and findText() read its
// text as its wrapper's own: a row found by its text is still the styled div around it.
const unhtml = (h) => String(h).replace(/^<span>/, "").replace(/<\/span>$/, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
const textView = (id, html) => { const el = make("textview"); el.key = id; el.html = html; el.text = unhtml(html); return el; };
export const TextView = { html: textView, markdown: textView };

// Every element whose own children include the string `text`, depth first.
export function findText(root, text) {
  const found = [];
  const walk = (x) => {
    if (x === null || typeof x !== "object") return;
    if ((x.kids ?? []).some((k) => (typeof k !== "object" && String(k) === text) || (k?.kind === "textview" && k.text === text))) found.push(x);
    for (const k of x.kids ?? []) walk(k);
  };
  walk(root);
  return found;
}

// The rendered tree: its text (each element's strings, depth first) and its click targets.
export function inspect(root) {
  const text = [], clicks = {};
  const walk = (x) => {
    if (x === null || x === undefined) return;
    if (typeof x !== "object") { text.push(String(x)); return; }
    if (x.kind === "textview") text.push(x.text);
    if (x.key !== null && x.click) clicks[x.key] = x.click;
    for (const k of x.kids ?? []) walk(k);
  };
  walk(root);
  return { text: text.join(" | "), clicks };
}
