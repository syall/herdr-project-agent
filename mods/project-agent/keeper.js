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
const SHELL = /(^|\/)-?(sh|bash|zsh|fish|dash|ksh|tcsh|csh)$/; // a shell's program or process name
const present = (v) => v !== undefined && v !== null && v !== "" && !(typeof v === "object" && Object.keys(v).length === 0);

// Positive evidence that a terminal is an idle shell at its prompt, the only thing the keeper
// replaces (and closes): no agent detected or reporting (any status at all, an app, a report: a
// live idle agent reports "done" with its app; terminal-shapes.md), the program table names a
// shell, and the foreground process, when the app gives one, is a shell too. Anything else
// (an editor, a build, ssh, an unknown program, any agent) is left alone.
function idleShell(term, pane) {
  const program = term.program ?? {};
  if (program.agent || present(term.status) || present(term.report) || present(term.app) || present(pane?.app) || present(pane?.status)) return false;
  const named = program.shell === true || program.key === "shell" || SHELL.test(String(program.key ?? "")) || SHELL.test(String(program.name ?? ""));
  if (!named) return false;
  const fg = term.foreground;
  if (!fg || typeof fg !== "object") return true; // not given: the program table decides (a known limit, MC6)
  if (!SHELL.test(String(fg.name ?? ""))) return false;
  // A bare interactive shell only: flags like -l / -i, no script and no -c command (bash ./deploy.sh is busy).
  const rest = Array.isArray(fg.argv) ? fg.argv.slice(1).map(String) : [];
  return rest.every((a) => /^-/.test(a) && !/^-[A-Za-z]*c/.test(a));
}

const STALE_READS = 2; // unchanged heartbeat comparisons that mean its launcher is gone
const MIN_GAP = 12000; // reads closer than this to the last counted one are not a comparison
const READ_GAP = 30000; // reads further apart than this (sleep, slow machine) start counting again
const YOUNG = 30000; // a terminal this new is never judged stale
const MISSING = 4000; // a PA terminal or tab gone this long is restored
const TIMEOUT = 30000; // a restore with no new terminal by then is given up
const GIVE_UP = 120000; // a new terminal that never shows a launcher (no label, no "starting") by then is given up
const SLOW = 180000; // a launcher that said it is starting but has not labelled itself: tell the person once
const RETRY = 60000; // at most one restore a minute per space
const FAIL_WINDOW = 600000, MAX_FAILS = 3; // then stop until the person acts
const LOCK_TTL = 60000; // another client's lock older than this may be taken over
const PEEK = 2000; // heartbeat reads while a restore waits for the new launcher

// A terminal the keeper started for this space's restore: argv runs pa-session for the space
// (or the launcher already labelled it). Without argv any new terminal in the tab counts: a
// compatibility fallback only (herdr.d.ts documents argv on terminals()).
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
  // Only for spaces whose machine is ready, and only for a bounded time each: a restore past
  // its deadline, an offline machine or a lock read that never ends falls back to the 1.5 s tick.
  busy() {
    const now = Date.now(), sp = spaces();
    const ready = (sid) => { const s = sp.find((x) => x.id === sid); return Boolean(s && this.machineReady(s)); };
    return Object.values(tabs()).some((r) => r.recovering && !r.recovering.waiting && ready(r.space) && now - r.recovering.since < GIVE_UP + 5000)
      || Object.entries(this.pending).some(([sid, p]) => ready(sid) && now - p.since < TIMEOUT + 5000)
      || Object.entries(this.locks).some(([sid, l]) => l.state === "taking" && ready(sid) && now - l.at < 10000);
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
            else if (now - last.at >= MIN_GAP) { last.same += 1; last.at = now; } // closer reads (overlaps) are not counted
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
      if (cx && now - l.at > LOCK_TTL / 2 && !l.refreshing && now - (l.tried ?? 0) >= 5000) { // a long wait: keep it from looking abandoned
        l.refreshing = true; l.tried = now;
        const text = JSON.stringify({ client: this.me, at: new Date(now).toISOString(), tab });
        cx.spawn(async () => { try { await replaceFile(path, text, l.text, on); l.text = text; l.at = now; } catch { /* taken over */ } l.refreshing = false; });
      }
      return true;
    }
    if (!cx || (l?.state === "taking" && now - l.at < 20000) || (l?.state === "busy" && now - l.at < 5000)) return false;
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

  // A restore that was given up. The next one comes a minute after the last start (RETRY),
  // so the notice can say so; the third in 10 minutes stops them.
  fail(space, r, why, now) {
    const list = (this.fails[space.id] ?? []).filter((t) => now - t < FAIL_WINDOW);
    list.push(now);
    this.fails[space.id] = list;
    if (list.length >= MAX_FAILS) {
      this.stopped[space.id] = true;
      notify({ title: "Project Agent needs you", body: `${r.name}: it could not be restored ${list.length} times in 10 minutes (last: ${why}). Fix the cause, then run pa-start in the space.`, level: "error" });
    } else notify({ title: "Project Agent not restored", body: `${r.name}: ${why}. It tries again in a minute.`, level: "warning" });
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
    // The launcher is gone. Only an idle shell is restored; when unsure, leave it and tell (foreign).
    return idleShell(term, ps.find((p) => p.terminal === term.address)) ? "shell" : "foreign";
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
      if (!this.machineReady(space)) { pend.since = now; continue; } // offline: wait, and count the 30 s from its return
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
      if (state !== "foreign") delete this.noticed[space.id]; // the next "something else" is a new episode
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
    const giveUp = (why, closeOwn) => {
      // closeOwn: the new pane is a shell the keeper itself made; nobody else's terminal.
      const own = closeOwn && ps.find((p) => p.tab === tab && p.terminal === rec.to && !rec.panes.includes(p.pane));
      if (own && !ts.find((t) => t.address === rec.to)?.parent) run("pane.close", { pane: own.pane });
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
    if (!rec.to) { if (now - rec.since > TIMEOUT) giveUp("no new terminal started in its tab"); return; }
    const term = ts.find((t) => t.address === rec.to);
    if (!term || term.state !== "live") { giveUp("its new terminal ended before the Project Agent started (on a remote machine, run pa-sync --on <machine> once)"); return; }
    const labelled = term.labels?.["herdr.pa"] === localId(space.id);
    const error = term.labels?.["herdr.pa.error"];
    if (error) { // the launcher shows why it cannot start and waits for Enter: wait with it, tell once per reason
      if (rec.waiting !== error) notify({ title: "Project Agent waiting for you", body: `${r.name}: ${error}. Fix it, then press Enter in its tab.`, level: "warning" });
      rec.waiting = error;
      return;
    }
    if (rec.waiting) { rec.waiting = false; rec.since = now; } // Enter pressed: judge it afresh
    if (labelled && beatAddress(this.hb[space.id]) === localId(rec.to)) {
      // Running and beating. Close the old pane only if it still holds the old PA terminal and that
      // is still an idle shell (or has ended): the person may have started something in it meanwhile.
      const old = rec.pane && ps.find((p) => p.pane === rec.pane);
      const oldTerm = ts.find((t) => t.address === rec.from);
      const closable = old && old.terminal === rec.from && !oldTerm?.parent && (!oldTerm || oldTerm.state !== "live" || idleShell(oldTerm, old));
      if (closable) run("pane.close", { pane: rec.pane });
      this.retired.add(rec.from);
      delete r.recovering; delete this.beat[tab]; delete this.stopped[space.id];
      this.release(cx, space);
      run("terminal.pin", { terminal: rec.to, pinned: true });
      notify({ title: "Project Agent restored", body: `${r.name}: restarted in its tab; its session resumes.${old && !closable ? " Its old pane was left open because something runs in it." : ""}`, level: "success" });
      return;
    }
    if (labelled) this.peek(cx, space, now);
    if (labelled || term.labels?.["herdr.pa.starting"]) {
      // The launcher has begun (labelled, or said it is starting). If its terminal has been an idle
      // shell for 30 s since, it died (killed, or an old label left behind): give up like a plain shell.
      if (idleShell(term, ps.find((p) => p.terminal === rec.to))) {
        rec.idleSince ??= now;
        if (now - rec.idleSince > TIMEOUT) {
          giveUp("its launcher stopped before the Project Agent ran (if this keeps happening, look at its tab or run pa-start in the space)", true);
          return;
        }
      } else delete rec.idleSince;
      // Otherwise wait, never close; tell once if slow.
      if (now - rec.since > SLOW && !rec.slow) {
        rec.slow = true;
        notify({ title: "Project Agent still starting", body: `${r.name}: its launcher has not finished starting after 3 minutes. Look at its tab.`, level: "warning" });
      }
      return;
    }
    // No sign of a launcher yet: a slow login shell, an older pa-session (pa-sync not run) that
    // refused, or a person who stopped it. Wait (bounded), tell once, then give up; close the
    // pane only if it is the keeper's own and an idle shell.
    const age = now - rec.since;
    if (age > TIMEOUT && !rec.told) {
      rec.told = true;
      notify({ title: "Project Agent restore waiting", body: `${r.name}: its new pane shows no launcher yet (on a remote machine, run pa-sync --on <machine> once).`, level: "warning" });
    }
    if (age <= GIVE_UP) return;
    const pane = ps.find((p) => p.terminal === rec.to);
    if (idleShell(term, pane)) giveUp("its new terminal is a plain shell: the launcher there stopped or refused to start (on a remote machine, run pa-sync --on <machine> once)", true);
    else giveUp("its new terminal did not start the launcher within 2 minutes; it was left open because something runs in it", false);
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
