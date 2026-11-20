// The Queue view (t-055): each repository's merge queue, read only, from the space's queue.json as pa-queue last
// wrote it (t-052; pa-queue is its only writer, every action on the queue stays a card). It shows the entries in
// merge order (state, the branch each is stacked on, what it waits for, its approval), the entries that left (why,
// the fix round they went to) and the last merges. Read by the panel's load (store.js loadQueue: one keyed, capped
// read); this file starts no call. It reads J3b's and J3c's fields (approval, queue cards, doc-only reviews) when
// they are there, and says "not asked yet" until then. t-084: each entry's workers (a link to the Agents view) and
// the evidence files it names (a link to the Files view; "(not yet)" while not written); an answered merge card for
// the entry's task stands for its approval until J3b writes one. t-154: each entry is a slot (no number; the stored
// order is merge order, the top one stacked nearest main), its task id a link to the task (as Costs links one), its
// workers the task card's worker item (tasks.js workerItem: the status dot; a closed one is not drawn); Left and Merged
// entries are task items as in the Tasks tab (id, title, state chip) with the facts as the detail line, and an entry
// that left and later merged is no longer under Left.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { home } from "herdr";
import { gaveUp } from "./host.js";
import { zone } from "./zone.js";
import { workerItem, stateChip } from "./tasks.js";

export const LEFT_SHOWN = 10, MERGED_SHOWN = 10; // t-145: the 10 most recent merged; the rest behind Archived (n)
export const STORED_LINE = "As pa-queue last wrote it; pa-queue show reads each state now.";
export const MISSING_LINE = "No merge queue in this space yet. pa-queue add starts one.";

const obj = (x) => Boolean(x) && typeof x === "object" && !Array.isArray(x);
const str = (x) => (typeof x === "string" && x ? x : null);
const list = (x) => (Array.isArray(x) ? x : []);
const short = (s) => (str(s) ? s.slice(0, 7) : "?");
const who = (x) => (x.job ? `${x.task} ${x.job}` : x.task);
const same = (a, task, job) => a?.task === task && (a?.job || null) === (job || null);
const readable = (x) => obj(x) && Boolean(str(x.task));
// A stored UTC time in the person's zone, "MM-DD HH:MM".
export function at(iso) {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return String(iso ?? "?");
  return new Date(ms + zone.offsetAt(ms) * 60000).toISOString().slice(5, 16).replace("T", " ");
}

// What a stored entry waits for (pa-queue's derive re-reads it from git; the stored fields are what it saw last).
function waits(e) {
  if (e.state === "invalidated") return "waits for pa-queue restack";
  if (e.state !== "checking") return null;
  const need = obj(e.needs) && obj(e.needs.shortReview) ? e.needs.shortReview : null;
  const out = [];
  if (!obj(e.suite) || typeof e.suite.exit !== "number") out.push("its suite");
  if (need && !(obj(e.review) && e.review.tip === e.tip)) out.push(`its ${need.kind === "doc-only" ? "doc-only " : ""}short review${str(need.file) ? ` (${need.file})` : ""}`);
  return `waits for ${out.length ? out.join(", ") : "pa-gate at its tip, or the next pa-queue refresh"}`;
}

// The approval line: the entry's approval (J3b), else a queue card that lists it, else not asked.
function approval(e, cards) {
  const a = e.approval;
  if (obj(a)) {
    if (str(a.void)) return `approval: voided (${a.void})`;
    if (str(a.carried)) return `approval: approved, carried over (${a.carried})`;
    return `approval: approved: ${a.option ?? "?"} (${a.card ?? "?"}${a.answeredAt ? `, ${at(a.answeredAt)}` : ""})`;
  }
  const lists = (c) => obj(c) && c.kind === "queue" && obj(c.queue) && list(c.queue.entries).some((x) => same(x, e.task, e.job));
  const open = cards.find((c) => lists(c) && c.status === "open");
  if (open) return `approval: card asked (${open.id})`;
  const answered = cards.find((c) => lists(c) && c.status === "answered" && !c.queue.applied && /^Merge/.test(String(c.answer ?? "")));
  if (answered) return `approval: answered on ${answered.id}, applied at the next pa-queue merge`;
  // t-084 R4: the newest answered merge card for the task (not a queue card); with a job, one that names it.
  const names = (c) => !e.job || c.job === e.job || new RegExp(`(^|[^A-Za-z0-9_-])${String(e.job).replace(/[^A-Za-z0-9]/g, "\\$&")}([^A-Za-z0-9_-]|$)`).test(String(c.question ?? ""));
  const merge = cards.filter((c) => obj(c) && c.kind !== "queue" && c.task === e.task && c.status === "answered" && /^Merge\b/.test(String(c.answer ?? "")) && names(c))
    .sort((a, b) => (Date.parse(b.answered) || 0) - (Date.parse(a.answered) || 0))[0];
  if (merge) return `approval: approved on ${merge.id} (${merge.answer}${merge.answered ? `, ${at(merge.answered)}` : ""})`;
  return "approval: not asked yet";
}

// t-084 R1: the entry's workers: the task's unfinished ledger workers on the entry's worktree, then the suite and
// short-review workers the entry names (their ledger role and finished mark when the task records them).
const trim = (p) => String(p ?? "").replace(/\/+$/, "");
function workersOf(e, ledger) {
  const task = list(ledger?.tasks).find((x) => obj(x) && x.id === e.task);
  const crew = list(task?.workers).filter((w) => obj(w) && str(w.address));
  const out = [], add = (w, address) => {
    if (w?.finished && w?.closed) return; // t-154: closed with pa-worker close: not drawn, as in the task card
    if (!out.some((x) => x.address === address)) out.push({ address, label: `${w?.role ?? "worker"} ${address}`, finished: Boolean(w?.finished), recorded: Boolean(w) });
  };
  if (str(e.worktree)) for (const w of crew) if (!w.finished && trim(w.worktree) === trim(e.worktree)) add(w, String(w.address));
  for (const a of [e.suite?.worker, e.review?.worker].map(str).filter(Boolean)) add([...crew].reverse().find((w) => w.address === a), a);
  return out;
}
// t-084 R2: the evidence files the entry names, once each.
const filesOf = (e) => [...new Set([e.needs?.shortReview?.file, e.review?.file, e.heldApproval?.file].map(str).filter(Boolean))];

// The fix round a task that left went to: the first build or fix worker of the ledger started at or after it left.
function fixRound(l, ledger) {
  const task = list(ledger?.tasks).find((x) => obj(x) && x.id === l.task);
  const left = Date.parse(l.at);
  const w = list(task?.workers).filter((x) => obj(x) && (/build|fix/.test(String(x.role ?? "")) || /fix/.test(String(x.name ?? "")))
    && (!x.job || !l.job || x.job === l.job) && Date.parse(x.started) >= left).sort((a, b) => Date.parse(a.started) - Date.parse(b.started))[0];
  return w ? `went to fix round ${w.round ?? "?"} (${w.address ?? "?"}, ${w.role ?? "?"})` : "no fix round yet";
}

// What the view draws, from loadQueue's result (null or a give-up: loading), the ledger and the panel's cards
// ([{ id, card }]). Pure. { state: loading | missing | error | ok, error, repos, count }.
export function queueRows(q, ledger, cards = []) {
  if (!q || gaveUp(q)) return { state: "loading", repos: [], count: null };
  if (q.error) return { state: "error", error: q.error, repos: [], count: null };
  if (q.missing || !obj(q.doc)) return { state: "missing", repos: [], count: null };
  const known = new Map(list(ledger?.tasks).filter((x) => obj(x) && str(x.id)).map((x) => [x.id, x]));
  // t-154: a Left or Merged entry as a task item: its id (a link when the ledger has the task), title and state.
  const item = (x) => { const k = known.get(x.task); return { task: x.task, linked: Boolean(k), title: k ? String(k.title ?? "(untitled)") : null, state: k ? str(k.state) : null }; };
  const jobOf = (x) => (str(x.job) ? `${x.job} · ` : "");
  const cardList = list(cards).map((c) => (obj(c?.card) ? { ...c.card, id: c.card.id ?? c.id } : null)).filter(Boolean);
  const paths = Object.keys(q.doc.repos ?? {}).sort();
  const base = (p) => p.split("/").filter(Boolean).pop() ?? p;
  const clash = (p) => paths.filter((x) => base(x) === base(p)).length > 1;
  let count = 0;
  const repos = paths.map((path) => {
    const rec = obj(q.doc.repos[path]) ? q.doc.repos[path] : {};
    const all = list(rec.entries), entries = all.filter(readable);
    const main = str(rec.main) ?? "main";
    const owner = (branch) => entries.find((x) => x.branch === branch);
    count += entries.length;
    const shown = entries.map((e, i) => {
      const ahead = i ? entries[i - 1].branch : main;
      const onto = str(e.onto) ?? "?";
      const by = owner(onto);
      const dep = obj(e.dependsOn) && str(e.dependsOn.task) ? who(e.dependsOn) : null;
      const stack = [onto === main ? "on main" : `on ${onto}${by ? ` (${who(by)})` : ""}`, ...(dep ? [`depends on ${dep}`] : []), ...(onto !== ahead ? ["restack pending"] : [])].join(" · ");
      const s = obj(e.suite) && typeof e.suite.exit === "number" ? e.suite : null;
      const suite = s ? `suite ${s.exit === 0 ? "passed" : `failed (exit ${s.exit})`}${s.ended ? ` ${at(s.ended)}` : ""}${s.worker ? ` by ${s.worker}` : ""}` : null;
      const r = obj(e.review) ? e.review : null;
      const review = r ? `short review ${r.verdict ?? "?"}${r.ended ? ` ${at(r.ended)}` : ""}${r.worker ? ` by ${r.worker}` : ""}` : null;
      return { key: `${path}#${i}`, task: e.task, job: str(e.job), linked: known.has(e.task), label: who(e), state: str(e.state) ?? "?", stack, waits: waits(e), suite, review, approval: approval(e, cardList),
        workers: workersOf(e, ledger), files: filesOf(e) };
    });
    // t-154: an entry that left and merged at or after it left is no longer under Left.
    const mergedAfter = (l) => list(rec.merged).some((m) => readable(m) && same(m, l.task, l.job) && Date.parse(m.at) >= Date.parse(l.at));
    const leftAll = list(rec.left).filter(readable).filter((l) => !mergedAfter(l));
    const left = leftAll.slice(-LEFT_SHOWN).reverse().map((l) => ({ ...item(l),
      detail: `${jobOf(l)}left ${at(l.at)}: ${l.why ?? "?"}`,
      round: fixRound(l, ledger),
      void: obj(l.approval) && str(l.approval.void) ? `approval voided: ${l.approval.void}` : null,
      resumable: l.resumable === true ? "approval kept for a doc-only rejoin" : null,
    }));
    const mergedAll = list(rec.merged).filter(readable).reverse() // newest first
      .map((m) => ({ ...item(m), detail: `${jobOf(m)}merged ${at(m.at)} at ${short(m.tip)} (${m.by ?? "queue"}${m.card ? `, ${m.card}` : ""})` }));
    const merged = mergedAll.slice(0, MERGED_SHOWN), mergedRest = mergedAll.slice(MERGED_SHOWN);
    return { path, title: clash(path) ? path.replace(home(), "~") : base(path), main, intoLine: `into ${main}`, entries: shown, skipped: all.length - entries.length,
      left, leftCount: leftAll.length, leftMore: Math.max(0, leftAll.length - LEFT_SHOWN), merged, mergedRest };
  });
  return { state: "ok", repos, count };
}

// The tab's label: the entries over every repository; "!" for an invalid file; no count while unknown or missing.
export function queueLabel(rows) {
  return rows.state === "error" ? "Queue · !" : rows.state === "ok" ? `Queue · ${rows.count}` : "Queue";
}

const TONE = { checking: "working", passed: "done", failed: "blocked", invalidated: "dim" };

// Draws the view; starts nothing (t-053). o (t-084): { isLive(address), onWorker(address), evidence (the load's Map of
// task -> files, or undefined: not known), onFile(task, file) }; a click only opens a view. t-145: mergedOpen(path) (the
// repository's merged entries past MERGED_SHOWN are shown), onMerged(path, cx) (its Archived (n) row clicked). t-154:
// statusOf(address) (a live terminal's status, for the worker dot), onTask(task, cx) (a task link clicked: the Tasks tab).
export function renderQueue(t, rows, o = {}) {
  const note = (text, color) => div().px(4).py(4).text_size(12).text_color(color ?? t.dim).child(text);
  const line = (text, color) => div().text_size(11.5).text_color(color ?? t.muted).child(text);
  const live = (a) => Boolean(o.isLive?.(a));
  // t-154: the task card's worker item. One with no ledger record and no live terminal is closed for all the view can tell: not drawn.
  const worker = (e, w, k) => {
    const on = live(w.address);
    return workerItem(t, { id: `queue-worker-${e.key}-${k}`, live: on, status: on ? o.statusOf?.(w.address) ?? null : null, onClick: on ? () => o.onWorker?.(w.address) : null,
      text: on ? `${w.label} ↗` : `${w.label} ${w.finished ? "(closed)" : "(ended)"}` });
  };
  const toTask = (task) => (_e, cx) => { cx?.stop_propagation?.(); o.onTask?.(task, cx); };
  const taskId = (id, task, linked, size = 12) => linked
    ? div().id(id).flex_none().text_size(size).text_color(t.accent).cursor_pointer().hover((s) => s.text_color(t.foreground)).on_click(toTask(task)).child(task)
    : div().flex_none().text_size(size).text_color(t.foreground).child(task);
  // t-154 (d-270): a Left or Merged entry as a task item: the head (id, title, state chip; a link to the task when the
  // ledger has it), the facts below it.
  const taskItem = (id, linkId, x, extra = []) => {
    const head = h_flex().h(28).px(9).gap(7).items_center()
      .child(div().flex_none().text_size(11.5).text_color(x.linked ? t.accent : t.dim).child(x.task))
      .when(Boolean(x.title), (b) => b.child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(t.foreground).child(x.title ?? "")))
      .when(Boolean(x.state), (b) => b.child(stateChip(t, x.state)));
    return v_flex().id(id).flex_none().rounded(7).border(1).border_color(t.border).bg(t.surface)
      .child(x.linked ? head.id(linkId).cursor_pointer().hover((s) => s.bg(t.hover)).on_click(toTask(x.task)) : head)
      .child(v_flex().px(9).pb(6).gap(1).child(line(x.detail, t.muted)).children(extra));
  };
  const known = o.evidence instanceof Map;
  const file = (e, f, k) => {
    const there = known && (o.evidence.get(e.task) ?? []).includes(f);
    return there ? div().id(`queue-file-${e.key}-${k}`).flex_none().text_size(11.5).text_color(t.accent).cursor_pointer().on_click(() => o.onFile?.(e.task, f)).child(`${f} ↗`)
      : div().flex_none().text_size(11.5).text_color(t.dim).child(known ? `${f} (not yet)` : f);
  };
  const box = v_flex().id("pa-queue").flex_1().min_h(0).p(8).gap(6).overflow_y_scrollbar();
  if (rows.state === "loading") return box.child(note("Loading queue…"));
  if (rows.state === "missing") return box.child(note(MISSING_LINE));
  if (rows.state === "error") return box.child(v_flex().p(10).gap(4).rounded(7).border(1).border_color(t.blocked).bg(t.surface)
    .child(div().text_size(12).text_color(t.blocked).child(rows.error))
    .child(div().text_size(11.5).text_color(t.muted).child("pa-queue refuses it too; fix it by hand. This view never writes it.")));
  return box.child(note(STORED_LINE))
    .children(rows.repos.map((r, k) => v_flex().flex_none().p(10).gap(4).rounded(7).border(1).border_color(t.border).bg(t.surface)
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_semibold().child(r.title))
        .child(div().text_size(11).text_color(t.dim).child(r.intoLine)))
      .when(!r.entries.length, (el) => el.child(line("Queue empty.", t.dim)))
      .children(r.entries.map((e) => {
        const crew = e.workers.filter((w) => w.recorded || live(w.address));
        return v_flex().id(`queue-slot-${e.key}`).flex_none().px(8).py(6).gap(1).rounded(6).border(1).border_color(t.border) // t-154: a slot, no number
        .child(h_flex().gap(8).items_center()
          .child(taskId(`queue-task-${e.key}`, e.task, e.linked))
          .when(Boolean(e.job), (b) => b.child(div().flex_none().text_size(12).text_color(t.muted).child(e.job ?? "")))
          .child(div().flex_1())
          .child(div().text_size(11).text_color(t[TONE[e.state]] ?? t.muted).child(e.state)))
        .child(line(e.stack))
        .when(crew.length > 0, (el) => el.child(h_flex().gap(6).flex_wrap().items_center().children(crew.map((w, k) => worker(e, w, k)))))
        .when(Boolean(e.waits), (el) => el.child(line(e.waits ?? "")))
        .when(Boolean(e.suite), (el) => el.child(line(e.suite ?? "", t.dim)))
        .when(Boolean(e.review), (el) => el.child(line(e.review ?? "", t.dim)))
        .when(e.files.length > 0, (el) => el.child(h_flex().gap(6).flex_wrap().items_center().child(line("evidence:", t.dim)).children(e.files.map((f, k) => file(e, f, k)))))
        .child(line(e.approval, t.dim));
      }))
      .when(r.skipped > 0, (el) => el.child(line(`${r.skipped} ${r.skipped === 1 ? "entry" : "entries"} could not be read`, t.blocked)))
      .when(r.leftCount > 0, (el) => el.child(div().pt(4).text_size(11.5).font_semibold().text_color(t.dim).child(`Left · ${r.leftCount}`))
        .children(r.left.map((l, i) => taskItem(`queue-left-${k}-${i}`, `queue-left-task-${k}-${i}`, l,
          [line(l.round, t.dim), ...(l.void ? [line(l.void, t.dim)] : []), ...(l.resumable ? [line(l.resumable, t.dim)] : [])])))
        .when(r.leftMore > 0, (x) => x.child(line(`and ${r.leftMore} earlier`, t.dim))))
      .when(r.merged.length > 0, (el) => el.child(div().pt(4).text_size(11.5).font_semibold().text_color(t.dim).child("Merged"))
        .children(r.merged.map((m, i) => taskItem(`queue-merged-${k}-${i}`, `queue-merged-task-${k}-${i}`, m)))
        .when(r.mergedRest.length > 0, (x) => {
          const open = Boolean(o.mergedOpen?.(r.path));
          return x.child(div().id(`queue-merged-archived-${k}`).py(1).text_size(11.5).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.foreground))
            .on_click((_e, cx) => o.onMerged?.(r.path, cx)).child(`${open ? "▾" : "▸"} Archived (${r.mergedRest.length})`))
            .when(open, (y) => y.children(r.mergedRest.map((m, i) => taskItem(`queue-merged-${k}-${MERGED_SHOWN + i}`, `queue-merged-task-${k}-${MERGED_SHOWN + i}`, m))));
        }))));
}
