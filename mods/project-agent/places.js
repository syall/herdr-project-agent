// Worker places and the start order in the Tasks tab (t-083 J2). Each kind of place with its limit and holders, the
// Project Agent's waiting list in effective order with why each job waits, and the person's ↑ ↓ ⤒ moves. Pure: the
// panel's load reads the files (store.js loadWaiting, loadStartOrder, loadRules), this file draws and computes; the
// one write (start-order.json, compare-and-write) is main.js's. limitsFrom, readOrder, effectiveOrder and allocate
// are runtime/tools/pa_places.py's limits_from, read_order, effective_order and allocate: both are checked against
// tests/samples/places-cases.json, so the panel and pa-places give the same answer. t-097 J2: the movable jobs follow
// their task's place in the task order once one exists (R9), and a ↑ ↓ ⤒ writes one pin for that job (version 2, R10). The panel does not read the
// journal: its reasons assume no hold (J2-D2); pa-places next decides.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { gaveUp } from "./host.js";
import { at } from "./queue.js";

export const KINDS = ["working", "no-test", "suite"];
export const DEFAULTS = { working: 4, "no-test": 0, suite: null }; // suite null: no cap beyond working
export const NOTICE = "start order changed";
export const MISSING_WAITING = "No waiting list yet: the Project Agent writes it with pa-places.";
export const MOVES_KEPT = 20;
const ORDER_WHY = {
  missing: "no start order yet", empty: "start-order.json is empty", "not-json": "start-order.json is not JSON",
  "not-object": "start-order.json is not a JSON object", version: "start-order.json is not version 1 or 2",
  order: "start-order.json's order is not a list of job ids", pins: "start-order.json's pins are not a list of {id, slot}",
};
const FULL = { working: "working", "no-test": "no-test extra", suite: "suite" }; // the place a skipped job waits for
const KIND = { working: "working", "no-test": "no-test", suite: "suite step" }; // a job's or holder's kind, as drawn

const obj = (x) => Boolean(x) && typeof x === "object" && !Array.isArray(x);
const whole = (v) => Number.isInteger(v) && v >= 0; // true, 1.5, "4" and -1 are not
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const stamp = () => new Date(Date.now()).toISOString().replace(/\.\d+Z$/, "Z");

// { limits, sources, notes } from a rules.json document: per kind, the active rule with the highest order that names
// it with a whole number >= 0; else the default. Bad values and unknown kinds are named, once each.
export function limitsFrom(doc) {
  const limits = { ...DEFAULTS }, sources = Object.fromEntries(KINDS.map((k) => [k, "default"])), notes = [], taken = new Set();
  const rules = obj(doc) && Array.isArray(doc.rules) ? doc.rules.filter((r) => obj(r) && r.status === "active") : [];
  for (const r of [...rules].sort((a, b) => num(b.order) - num(a.order))) { // stable: equal orders keep the file's order
    if (!("places" in r)) continue;
    if (!obj(r.places)) { notes.push(`${r.id}: places is not an object; ignored`); continue; }
    for (const [kind, value] of Object.entries(r.places)) {
      if (!KINDS.includes(kind)) notes.push(`${r.id}: places.${kind} is not a known kind; ignored`);
      else if (!whole(value)) notes.push(`${r.id}: places.${kind} is not a whole number >= 0; ignored`);
      else if (!taken.has(kind)) { taken.add(kind); limits[kind] = value; sources[kind] = r.id; }
    }
  }
  return { limits, sources, notes };
}

// { start, error, line, doc } from start-order.json's text (null: no file). start, when readable: { version: 1,
// order } (t-083's whole list) or { version: 2, pins: [{ id, slot }] } (t-097 R10); null otherwise. line: what both
// sides say, null when readable.
export function readOrder(text) {
  const bad = (error) => ({ start: null, error, line: error === "missing" ? "no start order yet; the Project Agent's order is used" : `start order unreadable: ${ORDER_WHY[error]}; the Project Agent's order is used`, doc: null });
  if (text === null || text === undefined) return bad("missing");
  if (!String(text).trim()) return bad("empty");
  let doc;
  try { doc = JSON.parse(text); } catch { return bad("not-json"); }
  if (!obj(doc)) return bad("not-object");
  if (doc.version !== 1 && doc.version !== 2) return bad("version");
  if (doc.version === 1) {
    if (!Array.isArray(doc.order) || !doc.order.every((i) => typeof i === "string")) return bad("order");
    return { start: { version: 1, order: doc.order }, error: null, line: null, doc };
  }
  if (!Array.isArray(doc.pins) || !doc.pins.every((p) => obj(p) && typeof p.id === "string" && whole(p.slot))) return bad("pins");
  return { start: { version: 2, pins: doc.pins.map((p) => ({ id: p.id, slot: p.slot })) }, error: null, line: null, doc };
}

const jobTask = (j) => (typeof j.task === "string" ? j.task : String(j.id).split("/")[0]);
// A start order's pins over the waiting movable ids: a version 1 file's ids as pins 0, 1, 2 ... (deduplicated, the
// ones waiting), a version 2 file's pins as they are.
function pinsOf(start, waiting) {
  if (start?.version === 1) { const named = []; for (const i of start.order) if (waiting.has(i) && !named.includes(i)) named.push(i); return named.map((id, slot) => ({ id, slot })); }
  return Array.isArray(start?.pins) ? start.pins : [];
}

// The jobs in effective order (t-097 R9, R10; pa_places.effective_order). Queue jobs keep their index. The movable
// jobs are sorted by their task's place in `rank` (ranked task ids, top first; one task's jobs and the jobs of
// unranked tasks keep their added order, unranked after); then the start order's pins apply oldest first, each taking
// its job out and putting it at `slot` among the movable jobs (clamped to the end; a pin whose job is not a waiting
// movable job is ignored). start: readOrder's (null: none).
export function effectiveOrder(jobs, start, rank = null) {
  let movable = jobs.filter((j) => j.source !== "queue");
  if (rank && rank.length) {
    const place = new Map();
    rank.forEach((id, k) => { if (!place.has(id)) place.set(id, k); });
    movable = movable.map((j, k) => [j, k]).sort((a, b) => (place.get(jobTask(a[0])) ?? place.size) - (place.get(jobTask(b[0])) ?? place.size) || a[1] - b[1]).map(([j]) => j);
  }
  for (const { id, slot } of pinsOf(start, new Set(movable.map((j) => j.id)))) {
    const at = movable.findIndex((j) => j.id === id);
    if (at < 0) continue;
    const [j] = movable.splice(at, 1);
    movable.splice(Math.min(slot, movable.length), 0, j);
  }
  let k = 0;
  return jobs.map((j) => (j.source === "queue" ? j : movable[k++]));
}

// [b, n, s]: working-type holders (working, suite, not recorded), no-test holders, suite holders.
const counts = (places) => { const n = places.filter((p) => p === "no-test").length; return [places.length - n, n, places.filter((p) => p === "suite").length]; };
// The working places in use as drawn: working-type holders, then no-test holders while a working place is free.
const workingUsed = (b, n, w) => b + Math.min(n, Math.max(0, w - b));

// One reason per job, in the given (effective) order: { id, action: start|skip|blocked|hold, place, full }. Pooled
// seats: a working or suite job needs b < working and b + n < working + no-test; a suite job also s < suite (when
// capped); a no-test job b + n < working + no-test. A job marked start counts for the ones after it.
export function allocate(jobs, limits, holders, hold) {
  const w = limits.working, x = limits["no-test"], cap = limits.suite;
  let [b, n, s] = counts(holders);
  return jobs.map((j) => {
    const r = { id: j.id, action: null, place: null, full: null };
    if (hold !== "off") return { ...r, action: "hold" };
    if (j.blockedBy) return { ...r, action: "blocked" };
    if (j.kind === "no-test") {
      if (b + n < w + x) { const place = b + n < w ? "working" : "no-test"; n++; return { ...r, action: "start", place }; }
      return { ...r, action: "skip", full: x ? { kind: "no-test", used: Math.max(0, b + n - w), limit: x } : { kind: "working", used: workingUsed(b, n, w), limit: w } };
    }
    if (j.kind === "suite" && cap !== null && s >= cap) return { ...r, action: "skip", full: { kind: "suite", used: s, limit: cap } };
    if (b >= w || b + n >= w + x) return { ...r, action: "skip", full: { kind: "working", used: workingUsed(b, n, w), limit: w } };
    b++; if (j.kind === "suite") s++;
    return { ...r, action: "start", place: j.kind };
  });
}

// The live unfinished workers of the ledger (R1, R4). isLive(address): its terminal is live.
export function holdersFrom(ledger, isLive) {
  const out = [];
  for (const t of Array.isArray(ledger?.tasks) ? ledger.tasks : []) {
    for (const w of obj(t) && Array.isArray(t.workers) ? t.workers : []) {
      if (!obj(w) || w.finished || !w.address || !isLive(String(w.address))) continue;
      const recorded = KINDS.includes(w.place);
      out.push({ address: String(w.address), task: t.id, role: w.role, name: w.name, started: w.started, place: recorded ? w.place : null, recorded });
    }
  }
  return out;
}

// The pin a move gives (R9, t-097 R10): the job's new position among the movable jobs in effective order; ↑/↓ swap
// with the nearest movable neighbour (fixed queue jobs are skipped over), ⤒ to the first movable slot. null: nothing
// to move.
export function moveOrder(effective, id, move) {
  const ids = effective.filter((j) => j.source !== "queue").map((j) => j.id), i = ids.indexOf(id);
  if (i < 0) return null;
  if (move === "up" || move === "top") return i === 0 ? null : { id, slot: move === "top" ? 0 : i - 1 };
  if (move === "down") return i === ids.length - 1 ? null : { id, slot: i + 1 };
  return null;
}

// The pins the panel writes after a move (R10): the start order's pins (a version 1 file's waiting ids as pins 0, 1,
// 2 ...), less the pins of jobs not waiting and the job's own earlier pin, then the job's new pin. When pins interact
// so that this would put the jobs elsewhere than the move drawn, every movable job is pinned in the drawn order
// (J2-D5). "reset": no pins (the task order alone). null: a no-op.
export function pinsAfter({ jobs, start, rank = null, id, move }) {
  if (move === "reset") return [];
  const effective = effectiveOrder(jobs, start, rank), pin = moveOrder(effective, id, move);
  if (!pin) return null;
  const want = effective.filter((j) => j.source !== "queue").map((j) => j.id);
  want.splice(pin.slot, 0, ...want.splice(want.indexOf(id), 1));
  const waiting = new Set(want);
  const pins = [...pinsOf(start, waiting).filter((p) => waiting.has(p.id) && p.id !== id).map((p) => ({ id: p.id, slot: p.slot })), pin];
  const got = effectiveOrder(jobs, { version: 2, pins }, rank).filter((j) => j.source !== "queue").map((j) => j.id);
  return got.every((x, k) => x === want[k]) ? pins : want.map((x, slot) => ({ id: x, slot }));
}

// The start-order.json text the panel writes (R10): version 2, the pins, by the person, the last MOVES_KEPT moves.
export function orderText(prevText, pins, { id = null, move }) {
  const prev = readOrder(prevText).doc, now = stamp();
  const moves = [...(Array.isArray(prev?.moves) ? prev.moves : []), { at: now, ...(id ? { id } : {}), move }].slice(-MOVES_KEPT);
  return `${JSON.stringify({ version: 2, updated: now, by: "person", pins, moves }, null, 2)}\n`;
}

// "t-020-verify" of task t-020 reads "verify" (J2-D1); any other name as it is.
const jobWord = (task, name) => { const s = String(name ?? ""); return task && s.startsWith(`${task}-`) ? s.slice(String(task).length + 1) : s; };
function holderLine(h, inSuite = false) {
  const what = `${h.address} · ${h.task ?? "?"} ${jobWord(h.task, h.name)}`.trimEnd();
  const mark = !h.recorded ? ["kind not recorded"] : h.place === "suite" && !inSuite ? ["suite step"] : [];
  return [what, h.role ?? "?", ...mark, `since ${h.started ? at(h.started) : "?"}`].join(" · ");
}
const holderKey = (h) => `holder:${h.address}`, jobKey = (j) => `job:${j.id}`;

// The limits the load's rules give: loadRules's result (null or a give-up: the defaults, said once).
function limitsOf(rules) {
  if (!rules || gaveUp(rules)) return { ...limitsFrom({}), notes: [] };
  if (rules.error) return { ...limitsFrom({}), notes: [`${rules.error}; the default limits are used`] };
  return limitsFrom(rules.doc ?? { rules: rules.rules });
}

// What the section draws (pure). waiting, startOrder: the load's results (null or a give-up: not known yet); rules:
// loadRules's; isLive(address): the worker's terminal is live; rank: the ranked task ids once a task order exists
// (t-097 R9, J1-D10), else null (the added order). shown: false with no waiting list and no holder (D3).
export function placesRows({ waiting, startOrder, rules, ledger, isLive, rank = null }) {
  const holders = holdersFrom(ledger, isLive);
  const { limits, sources, notes } = limitsOf(rules);
  const w = limits.working, x = limits["no-test"], cap = limits.suite;
  const [b, n, s] = counts(holders.map((h) => h.place)), used = workingUsed(b, n, w);
  const inWorking = holders.filter((h) => h.place !== "no-test"), noTest = holders.filter((h) => h.place === "no-test");
  const seated = [...inWorking, ...noTest.slice(0, used - b)], extra = noTest.slice(used - b);
  const line = (h, inSuite) => ({ key: holderKey(h), text: holderLine(h, inSuite) });
  const kinds = [{ kind: "working", head: `Working · ${used} of ${w}${sources.working === "default" ? " (default)" : ""}`, over: used > w, holders: seated.map((h) => line(h)) }];
  if (x || extra.length) kinds.push({ kind: "no-test", head: `No-test extra · ${extra.length} of ${x}`, over: extra.length > x, holders: extra.map((h) => line(h)) });
  if (cap !== null) kinds.push({ kind: "suite", head: `Suite step · ${s} of ${cap}  (held within Working)`, over: s > cap, holders: holders.filter((h) => h.place === "suite").map((h) => line(h, true)) });
  const named = [...new Set(KINDS.map((k) => sources[k]).filter((v) => v !== "default"))];
  const out = { shown: false, state: "loading", kinds, notes, limitsLine: `limits: ${named.length ? named.join(", ") : "default"}`, jobs: [], badges: {}, effective: [], orderKnown: false,
    orderLine: null, reset: false, orderTextRead: null, waitingLine: null, head: `Places · ${used} of ${w} working` };
  const badge = (task, b) => { (out.badges[task] ??= []).push(b); };
  for (const h of holders) badge(h.task, { key: holderKey(h), text: `running · ${h.recorded ? KIND[h.place] : "working"}` });
  const known = waiting && !gaveUp(waiting);
  out.shown = holders.length > 0 || Boolean(known && !waiting.missing);
  if (!known) return out;
  if (waiting.missing) return { ...out, state: "missing", waitingLine: MISSING_WAITING };
  if (waiting.error) return { ...out, state: "error", waitingLine: waiting.error };
  const orderKnown = Boolean(startOrder && !gaveUp(startOrder));
  const read = readOrder(orderKnown && !startOrder.missing ? startOrder.text : null);
  const jobsIn = waiting.doc.jobs.map((j) => ({ ...j, source: j.source ?? "pa" }));
  const effective = effectiveOrder(jobsIn, read.start, rank);
  const reasons = allocate(effective, limits, holders.map((h) => h.place), "off");
  const movable = effective.filter((j) => j.source !== "queue").map((j) => j.id);
  const jobs = effective.map((j, i) => {
    const r = reasons[i], fixed = j.source === "queue", k = movable.indexOf(j.id);
    const worker = holders.find((h) => h.task === j.task && (h.name === j.job || h.name === `${j.task}-${j.job}`));
    const reason = worker ? `started? its worker ${worker.address} holds a place`
      : r.action === "blocked" ? `blocked: ${j.blockedBy}`
      : r.action === "skip" ? `waits for a ${FULL[r.full.kind]} place (${r.full.used} of ${r.full.limit})`
      : r.place === "no-test" ? "next to start: the no-test extra place is free" : "next to start";
    badge(j.task, { key: jobKey(j), text: `next #${i + 1} · ${KIND[j.kind] ?? j.kind}` });
    return { key: jobKey(j), id: j.id, label: `${i + 1}. ${j.task ?? "?"} ${j.job ?? ""} · ${j.role ?? "?"} · ${KIND[j.kind] ?? j.kind}`, reason, fixed,
      note: fixed ? "merge queue order (Queue tab)" : null, up: orderKnown && !fixed && k > 0, top: orderKnown && !fixed && k > 0, down: orderKnown && !fixed && k >= 0 && k < movable.length - 1 };
  });
  return { ...out, state: "ok", jobs, effective, jobsIn, start: read.start, rank, orderKnown, head: `${out.head} · ${jobs.length} waiting`,
    orderLine: !orderKnown ? "order: not read yet" : read.error === null && !(read.start.version === 2 && !read.start.pins.length) ? "order: yours"
      : read.error === null || read.error === "missing" ? (rank ? "order: the task order" : "order: the Project Agent's") : read.line, // J2-D6: no pins, the task order alone
    reset: orderKnown && read.error !== null && read.error !== "missing", orderTextRead: orderKnown && !startOrder.missing ? startOrder.text : null };
}

// Draws the section (R1, R9). o: { folded, mark (a line key or null), busy, error, asOf, onFold, onMove(id, move, cx), onReset(cx) }
// (each handler gets its click's cx: a cx is valid only for the call that gave it).
export function renderPlaces(t, rows, o) {
  const line = (text, color) => div().text_size(11.5).text_color(color ?? t.muted).child(text);
  const btn = (id, label, on) => div().id(id).flex_none().px(6).h(20).flex().items_center().rounded(5).border(1).border_color(t.border).text_size(11.5)
    .text_color(o.busy ? t.dim : t.foreground).when(!o.busy, (b) => b.cursor_pointer().hover((s) => s.bg(t.hover))).on_click(on).child(label);
  const marked = (el, key) => el.when(o.mark === key, (x) => x.bg(t.selected).rounded(4));
  const box = v_flex().id("pa-places").flex_none().p(10).gap(4).rounded(7).border(1).border_color(t.border).bg(t.surface)
    .child(h_flex().id("places-head").gap(8).items_center().cursor_pointer().on_click(o.onFold)
      .child(div().flex_none().text_size(11).text_color(t.dim).child(o.folded ? "▸" : "▾"))
      .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_semibold().child(rows.head))
      .child(div().flex_none().text_size(11).text_color(t.dim).child(rows.limitsLine)));
  if (o.folded) return box;
  return box
    .children(rows.notes.map((x) => line(x, t.blocked)))
    .children(rows.kinds.flatMap((k) => [
      div().pt(2).text_size(12).text_color(k.over ? t.blocked : t.foreground).child(k.head),
      ...(k.holders.length ? k.holders.map((h) => marked(div().pl(12), h.key).child(line(h.text))) : [div().pl(12).child(line("free", t.dim))]),
    ]))
    .when(Boolean(o.error), (el) => el.child(line(o.error ?? "", t.blocked)))
    .when(rows.state === "loading", (el) => el.child(line("Loading waiting list…", t.dim)))
    .when(rows.state === "missing" || rows.state === "error", (el) => el.child(line(rows.waitingLine ?? "", rows.state === "error" ? t.blocked : t.dim)))
    .when(rows.state === "ok", (el) => el
      .child(h_flex().pt(6).gap(8).items_center()
        .child(div().flex_1().text_size(12).font_semibold().child(`Waiting · ${rows.jobs.length}`))
        .child(div().flex_none().text_size(11).text_color(t.dim).child(`as of ${o.asOf} · ${rows.orderLine?.startsWith("start order unreadable") ? "order: the Project Agent's" : rows.orderLine} · pa-places next decides (holds, live terminals)`)))
      .when(rows.reset, (x) => x.child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).child(line(rows.orderLine ?? "", t.blocked)))
        .child(btn("places-reset", "Reset order", (_e, cx) => o.onReset(cx)))))
      .when(!rows.jobs.length, (x) => x.child(line("Nothing waiting.", t.dim)))
      .children(rows.jobs.map((j) => marked(v_flex().gap(1).py(1), j.key)
        .child(h_flex().gap(6).items_center()
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.foreground).child(j.label))
          .when(Boolean(j.note), (x) => x.child(div().flex_none().text_size(11).text_color(t.dim).child(j.note ?? "")))
          .when(j.top, (x) => x.child(btn(`place-top-${j.id}`, "⤒", (_e, cx) => o.onMove(j.id, "top", cx))))
          .when(j.up, (x) => x.child(btn(`place-up-${j.id}`, "↑", (_e, cx) => o.onMove(j.id, "up", cx))))
          .when(j.down, (x) => x.child(btn(`place-down-${j.id}`, "↓", (_e, cx) => o.onMove(j.id, "down", cx)))))
        .child(div().pl(12).child(line(j.reason, t.dim))))));
}
