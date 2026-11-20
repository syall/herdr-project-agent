// The person's task order in the Tasks tab (t-097 J2). Pure: the panel's load reads task-order.json (store.js
// loadTaskOrder) and the ledger; this file computes the ranking, the moves and the rows; the one write
// (task-order.json, compare-and-write) is main.js's. readTaskOrder, effectiveTasks and moveTask are
// runtime/tools/pa_places.py's read_task_order, effective_tasks and move_task: both are checked against
// tests/samples/task-order-cases.json, so the panel and pa-places tasks give the same ranking.

import { div } from "gpui-kit";
import { h_flex } from "gpui-base";

export const RANKED = ["in-progress", "investigating", "review", "queued"]; // drawn with numbers and moves (R5)
export const HIDDEN = ["needs-decision", "parked"]; // a place in the ranking, no number, no moves
export const NOTICE = "task order changed";
export const MOVES_KEPT = 20;
const SECTIONS = ["needs-decision", "review", "in-progress", "investigating", "queued", "parked", "done"]; // the order that needs you first
const WHY = {
  empty: "task-order.json is empty", "not-json": "task-order.json is not JSON", "not-object": "task-order.json is not a JSON object",
  version: "task-order.json is not version 1", order: "task-order.json's order is not a list of task ids", last: "task-order.json's last is not a list of task ids",
};

const obj = (x) => Boolean(x) && typeof x === "object" && !Array.isArray(x);
const strings = (v) => Array.isArray(v) && v.every((i) => typeof i === "string");
const stamp = () => new Date(Date.now()).toISOString().replace(/\.\d+Z$/, "Z");

// { error, line, order, last, updated, doc } from task-order.json's text (null: no file; R1, R2). An unreadable file
// gives nothing: never the readable part (J1-D2). line: what both sides say (null when readable).
export function readTaskOrder(text) {
  const bad = (error) => ({ error, line: error === "missing" ? "order: the Project Agent's" : `task order unreadable: ${WHY[error]}; the Project Agent's order is used`, order: [], last: [], updated: null, doc: null });
  if (text === null || text === undefined) return bad("missing");
  if (!String(text).trim()) return bad("empty");
  let doc;
  try { doc = JSON.parse(text); } catch { return bad("not-json"); }
  if (!obj(doc)) return bad("not-object");
  if (doc.version !== 1) return bad("version");
  if (!strings(doc.order)) return bad("order");
  const last = doc.last === undefined ? [] : doc.last;
  if (!strings(last)) return bad("last");
  return { error: null, line: null, order: doc.order, last, updated: typeof doc.updated === "string" ? doc.updated : null, doc };
}

// The order line above the sections (R2, R15). read: readTaskOrder's (null: not read yet).
export function orderLine(read) {
  if (!read) return "order: not read yet";
  if (read.error === null) return "order: yours · moves are saved to task-order.json and tell the Project Agent";
  return read.line;
}

// (order ids, chat entries, shape ok) from the ledger's taskOrder (pa_places.task_order_parts).
function taskOrderParts(taskOrder) {
  if (taskOrder === null || taskOrder === undefined) return [[], [], true];
  if (!obj(taskOrder)) return [[], [], false];
  const order = "order" in taskOrder ? taskOrder.order : [], chat = "chat" in taskOrder ? taskOrder.chat : []; // as dict.get(k, []): an explicit null is not a list
  const ok = Array.isArray(order) && Array.isArray(chat);
  return [Array.isArray(order) ? order.filter((i) => typeof i === "string") : [], Array.isArray(chat) ? chat.filter((e) => obj(e) && typeof e.task === "string") : [], ok];
}

// A task order exists (J1-D10): a readable task-order.json, or a ledger taskOrder of its shape. Until then the
// waiting list keeps the added order (R27).
export function hasTaskOrder(read, taskOrder) {
  return Boolean(read && read.error === null) || (taskOrder !== null && taskOrder !== undefined && taskOrderParts(taskOrder)[2]);
}

// Task ids by number: t-9 before t-10 (pa_places.id_key).
const idKey = (id) => String(id).split(/(\d+)/).map((p) => (/^\d+$/.test(p) ? Number(p) : p));
function cmpKey(a, b) {
  const x = idKey(a), y = idKey(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    if (x[i] === y[i]) continue;
    if (typeof x[i] === typeof y[i]) return x[i] < y[i] ? -1 : 1;
    return typeof x[i] === "number" ? -1 : 1; // python compares int < str as an error; split keeps them aligned, this never decides in practice
  }
  return x.length - y.length;
}

// The task ranking, top first (R3): [{ id, state, source, quote? }]. tasks: the ledger's; taskOrder: its
// taskOrder; read: readTaskOrder's. Chat entries that apply (over == the file's updated, null for no file), the
// file's order, the Project Agent's (taskOrder.order, then created oldest first, undated after, then id by number;
// reopened tasks after those, in the order they were reopened), the file's last. A task reopened since the file was
// written skips its file entries (staleIds), so a reopened done task starts at the bottom of Queued (t-120).
export function effectiveTasks(tasks, taskOrder, read) {
  const byId = new Map();
  for (const t of Array.isArray(tasks) ? tasks : []) {
    if (obj(t) && typeof t.id === "string" && [...RANKED, ...HIDDEN].includes(t.state) && !byId.has(t.id)) byId.set(t.id, t);
  }
  const out = [], placed = new Set();
  const put = (id, source, quote) => {
    if (!byId.has(id) || placed.has(id)) return;
    placed.add(id);
    out.push({ id, state: byId.get(id).state, source, ...(source === "chat" ? { quote: quote ?? null } : {}) });
  };
  const doc = read && read.error === null ? read : null;
  const [order, chat] = taskOrderParts(taskOrder);
  const updated = doc ? doc.updated : null, stale = staleIds([...byId.values()], read);
  for (const e of chat) if ((e.over ?? null) === updated) put(e.task, "chat", e.quote);
  for (const id of doc ? doc.order : []) if (!stale.has(id)) put(id, "person");
  const last = new Set((doc ? doc.last : []).filter((id) => !stale.has(id)));
  for (const id of order) if (!last.has(id)) put(id, "agent");
  const dated = (t) => typeof t.created === "string", reopened = (t) => typeof t.reopened === "string";
  const rest = [...byId.values()].filter((t) => !placed.has(t.id) && !last.has(t.id)).sort((a, b) =>
    (reopened(a) === reopened(b) ? 0 : reopened(a) ? 1 : -1) || (reopened(a) && a.reopened !== b.reopened ? (a.reopened < b.reopened ? -1 : 1) : 0) ||
    (dated(a) === dated(b) ? 0 : dated(a) ? -1 : 1) || (dated(a) && a.created !== b.created ? (a.created < b.created ? -1 : 1) : 0) || cmpKey(a.id, b.id));
  for (const t of rest) put(t.id, "agent");
  for (const id of doc ? doc.last : []) if (!stale.has(id)) put(id, "person");
  return out;
}

// t-120 (t-097 R5; pa_places.stale_ids): the tasks reopened at or after task-order.json was written. The file's old
// place for them is not read (prune on read), and freshOrder leaves them out of the next write. No updated: older.
export function staleIds(tasks, read) {
  const updated = read && read.error === null ? read.updated : null;
  return new Set((Array.isArray(tasks) ? tasks : []).filter((t) => obj(t) && typeof t.id === "string" && typeof t.reopened === "string" && (updated === null || t.reopened >= updated)).map((t) => t.id));
}

// The readable file's { order, last } without its stale entries (staleIds): what a move or Start next builds on, so
// it never writes a reopened task's old place back (t-120).
export function freshOrder(read, tasks) {
  const stale = staleIds(tasks, read), ok = read && read.error === null;
  return { order: (ok ? read.order : []).filter((x) => !stale.has(x)), last: (ok ? read.last : []).filter((x) => !stale.has(x)) };
}

// The new { order, last } after a move of task `id` in the section of state `section`, or null for a no-op (R7;
// pa_places.move_task). The neighbour is the nearest task of the same state; other tasks keep their places. up, down
// and top write the ranking from the top down to the lower moved place into order (the person's ids below stay after
// it, last keeps only the ids below it); bottom sends the task to the end of last. Every write also puts the chat
// tasks into order (R12). The inputs are not changed.
export function moveTask(ranking, section, id, move, order, last) {
  const ids = ranking.map((r) => r.id);
  if (!ids.includes(id) || HIDDEN.includes(section) || !RANKED.includes(section)) return null;
  const i = ids.indexOf(id);
  if (ranking[i].state !== section) return null;
  const same = ranking.map((r, k) => (r.state === section ? k : -1)).filter((k) => k >= 0);
  const pos = same.indexOf(i), seq = [...ids];
  const chatIds = ranking.filter((r) => r.source === "chat").map((r) => r.id), chats = chatIds.length;
  let end;
  if (move === "up" || move === "top") {
    if (pos === 0) return null;
    if (move === "up") { const j = same[pos - 1]; [seq[i], seq[j]] = [seq[j], seq[i]]; end = i; }
    else { end = same[0]; seq.splice(end, 0, ...seq.splice(i, 1)); }
  } else if (move === "down" || move === "bottom") {
    if (pos === same.length - 1) return null;
    if (move === "down") { const j = same[pos + 1]; [seq[i], seq[j]] = [seq[j], seq[i]]; end = j; }
    else { seq.splice(i, 1); end = -1; }
  } else return null;
  // the prefix runs to the lower moved place and to every chat task's place after the move (a top can shift them)
  const lowestChat = seq.reduce((m, x, k) => (chatIds.includes(x) ? k : m), -1);
  const prefix = move !== "bottom" ? seq.slice(0, Math.max(end, lowestChat) + 1) : ids.slice(0, chats).filter((x) => x !== id);
  const newOrder = [...prefix];
  for (const x of order) if (!newOrder.includes(x) && !(move === "bottom" && x === id)) newOrder.push(x);
  const newLast = [];
  for (const x of last) if (!prefix.includes(x) && !newLast.includes(x) && x !== (move === "bottom" ? id : null)) newLast.push(x);
  if (move === "bottom") newLast.push(id);
  return { order: newOrder, last: newLast };
}

// Start next's rank (R18 (a)): the task to the very top of the ranking, out of last; the chat tasks into order (R12),
// the person's ids after. null: nothing to write (already first and the person's, or not in the ranking).
export function startNextOrder(ranking, id, order, last) {
  const at = ranking.findIndex((r) => r.id === id);
  if (at < 0) return null;
  const chatIds = ranking.filter((r) => r.source === "chat" && r.id !== id).map((r) => r.id);
  if (at === 0 && ranking[0].source === "person" && !chatIds.length && order[0] === id) return null;
  const newOrder = [id];
  for (const x of [...chatIds, ...order]) if (!newOrder.includes(x)) newOrder.push(x);
  return { order: newOrder, last: last.filter((x, k) => x !== id && last.indexOf(x) === k) };
}

// R4: every write drops the ids no longer in the ledger and the ids of done tasks; parked and needs-decision stay.
export function pruneOrder({ order, last }, tasks) {
  const live = new Set((Array.isArray(tasks) ? tasks : []).filter((t) => obj(t) && typeof t.id === "string" && t.state !== "done").map((t) => t.id));
  return { order: order.filter((x) => live.has(x)), last: last.filter((x) => live.has(x)) };
}

// The task-order.json text the panel writes (R1): version 1, by the person, the last MOVES_KEPT moves.
export function taskOrderText(prevText, { order, last }, { task = null, move, section = null }) {
  const prev = readTaskOrder(prevText).doc, now = stamp();
  const moves = [...(Array.isArray(prev?.moves) ? prev.moves : []), { at: now, ...(task ? { task } : {}), move, ...(section ? { section } : {}) }].slice(-MOVES_KEPT);
  return `${JSON.stringify({ version: 1, updated: now, by: "person", order, last, moves }, null, 2)}\n`;
}

// R13: the marker after a row's title. agent: PA; chat: PA · your words; the person's: none.
export const chipText = (source) => (source === "agent" ? "PA" : source === "chat" ? "PA · your words" : null);

// The Tasks tab's sections (R5, R14, R15). tasks: the ledger's, in the order the unranked sections draw (newest
// first); ranking: effectiveTasks's. [{ state, ranked, items }] in the order that needs you first, empty ones left
// out. A ranked section's rows follow the ranking with their number (1, 2, ... in the section) and the moves that do
// something (R7: no ↑ ⤒ on the first, no ↓ ⤓ on the last); a hidden-rank section follows the ranking with no number;
// parked and done keep the order given. In Queued, the follow-ups of one origin (followUpOf, two or more) fold into
// one item at their first member's place. An item: { kind: "task", task, n, source, quote, up, down, top, bottom }
// (after: its after tasks not done) or { kind: "fold", key, origin, count, first, rows }.
export function taskSections(tasks, ranking) {
  const list = Array.isArray(tasks) ? tasks.filter(obj) : [];
  const states = [...SECTIONS, ...new Set(list.map((t) => t.state).filter((s) => !SECTIONS.includes(s)))];
  const place = new Map(ranking.map((r, k) => [r.id, k])), info = new Map(ranking.map((r) => [r.id, r]));
  const stateOf = new Map(list.map((t) => [t.id, t.state]));
  const after = (t) => (Array.isArray(t.after) ? t.after.filter((a) => typeof a === "string" && stateOf.get(a) !== "done") : []); // R18: an id not in the ledger counts as not done
  const out = [];
  for (const state of states) {
    const mine = list.filter((t) => t.state === state);
    if (!mine.length) continue;
    const ranked = RANKED.includes(state), byRank = ranked || state === "needs-decision";
    const sorted = byRank ? [...mine].sort((a, b) => (place.get(a.id) ?? Infinity) - (place.get(b.id) ?? Infinity)) : mine; // stable: unranked keep their order, after
    const rows = sorted.map((task, k) => {
      const r = info.get(task.id), n = ranked && r ? k + 1 : null, movable = ranked && Boolean(r);
      return { kind: "task", task, n, source: r?.source ?? null, quote: r?.quote ?? null, after: after(task),
        up: movable && k > 0, top: movable && k > 0, down: movable && k < sorted.length - 1, bottom: movable && k < sorted.length - 1 };
    });
    let items = rows;
    if (state === "queued") {
      const groups = new Map();
      for (const row of rows) { const o = row.task.followUpOf; if (typeof o === "string" && o) groups.set(o, [...(groups.get(o) ?? []), row]); }
      items = [];
      for (const row of rows) {
        const o = row.task.followUpOf, g = typeof o === "string" ? groups.get(o) : null;
        if (!g || g.length < 2) { items.push(row); continue; }
        if (g[0] === row) items.push({ kind: "fold", key: `${state}-${o}`, origin: o, count: g.length, first: row.n, rows: g });
      }
    }
    out.push({ state, ranked, items });
  }
  return out;
}

// R18: "waits for t-002 (after)" on a row whose after tasks are not all done; null when none.
export const afterLine = (ids) => (Array.isArray(ids) && ids.length ? `waits for ${ids.join(", ")} (after)` : null);

// The fold's line (R14): "▸ 4 follow-ups of t-083 · first #6".
export const foldLine = (fold, open) => `${open ? "▾" : "▸"} ${fold.count} follow-ups of ${fold.origin}${fold.first ? ` · first #${fold.first}` : ""}`;

// Mark done's confirmation line (R20), from what the panel already read: the task's change request, its worktrees,
// its merge-queue entries and its live workers.
export function doneLine({ changeRequest = null, worktrees = 0, queueEntries = 0, live = [] } = {}) {
  const parts = [];
  if (obj(changeRequest) && (changeRequest.id || changeRequest.url)) parts.push(typeof changeRequest.id === "string" && changeRequest.id ? `change request ${changeRequest.id} stays unmerged` : "its change request stays unmerged");
  if (worktrees > 0) parts.push(`${worktrees} worktree${worktrees === 1 ? " stays" : "s stay"}`);
  if (queueEntries > 0) parts.push(`${queueEntries} merge-queue ${queueEntries === 1 ? "entry stays" : "entries stay"}`);
  for (const a of live) parts.push(`${a} keeps running until you close it`);
  return `Done merges nothing: ${parts.length ? parts.join(" · ") : "nothing is left open"}.`;
}

// A worker item (t-142): the box a task card draws for each of its workers, also drawn for each worker line in
// Places. live: its terminal is live; status: that terminal's (hc state: working, blocked, idle, done), drawn as the
// dot's color (unknown: idle; not live: dim). id and onClick only for a link; text: the label as shown; trailing: an
// element after it (the task card's ×). Returns the element, for the caller's own .when(...) marks.
export function workerItem(t, { id = null, text, live, status = null, onClick = null, trailing = null }) {
  const el = h_flex().flex_none().px(8).h(24).gap(6).items_center().rounded(6).border(1).border_color(t.border).text_size(11.5).text_color(live ? t.foreground : t.dim);
  return (id ? el.id(id) : el)
    .when(Boolean(id && onClick), (b) => b.cursor_pointer().hover((s) => s.bg(t.hover)).on_click(onClick))
    .child(div().size(6).rounded(3).bg(live ? t[status] ?? t.idle : t.dim))
    .child(text)
    .when(Boolean(trailing), (b) => b.child(trailing));
}
