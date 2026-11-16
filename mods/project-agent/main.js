// Project Agent panel: the current space's Project Agent, its decision cards, its tasks,
// and the buttons its profile declares. Generic: profiles add data and scripts, never code.
// Reads ~/.config/herdr-pa (on the space's machine). Writes only decision answers and rule changes.
// Runs a program only on a click: a profile action, `hc send` after an answer, or the
// launcher that starts a Project Agent.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, ScrollHandle } from "gpui-base";
import { spaces, panes, agents, terminals, theme, run, exec, props, home, watchFolder, notify, shared, setShared, closeOverlay, openOverlay, window, layout } from "herdr";
import { loadSpace, loadDecisions, answerDecision, notifyOrchestrator, changeRules, touched, localId, paHome, stateDir, taskActions, taskLinks, evidenceTasks, cardEvidence, staleInstructions, liveTarget, lookAlike, OPEN_REQUESTS } from "./store.js";
import { RulesPane } from "./rules.js";
import { SetupPane } from "./setup.js";
import { Keeper } from "./keeper.js";
import { WorkerPane } from "./worker.js";
import { tick, cadence, holdReason, setSchedule, requestPass, PRESETS, MIN_MINUTES } from "./timer.js";

const ACTIVE = ["needs-decision", "review", "in-progress", "investigating", "queued"];
const ORDER = [...ACTIVE, "parked", "done"];
const OPEN_WAIT = 30000; // an Open request with nothing started by then: say why, offer the button again
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
    this.data = null; // loadSpace() result for this.space
    this.cards = []; // loadDecisions() result
    this.space = null;
    this.view = null; // "decisions" | "tasks" | "rules"; null: decisions when any is open
    this.rules = new RulesPane();
    this.open = new Set(); // expanded task ids
    this.folded = new Set(["done", "parked"]); // task sections folded away (finished work starts folded)
    this.older = false; // answered cards shown
    this.output = {}; // `${task}:${label}` -> { text, code, running }
    this.armedTask = null; // task id armed for delete (second click deletes)
    this.taskError = {}; // task id -> why the last change failed
    this.notes = {}; // card id -> InputState (the optional note)
    this.choice = {}; // card id -> the option picked, not yet submitted
    this.cardScroll = ScrollHandle.new(); // the Decisions list, to bring one card into view
    this.spotlight = null; // card id reached from its task: drawn with an accent border
    this.busy = {}; // card id -> answering now
    this.failed = {}; // card id -> why the answer wasn't written
    this.unsent = []; // [{ space, agent, message }] notices the orchestrator hasn't had yet
    this.opening = {}; // space id -> when this copy asked the keeper to open its Project Agent
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
    cx.spawn(async (cx) => {
      for (;;) {
        try { await watchFolder(`${paHome(null)}/spaces`); } catch { return; }
        this.reload(cx);
      }
    });
  }

  reload(cx) {
    const space = spaces().find((s) => s.active);
    if (!space) { this.data = null; this.space = null; cx.notify(); return; }
    if (this.loading === space.id) { this.again = true; return; }
    this.loading = space.id;
    cx.spawn(async (cx) => {
      let data, cards = [];
      try { data = await loadSpace(space, props()?.profile); if (data.agent) { cards = await loadDecisions(space); await this.rules.load(space); data.evidence = await evidenceTasks(space); data.stale = await staleInstructions(space, data.agent.profile); } }
      catch (e) { data = { agent: null, errors: [String(e.message ?? e)] }; }
      this.loading = null;
      if (spaces().find((s) => s.active)?.id === space.id) {
        this.data = data; this.cards = cards; this.space = space.id;
        // One note field per open card, made here (never in render) and dropped with its card.
        const open = new Set(cards.filter((c) => c.card?.status === "open").map((c) => c.id));
        for (const id of Object.keys(this.notes)) if (!open.has(id)) { this.notes[id].release?.(); delete this.notes[id]; }
        for (const id of open) this.notes[id] ??= InputState.new({ placeholder: "Note for the Project Agent (optional)" });
        // Tell other mods (a Changes view, say) which worktrees this space's tasks use.
        const trees = (data.ledger?.tasks ?? []).filter((x) => x.worktree && x.id).map((x) => ({ task: x.id, path: String(x.worktree), title: String(x.title ?? "") }));
        const allTrees = { ...(shared("project-agent.worktrees") ?? {}) };
        if (JSON.stringify(allTrees[space.id] ?? []) !== JSON.stringify(trees)) { if (trees.length) allTrees[space.id] = trees; else delete allTrees[space.id]; setShared("project-agent.worktrees", allTrees); }
        // Folders worth a shortcut in a Files view (shared "my-files.places": { space: [{ label, path, group }] }):
        // all evidence, each task's evidence (newest task first), each task's worktree.
        const dir = stateDir(space), byNewest = [...(data.ledger?.tasks ?? [])].sort((x, y) => String(y.updated ?? y.created ?? "").localeCompare(String(x.updated ?? x.created ?? "")));
        const spots = data.evidence?.size ? [{ label: "All evidence", path: `${dir}/evidence`, group: "Project Agent" }] : [];
        for (const x of byNewest.slice(0, 15)) {
          const files = data.evidence?.get(x.id);
          if (files) spots.push({ label: `${x.id} evidence · ${files.length} ${files.length === 1 ? "file" : "files"}`, detail: String(x.title ?? ""), path: `${dir}/evidence/${x.id}`, group: "Project Agent", task: x.id });
          if (x.worktree) spots.push({ label: `${x.id} worktree`, detail: String(x.title ?? ""), path: String(x.worktree), group: "Project Agent", task: x.id });
        }
        const allSpots = { ...(shared("my-files.places") ?? {}) };
        if (JSON.stringify(allSpots[space.id] ?? []) !== JSON.stringify(spots)) { if (spots.length) allSpots[space.id] = spots; else delete allSpots[space.id]; setShared("my-files.places", allSpots); }
        // The profile's main-branch names, for views that compare a branch with its base.
        const mains = Array.isArray(data.profile?.mainBranches) ? data.profile.mainBranches : [];
        const allMains = { ...(shared("project-agent.mainBranches") ?? {}) };
        if (JSON.stringify(allMains[space.id] ?? []) !== JSON.stringify(mains)) { if (mains.length) allMains[space.id] = mains; else delete allMains[space.id]; setShared("project-agent.mainBranches", allMains); }
        // And which task each worker belongs to (an Agents view can link a worker back to its task).
        const crew = {};
        for (const x of data.ledger?.tasks ?? []) for (const w of x.workers ?? []) if (w.address) crew[space.machine ? `${space.machine}/${w.address}` : w.address] = { task: x.id, title: String(x.title ?? ""), role: w.role ?? null, state: x.state ?? null };
        const allCrew = { ...(shared("project-agent.workers") ?? {}) };
        if (JSON.stringify(allCrew[space.id] ?? {}) !== JSON.stringify(crew)) { if (Object.keys(crew).length) allCrew[space.id] = crew; else delete allCrew[space.id]; setShared("project-agent.workers", allCrew); }
        const counts = { ...(shared("project-agent.open") ?? {}) };
        if ((counts[space.id] ?? 0) !== open.size) { if (open.size) counts[space.id] = open.size; else delete counts[space.id]; setShared("project-agent.open", counts); }
        cx.notify();
      }
      if (this.again) { this.again = false; this.reload(cx); }
    });
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
    this.opening[space.id] = at;
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
    this.view = "decisions"; this.spotlight = id;
    const at = this.cards.filter((c) => !c.card || c.card.status === "open").findIndex((c) => c.id === id);
    if (at >= 0) this.cardScroll.scroll_to_item(at);
    cx.notify();
  }

  answer(space, entry, label, cx) {
    if (this.busy[entry.id] || !label) return;
    delete this.choice[entry.id];
    this.busy[entry.id] = true; delete this.failed[entry.id];
    const note = (this.notes[entry.id]?.value() ?? "").trim();
    const agent = this.data?.agent;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        await answerDecision(space, entry, label, note);
        // A card for a rule proposal ("rule": "<id>", options Approve / Decline) decides that rule too.
        const decided = entry.card?.rule && (/^approve/i.test(label) ? "active" : /^decline/i.test(label) ? "declined" : null);
        if (decided) await changeRules(space, (rules) => rules.map((r) => (r.id === entry.card.rule && r.status === "proposed" ? touched({ ...r, status: decided }, decided === "active" ? "approved" : "declined") : r)));
        this.unsent.push({ space, agent, message: `decision ${entry.id} answered` });
        this.resend(cx);
      } catch (e) { this.failed[entry.id] = String(e.message ?? e); }
      delete this.busy[entry.id];
      this.reload(cx);
      cx.notify();
    });
  }

  // Deliver what the orchestrator hasn't had yet; an undelivered notice is kept and retried.
  resend(cx) {
    if (!this.unsent.length || this.sending) return;
    this.sending = true;
    cx.spawn(async (cx) => {
      const left = [];
      for (const item of this.unsent.splice(0)) {
        try { await notifyOrchestrator(item.space, item.agent, item.message); }
        catch (e) { left.push({ ...item, why: String(e.message ?? e) }); }
      }
      this.unsent.push(...left);
      this.sending = false;
      cx.notify();
    });
  }

  // A proposal approved or declined in the Rules view: answer its card too, and tell the orchestrator.
  async ruleDecided(space, id, what, cx) {
    const entry = this.cards.find((c) => c.card?.status === "open" && c.card.rule === id);
    const label = entry?.card.options?.find((o) => (what === "declined" ? /^decline/i : /^approve/i).test(o.label))?.label;
    if (label) await answerDecision(space, entry, label, what === "edited and approved" ? "Edited before approving; read rules.json." : "");
    this.unsent.push({ space, agent: this.data?.agent, message: label ? `decision ${entry.id} answered` : `rule ${id} ${what}` });
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
              st.custom = InputState.new({ placeholder: "min", value: PRESETS.includes(every) || !every ? "" : String(every) });
              st.quietFrom = InputState.new({ placeholder: "19:00", value: s.quietHours?.from ?? "" });
              st.quietTo = InputState.new({ placeholder: "08:00", value: s.quietHours?.to ?? "" });
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
            const from = st.quietFrom.value().trim(), to = st.quietTo.value().trim(), ok = (v) => /^([01]\d|2[0-3]):[0-5]\d$/.test(v);
            if (!ok(from) || !ok(to) || from === to) { st.error = "Quiet hours are two different times like 19:00 and 08:00."; cx.notify(); return; }
            this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { quietHours: { from, to } }));
          }))
          .child(pill("quiet-clear", "None", !s.quietHours, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { quietHours: null }))))
          .child(pill("sched-active", "Only while tasks are active", s.onlyWhileActive !== false, (_e, cx) => this.scheduleChange(space, cx, (dir, on) => setSchedule(dir, on, { onlyWhileActive: s.onlyWhileActive === false })))))
        .child(div().px(4).text_size(10.5).text_color(t.dim).child(`Last pass ${s.lastPass ? clock(s.lastPass) : "never"}. A pass never interrupts a turn; a new cadence applies from the next pass.`)));
  }

  // The person's own change to a task: done, park, reopen, delete (to the trash). Runs pa-task on
  // the space's machine; it edits the ledger under a lock and tells the Project Agent.
  changeTask(space, task, action, cx) {
    delete this.taskError[task.id]; this.armedTask = null;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec("pa-task", [action, String(task.id), "--space", localId(space.id)], { on: space.machine ?? null, timeout: 30000 });
        if (r.code !== 0) this.taskError[task.id] = (r.stderr || r.stdout || `pa-task exited with ${r.code}`).trim().slice(0, 300);
      } catch (e) { this.taskError[task.id] = String(e.message ?? e); }
      this.reload(cx); cx.notify();
    });
  }

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
    const open = this.open.has(task.id);
    const tone = { "needs-decision": t.blocked, review: t.done, "in-progress": t.working, investigating: t.working, queued: t.muted, parked: t.dim, done: t.dim }[task.state] ?? t.muted;
    return v_flex().id(`task-${task.id}`).flex_none().rounded(7).border(1).border_color(t.border).bg(t.surface)
      .child(h_flex().id(`task-head-${task.id}`).h(32).px(9).gap(7).items_center().cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { open ? this.open.delete(task.id) : this.open.add(task.id); cx.notify(); })
        .child(div().flex_none().text_size(11).text_color(t.dim).child(open ? "▾" : "▸"))
        .child(div().flex_none().text_size(11.5).text_color(t.dim).child(String(task.id ?? "?")))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(t.foreground).child(String(task.title ?? "(untitled)")))
        .when(Boolean(task.phase), (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child(String(task.phase))))
        .child(this.chip(t, String(task.state ?? "?"), tone)))
      .when(open, (el) => el.child(v_flex().px(10).pb(9).gap(6).text_size(12).text_color(t.muted)
        .when(Boolean(task.goal), (e) => e.child(div().child(String(task.goal))))
        .child(div().text_size(11).text_color(t.dim).child([task.type, task.tier && `${task.tier} tier`].filter(Boolean).join(" · ")))
        // Its workers: a live one opens in the Agents view.
        .when((task.workers ?? []).length > 0, (e) => e.child(h_flex().gap(6).flex_wrap().children((task.workers ?? []).map((w, i) => {
          const address = space.machine ? `${space.machine}/${w.address}` : w.address;
          const term = terminals().find((x) => x.address === address && x.state === "live");
          const label = `${w.role ?? "worker"} ${w.address ?? ""}`.trim();
          return h_flex().id(`worker-${task.id}-${i}`).flex_none().px(8).h(24).gap(6).items_center().rounded(6).border(1).border_color(t.border).text_size(11.5)
            .text_color(term ? t.foreground : t.dim)
            .when(Boolean(term), (b) => b.cursor_pointer().hover((s) => s.bg(t.hover)).on_click(() => this.showAgent(address)))
            .child(div().size(6).rounded(3).bg(term ? t[term.status] ?? t.idle : t.dim))
            .child(term ? `${label} ↗` : `${label} (ended)`)
            // Workers stay until you close them: this opens the delete dialog.
            .child(div().id(`worker-x-${task.id}-${i}`).flex_none().px(3).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.blocked))
              .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
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
        // The person's own controls for the task.
        .child(h_flex().gap(6).flex_wrap().pt(2)
          .when(task.state === "done" || task.state === "parked", (e) => e.child(this.button(t, `reopen-${task.id}`, "Reopen", (_e, cx) => this.changeTask(space, task, "reopen", cx))))
          .when(task.state !== "done", (e) => e.child(this.button(t, `done-${task.id}`, "Mark done", (_e, cx) => this.changeTask(space, task, "done", cx))))
          .when(task.state !== "done" && task.state !== "parked", (e) => e.child(this.button(t, `park-${task.id}`, "Park", (_e, cx) => this.changeTask(space, task, "park", cx))))
          .child(div().id(`delete-${task.id}`).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(this.armedTask === task.id ? t.blocked : t.border)
            .text_size(11.5).text_color(this.armedTask === task.id ? t.blocked : t.muted).cursor_pointer().hover((s) => s.bg(t.hover))
            .on_click((_e, cx) => { if (this.armedTask === task.id) this.changeTask(space, task, "delete", cx); else { this.armedTask = task.id; cx.notify(); } })
            .child(this.armedTask === task.id ? "Delete the task, its evidence and its cards?" : "Delete…")))
        .when(Boolean(this.taskError[task.id]), (e) => e.child(div().text_size(11.5).text_color(t.blocked).child(this.taskError[task.id])))));
  }

  card(t, entry, space) {
    const c = entry.card;
    if (!c) {
      return v_flex().id(`card-${entry.id}`).flex_none().p(10).gap(4).rounded(7).border(1).border_color(t.blocked).bg(t.surface)
        .child(div().text_size(12).text_color(t.foreground).child(`${entry.id}: can't read this card`))
        .child(div().text_size(11.5).text_color(t.blocked).child(entry.error ?? ""))
        .child(div().text_size(11).text_color(t.dim).truncate().child(entry.path.replace(home(), "~")));
    }
    const open = c.status === "open", busy = Boolean(this.busy[entry.id]);
    const task = c.task && (this.data?.ledger?.tasks ?? []).find((x) => x.id === c.task);
    const picked = open ? this.choice[entry.id] ?? null : null;
    return v_flex().id(`card-${entry.id}`).flex_none().p(10).gap(7).rounded(7).border(1).border_color(this.spotlight === entry.id ? t.accent : open ? t.input : t.border).bg(t.surface)
      .child(h_flex().gap(7).items_center()
        .child(div().flex_none().text_size(11.5).text_color(t.dim).child(entry.id))
        .when(Boolean(c.task) && Boolean(this.data?.evidence?.has(c.task)), (el) => el.child(div().id(`card-task-${entry.id}`).flex_none().text_size(11.5).text_color(t.accent).cursor_pointer()
          .on_click(() => this.showEvidence(space, c.task, cardEvidence(c, this.data?.evidence?.get(c.task)))).child(`${cardEvidence(c, this.data?.evidence?.get(c.task)) ?? `${c.task} evidence`} ↗`)))
        .when(Boolean(c.task) && !this.data?.evidence?.has(c.task), (el) => el.child(div().flex_none().text_size(11.5).text_color(t.dim).child(c.task)))
        .child(div().flex_1())
        .child(div().flex_none().text_size(10.5).text_color(t.dim).child(when(open ? c.created : c.answered)))
        .child(this.chip(t, open ? "open" : c.status === "withdrawn" ? "withdrawn" : `answered: ${c.answer}`, open ? t.blocked : t.dim)))
      .child(div().text_size(13).font_semibold().text_color(t.foreground).child(String(c.question)))
      .when(Boolean(c.context), (el) => el.child(div().text_size(12).text_color(t.muted).child(String(c.context))))
      .when(Boolean(task), (el) => el.children(this.actions(t, task, space)))
      .children((c.options ?? []).map((o, i) => {
        const recommended = c.recommendation === o.label, chosen = open ? picked === o.label : c.answer === o.label;
        return h_flex().id(`opt-${entry.id}-${i}`).gap(8).px(8).py(6).items_center().rounded(6).border(1)
          .border_color(chosen ? t.accent : recommended && open ? t.input : t.border).bg(chosen ? t.selected : t.background)
          // Picking an option only selects it; the Submit button below sends it, with the note.
          .when(open && !busy, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => { this.choice[entry.id] = picked === o.label ? null : o.label; cx.notify(); }))
          .when(open, (el) => el.child(div().flex_none().size(12).rounded(6).border(1).border_color(chosen ? t.accent : t.input).bg(chosen ? t.accent : t.background)))
          .child(div().flex_none().min_w(18).text_size(12.5).font_semibold().text_color(t.foreground).child(String(o.label)))
          .child(div().flex_1().min_w(0).text_size(12).text_color(t.muted).child(String(o.detail ?? "")))
          .when(recommended, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child("recommended")));
      }))
      .when(open && Boolean(this.notes[entry.id]), (el) => el.child(Input.new(this.notes[entry.id]).h(30).px(8).rounded(6).border_1().border_color(t.input).bg(t.background).text_size(12).text_color(t.foreground))
        .child(h_flex().gap(8).items_center()
          .child(div().id(`submit-${entry.id}`).flex_none().px(12).h(28).flex().items_center().rounded(7).text_size(12.5).font_semibold()
            .bg(picked && !busy ? t.solid : t.raised).text_color(picked && !busy ? t.solid_foreground : t.dim)
            .when(Boolean(picked) && !busy, (b) => b.cursor_pointer().hover((s) => s.opacity(0.88)).on_click((_e, cx) => this.answer(space, entry, picked, cx)))
            .child(busy ? "Saving…" : picked ? `Submit answer: ${picked}` : "Submit answer"))
          .child(div().flex_1().min_w(0).text_size(10.5).text_color(t.dim).child(picked ? "Add a note above if you want; it is sent with the answer." : "Pick an option, add a note if you want, then submit."))))
      // Closed from the conversation: say so, with the words that decided it (or why it was withdrawn).
      .when(!open && Boolean(c.answeredBy), (el) => el.child(div().text_size(11).text_color(t.dim).child(c.status === "withdrawn" ? "Closed by the Project Agent as no longer needed" : `Answered by the ${c.answeredBy}`)))
      .when(!open && Boolean(c.note), (el) => el.child(div().text_size(12).text_color(t.muted).child(c.status === "withdrawn" ? `Why: ${c.note}` : c.answeredBy ? `You said: “${c.note}”` : `Note: ${c.note}`)))
      .when(Boolean(this.failed[entry.id]), (el) => el.child(div().text_size(11.5).text_color(t.blocked).child(this.failed[entry.id])));
  }

  render(cx) {
    if (this.worker) return this.worker.render();
    if (this.setup) return this.setup.render();
    if (props()?.timer) return div();
    const t = theme();
    const space = spaces().find((s) => s.active);
    if (space?.id !== this.space && space && this.loading !== space.id) this.reload(cx);
    const root = v_flex().id("project-agent").size_full().min_h(0).bg(t.chrome).text_color(t.foreground);
    const head = (title, right) => h_flex().h(34).px(12).gap(8).flex_none().items_center().border_b(1).border_color(t.border)
      .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().child(title)).when(Boolean(right), (el) => el.child(right));
    const note = (text) => div().p(12).text_size(12).text_color(t.dim).child(text);
    if (!space) return root.child(head("Project Agent")).child(note("No space open."));
    const data = space.id === this.space ? this.data : null;
    if (!data) return root.child(head("Project Agent")).child(note("Loading…"));
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
    const target = liveTarget(space.id);
    const live = target ? agents().find((x) => x.address === target.address) ?? { status: null } : null;
    const req = (shared(OPEN_REQUESTS) ?? {})[space.id], now = Date.now();
    if (target) { delete this.opening[space.id]; delete this.openMsg[space.id]; delete this.confirmOpen[space.id]; }
    if (this.opening[space.id] && now - this.opening[space.id] > OPEN_WAIT) {
      this.openMsg[space.id] = `The Project Agent did not open within 30 s: ${req?.why ?? (req ? "the request was not taken (the keeper runs in a layout with the Project Agent panel)" : "its new tab did not appear; see the notifications")}.`;
      delete this.opening[space.id];
    }
    const waiting = !target && (Boolean(this.opening[space.id]) || Boolean(req && now - Number(req.at) < OPEN_WAIT));
    const like = this.confirmOpen[space.id];
    // Newest first: by when a task last changed (else when it was made), then by id.
    const stamp = (x) => String(x.updated ?? x.created ?? "");
    const tasks = [...(data.ledger?.tasks ?? [])].sort((x, y) => stamp(y).localeCompare(stamp(x)) || String(y.id ?? "").localeCompare(String(x.id ?? ""), undefined, { numeric: true }));
    // One section per state, in the order that needs you first.
    const sections = [...ORDER, ...new Set(tasks.map((x) => x.state).filter((s) => !ORDER.includes(s)))]
      .map((state) => ({ state, tasks: tasks.filter((x) => x.state === state) })).filter((s) => s.tasks.length);
    const active = tasks.filter((x) => ACTIVE.includes(x.state)).length;
    const openCards = this.cards.filter((c) => !c.card || c.card.status === "open");
    const answered = this.cards.filter((c) => c.card && c.card.status !== "open");
    // Another mod asked to show a task (shared "project-agent.show": { space, task, at }): once.
    const ask = shared("project-agent.show");
    if (ask && ask.space === space.id && ask.at !== this.shownAt) {
      this.shownAt = ask.at; this.view = "tasks"; this.open.add(ask.task);
      const state = tasks.find((x) => x.id === ask.task)?.state;
      if (state) this.folded.delete(state);
    }
    const view = this.view ?? (openCards.length || !tasks.length ? "decisions" : "tasks");
    const tab = (id, label) => div().id(`view-${id}`).px(9).h(24).flex().items_center().rounded(6).text_size(12).cursor_pointer()
      .bg(view === id ? t.selected : t.chrome).text_color(view === id ? t.foreground : t.muted).hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => { this.view = id; cx.notify(); }).child(label);
    const unsent = this.unsent.filter((u) => u.space.id === space.id);
    return root
      .child(head(`PA: ${a.name ?? space.name}`, h_flex().flex_none().gap(6).items_center()
        .child(div().size(7).rounded(4).bg(live ? t[live.status] ?? t.idle : t.dim))
        .child(div().text_size(11).text_color(t.dim).child(live ? live.status ?? "running" : "Not running"))
        .when(!live, (el) => el.child(this.openButton(t, space, a, "pa-open", waiting)))))
      .when(!live && Boolean(like), (el) => el.child(v_flex().px(12).py(8).gap(6).flex_none().border_b(1).border_color(t.border).bg(t.working + "1a").text_size(11.5).text_color(t.foreground)
        .child(div().child(`A Claude session for this Project Agent seems to run in tab ${like?.tab}. Opening another starts a second one. Open anyway?`))
        .child(h_flex().gap(6)
          .child(this.button(t, "pa-open-anyway", "Open anyway", (_e, cx) => this.requestOpen(space, a, cx, true), true))
          .child(this.button(t, "pa-open-cancel", "Cancel", (_e, cx) => { delete this.confirmOpen[space.id]; cx.notify(); })))))
      .when(!live && Boolean(this.openMsg[space.id]), (el) => el.child(div().px(12).py(6).border_b(1).border_color(t.border).text_size(11.5).text_color(t.blocked).child(this.openMsg[space.id] ?? "")))
      .child(v_flex().px(12).py(8).gap(2).flex_none().border_b(1).border_color(t.border).text_size(11.5).text_color(t.dim)
        .child(div().truncate().child(String(a.project ?? "").replace(home(), "~")))
        .child(div().child(`profile ${a.profile ?? "?"} · ${active} active of ${tasks.length} tasks · pass ${data.ledger?.passCount ?? 0}`)))
      // The agent reads its instructions when its session starts; say when they have changed since.
      .when((data.stale ?? []).length > 0 && Boolean(live), (el) => el.child(div().px(12).py(6).border_b(1).border_color(t.border).bg(t.working + "1a").text_size(11.5).text_color(t.foreground)
        .child(`Its instructions changed since this agent started (${data.stale.slice(0, 3).join(", ")}). Type /exit in its tab: it restarts in place, keeps the conversation and loads them.`)))
      .child(this.scheduleBar(t, space, data))
      .child(h_flex().px(8).py(6).gap(4).flex_none().border_b(1).border_color(t.border)
        .child(tab("decisions", openCards.length ? `Decisions · ${openCards.length}` : "Decisions"))
        .child(tab("tasks", `Tasks · ${tasks.length}`))
        .child(tab("rules", this.rules.data?.error ? "Rules · !" : `Rules · ${(this.rules.data?.rules ?? []).filter((r) => r.status === "active").length}`)))
      .children((data.errors ?? []).map((e) => div().px(12).py(6).text_size(11.5).text_color(t.blocked).child(e)))
      .when(unsent.length > 0, (el) => el.child(div().px(12).py(6).text_size(11.5).text_color(t.blocked)
        .child(`Saved, but the Project Agent hasn't been told yet (${unsent[0].why ?? "sending"}). Trying again every 10 s.`)))
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
      .when(view === "rules", (el) => el.child(this.rules.render(t, space, (id, what) => new Promise((done) => cx.spawn(async (cx) => { try { await this.ruleDecided(space, id, what, cx); } finally { done(); } })))))
      .when(view === "decisions", (el) => el.child(v_flex().id("pa-decisions").flex_1().min_h(0).p(8).gap(8).overflow_y_scrollbar().track_scroll(this.cardScroll)
        .when(!openCards.length, (e) => e.child(note("No open decisions. The Project Agent opens a card here when it needs you.")))
        .children(openCards.map((c) => this.card(t, c, space)))
        .when(answered.length > 0, (e) => e.child(div().id("pa-older").px(4).py(4).text_size(11.5).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.foreground))
          .on_click((_e, cx) => { this.older = !this.older; cx.notify(); }).child(`${this.older ? "▾" : "▸"} ${answered.length} answered`)))
        .when(this.older, (e) => e.children(answered.slice(0, 50).map((c) => this.card(t, c, space))))));
  }
}
