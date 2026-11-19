// The Queue view (t-055): each repository's merge queue, read only, from the space's queue.json as pa-queue last
// wrote it (t-052; pa-queue is its only writer, every action on the queue stays a card). It shows the entries in
// merge order (state, the branch each is stacked on, what it waits for, its approval), the entries that left (why,
// the fix round they went to) and the last merges. Read by the panel's load (store.js loadQueue: one keyed, capped
// read); this file starts no call. It reads J3b's and J3c's fields (approval, queue cards, doc-only reviews) when
// they are there, and says "not asked yet" until then.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { home } from "herdr";
import { gaveUp } from "./host.js";
import { zone } from "./zone.js";

export const LEFT_SHOWN = 10, MERGED_SHOWN = 5;
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
  return "approval: not asked yet";
}

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
      return { key: `${path}#${i}`, label: `${i + 1}. ${who(e)}`, state: str(e.state) ?? "?", stack, waits: waits(e), suite, review, approval: approval(e, cardList) };
    });
    const leftAll = list(rec.left).filter(readable);
    const left = leftAll.slice(-LEFT_SHOWN).reverse().map((l) => ({
      text: `${who(l)} left ${at(l.at)}: ${l.why ?? "?"}`,
      round: fixRound(l, ledger),
      void: obj(l.approval) && str(l.approval.void) ? `approval voided: ${l.approval.void}` : null,
      resumable: l.resumable === true ? "approval kept for a doc-only rejoin" : null,
    }));
    const merged = list(rec.merged).filter(readable).slice(-MERGED_SHOWN).reverse()
      .map((m) => `${who(m)} merged ${at(m.at)} at ${short(m.tip)} (${m.by ?? "queue"}${m.card ? `, ${m.card}` : ""})`);
    return { path, title: clash(path) ? path.replace(home(), "~") : base(path), main, entries: shown, skipped: all.length - entries.length,
      left, leftCount: leftAll.length, leftMore: Math.max(0, leftAll.length - LEFT_SHOWN), merged };
  });
  return { state: "ok", repos, count };
}

// The tab's label: the entries over every repository; "!" for an invalid file; no count while unknown or missing.
export function queueLabel(rows) {
  return rows.state === "error" ? "Queue · !" : rows.state === "ok" ? `Queue · ${rows.count}` : "Queue";
}

const TONE = { checking: "working", passed: "done", failed: "blocked", invalidated: "dim" };

// Draws the view; starts nothing (t-053).
export function renderQueue(t, rows) {
  const note = (text, color) => div().px(4).py(4).text_size(12).text_color(color ?? t.dim).child(text);
  const line = (text, color) => div().text_size(11.5).text_color(color ?? t.muted).child(text);
  const box = v_flex().id("pa-queue").flex_1().min_h(0).p(8).gap(6).overflow_y_scrollbar();
  if (rows.state === "loading") return box.child(note("Loading queue…"));
  if (rows.state === "missing") return box.child(note(MISSING_LINE));
  if (rows.state === "error") return box.child(v_flex().p(10).gap(4).rounded(7).border(1).border_color(t.blocked).bg(t.surface)
    .child(div().text_size(12).text_color(t.blocked).child(rows.error))
    .child(div().text_size(11.5).text_color(t.muted).child("pa-queue refuses it too; fix it by hand. This view never writes it.")));
  return box.child(note(STORED_LINE))
    .children(rows.repos.map((r) => v_flex().flex_none().p(10).gap(4).rounded(7).border(1).border_color(t.border).bg(t.surface)
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_semibold().child(r.title))
        .child(div().text_size(11).text_color(t.dim).child(`main ${r.main}`)))
      .when(!r.entries.length, (el) => el.child(line("Queue empty.", t.dim)))
      .children(r.entries.map((e) => v_flex().gap(1).py(2)
        .child(h_flex().gap(8).items_center()
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.foreground).child(e.label))
          .child(div().text_size(11).text_color(t[TONE[e.state]] ?? t.muted).child(e.state)))
        .child(line(e.stack))
        .when(Boolean(e.waits), (el) => el.child(line(e.waits ?? "")))
        .when(Boolean(e.suite), (el) => el.child(line(e.suite ?? "", t.dim)))
        .when(Boolean(e.review), (el) => el.child(line(e.review ?? "", t.dim)))
        .child(line(e.approval, t.dim))))
      .when(r.skipped > 0, (el) => el.child(line(`${r.skipped} ${r.skipped === 1 ? "entry" : "entries"} could not be read`, t.blocked)))
      .when(r.leftCount > 0, (el) => el.child(div().pt(4).text_size(11.5).font_semibold().text_color(t.dim).child(`Left · ${r.leftCount}`))
        .children(r.left.map((l) => v_flex().gap(1).py(1)
          .child(line(l.text, t.foreground))
          .child(line(l.round, t.dim))
          .when(Boolean(l.void), (x) => x.child(line(l.void ?? "", t.dim)))
          .when(Boolean(l.resumable), (x) => x.child(line(l.resumable ?? "", t.dim)))))
        .when(r.leftMore > 0, (x) => x.child(line(`and ${r.leftMore} earlier`, t.dim))))
      .when(r.merged.length > 0, (el) => el.child(div().pt(4).text_size(11.5).font_semibold().text_color(t.dim).child("Merged"))
        .children(r.merged.map((m) => line(m, t.dim))))));
}
