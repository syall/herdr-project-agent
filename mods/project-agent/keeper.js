// The keeper: what makes a Project Agent's tab special, for every space this window shows.
// The agent's launcher labels its own terminal (herdr.pa=<space>); the keeper mirrors those
// labels into shared state "project-agent.tabs" (tab id -> { space, project, profile, name,
// terminal, recovering? }), keeps that tab first, restores it in place when its launcher is
// gone, and opens the setup dialog for a new space. The labels are the truth; the shared state
// is rebuilt from them. A tab bar, menu or pane header reads "project-agent.tabs" to leave
// out its close and pin controls for that tab.
// A space counts only while its agent.json exists and is not marked closed ("configured").
// Runs in one copy of the mod only (the one holding the timer lease); a keeper.lock file in the
// space's state folder keeps two Herdr clients from restoring the same Project Agent at once.

import { spaces, panes, terminals, machines, focused, run, openOverlay, notify, readTextFile, home, shared, setShared } from "herdr";
import { replaceFile } from "./store.js";

const localId = (id) => String(id ?? "").split("/").pop();
const tabs = () => shared("project-agent.tabs") ?? {};
const dirOf = (space) => `${space.machine ? "" : `${home()}/`}.config/herdr-pa/spaces/${localId(space.id)}`;
const beatAddress = (text) => String(text ?? "").trim().split(/\s+/)[1] ?? null; // heartbeat: "<epoch> <address>"
const argvOf = (r) => ["pa-session", "--space", localId(r.space), "--project", r.project, "--profile", r.profile, "--name", r.name];
const NO_STATUS = new Set([undefined, null, "", "none", "unknown"]);

const STALE_READS = 2; // unchanged heartbeat comparisons that mean its launcher is gone
const READ_GAP = 30000; // reads further apart than this (sleep, slow machine) start counting again
const YOUNG = 30000; // a terminal this new is never judged stale
const MISSING = 4000; // a PA terminal or tab gone this long is restored
const TIMEOUT = 30000; // a restore that has not produced a new terminal by then is given up
const RETRY = 60000; // at most one restore a minute per space
const FAIL_WINDOW = 600000, MAX_FAILS = 3; // then stop until the person acts
const LOCK_TTL = 60000; // another client's lock older than this may be taken over
const PEEK = 2000; // heartbeat reads while a restore waits for the new launcher

// A terminal the keeper started for this space's restore: argv runs pa-session for the space
// (or the launcher already labelled it). Without argv (an older app) any new terminal counts.
function isLauncher(term, space) {
  if (!term || term.state !== "live") return false;
  if (term.labels?.["herdr.pa"] === localId(space.id)) return true;
  if (!Array.isArray(term.argv)) return true;
  const line = term.argv.map(String).join(" ");
  if (!/(^|[\s/])pa-session(\s|$)/.test(line)) return false;
  const m = line.match(/--space\s+(\S+)/);
  return !m || m[1] === localId(space.id);
}

export class Keeper {
  constructor(me = `keeper-${Date.now()}`) {
    this.me = me; // this copy's id (the timer lease's), written into keeper.lock
    this.missing = {}; // PA tab id -> when its tab or terminal was first seen gone
    this.beat = {}; // PA tab id -> { value, same, at }: its launcher's heartbeat, how many spaced reads it hasn't changed
    this.hb = {}; // space id -> the heartbeat text last read (any read)
    this.config = {}; // space id -> { state: "absent" | "closed" | "open", at }; no entry: not read yet
    this.reopened = {}; // space id -> when a restore or reopen last started
    this.warned = {}; // space id -> told once that it isn't restoring again yet
    this.fails = {}; // space id -> times of failed restores
    this.stopped = {}; // space id -> gave up after MAX_FAILS; cleared when its PA runs again
    this.noticed = {}; // space id -> told once about a program outside the launcher
    this.pending = {}; // space id -> { tab, since, before }: a reopened tab not yet seen
    this.locks = {}; // space id -> { state: "taking" | "held" | "busy", text, at }
    this.seen = {}; // terminal address -> when the keeper first saw it
    this.retired = new Set(); // old PA terminals replaced by a restore: never adopted again
    this.ready = {}; // machine name -> its layout state last tick
    this.peeked = {}; // space id -> last fast heartbeat read
    this.reading = false;
    this.asked = new Set(); // terminals whose new-space setup dialog was already opened
    // One time: carry over the records an older tab bar kept under its own key.
    if (shared("project-agent.tabs") === undefined || shared("project-agent.tabs") === null) setShared("project-agent.tabs", shared("my-tabs.pa") ?? {});
  }

  // A new space's first tab runs a launcher that waits for a choice (label herdr.pa.ask). Show
  // the setup dialog over it, once, while that tab is the one on screen.
  offerSetup() {
    const at = focused();
    for (const term of terminals()) {
      const state = term.labels?.["herdr.pa.ask"];
      if (term.state !== "live" || (state !== "project" && state !== "home") || this.asked.has(term.address)) continue;
      const pane = panes().find((p) => p.terminal === term.address);
      if (!pane || pane.tab !== at?.tab) continue;
      this.asked.add(term.address);
      const space = spaces().find((s) => s.tabs.some((x) => x.id === pane.tab));
      openOverlay({ mod: "project-agent", kind: "dialog", modal: true, width: 600, height: 440,
        props: { setup: { terminal: term.address, space: space?.id, machine: space?.machine ?? null, home: state === "home",
          folder: term.labels["herdr.pa.ask.folder"] ?? pane.cwd ?? "", claude: term.labels["herdr.pa.ask.claude"], codex: term.labels["herdr.pa.ask.codex"] } } });
    }
  }

  // True while a restore or reopen is under way: main.js then runs syncPA every 250 ms.
  busy() {
    return Object.values(tabs()).some((r) => r.recovering && !r.recovering.waiting) || Object.keys(this.pending).length > 0
      || Object.values(this.locks).some((l) => l.state === "taking");
  }

  machineReady(space) {
    const ms = machines();
    return ms.some((m) => m.name === null && m.layout === "ready") && (!space.machine || ms.some((m) => m.name === space.machine && m.layout === "ready"));
  }

  // The launcher rewrites spaces/<id>/heartbeat every 10 s while it runs. Read every 15 s (and
  // when the lease is taken or a machine comes back): the heartbeat of every recorded tab, and
  // agent.json of every space with a record or a labelled terminal (what "configured" means).
  readBeats(cx) { this.read(cx, true); }

  read(cx, beats) {
    const records = tabs(), sp = spaces(), ps = panes();
    for (const tab of Object.keys(this.beat)) if (!records[tab]) delete this.beat[tab];
    const wanted = new Map();
    for (const r of Object.values(records)) { const s = sp.find((x) => x.id === r.space); if (s) wanted.set(s.id, s); }
    for (const term of terminals()) {
      if (term.state !== "live" || !term.labels?.["herdr.pa"]) continue;
      const pane = ps.find((p) => p.terminal === term.address);
      const s = pane && sp.find((x) => x.tabs.some((t) => t.id === pane.tab));
      if (s) wanted.set(s.id, s);
    }
    const list = [...wanted.values()].filter((s) => this.machineReady(s));
    if (!beats) this.reading = true;
    cx.spawn(async () => {
      try {
        for (const space of list) {
          const on = space.machine ?? null, dir = dirOf(space);
          try {
            const text = await readTextFile(`${dir}/agent.json`, { on });
            if (text === null) this.config[space.id] = { state: "absent", at: Date.now() };
            else {
              try { const v = JSON.parse(text); this.config[space.id] = { state: v?.closed ? "closed" : "open", at: Date.now() }; }
              catch { /* half-written or broken: keep what was known */ }
            }
          } catch { /* unreachable: keep what was known */ }
          if (!beats) continue;
          let value;
          try { value = await readTextFile(`${dir}/heartbeat`, { on }); } catch { continue; }
          this.hb[space.id] = value;
          const now = Date.now();
          for (const [tab, r] of Object.entries(tabs())) {
            if (r.space !== space.id) continue;
            const last = this.beat[tab];
            // Counted in spaced reads: after the computer sleeps, the clock has jumped but nothing was read.
            if (!last || last.value !== value || now - last.at > READ_GAP) this.beat[tab] = { value, same: 0, at: now };
            else { last.same += 1; last.at = now; }
          }
        }
      } finally { if (!beats) this.reading = false; }
    });
  }

  // A heartbeat read only (no staleness counting), while a restore waits for the new launcher.
  peek(cx, space, now) {
    if (!cx || now - (this.peeked[space.id] ?? 0) < PEEK) return;
    this.peeked[space.id] = now;
    cx.spawn(async () => { try { this.hb[space.id] = await readTextFile(`${dirOf(space)}/heartbeat`, { on: space.machine ?? null }); } catch { /* next time */ } });
  }

  // keeper.lock in the space's state folder: true once this keeper holds it (taking it is async).
  lock(cx, space, tab, now) {
    const l = this.locks[space.id];
    const path = `${dirOf(space)}/keeper.lock`, on = space.machine ?? null;
    if (l?.state === "held") {
      if (cx && now - l.at > LOCK_TTL / 2 && !l.refreshing) { // a long wait: keep it from looking abandoned
        l.refreshing = true;
        const text = JSON.stringify({ client: this.me, at: new Date(now).toISOString(), tab });
        cx.spawn(async () => { try { await replaceFile(path, text, l.text, on); l.text = text; l.at = now; } catch { /* taken over */ } l.refreshing = false; });
      }
      return true;
    }
    if (!cx || l?.state === "taking" || (l?.state === "busy" && now - l.at < 5000)) return false;
    this.locks[space.id] = { state: "taking", at: now };
    cx.spawn(async () => {
      try {
        const old = await readTextFile(path, { on });
        let info = null;
        try { info = old ? JSON.parse(old) : null; } catch { /* unreadable: free */ }
        const t = Date.now();
        if (info && !info.released && info.client !== this.me && t - Date.parse(info.at) <= LOCK_TTL) { this.locks[space.id] = { state: "busy", at: t }; return; }
        const text = JSON.stringify({ client: this.me, at: new Date(t).toISOString(), tab });
        await replaceFile(path, text, old, on);
        this.locks[space.id] = { state: "held", text, at: t };
      } catch { this.locks[space.id] = { state: "busy", at: Date.now() }; }
    });
    return false;
  }

  release(cx, space) {
    const l = this.locks[space.id];
    delete this.locks[space.id];
    if (l?.state !== "held" || !cx) return;
    const text = JSON.stringify({ released: true, at: new Date(Date.now()).toISOString() });
    cx.spawn(async () => { try { await replaceFile(`${dirOf(space)}/keeper.lock`, text, l.text, space.machine ?? null); } catch { /* expires */ } });
  }

  fail(space, r, why, now) {
    const list = (this.fails[space.id] ?? []).filter((t) => now - t < FAIL_WINDOW);
    list.push(now);
    this.fails[space.id] = list;
    const reason = why ? `: ${why}` : "";
    if (list.length >= MAX_FAILS) {
      this.stopped[space.id] = true;
      notify({ title: "Project Agent needs you", body: `${r.name}: it could not be restored ${list.length} times in 10 minutes${reason}. Fix the cause, then press Enter in its tab or run pa-start in the space.`, level: "error" });
    } else notify({ title: "Project Agent not restored", body: `${r.name}${reason}. It tries again in a minute.`, level: "warning" });
  }

  // The state of a record's terminal: alive (its launcher beats, or it is new) | elsewhere (the
  // space's launcher beats in another terminal) | shell (a plain shell is left) | foreign (some
  // other program runs there: never touched) | ended.
  classify(tab, r, ts, ps, now) {
    const term = ts.find((x) => x.address === r.terminal);
    if (!term || term.state !== "live") return "ended";
    if (now - (this.seen[term.address] ?? now) < YOUNG) return "alive";
    const b = this.beat[tab];
    if (!b) return "alive"; // not read yet
    if (b.same < STALE_READS) return beatAddress(b.value) === localId(term.address) ? "alive" : "elsewhere";
    const program = term.program ?? {};
    const isShell = program.shell ?? program.key === "shell";
    const title = ps.find((p) => p.terminal === term.address)?.title ?? "";
    return isShell && !program.agent && NO_STATUS.has(term.status) && !/^PA: /.test(title) ? "shell" : "foreign";
  }

  // Runs on a timer, not in render: adopts labelled terminals, keeps each Project Agent tab
  // first, and restores one whose launcher is gone, in its own tab (see README).
  syncPA(cx) {
    const ms = machines();
    if (ms.some((m) => m.name === null && m.layout !== "ready")) return;
    const now = Date.now(), ts = terminals(), ps = panes(), sp = spaces();
    const before = JSON.stringify(tabs());
    const records = JSON.parse(before);
    const spaceOfTab = (tab) => sp.find((s) => s.tabs.some((x) => x.id === tab));
    const cfg = (space) => this.config[space.id]?.state ?? "unknown";
    let wantRead = false, readAll = false;
    for (const m of ms) { // a machine that comes back: read its files now, count its heartbeats afresh
      if (m.layout === "ready" && this.ready[m.name ?? ""] !== undefined && this.ready[m.name ?? ""] !== "ready") readAll = true;
      this.ready[m.name ?? ""] = m.layout;
    }
    const live = new Set(ts.map((t) => t.address));
    for (const t of ts) this.seen[t.address] ??= typeof t.created_ms === "number" ? Math.min(t.created_ms, now) : now;
    for (const a of Object.keys(this.seen)) if (!live.has(a)) { delete this.seen[a]; this.retired.delete(a); }

    for (const term of ts) {
      if (term.state !== "live" || !term.labels?.["herdr.pa"] || term.parent || this.retired.has(term.address)) continue;
      const pane = ps.find((p) => p.terminal === term.address);
      const space = pane && spaceOfTab(pane.tab);
      if (!space || !this.machineReady(space) || records[pane.tab]?.terminal === term.address) continue;
      const c = cfg(space);
      if (c !== "open") { if (c !== "absent") wantRead = true; continue; } // no agent.json: not a Project Agent space
      const mine = Object.entries(records).filter(([, r]) => r.space === space.id);
      if (this.pending[space.id] || mine.some(([, r]) => r.recovering)) continue; // mid-restore: the restore owns the record
      // The space's record still points at a live terminal: move only to the one its launcher beats in.
      const current = mine.some(([, r]) => ts.some((x) => x.address === r.terminal && x.state === "live"));
      if (current && beatAddress(this.hb[space.id]) !== localId(term.address)) continue;
      for (const [tab] of mine) { delete records[tab]; delete this.beat[tab]; }
      const name = term.labels["herdr.pa.name"] || space.name;
      records[pane.tab] = { space: space.id, project: term.labels["herdr.pa.project"] ?? "", profile: term.labels["herdr.pa.profile"] ?? "", name, terminal: term.address };
      delete this.stopped[space.id];
      if (!space.tabs.find((x) => x.id === pane.tab)?.named) run("tab.rename", { id: pane.tab, name: `PA: ${name}` });
      run("terminal.pin", { terminal: term.address, pinned: true });
    }

    // Reopened tabs (their old tab vanished): record the new tab as soon as it shows, then drop the old record.
    for (const [sid, pend] of Object.entries(this.pending)) {
      const space = sp.find((s) => s.id === sid), r = records[pend.tab];
      if (!space || !r) { delete this.pending[sid]; if (space) this.release(cx, space); continue; }
      const fresh = space.tabs.filter((x) => !pend.before.includes(x.id));
      const found = fresh.map((x) => ps.find((p) => p.tab === x.id && isLauncher(ts.find((t) => t.address === p.terminal), space))).find(Boolean)
        ?? fresh.map((x) => (x.name === `PA: ${r.name}` ? ps.find((p) => p.tab === x.id && p.terminal) : null)).find(Boolean);
      if (found) {
        records[found.tab] = { space: r.space, project: r.project, profile: r.profile, name: r.name, terminal: found.terminal };
        delete records[pend.tab]; delete this.missing[pend.tab]; delete this.beat[pend.tab]; delete this.pending[sid];
        this.release(cx, space);
      } else if (now - pend.since > TIMEOUT) {
        delete records[pend.tab]; delete this.missing[pend.tab]; delete this.beat[pend.tab]; delete this.pending[sid];
        this.release(cx, space);
        this.fail(space, r, "its new tab did not appear", now);
      }
    }

    for (const [tab, r] of Object.entries(records)) {
      const space = sp.find((s) => s.id === r.space);
      if (!space) { delete records[tab]; delete this.missing[tab]; delete this.beat[tab]; continue; } // its space closed: the one way a Project Agent ends
      if (!this.machineReady(space)) { delete this.beat[tab]; delete this.missing[tab]; continue; } // offline: keep the record, count afresh later
      const c = cfg(space);
      if (c === "unknown") { wantRead = true; continue; }
      if (c === "absent") { delete records[tab]; delete this.missing[tab]; delete this.beat[tab]; continue; }
      const here = space.tabs.find((x) => x.id === tab);
      if (c === "closed") {
        // Closed on purpose. A launcher running here anyway clears the mark on its next start: read again.
        if (!r.recovering && here && this.classify(tab, r, ts, ps, now) === "alive") { wantRead = true; continue; }
        if (r.recovering) this.release(cx, space);
        delete records[tab]; delete this.missing[tab]; delete this.beat[tab];
        continue;
      }
      if (r.recovering) { this.advance(cx, tab, r, space, here, now, ts, ps); continue; }
      if (!here) { this.reopen(cx, tab, r, space, now); continue; }
      if (here.index !== 1) run("tab.move", { id: tab, index: 1 });
      const state = this.classify(tab, r, ts, ps, now);
      if (state === "alive" || state === "elsewhere") { delete this.missing[tab]; if (state === "alive") delete this.stopped[space.id]; continue; }
      if (state === "foreign") {
        delete this.missing[tab];
        if (!this.noticed[space.id]) notify({ title: "Project Agent tab runs something else", body: `${r.name}: the program in its tab is not running under its launcher, so it is left alone. Exit it to have the Project Agent restored.`, level: "warning" });
        this.noticed[space.id] = true;
        continue;
      }
      if (state === "ended") { this.missing[tab] ??= now; if (now - this.missing[tab] < MISSING) continue; }
      this.restore(cx, tab, r, space, now, ts, ps);
    }

    if ((wantRead || readAll) && cx && !this.reading) this.read(cx, readAll);
    const after = JSON.stringify(records);
    if (after !== before) setShared("project-agent.tabs", records);
  }

  // May a restore or reopen start now? (Not stopped, at most one a minute, and the lock held.)
  mayStart(cx, tab, r, space, now) {
    if (this.stopped[space.id]) return false;
    if (now - (this.reopened[space.id] ?? 0) < RETRY) {
      if (!this.warned[space.id]) notify({ title: "Project Agent stopped again", body: `${r.name}: it is restored in a minute.`, level: "warning" });
      this.warned[space.id] = true;
      return false;
    }
    return this.lock(cx, space, tab, now);
  }

  // In-place restore: a new launcher in a pane beside the old one, in the same tab, so the
  // record (keyed by the tab) protects the tab throughout. See advance() for the rest.
  restore(cx, tab, r, space, now, ts, ps) {
    const inTab = ps.filter((p) => p.tab === tab);
    const old = inTab.find((p) => p.terminal === r.terminal);
    const target = old ?? inTab.find((p) => p.terminal && !ts.find((t) => t.address === p.terminal)?.parent) ?? inTab[0];
    if (!target || !this.mayStart(cx, tab, r, space, now)) return;
    this.reopened[space.id] = now;
    delete this.warned[space.id]; delete this.missing[tab];
    r.recovering = { since: now, from: r.terminal, pane: old?.pane ?? null, attempt: (this.fails[space.id]?.length ?? 0) + 1, panes: inTab.map((p) => p.pane) };
    run("pane.split", { pane: target.pane, direction: "right", argv: argvOf(r) });
  }

  advance(cx, tab, r, space, here, now, ts, ps) {
    const rec = r.recovering;
    const giveUp = (why) => {
      r.terminal = rec.from; delete r.recovering;
      this.release(cx, space);
      this.fail(space, r, why, now);
    };
    if (!here) { r.terminal = rec.from; delete r.recovering; this.release(cx, space); return; } // the tab went: reopened next tick
    this.lock(cx, space, tab, now); // keeps a long wait's lock fresh
    if (!rec.to) { // the new pane: the record points at it from the first tick it shows
      const fresh = ps.find((p) => p.tab === tab && p.terminal && !rec.panes.includes(p.pane) && isLauncher(ts.find((t) => t.address === p.terminal), space));
      if (fresh) { rec.to = fresh.terminal; r.terminal = fresh.terminal; }
    }
    if (!rec.to) { if (now - rec.since > TIMEOUT) giveUp("no new terminal started"); return; }
    const term = ts.find((t) => t.address === rec.to);
    if (!term || term.state !== "live") { giveUp("its new terminal ended"); return; }
    const labelled = term.labels?.["herdr.pa"] === localId(space.id);
    if (labelled && beatAddress(this.hb[space.id]) === localId(rec.to)) {
      // Running and beating: close the old pane, but only if it still holds the old PA terminal.
      const old = rec.pane && ps.find((p) => p.pane === rec.pane);
      if (old && old.terminal === rec.from && !ts.find((t) => t.address === rec.from)?.parent) run("pane.close", { pane: rec.pane });
      this.retired.add(rec.from);
      delete r.recovering; delete this.beat[tab]; delete this.stopped[space.id];
      this.release(cx, space);
      run("terminal.pin", { terminal: rec.to, pinned: true });
      notify({ title: "Project Agent restored", body: `${r.name}: restarted in its tab; its session resumes.`, level: "success" });
      return;
    }
    if (labelled) this.peek(cx, space, now);
    if (now - rec.since > TIMEOUT && !rec.waiting) {
      // It started but is not running yet: usually waiting for Enter after an error it shows.
      rec.waiting = true;
      this.fail(space, r, term.labels?.["herdr.pa.error"] || "it has not started yet", now);
    }
  }

  // The tab itself is gone while its space lives on: open a new one (until Change 2's Close /
  // Open controls exist). The old record stays until the new tab is recorded (pending).
  reopen(cx, tab, r, space, now) {
    if (this.pending[space.id]) return;
    this.missing[tab] ??= now;
    if (now - this.missing[tab] < MISSING || !this.mayStart(cx, tab, r, space, now)) return;
    this.reopened[space.id] = now;
    delete this.warned[space.id];
    this.pending[space.id] = { tab, since: now, before: space.tabs.map((x) => x.id) };
    run("tab.new", { space: r.space, folder: r.project, name: `PA: ${r.name}`, argv: argvOf(r) });
    notify({ title: "Project Agent reopened", body: `${r.name}: its session resumes; the ledger carries the state.` });
  }
}
