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
import { at, slotBox, taskLink } from "./queue.js"; // t-156: the Waiting list's jobs in the Queue's slots
import { workerItem } from "./tasks.js"; // t-142: a worker line is the task card's worker item

export const KINDS = ["working", "no-test", "suite"];
export const DEFAULTS = { working: 4, "no-test": 0, suite: null }; // suite null: no cap beyond working
export const NOTICE = "start order changed";
export const MISSING_WAITING = "No waiting list yet: the Project Agent writes it with pa-places.";
export const RANK_WAIT = "moves wait for the task order to be read"; // t-120: Places moves while task-order.json is not known
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

// The pins the panel writes after a move (R10): the start order's pins, less the pins of jobs not waiting and the
// job's own earlier pin, then the job's new pin. When pins interact so that this would put the jobs elsewhere than
// the move drawn (J2-D5), or the file is version 1 (a whole list, not moves: its order is what is drawn), only the
// pins the drawn order needs are written: from none, at each first slot where the order differs from the drawn one,
// a pin for the job drawn there (t-120, t-097 REVIEW J2 RL-1), so a later task move still reorders the others.
// "reset": no pins (the task order alone). null: a no-op.
export function pinsAfter({ jobs, start, rank = null, id, move }) {
  if (move === "reset") return [];
  const effective = effectiveOrder(jobs, start, rank), pin = moveOrder(effective, id, move);
  if (!pin) return null;
  const want = effective.filter((j) => j.source !== "queue").map((j) => j.id);
  want.splice(pin.slot, 0, ...want.splice(want.indexOf(id), 1));
  const waiting = new Set(want);
  const drawn = (pins) => { const got = effectiveOrder(jobs, { version: 2, pins }, rank).filter((j) => j.source !== "queue").map((j) => j.id); return got.findIndex((x, k) => x !== want[k]); };
  if (start?.version !== 1) {
    const pins = [...pinsOf(start, waiting).filter((p) => waiting.has(p.id) && p.id !== id).map((p) => ({ id: p.id, slot: p.slot })), pin];
    if (drawn(pins) < 0) return pins;
  }
  // each pin, applied last, fixes its slot and leaves the slots above it as they are: at most one pin per job
  const pins = [];
  for (let k = drawn(pins); k >= 0 && pins.length < want.length; k = drawn(pins)) pins.push({ id: want[k], slot: k });
  return pins;
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

// The limits the load's rules give: loadRules's result (null or a give-up: the defaults, said once). A file that only
// fails the Rules view's checks (rules.doc kept) gives the limits pa-places reads from it (t-133, t-083 J2 RL1).
function limitsOf(rules) {
  if (!rules || gaveUp(rules)) return { ...limitsFrom({}), notes: [] };
  if (rules.error && rules.doc && Array.isArray(rules.doc.rules)) {
    const got = limitsFrom(rules.doc);
    return { ...got, notes: [`${rules.error}; the limits are read as pa-places reads them`, ...got.notes] };
  }
  if (rules.error) return { ...limitsFrom({}), notes: [`${rules.error}; the default limits are used`] };
  return limitsFrom(rules.doc ?? { rules: rules.rules });
}

// The suite machines' slots (t-119 R4-R6), from loadMachines's answers as main.js keeps them: m = { doc, error, at }
// (doc: the last good answer, error: the last read's, at: when doc was read) or null (not answered yet). isLive(address):
// the worker's terminal is live; here: the label of the tool's own machine ("this Mac", or the space's machine). Each
// holder line: { key, id, worker, live, status, text }; id and worker only when its worker's terminal is live (a link, R5). shown:
// a configured machine or a slot holder (R6). t-155: the text is the one-line worker item, as the other Places rows
// (worker, task and job, role, since the slot's start), its job and role from the ledger worker of the holder's task
// with that address, live or not; the holder's command (`what`) is never shown. t-123 (t-119 REVIEW R1): this Mac's
// slots are `@local` in ids and keys, a word no machine name can be (it starts with a letter or digit, t-103 R11), so a
// suite machine named `local` never shares an id or a key with them.
const LOCAL = "@local";
const SLOT_ID = (place, i) => `places-slot-${place}-${i}`;
function ledgerWorker(ledger, task, address) {
  const t = (Array.isArray(ledger?.tasks) ? ledger.tasks : []).find((x) => obj(x) && x.id === task);
  const ws = (obj(t) && Array.isArray(t.workers) ? t.workers : []).filter((w) => obj(w) && String(w.address ?? "") === address);
  return ws.filter((w) => !w.finished).pop() ?? ws.pop() ?? null;
}
function slotLines(place, holders, isLive, statusOf, ledger) {
  return (Array.isArray(holders) ? holders : []).filter(obj).map((h, i) => {
    const worker = typeof h.worker === "string" && h.worker ? h.worker : null, live = Boolean(worker && isLive(worker));
    const task = h.task ?? "?", since = `since ${h.start ? at(h.start) : "?"}`, w = worker && ledgerWorker(ledger, h.task, worker);
    const text = worker ? [`${worker} · ${task} ${w ? jobWord(h.task, w.name) : ""}`.trimEnd(), (w && w.role) ?? "?", since].join(" · ")
      : [task, "no worker recorded", since].join(" · ");
    return { key: `slot:${place}:${i}`, id: live ? SLOT_ID(place, i) : null, worker: live ? worker : null, live, status: live ? statusOf(worker) : null, text: live ? `${text} ↗` : text };
  });
}
const slotHead = (name, cap, n) => `${name} · ${n} of ${cap ?? "?"} slots held`;
export function machinesRows(m, { isLive = () => false, statusOf = () => null, here = "this Mac", ledger = null } = {}) {
  const out = { state: "checking", line: null, error: m?.error ?? null, asOf: null, notes: [], places: [], shown: false };
  const doc = m?.doc;
  if (!doc) return { ...out, state: m?.error ? "error" : "checking" };
  out.state = "ok"; out.asOf = m.at ? at(new Date(m.at).toISOString()).slice(6) : null;
  out.notes = (Array.isArray(doc.notes) ? doc.notes : []).map(String);
  for (const x of doc.machines.filter(obj)) {
    const name = String(x.machine ?? "?"), holders = slotLines(name, x.holders, isLive, statusOf, ledger);
    const state = x.reachable ? "reachable" : `not reachable${x.reason ? ` (${x.reason})` : ""}`;
    out.places.push({ name, head: `${name} · ${state} · ${holders.length} of ${x.cap ?? "?"} slots held`, over: !x.reachable, holders });
  }
  const local = slotLines(LOCAL, doc.local?.holders, isLive, statusOf, ledger);
  out.places.push({ name: "local", head: slotHead(here, doc.local?.cap, local.length), over: false, holders: local });
  out.shown = doc.machines.length > 0 || out.places.some((p) => p.holders.length > 0);
  return out;
}

// What the section draws (pure). waiting, startOrder: the load's results (null or a give-up: not known yet); rules:
// loadRules's; isLive(address): the worker's terminal is live; statusOf(address): that terminal's status (t-142); rank: the ranked task ids once a task order exists
// (t-097 R9, J1-D10), else null (the added order); machines, here: machinesRows's (t-119). shown: false with no
// waiting list, no holder and no suite machine or slot holder (D3, t-119 R6).
export function placesRows({ waiting, startOrder, rules, ledger, isLive, statusOf = () => null, rank = null, rankKnown = true, machines = null, here = "this Mac" }) {
  const holders = holdersFrom(ledger, isLive);
  const { limits, sources, notes } = limitsOf(rules);
  const w = limits.working, x = limits["no-test"], cap = limits.suite;
  const [b, n, s] = counts(holders.map((h) => h.place)), used = workingUsed(b, n, w);
  const inWorking = holders.filter((h) => h.place !== "no-test"), noTest = holders.filter((h) => h.place === "no-test");
  const seated = [...inWorking, ...noTest.slice(0, used - b)], extra = noTest.slice(used - b);
  const line = (h, inSuite) => ({ key: holderKey(h), address: h.address, status: statusOf(h.address), text: holderLine(h, inSuite) });
  const kinds = [{ kind: "working", head: `Working · ${used} of ${w}${sources.working === "default" ? " (default)" : ""}`, over: used > w, holders: seated.map((h) => line(h)) }];
  if (x || extra.length) kinds.push({ kind: "no-test", head: `No-test extra · ${extra.length} of ${x}`, over: extra.length > x, holders: extra.map((h) => line(h)) });
  if (cap !== null) kinds.push({ kind: "suite", head: `Suite step · ${s} of ${cap}  (held within Working)`, over: s > cap, holders: holders.filter((h) => h.place === "suite").map((h) => line(h, true)) });
  const named = [...new Set(KINDS.map((k) => sources[k]).filter((v) => v !== "default"))];
  // t-156 (d-272): the rule ids link to their rules when the Rules view lists them (rules.json read without an error)
  const ruleLinks = Boolean(rules && !gaveUp(rules) && !rules.error) && named.length > 0;
  const tasksKnown = new Set((Array.isArray(ledger?.tasks) ? ledger.tasks : []).filter((x) => obj(x) && typeof x.id === "string").map((x) => x.id));
  const slots = machinesRows(machines, { isLive, statusOf, here, ledger }); // t-119: the suite machines' slots
  const out = { shown: false, state: "loading", kinds, slots, notes, limitsLine: `limits: ${named.length ? named.join(", ") : "default"}`, limitRules: ruleLinks ? named : [], jobs: [], badges: {}, effective: [], orderKnown: false,
    orderLine: null, reset: false, orderTextRead: null, waitingLine: null, head: `Places · ${used} of ${w} working` };
  const badge = (task, b) => { (out.badges[task] ??= []).push(b); };
  for (const h of holders) badge(h.task, { key: holderKey(h), text: `running · ${h.recorded ? KIND[h.place] : "working"}` });
  const known = waiting && !gaveUp(waiting);
  out.shown = holders.length > 0 || Boolean(known && !waiting.missing) || slots.shown;
  if (!known) return out;
  if (waiting.missing) return { ...out, state: "missing", waitingLine: MISSING_WAITING };
  if (waiting.error) return { ...out, state: "error", waitingLine: waiting.error };
  const orderKnown = Boolean(startOrder && !gaveUp(startOrder));
  const movesOn = orderKnown && rankKnown; // t-120 (t-097 REVIEW J2 RL-2): a slot counted without the rank could land elsewhere
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
    // t-156: no number; the slot's head is the task id (a link when the ledger has it), the job, role · kind
    return { key: jobKey(j), id: j.id, task: String(j.task ?? "?"), job: j.job ? String(j.job) : null, linked: tasksKnown.has(j.task), meta: `${j.role ?? "?"} · ${KIND[j.kind] ?? j.kind}`, reason, fixed,
      note: fixed ? "merge queue order (Queue tab)" : null, up: movesOn && !fixed && k > 0, top: movesOn && !fixed && k > 0, down: movesOn && !fixed && k >= 0 && k < movable.length - 1 };
  });
  return { ...out, state: "ok", jobs, effective, jobsIn, start: read.start, rank, orderKnown, movesOn, rankLine: rankKnown ? null : RANK_WAIT, head: `${out.head} · ${jobs.length} waiting`,
    orderLine: !orderKnown ? "order: not read yet" : read.error === null && !(read.start.version === 2 && !read.start.pins.length) ? "order: yours"
      : read.error === null || read.error === "missing" ? (rank ? "order: the task order" : "order: the Project Agent's") : read.line, // J2-D6: no pins, the task order alone
    reset: orderKnown && read.error !== null && read.error !== "missing", orderTextRead: orderKnown && !startOrder.missing ? startOrder.text : null };
}

// t-119 R4, R5: the Suite machines block's elements (read only: a holder with a live worker opens it, nothing else).
function slotsBlock(t, s, o, line, marked, items) {
  if (!s) return [];
  const head = div().pt(6).text_size(12).font_semibold().text_color(t.foreground);
  if (s.state === "checking") return [head.child("Suite machines: checking…")];
  const failed = s.error ? [line(`Suite machines: could not read them (${s.error})`, t.blocked)] : [];
  if (s.state === "error") return failed;
  return [
    h_flex().pt(6).gap(8).items_center()
      .child(div().flex_1().text_size(12).font_semibold().text_color(t.foreground).child("Suite machines"))
      .child(div().flex_none().text_size(11).text_color(t.dim).child(`${s.asOf ? `as of ${s.asOf} · ` : ""}pa-full-suite --places`)),
    ...failed,
    ...s.notes.map((x) => line(x, t.blocked)),
    ...s.places.flatMap((p) => [
      div().pl(4).text_size(12).text_color(p.over ? t.blocked : t.foreground).child(p.head),
      ...(p.holders.length ? [items(p.holders.map((h) => marked(workerItem(t, { id: h.id, text: h.text, live: h.live, status: h.status, onClick: h.id ? () => o.onWorker?.(h.worker) : null }), h.key)))]
        : [div().pl(12).child(line("free", t.dim))]),
    ]),
  ];
}

// Draws the section (R1, R9). o: { folded, mark (a line key or null), busy, error, asOf, onFold, onMove(id, move, cx), onReset(cx),
// onWorker(address), onTask(task, cx) (t-156: a job's task id), onRule(id, cx) (t-156: a limits rule id) } (each handler gets its click's cx: a cx is valid only for the call that gave it). t-084 R5: a
// holder line opens its worker in the Agents view (a holder's terminal is live: holdersFrom).

export function renderPlaces(t, rows, o) {
  const line = (text, color) => div().text_size(11.5).text_color(color ?? t.muted).child(text);
  const btn = (id, label, on) => div().id(id).flex_none().px(6).h(20).flex().items_center().rounded(5).border(1).border_color(t.border).text_size(11.5)
    .text_color(o.busy ? t.dim : t.foreground).when(!o.busy, (b) => b.cursor_pointer().hover((s) => s.bg(t.hover))).on_click(on).child(label);
  const marked = (el, key) => el.when(o.mark === key, (x) => x.bg(t.selected).rounded(4));
  // t-142: the worker lines, one worker item each (its own rounding kept when marked)
  const items = (list) => v_flex().pl(12).gap(4).items_start().children(list), markedItem = (el, key) => el.when(o.mark === key, (x) => x.bg(t.selected));
  const box = v_flex().id("pa-places").flex_none().p(10).gap(4).rounded(7).border(1).border_color(t.border).bg(t.surface)
    .child(h_flex().id("places-head").gap(8).items_center().cursor_pointer().on_click(o.onFold)
      .child(div().flex_none().text_size(11).text_color(t.dim).child(o.folded ? "▸" : "▾"))
      .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_semibold().child(rows.head))
      .child(limitsEl(t, rows, o)));
  if (o.folded) return box;
  return box
    .children(rows.notes.map((x) => line(x, t.blocked)))
    .children(rows.kinds.flatMap((k) => [
      div().pt(2).text_size(12).text_color(k.over ? t.blocked : t.foreground).child(k.head),
      ...(k.holders.length ? [items(k.holders.map((h) => markedItem(workerItem(t, { id: `places-holder-${h.address}`, text: `${h.text} ↗`, live: true, status: h.status, onClick: () => o.onWorker?.(h.address) }), h.key)))]
        : [div().pl(12).child(line("free", t.dim))]),
    ]))
    .children(slotsBlock(t, rows.slots, o, line, markedItem, items))
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
      .when(Boolean(rows.rankLine) && rows.jobs.length > 1, (x) => x.child(line(rows.rankLine ?? "", t.dim))) // t-120
      .when(!rows.jobs.length, (x) => x.child(line("Nothing waiting.", t.dim)))
      .children(rows.jobs.map((j) => marked(slotBox(t, `place-slot-${j.id}`), j.key) // t-156: the Queue's slot, no number
        .child(h_flex().gap(8).items_center()
          .child(taskLink(t, { id: `places-task-${j.id}`, task: j.task, linked: j.linked, onTask: o.onTask }))
          .when(Boolean(j.job), (x) => x.child(div().flex_none().text_size(12).text_color(t.muted).child(j.job ?? "")))
          .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.muted).child(j.meta))
          .when(Boolean(j.note), (x) => x.child(div().flex_none().text_size(11).text_color(t.dim).child(j.note ?? "")))
          .when(j.top, (x) => x.child(btn(`place-top-${j.id}`, "⤒", (_e, cx) => o.onMove(j.id, "top", cx))))
          .when(j.up, (x) => x.child(btn(`place-up-${j.id}`, "↑", (_e, cx) => o.onMove(j.id, "up", cx))))
          .when(j.down, (x) => x.child(btn(`place-down-${j.id}`, "↓", (_e, cx) => o.onMove(j.id, "down", cx)))))
        .child(line(j.reason, t.dim)))));
}

// t-156 (d-272): "limits: r-a, r-b" with each rule id a link to its rule (the click stopped there: the head folds);
// "limits: default", or rules the Rules view does not list, as plain text.
function limitsEl(t, rows, o) {
  const plain = div().flex_none().text_size(11).text_color(t.dim);
  if (!rows.limitRules?.length) return plain.child(rows.limitsLine);
  const link = (id) => div().id(`places-rule-${id}`).flex_none().text_color(t.accent).cursor_pointer().hover((s) => s.text_color(t.foreground))
    .on_click((_e, cx) => { cx?.stop_propagation?.(); o.onRule?.(id, cx); }).child(id);
  return h_flex().flex_none().text_size(11).text_color(t.dim).child(div().flex_none().pr(4).child("limits:"))
    .children(rows.limitRules.flatMap((id, k) => [...(k ? [div().flex_none().pr(4).child(",")] : []), link(id)]));
}
