// Project Agent panel: the current space's Project Agent, its decision cards, its tasks,
// and the buttons its profile declares. Generic: profiles add data and scripts, never code.
// Reads ~/.config/herdr-pa (on the space's machine). Writes only decision answers and rule changes.
// Runs a program only on a click: a profile action, `hc send` after an answer, or the
// launcher that starts a Project Agent. Push cards (push.js, t-007b): a shown copy runs the
// read-only `pa-push-main check` when one first appears and on a click, and the Rules view the
// read-only `pa-push-allow show`; `pa-push-main run` runs only from a push card's Submit.
// The Costs view (costs.js, t-009) reads costs/summary.json only while it is shown, and runs the
// read-only `pa-costs billed compare`; its Refresh, Reconstruct and Turn off run `pa-costs` on a click.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, ScrollHandle } from "gpui-base";
import { spaces, panes, agents, terminals, machines, theme, run, exec, props, home, watchFolder, notify, shared, setShared, closeOverlay, openOverlay, window, layout, readTextFile } from "herdr";
import { loadSpace, loadLedger, loadProfile, loadDecisions, loadRules, withTimeout, TIMED_OUT, READ_LIMIT, answerDecision, notifyOrchestrator, changeRules, touched, localId, paHome, stateDir, taskActions, taskLinks, evidenceTasks, cardEvidence, staleInstructions, liveTarget, lookAlike, OPEN_REQUESTS, keeperJudging, startingLabels } from "./store.js";
import { RulesPane, arm, armHolds, fieldState } from "./rules.js"; // arm, armHolds: the two-click Delete (ARM_WAIT, 10 s)
import { SetupPane } from "./setup.js";
import { Keeper, idleShell } from "./keeper.js";
import { WorkerPane } from "./worker.js";
import { PushCards, isPush, shownOnly, PUSH, PUSH_OPTIONS, RUN_TIMEOUT, GRACE } from "./push.js";
import { CostsPane } from "./costs.js";
import { Copier, sel } from "./copy.js";
import { tick, cadence, holdReason, setSchedule, requestPass, PRESETS, MIN_MINUTES } from "./timer.js";

const ACTIVE = ["needs-decision", "review", "in-progress", "investigating", "queued"];
const ORDER = [...ACTIVE, "parked", "done"];
const OPEN_WAIT = 30000; // an Open request with nothing started by then: say why, offer the button again
const OPEN_SLOW = 180000; // its new tab is recorded but its launcher has not started by then: say so
const paRecords = (spaceId) => Object.values(shared("project-agent.tabs") ?? {}).filter((r) => r?.space === spaceId);
const LOAD_LIMIT = READ_LIMIT + 1000; // a load with no read settled or timed out for this long is given up: a new one may start
const CACHE_MAX = 8; // spaces whose last load is kept (in memory), the least recently shown dropped first
const CACHE_OLD = 60000; // a kept load older than this says "updating…" while its refresh runs
const COSTS_MARK = 20000; // while Costs is shown: shared "project-agent.costsShown" is stamped at most this often (the timer collects faster then)
const COUNT_EVERY = 10000; // while hidden: at most one card read and one ledger read per space this often
const PROFILE_RETRY = 60000; // while hidden: a profile read that failed is tried again no sooner than this
// A part of loadSpace's result that did not answer (its `unknown`), and the fields it fills.
const PARTS = { ledger: ["ledger"], profile: ["profile", "profileDir"], schedule: ["schedule"], request: ["request"] };
const NOTE = "Note for the Project Agent (optional)";
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
    this.data = null; // loadSpace() result for this.space (after the first paint, the cards and the rest are still coming)
    this.cards = []; // loadDecisions() result
    this.space = null;
    this.cardsFor = null; // the space this.cards belong to
    this.dataAt = 0; // when the data shown was loaded
    this.cache = new Map(); // space id -> { data, cards, rules, at }: its last finished load (most recently shown last)
    this.loads = {}; // space id -> { token, at }: the load in flight (at: its last progress)
    this.committed = {}; // space id -> the token of its last load that committed; an older one commits nothing
    this.seq = 0;
    this.hidden = {}; // space id -> { at, busy, seq }: the hidden panel's last count and ledger read
    this.wrote = {}; // space id -> the seq (start order) of the last read, full load or hidden, whose data went to its shared keys; an older one writes none
    this.again = {}; // space id -> a reload came while its load was in flight: one more when it ends (if the space is still shown)
    this.profiles = {}; // space id -> { at, busy, done, profile }: its profile, read once per session (a full load's counts)
    this.asleep = false; // not on screen: only the open-card count is read
    this.view = null; // "decisions" | "tasks" | "rules" | "costs"; null: decisions when any is open
    this.viewShown = null; // the view the last render drew (this.view, or the one picked for null)
    this.rules = new RulesPane((options) => this.field(options));
    this.copier = new Copier(); // the Copy controls of cards and tasks (copy.js, t-026)
    // push cards: the last pa-push-main check and run of each (push.js); its runs take the shared mark (t-045 round 2)
    this.push = new PushCards((spaceId, id, label, running, unknown) => this.sharePush(spaceId, id, label, running, unknown), (spaceId, id, asked) => this.openedPush(spaceId, id, asked));
    // The Costs view: each space's last summary and billed comparison (costs.js); it reads and runs only while
    // this check holds (asked live: `asleep` is updated only by the next reload).
    this.costs = new CostsPane((id) => this.view === "costs" && spaces().find((s) => s.active)?.id === id && onScreen());
    this.costsMarked = {}; // space id -> when this copy last stamped "project-agent.costsShown"
    this.open = new Set(); // expanded task ids
    this.folded = new Set(["done", "parked"]); // task sections folded away (finished work starts folded)
    this.older = false; // answered cards shown
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
    this.unsent = []; // [{ spaceId, message, why? }] notices the orchestrator hasn't had yet (its terminal is looked up when sending)
    this.opening = {}; // space id -> { at, before }: when this copy asked the keeper to open its Project Agent, and the record terminals then
    this.openMsg = {}; // space id -> why the last Open did not start anything
    this.confirmOpen = {}; // space id -> { tab, terminal }: a look-alike agent runs; asking first
    this.me = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`; // this copy, for the timer lease
    this.schedule = { open: false, error: null, custom: null, quietFrom: null, quietTo: null }; // the schedule bar
    cx.timer.every(15000, (cx) => cx.spawn(async (cx) => { await tick(this.me); if (!props()?.timer) this.reload(cx); }));
    // The keeper (Project Agent tabs: tracking, ordering, reopening, the new-space dialog) runs in
    // the copy that holds the timer lease, so it runs once however many copies a layout has.
    this.keeper = new Keeper(this.me);
    const mine = () => shared("project-agent.timer")?.id === this.me;
    let keeping = false; // this copy held the lease last tick: read heartbeats and agent.json at once when it takes it
    cx.spawn(async (cx) => { await tick(this.me); if (mine()) { keeping = true; this.keeper.readBeats(cx); } });
    cx.timer.every(1500, (cx) => {
      if (!mine()) { keeping = false; return; }
      if (!keeping) { keeping = true; this.keeper.readBeats(cx); }
      this.keeper.syncPA(cx);
    });
    cx.timer.every(250, (cx) => { if (mine() && this.keeper.busy()) this.keeper.syncPA(cx); }); // while a restore runs
    cx.timer.every(400, () => { if (mine()) this.keeper.offerSetup(); });
    cx.timer.every(15000, (cx) => { if (mine()) this.keeper.readBeats(cx); });
    if (props()?.timer) return; // the always-on copy only keeps time; it shows nothing
    this.reload(cx);
    cx.timer.every(10000, (cx) => { this.reload(cx); this.resend(cx); });
    cx.timer.every(1000, (cx) => { this.wake(cx); this.costsDue(cx); if ((this.armed && !this.armedTask(this.space)) || (this.rules.armed && !this.rules.armedRule(this.space))) cx.notify(); }); // shown again (render may not run for a hidden copy); an armed Delete timed out
    cx.spawn(async (cx) => {
      for (;;) {
        try { await watchFolder(`${paHome(null)}/spaces`); } catch { return; }
        this.reload(cx);
      }
    });
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
    for (const k of open) this.notes[k] ??= this.field({ placeholder: NOTE });
    return open.size;
  }

  // Shown again after a time off screen: what it has shows at once; one full reload starts.
  wake(cx) {
    if (!this.asleep || !onScreen()) return;
    this.asleep = false;
    this.reload(cx);
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
    cx.spawn(async (cx) => {
      const rcx = { sleep: (ms) => cx.sleep(ms) }; // only what the store's cap uses
      const [cards, ledger] = await Promise.all([
        loadDecisions(space, { cx: rcx, batchOnly: true }).catch(() => null),
        loadLedger(space, rcx).catch(() => null),
      ]);
      mine.busy = false;
      if (!this.asleep || mine.seq < (this.wrote[space.id] ?? 0)) return;
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
    });
  }

  // While hidden with no kept load whose profile answered: the space's profile, for "project-agent.mainBranches". Read once
  // per session once it has answered; a failed or timed-out read is tried again after PROFILE_RETRY.
  readProfile(cx, space) {
    const p = this.profiles[space.id] ??= {}, now = Date.now();
    if (p.done || p.busy || (p.at && now - p.at < PROFILE_RETRY)) return;
    p.at = now; p.busy = true;
    cx.spawn(async (cx) => {
      let r = { answered: false };
      try { r = await loadProfile(space, props()?.profile, { sleep: (ms) => cx.sleep(ms) }); } catch {}
      p.busy = false;
      if (!r.answered || p.done) return;
      p.done = true; p.profile = r.profile;
      if (this.asleep && r.profile !== undefined && !this.knownProfile(space.id)) this.shareMains(space, r.profile);
    });
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
  reload(cx) {
    const space = spaces().find((s) => s.active);
    if (!space) { this.data = null; this.space = null; cx.notify(); return; }
    if (space.id !== this.space) { this.disarm(); this.adopt(space, cx); }
    if (!onScreen()) { this.asleep = true; this.hiddenRead(cx, space); return; }
    this.asleep = false;
    if (this.view === "costs") this.costsShown(space, cx); // its own read, outside the load (costs.js); never while hidden
    if (this.inFlight(space.id)) { this.again[space.id] = true; return; }
    const token = ++this.seq, load = { token, at: Date.now() };
    this.loads[space.id] = load;
    const current = () => this.loads[space.id] === load;
    const shown = () => spaces().find((s) => s.active)?.id === space.id;
    cx.spawn(async (cx) => {
      const rcx = { sleep: (ms) => cx.sleep(ms), progress: () => { load.at = Date.now(); } }; // the store's reads move `at`
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
          this.data = { ...data, evidence: same ? this.data?.evidence : undefined, stale: same ? this.data?.stale : undefined };
          if (this.cardsFor !== space.id) { this.cards = []; this.cardsFor = null; this.rules.data = null; }
          if (!same) this.dataAt = Date.now();
          this.space = space.id;
          cx.notify();
        }
        if (data.agent) {
          // A read that times out or fails keeps the last known value: the kept load's, else what is shown for the space.
          const cap = (p, fallback) => withTimeout(rcx, p).then((v) => (v === TIMED_OUT ? fallback() : v), () => fallback());
          const known = [...(kept && !kept.cardsUnknown ? kept.cards : []), ...(this.space === space.id && this.cardsFor === space.id ? this.cards : [])];
          let evidence, stale;
          [cards, rules, evidence, stale] = await Promise.all([
            loadDecisions(space, { cx: rcx, live: current, known }),
            cap(loadRules(space), () => kept?.rules ?? (this.space === space.id && this.cardsFor === space.id ? this.rules.data : null) ?? null), // null: the Rules view says Loading…
            cap(evidenceTasks(space), () => kept?.data?.evidence ?? shownNow()?.evidence), // undefined: unknown
            cap(staleInstructions(space, data.agent.profile), () => kept?.data?.stale ?? shownNow()?.stale ?? []),
          ]);
          data.evidence = evidence; data.stale = stale;
        }
      }
      catch (e) { data = { agent: null, errors: [String(e.message ?? e)], failed: true }; } // a read in the first round threw
      const wasCurrent = current();
      if (wasCurrent) delete this.loads[space.id];
      // Given up with its card fallback skipped (no result), or a newer load of this space has committed.
      if (!((cards === null && !wasCurrent) || token < (this.committed[space.id] ?? 0))) this.commit(space, token, data, cards, rules, cx, missed);
      if (wasCurrent && this.again[space.id]) { delete this.again[space.id]; if (shown()) this.reload(cx); }
    });
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
  commit(space, token, data, cards, rules, cx, missed = []) {
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
      data.errors = [...(data.errors ?? []), src ? "decision cards: did not answer; the last known are shown" : "decision cards: did not answer"];
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
    const trees = (data.ledger?.tasks ?? []).filter((x) => x.worktree && x.id).map((x) => ({ task: x.id, path: String(x.worktree), title: String(x.title ?? "") }));
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
      if (x.worktree) spots.push({ label: `${x.id} worktree`, detail: String(x.title ?? ""), path: String(x.worktree), group: "Project Agent", task: x.id });
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
    delete this.choice[entry.id];
    delete this.failed[entry.id];
    const note = (this.notes[noteKey(space.id, entry.id)]?.value() ?? "").trim();
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
        // its own message: the answer is saved and stays.
        const decided = entry.card?.rule && (/^approve/i.test(label) ? "active" : /^decline/i.test(label) ? "declined" : null);
        if (decided) {
          try { await changeRules(space, (rules) => rules.map((r) => (r.id === entry.card.rule && r.status === "proposed" ? touched({ ...r, status: decided }, decided === "active" ? "approved" : "declined") : r))); }
          catch (e) { this.failed[entry.id] = `The answer was saved; the rule was not changed: ${String(e.message ?? e)}`; }
        }
        this.unsent.push({ spaceId: space.id, message: `decision ${entry.id} answered` });
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
  sharePush(spaceId, id, label, running, unknown = false) {
    const k = noteKey(spaceId, id);
    if (label === null) { if ((shared(ANSWERING) ?? {})[k]?.by === this.me) this.shareMark(k, null); return; }
    this.shareMark(k, { label, note: null, at: Date.now(), by: this.me, ...(running ? { ttl: RUN_TIMEOUT + GRACE + ANSWERING_TTL } : {}), ...(unknown ? { unknown: true } : {}) });
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
    try { text = await withTimeout({ sleep: (ms) => cx.sleep(ms) }, readTextFile(entry.path, { on: space.machine ?? null })); } catch { text = TIMED_OUT; }
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
            try { await notifyOrchestrator(target.address, item.message, { sleep: (ms) => cx.sleep(ms) }); } // capped (store.js SEND_LIMIT)
            catch (e) { held.add(item.spaceId); left.push({ ...item, why: String(e.message ?? e) }); }
          }
        } finally {
          this.unsent = [...left, ...items.slice(at), ...this.unsent]; // kept ones first: anything queued meanwhile comes after
          this.sending = false;
        }
        cx.notify();
      });
    } catch { this.sending = false; }
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

  // The person's own change to a task: done, park, reopen, delete (to the trash). Runs pa-task on
  // the space's machine; it edits the ledger under a lock and tells the Project Agent.
  changeTask(space, task, action, cx) {
    delete this.taskError[task.id]; this.disarm();
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec("pa-task", [action, String(task.id), "--space", localId(space.id)], { on: space.machine ?? null, timeout: 30000 });
        if (r.code !== 0) this.taskError[task.id] = (r.stderr || r.stdout || `pa-task exited with ${r.code}`).trim().slice(0, 300);
      } catch (e) { this.taskError[task.id] = String(e.message ?? e); }
      this.reload(cx); cx.notify();
    });
  }

  // The task id whose Delete is armed, if the arm still holds: same space, Tasks view, the task
  // expanded, under ARM_WAIT old (the view the last render drew: a new open card can switch it). One that no longer holds is dropped (it never comes back).
  armedTask(spaceId) {
    const a = this.armed;
    if (armHolds(a, spaceId, a && this.viewShown === "tasks" && this.open.has(a.id))) return a.id;
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
    cx.spawn(async () => { try { await exec(opener, [url], { timeout: 20000 }); } catch (e) { notify({ title: "Couldn't open the link", body: String(e.message ?? e), level: "error" }); } });
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
        const r = action.local ? await exec(action.argv[0], action.argv.slice(1), { timeout: 20000 })
          : await exec(action.argv[0], action.argv.slice(1), { cwd: this.data?.agent?.project, on: space.machine ?? null, timeout: 60000 });
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

  task(t, task, space) {
    const open = this.open.has(task.id), armed = open && this.armedTask(space.id) === task.id;
    // A press on the armed Delete or its Cancel stops here: a press anywhere else in the panel disarms (render's root).
    const keep = (el) => el.on_mouse_down("left", (_e, cx) => cx.stop_propagation());
    const crew = (task.workers ?? []).filter((w) => !(w?.finished && w?.closed));
    const tone = { "needs-decision": t.blocked, review: t.done, "in-progress": t.working, investigating: t.working, queued: t.muted, parked: t.dim, done: t.dim }[task.state] ?? t.muted;
    return v_flex().id(`task-${task.id}`).flex_none().rounded(7).border(1).border_color(t.border).bg(t.surface)
      .child(h_flex().id(`task-head-${task.id}`).h(32).px(9).gap(7).items_center().cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { if (open) { this.open.delete(task.id); if (this.armed?.id === task.id) this.armed = null; } else this.open.add(task.id); cx.notify(); })
        .child(div().flex_none().text_size(11).text_color(t.dim).child(open ? "▾" : "▸"))
        .child(div().flex_none().text_size(11.5).text_color(t.dim).child(String(task.id ?? "?")))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(t.foreground).child(String(task.title ?? "(untitled)")))
        .when(Boolean(task.phase), (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child(String(task.phase))))
        .child(this.chip(t, String(task.state ?? "?"), tone)))
      .when(open, (el) => el.child(v_flex().px(10).pb(9).gap(6).text_size(12).text_color(t.muted)
        .when(Boolean(task.goal), (e) => e.child(div().child(sel(`sel-goal-${task.id}`, String(task.goal)))))
        .child(div().text_size(11).text_color(t.dim).child(sel(`sel-kind-${task.id}`, [task.type, task.tier && `${task.tier} tier`].filter(Boolean).join(" · "))))
        // Its workers: a live one opens in the Agents view. One closed with pa-worker close (finished,
        // closed set) is not drawn; its ledger entry stays (only the panel leaves it out). None left: no row.
        .when(crew.length > 0, (e) => e.child(h_flex().gap(6).flex_wrap().children(crew.map((w, i) => {
          const address = space.machine ? `${space.machine}/${w.address}` : w.address;
          const term = terminals().find((x) => x.address === address && x.state === "live");
          // Read-only marks the tools write: "kept" (pa-worker keep); "(closed)" only for a finished entry with no closed stamp.
          const label = `${w.role ?? "worker"} ${w.address ?? ""}`.trim() + (w.keep ? " · kept" : "");
          return h_flex().id(`worker-${task.id}-${i}`).flex_none().px(8).h(24).gap(6).items_center().rounded(6).border(1).border_color(t.border).text_size(11.5)
            .text_color(term ? t.foreground : t.dim)
            .when(Boolean(term), (b) => b.cursor_pointer().hover((s) => s.bg(t.hover)).on_click(() => this.showAgent(address)))
            .child(div().size(6).rounded(3).bg(term ? t[term.status] ?? t.idle : t.dim))
            .child(term ? `${label} ↗` : w.finished ? `${label} (closed)` : `${label} (ended)`)
            // The × opens the delete dialog: the person's way to close any worker (the agent closes finished ones itself).
            // It stops the press, so it drops an armed Delete itself (the root never sees this press).
            .child(div().id(`worker-x-${task.id}-${i}`).flex_none().px(3).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.blocked))
              .on_mouse_down("left", (_e, cx) => { cx.stop_propagation(); if (this.armed || this.rules.armed) { this.disarm(); cx.notify(); } })
              .on_click((_e, cx) => { cx.stop_propagation(); openOverlay({ mod: "project-agent", kind: "dialog", width: 540, height: 400, props: { deleteWorker: { space: space.id, machine: space.machine ?? null, address, name: w.name ?? label } } }); })
              .child("×"));
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
          .when(task.state !== "done", (e) => e.child(this.button(t, `done-${task.id}`, "Mark done", (_e, cx) => this.changeTask(space, task, "done", cx))))
          .when(task.state !== "done" && task.state !== "parked", (e) => e.child(this.button(t, `park-${task.id}`, "Park", (_e, cx) => this.changeTask(space, task, "park", cx))))
          // Two clicks on the same button: the first arms it (ARM_WAIT; Cancel, a press elsewhere, collapsing, another view or space disarm).
          .child(keep(div().id(`delete-${task.id}`)).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(armed ? t.blocked : t.border)
            .text_size(11.5).text_color(armed ? t.blocked : t.muted).cursor_pointer().hover((s) => s.bg(t.hover))
            // The arm is asked again at the click (t-028): one that ran out since this was drawn only arms again.
            .on_click((_e, cx) => { cx.stop_propagation(); if (this.armedTask(space.id) === task.id) this.changeTask(space, task, "delete", cx); else { this.armed = arm(task.id, space.id); cx.notify(); } })
            .child(armed ? "Delete the task, its evidence and its cards?" : "Delete…"))
          .when(armed, (e) => e.child(keep(this.button(t, `delete-cancel-${task.id}`, "Cancel", (_e, cx) => { cx.stop_propagation(); this.disarm(); cx.notify(); })))))
        .when(Boolean(this.taskError[task.id]), (e) => e.child(div().text_size(11.5).text_color(t.blocked).child(this.taskError[task.id])))));
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
    const open = c.status === "open", options = isPush(c) ? PUSH_OPTIONS : c.options ?? [];
    return [
      [entry.id, c.task, open ? "open" : c.status === "withdrawn" ? "withdrawn" : `answered: ${c.answer}`, when(open ? c.created : c.answered)].filter(Boolean).join(" · "),
      String(c.question),
      ...(isPush(c) ? pushLines : c.context ? String(c.context).split("\n") : []),
      ...(options.length ? ["Options:", ...options.map((o) => `  ${o.label}${o.detail ? `: ${o.detail}` : ""}${c.recommendation === o.label ? " (recommended)" : ""}`)] : []),
      ...(evidence ? [`Evidence: ${evidence}`] : []),
      ...(!open && c.status !== "withdrawn" && c.answer ? [`Answer: ${c.answer}`] : []),
      ...(!open && c.answeredBy ? [c.status === "withdrawn" ? "Closed by the Project Agent as no longer needed" : `Answered by the ${c.answeredBy}`] : []),
      ...(!open && c.note ? [c.status === "withdrawn" ? `Why: ${c.note}` : c.answeredBy ? `You said: “${c.note}”` : `Note: ${c.note}`] : []),
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
    if (m && !push) c = { ...c, status: "answered", answer: m.label, note: m.note, answered: isoAt(m.at) };
    const markLine = m?.state === "saving" ? "Saving…" : m?.state === "unsure" ? "Could not tell whether the answer was saved; reading the card again."
      : m?.state === "elsewhere" && m.unknown ? "A push from another panel window has an unknown result: locked until a check from here reads the card open (Check again)."
      : m?.state === "elsewhere" ? "Answered in another panel window." : null;
    const pushAnswered = push && c.status === "open" ? this.push.answeredHere(space.id, entry.id) ?? (m?.unknown ? null : m?.label) ?? null : null; // a run from here (or another window) answered it
    const open = c.status === "open", busy = Boolean(this.busy[entry.id]) || Boolean(running) || (push && (this.push.locked(space.id, entry.id) || Boolean(m)));
    const task = c.task && (this.data?.ledger?.tasks ?? []).find((x) => x.id === c.task);
    const picked = open ? this.choice[entry.id] ?? null : null;
    const ready = Boolean(picked) && !busy && !(push && shownOnly(entry)) && (!push || picked !== PUSH || this.push.canSubmit(space.id, entry.id)); // Push: a fresh check that passed; a shown-only card: never
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
      .children((push ? PUSH_OPTIONS : c.options ?? []).map((o, i) => {
        const recommended = c.recommendation === o.label, chosen = open ? picked === o.label : c.answer === o.label;
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
            : picked ? "Add a note above if you want; it is sent with the answer." : "Pick an option, add a note if you want, then submit."))))
      // Closed from the conversation: say so, with the words that decided it (or why it was withdrawn).
      .when(!open && Boolean(c.answeredBy), (el) => el.child(div().text_size(11).text_color(t.dim).child(sel(`sel-by-${entry.id}`, c.status === "withdrawn" ? "Closed by the Project Agent as no longer needed" : `Answered by the ${c.answeredBy}`))))
      .when(!open && Boolean(c.note), (el) => el.child(div().text_size(12).text_color(t.muted).child(sel(`sel-note-${entry.id}`, c.status === "withdrawn" ? `Why: ${c.note}` : c.answeredBy ? `You said: “${c.note}”` : `Note: ${c.note}`))))
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
    const stateText = { offline: "Machine offline · status unknown", unknown: "Status unknown", restoring: "Restoring…", reopening: "Reopening…", down: "Not running" }[known];
    // Newest first: by when a task last changed (else when it was made), then by id.
    const stamp = (x) => String(x.updated ?? x.created ?? "");
    const tasks = [...(data.ledger?.tasks ?? [])].sort((x, y) => stamp(y).localeCompare(stamp(x)) || String(y.id ?? "").localeCompare(String(x.id ?? ""), undefined, { numeric: true }));
    // One section per state, in the order that needs you first.
    const sections = [...ORDER, ...new Set(tasks.map((x) => x.state).filter((s) => !ORDER.includes(s)))]
      .map((state) => ({ state, tasks: tasks.filter((x) => x.state === state) })).filter((s) => s.tasks.length);
    const active = tasks.filter((x) => ACTIVE.includes(x.state)).length;
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
    }
    const view = this.view ?? (listed.length || !tasks.length ? "decisions" : "tasks");
    this.viewShown = view;
    const tab = (id, label) => div().id(`view-${id}`).px(9).h(24).flex().items_center().rounded(6).text_size(12).cursor_pointer()
      .bg(view === id ? t.selected : t.chrome).text_color(view === id ? t.foreground : t.muted).hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => { this.view = id; this.disarm(); if (id !== "decisions") this.push.leave(space.id); if (id === "rules") this.rules.wantAllow(space, cx); if (id === "costs") this.costsShown(space, cx, true); cx.notify(); }).child(label);
    const unsent = this.unsent.filter((u) => u.spaceId === space.id);
    return root
      .child(head(`PA: ${a.name ?? space.name}`, h_flex().flex_none().gap(6).items_center()
        .child(div().size(7).rounded(4).bg(live ? t[live.status] ?? t.idle : t.dim))
        .child(div().text_size(11).text_color(t.dim).child(live ? live.status ?? "running" : stateText))
        .when(known === "down", (el) => el.child(this.openButton(t, space, a, "pa-open", waiting)))))
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
        .child(div().child(`profile ${a.profile ?? "?"} · ${active} active of ${tasks.length} tasks · pass ${data.ledger?.passCount ?? 0}${this.inFlight(space.id) && now - this.dataAt > CACHE_OLD ? " · updating…" : ""}`)))
      // The agent reads its instructions when its session starts; say when they have changed since.
      .when((data.stale ?? []).length > 0 && Boolean(live), (el) => el.child(div().px(12).py(6).border_b(1).border_color(t.border).bg(t.working + "1a").text_size(11.5).text_color(t.foreground)
        .child(`Its instructions changed since this agent started (${data.stale.slice(0, 3).join(", ")}). Type /exit in its tab: it restarts in place, keeps the conversation and loads them.`)))
      .child(this.scheduleBar(t, space, data))
      .child(h_flex().px(8).py(6).gap(4).flex_none().border_b(1).border_color(t.border)
        .child(tab("decisions", openCards.length ? `Decisions · ${openCards.length}` : "Decisions"))
        .child(tab("tasks", `Tasks · ${tasks.length}`))
        .child(tab("rules", this.rules.data?.error ? "Rules · !" : `Rules · ${(this.rules.data?.rules ?? []).filter((r) => r.status === "active").length}`))
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
        .when(!tasks.length, (e) => e.child(note("No tasks yet. Give the Project Agent a goal in its tab.")))
        .children(sections.flatMap((s) => {
          const folded = this.folded.has(s.state);
          return [
            h_flex().id(`tasks-${s.state}`).h(24).px(4).gap(6).flex_none().items_center().cursor_pointer().text_size(11.5).text_color(t.dim).hover((x) => x.text_color(t.foreground))
              .on_click((_e, cx) => { folded ? this.folded.delete(s.state) : this.folded.add(s.state); cx.notify(); })
              .child(div().child(folded ? "▸" : "▾")).child(div().font_semibold().child(String(s.state ?? "no state").replace(/-/g, " ")))
              .child(div().child(String(s.tasks.length))),
            ...(folded ? [] : s.tasks.map((task) => this.task(t, task, space))),
          ];
        }))))
      .when(view === "costs", (el) => el.child(this.costs.render(t, space, (shared("project-agent.costs") ?? {})[space.id] ?? null)))
      .when(view === "rules", (el) => el.child(this.rules.render(t, space, (id, what, cx) => this.ruleDecided(space, id, what, cx))))
      .when(view === "decisions", (el) => el.child(v_flex().id("pa-decisions").flex_1().min_h(0).p(8).gap(8).overflow_y_scrollbar().track_scroll(this.cardScroll)
        .when(!listed.length && this.cardsFor !== space.id, (e) => e.child(note("Loading decisions…")))
        .when(!listed.length && this.cardsFor === space.id, (e) => e.child(note("No open decisions. The Project Agent opens a card here when it needs you.")))
        .children(listed.map((c) => this.card(t, c, space)))
        .when(answered.length > 0, (e) => e.child(div().id("pa-older").px(4).py(4).text_size(11.5).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.foreground))
          .on_click((_e, cx) => { this.older = !this.older; cx.notify(); }).child(`${this.older ? "▾" : "▸"} ${answered.length} answered`)))
        .when(this.older, (e) => e.children(answered.slice(0, 50).map((c) => this.card(t, c, space))))));
  }
}
