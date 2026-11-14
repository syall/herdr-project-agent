// The keeper: what makes a Project Agent's tab special, for every space this window shows.
// The agent's launcher labels its own terminal (herdr.pa=<space>); the keeper mirrors those
// labels into shared state "project-agent.tabs" (tab id -> { space, project, profile, name,
// terminal }), keeps that tab first, reopens it when its terminal ends while its space lives
// on, and opens the setup dialog for a new space. The labels are the truth; the shared state
// is rebuilt from them. A tab bar, menu or pane header reads "project-agent.tabs" to leave
// out its close and pin controls for that tab.
// Runs in one copy of the mod only (the one holding the timer lease).

import { spaces, panes, terminals, machines, focused, run, openOverlay, notify, readTextFile, home, shared, setShared } from "herdr";

const localId = (id) => String(id ?? "").split("/").pop();
const tabs = () => shared("project-agent.tabs") ?? {};

export class Keeper {
  constructor() {
    this.missing = {}; // PA tab id -> when its tab or terminal was first seen gone
    this.beat = {}; // PA tab id -> { value, same }: its launcher's heartbeat file, and how many reads it hasn't changed
    this.reopened = {}; // space id -> when its PA tab was last reopened
    this.warned = {}; // space id -> told once that it isn't reopening again yet
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

  // The launcher rewrites spaces/<id>/heartbeat every 10 s while it runs. A terminal that is
  // live but whose heartbeat stopped has only a shell left (the launcher was killed, or the
  // core replaced a lost terminal): syncPA then replaces the tab.
  readBeats(cx) {
    const records = tabs(), sp = spaces();
    for (const tab of Object.keys(this.beat)) if (!records[tab]) delete this.beat[tab];
    cx.spawn(async () => {
      for (const [tab, r] of Object.entries(records)) {
        const machine = sp.find((s) => s.id === r.space)?.machine ?? null;
        let value = null;
        try { value = await readTextFile(`${machine ? "" : `${home()}/`}.config/herdr-pa/spaces/${localId(r.space)}/heartbeat`, { on: machine }); } catch { continue; }
        const last = this.beat[tab];
        // Counted in reads, not seconds: after the computer sleeps, the clock has jumped but nothing was read.
        if (!last || last.value !== value) this.beat[tab] = { value, same: 0 }; else last.same += 1;
      }
    });
  }

  // Runs on a timer, not in render: it adopts labelled terminals, keeps each Project
  // Agent tab first, and reopens one whose terminal ended while its space lives on.
  syncPA() {
    if (machines().some((m) => m.name === null && m.layout !== "ready")) return;
    const now = Date.now(), ts = terminals(), ps = panes(), sp = spaces();
    const before = JSON.stringify(tabs());
    const records = JSON.parse(before);
    const spaceOfTab = (tab) => sp.find((s) => s.tabs.some((x) => x.id === tab));
    for (const term of ts) {
      if (term.state !== "live" || !term.labels?.["herdr.pa"]) continue;
      const pane = ps.find((p) => p.terminal === term.address);
      const space = pane && spaceOfTab(pane.tab);
      if (!space || records[pane.tab]?.terminal === term.address) continue;
      for (const [tab, r] of Object.entries(records)) if (r.space === space.id) delete records[tab];
      const name = term.labels["herdr.pa.name"] || space.name;
      records[pane.tab] = { space: space.id, project: term.labels["herdr.pa.project"] ?? "", profile: term.labels["herdr.pa.profile"] ?? "", name, terminal: term.address };
      if (!space.tabs.find((x) => x.id === pane.tab)?.named) run("tab.rename", { id: pane.tab, name: `PA: ${name}` });
      run("terminal.pin", { terminal: term.address, pinned: true });
    }
    for (const [tab, r] of Object.entries(records)) {
      const space = sp.find((s) => s.id === r.space);
      if (!space) { delete records[tab]; continue; } // its space closed: the one way a Project Agent ends
      if (space.machine && machines().some((m) => m.name === space.machine && m.layout !== "ready")) continue;
      const here = space.tabs.find((x) => x.id === tab);
      const term = ts.find((x) => x.address === r.terminal && x.state === "live");
      // Its launcher restarts the agent in place; a stopped heartbeat means the launcher itself is gone.
      const stranded = Boolean(here && term && (this.beat[tab]?.same ?? 0) >= 4);
      if (here && term && !stranded) {
        delete this.missing[tab];
        if (here.index !== 1) run("tab.move", { id: tab, index: 1 });
        continue;
      }
      this.missing[tab] ??= now;
      if (!stranded && now - this.missing[tab] < 4000) continue;
      if (now - (this.reopened[r.space] ?? 0) < 60000) { // at most one reopen a minute
        if (!this.warned[r.space]) notify({ title: "Project Agent stopped again", body: `${r.name}: it reopens in a minute.`, level: "warning" });
        this.warned[r.space] = true;
        continue;
      }
      delete records[tab]; delete this.missing[tab]; delete this.beat[tab]; delete this.warned[r.space];
      this.reopened[r.space] = now;
      run("tab.new", { space: r.space, folder: r.project, name: `PA: ${r.name}`, argv: ["pa-session", "--space", localId(r.space), "--project", r.project, "--profile", r.profile, "--name", r.name] });
      if (here && stranded) run("tab.close", { id: tab, confirmed: true });
      notify({ title: "Project Agent reopened", body: `${r.name}: its session resumes; the ledger carries the state.` });
    }
    const after = JSON.stringify(records);
    if (after !== before) setShared("project-agent.tabs", records);
  }
}
