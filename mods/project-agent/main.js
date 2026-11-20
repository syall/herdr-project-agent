// Project Agent panel: the current space's Project Agent, its decision cards, its tasks,
// and the buttons its profile declares. Generic: profiles add data and scripts, never code.
// Reads ~/.config/herdr-pa (on the space's machine). Writes only decision answers and rule changes.
// Runs a program only on a click: a profile action, `hc send` after an answer, or the
// launcher that starts a Project Agent. Push cards (push.js, t-007b): a shown copy runs the
// read-only `pa-push-main check` when one first appears and on a click, and the Rules view the
// read-only `pa-push-allow show`; `pa-push-main run` runs only from a push card's Submit.
// The Costs view (costs.js, t-009) reads costs/summary.json only while it is shown, and runs the
// read-only `pa-costs billed compare`; its Refresh, Reconstruct and Turn off run `pa-costs` on a click.
// Restart (t-037 J4) runs `pa-restart --dry-run` on its click and `pa-restart` (in place, once) on its confirm, on the
// space's machine; never `--end-terminal`: the panel ends no terminal.
// Fresh contexts (t-041 J5, t-145): one on/off switch in the head row runs the context tool's `set` on its click, on the
// space's machine, with --by panel. The panel no longer asks for a fresh context itself (the tool still can).

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, ScrollHandle } from "gpui-base";
import { spaces, panes, agents, terminals, machines, theme, run, props, home, notify, shared, setShared, closeOverlay, openOverlay, window, layout } from "herdr";
import { Host, gaveUp, read as hostRead, run as hostRun, watch, refusalLine } from "./host.js";
import { loadSpace, loadLedger, loadQueue, loadWaiting, loadStartOrder, writeStartOrder, loadMachines, loadTaskOrder, writeTaskOrder, loadProfile, loadDecisions, loadRules, TIMED_OUT, READ_LIMIT, answerDecision, notifyOrchestrator, changeRules, touched, localId, paHome, stateDir, taskActions, taskLinks, evidenceTasks, cardEvidence, staleInstructions, liveTarget, lookAlike, OPEN_REQUESTS, keeperJudging, startingLabels, OTHER, OTHER_MAX, OTHER_TOO_LONG, isOther, oneLine, codeState, codeBadge, loadCode, restartArgs, runRestart, restartOutcome, endTerminalLine, RESTART_MARK, RESTART_MARK_TTL, RESTARTED_FOR, RESTART_TIMEOUT, DRY_TIMEOUT, contextSettings, loadContext, contextLine, contextAt, contextArgs, runContext, contextOutcome, resetLine, CONTEXT_FOR } from "./store.js";
import { RulesPane, arm, armHolds, fieldState, noticeText as rulesNotice, mergeNotices as mergeRuleNotices } from "./rules.js"; // arm, armHolds: the two-click Delete (ARM_WAIT, 10 s)
import { queueRows, queueLabel, renderQueue, at as atZone } from "./queue.js"; // t-055: the Queue view (read only)
import { placesRows, renderPlaces, pinsAfter, orderText, NOTICE as ORDER_NOTICE } from "./places.js"; // t-083 J2: places and the start order (Tasks)
import { readTaskOrder, effectiveTasks, hasTaskOrder, moveTask, startNextOrder, pruneOrder, freshOrder, taskOrderText, taskSections, foldLine, chipText, afterLine, orderLine as taskOrderLine, doneLine, workerItem, NOTICE as TASK_ORDER_NOTICE } from "./tasks.js"; // t-097 J2: the person's task order (Tasks)
import { SetupPane } from "./setup.js";
import { Keeper, idleShell } from "./keeper.js";
import { WorkerPane } from "./worker.js";
import { PushCards, isPush, shownOnly, PUSH, PUSH_OPTIONS, RUN_TIMEOUT, GRACE } from "./push.js";
import { CostsPane } from "./costs.js";
import { Copier, sel } from "./copy.js";
import { tick, cadence, holdReason, setSchedule, requestPass, PRESETS, MIN_MINUTES } from "./timer.js";
import { Awake } from "./awake.js";

// t-145: the active states, in the order the profile line counts them (never parked or done).
const ACTIVE = ["needs-decision", "in-progress", "investigating", "review", "queued"];
const FINISHED_SHOWN = 10; // t-145: finished decisions and done tasks drawn before their Archived (n) row
const TASK_FOCUS = ["head", "top", "up", "down", "bottom", "next"]; // t-142: a task head's focus handles (wantTaskFocus)
const OPEN_WAIT = 30000; // an Open request with nothing started by then: say why, offer the button again
const OPEN_SLOW = 180000; // its new tab is recorded but its launcher has not started by then: say so
const paRecords = (spaceId) => Object.values(shared("project-agent.tabs") ?? {}).filter((r) => r?.space === spaceId);
const LOAD_LIMIT = READ_LIMIT + 1000; // a load with no read settled or timed out for this long is given up: a new one may start
const CACHE_MAX = 8; // spaces whose last load is kept (in memory), the least recently shown dropped first
const CACHE_OLD = 60000; // a kept load older than this says "updating…" while its refresh runs
const MACHINES_EVERY = 60000; // t-119: the suite machines (pa-full-suite --places --json, a probe per machine) are read at most this often
const COSTS_MARK = 20000; // while Costs is shown: shared "project-agent.costsShown" is stamped at most this often (the timer collects faster then)
const COUNT_EVERY = 10000; // while hidden: at most one card read and one ledger read per space this often
const PROFILE_RETRY = 60000; // while hidden: a profile read that failed is tried again no sooner than this
const COALESCE = 2000; // t-053: a background trigger (timer, watch) starts at most one load per space this often; the rest wait for its end
const BG = Object.freeze({ background: true });
// t-053: the watch batch paths, under the shown local space's folder, that the panel shows (a heartbeat, a log, the lock,
// the session log, the cost records and other spaces' folders reload nothing; the root itself: everything).
const SHOWN_PATH = /^(agent\.json|launcher\.json|ledger\.json|schedule\.json|pass-request\.json|rules\.json|queue\.json|decisions(\/[^/]+)?|evidence(\/[^/]+(\/(?!build\.log$)[^/]+)?)?|costs\/summary\.json)$/;
// A part of loadSpace's result that did not answer (its `unknown`), and the fields it fills.
const PARTS = { ledger: ["ledger"], profile: ["profile", "profileDir"], schedule: ["schedule"], request: ["request"] };
const NOTE = "Note for the Project Agent (optional)";
// t-046: the Other row every card has, and the hints under Submit while it is picked (the field is the answer then).
const OTHER_DETAIL = "Your own answer, in your words. The Project Agent acts on it as written; no tool's action runs on it.";
const OTHER_EMPTY = "Write your answer above to submit Other.", OTHER_HINT = "Write your answer above; it is sent as your own words.";
const RULE_STAYS = "The rule stays proposed: Other is not Approve or Decline.";
// A card's own option named Other (any case): the panel's Other row takes its place (spec R1 AC5).
const ownOther = (o) => String(o?.label ?? "").trim().toLowerCase() === "other";
// The closing line with the card's note: why it was withdrawn, the person's own answer (from the panel or from the
// conversation), or the note beside a listed answer.
const noteLine = (c) => (c.status === "withdrawn" ? `Why: ${c.note}`
  : isOther(c) ? (!c.answeredBy || /in the panel/.test(c.answeredBy) ? `Your answer: “${c.note}”` : `You said: “${c.note}”`)
  : c.answeredBy ? `You said: “${c.note}”` : `Note: ${c.note}`);
// The answer kept on a withdrawn card, and on the card asked again after it (pa.keep_previous, t-046 J1b), or null.
const earlierLine = (c) => {
  if (!c.previousAnswer || (c.status !== "open" && c.status !== "withdrawn")) return null;
  const said = `${c.previousAnswer}${c.previousNote ? `: “${c.previousNote}”` : ""}`;
  return c.status === "open" ? `Asked again${c.previousCard ? ` (after ${c.previousCard})` : ""}. Your earlier answer: ${said}` : `Earlier answer: ${said}`;
};
const QUIET = { from: "19:00", to: "08:00" }; // what the quiet-hours fields show while empty, and what Set takes for an empty one
// A quiet-hours field's time as HH:MM (24-hour; "7:30" is 07:30), or null when it is not one.
const hhmm = (v) => { const m = /^(\d{1,2}):(\d{2})$/.exec(v); return m && Number(m[1]) <= 23 && Number(m[2]) <= 59 ? `${m[1].padStart(2, "0")}:${m[2]}` : null; };
const noteKey = (spaceId, cardId) => `${spaceId}\0${cardId}`; // note fields are per space: two spaces can have the same card id
// t-045: a card answered from a panel copy is marked there (this.sent, the same key as noteKey) and, for the other
// copies, in shared ANSWERING { key: { label, note, at, by } }, no longer than ANSWERING_TTL.
const ANSWERING = "project-agent.answering", ANSWERING_TTL = 120000;
// A push run in flight carries its own (t-045 round 2); a push run whose result is unknown never expires: it goes only when
// a check reads the card open (openedPush) or a read shows the card no longer open (resolveMarks), t-045 round 3.
const ttlOf = (x) => (x?.unknown === true ? Infinity : typeof x?.ttl === "number" ? Math.min(x.ttl, RUN_TIMEOUT + GRACE + ANSWERING_TTL) : ANSWERING_TTL);
const isoAt = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");

// Whether this panel is on screen: a block showing it in the shown layout with no hidden node above
// it, and the selected child of each stack above it (how the tabs mod reads the side panel; the
// Changes view uses the same test). Only blocks with this copy's props are this copy's (a block
// does not name the copy it runs); the always-on timer copy never counts. A copy with no such block
// (a pane mod or a window with other props) counts as shown. Two blocks with the same props are
// one copy to this test: either shown counts. A hidden copy stays alive and its timers run.
const sameProps = (a, b) => { const flat = (o) => JSON.stringify(Object.keys(o ?? {}).sort().map((k) => [k, o[k]])); return flat(a) === flat(b); };
function onScreen(name = "project-agent") {
  let found = false, shown = false;
  const mine = props() ?? {};
  const walk = (n, ok) => {
    if (!n || typeof n !== "object") return;
    ok = ok && !n.hidden;
    if (n.content?.mod === name && !n.content?.props?.timer && sameProps(n.content?.props, mine)) { found = true; shown ||= ok; }
    const kids = Array.isArray(n.children) ? n.children : [], pick = n.type === "stack" ? n.active ?? kids[0]?.id : null;
    for (const c of kids) walk(c, ok && (pick === null || c?.id === pick));
  };
  walk(layout(), true);
  return shown || !found;
}
// t-020 J6 (AC9.1): a ledger task's worktrees, main first: [{path (as written), job, main}]. The panel's mirror of
// pa.task_worktrees: `worktree` (job main), then `worktrees` entries, then the old `extraWorktrees` strings (job from the
// folder); merged by path (no realpath here: the space may be on another machine, so the written path with "//", "/./"
// and a trailing "/" folded), the first one wins; an entry with `removed` is skipped and hides the same path in later
// sources, unless it is `worktree` itself; a job already taken in the task gets -2, -3, ...
const jobSlug = (text) => String(text ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "") || "job";
const pathKey = (p) => { const k = String(p).trim().replace(/\/+/g, "/").replace(/\/(\.\/)+/g, "/").replace(/\/\.$/, "/"); return k.length > 1 ? k.replace(/\/$/, "") : k; };
const legacyJob = (taskId, p) => {
  const parts = pathKey(p).split("/"), name = parts.at(-3) === "worktrees" ? parts.at(-2) : parts.at(-1);
  return jobSlug(taskId && name.startsWith(`${taskId}-`) ? name.slice(taskId.length + 1) : name);
};
function taskTrees(x) {
  if (!x || typeof x !== "object") return [];
  const tid = String(x.id ?? ""), usable = (e) => e && typeof e === "object" && typeof e.path === "string" && e.path.trim() !== "";
  const entries = (Array.isArray(x.worktrees) ? x.worktrees : []).filter(usable), extras = Array.isArray(x.extraWorktrees) ? x.extraWorktrees : [];
  const out = [], seen = new Set(), jobs = new Set();
  const add = (path, job, main) => {
    if (jobs.has(job)) { let n = 2; while (jobs.has(`${job}-${n}`)) n++; job = `${job}-${n}`; }
    jobs.add(job); seen.add(pathKey(path)); out.push({ path, job, main });
  };
  const entryJob = (e) => (typeof e.job === "string" && e.job.trim() ? jobSlug(e.job) : legacyJob(tid, e.path));
  const main = typeof x.worktree === "string" && x.worktree.trim() ? x.worktree : null;
  if (main) { const own = entries.find((e) => pathKey(e.path) === pathKey(main)); add(main, own ? entryJob(own) : "main", true); }
  for (const e of entries) if (e.removed) seen.add(pathKey(e.path));
  for (const e of entries) if (!e.removed && !seen.has(pathKey(e.path))) add(e.path, entryJob(e), false);
  for (const p of extras) if (typeof p === "string" && p.trim() && !seen.has(pathKey(p))) add(p, legacyJob(tid, p), false);
  return out;
}
const when = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };

export default class ProjectAgent extends View {
  init(_props, cx) {
    if (props()?.deleteWorker) { this.worker = new WorkerPane(cx); return; } // the delete-worker dialog
    if (props()?.setup) { // the new-space dialog: nothing else runs
      this.setup = new SetupPane(cx);
      // Answered some other way (typed in the tab, or the tab closed): the dialog goes too.
      cx.timer.every(500, () => {
        const state = terminals().find((x) => x.address === props().setup.terminal && x.state === "live")?.labels?.["herdr.pa.ask"];
        if (state !== "project" && state !== "home") closeOverlay(props().overlay);
      });
      return;
    }
    this.host = new Host(); // t-053: this copy's host calls (host.js): their keys, limits and budget
    this.started = {}; // space id -> when its last load started (the 2 s window, COALESCE)
    this.deferred = {}; // space id -> a background reload came inside the window: one load at its end (the 1 s timer)
    this.data = null; // loadSpace() result for this.space (after the first paint, the cards and the rest are still coming)
    this.cards = []; // loadDecisions() result
    this.space = null;
    this.cardsFor = null; // the space this.cards belong to
    this.dataAt = 0; // when the data shown was loaded
    this.hovered = null; this.taskFocus = new Map(); this.taskFocusAsked = false; // t-142: the task under the pointer; task id -> its focus handles
    this.cache = new Map(); // space id -> { data, cards, rules, at }: its last finished load (most recently shown last)
    this.loads = {}; // space id -> { token, at }: the load in flight (at: its last progress)
    this.committed = {}; // space id -> the token of its last load that committed; an older one commits nothing
    this.seq = 0;
    this.hidden = {}; // space id -> { at, busy, seq }: the hidden panel's last count and ledger read
    this.wrote = {}; // space id -> the seq (start order) of the last read, full load or hidden, whose data went to its shared keys; an older one writes none
    this.again = {}; // space id -> a reload came while its load was in flight: one more when it ends (if the space is still shown)
    this.profiles = {}; // space id -> { at, busy, done, profile }: its profile, read once per session (a full load's counts)
    this.asleep = false; // not on screen: only the open-card count is read
    this.view = null; // "decisions" | "tasks" | "queue" | "rules" | "costs"; null: decisions when any is open
    this.viewShown = null; // the view the last render drew (this.view, or the one picked for null)
    this.rules = new RulesPane((options) => this.field(options));
    this.rules.host = this.host;
    this.rules.changed = (space, ids, cx) => this.rulesChanged(space, ids, cx); // t-101: the person's rule changes
    this.copier = new Copier(); // the Copy controls of cards and tasks (copy.js, t-026)
    // push cards: the last pa-push-main check and run of each (push.js); its runs take the shared mark (t-045 round 2)
    this.push = new PushCards((spaceId, id, label, running, unknown, note) => this.sharePush(spaceId, id, label, running, unknown, note), (spaceId, id, asked) => this.openedPush(spaceId, id, asked), this.host);
    // The Costs view: each space's last summary and billed comparison (costs.js); it reads and runs only while
    // this check holds (asked live: `asleep` is updated only by the next reload).
    this.costs = new CostsPane((id) => this.view === "costs" && spaces().find((s) => s.active)?.id === id && onScreen(), this.host);
    this.costsMarked = {}; // space id -> when this copy last stamped "project-agent.costsShown"
    this.open = new Set(); // expanded task ids
    this.folded = new Set(["done", "parked"]); // task sections folded away (finished work starts folded)
    this.older = false; // answered cards shown
    this.doneArchive = new Set(); // t-145: space ids whose done tasks past the 10 most recent are shown
    this.mergedOpen = new Set(); // t-145: `${space id}\0${repo path}`: a queue's merged entries past the 10 most recent shown
    this.archive = {}; // t-054: space id -> { open, busy, cards, error }: decisions-archive/, read only on a click
    this.output = {}; // `${task}:${label}` -> { text, code, running }
    this.armed = null; // arm() (rules.js): the task whose Delete was clicked once (a second click deletes); see armedTask()
    this.taskError = {}; // task id -> why the last change failed
    this.notes = {}; // noteKey(space id, card id) -> InputState (the optional note)
    this.choice = {}; // card id -> the option picked, not yet submitted
    this.cardScroll = ScrollHandle.new(); // the Decisions list, to bring one card into view
    this.spotlight = null; // card id reached from its task: drawn with an accent border
    this.busy = {}; // card id -> answering now
    // t-045: noteKey -> { label, note, at, state, before, after, why, seq }: a card answered from this copy, drawn
    // answered and never answerable again while the mark lasts. state: saving (the write in flight), saved, unsure
    // (the write threw and a re-read could not tell); seq: this.seq when the write ended (a load with a newer token
    // read after it). Dropped by resolveMarks (commit) once a read shows the answer or a later change, or on a failure.
    this.sent = {};
    this.failed = {}; // card id -> why the answer wasn't written
    // t-083 J2: the Places section of the Tasks view (open unless folded), a start-order write in flight per space, its
    // refusal line, and the line a task-row badge marks for 3 s ({ space, key, until }).
    this.placesFolded = false; this.placesBusy = {}; this.placesError = {}; this.placeMark = null;
    this.machines = {}; // t-119: space id -> { doc, error, at, asked, busy }: the suite machines' slots as last read
    // t-097 J2: a task-order.json write in flight per space, its refusal, and the follow-up folds the person opened.
    this.taskOrderBusy = {}; this.taskOrderError = {}; this.foldOpen = new Set();
    this.unsent = []; // [{ spaceId, message, why? }] notices the orchestrator hasn't had yet (its terminal is looked up when sending)
    this.opening = {}; // space id -> { at, before }: when this copy asked the keeper to open its Project Agent, and the record terminals then
    this.openMsg = {}; // space id -> why the last Open did not start anything
    this.confirmOpen = {}; // space id -> { tab, terminal }: a look-alike agent runs; asking first
    // t-037 J4, the Restart button: the confirm (its dry run's lines), this copy's run in flight, the result line.
    this.restartAsk = {}; // space id -> { busy, lines, refused, capable, instance, prompt, older }
    this.restarting = {}; // space id -> { at, instance }: this copy's one run, set before it starts (never twice)
    this.restartMsg = {}; // space id -> { ok, text, at }
    // t-041 J5, t-145: the fresh-contexts switch: this copy's run in flight, the result.
    this.contextBusy = {}; // space id -> { what: "ask" | "withdraw" | "mode" }
    this.contextMsg = {}; // space id -> { ok, text, at }
    this.me = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`; // this copy, for the timer lease
    this.schedule = { open: false, error: null, custom: null, quietFrom: null, quietTo: null }; // the schedule bar
    // The 15 s pass: one at a time (t-053: a tick still waiting on a call is not joined by another).
    this.ticking = false;
    const pass = (cx, then) => {
      if (this.ticking) return;
      this.ticking = true;
      const ok = this.host.spawn(cx, async (cx) => { try { await tick(this.me, this.host, cx); } finally { this.ticking = false; } then?.(cx); }, { what: "the timer's pass" });
      if (!ok) this.ticking = false;
    };
    cx.timer.every(15000, (cx) => pass(cx, (cx) => { if (!props()?.timer) this.reload(cx, BG); }));
    // The keeper (Project Agent tabs: tracking, ordering, reopening, the new-space dialog) runs in
    // the copy that holds the timer lease, so it runs once however many copies a layout has.
    this.keeper = new Keeper(this.me, this.host);
    const mine = () => shared("project-agent.timer")?.id === this.me;
    let keeping = false; // this copy held the lease last tick: read heartbeats and agent.json at once when it takes it
    pass(cx, (cx) => { if (mine()) { keeping = true; this.keeper.readBeats(cx); } });
    cx.timer.every(1500, (cx) => {
      if (!mine()) { keeping = false; return; }
      if (!keeping) { keeping = true; this.keeper.readBeats(cx); }
      this.keeper.syncPA(cx);
    });
    cx.timer.every(250, (cx) => { if (mine() && this.keeper.busy()) this.keeper.syncPA(cx); }); // while a restore runs
    cx.timer.every(400, () => { if (mine()) this.keeper.offerSetup(); });
    cx.timer.every(15000, (cx) => { if (mine()) this.keeper.readBeats(cx); });
    this.awake = new Awake(mine, this.host); // t-060: keep the Mac awake while Herdr runs (the lease holder only)
    cx.timer.every(1000, (cx) => this.awake.tick(cx));
    if (props()?.timer) return; // the always-on copy only keeps time; it shows nothing
    this.reload(cx);
    cx.timer.every(10000, (cx) => { this.reload(cx, BG); this.resend(cx); });
    cx.timer.every(1000, (cx) => { this.wake(cx); this.costsDue(cx); this.deferredDue(cx); if ((this.armed && !this.armedTask(this.space, this.armed.what ?? "delete")) || (this.rules.armed && !this.rules.armedRule(this.space))) cx.notify(); if (this.placeMark && Date.now() >= this.placeMark.until) { this.placeMark = null; cx.notify(); } }); // shown again (render may not run for a hidden copy); an armed Delete timed out; a badge's mark ended
    // Changes under spaces/ (one watch, D3): a batch reloads only for what the panel shows (watched()), coalesced.
    this.host.spawn(cx, async (cx) => {
      for (;;) {
        let paths;
        try { paths = await watch(`${paHome(null)}/spaces`, this.host); } catch { return; }
        if (this.watched(paths)) this.reload(cx, BG);
      }
    }, { what: "the folder watch", background: false });
  }

  // Whether a watch batch names something the panel shows (t-053 R4): the watched root itself ("refresh everything"),
  // or the shown local space's folder or a shown name under it. [] (a cancelled watch): nothing. A space on another
  // machine changes nothing here: its 10 s timer reads it.
  watched(paths) {
    if (!Array.isArray(paths) || !paths.length) return false;
    const root = `${paHome(null)}/spaces`, trim = (p) => String(p ?? "").replace(/\/+$/, "");
    if (paths.some((p) => trim(p) === root)) return true;
    const space = spaces().find((s) => s.active);
    if (!space || space.machine) return false;
    const dir = `${root}/${localId(space.id)}`;
    return paths.some((p) => { const x = trim(p); return x === dir || (x.startsWith(`${dir}/`) && SHOWN_PATH.test(x.slice(dir.length + 1))); });
  }

  // A background reload that waited for its space's 2 s window (COALESCE): from the 1 s timer, once the window ended.
  deferredDue(cx) {
    const space = spaces().find((s) => s.active);
    if (space && this.deferred[space.id] && Date.now() - (this.started[space.id] ?? 0) >= COALESCE) this.reload(cx, BG);
  }

  // The runtime refused (or the budget skipped) a call this copy needed: one plain line on what is shown (R6).
  showRefusal(space, cx) {
    const r = this.host.refusal;
    if (!r || this.space !== space.id || !this.data) return;
    this.data = { ...this.data, errors: [...(this.data.errors ?? []).filter((e) => !/^Too many calls waiting/.test(e)), refusalLine(r, this.dataAt)] };
    cx.notify();
  }

  // A load in flight for this space that is not yet given up: one of its reads settled or reached its
  // limit within LOAD_LIMIT. Every read is capped, so only a load whose read ignores its cap is given up.
  inFlight(id) { const l = this.loads[id]; return Boolean(l && Date.now() - l.at < LOAD_LIMIT); }

  // Shows a space's kept load at once (from reload, before anything is read). Note fields are made
  // in a task, never in render.
  adopt(space, cx) {
    const kept = this.cache.get(space.id);
    if (!kept) return;
    this.cache.delete(space.id); this.cache.set(space.id, kept); // now the most recently shown
    this.data = kept.data; this.cards = kept.cards; this.cardsFor = kept.cardsUnknown ? null : space.id; this.rules.data = kept.rules; this.space = space.id; this.dataAt = kept.at;
    cx.spawn(async (cx) => { this.syncNotes(space, kept.cards, false); cx.notify(); });
  }

  // One note field per open card of the space, made in a task (never in render). release: the
  // space's fields whose card is not open in `cards` are dropped (only from a list that answered).
  syncNotes(space, cards, release) {
    const open = new Set((cards ?? []).filter((c) => c.card?.status === "open").map((c) => noteKey(space.id, c.id)));
    if (release) for (const k of Object.keys(this.notes)) if (k.startsWith(noteKey(space.id, "")) && !open.has(k)) { this.notes[k].release?.(); delete this.notes[k]; }
    // change: Submit for Other follows the typed words (t-046); the placeholder cannot change once made (the hint says it).
    for (const k of open) if (!this.notes[k]) { this.notes[k] = this.field({ placeholder: NOTE }); this.notes[k].on?.("change", (_e, cx) => cx.notify()); }
    return open.size;
  }

  // Shown again after a time off screen: what it has shows at once; one full reload starts.
  wake(cx) {
    if (!this.asleep || !onScreen()) return;
    this.asleep = false;
    this.reload(cx, BG);
  }

  // While hidden: the open-card count on the panel's button (shared "project-agent.open") from one
  // card read, and the ledger, read together, per space at most every 10 s (d-039). From the ledger
  // the shared keys other views use are refreshed (share(): the worktrees, the workers, the Files
  // shortcuts with the evidence of the last full load), and the main branches from the kept load's
  // profile; with none kept, the profile is read once per session (readProfile). A failed read
  // changes nothing; one that ends after a newer one wrote, nothing.
  hiddenRead(cx, space) {
    if (!this.knownProfile(space.id)) this.readProfile(cx, space);
    const h = this.hidden[space.id], now = Date.now();
    if (h && (now - h.at < COUNT_EVERY || (h.busy && now - h.at < LOAD_LIMIT))) return;
    const mine = { at: now, busy: true, seq: ++this.seq };
    this.hidden[space.id] = mine;
    const ok = this.host.spawn(cx, async (cx) => {
      const rcx = this.host.ctx(cx, { origin: "hidden" }), out = {}; // keyed, capped (host.js)
      const [cards, ledger] = await Promise.all([
        loadDecisions(space, { cx: rcx, batchOnly: true, out }).catch(() => null),
        loadLedger(space, rcx).catch(() => null),
      ]);
      mine.busy = false;
      if (!this.asleep || mine.seq < (this.wrote[space.id] ?? 0)) return;
      if (out.busy && ledger?.busy) return; // t-053: both skipped (the last read still pending): this read read nothing, so it hides no older one
      this.wrote[space.id] = mine.seq;
      if (cards) {
        const open = cards.filter((x) => x.card?.status === "open").length;
        const counts = { ...(shared("project-agent.open") ?? {}) };
        if ((counts[space.id] ?? 0) !== open) { if (open) counts[space.id] = open; else delete counts[space.id]; setShared("project-agent.open", counts); }
      }
      if (ledger && !ledger.timedOut && !ledger.error && ledger.value && typeof ledger.value === "object") {
        const known = this.cache.get(space.id)?.data ?? (this.space === space.id ? this.data : null);
        this.share(space, { ledger: ledger.value, evidence: known?.evidence, profile: known?.profile }, { mains: Boolean(known?.agent && !(known.unknown ?? []).includes("profile")) });
      }
    }, { what: "the hidden panel's read" });
    if (!ok) mine.busy = false;
  }

  // The archive (t-054): old answered cards pa-task archive moved to decisions-archive/. Never read on a reload; each
  // click that opens it reads it once (batch only), newest first, at most 50 shown.
  // t-145: one row, "Archived (n)" (n: the finished cards past the 10 shown); opened, it draws them and the archive's.
  archiveLine(t, space, rest = 0) {
    const a = this.archive[space.id];
    const said = !a?.open ? "" : a?.busy ? " · reading…" : a?.error ? ` · ${a.error}` : a?.cards ? ` · ${a.cards.length} in decisions-archive${a.cards.omitted ? ` (${a.cards.omitted} older not shown)` : ""}` : "";
    return div().id("pa-archive").px(4).py(4).text_size(11.5).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.foreground))
      .on_click((_e, cx) => { this.toggleArchive(space, cx); cx.notify(); }).child(`${a?.open ? "▾" : "▸"} Archived (${rest})${said}`);
  }
  toggleArchive(space, cx) {
    const a = (this.archive[space.id] ??= { open: false, busy: false, cards: null, error: null });
    a.open = !a.open;
    if (!a.open || a.busy) return;
    a.busy = true;
    const ok = this.host.spawn(cx, async (cx) => {
      const out = {};
      let r = null;
      try { r = await loadDecisions(space, { cx: this.host.ctx(cx, { origin: "archive", background: false }), batchOnly: true, folder: "decisions-archive", out }); } catch {}
      a.busy = false;
      if (r) { a.cards = r; a.error = null; } else if (!out.busy) a.error = "could not read it now";
      cx.notify();
    }, { what: "the archive read", background: false });
    if (!ok) { a.busy = false; a.error = "could not read it now"; }
  }

  // While hidden with no kept load whose profile answered: the space's profile, for "project-agent.mainBranches". Read once
  // per session once it has answered; a failed or timed-out read is tried again after PROFILE_RETRY.
  readProfile(cx, space) {
    const p = this.profiles[space.id] ??= {}, now = Date.now();
    if (p.done || p.busy || (p.at && now - p.at < PROFILE_RETRY)) return;
    p.at = now; p.busy = true;
    const ok = this.host.spawn(cx, async (cx) => {
      let r = { answered: false };
      try { r = await loadProfile(space, props()?.profile, this.host.ctx(cx, { origin: "profile" })); } catch {}
      p.busy = false;
      if (!r.answered || p.done) return;
      p.done = true; p.profile = r.profile;
      if (this.asleep && r.profile !== undefined && !this.knownProfile(space.id)) this.shareMains(space, r.profile);
    }, { what: "the profile read" });
    if (!ok) p.busy = false;
  }

  // Whether the space's kept load has a profile that answered (else the hidden panel reads it itself).
  knownProfile(id) { const d = this.cache.get(id)?.data; return Boolean(d?.agent && !(d.unknown ?? []).includes("profile")); }

  // Keeps a finished load of a space (the most recently shown last; at most CACHE_MAX).
  keep(id, entry) {
    this.cache.delete(id); this.cache.set(id, entry);
    while (this.cache.size > CACHE_MAX) this.cache.delete(this.cache.keys().next().value);
  }

  // Loads the shown space: agent.json, then the header's reads together (first paint), then the
  // cards, rules, evidence and stale instructions together; then commits (commit()). One load per
  // space in flight; one with no progress for LOAD_LIMIT is given up and a new one may start. A load
  // that ends commits unless a newer load of its space has committed (this.committed). The last
  // finished load of each space is kept and shown at once when the space is shown again. A read
  // that times out or fails never stands for "none": its part keeps the last known value (the kept
  // entry's, else what is shown for the space), or stays unknown, and an unknown part writes none of
  // the shared keys it feeds.
  // t-053: { background: true } (the timers, the watch, wake): at most one load per space per COALESCE, the rest deferred
  // to the window's end; its reads skip a call still pending (BUSY: quietly). A reload from a click or a write starts at
  // once, and its reads wait for a call still pending of their kind, then read again.
  reload(cx, { background = false } = {}) {
    const space = spaces().find((s) => s.active);
    if (!space) { this.data = null; this.space = null; cx.notify(); return; }
    if (space.id !== this.space) { this.disarm(); this.adopt(space, cx); }
    if (!onScreen()) { this.asleep = true; this.hiddenRead(cx, space); return; }
    this.asleep = false;
    if (this.view === "costs") this.costsShown(space, cx); // its own read, outside the load (costs.js); never while hidden
    this.machinesWant(space, cx); // t-119: its own read, outside the load; only while Places is seen
    if (this.inFlight(space.id)) { this.again[space.id] = true; return; }
    const at0 = Date.now();
    if (background && at0 - (this.started[space.id] ?? -Infinity) < COALESCE) { this.deferred[space.id] = true; return; }
    delete this.deferred[space.id];
    this.started[space.id] = at0;
    const token = ++this.seq, load = { token, at: at0 };
    this.loads[space.id] = load;
    const current = () => this.loads[space.id] === load;
    const shown = () => spaces().find((s) => s.active)?.id === space.id;
    const started = this.host.spawn(cx, async (cx) => {
      const rcx = this.host.ctx(cx, { origin: "load", background, wait: !background, progress: () => { load.at = Date.now(); } }); // the store's reads move `at`
      const cardsOut = {}; // cardsOut.busy: the card read was skipped (a call of its kind still pending): no "did not answer" line
      let data, cards = [], rules = null, missed = []; // missed: the parts that did not answer (shown from the last known value, written nowhere)
      const kept = this.cache.get(space.id);
      const shownNow = () => (this.space === space.id && this.data?.agent ? this.data : null); // what is shown for this space, if it is
      const lastKnown = (part) => [kept?.data, shownNow()].find((d) => d?.agent && !(d.unknown ?? []).includes(part)) ?? null;
      try {
        data = await loadSpace(space, props()?.profile, rcx);
        if (data.agent) { // a part that did not answer: its last known value, else it stays unknown (its error line stays)
          const unknown = []; missed = [...(data.unknown ?? [])];
          for (const part of data.unknown ?? []) { const src = lastKnown(part); if (src) for (const f of PARTS[part]) data[f] = src[f]; else unknown.push(part); }
          data.unknown = unknown;
        }
        if (data.agent && current() && shown()) { // first paint: the header and the tasks
          const same = this.space === space.id;
          this.data = { ...data, evidence: same ? this.data?.evidence : undefined, stale: same ? this.data?.stale : undefined, code: same ? this.data?.code : undefined, context: same ? this.data?.context : undefined, queue: same ? this.data?.queue : undefined, waiting: same ? this.data?.waiting : undefined, startOrder: same ? this.data?.startOrder : undefined, taskOrder: same ? this.data?.taskOrder : undefined };
          if (this.cardsFor !== space.id) { this.cards = []; this.cardsFor = null; this.rules.data = null; }
          if (!same) this.dataAt = Date.now();
          this.space = space.id;
          cx.notify();
        }
        if (data.agent) {
          // A read that times out or fails keeps the last known value: the kept load's, else what is shown for the space.
          const cap = (p, fallback) => p.then((v) => (gaveUp(v) ? fallback() : v), () => fallback()); // each read capped in the store
          const known = [...(kept && !kept.cardsUnknown ? kept.cards : []), ...(this.space === space.id && this.cardsFor === space.id ? this.cards : [])];
          let evidence, stale, code, queue, waiting, startOrder, context, taskOrder;
          [cards, rules, evidence, stale, code, queue, waiting, startOrder, context, taskOrder] = await Promise.all([
            loadDecisions(space, { cx: rcx, live: current, known, out: cardsOut }),
            cap(loadRules(space, rcx), () => kept?.rules ?? (this.space === space.id && this.cardsFor === space.id ? this.rules.data : null) ?? null), // null: the Rules view says Loading…
            cap(evidenceTasks(space, rcx), () => kept?.data?.evidence ?? shownNow()?.evidence), // undefined: unknown
            cap(staleInstructions(space, data.agent.profile, rcx), () => kept?.data?.stale ?? shownNow()?.stale ?? []),
            cap(loadCode(space, rcx), () => kept?.data?.code ?? shownNow()?.code ?? null), // t-037 J4: launcher.json + installed.json
            cap(loadQueue(space, rcx), () => kept?.data?.queue ?? shownNow()?.queue ?? null), // t-055: queue.json; a give-up keeps the last known (null: "Loading queue…")
            cap(loadWaiting(space, rcx), () => kept?.data?.waiting ?? shownNow()?.waiting ?? null), // t-083 J2: waiting.json (pa-places writes it); as loadQueue
            cap(loadStartOrder(space, rcx), () => kept?.data?.startOrder ?? shownNow()?.startOrder ?? null), // t-083 J2: start-order.json's text (the panel writes it)
            cap(loadContext(space, rcx), () => kept?.data?.context ?? shownNow()?.context ?? null), // t-041 J5: context.json (no costs file: t-009 R11)
            cap(loadTaskOrder(space, rcx), () => kept?.data?.taskOrder ?? shownNow()?.taskOrder ?? null), // t-097 J2: task-order.json's text (the panel writes it)
          ]);
          data.evidence = evidence; data.stale = stale; data.code = code; data.queue = queue; data.waiting = waiting; data.startOrder = startOrder; data.context = context; data.taskOrder = taskOrder;
        }
      }
      catch (e) { data = { agent: null, errors: [String(e.message ?? e)], failed: true }; } // a read in the first round threw
      const wasCurrent = current();
      if (wasCurrent) delete this.loads[space.id];
      // t-053: agent.json skipped (its last read still pending): nothing commits, nothing is said; the next trigger reads.
      if (data.busy) { if (wasCurrent && this.again[space.id]) { delete this.again[space.id]; if (shown()) this.reload(cx); } return; }
      // The runtime refused a call of this load (or the budget skipped one): one plain line, never its own text (R6).
      const refused = this.host.refusalSince(at0);
      if (refused) data.errors = [...(data.errors ?? []).filter((e) => !/^Too many calls waiting/.test(e)), refusalLine(refused, this.space === space.id ? this.dataAt : null)];
      // Given up with its card fallback skipped (no result), or a newer load of this space has committed.
      if (!((cards === null && !wasCurrent) || token < (this.committed[space.id] ?? 0))) this.commit(space, token, data, cards, rules, cx, missed, Boolean(cardsOut.busy));
      if (wasCurrent && this.again[space.id]) { delete this.again[space.id]; if (shown()) this.reload(cx); }
    }, { what: "the panel's load", background });
    if (!started) { if (current()) delete this.loads[space.id]; this.showRefusal(space, cx); }
  }

  // t-119 R3: the suite machines' slots (store.js loadMachines), read only while the Tasks view shows Places unfolded on
  // screen, at most every MACHINES_EVERY and one at a time per space. A give-up keeps what is shown; an error keeps the
  // last good answer with the error.
  machinesWant(space, cx) {
    if (this.view !== "tasks" || this.placesFolded || !onScreen()) return;
    const m = (this.machines[space.id] ??= { doc: null, error: null, at: 0, asked: 0, busy: false }), now = Date.now();
    if (m.busy || (m.asked && now - m.asked < MACHINES_EVERY)) return;
    m.busy = true; m.asked = now;
    const started = this.host.spawn(cx, async (cx) => {
      try {
        const r = await loadMachines(space, this.host.ctx(cx, { origin: "machines", background: true }));
        if (!gaveUp(r)) { if (r.error) m.error = r.error; else { m.doc = r.doc; m.error = null; m.at = Date.now(); } }
      } catch (e) { m.error = String(e?.message ?? e); }
      finally { m.busy = false; cx.notify(); }
    }, { what: "the suite machines read" });
    if (!started) m.busy = false;
  }

  // The Costs view chosen: a reread or a billed compare asked for while it was left starts once it is shown (costs.js due()).
  costsDue(cx) {
    if (this.view !== "costs") return;
    const space = spaces().find((s) => s.active);
    if (space) this.costs.due(space, cx);
  }

  // The Costs view is shown for `space` in this shown copy: its summary (at most every 10 s, costs.js), and the
  // shared stamp the timer reads to collect every 120 s instead of every 30 min (in memory, not a file).
  costsShown(space, cx, force = false) {
    this.costs.want(space, cx, force);
    const now = Date.now();
    if (now - (this.costsMarked[space.id] ?? 0) < COSTS_MARK) return;
    this.costsMarked[space.id] = now;
    setShared("project-agent.costsShown", { ...(shared("project-agent.costsShown") ?? {}), [space.id]: now });
  }

  // A finished load of `space` (newest so far). Branches:
  // - nothing answered (agent.json timed out, or a first-round read threw): if shown, what is kept or
  //   shown for the space stays, with the error ("did not answer" with neither); nothing is kept,
  //   committed or written;
  // - cards null (the listing never answered): the last known cards, else unknown (no count written,
  //   no note field dropped, "Loading decisions…");
  // - a part in `missed` (its read did not answer; shown from the last known value) writes none of the
  //   shared keys it feeds, and the kept entry keeps its own value for it (else it is unknown there);
  //   cards that did not answer write no count and leave the kept cards as they were;
  // - not shown: only kept (for the way back); nothing drawn or written;
  // - shown: drawn, kept, and the shared keys written unless a newer read of the space wrote them.
  commit(space, token, data, cards, rules, cx, missed = [], quiet = false) {
    const kept = this.cache.get(space.id), same = this.space === space.id;
    const shown = spaces().find((s) => s.active)?.id === space.id;
    if (data.timedOut || data.failed) {
      if (!shown) return;
      const prior = kept ?? (same && this.data?.agent ? { data: this.data, cards: this.cards, cardsUnknown: this.cardsFor !== space.id, rules: this.rules.data, at: this.dataAt } : null);
      if (prior) {
        this.data = { ...prior.data, errors: [...data.errors] }; this.cards = prior.cards; this.cardsFor = prior.cardsUnknown ? null : space.id; this.dataAt = prior.at;
        if (prior.rules) this.rules.data = prior.rules;
        this.syncNotes(space, prior.cards, false);
      } else { this.data = data; this.cards = []; this.cardsFor = null; this.dataAt = Date.now(); }
      this.space = space.id;
      cx.notify();
      return;
    }
    let cardsUnknown = false;
    const listed = cards !== null;
    if (!listed) {
      const src = kept && !kept.cardsUnknown ? kept.cards : same && this.cardsFor === space.id ? this.cards : null;
      if (src) cards = src; else { cards = []; cardsUnknown = true; }
      if (!quiet) data.errors = [...(data.errors ?? []), src ? "decision cards: did not answer; the last known are shown" : "decision cards: did not answer"]; // quiet: skipped (BUSY), t-053
    }
    this.committed[space.id] = token;
    if (listed) this.resolveMarks(space, token, cards); // t-045
    // The kept entry takes only what answered: a missed part keeps the kept entry's value, else is unknown there.
    const keptKnows = (part) => Boolean(kept?.data?.agent && !(kept.data.unknown ?? []).includes(part));
    const keptData = { ...data, unknown: [...(data.unknown ?? []), ...missed.filter((p) => !keptKnows(p) && !(data.unknown ?? []).includes(p))] };
    const keptCards = listed ? { cards, cardsUnknown } : kept && !kept.cardsUnknown ? { cards: kept.cards, cardsUnknown: false } : { cards: [], cardsUnknown: true };
    if (!shown) { this.keep(space.id, { data: keptData, ...keptCards, rules: rules ?? kept?.rules ?? null, at: Date.now() }); return; }
    this.data = data; this.cards = cards; this.space = space.id; this.cardsFor = cardsUnknown ? null : space.id; this.dataAt = Date.now();
    if (rules) this.rules.data = rules;
    this.keep(space.id, { data: keptData, ...keptCards, rules: rules ?? this.rules.data, at: this.dataAt });
    if (data.agent && !keptData.unknown.includes("profile")) this.profiles[space.id] = { done: true, profile: data.profile ?? null }; // read once per session
    for (const c of cards) this.push.wantCheck(space, c, cx); // an open push card's first check (push.js)
    if (this.view === "rules") this.rules.wantAllow(space, cx); // the Rules view's push allowlist, at most every 30 s
    const open = listed ? this.syncNotes(space, cards, true) : null; // cards that did not answer write no count
    if (!listed) this.syncNotes(space, cards, false);
    if (token > (this.wrote[space.id] ?? 0)) { this.wrote[space.id] = token; this.share(space, data.agent ? { ...data, unknown: [...(data.unknown ?? []), ...missed] } : { ...data, evidence: new Map() }, { open }); }
    cx.notify();
  }

  // The shared keys other views read, for one space, each written only when it changed: its
  // tasks' worktrees, the Files shortcuts, the profile's main branches (writeMains), its workers,
  // and the open-card count (open; null leaves it). From a full load of the shown space, or from the
  // ledger alone while hidden (hiddenRead).
  shareMains(space, profile) {
    const mains = Array.isArray(profile?.mainBranches) ? profile.mainBranches : [];
    const allMains = { ...(shared("project-agent.mainBranches") ?? {}) };
    if (JSON.stringify(allMains[space.id] ?? []) !== JSON.stringify(mains)) { if (mains.length) allMains[space.id] = mains; else delete allMains[space.id]; setShared("project-agent.mainBranches", allMains); }
  }

  // A part of `data` that is unknown (data.unknown: its read did not answer in this load, whether or
  // not it is shown from the last known value) writes none of the keys it feeds: the ledger feeds the worktrees, the Files shortcuts and the
  // workers; the profile, the main branches.
  share(space, data, { open = null, mains: writeMains = true } = {}) {
    const unknown = data.unknown ?? [];
    if (writeMains && !unknown.includes("profile")) this.shareMains(space, data.profile);
    if (!unknown.includes("ledger")) this.shareLedger(space, data);
    if (open !== null) {
      const counts = { ...(shared("project-agent.open") ?? {}) };
      if ((counts[space.id] ?? 0) !== open) { if (open) counts[space.id] = open; else delete counts[space.id]; setShared("project-agent.open", counts); }
    }
  }

  shareLedger(space, data) {
    // Tell other mods (a Changes view, say) which worktrees this space's tasks use.
    // Every recorded, unremoved worktree (t-020 AC9.1): the main one exactly { task, path, title } as before, the others with their job.
    const trees = (data.ledger?.tasks ?? []).filter((x) => x?.id).flatMap((x) => taskTrees(x).map((w) => ({ task: x.id, path: w.path, title: String(x.title ?? ""), ...(w.main ? {} : { job: w.job }) })));
    const allTrees = { ...(shared("project-agent.worktrees") ?? {}) };
    if (JSON.stringify(allTrees[space.id] ?? []) !== JSON.stringify(trees)) { if (trees.length) allTrees[space.id] = trees; else delete allTrees[space.id]; setShared("project-agent.worktrees", allTrees); }
    // Folders worth a shortcut in a Files view (shared "my-files.places": { space: [{ label, path, group }] }):
    // all evidence, each task's evidence (newest task first), each task's worktree.
    const dir = stateDir(space), byNewest = [...(data.ledger?.tasks ?? [])].sort((x, y) => String(y.updated ?? y.created ?? "").localeCompare(String(x.updated ?? x.created ?? "")));
    // Evidence unknown (its read timed out, or not read while hidden): the evidence shortcuts the key has stay.
    const had = (shared("my-files.places") ?? {})[space.id] ?? [], known = data.evidence instanceof Map;
    const hadAt = (path) => (Array.isArray(had) ? had : []).find((x) => x?.path === path);
    const spots = known ? (data.evidence.size ? [{ label: "All evidence", path: `${dir}/evidence`, group: "Project Agent" }] : []) : [hadAt(`${dir}/evidence`)].filter(Boolean);
    for (const x of byNewest.slice(0, 15)) {
      const files = known && data.evidence.get(x.id);
      if (files) spots.push({ label: `${x.id} evidence · ${files.length} ${files.length === 1 ? "file" : "files"}`, detail: String(x.title ?? ""), path: `${dir}/evidence/${x.id}`, group: "Project Agent", task: x.id });
      else if (!known && hadAt(`${dir}/evidence/${x.id}`)) spots.push(hadAt(`${dir}/evidence/${x.id}`));
      for (const w of taskTrees(x)) spots.push(w.main ? { label: `${x.id} worktree`, detail: String(x.title ?? ""), path: w.path, group: "Project Agent", task: x.id }
        : { label: `${x.id} worktree · ${w.job}`, detail: String(x.title ?? ""), path: w.path, group: "Project Agent", task: x.id, job: w.job });
    }
    const allSpots = { ...(shared("my-files.places") ?? {}) };
    if (JSON.stringify(allSpots[space.id] ?? []) !== JSON.stringify(spots)) { if (spots.length) allSpots[space.id] = spots; else delete allSpots[space.id]; setShared("my-files.places", allSpots); }
    // And which task each worker belongs to (an Agents view can link a worker back to its task).
    const crew = {};
    for (const x of data.ledger?.tasks ?? []) for (const w of x.workers ?? []) if (w.address) crew[space.machine ? `${space.machine}/${w.address}` : w.address] = { task: x.id, title: String(x.title ?? ""), role: w.role ?? null, state: x.state ?? null };
    const allCrew = { ...(shared("project-agent.workers") ?? {}) };
    if (JSON.stringify(allCrew[space.id] ?? {}) !== JSON.stringify(crew)) { if (Object.keys(crew).length) allCrew[space.id] = crew; else delete allCrew[space.id]; setShared("project-agent.workers", allCrew); }
  }

  // "Open Project Agent": asks the keeper (shared request), which opens a new tab running the
  // launcher and records it. A live agent that looks like this one gets a confirm first.
  requestOpen(space, agent, cx, anyway) {
    delete this.openMsg[space.id];
    const like = !anyway && lookAlike(space, agent);
    if (like) { this.confirmOpen[space.id] = like; cx.notify(); return; }
    delete this.confirmOpen[space.id];
    const at = Date.now();
    setShared(OPEN_REQUESTS, { ...(shared(OPEN_REQUESTS) ?? {}), [space.id]: { at, by: "panel" } });
    this.opening[space.id] = { at, before: paRecords(space.id).map((r) => r.terminal) };
    cx.notify();
  }

  openButton(t, space, agent, id, waiting) {
    const el = div().id(id).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(waiting ? t.border : t.accent).text_size(11.5)
      .text_color(waiting ? t.dim : t.foreground);
    return waiting ? el.child("Opening…") : el.cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.requestOpen(space, agent, cx, false)).child("Open Project Agent");
  }

  // ---- t-037 J4: Restart (R13). Only the restart tool in place, on the space's machine; never a terminal ended here. ----
  // What the launcher runs (launcher.json vs installed.json), from this space's last load, or null.
  codeOf(space, data) {
    const c = data?.code;
    return c && typeof c === "object" && "launcher" in c ? codeState(c.launcher, c.installed, data.agent?.terminal ?? null) : null;
  }

  // Another panel window's run for this space, while its mark is fresh.
  restartElsewhere(space) {
    const m = (shared(RESTART_MARK) ?? {})[space.id];
    return Boolean(m && m.by !== this.me && typeof m.at === "number" && Date.now() - m.at < RESTART_MARK_TTL);
  }
  restartBusy(space) { return Boolean(this.restarting[space.id]) || this.restartElsewhere(space); }
  shareRestart(space, mark) {
    const all = { ...(shared(RESTART_MARK) ?? {}) };
    if (mark) all[space.id] = mark;
    else if (all[space.id]?.by === this.me) delete all[space.id];
    else return;
    setShared(RESTART_MARK, all);
  }

  // The button: asks first (a dry run). A launcher from before restart support says what Restart means for it.
  restartButton(t, space, code, id) {
    const label = code && !code.capable ? "Restart (ends workers)" : "Restart";
    return div().id(id).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(t.border).text_size(11.5)
      .text_color(t.foreground).cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.askRestart(space, cx)).child(label);
  }

  // The confirm: one dry run of the tool (read-only: every identification check, the summary), its lines shown.
  askRestart(space, cx) {
    if (this.restartBusy(space) || this.restartAsk[space.id]?.busy) return;
    const code = this.space === space.id ? this.codeOf(space, this.data) : null;
    const ask = { busy: true, lines: [], refused: false, capable: code ? code.capable : true, instance: code?.instance ?? null, prompt: code?.prompt ?? null, older: code?.word ?? null };
    this.restartAsk[space.id] = ask;
    delete this.restartMsg[space.id];
    cx.notify();
    const started = this.host.spawn(cx, async (cx) => {
      const res = await runRestart(this.host.ctx(cx, { origin: "restart", background: false }), space, restartArgs(space, { dry: true }), DRY_TIMEOUT);
      if (this.restartAsk[space.id] !== ask) return; // cancelled meanwhile
      if (res.r && res.r.code === 0) ask.lines = String(res.r.stdout ?? "").split("\n").filter((l) => l.trim() && !/^Dry run/.test(l));
      else { ask.refused = true; ask.lines = [res.r ? String(res.r.stderr || res.r.stdout || `exit ${res.r.code}`).trim() : restartOutcome(res, space)?.text ?? "The check did not answer; try again."]; }
      if (ask.refused && /started before restart support/.test(ask.lines.join(" "))) ask.capable = false;
      ask.busy = false;
      cx.notify();
    }, { what: "the restart check", background: false });
    if (!started) { ask.busy = false; ask.refused = true; ask.lines = ["Too many calls waiting; try again."]; cx.notify(); }
  }

  // Confirmed: one run bound to the instance the confirm showed. this.restarting and the shared mark are set before it
  // starts, so a second click (here or in another window) runs nothing.
  confirmRestart(space, { now = false, fresh = false }, cx) {
    const ask = this.restartAsk[space.id];
    if (this.restartBusy(space) || !ask || ask.busy || ask.refused || !ask.capable) return;
    const args = restartArgs(space, { instance: ask.instance, now, fresh });
    if (!args) { ask.refused = true; ask.lines = ["Its launcher.json names no instance: not restarted. Try again after the next refresh."]; cx.notify(); return; }
    delete this.restartAsk[space.id];
    const mark = { at: Date.now(), instance: ask.instance };
    this.restarting[space.id] = mark;
    this.shareRestart(space, { by: this.me, at: mark.at, instance: ask.instance });
    cx.notify();
    const end = (msg, cx) => {
      if (this.restarting[space.id] === mark) delete this.restarting[space.id];
      this.shareRestart(space, null);
      if (msg) this.restartMsg[space.id] = { ...msg, at: Date.now() };
      cx.notify();
    };
    const started = this.host.spawn(cx, async (cx) => {
      let msg;
      try { msg = restartOutcome(await runRestart(this.host.ctx(cx, { origin: "restart", background: false }), space, args, RESTART_TIMEOUT), space); }
      catch (e) { msg = { ok: false, text: `The restart could not run: ${String(e?.message ?? e)}` }; }
      end(msg, cx);
      this.reload(cx);
    }, { what: "the restart", background: false });
    if (!started) end({ ok: false, text: "The restart was not started: too many calls waiting. Try again." }, cx);
  }

  // The confirm box, the run in flight, or the result.
  restartBox(t, space, live) {
    const ask = this.restartAsk[space.id], box = () => v_flex().px(12).py(8).gap(4).flex_none().border_b(1).border_color(t.border).text_size(11.5).text_color(t.foreground);
    if (this.restarting[space.id]) return box().bg(t.working + "1a").child(div().child(["working", "blocked"].includes(live?.status) ? "Restarting… (waiting for its turn to end)" : "Restarting…"));
    if (this.restartElsewhere(space)) return box().child(div().child("Restarting… (from another panel window)"));
    if (ask) {
      const b = box().bg(t.working + "1a");
      if (ask.busy) return b.child(div().text_color(t.dim).child("Checking what a restart does…"));
      const lines = b.children(ask.lines.map((l, i) => div().child(sel(`pa-restart-line-${i}`, l))));
      if (ask.refused) {
        const line = endTerminalLine(space);
        return lines
          .when(!ask.capable, (e) => e.child(div().text_color(t.dim).child("The panel never ends a terminal. When its workers are done, run this in a terminal (it asks before it ends anything):"))
            .child(h_flex().gap(6).items_center().child(div().flex_1().min_w(0).child(sel("pa-restart-end-line", line))).child(this.copier.button(t, "copy-end-terminal", () => line))))
          .child(h_flex().gap(6).child(this.button(t, "pa-restart-cancel", "Cancel", (_e, cx) => { delete this.restartAsk[space.id]; cx.notify(); })));
      }
      const fresh = ask.prompt === "recorded" || /core/.test(String(ask.older ?? ""));
      return lines
        .child(div().text_color(t.dim).child(`Restart: when its current turn ends. Restart now: its current turn is cut off.${fresh ? " A new conversation loads the current core text; the previous one stays in /resume." : ""}`))
        .child(h_flex().gap(6)
          .child(this.button(t, "pa-restart-go", "Restart", (_e, cx) => this.confirmRestart(space, {}, cx), true))
          .child(this.button(t, "pa-restart-now", "Restart now", (_e, cx) => this.confirmRestart(space, { now: true }, cx)))
          .when(fresh, (e) => e.child(this.button(t, "pa-restart-fresh", "Restart in a new conversation", (_e, cx) => this.confirmRestart(space, { fresh: true }, cx))))
          .child(this.button(t, "pa-restart-cancel", "Cancel", (_e, cx) => { delete this.restartAsk[space.id]; cx.notify(); })));
    }
    const m = this.restartMsg[space.id];
    if (m && (!m.ok || Date.now() - m.at < RESTARTED_FOR)) return box().child(div().text_color(m.ok ? t.foreground : t.blocked).child(sel("pa-restart-result", m.text)));
    return null;
  }

  // ---- t-041 J5, t-145: the fresh-contexts switch (R13) in the head row; the context line (R15) and the reset line (C6)
  // in the Costs tab. Only the context tool on the space's machine (store.runContext), with --by panel; the panel writes
  // neither context-request.json nor context.json. One run per space at a time in this copy. ----
  contextClock(iso) { const s = atZone(iso); return /^\d\d-\d\d \d\d:\d\d$/.test(s) ? s.slice(6) : "?"; }

  // One run that writes the mode, then its result and a reload.
  contextRun(space, what, args, cx) {
    if (this.contextBusy[space.id] || !args) return;
    delete this.contextMsg[space.id];
    const mark = { what };
    this.contextBusy[space.id] = mark;
    cx.notify();
    const end = (msg, cx) => {
      if (this.contextBusy[space.id] === mark) delete this.contextBusy[space.id];
      if (msg) this.contextMsg[space.id] = { ...msg, at: Date.now() };
      cx.notify();
    };
    const started = this.host.spawn(cx, async (cx) => {
      let msg;
      try { msg = contextOutcome(await runContext(this.host.ctx(cx, { origin: "context", background: false }), space, args), space, (iso) => this.contextClock(iso)); }
      catch (e) { msg = { ok: false, text: `The switch could not run: ${String(e?.message ?? e)}` }; }
      end(msg, cx);
      this.reload(cx);
    }, { what: "the context mode switch", background: false });
    if (!started) end({ ok: false, text: "Not run: too many calls waiting. Try again." }, cx);
  }

  // The switch (t-145): ↻, on while the mode is fresh; its state, what it does and an invalid context.json in its
  // tooltip; the last switch's line there too for CONTEXT_FOR. null while context.json is not read or the machine is offline.
  contextToggle(t, space, data, known) {
    const c = data?.context;
    if (!c || typeof c !== "object" || known === "offline") return null;
    const s = c.settings ?? contextSettings(null), busy = this.contextBusy[space.id], m = this.contextMsg[space.id];
    const on = s.mode === "fresh";
    const said = m?.ok && Date.now() - m.at < CONTEXT_FOR ? ` ${m.text}` : "";
    const tip = busy ? "Fresh contexts: switching…" : `Fresh contexts: ${on ? "on" : "off"}. ${on ? "The Project Agent starts a new conversation at a clean point when its context grows large; the previous one stays in /resume." : "No fresh contexts are taken."} Click to turn them ${on ? "off" : "on"}.${s.problem ? ` context.json is not valid (${s.problem}): the launcher uses the defaults.` : ""}${said}`;
    return div().id("pa-context-mode").flex_none().w(22).h(20).flex().items_center().justify_center().rounded(5).text_size(12)
      .bg(on ? t.selected : t.chrome).border(1).border_color(s.problem ? t.blocked : on ? t.accent : t.border).text_color(on && !busy ? t.foreground : t.dim)
      .when(!busy, (b) => b.cursor_pointer().hover((x) => x.bg(t.hover)).on_click((_e, cx) => this.contextRun(space, "mode", contextArgs(space, { mode: on ? "off" : "fresh" }), cx)))
      .tooltip(tip).child("↻");
  }

  // A switch that failed: one line under the head (until the next switch).
  contextFailed(t, space) {
    const m = this.contextMsg[space.id];
    if (!m || m.ok) return null;
    return div().px(12).py(6).flex_none().border_b(1).border_color(t.border).text_size(11.5).text_color(t.blocked).child(sel("pa-context-result", m.text));
  }

  // The top of the Costs tab: the context line from the Costs view's last read (J5-D1) and the reset line with its Copy.
  costsContext(t, space, data, known) {
    const c = data?.context;
    if (!c || typeof c !== "object" || known === "offline") return null;
    const launcher = data.code?.launcher ?? null, clock = (iso) => this.contextClock(iso), costs = this.costs.peek(space.id);
    // The day of the summary's figures (its updated, else the read: MF-J5r2-1), not today's (MF-J5-1); an earlier day
    // is named, and so is the date in its "as of". One time gives both the day and the "as of".
    const now = Date.now(), at = costs && costs.readAt ? contextAt(costs.summary, costs.readAt) : null;
    const read = at !== null ? contextLine(costs.summary?.paContext, at, now) : null;
    const readIso = at !== null ? new Date(at).toISOString() : null;
    const asOf = readIso && readIso.slice(0, 10) !== new Date(now).toISOString().slice(0, 10) ? atZone(readIso) : clock(readIso);
    const line = read ? `${read} (as of ${asOf})` : null;
    const reset = resetLine(launcher, data.agent?.sessionId, clock);
    if (!line && !reset) return null;
    return v_flex().px(12).py(6).gap(4).flex_none().border_b(1).border_color(t.border).text_size(11.5).text_color(t.dim)
      .when(Boolean(line), (e) => e.child(div().id("pa-context-line").child(sel("pa-context-text", line ?? ""))))
      .when(Boolean(reset), (e) => e.child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).child(sel("pa-reset-line", reset?.text ?? "")))
        .child(this.copier.button(t, "copy-previous-session", () => `/resume ${reset?.previous ?? ""}`))));
  }

  start(space, folder) {
    run("tab.new", { space: space.id, folder, argv: ["pa-session", "--space", localId(space.id), "--project", folder, ...(props()?.profile ? ["--profile", props().profile] : [])] });
  }

  // From a task to its open decision card.
  showCard(id, cx) {
    this.view = "decisions"; this.spotlight = id; this.disarm();
    const at = this.cards.filter((c) => !c.card || c.card.status === "open").findIndex((c) => c.id === id);
    if (at >= 0) this.cardScroll.scroll_to_item(at);
    cx.notify();
  }

  answer(space, entry, label, cx) {
    if (isPush(entry.card)) { this.answerPush(space, entry, label, cx); return; } // never answerDecision
    if (this.busy[entry.id] || !label || this.mark(space, entry)) return; // answered here or in another copy: never again
    const note = (this.notes[noteKey(space.id, entry.id)]?.value() ?? "").trim();
    // t-046: Other's words are the answer; Submit is off without them, and a stale click never writes an empty one.
    const other = label === OTHER;
    if (other && (!note || note.length > OTHER_MAX)) { this.failed[entry.id] = !note ? OTHER_EMPTY : OTHER_TOO_LONG; cx.notify(); return; }
    delete this.choice[entry.id];
    delete this.failed[entry.id];
    const m = this.markSaving(space, entry, label, note);
    this.busy[entry.id] = true;
    cx.notify();
    cx.spawn(async (cx) => {
      let saved = null;
      try { saved = await answerDecision(space, entry, label, note); }
      catch (e) { await this.notSaved(space, entry, m, String(e.message ?? e), cx); }
      if (saved) {
        Object.assign(m, { state: "saved", before: saved.before, after: saved.after, seq: this.seq });
        // A card for a rule proposal ("rule": "<id>", options Approve / Decline) decides that rule too. Its failure is
        // its own message: the answer is saved and stays. An Other answer decides no rule (t-046 R6): it stays proposed.
        const decided = entry.card?.rule && !isOther(saved.card) && (/^approve/i.test(label) ? "active" : /^decline/i.test(label) ? "declined" : null);
        if (decided) {
          try { await changeRules(space, (rules) => rules.map((r) => (r.id === entry.card.rule && r.status === "proposed" ? touched({ ...r, status: decided }, decided === "active" ? "approved" : "declined") : r))); }
          catch (e) { this.failed[entry.id] = `The answer was saved; the rule was not changed: ${String(e.message ?? e)}`; }
        }
        this.unsent.push({ spaceId: space.id, message: other ? `decision ${entry.id} answered Other, in the person's own words: "${oneLine(saved.card.note)}"` : `decision ${entry.id} answered` });
        this.resend(cx);
      }
      delete this.busy[entry.id];
      this.reload(cx);
      cx.notify();
    });
  }

  // t-045. The mark that locks a card for this copy: its own (saving, saved, unsure), else another copy's shared one
  // under ANSWERING_TTL old (state "elsewhere"); null when the card may be answered.
  mark(space, entry) {
    const k = noteKey(space.id, entry.id), own = this.sent[k];
    if (own) return own;
    const o = (shared(ANSWERING) ?? {})[k];
    if (o && o.by !== this.me && typeof o.at === "number" && Date.now() - o.at < ttlOf(o)) return { label: String(o.label ?? ""), note: o.note ?? null, at: o.at, state: "elsewhere", unknown: o.unknown === true };
    return null;
  }

  // Marks the card saving, here and in the shared key (expired entries pruned).
  markSaving(space, entry, label, note) {
    const k = noteKey(space.id, entry.id), at = Date.now();
    // before: the open copy the panel had (answerDecision's own read replaces it on success); path: to tell a card not read.
    const m = { label, note: note || null, at, state: "saving", before: typeof entry.text === "string" ? entry.text : null, after: null, why: null, seq: this.seq, path: entry.path };
    this.sent[k] = m;
    this.shareMark(k, { label, note: note || null, at, by: this.me });
    return m;
  }

  shareMark(k, value) {
    const now = Date.now(), all = {};
    for (const [key, x] of Object.entries(shared(ANSWERING) ?? {})) if (key !== k && typeof x?.at === "number" && now - x.at < ttlOf(x)) all[key] = x;
    if (value) all[k] = value;
    setShared(ANSWERING, all);
  }

  // A push card's run (push.js share, t-045 round 2): the same shared mark, so another window draws the card locked and
  // never runs pa-push-main. While the run is in flight the mark lasts as long as the run may (RUN_TIMEOUT); once it ended
  // answered ANSWERING_TTL from then; unknown (round 3): no expiry, see openedPush; label null (refused, or a check read it
  // open): this copy's mark goes.
  sharePush(spaceId, id, label, running, unknown = false, note = null) {
    const k = noteKey(spaceId, id);
    if (label === null) { if ((shared(ANSWERING) ?? {})[k]?.by === this.me) this.shareMark(k, null); return; }
    this.shareMark(k, { label, note: label === OTHER ? note || null : null, at: Date.now(), by: this.me, ...(running ? { ttl: RUN_TIMEOUT + GRACE + ANSWERING_TTL } : {}), ...(unknown ? { unknown: true } : {}) });
  }

  // A check from this copy, asked at `asked`, read the push card open (push.js opened, t-045 round 3): an unknown result's
  // mark written before that check, by any window (the owner may be gone), goes. A check asked before it proves nothing.
  openedPush(spaceId, id, asked) {
    const k = noteKey(spaceId, id), o = (shared(ANSWERING) ?? {})[k];
    if (o?.unknown === true && typeof o.at === "number" && asked >= o.at) this.shareMark(k, null);
  }

  // The card's write threw: read the file once to tell. Answered (here, or before by another route): it stays answered.
  // Open and unchanged (the text the panel had): the write really failed; the mark goes and the card says so (W3). Open
  // with other text: the card changed since; the mark goes and the file is shown. Unreadable: unsure, locked until a
  // load newer than now reads it (resolveMarks).
  async notSaved(space, entry, m, why, cx) {
    const k = noteKey(space.id, entry.id);
    let text;
    try { text = await this.host.call(this.host.ctx(cx), null, () => hostRead(entry.path, { on: space.machine ?? null }), { limit: READ_LIMIT, background: false, what: "the card read" }); } catch { text = TIMED_OUT; }
    let card = null;
    if (typeof text === "string") { try { card = JSON.parse(text); } catch { card = null; } }
    Object.assign(m, { why, seq: this.seq });
    if (text === null || (card && card.status === "open" && (m.before === null || text === m.before))) {
      delete this.sent[k]; this.shareMark(k, null);
      this.failed[entry.id] = `Not saved: ${why.replace(/\.?\s*$/, ".")} Pick an option and submit again.`;
    } else if (card && card.status === "open") {
      delete this.sent[k]; this.shareMark(k, null);
      this.failed[entry.id] = `Not saved: ${why.replace(/\.?\s*$/, ".")} The card has changed since; it is shown as it is now.`;
    } else if (card && typeof card === "object") {
      Object.assign(m, { state: "saved", label: String(card.answer ?? card.status ?? m.label), note: card.note ?? null }); // what the file has
      this.shareMark(k, { label: m.label, note: m.note, at: m.at, by: this.me });
      if (/already answered/.test(why)) this.failed[entry.id] = why; // today's words: "This card was already answered."
    } else m.state = "unsure";
  }

  // From commit() of a load of `space` that listed its cards (token: the load's): each of this copy's marks there.
  // Only a read of the card file made by this load resolves a mark: a card whose read failed (its last known copy,
  // `fallback`, or left out and named in `cards.unread`) never does (t-045 round 2).
  resolveMarks(space, token, cards) {
    // An unknown push result's shared mark (round 3): a read of the card made now that shows it no longer open ends it.
    for (const [k, x] of Object.entries(shared(ANSWERING) ?? {})) {
      if (x?.unknown !== true || !k.startsWith(noteKey(space.id, ""))) continue;
      const entry = cards.find((c) => noteKey(space.id, c.id) === k);
      if (entry?.card && !entry.fallback && entry.card.status !== "open") this.shareMark(k, null);
    }
    for (const [k, m] of Object.entries(this.sent)) {
      if (!k.startsWith(noteKey(space.id, ""))) continue;
      if (m.state === "saving") continue; // the write is in flight
      const entry = cards.find((c) => noteKey(space.id, c.id) === k);
      if (entry?.fallback || (!entry && m.path && cards.unread?.has(m.path))) continue; // not read now
      // Gone, not a card, read answered, or the text written: the file shows it now.
      if (!entry?.card || entry.card.status !== "open" || (m.after && entry.text === m.after)) { delete this.sent[k]; continue; }
      if (token <= m.seq) continue; // a read that started before the write ended: the old copy
      if (m.state === "unsure") { // a fresh read finds it open
        delete this.sent[k]; this.shareMark(k, null);
        // Unchanged (the text the panel had): the write did not happen. Other text: the card changed since; shown as it is.
        if (m.before !== null && entry.text === m.before) this.failed[entry.id] = `Not saved: ${String(m.why ?? "the write failed").replace(/\.?\s*$/, ".")} Pick an option and submit again.`;
        continue;
      }
      if (m.before !== null && entry.text === m.before) continue; // the copy from before the write (a read's fallback)
      delete this.sent[k]; // open with other text, read after the write: the card changed since; show the file
    }
  }

  // A push card's Submit: pa-push-main run (push.js) records the answer and pushes; the panel shows
  // its result, reloads and tells the orchestrator.
  answerPush(space, entry, label, cx) {
    if (this.mark(space, entry)) return; // answered in another panel window: never a run from here (t-045 round 2)
    const note = (this.notes[noteKey(space.id, entry.id)]?.value() ?? "").trim();
    if (label === OTHER && (!note || note.length > OTHER_MAX)) { this.failed[entry.id] = !note ? OTHER_EMPTY : OTHER_TOO_LONG; cx.notify(); return; } // t-131: never an empty run
    // told() runs when the tool's run has ended, long after this click: it gets the run task's cx
    // (a cx is valid only for the call that gave it; this click's would throw "stale context").
    const started = this.push.run(space, entry, label, note, cx, (message, cx) => {
      this.unsent.push({ spaceId: space.id, message });
      this.resend(cx);
      this.reload(cx);
    });
    if (started) delete this.choice[entry.id];
    cx.notify();
  }

  // t-083 J2: the Places section's rows for the space as loaded: limits from the load's rules, holders from the ledger
  // with their terminals looked up as the task rows do (the machine prefix on a remote space).
  placesOf(space, data) {
    const term = (address) => { const a = space.machine ? `${space.machine}/${address}` : address; return terminals().find((x) => x.address === a && x.state === "live"); };
    const o = this.taskOrderOf(data);
    return placesRows({ waiting: data?.waiting, startOrder: data?.startOrder, rules: this.rules.data, ledger: data?.ledger, isLive: (a) => Boolean(term(a)), statusOf: (a) => term(a)?.status ?? null, rank: o.has ? o.ranking.map((r) => r.id) : null, rankKnown: o.known,
      machines: this.machines[space.id] ?? null, here: space.machine ? `${space.machine} (the space's machine)` : "this Mac" }); // t-119: the suite machines' slots
  }

  // t-097 J2: the task order as loaded (R2, R3). known: task-order.json was read (its text, or no file); read:
  // readTaskOrder's (null while not known); ranking: effectiveTasks's (with the file unknown, the Project Agent's);
  // has: a task order exists, so the waiting list follows it (J1-D10, R27); text: what the compare-and-write expects.
  taskOrderOf(data) {
    const t = data?.taskOrder, known = Boolean(t && !gaveUp(t)), read = known ? readTaskOrder(t.missing ? null : t.text) : null;
    const tasks = data?.ledger?.tasks ?? [], to = data?.ledger?.taskOrder;
    return { known, read, text: known && !t.missing ? t.text : null, ranking: effectiveTasks(tasks, to, read ?? readTaskOrder(null)), has: known && hasTaskOrder(read, to), tasks };
  }

  // t-097 J2 (R7, R8): a task's ↑ ↓ ⤒ ⤓, Reset order, or Start next's rank: the new order (pruned, R4) written whole by
  // compare-and-write against the text read; then one "task order changed" notice per space while unsent, and a
  // reload. A refused write says why in the Tasks tab and reloads; nothing else is written. One write per space in
  // flight. after(cx): what runs once the write succeeded (Start next's pa-task next); not after a refusal.
  writeTaskOrderMove(space, next, { task = null, move, section = null }, cx, after = null) {
    if (this.taskOrderBusy[space.id] || this.space !== space.id) return;
    const o = this.taskOrderOf(this.data);
    if (!o.known) return;
    const text = taskOrderText(o.text, pruneOrder(next, o.tasks), { task, move, section }), expected = o.text;
    this.taskOrderBusy[space.id] = true; delete this.taskOrderError[space.id];
    cx.spawn(async (cx) => {
      let ok = false;
      try {
        await writeTaskOrder(space, text, expected);
        ok = true;
        if (this.space === space.id && this.data) this.data = { ...this.data, taskOrder: { text, missing: false } }; // drawn now; the reload reads it again
        if (!this.unsent.some((u) => u.spaceId === space.id && u.message === TASK_ORDER_NOTICE)) this.unsent.push({ spaceId: space.id, message: TASK_ORDER_NOTICE });
        this.resend(cx);
      } catch (e) { this.taskOrderError[space.id] = String(e.message ?? e); }
      finally { delete this.taskOrderBusy[space.id]; if (!ok || !after) this.reload(cx); cx.notify(); }
      if (ok && after) after(cx);
    });
    cx.notify();
  }
  moveTaskOrder(space, id, move, section, cx) {
    const o = this.taskOrderOf(this.data);
    if (!o.known || this.taskOrderBusy[space.id]) return;
    const fresh = freshOrder(o.read, o.tasks); // t-120: a reopened task's old place is not written back
    const next = move === "reset" ? { order: [], last: [] } : moveTask(o.ranking, section, id, move, fresh.order, fresh.last);
    if (next) this.writeTaskOrderMove(space, next, { task: id, move, section }, cx);
  }
  // R18: Start next: (a) the task to the very top of the ranking, then (b) pa-task next. (a) refused: nothing runs;
  // (b) failing after (a): the rank stays and the error shows on the row. No worker is started by the move.
  startNext(space, task, cx) {
    const o = this.taskOrderOf(this.data);
    if (!o.known || this.taskOrderBusy[space.id] || this.space !== space.id) return;
    const fresh = freshOrder(o.read, o.tasks);
    const next = startNextOrder(o.ranking, task.id, fresh.order, fresh.last);
    if (!next) { this.changeTask(space, task, "next", cx); return; }
    this.writeTaskOrderMove(space, next, { task: task.id, move: "next", section: task.state }, cx, (cx) => this.changeTask(space, task, "next", cx));
  }

  // A ↑ ↓ ⤒ click or Reset order (R6): the whole new order written by compare-and-write against the text read; then one
  // "start order changed" notice per space while unsent (J2-D5), and a reload. A refused write says why and reloads.
  // Nothing while a write of this space is in flight (AC9.3). It decides nothing else (R10): it only writes the order.
  moveJob(space, id, move, cx) {
    if (this.placesBusy[space.id] || this.space !== space.id) return;
    const p = this.placesOf(space, this.data);
    if (p.state !== "ok" || !p.orderKnown || (move !== "reset" && !p.movesOn)) return; // t-120: no move while the task order is not known
    const pins = pinsAfter({ jobs: p.jobsIn, start: p.start, rank: p.rank, id, move }); // t-097 R10: one pin for the job; reset: none
    if (!pins) return;
    const expected = p.orderTextRead, text = orderText(expected, pins, { id, move });
    this.placesBusy[space.id] = true; delete this.placesError[space.id];
    cx.spawn(async (cx) => {
      try {
        await writeStartOrder(space, text, expected);
        if (this.space === space.id && this.data) this.data = { ...this.data, startOrder: { text, missing: false } }; // drawn now; the reload reads it again
        if (!this.unsent.some((u) => u.spaceId === space.id && u.message === ORDER_NOTICE)) this.unsent.push({ spaceId: space.id, message: ORDER_NOTICE });
        this.resend(cx);
      } catch (e) { this.placesError[space.id] = String(e.message ?? e); }
      finally { delete this.placesBusy[space.id]; this.reload(cx); cx.notify(); }
    });
    cx.notify();
  }

  // Deliver what the orchestrator hasn't had yet, to the terminal its launcher runs in now. With
  // none, nothing is sent and the notice waits; an undelivered notice is kept and retried. A
  // space's notices go out in order: one that waits holds back the ones after it.
  // The sending mark is always cleared: when the task cannot start (the queue is untouched; the 10 s
  // timer tries again) and when a step in it throws (every item not yet sent is put back).
  resend(cx) {
    if (!this.unsent.length || this.sending) return;
    this.sending = true;
    try {
      cx.spawn(async (cx) => {
        const left = [], held = new Set(), items = this.unsent.splice(0);
        let at = 0;
        try {
          for (; at < items.length; at++) {
            const item = items[at];
            const target = !held.has(item.spaceId) && liveTarget(item.spaceId);
            if (!target) { held.add(item.spaceId); left.push(item); continue; }
            try { await notifyOrchestrator(target.address, item.message, this.host.ctx(cx, { origin: "send" })); } // capped (store.js SEND_LIMIT), one send per address in flight
            catch (e) { held.add(item.spaceId); left.push(e?.busy ? item : { ...item, why: String(e.message ?? e) }); } // busy: the last send there still waits; the notice keeps its line
          }
        } finally {
          this.unsent = mergeRuleNotices([...left, ...items.slice(at), ...this.unsent]); // kept ones first: anything queued meanwhile comes after
          this.sending = false;
        }
        cx.notify();
      });
    } catch { this.sending = false; }
  }

  // t-101: the person changed these rules in the Rules view (rules.js). One "rules changed: <ids>" notice per space
  // while unsent: a later change adds its ids to it (each named once). A notice already being sent is not in
  // `unsent`, so a change made meanwhile starts a new one; if that send fails, resend merges the two (t-085).
  rulesChanged(space, ids, cx) {
    const queued = this.unsent.find((u) => u.spaceId === space.id && Array.isArray(u.rules));
    if (queued) { for (const id of ids) if (!queued.rules.includes(id)) queued.rules.push(id); queued.message = rulesNotice(queued.rules); }
    else { const rules = [...new Set(ids)]; this.unsent.push({ spaceId: space.id, rules, message: rulesNotice(rules) }); }
    this.resend(cx);
  }

  // A proposal approved or declined in the Rules view: answer its card too, and tell the orchestrator.
  async ruleDecided(space, id, what, cx) {
    const entry = this.cards.find((c) => c.card?.status === "open" && c.card.rule === id && !this.mark(space, c));
    const label = entry?.card.options?.find((o) => (what === "declined" ? /^decline/i : /^approve/i).test(o.label))?.label;
    if (label) { // marked as from the Decisions view (t-045): drawn answered from now, never answered twice
      const note = what === "edited and approved" ? "Edited before approving; read rules.json." : "";
      const m = this.markSaving(space, entry, label, note);
      try { const saved = await answerDecision(space, entry, label, note); Object.assign(m, { state: "saved", before: saved.before, after: saved.after, seq: this.seq }); }
      catch (e) { await this.notSaved(space, entry, m, String(e.message ?? e), cx); throw e; }
    }
    this.unsent.push({ spaceId: space.id, message: label ? `decision ${entry.id} answered` : `rule ${id} ${what}` });
    this.resend(cx);
    this.reload(cx);
  }

  // Schedule settings and on-demand passes (the timer copy does the sending).
  scheduleChange(space, cx, work) {
    this.schedule.error = null;
    cx.spawn(async (cx) => {
      try { await work(stateDir(space), space.machine ?? null); } catch (e) { this.schedule.error = String(e.message ?? e); }
      this.reload(cx); cx.notify();
    });
  }

  // t-145: the Passes tab's label: off, paused, a pass queued, its cadence, or ! (schedule.json missing or invalid).
  scheduleLabel(data) {
    const s = data.schedule, every = s ? cadence(s) : 0;
    return !s ? "Passes · !" : data.request?.pending ? "Passes · queued" : !every ? "Passes · off" : s.paused ? "Passes · paused" : `Passes · ${every} min`;
  }

  // Points the Files view at a folder; `file` opens that file in it (else its list). The evidence and profile links.
  showFile(space, path, label, file, task = null) {
    setShared("my-files.root", { space: space.id, path, label, file: file ?? null, ...(task ? { task } : {}), at: Date.now() });
    run("layout.select", { stack: "panel-body", child: "files" });
  }

  scheduleBar(t, space, data) {
    const s = data.schedule, st = this.schedule;
    if (!s) return div().px(12).py(6).text_size(11.5).text_color(t.blocked).child("schedule.json is missing or invalid; scheduled passes are off.");
    const every = cadence(s), hold = holdReason(s, data.ledger), pending = data.request?.pending;
    const clock = (isoText) => { const d = new Date(isoText); return Number.isNaN(d.getTime()) ? "?" : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); };
    const summary = pending ? `${data.request.retro ? "Retrospective" : "Pass"} queued: runs when the current turn ends`
      : hold === "off" ? "Scheduled passes: off" : hold === "paused" ? `Scheduled passes paused (every ${every} min when resumed)` : `Passes every ${every} min · ${hold ? hold : s.nextPass ? `next ${clock(s.nextPass)}` : "next: when the timer next looks"}`;
    const pill = (id, label, on, click) => div().id(id).flex_none().px(7).h(22).flex().items_center().rounded(6).text_size(11.5).cursor_pointer()
      .bg(on ? t.selected : t.chrome).border(1).border_color(on ? t.accent : t.border).text_color(on ? t.foreground : t.muted).hover((x) => x.bg(t.hover)).on_click(click).child(label);
    const field = (state, w) => Input.new(state).w(w).h(24).px(6).rounded(6).border_1().border_color(t.input).bg(t.background).text_size(11.5).text_color(t.foreground);
    return v_flex().px(8).py(6).gap(6).flex_none().border_b(1).border_color(t.border)
      .child(h_flex().gap(6).items_center()
        .child(div().id("sched-summary").flex_1().min_w(0).truncate().px(4).text_size(11.5).text_color(t.muted).cursor_pointer().hover((x) => x.text_color(t.foreground))
          .on_click((_e, cx) => {
            st.open = !st.open;
            if (st.open && !st.custom) {
              st.custom = this.field({ placeholder: "min", value: PRESETS.includes(every) || !every ? "" : String(every) });
              st.quietFrom = this.field({ placeholder: QUIET.from, value: s.quietHours?.from ?? "" });
              st.quietTo = this.field({ placeholder: QUIET.to, value: s.quietHours?.to ?? "" });
            }
            cx.notify();
          }).child(`${st.open ? "▾" : "▸"} ${summary}`))
        .child(pill("sched-pause-top", s.paused ? "Resume passes" : "Pause passes", Boolean(s.paused), (_e, cx) => this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { paused: !s.paused }))))
        .child(pill("pass-now", "Run pass now", false, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => requestPass(dir, on, false))))
        .child(pill("pass-retro", "Run retrospective now", false, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => requestPass(dir, on, true)))))
      .when(Boolean(st.error), (el) => el.child(div().px(4).text_size(11.5).text_color(t.blocked).child(st.error)))
      .when(st.open, (el) => el
        .child(h_flex().gap(4).items_center().flex_wrap()
          .child(div().px(4).text_size(11.5).text_color(t.dim).child("Every"))
          .children(PRESETS.map((m) => pill(`every-${m}`, `${m}`, every === m, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { everyMinutes: m })))))
          .child(field(st.custom, 54))
          .child(pill("every-custom", "Set min", false, (_e, cx) => {
            const m = Math.round(Number(st.custom.value()));
            if (!Number.isFinite(m) || m < MIN_MINUTES) { st.error = `The shortest cadence is ${MIN_MINUTES} minutes.`; cx.notify(); return; }
            this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { everyMinutes: m }));
          }))
          .child(pill("every-off", "Off", !every, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { everyMinutes: 0 }))))
          )
        .child(h_flex().gap(4).items_center().flex_wrap()
          .child(div().px(4).text_size(11.5).text_color(t.dim).child("Quiet hours"))
          .child(field(st.quietFrom, 62)).child(div().text_size(11.5).text_color(t.dim).child("to")).child(field(st.quietTo, 62))
          .child(pill("quiet-set", "Set", false, (_e, cx) => {
            // An empty field stands for the time it shows (QUIET); a typed one must be a 24-hour time.
            const typed = { from: st.quietFrom.value().trim(), to: st.quietTo.value().trim() };
            const from = hhmm(typed.from || QUIET.from), to = hhmm(typed.to || QUIET.to);
            const why = !from ? `Quiet hours start "${typed.from}" is not a time: use 24-hour HH:MM, like ${QUIET.from}.`
              : !to ? `Quiet hours end "${typed.to}" is not a time: use 24-hour HH:MM, like ${QUIET.to}.`
              : from === to ? `Quiet hours start and end are the same time (${from}): pick two different times.` : null;
            if (why) { st.error = why; cx.notify(); return; }
            this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { quietHours: { from, to } }));
          }))
          .child(pill("quiet-clear", "None", !s.quietHours, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { quietHours: null }))))
          .child(pill("sched-active", "Only while tasks are active", s.onlyWhileActive !== false, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { onlyWhileActive: s.onlyWhileActive === false })))))
        .child(div().px(4).text_size(10.5).text_color(t.dim).child(`Last pass ${s.lastPass ? clock(s.lastPass) : "never"}. A pass never interrupts a turn; a new cadence applies from the next pass.`)));
  }

  // The person's own change to a task: done, park, reopen, next, pause, unpause (t-097), delete (to the trash). Runs pa-task on
  // the space's machine; it edits the ledger under a lock and tells the Project Agent.
  changeTask(space, task, action, cx) {
    delete this.taskError[task.id]; this.disarm();
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await hostRun("pa-task", [action, String(task.id), "--space", localId(space.id)], { on: space.machine ?? null, timeout: 30000 });
        if (r.code !== 0) this.taskError[task.id] = (r.stderr || r.stdout || `pa-task exited with ${r.code}`).trim().slice(0, 300);
      } catch (e) { this.taskError[task.id] = String(e.message ?? e); }
      this.reload(cx); cx.notify();
    });
  }

  // The task id whose Delete is armed, if the arm still holds: same space, Tasks view, the task
  // expanded, under ARM_WAIT old (the view the last render drew: a new open card can switch it). One that no longer holds is dropped (it never comes back).
  // what: "delete" (Delete…) or "done" (t-097 R20: Mark done's confirmation); one arm at a time.
  armedTask(spaceId, what = "delete") {
    const a = this.armed;
    if (armHolds(a, spaceId, a && this.viewShown === "tasks" && this.open.has(a.id))) return (a.what ?? "delete") === what ? a.id : null;
    this.armed = null;
    return null;
  }

  // Drops every armed Delete: the task's and the Rules view's.
  disarm() { this.armed = null; this.rules.armed = null; }
  // A panel text field (t-028): focus in it drops an armed Delete, whether or not the Input stops the press.
  field(options) { return fieldState(options, (_e, cx) => { if (this.armed || this.rules.armed) { this.disarm(); cx.notify(); } }); }

  // Opens a link on this computer (wherever the space lives).
  openLink(url, cx) {
    const opener = window()?.platform === "macos" ? "open" : "xdg-open";
    cx.spawn(async () => { try { await hostRun(opener, [url], { timeout: 20000 }); } catch (e) { notify({ title: "Couldn't open the link", body: String(e.message ?? e), level: "error" }); } });
  }

  // Shows a worker's live terminal: in the layout's Agents view when it has one (a stack child
  // "agents-view" holding a block "agents-live"), else as a peek over the tab.
  showAgent(address) {
    const find = (n, id) => { if (n?.id === id) return n; for (const c of n?.children ?? []) { const f = find(c, id); if (f) return f; } return null; };
    const tree = JSON.parse(JSON.stringify(layout() ?? {})), view = find(tree, "agents-view");
    if (!view) { run("terminal.peek", { terminal: address }); return; }
    let live = find(tree, "agents-live");
    if (!live) { live = { id: "agents-live", type: "block" }; view.children = [...(view.children ?? []), live]; }
    live.content = { terminal: address }; live.hidden = false;
    run("layout.set", { layout: tree });
    run("layout.select", { stack: "panel-body", child: "agents-view" });
    run("layout.show", { id: "panel" });
  }

  // Points the Files view at a task's evidence folder; `file` opens that file in it (else its list).
  showEvidence(space, task, file) {
    setShared("my-files.root", { space: space.id, path: `${stateDir(space)}/evidence/${task}`, label: `evidence ${task}`, file: file ?? null, task, at: Date.now() });
    run("layout.select", { stack: "panel-body", child: "files" });
  }

  runAction(task, action, space, cx) {
    const key = `${task.id}:${action.label}`;
    if (this.output[key]?.running) return;
    this.output[key] = { running: true, text: "" };
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        // A link opens on this computer; a command runs where the space lives.
        const r = action.local ? await hostRun(action.argv[0], action.argv.slice(1), { timeout: 20000 })
          : await hostRun(action.argv[0], action.argv.slice(1), { cwd: this.data?.agent?.project, on: space.machine ?? null, timeout: 60000 });
        this.output[key] = { code: r.code, text: `${r.stdout}${r.stderr}`.trim().slice(0, 4000) };
      } catch (e) { this.output[key] = { code: -1, text: String(e.message ?? e) }; }
      cx.notify();
    });
  }

  chip(t, text, color) {
    return div().flex_none().px(6).h(18).flex().items_center().rounded(5).bg(color + "26").text_size(10.5).font_semibold().text_color(color).child(text);
  }

  button(t, id, label, onClick, strong) {
    return div().id(id).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(strong ? t.accent : t.border).text_size(11.5)
      .text_color(t.foreground).cursor_pointer().hover((s) => s.bg(t.hover)).on_click(onClick).child(label);
  }

  actions(t, task, space) {
    const list = taskActions(this.data.profile, this.data.profileDir, task, window()?.platform);
    if (!list.length) return [];
    return [
      h_flex().gap(6).flex_wrap().children(list.map((a, i) => {
        const out = this.output[`${task.id}:${a.label}`];
        return div().id(`act-${task.id}-${i}`).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(t.border).text_size(11.5)
          .text_color(t.foreground).cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.runAction(task, a, space, cx))
          .child(out?.running ? `${a.label}…` : a.label);
      })),
      ...list.map((a) => this.output[`${task.id}:${a.label}`]).filter((o) => o && !o.running && (o.text || o.code !== 0)).map((o, i) =>
        div().id(`out-${task.id}-${i}`).p(6).rounded(5).bg(t.background).text_size(11).text_color(o.code === 0 ? t.muted : t.blocked).child(o.text || `exited with code ${o.code}`)),
    ];
  }

  // t-142: a task's moves and Start next show while the pointer is over it or while its head or one of those buttons
  // has the keyboard. Their focus handles (TASK_FOCUS, per task) are made in a task, never in render; one ask at a time.
  wantTaskFocus(ids, cx) {
    const missing = ids.filter((id) => !this.taskFocus.has(id));
    if (!missing.length || this.taskFocusAsked) return;
    this.taskFocusAsked = true;
    cx.spawn(async (cx) => {
      this.taskFocusAsked = false;
      for (const id of missing) if (!this.taskFocus.has(id)) this.taskFocus.set(id, Object.fromEntries(TASK_FOCUS.map((k) => [k, cx.focus_handle()])));
      cx.notify();
    });
  }

  // row (t-097 J2): the task's place in its section (tasks.js taskSections: n, source, quote, the moves that apply).
  task(t, task, space, badges = [], row = null) {
    const open = this.open.has(task.id), armed = open && this.armedTask(space.id) === task.id, doneArmed = open && this.armedTask(space.id, "done") === task.id;
    // R17: a needs-decision task with an open card does not leave Needs decision from here (Delete stays).
    const waitsOn = task.state === "needs-decision" ? this.cards.find((c) => c.card?.status === "open" && c.card.task === task.id) : null;
    const unread = task.state === "needs-decision" && this.cardsFor !== space.id; // t-120 (t-097 REVIEW J2 RL-3): its cards not read yet
    const busy = Boolean(this.taskOrderBusy[space.id]), orderKnown = this.taskOrderOf(this.data).known;
    const focus = this.taskFocus.get(task.id) ?? null; // t-142
    const near = this.hovered === task.id || Boolean(focus && Object.values(focus).some((h) => h.is_focused()));
    const mini = (id, label, on, dim = false, fh = null) => div().id(id).flex_none().px(5).h(20).flex().items_center().rounded(5).border(1).border_color(t.border).text_size(11)
      .text_color(dim ? t.dim : t.foreground).when(!dim, (b) => b.cursor_pointer().hover((x) => x.bg(t.hover))).when(Boolean(fh), (b) => b.track_focus(fh).tab_index(0))
      .on_click((_e, cx) => { cx.stop_propagation(); if (!dim) on(cx); }).child(label);
    const moves = row && orderKnown && near ? [["top", "⤒"], ["up", "↑"], ["down", "↓"], ["bottom", "⤓"]].filter(([m]) => row[m]) : [];
    const chip = row ? chipText(row.source) : null;
    // A press on the armed Delete or its Cancel stops here: a press anywhere else in the panel disarms (render's root).
    const keep = (el) => el.on_mouse_down("left", (_e, cx) => cx.stop_propagation());
    const crew = (task.workers ?? []).filter((w) => !(w?.finished && w?.closed));
    const tone = { "needs-decision": t.blocked, review: t.done, "in-progress": t.working, investigating: t.working, queued: t.muted, parked: t.dim, done: t.dim }[task.state] ?? t.muted;
    return v_flex().id(`task-${task.id}`).flex_none().rounded(7).border(1).border_color(t.border).bg(t.surface)
      .on_hover((on, cx) => { if (on) this.hovered = task.id; else if (this.hovered === task.id) this.hovered = null; cx.notify(); }) // t-142
      .child(h_flex().id(`task-head-${task.id}`).h(32).px(9).gap(7).items_center().cursor_pointer().hover((s) => s.bg(t.hover))
        .when(Boolean(focus), (el) => el.track_focus(focus.head).tab_index(0))
        .on_click((_e, cx) => { if (open) { this.open.delete(task.id); if (this.armed?.id === task.id) this.armed = null; } else this.open.add(task.id); cx.notify(); })
        .child(div().flex_none().text_size(11).text_color(t.dim).child(open ? "▾" : "▸"))
        .when(Boolean(row?.n), (el) => el.child(div().flex_none().text_size(11.5).text_color(t.muted).child(String(row?.n)))) // R15: its place in the section
        .child(div().flex_none().text_size(11.5).text_color(t.dim).child(String(task.id ?? "?")))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(t.foreground).child(String(task.title ?? "(untitled)")))
        .when(Boolean(chip), (el) => el.child(div().flex_none().text_size(10).text_color(t.dim).child(chip ?? ""))) // R13: a rank the Project Agent set
        .when(task.state === "queued" && Boolean(task.paused), (el) => el.child(div().flex_none().text_size(10.5).text_color(t.working).child("paused"))) // R19
        .when(Boolean(afterLine(row?.after)), (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child(afterLine(row?.after) ?? ""))) // R18
        .when(Boolean(task.phase), (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child(String(task.phase))))
        // t-083 J2 (R11): one badge per waiting or holding job; a click unfolds Places and marks that line for 3 s.
        .children(badges.map((b, i) => div().id(`badge-${task.id}-${i}`).flex_none().px(6).h(18).flex().items_center().rounded(5).border(1).border_color(t.border)
          .text_size(10.5).text_color(t.muted).cursor_pointer().hover((s) => s.bg(t.hover))
          .on_click((_e, cx) => { cx.stop_propagation(); this.placesFolded = false; this.placeMark = { space: space.id, key: b.key, until: Date.now() + 3000 }; cx.notify(); })
          .child(b.text)))
        // t-097 J2: the moves within the section (R7) and the category moves (R18, R19); dimmed while an order write is in flight.
        .children(moves.map(([m, label]) => mini(`tmove-${m}-${task.id}`, label, (cx) => this.moveTaskOrder(space, task.id, m, task.state, cx), busy, focus?.[m])))
        .when(task.state === "queued" && orderKnown && near, (el) => el.child(mini(`next-${task.id}`, "Start next", (cx) => this.startNext(space, task, cx), busy, focus?.next)))
        .when(task.state === "queued" && Boolean(task.paused), (el) => el.child(mini(`unpause-${task.id}`, "Unpause", (cx) => this.changeTask(space, task, "unpause", cx))))
        .when(task.state === "in-progress" || task.state === "investigating", (el) => el.child(mini(`pause-${task.id}`, "Pause", (cx) => this.changeTask(space, task, "pause", cx))))
        .child(this.chip(t, String(task.state ?? "?"), tone)))
      .when(open, (el) => el.child(v_flex().px(10).pb(9).gap(6).text_size(12).text_color(t.muted)
        .when(Boolean(task.goal), (e) => e.child(div().child(sel(`sel-goal-${task.id}`, String(task.goal)))))
        .when(row?.source === "chat" && Boolean(row?.quote), (e) => e.child(div().text_size(11.5).text_color(t.muted).child(`You asked in chat: "${row?.quote}"`))) // R13
        .when(Boolean(waitsOn), (e) => e.child(div().text_size(11.5).text_color(t.blocked).child(`Answer ${waitsOn?.id} first, or ask the Project Agent to withdraw it.`))) // R17
        .when(!waitsOn && unread, (e) => e.child(div().text_size(11.5).text_color(t.dim).child("Checking for open decisions…"))) // t-120: R17 waits for the cards
        .child(div().text_size(11).text_color(t.dim).child(sel(`sel-kind-${task.id}`, [task.type, task.tier && `${task.tier} tier`].filter(Boolean).join(" · "))))
        // Its workers: a live one opens in the Agents view. One closed with pa-worker close (finished,
        // closed set) is not drawn; its ledger entry stays (only the panel leaves it out). None left: no row.
        .when(crew.length > 0, (e) => e.child(h_flex().gap(6).flex_wrap().children(crew.map((w, i) => {
          const address = space.machine ? `${space.machine}/${w.address}` : w.address;
          const term = terminals().find((x) => x.address === address && x.state === "live");
          // Read-only marks the tools write: "kept" (pa-worker keep); "(closed)" only for a finished entry with no closed stamp.
          const label = `${w.role ?? "worker"} ${w.address ?? ""}`.trim() + (w.keep ? " · kept" : "");
          // t-142: the shared worker item (tasks.js workerItem), also drawn for the worker lines in Places.
          return workerItem(t, { id: `worker-${task.id}-${i}`, live: Boolean(term), status: term?.status ?? null, onClick: term ? () => this.showAgent(address) : null,
            text: term ? `${label} ↗` : w.finished ? `${label} (closed)` : `${label} (ended)`,
            // The × opens the delete dialog: the person's way to close any worker (the agent closes finished ones itself).
            // It stops the press, so it drops an armed Delete itself (the root never sees this press).
            trailing: div().id(`worker-x-${task.id}-${i}`).flex_none().px(3).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.blocked))
              .on_mouse_down("left", (_e, cx) => { cx.stop_propagation(); if (this.armed || this.rules.armed) { this.disarm(); cx.notify(); } })
              .on_click((_e, cx) => { cx.stop_propagation(); openOverlay({ mod: "project-agent", kind: "dialog", width: 540, height: 400, props: { deleteWorker: { space: space.id, machine: space.machine ?? null, address, name: w.name ?? label } } }); })
              .child("×") });
        }))))
        // Its open decisions: one button each, straight to the card.
        .when(this.cards.some((c) => c.card?.status === "open" && c.card.task === task.id), (e) => e.child(h_flex().gap(6).flex_wrap()
          .children(this.cards.filter((c) => c.card?.status === "open" && c.card.task === task.id).map((c, i) =>
            div().id(`todecide-${task.id}-${i}`).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(t.blocked).text_size(11.5)
              .text_color(t.foreground).cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.showCard(c.id, cx)).child(`Needs your decision: ${c.id} →`)))))
        // Its artifacts: one button per change request, issue or other link.
        .when(taskLinks(this.data.profile, task).length > 0, (e) => e.child(h_flex().gap(6).flex_wrap().children(taskLinks(this.data.profile, task).map((l, i) =>
          this.button(t, `link-${task.id}-${i}`, `${l.label} ↗`, (_e, cx) => this.openLink(l.url, cx))))))
        .when(Boolean(this.data.evidence?.has(task.id)), (e) => e.child(h_flex().gap(6).child(this.button(t, `ev-${task.id}`, "Evidence", () => this.showEvidence(space, task.id)))))
        .children(this.actions(t, task, space))
        // The person's own controls for the task. Copy (t-026) puts the task's text on the clipboard and does nothing else.
        .child(h_flex().gap(6).flex_wrap().pt(2)
          .child(this.copier.button(t, `copy-task-${task.id}`, () => this.taskText(task, space, crew)))
          .when(task.state === "done" || task.state === "parked", (e) => e.child(this.button(t, `reopen-${task.id}`, "Reopen", (_e, cx) => this.changeTask(space, task, "reopen", cx))))
          // t-097 R20: Mark done takes two clicks; the first says what stays. R17: neither it nor Park while a card waits.
          .when(task.state !== "done" && !waitsOn && !unread && !doneArmed, (e) => e.child(keep(this.button(t, `done-${task.id}`, "Mark done", (_e, cx) => { this.armed = { ...arm(task.id, space.id), what: "done" }; cx.notify(); }))))
          .when(task.state !== "done" && task.state !== "parked" && !waitsOn && !unread, (e) => e.child(this.button(t, `park-${task.id}`, "Park", (_e, cx) => this.changeTask(space, task, "park", cx))))
          // Two clicks on the same button: the first arms it (ARM_WAIT; Cancel, a press elsewhere, collapsing, another view or space disarm).
          .child(keep(div().id(`delete-${task.id}`)).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(armed ? t.blocked : t.border)
            .text_size(11.5).text_color(armed ? t.blocked : t.muted).cursor_pointer().hover((s) => s.bg(t.hover))
            // The arm is asked again at the click (t-028): one that ran out since this was drawn only arms again.
            .on_click((_e, cx) => { cx.stop_propagation(); if (this.armedTask(space.id) === task.id) this.changeTask(space, task, "delete", cx); else { this.armed = arm(task.id, space.id); cx.notify(); } })
            .child(armed ? "Delete the task, its evidence and its cards?" : "Delete…"))
          .when(armed, (e) => e.child(keep(this.button(t, `delete-cancel-${task.id}`, "Cancel", (_e, cx) => { cx.stop_propagation(); this.disarm(); cx.notify(); })))))
        .when(doneArmed, (e) => e.child(keep(v_flex().gap(4)
          .child(div().text_size(11.5).text_color(t.foreground).child(this.doneText(task, space, crew)))
          .child(div().text_size(10.5).text_color(t.dim).child(`as of ${atZone(new Date(this.dataAt ?? Date.now()).toISOString()).slice(6)}`)) // spec concern 6: what this load read
          .child(h_flex().gap(6)
            .child(this.button(t, `done-confirm-${task.id}`, "Confirm done", (_e, cx) => { cx.stop_propagation(); this.changeTask(space, task, "done", cx); }))
            .child(this.button(t, `done-cancel-${task.id}`, "Cancel", (_e, cx) => { cx.stop_propagation(); this.disarm(); cx.notify(); }))))))
        .when(Boolean(this.taskError[task.id]), (e) => e.child(div().text_size(11.5).text_color(t.blocked).child(this.taskError[task.id])))));
  }

  // t-097 R20: Mark done's confirmation line, from what this load read: the task's change request, its worktrees,
  // its merge-queue entries and its live workers (as of the load: a remote space's may be a pass old).
  doneText(task, space, crew) {
    const repos = this.data?.queue?.doc?.repos;
    const queueEntries = repos && typeof repos === "object" ? Object.values(repos).reduce((n, r) => n + (Array.isArray(r?.entries) ? r.entries.filter((e) => e?.task === task.id).length : 0), 0) : 0;
    const live = crew.filter((w) => !w.finished && w.address && terminals().some((x) => x.address === (space.machine ? `${space.machine}/${w.address}` : w.address) && x.state === "live")).map((w) => String(w.address));
    return doneLine({ changeRequest: task.changeRequest ?? null, worktrees: taskTrees(task).length, queueEntries, live });
  }

  // A task as plain text lines (its Copy, t-026): what its open card shows, the workers by their full address.
  taskText(task, space, crew) {
    const kind = [task.type, task.tier && `${task.tier} tier`].filter(Boolean).join(" · ");
    const links = taskLinks(this.data.profile, task);
    const workers = crew.map((w) => {
      const address = space.machine ? `${space.machine}/${w.address}` : w.address;
      const live = terminals().some((x) => x.address === address && x.state === "live");
      return `  ${`${w.role ?? "worker"} ${address ?? ""}`.trim()}${w.keep ? " · kept" : ""}${live ? "" : w.finished ? " (closed)" : " (ended)"}`;
    });
    return [
      `${task.id ?? "?"} ${task.title ?? "(untitled)"}`,
      `State: ${task.state ?? "?"}${task.phase ? ` · ${task.phase}` : ""}`,
      ...(task.goal ? [String(task.goal)] : []),
      ...(kind ? [kind] : []),
      ...(workers.length ? ["Workers:", ...workers] : []),
      ...(links.length ? ["Links:", ...links.map((l) => `  ${l.label}: ${l.url}`)] : []),
      ...this.cards.filter((c) => c.card?.status === "open" && c.card.task === task.id).map((c) => `Needs your decision: ${c.id}`),
    ].join("\n");
  }

  // A decision card as plain text lines (its Copy, t-026). A push card's facts are the lines its block drew.
  cardText(c, entry, evidence, pushLines) {
    const open = c.status === "open", options = (isPush(c) ? PUSH_OPTIONS : c.options ?? []).filter((o) => !ownOther(o));
    const earlier = earlierLine(c);
    return [
      [entry.id, c.task, open ? "open" : c.status === "withdrawn" ? "withdrawn" : `answered: ${c.answer}`, when(open ? c.created : c.answered)].filter(Boolean).join(" · "),
      String(c.question),
      ...(isPush(c) ? pushLines : c.context ? String(c.context).split("\n") : []),
      ...(options.length ? ["Options:", ...options.map((o) => `  ${o.label}${o.detail ? `: ${o.detail}` : ""}${c.recommendation === o.label ? " (recommended)" : ""}`)] : []),
      ...(evidence ? [`Evidence: ${evidence}`] : []),
      ...(!open && c.status !== "withdrawn" && c.answer ? [isOther(c) ? `Answer: ${OTHER} (the person's own words)` : `Answer: ${c.answer}`] : []),
      ...(!open && c.answeredBy ? [c.status === "withdrawn" ? "Closed by the Project Agent as no longer needed" : `Answered by the ${c.answeredBy}`] : []),
      ...(!open && c.note ? [noteLine(c)] : []),
      ...(earlier ? [earlier] : []),
    ].join("\n");
  }

  card(t, entry, space) {
    let c = entry.card;
    if (!c) {
      return v_flex().id(`card-${entry.id}`).flex_none().p(10).gap(4).rounded(7).border(1).border_color(t.blocked).bg(t.surface)
        .child(div().text_size(12).text_color(t.foreground).child(`${entry.id}: can't read this card`))
        .child(div().text_size(11.5).text_color(t.blocked).child(entry.error ?? ""))
        .child(div().text_size(11).text_color(t.dim).truncate().child(entry.path.replace(home(), "~")));
    }
    const push = isPush(entry.card), running = push ? this.push.running(space.id, entry.id) : null;
    // t-045: a card answered from here (or another copy) is drawn answered while the file read still says open.
    // A push card: only another window's mark (this copy's own lock is push.js locked()); its block stays the open one.
    const m = c.status === "open" ? this.mark(space, entry) : null;
    if (m && !push) c = { ...c, status: "answered", answer: m.label, note: m.note, answered: isoAt(m.at), ...(m.label === OTHER ? { other: true } : {}) };
    const markLine = m?.state === "saving" ? "Saving…" : m?.state === "unsure" ? "Could not tell whether the answer was saved; reading the card again."
      : m?.state === "elsewhere" && m.unknown ? "A push from another panel window has an unknown result: locked until a check from here reads the card open (Check again)."
      : m?.state === "elsewhere" ? "Answered in another panel window." : null;
    const pushAnswered = push && c.status === "open" ? this.push.answeredHere(space.id, entry.id) ?? (m?.unknown ? null : m?.label) ?? null : null; // a run from here (or another window) answered it
    const pushWords = push && c.status === "open" ? this.push.noteHere(space.id, entry.id) ?? m?.note ?? null : null; // t-131: an Other run's words
    const open = c.status === "open", busy = Boolean(this.busy[entry.id]) || Boolean(running) || (push && (this.push.locked(space.id, entry.id) || Boolean(m)));
    const task = c.task && (this.data?.ledger?.tasks ?? []).find((x) => x.id === c.task);
    // t-100: a push card's run keeps its answer chosen: the label in flight, then the one a run from here answered.
    // A refused or unknown run has neither, and answerPush cleared the choice: nothing chosen, as before.
    const picked = !open ? null : (push && (running ?? this.push.answeredHere(space.id, entry.id))) || (this.choice[entry.id] ?? null);
    // Other (t-046): the field is the answer; Submit needs words, at most OTHER_MAX.
    const words = picked === OTHER ? (this.notes[noteKey(space.id, entry.id)]?.value() ?? "").trim() : "";
    // t-131: a field with no change event (the host's InputState without `on`) cannot turn Submit on as the person types:
    // Submit stays on for Other and an empty click is refused with the hint (answer(), answerPush()).
    const live = typeof this.notes[noteKey(space.id, entry.id)]?.on === "function";
    const wordsOk = picked !== OTHER || (live ? words.length > 0 && words.length <= OTHER_MAX : true);
    const ready = Boolean(picked) && wordsOk && !busy && !(push && shownOnly(entry)) && (!push || picked !== PUSH || this.push.canSubmit(space.id, entry.id)); // Push: a fresh check that passed; a shown-only card: never
    // The rows: the card's options but its own Other (each keeps its index in the id), then the panel's Other row.
    const listed = (push ? PUSH_OPTIONS : c.options ?? []).map((o, i) => [o, i]).filter(([o]) => !ownOther(o));
    const otherDetail = (push ? null : (c.options ?? []).find(ownOther)?.detail) || OTHER_DETAIL;
    const earlier = earlierLine(c);
    const evidence = Boolean(c.task) && Boolean(this.data?.evidence?.has(c.task)) ? cardEvidence(c, this.data?.evidence?.get(c.task)) ?? `${c.task} evidence` : null;
    const pushLines = []; // filled by the push block below, in this render; read by Copy at its click
    return v_flex().id(`card-${entry.id}`).flex_none().p(10).gap(7).rounded(7).border(1).border_color(this.spotlight === entry.id ? t.accent : open ? t.input : t.border).bg(t.surface)
      .child(h_flex().gap(7).items_center()
        .child(div().flex_none().text_size(11.5).text_color(t.dim).child(entry.id))
        .when(Boolean(c.task) && Boolean(this.data?.evidence?.has(c.task)), (el) => el.child(div().id(`card-task-${entry.id}`).flex_none().text_size(11.5).text_color(t.accent).cursor_pointer()
          .on_click(() => this.showEvidence(space, c.task, cardEvidence(c, this.data?.evidence?.get(c.task)))).child(`${cardEvidence(c, this.data?.evidence?.get(c.task)) ?? `${c.task} evidence`} ↗`)))
        .when(Boolean(c.task) && !this.data?.evidence?.has(c.task), (el) => el.child(div().flex_none().text_size(11.5).text_color(t.dim).child(c.task)))
        .child(div().flex_1())
        .child(div().flex_none().text_size(10.5).text_color(t.dim).child(when(open ? c.created : c.answered)))
        .child(this.chip(t, pushAnswered ? `answered: ${pushAnswered}` : open ? "open" : c.status === "withdrawn" ? "withdrawn" : `answered: ${c.answer}`, open && !pushAnswered ? t.blocked : t.dim))
        .child(this.copier.button(t, `copy-${entry.id}`, () => this.cardText(c, entry, evidence, pushLines))))
      .child(div().text_size(13).font_semibold().text_color(t.foreground).child(sel(`sel-q-${entry.id}`, String(c.question))))
      // A push card: the facts pa-push-main check read now, never the card file's context (push.js).
      .when(push, (el) => el.child(this.push.block(t, space, entry, (_e, cx) => { this.push.check(space, entry, cx); cx.notify(); }, (_e, cx) => { this.push.leave(space.id); this.view = "decisions"; cx.notify(); }, pushLines)))
      // The context, one row per line; a line that starts with two spaces is indented (a worktree card's details).
      // Selectable (t-026): the question, the context rows and the closing lines below; never the option rows.
      .when(!push && Boolean(c.context) && !String(c.context).includes("\n"), (el) => el.child(div().text_size(12).text_color(t.muted).child(sel(`sel-ctx-${entry.id}`, String(c.context)))))
      .when(!push && String(c.context ?? "").includes("\n"), (el) => el.child(v_flex().gap(2).children(String(c.context).split("\n").map((line, i) =>
        div().text_size(12).text_color(t.muted).when(line.startsWith("  "), (d) => d.pl(12)).child(sel(`sel-ctx-${entry.id}-${i}`, line.startsWith("  ") ? line.trimStart() : line))))))
      .when(Boolean(task), (el) => el.children(this.actions(t, task, space)))
      .children([...listed, ...(open ? [[{ label: OTHER, detail: otherDetail }, "other"]] : [])].map(([o, i]) => {
        // The Other row (t-046) is drawn only while the card is open, and is never the recommended one.
        const recommended = i !== "other" && c.recommendation === o.label, chosen = open ? picked === o.label : c.answer === o.label;
        return h_flex().id(`opt-${entry.id}-${i}`).gap(8).px(8).py(6).items_center().rounded(6).border(1)
          .border_color(chosen ? t.accent : recommended && open ? t.input : t.border).bg(chosen ? t.selected : t.background)
          // Picking an option only selects it; the Submit button below sends it, with the note.
          // On a push card, picking Push checks it again (push.js).
          .when(open && !busy, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => {
            this.choice[entry.id] = picked === o.label ? null : o.label;
            if (push && this.choice[entry.id] === PUSH) this.push.check(space, entry, cx);
            cx.notify();
          }))
          .when(open, (el) => el.child(div().flex_none().size(12).rounded(6).border(1).border_color(chosen ? t.accent : t.input).bg(chosen ? t.accent : t.background)))
          .child(div().flex_none().min_w(18).text_size(12.5).font_semibold().text_color(t.foreground).child(String(o.label)))
          .child(div().flex_1().min_w(0).text_size(12).text_color(t.muted).child(String(o.detail ?? "")))
          .when(recommended, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child("recommended")));
      }))
      .when(open && !pushAnswered && Boolean(this.notes[noteKey(space.id, entry.id)]), (el) => el.child(Input.new(this.notes[noteKey(space.id, entry.id)]).h(30).px(8).rounded(6).border_1().border_color(t.input).bg(t.background).text_size(12).text_color(t.foreground))
        .child(h_flex().gap(8).items_center()
          .child(div().id(`submit-${entry.id}`).flex_none().px(12).h(28).flex().items_center().rounded(7).text_size(12.5).font_semibold()
            .bg(ready ? t.solid : t.raised).text_color(ready ? t.solid_foreground : t.dim)
            .when(ready, (b) => b.cursor_pointer().hover((s) => s.opacity(0.88)).on_click((_e, cx) => this.answer(space, entry, picked, cx)))
            .child(running === PUSH ? "Pushing…" : busy ? "Saving…" : picked ? `Submit answer: ${picked}` : "Submit answer"))
          .child(div().flex_1().min_w(0).text_size(10.5).text_color(t.dim).child(push && picked === PUSH && !ready && !busy ? "Push needs a check that passed, under 2 minutes old, that says this panel may push."
            : picked === OTHER ? (!words ? OTHER_EMPTY : words.length > OTHER_MAX ? OTHER_TOO_LONG : OTHER_HINT)
            : picked ? "Add a note above if you want; it is sent with the answer." : "Pick an option, add a note if you want, then submit."))))
      // Closed from the conversation: say so, with the words that decided it (or why it was withdrawn).
      .when(!open && Boolean(c.answeredBy), (el) => el.child(div().text_size(11).text_color(t.dim).child(sel(`sel-by-${entry.id}`, c.status === "withdrawn" ? "Closed by the Project Agent as no longer needed" : `Answered by the ${c.answeredBy}`))))
      .when(!open && Boolean(c.note), (el) => el.child(div().text_size(12).text_color(t.muted).child(sel(`sel-note-${entry.id}`, noteLine(c)))))
      // t-131: a push card answered Other here or in another window, before this copy reads it answered: the person's words.
      .when(pushAnswered === OTHER && Boolean(pushWords), (el) => el.child(div().text_size(12).text_color(t.muted).child(sel(`sel-note-${entry.id}`, `Your answer: “${pushWords}”`))))
      .when(Boolean(earlier), (el) => el.child(div().text_size(11.5).text_color(t.dim).child(sel(`sel-earlier-${entry.id}`, earlier))))
      .when(!open && c.status === "answered" && Boolean(c.rule) && isOther(c), (el) => el.child(div().text_size(11.5).text_color(t.dim).child(RULE_STAYS)))
      .when(Boolean(markLine), (el) => el.child(div().text_size(11.5).text_color(m?.state === "unsure" ? t.blocked : t.dim).child(markLine)))
      .when(Boolean(this.failed[entry.id]), (el) => el.child(div().text_size(11.5).text_color(t.blocked).child(this.failed[entry.id])));
  }

  render(cx) {
    if (this.worker) return this.worker.render();
    if (this.setup) return this.setup.render();
    if (props()?.timer) return div();
    const t = theme();
    const space = spaces().find((s) => s.active);
    this.wake(cx);
    if (space?.id !== this.space && space && !this.inFlight(space.id)) this.reload(cx);
    // A press anywhere in the panel but the armed Delete and its Cancel (they stop it) disarms the Delete.
    const root = v_flex().id("project-agent").size_full().min_h(0).bg(t.chrome).text_color(t.foreground)
      .on_mouse_down("left", (_e, cx) => { if (this.armed || this.rules.armed) { this.disarm(); cx.notify(); } });
    const head = (title, right) => h_flex().h(34).px(12).gap(8).flex_none().items_center().border_b(1).border_color(t.border)
      .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().child(title)).when(Boolean(right), (el) => el.child(right));
    const note = (text) => div().p(12).text_size(12).text_color(t.dim).child(text);
    if (!space) return root.child(head("Project Agent")).child(note("No space open."));
    const data = space.id === this.space ? this.data : null;
    if (!data) return root.child(head("Project Agent")).child(note("Loading…"));
    if (!data.agent && (data.timedOut || data.failed)) return root.child(head("Project Agent")).child(note("This space's Project Agent did not answer. Trying again shortly.")).children((data.errors ?? []).map((e) => note(e)));
    if (!data.agent) {
      const tabs = new Set(space.tabs.map((x) => x.id));
      const folder = panes().find((p) => tabs.has(p.tab) && p.cwd)?.cwd || space.folder;
      const isHome = !folder || folder.replace(/\/+$/, "") === home() || folder === "~";
      return root.child(head("Project Agent"))
        .child(note(isHome ? "This space is in the home folder, so it has no Project Agent. Open a space in a project folder, or run pa-start <folder> in a terminal here."
          : "This space has no Project Agent yet."))
        .children((data.errors ?? []).map((e) => note(e)))
        .when(!isHome, (el) => el.child(h_flex().px(12).child(div().id("pa-start").px(10).h(28).flex().items_center().rounded(6).bg(t.solid).text_color(t.solid_foreground).text_size(12.5).cursor_pointer()
          .on_click(() => this.start(space, folder)).child(`Start a Project Agent for ${folder.split("/").pop()}`))));
    }
    const a = data.agent;
    // Who is live: the keeper's record whose launcher runs (not agent.json's terminal, which goes
    // stale when the agent is started again elsewhere).
    // What is known: its machine is offline; no current keeper has judged the records (the marks
    // may be a stale copy's); it runs (live); or it does not (down: the only state with Open).
    const offline = Boolean(space.machine) && !machines().some((m) => m.name === space.machine && m.layout === "ready");
    const target = offline ? null : liveTarget(space.id);
    const recs = Object.entries(shared("project-agent.tabs") ?? {}).filter(([, r]) => r?.space === space.id);
    const known = offline ? "offline" : !keeperJudging() ? "unknown" : target ? "live"
      : recs.some(([, r]) => r.restarting) ? "restarting" // t-037 R15: its launcher restarts in place (the keeper holds it)
      : recs.some(([, r]) => r.recovering) ? "restoring" // the keeper restores it in its tab
      : recs.some(([tab, r]) => !r.stopped && !space.tabs.some((x) => x.id === tab)) ? "reopening" // its tab vanished: the keeper reopens it (unless it has stopped: then Open)
      : "down";
    const live = target ? agents().find((x) => x.address === target.address) ?? { status: null } : null;
    const req = (shared(OPEN_REQUESTS) ?? {})[space.id], now = Date.now();
    if (target) { delete this.opening[space.id]; delete this.openMsg[space.id]; delete this.confirmOpen[space.id]; }
    // A recorded launcher that is starting, or waits on an error for Enter: still opening.
    const recTerms = paRecords(space.id).map((r) => terminals().find((x) => x.address === r.terminal && x.state === "live")).filter(Boolean);
    // A stale label on an idle shell (its launcher was killed while starting) does not count.
    const startingNow = recTerms.filter((x) => !idleShell(x, panes().find((p) => p.terminal === x.address))).map(startingLabels).find(Boolean) ?? null;
    const o = this.opening[space.id];
    if (o && req?.why) o.why = req.why; // kept: the keeper may give the request up (and delete it) before the panel's 30 s
    if (o && !startingNow) {
      const opened = recTerms.some((x) => !o.before.includes(x.address)); // the keeper recorded a new tab since the click
      if (now - o.at > (opened ? OPEN_SLOW : OPEN_WAIT)) {
        this.openMsg[space.id] = opened ? "The Project Agent's new tab is open, but its launcher has not started after 3 minutes. Look at its tab."
          : req ? `The Project Agent did not open within 30 s: ${req.why ?? "the request was not taken (the keeper runs in a layout with the Project Agent panel)"}.`
          : o.why ? `The Project Agent did not open: ${o.why}.`
          : "The Project Agent did not open: the keeper took the request but no new tab was recorded. See the notifications for why.";
        delete this.opening[space.id];
      }
    }
    const waiting = known === "down" && (Boolean(this.opening[space.id]) || Boolean(req && now - Number(req.at) < OPEN_WAIT) || Boolean(startingNow));
    const like = known === "down" ? this.confirmOpen[space.id] : null;
    const stateText = { offline: "Machine offline · status unknown", unknown: "Status unknown", restarting: "Restarting…", restoring: "Restoring…", reopening: "Reopening…", down: "Not running" }[known];
    // Newest first: by when a task last changed (else when it was made), then by id.
    const stamp = (x) => String(x.updated ?? x.created ?? "");
    const tasks = [...(data.ledger?.tasks ?? [])].sort((x, y) => stamp(y).localeCompare(stamp(x)) || String(y.id ?? "").localeCompare(String(x.id ?? ""), undefined, { numeric: true }));
    // One section per state, in the order that needs you first; ranked sections in the task order (t-097 J2, R5).
    const tasksOrder = this.taskOrderOf(data);
    const sections = taskSections(tasks, tasksOrder.ranking);
    // t-145: a count per active state (none: "no active tasks"); the Tasks tab counts every task not parked or done.
    const counts = ACTIVE.map((st) => [st, tasks.filter((x) => x.state === st).length]).filter(([, n]) => n > 0).map(([st, n]) => `${st.replace(/-/g, " ")} ${n}`).join(" · ") || "no active tasks";
    const current = tasks.filter((x) => x.state !== "parked" && x.state !== "done").length;
    const passesOn = Boolean(data.schedule) && cadence(data.schedule) > 0 && !data.schedule.paused; // t-145: the pass number only then
    const openCards = this.cards.filter((c) => !c.card || (c.card.status === "open" && !this.mark(space, c))); // one answered here is not counted (t-045)
    // A push card pushed from here stays with the open ones, answered, until the person leaves it (push.js kept).
    const kept = (c) => Boolean(c.card) && c.card.status !== "open" && this.push.kept(space.id, c.id);
    const listed = this.cards.filter((c) => !c.card || c.card.status === "open" || kept(c));
    const answered = this.cards.filter((c) => c.card && c.card.status !== "open" && !kept(c));
    // Another mod asked to show a task (shared "project-agent.show": { space, task, at }): once.
    const ask = shared("project-agent.show");
    if (ask && ask.space === space.id && ask.at !== this.shownAt) {
      this.shownAt = ask.at; this.view = "tasks"; this.open.add(ask.task); this.disarm();
      const state = tasks.find((x) => x.id === ask.task)?.state;
      if (state) this.folded.delete(state);
      if (state === "done" && tasks.filter((x) => x.state === "done").findIndex((x) => x.id === ask.task) >= FINISHED_SHOWN) this.doneArchive.add(space.id); // t-145: past the 10 shown
    }
    const view = this.view ?? (listed.length || !tasks.length ? "decisions" : "tasks");
    const queue = queueRows(data.queue, data.ledger, this.cardsFor === space.id ? this.cards : []); // t-055: drawn only from what the load read
    const places = this.placesOf(space, data); // t-083 J2: the Places section and the task rows' badges
    const onMachine = (address) => (space.machine ? `${space.machine}/${address}` : address); // t-084: a worker's terminal address, as the task rows'
    this.viewShown = view;
    const tab = (id, label) => div().id(`view-${id}`).px(9).h(24).flex().items_center().rounded(6).text_size(12).cursor_pointer()
      .bg(view === id ? t.selected : t.chrome).text_color(view === id ? t.foreground : t.muted).hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => { this.view = id; this.disarm(); if (id !== "decisions") this.push.leave(space.id); if (id === "rules") this.rules.wantAllow(space, cx); if (id === "costs") this.costsShown(space, cx, true); if (id === "tasks") this.machinesWant(space, cx); cx.notify(); }).child(label);
    const unsent = this.unsent.filter((u) => u.spaceId === space.id);
    // t-037 J4 (R14): what the launcher runs against what is installed; the Restart button (R13) while it is live.
    const code = this.codeOf(space, data), badge = codeBadge(code), restartable = known === "live" && !this.restartBusy(space);
    const older = /^older/.test(code?.word ?? ""), changed = [...(data.stale ?? []).slice(0, 3), ...(/tools/.test(older ? code.word : "") ? ["tools"] : []), ...(/core/.test(older ? code.word : "") && !(data.stale ?? []).length ? ["core text"] : [])];
    const restartBox = this.restartBox(t, space, live);
    const costsContext = view === "costs" ? this.costsContext(t, space, data, known) : null;
    const contextToggle = this.contextToggle(t, space, data, known), contextFailed = this.contextFailed(t, space), awake = this.awake.button(t); // t-145: beside the status
    return root
      .child(head(`PA: ${a.name ?? space.name}`, h_flex().flex_none().gap(6).items_center()
        .child(div().size(7).rounded(4).bg(live ? t[live.status] ?? t.idle : t.dim))
        .child(div().text_size(11).text_color(t.dim).child(live ? live.status ?? "running" : stateText))
        .when(Boolean(awake), (el) => el.child(awake)) // t-145: keep awake (awake.js)
        .when(Boolean(contextToggle), (el) => el.child(contextToggle)) // t-145: fresh contexts on or off
        .when(Boolean(badge) && known !== "down", (el) => el.child(div().text_size(11).text_color(older || !code?.capable ? t.working : t.dim).child(badge ?? "")))
        .when(restartable, (el) => el.child(this.restartButton(t, space, code, "pa-restart")))
        .when(known === "down", (el) => el.child(this.openButton(t, space, a, "pa-open", waiting)))))
      .when(Boolean(restartBox), (el) => el.child(restartBox))
      .when(Boolean(contextFailed), (el) => el.child(contextFailed))
      .when((known === "down" || known === "restoring") && Boolean(startingNow?.error), (el) => el.child(div().px(12).py(6).border_b(1).border_color(t.border).text_size(11.5).text_color(t.blocked)
        .child(`The Project Agent is waiting: ${startingNow?.error}. Fix it, then press Enter in its tab.`)))
      .when(Boolean(like), (el) => el.child(v_flex().px(12).py(8).gap(6).flex_none().border_b(1).border_color(t.border).bg(t.working + "1a").text_size(11.5).text_color(t.foreground)
        .child(div().child(`An agent seems to run in tab ${like?.tab} for this project. If it is this Project Agent run by hand, opening another starts a second one. Open anyway?`))
        .child(h_flex().gap(6)
          .child(this.button(t, "pa-open-anyway", "Open anyway", (_e, cx) => this.requestOpen(space, a, cx, true), true))
          .child(this.button(t, "pa-open-cancel", "Cancel", (_e, cx) => { delete this.confirmOpen[space.id]; cx.notify(); })))))
      .when(known === "down" && Boolean(this.openMsg[space.id]), (el) => el.child(div().px(12).py(6).border_b(1).border_color(t.border).text_size(11.5).text_color(t.blocked).child(this.openMsg[space.id] ?? "")))
      .child(v_flex().px(12).py(8).gap(2).flex_none().border_b(1).border_color(t.border).text_size(11.5).text_color(t.dim)
        .child(div().truncate().child(String(a.project ?? "").replace(home(), "~")))
        // t-145: the profile name opens its profile.json in the Files view.
        .child(h_flex().gap(4).items_center().flex_wrap()
          .child(div().flex_none().child("profile"))
          .child(a.profile && data.profileDir ? div().id("pa-profile").flex_none().text_color(t.accent).cursor_pointer().hover((x) => x.text_color(t.foreground))
            .on_click(() => this.showFile(space, data.profileDir, `profile ${a.profile}`, "profile.json")).child(String(a.profile)) : div().flex_none().child(String(a.profile ?? "?")))
          .child(div().min_w(0).child(`· ${counts}${passesOn ? ` · pass ${data.ledger?.passCount ?? 0}` : ""}${this.inFlight(space.id) && now - this.dataAt > CACHE_OLD ? " · updating…" : ""}`))))
      // Its instructions or its tools changed since its launcher started (R14): Restart reloads them (R7: /exit too).
      .when(changed.length > 0 && Boolean(live), (el) => el.child(h_flex().px(12).py(6).gap(8).items_center().border_b(1).border_color(t.border).bg(t.working + "1a").text_size(11.5).text_color(t.foreground)
        .child(div().flex_1().min_w(0).child(`Its instructions or tools changed since it started (${changed.join(", ")}). Restart reloads them (or /exit in its tab).${code?.prompt === "recorded" ? " Restart in place keeps the core text this conversation began with; Restart in a new conversation loads it." : ""}`))
        .when(restartable, (e) => e.child(this.restartButton(t, space, code, "pa-restart-notice")))))
      .child(h_flex().px(8).py(6).gap(4).flex_none().border_b(1).border_color(t.border)
        .child(tab("decisions", openCards.length ? `Decisions · ${openCards.length}` : "Decisions"))
        .child(tab("tasks", `Tasks · ${current}`))
        .child(tab("queue", queueLabel(queue)))
        .child(tab("rules", this.rules.data?.error ? "Rules · !" : `Rules · ${(this.rules.data?.rules ?? []).filter((r) => r.status === "active").length}`))
        .child(tab("schedule", this.scheduleLabel(data))) // t-145: scheduled passes, between Rules and Costs
        .child(tab("costs", this.costs.tabLabel(space.id))))
      .children((data.errors ?? []).map((e) => div().px(12).py(6).text_size(11.5).text_color(t.blocked).child(e)))
      .when(unsent.length > 0 && !live, (el) => el.child(h_flex().px(12).py(6).gap(8).items_center().text_size(11.5).text_color(t.foreground)
        .child(div().flex_1().min_w(0).child(known === "down" ? "Answer saved. The Project Agent is not running."
          : known === "offline" ? "Answer saved. Its machine is offline; the Project Agent is told once it is back and running."
          : "Answer saved. The Project Agent is told once its status is known."))
        .when(known === "down", (e) => e.child(this.openButton(t, space, a, "pa-open-notice", waiting)))))
      .when(unsent.length > 0 && Boolean(live), (el) => el.child(div().px(12).py(6).text_size(11.5).text_color(t.blocked)
        .child(`Saved, but the Project Agent hasn't been told yet (${unsent[0]?.why ?? "sending"}). Trying again every 10 s.`)))
      .when(view === "tasks", (el) => el.child(v_flex().id("pa-tasks").flex_1().min_h(0).p(8).gap(6).overflow_y_scrollbar()
        .when(places.shown, (e) => e.child(renderPlaces(t, places, { folded: this.placesFolded, busy: Boolean(this.placesBusy[space.id]), error: this.placesError[space.id] ?? null,
          mark: this.placeMark?.space === space.id && Date.now() < this.placeMark.until ? this.placeMark.key : null, asOf: atZone(new Date(this.dataAt ?? Date.now()).toISOString()).slice(6),
          onFold: (_e, cx) => { this.placesFolded = !this.placesFolded; this.machinesWant(space, cx); cx.notify(); }, onMove: (id, move, cx) => this.moveJob(space, id, move, cx), onReset: (cx) => this.moveJob(space, null, "reset", cx),
          onWorker: (address) => this.showAgent(onMachine(address)) })))
        .when(!tasks.length, (e) => e.child(note("No tasks yet. Give the Project Agent a goal in its tab.")))
        // t-097 J2 (R2, R15): the order line once, above the sections; Reset order over an unreadable file; a refused write.
        .when(tasks.length > 0, (e) => e.child(h_flex().id("task-order-line").px(4).gap(8).items_center()
          .child(div().flex_1().min_w(0).text_size(11).text_color(tasksOrder.read && tasksOrder.read.error !== null && tasksOrder.read.error !== "missing" ? t.blocked : t.dim).child(taskOrderLine(tasksOrder.read)))
          .when(Boolean(tasksOrder.read && tasksOrder.read.error !== null && tasksOrder.read.error !== "missing"), (x) => x.child(this.button(t, "task-order-reset", "Reset order", (_e, cx) => this.moveTaskOrder(space, null, "reset", null, cx))))))
        .when(Boolean(this.taskOrderError[space.id]), (e) => e.child(div().px(4).text_size(11.5).text_color(t.blocked).child(this.taskOrderError[space.id] ?? "")))
        .children(sections.flatMap((s) => {
          this.wantTaskFocus(s.items.flatMap((i) => (i.kind === "fold" ? i.rows : [i])).map((i) => i.task.id), cx); // t-142
          const folded = this.folded.has(s.state), count = s.items.reduce((n, i) => n + (i.kind === "fold" ? i.count : 1), 0);
          const row = (i) => this.task(t, i.task, space, places.badges[i.task.id] ?? [], i);
          // t-145: done draws its 10 most recent; the rest behind one Archived (n) row.
          const capped = s.state === "done" && s.items.length > FINISHED_SHOWN, archOpen = this.doneArchive.has(space.id);
          const archived = capped ? [div().id("tasks-done-archived").flex_none().px(9).h(26).flex().items_center().rounded(7).border(1).border_color(t.border).text_size(12).text_color(t.muted)
            .cursor_pointer().hover((x) => x.bg(t.hover)).on_click((_e, cx) => { archOpen ? this.doneArchive.delete(space.id) : this.doneArchive.add(space.id); cx.notify(); })
            .child(`${archOpen ? "▾" : "▸"} Archived (${s.items.length - FINISHED_SHOWN})`)] : [];
          const items = capped ? [...s.items.slice(0, FINISHED_SHOWN), "archived", ...(archOpen ? s.items.slice(FINISHED_SHOWN) : [])] : s.items;
          return [
            h_flex().id(`tasks-${s.state}`).h(24).px(4).gap(6).flex_none().items_center().cursor_pointer().text_size(11.5).text_color(t.dim).hover((x) => x.text_color(t.foreground))
              .on_click((_e, cx) => { folded ? this.folded.delete(s.state) : this.folded.add(s.state); cx.notify(); })
              .child(div().child(folded ? "▸" : "▾")).child(div().font_semibold().child(String(s.state ?? "no state").replace(/-/g, " ")))
              .child(div().child(String(count))),
            ...(folded ? [] : items.flatMap((i) => {
              if (i === "archived") return archived;
              if (i.kind !== "fold") return [row(i)];
              const openFold = this.foldOpen.has(`${space.id}:${i.key}`); // R14: starts folded
              return [div().id(`fold-${i.key}`).flex_none().px(9).h(26).flex().items_center().rounded(7).border(1).border_color(t.border).text_size(12).text_color(t.muted)
                .cursor_pointer().hover((x) => x.bg(t.hover)).on_click((_e, cx) => { const k = `${space.id}:${i.key}`; this.foldOpen.has(k) ? this.foldOpen.delete(k) : this.foldOpen.add(k); cx.notify(); })
                .child(foldLine(i, openFold)), ...(openFold ? i.rows.map(row) : [])];
            })),
          ];
        }))))
      .when(view === "queue", (el) => el.child(renderQueue(t, queue, { isLive: (a) => terminals().some((x) => x.address === onMachine(a) && x.state === "live"), onWorker: (a) => this.showAgent(onMachine(a)),
        evidence: data.evidence, onFile: (task, file) => this.showEvidence(space, task, file), // t-084: worker and evidence links
        mergedOpen: (path) => this.mergedOpen.has(`${space.id}\0${path}`), onMerged: (path, cx) => { const k = `${space.id}\0${path}`; this.mergedOpen.has(k) ? this.mergedOpen.delete(k) : this.mergedOpen.add(k); cx.notify(); } }))) // t-145
      .when(view === "costs", (el) => el.when(Boolean(costsContext), (e) => e.child(costsContext)).child(this.costs.render(t, space, (shared("project-agent.costs") ?? {})[space.id] ?? null))) // t-145: the context rows on top
      .when(view === "schedule", (el) => el.child(v_flex().id("pa-schedule").flex_1().min_h(0).overflow_y_scrollbar().child(this.scheduleBar(t, space, data)))) // t-145
      .when(view === "rules", (el) => el.child(this.rules.render(t, space, (id, what, cx) => this.ruleDecided(space, id, what, cx))))
      .when(view === "decisions", (el) => el.child(v_flex().id("pa-decisions").flex_1().min_h(0).p(8).gap(8).overflow_y_scrollbar().track_scroll(this.cardScroll)
        .when(!listed.length && this.cardsFor !== space.id, (e) => e.child(note("Loading decisions…")))
        .when(!listed.length && this.cardsFor === space.id, (e) => e.child(note("No open decisions. The Project Agent opens a card here when it needs you.")))
        .children(listed.map((c) => this.card(t, c, space)))
        .when(this.cards.missing > 0, (e) => e.child(note(`${this.cards.missing} card${this.cards.missing === 1 ? "" : "s"} could not be read now; the next load reads ${this.cards.missing === 1 ? "it" : "them"} again.`))) // t-131: never silently missing
        .when(answered.length > 0 || this.cards.omitted > 0, (e) => e.child(div().id("pa-older").px(4).py(4).text_size(11.5).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.foreground))
          .on_click((_e, cx) => { this.older = !this.older; cx.notify(); }).child(`${this.older ? "▾" : "▸"} ${answered.length} answered${this.cards.omitted > 0 ? ` · ${this.cards.omitted} older not shown` : ""}`)))
        // t-145: the 10 most recent finished cards; the rest, and decisions-archive/ (read on its open), behind Archived (n).
        .when(this.older, (e) => e.children(answered.slice(0, FINISHED_SHOWN).map((c) => this.card(t, c, space))))
        .when(this.older, (e) => e.child(this.archiveLine(t, space, Math.max(0, answered.length - FINISHED_SHOWN))))
        .when(this.older && Boolean(this.archive[space.id]?.open), (e) => e.children(answered.slice(FINISHED_SHOWN).map((c) => this.card(t, c, space)))
          .children((this.archive[space.id]?.cards ?? []).filter((c) => !this.cards.some((x) => x.id === c.id)).slice(0, 50).map((c) => this.card(t, c, space))))));
  }
}
