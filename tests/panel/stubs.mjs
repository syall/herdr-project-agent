// Stand-ins for "herdr", "gpui-kit" and "gpui-base", for rendering mods/project-agent/main.js
// under Node (tests/panel/run.mjs). The "herdr" part is tests/keeper/stubs.mjs (copied next to
// this file as keeper-stubs.mjs) plus what the panel also uses:
//   __agents   agents()          __props   props()
// Elements are plain objects; render() returns a tree that run.mjs walks for its text and for
// the click handlers of elements with an id.
export * from "./keeper-stubs.mjs";
const g = globalThis;

export const agents = () => g.__agents ?? [];
export const props = () => g.__props ?? {};
export const theme = () => new Proxy({}, { get: () => "#808080" });
export const window = () => ({ platform: "macos" });
export const layout = () => ({});
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
export const InputState = { new: (o = {}) => { let v = o.value ?? ""; return { value: () => v, set: (x) => { v = x; }, release() {} }; } };
export const Input = { new: () => make("input") };
export const ScrollHandle = { new: () => ({ scroll_to_item() {} }) };

// Every element whose own children include the string `text`, depth first.
export function findText(root, text) {
  const found = [];
  const walk = (x) => {
    if (x === null || typeof x !== "object") return;
    if ((x.kids ?? []).some((k) => typeof k !== "object" && String(k) === text)) found.push(x);
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
    if (x.key !== null && x.click) clicks[x.key] = x.click;
    for (const k of x.kids ?? []) walk(k);
  };
  walk(root);
  return { text: text.join(" | "), clicks };
}
