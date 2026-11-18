// The one place this mod starts a host call (t-053). Herdr's runtime gives every copy's runtime one pool of
// 1024 outstanding host tasks: each pending cx.spawn, cx.sleep, timer, readTextFile, writeTextFile, exec, hc and
// watchFolder holds a slot until it settles, and past the limit the call throws a RangeError. So:
// - read/write/run/send/watch wrap the herdr calls (counted while pending when given a Host), and a refusal of the runtime
//   becomes a plain Error with `refused: true` (a Host records it once for the panel's line: refusalSince, refusalLine);
// - Host.call gives a call a limit without a cx.sleep per call (one clock per Host, D1), a key (a call of a key
//   never starts while an older one of that key is still pending, given up or not: the caller gets BUSY), and a
//   soft budget (above SOFT_BUDGET pending, or within QUIET_MS of a refusal, a background call starts nothing and
//   answers SKIPPED; a call the runtime refuses answers REFUSED).
// TIMED_OUT, BUSY, SKIPPED and REFUSED all have `timedOut: true` (gaveUp): "did not answer", never "none".

import { readTextFile, writeTextFile, exec, hc, watchFolder } from "herdr";

export const TIMED_OUT = Object.freeze({ timedOut: true });
export const BUSY = Object.freeze({ timedOut: true, busy: true }); // a call of this key is still pending: nothing started
export const SKIPPED = Object.freeze({ timedOut: true, skipped: true }); // over the soft budget: nothing started
export const REFUSED = Object.freeze({ timedOut: true, refused: true }); // the runtime refused it
export const gaveUp = (v) => Boolean(v && typeof v === "object" && v.timedOut === true);
export const SOFT_BUDGET = 256;
export const QUIET_MS = 1000; // after a refusal no background call starts for this long (never a tight retry loop)

const LIMIT = /outstanding host task limit/;
const isLimit = (e) => e instanceof RangeError || e?.refused === true || LIMIT.test(String(e?.message ?? e));

class Refused extends Error {
  constructor(what) { super(`Too many calls waiting: ${what} was refused`); this.refused = true; }
}

// Starts one host task; `host` (a Host, or null) counts it while pending and records a refusal. A synchronous refusal
// throws Refused; a rejection with the limit's text rejects with Refused.
function counted(what, start, host = null) {
  let p;
  try { p = start(); } catch (e) { if (isLimit(e)) { host?.noteRefusal(what); throw new Refused(what); } throw e; }
  const end = host ? host.hold() : () => {};
  return Promise.resolve(p).then((v) => { end(); return v; }, (e) => { end(); if (isLimit(e)) { host?.noteRefusal(what); throw new Refused(what); } throw e; });
}
const base = (path) => String(path ?? "").split("/").pop();
// The herdr calls, for clicks and dialogs (no key, no limit of their own, never skipped): a refusal is a plain Error.
export const read = (path, options, host = null) => counted(`the ${base(path)} read`, () => readTextFile(path, options), host);
export const write = (path, text, expected, host = null) => counted(`the ${base(path)} write`, () => writeTextFile(path, text, expected), host);
export const run = (command, args, options, host = null) => counted(`the ${command} call`, () => exec(command, args, options), host);
export const send = (args, host = null) => counted("the hc send", () => hc(args), host);
// A folder watch: the FolderWatch itself (its cancel() kept), counted until it settles.
export function watch(path, host = null) {
  let w;
  try { w = watchFolder(path); } catch (e) { if (isLimit(e)) { host?.noteRefusal("the folder watch"); throw new Refused("the folder watch"); } throw e; }
  const end = host ? host.hold() : () => {};
  Promise.resolve(w).then(end, end);
  return w;
}

// The panel's one line for a refusal or a skip (R6): never the runtime's own text. shownAt: when the data shown was read.
export function refusalLine(r, shownAt) {
  const t = shownAt ? new Date(shownAt) : null;
  const hhmm = t && !Number.isNaN(t.getTime()) ? `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}` : null;
  return `Too many calls waiting (${r.n}): ${r.what} was skipped; ${hhmm ? `showing what was read at ${hhmm}` : "nothing read yet"}`;
}

// One per panel copy (its view; the keeper and the timer get the copy's). It counts the copy's own pending calls, sleeps
// and spawns that went through it (the budget is per copy: SOFT_BUDGET), keeps the keys in flight, the deadlines,
// and the last refusal or skip.
export class Host {
  constructor() {
    this.live = 0;
    this.pending = new Map(); // key -> { at, done }: a call started and not settled (given up or not)
    this.deadlines = new Set(); // { due, fire }
    this.clocking = false;
    this.refusal = null; // { at, n, what, skipped }
    this.quietUntil = 0;
  }

  hold() {
    this.live += 1;
    let done = false;
    return () => { if (!done) { done = true; this.live -= 1; } };
  }
  quiet() { return Date.now() < this.quietUntil; }
  noteRefusal(what, skipped = false) {
    const now = Date.now();
    this.refusal = { at: now, n: this.live, what, skipped };
    if (!skipped) this.quietUntil = now + QUIET_MS;
  }
  // The last refusal or skip at or after `at`, or null.
  refusalSince(at) { return this.refusal && this.refusal.at >= at ? this.refusal : null; }
  busy(key) { return this.pending.has(key); }
  // The context a task hands the store's reads: this Host, the task's spawn (for the clock), and `extra`
  // ({ origin, background, wait, progress }).
  ctx(cx, extra = {}) { return { host: this, ...(cx?.spawn ? { spawn: (f) => cx.spawn(f) } : {}), ...extra }; }

  // `start()` makes the herdr call. Answers its value, or TIMED_OUT after `limit` ms, BUSY, SKIPPED, REFUSED; a
  // rejection rejects. `cx`: the waiting task's (its spawn and sleep run the clock; without them there is no limit, as
  // before); cx.progress() is called on settle or give-up. key null: no single-flight. background false: never skipped
  // (clicks run). wait: a pending call of the key is waited for (up to `limit`), then a new one starts (a click never
  // gets BUSY). onLate(value): the call's own answer, when it came after this caller gave up on it.
  async call(cx, key, start, { limit = Infinity, what = "a call", background = true, wait = false, onLate = null } = {}) {
    if (key && this.pending.has(key)) {
      if (!wait) return BUSY;
      const first = await this.within(cx, this.pending.get(key).done, limit);
      if (first === TIMED_OUT || this.pending.has(key)) { cx?.progress?.(); return TIMED_OUT; }
    }
    if (background && (this.quiet() || this.live >= SOFT_BUDGET)) {
      if (!this.quiet()) this.noteRefusal(what, true);
      return SKIPPED;
    }
    let p;
    try { p = counted(what, start, this); } catch (e) { if (e?.refused) return REFUSED; throw e; }
    const settled = p.then((v) => ({ v }), (e) => ({ e }));
    if (key) {
      const entry = { at: Date.now(), done: settled };
      this.pending.set(key, entry);
      settled.then(() => { if (this.pending.get(key) === entry) this.pending.delete(key); });
    }
    const r = await this.within(cx, settled, limit);
    cx?.progress?.();
    if (r === TIMED_OUT) { if (onLate) settled.then((x) => { if (!x.e) onLate(x.v); }); return TIMED_OUT; }
    if (r.e) { if (r.e.refused) return REFUSED; throw r.e; }
    return r.v;
  }

  // A background task: cx.spawn(fn), counted until fn ends. false when it did not start (quiet after a refusal, or
  // refused now: recorded).
  spawn(cx, fn, { what = "a task", background = true } = {}) {
    if (background && this.quiet()) return false;
    const end = this.hold();
    try {
      cx.spawn(async (c) => { try { return await fn(c); } finally { end(); } });
      return true;
    } catch (e) {
      end();
      if (isLimit(e)) { this.noteRefusal(what); return false; }
      throw e;
    }
  }

  // `p`, or TIMED_OUT once `limit` ms have passed (swept by the clock).
  within(cx, p, limit) {
    if (!Number.isFinite(limit)) return p;
    // A caller with a sleep and no spawn (an old-style context, a test's): one sleep of its own, as before t-053.
    if (typeof cx?.spawn !== "function" && typeof cx?.sleep === "function") return Promise.race([p, cx.sleep(limit).then(() => TIMED_OUT, () => TIMED_OUT)]);
    if (typeof cx?.spawn !== "function" || this.noClock) return p;
    let d;
    const out = new Promise((resolve) => { d = { due: Date.now() + limit, fire: () => resolve(TIMED_OUT) }; });
    this.deadlines.add(d);
    this.ensureClock(cx);
    return Promise.race([p.then((v) => { this.deadlines.delete(d); return v; }), out]);
  }

  // The deadline clock (D1): one task per Host while a deadline is pending, one sleep at a time (at most 1 s), so a
  // capped call costs no slot of its own. It ends when no deadline is left.
  ensureClock(cx) {
    if (this.clocking) return;
    this.clocking = true;
    const started = this.spawn(cx, async (c) => {
      try {
        if (typeof c?.sleep !== "function") { this.noClock = true; return; } // a context with no clock (a test's): no limits
        while (this.deadlines.size) {
          let next = Infinity;
          for (const d of this.deadlines) next = Math.min(next, d.due);
          const wait = Math.min(1000, next - Date.now());
          if (wait > 0) await counted("a sleep", () => c.sleep(wait), this);
          await counted("a sleep", () => c.sleep(0), this); // one turn of the executor: a call that answered by now settles before its deadline fires
          this.sweep(Date.now());
        }
      } catch { /* refused, or the copy is gone: the next capped call starts the clock again */ }
      finally { this.clocking = false; }
    }, { what: "the deadline clock", background: false });
    if (!started) this.clocking = false;
  }

  sweep(now) {
    for (const d of [...this.deadlines]) if (d.due <= now) { this.deadlines.delete(d); d.fire(); }
  }
}
