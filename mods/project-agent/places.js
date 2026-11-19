// Worker places and the start order in the Tasks tab (t-083 J2). Each kind of place with its limit and holders, the
// Project Agent's waiting list in effective order with why each job waits, and the person's ↑ ↓ ⤒ moves. Pure: the
// panel's load reads the files (store.js loadWaiting, loadStartOrder, loadRules), this file draws and computes; the
// one write (start-order.json, compare-and-write) is main.js's. limitsFrom, readOrder, effectiveOrder and allocate
// are runtime/tools/pa_places.py's limits_from, read_order, effective_order and allocate: both are checked against
// tests/samples/places-cases.json, so the panel and pa-places give the same answer. The panel does not read the
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
  "not-object": "start-order.json is not a JSON object", version: "start-order.json is not version 1",
  order: "start-order.json's order is not a list of job ids",
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

// { order, error, line } from start-order.json's text (null: no file). line: what both sides say, null when readable.
export function readOrder(text) {
  const bad = (error) => ({ order: [], error, line: error === "missing" ? "no start order yet; the Project Agent's order is used" : `start order unreadable: ${ORDER_WHY[error]}; the Project Agent's order is used` });
  if (text === null || text === undefined) return bad("missing");
  if (!String(text).trim()) return bad("empty");
  let doc;
  try { doc = JSON.parse(text); } catch { return bad("not-json"); }
  if (!obj(doc)) return bad("not-object");
  if (doc.version !== 1) return bad("version");
  if (!Array.isArray(doc.order) || !doc.order.every((i) => typeof i === "string")) return bad("order");
  return { order: doc.order, error: null, line: null, doc };
}

// The jobs in effective order (R8): queue jobs keep their index; the movable slots take the movable jobs the person's
// order names, in that order, then the others in the Project Agent's order. Unknown ids are ignored.
export function effectiveOrder(jobs, order) {
  const movable = jobs.filter((j) => j.source !== "queue"), byId = new Map(movable.map((j) => [j.id, j])), seen = new Set(), ordered = [];
  for (const i of order) if (byId.has(i) && !seen.has(i)) { seen.add(i); ordered.push(byId.get(i)); }
  ordered.push(...movable.filter((j) => !seen.has(j.id)));
  let k = 0;
  return jobs.map((j) => (j.source === "queue" ? j : ordered[k++]));
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

// The new order after a move (R6, R9): the movable ids in effective order with `id` moved; ↑/↓ swap with the nearest
// movable neighbour (fixed queue jobs are skipped over), ⤒ to the first movable slot. null: nothing to move.
export function moveOrder(effective, id, move) {
  const ids = effective.filter((j) => j.source !== "queue").map((j) => j.id), i = ids.indexOf(id);
  if (i < 0) return null;
  if (move === "up" || move === "top") {
    if (i === 0) return null;
    const [x] = ids.splice(i, 1);
    ids.splice(move === "top" ? 0 : i - 1, 0, x);
    return ids;
  }
  if (move === "down") {
    if (i === ids.length - 1) return null;
    [ids[i], ids[i + 1]] = [ids[i + 1], ids[i]];
    return ids;
  }
  return null;
}

// The start-order.json text the panel writes: the whole order, by the person, the last MOVES_KEPT moves.
export function orderText(prevText, order, { id = null, move }) {
  const prev = readOrder(prevText).doc, now = stamp();
  const moves = [...(Array.isArray(prev?.moves) ? prev.moves : []), { at: now, ...(id ? { id } : {}), move }].slice(-MOVES_KEPT);
  return `${JSON.stringify({ version: 1, updated: now, by: "person", order, moves }, null, 2)}\n`;
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
// loadRules's; isLive(address): the worker's terminal is live. shown: false with no waiting list and no holder (D3).
export function placesRows({ waiting, startOrder, rules, ledger, isLive }) {
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
  const effective = effectiveOrder(waiting.doc.jobs.map((j) => ({ ...j, source: j.source ?? "pa" })), read.order);
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
  return { ...out, state: "ok", jobs, effective, orderKnown, head: `${out.head} · ${jobs.length} waiting`,
    orderLine: !orderKnown ? "order: not read yet" : read.error === null ? "order: yours" : read.error === "missing" ? "order: the Project Agent's" : read.line,
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
