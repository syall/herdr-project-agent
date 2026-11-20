// Panel harness (t-003): renders mods/project-agent/main.js against stubbed Herdr data and
// clicks its controls. Usage: node tests/panel/run.mjs [--list] [--shard=<k>/<n>] [case ...]
// Prints one line per case; exits non-zero when any case fails (2: a bad argument). No case names: every case.
// --shard=<k>/<n>: every n-th case from the k-th (tests/run-all.sh runs four shards at once); --list: the names only.
import fs from "fs";
import os from "os";
import path from "path";

const here = path.dirname(new URL(import.meta.url).pathname);
const src = path.join(here, "..", "..", "mods", "project-agent");
const dst = fs.mkdtempSync(path.join(os.tmpdir(), "t003-panel-"));
fs.copyFileSync(path.join(here, "..", "keeper", "stubs.mjs"), path.join(dst, "keeper-stubs.mjs"));
fs.copyFileSync(path.join(here, "stubs.mjs"), path.join(dst, "stubs.mjs"));
for (const f of fs.readdirSync(src).filter((n) => n.endsWith(".js"))) {
  fs.writeFileSync(path.join(dst, f), fs.readFileSync(path.join(src, f), "utf8").replace(/from "(herdr|gpui-kit|gpui-base)"/g, 'from "./stubs.mjs"'));
}
const stubs = await import(path.join(dst, "stubs.mjs"));
const { default: ProjectAgent } = await import(path.join(dst, "main.js"));
const { cx: baseCx, flush, inspect } = stubs;
// The person's timezone (t-036): zone.js's offsetAt/name, UTC in every world unless a case sets its own (never the machine's).
const zoneMod = await import(path.join(dst, "zone.js")).catch(() => null);
const setZone = (offsetAt, name) => { if (zoneMod) { zoneMod.zone.offsetAt = offsetAt; zoneMod.zone.name = () => name; } };
let cx = { ...baseCx, stop_propagation() {} }; // a new one per world (world())
const g = globalThis;
g.__rejected = []; // what tasks threw and nobody caught (the runner fails the case)
process.on("unhandledRejection", (e) => { g.__rejected.push(String(e?.message ?? e)); });

const T0 = 1_800_000_000_000; // ms
const S = "sp1";
const DIR = `/home/u/.config/herdr-pa/spaces/${S}`;
const shell = { key: "zsh", name: "zsh", shell: true, agent: false };
const claude = { key: "claude", name: "Claude Code", shell: false, agent: true };

class Failed extends Error {}
const check = (ok, what) => { if (!ok) throw new Failed(what); };

// One local space sp1 with a plain tab tb9 (shell t9). agent: write agent.json (its "terminal"
// field names `agentTerminal`); records: project-agent.tabs; extra: more panes/terminals.
function world({ agent = true, agentTerminal = "t30", records = {}, extraPanes = [], extraTerms = [], extraTabs = [], agents = [], machine = null } = {}) {
  g.__now = T0; g.__gen = (g.__gen ?? 0) + 1; g.__delay = {};
  setZone(() => 0, "UTC");
  cx = stubs.makeCx({ stop_propagation() {} }); g.__draining = false;
  g.__runs = []; g.__notes = []; g.__reads = []; g.__writes = []; g.__overlays = []; g.__offline = []; g.__hcs = []; g.__hcFail = []; g.__hcHang = []; g.__ls = {};
  g.__machines = [{ name: null, layout: "ready" }, ...(machine ? [{ name: machine, layout: "ready" }] : [])];
  g.__spaces = [{ id: machine ? `${machine}/${S}` : S, name: "proj", machine, folder: "/proj", active: true, tabs: [{ id: "tb9", index: 1, name: "2", named: false }, ...extraTabs] }];
  g.__panes = [{ pane: "p9", terminal: "t9", tab: "tb9", title: "zsh", cwd: "/proj", status: null, program: shell }, ...extraPanes];
  g.__terminals = [{ address: "t9", state: "live", argv: ["zsh"], labels: {}, status: null, program: shell }, ...extraTerms];
  g.__agents = agents;
  g.__props = {};
  // A keeper ("K", in another copy) holds the lease and has judged the records.
  g.__shared = { "project-agent.tabs": records, "project-agent.timer": { id: "K", at: T0 }, "project-agent.keeper": { id: "K", at: T0 } };
  g.__files = {}; g.__fail = []; g.__hang = [];
  g.__remoteDelay = 0; g.__execHang = []; g.__batchFail = {}; g.__issued = []; g.__timers = []; g.__layout = {}; g.__execs = []; g.__execOut = {}; g.__sleepHang = false; g.__execFail = []; g.__execFn = null;
  g.__themeNames = false; g.__focus = null; // t-142
  g.__pool = null; g.__watchers = null; g.__hangOn = []; g.__delayOn = {}; g.__als = null; g.__cardReads = { live: 0, peak: 0 }; // t-053: off unless a case sets them
  if (agent) {
    const dir = machine ? `${machine}:.config/herdr-pa/spaces/${S}` : `:${DIR}`, home = machine ? `${machine}:.config/herdr-pa` : ":/home/u/.config/herdr-pa";
    g.__files[`${dir}/agent.json`] = JSON.stringify({ space: S, project: "/proj", profile: "test", name: "proj", terminal: agentTerminal });
    g.__files[`${dir}/ledger.json`] = JSON.stringify({ tasks: [] });
    g.__files[`${home}/profiles/test/profile.json`] = JSON.stringify({ name: "test" });
  }
}

async function mount() {
  const v = new ProjectAgent();
  v.init({}, cx); await flush();
  return v;
}
const look = (v) => inspect(v.render(cx));
// Time passes while the keeper (in another copy) keeps running: its lease and its mark stay fresh.
const advance = (ms) => { g.__now += ms; for (const k of ["project-agent.timer", "project-agent.keeper"]) if (g.__shared[k]?.id === "K") g.__shared[k] = { id: "K", at: g.__now }; };
// t-142: a task's moves and Start next are drawn while it is hovered or focused; a click on one hovers its task first.
async function hoverTask(v, id, on = true) {
  let el = null;
  const walk = (x) => { if (el || !x || typeof x !== "object") return; if (x.key === `task-${id}`) { el = x; return; } for (const k of x.kids ?? []) walk(k); };
  walk(v.render(cx));
  el?.style?.on_hover?.[0]?.(on, cx); await flush();
  return look(v);
}
async function click(v, id) {
  const task = /^(?:tmove-[a-z]+-|next-)(.+)$/.exec(id)?.[1];
  if (task && !look(v).clicks[id]) await hoverTask(v, task);
  // t-145: the schedule's controls live in the Passes tab: open it first.
  if (/^(sched-|every-|quiet-|pass-now|pass-retro)/.test(id) && !look(v).clicks[id] && look(v).clicks["view-schedule"]) { look(v).clicks["view-schedule"]({}, cx); await flush(); }
  const { clicks } = look(v);
  check(Boolean(clicks[id]), `a click target ${id} (have ${Object.keys(clicks).join(", ")})`);
  clicks[id]({}, cx); await flush();
}

const cases = {};

cases["P-baseline"] = async () => { // a configured space renders its header and views
  world();
  const v = await mount();
  const { text, clicks } = look(v);
  check(/PA: proj/.test(text), `header names the agent (got ${text.slice(0, 200)})`);
  check(Boolean(clicks["view-decisions"]) && Boolean(clicks["view-tasks"]), "view switches present");
};

cases["P-noagent"] = async () => { // no agent.json: today's branch, the start button, no Open
  world({ agent: false });
  const v = await mount();
  const { text, clicks } = look(v);
  check(/has no Project Agent yet/.test(text), `says there is none (got ${text})`);
  check(Boolean(clicks["pa-start"]) && !clicks["pa-open"], `pa-start offered, no pa-open (got ${Object.keys(clicks)})`);
  await click(v, "pa-start");
  const made = g.__runs.find(([n]) => n === "tab.new");
  check(made && made[1].argv[0] === "pa-session", `pa-start runs pa-session in a new tab (got ${JSON.stringify(g.__runs)})`);
};

// ---- R6: Open Project Agent ----
const REQ = "project-agent.openAgent";
const alive = (terminal = "t4w") => ({ tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal, alive: true } });
const paTab = { id: "tb1", index: 1, name: "PA: proj", named: true };
const paPane = (terminal = "t4w") => ({ pane: "p1", terminal, tab: "tb1", title: "PA: proj", cwd: "/proj", status: "working", program: claude });
const paTerm = (address = "t4w") => ({ address, state: "live", argv: ["pa-session"], labels: { "herdr.pa": S }, status: "working", program: claude });

cases["P-open-shown"] = async () => { // agent.json, no records, no labelled terminal
  world();
  const v = await mount();
  const { text, clicks } = look(v);
  check(/Not running/.test(text), `header says Not running (got ${text.slice(0, 300)})`);
  check(Boolean(clicks["pa-open"]) && /Open Project Agent/.test(text), `an Open Project Agent button (clicks ${Object.keys(clicks)})`);
};

cases["P-open-hidden"] = async () => { // an alive record: no button, the agent's status
  world({ records: alive(), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "working" }] });
  const v = await mount();
  const { text, clicks } = look(v);
  check(!clicks["pa-open"] && !/Not running/.test(text), `no Open button while it runs (clicks ${Object.keys(clicks)})`);
  check(/working/.test(text), `shows its status (got ${text.slice(0, 200)})`);
};

cases["P-open-norecord-stale-terminal"] = async () => { // agent.json's terminal names some other live agent; no record
  world({ agentTerminal: "t5", extraPanes: [{ pane: "p5", terminal: "t5", tab: "tb9", title: "worker", cwd: "/wt", status: "idle", program: claude }],
    extraTerms: [{ address: "t5", state: "live", argv: ["claude"], parent: "t1", labels: {}, status: "idle", program: claude }], agents: [{ address: "t5", status: "idle" }] });
  const v = await mount();
  const { text, clicks } = look(v);
  check(Boolean(clicks["pa-open"]) && /Not running/.test(text), `Open offered: agent.json's terminal is not the PA's (got ${text.slice(0, 200)})`);
};

cases["P-open-click"] = async () => { // the click posts a request; "Opening…" and disabled until the tab is recorded
  world();
  const v = await mount();
  await click(v, "pa-open");
  const req = (g.__shared[REQ] ?? {})[S];
  check(req && req.at === T0 && req.by === "panel", `request written (got ${JSON.stringify(g.__shared[REQ])})`);
  let { text, clicks } = look(v);
  check(/Opening…/.test(text) && !clicks["pa-open"], `Opening… and no click (got ${text.slice(0, 200)}; clicks ${Object.keys(clicks)})`);
  delete g.__shared[REQ][S]; advance(3000); // the keeper took it; the tab is not recorded yet
  ({ text, clicks } = look(v));
  check(/Opening…/.test(text) && !clicks["pa-open"], "still Opening… until the tab is recorded");
  g.__shared["project-agent.tabs"] = alive(); g.__spaces[0].tabs.push(paTab); g.__panes.push(paPane()); g.__terminals.push(paTerm()); g.__agents = [{ address: "t4w", status: "idle" }];
  ({ text, clicks } = look(v));
  check(!/Opening…|Not running/.test(text) && !clicks["pa-open"], `running: no button (got ${text.slice(0, 200)})`);
};

cases["P-open-timeout"] = async () => { // nothing started after 30 s: says why, the button is back
  world();
  const v = await mount();
  await click(v, "pa-open");
  g.__shared[REQ][S].why = "another Herdr client holds its keeper.lock";
  advance(31000);
  const { text, clicks } = look(v);
  check(/did not open|not opened/i.test(text) && /keeper\.lock/.test(text), `a message with the reason (got ${text.slice(0, 400)})`);
  check(Boolean(clicks["pa-open"]), "the button is back");
};

cases["P-lookalike"] = async () => { // a hand-run agent that looks like this PA: confirm first
  world({ extraTabs: [{ id: "tb3", index: 2, name: "claude", named: false }], extraPanes: [{ pane: "p3", terminal: "t3", tab: "tb3", title: "PA: proj", cwd: "/elsewhere", status: "idle", program: claude }],
    extraTerms: [{ address: "t3", state: "live", argv: ["claude", "--resume"], labels: {}, status: "idle", program: claude }], agents: [{ address: "t3", status: "idle" }] });
  const v = await mount();
  await click(v, "pa-open");
  check(!(g.__shared[REQ] ?? {})[S], `no request before the confirm (got ${JSON.stringify(g.__shared[REQ])})`);
  const { text, clicks } = look(v);
  check(/seems to run in tab/.test(text) && Boolean(clicks["pa-open-anyway"]) && Boolean(clicks["pa-open-cancel"]), `confirm shown (got ${text.slice(0, 400)})`);
  await click(v, "pa-open-cancel");
  check(!(g.__shared[REQ] ?? {})[S] && !/seems to run in tab/.test(look(v).text), "Cancel: nothing written, confirm gone");
  await click(v, "pa-open");
  await click(v, "pa-open-anyway");
  check(Boolean((g.__shared[REQ] ?? {})[S]), "Open anyway writes the request");
};

// ---- R8: notices go to the live terminal chosen at send time ----
const card = (id) => { g.__files[`:${DIR}/decisions/${id}.json`] = JSON.stringify({ id, status: "open", question: `Q ${id}?`, options: [{ label: "A" }, { label: "B" }], created: "2026-11-16T10:00:00Z" }); };
async function cardsWorld(opts = {}, ids = ["d-x"]) {
  world(opts);
  for (const id of ids) card(id);
  g.__ls[`:${DIR}/decisions`] = ids.map((id) => `${id}.json`);
  const v = await mount();
  return v;
}
async function answerCard(v, id) { await click(v, `opt-${id}-0`); await click(v, `submit-${id}`); }
const sends = () => g.__hcs.filter((a) => a[0] === "send");

cases["R8-none"] = async () => { // no alive target: nothing sent, a plain message with the Open button
  const v = await cardsWorld();
  await answerCard(v, "d-x");
  check(JSON.parse(g.__files[`:${DIR}/decisions/d-x.json`]).status === "answered", "the answer is written");
  v.resend(cx); await flush();
  check(sends().length === 0, `no hc send (got ${JSON.stringify(g.__hcs)})`);
  const { text, clicks } = look(v);
  check(/Answer saved\. The Project Agent is not running\./.test(text) && !/Trying again/.test(text), `plain not-running message (got ${text.slice(0, 500)})`);
  check(Boolean(clicks["pa-open"]), "the Open button is offered");
};

cases["R8-later"] = async () => { // an alive target appears: each queued notice sent once, in order
  const v = await cardsWorld({}, ["d-x", "d-y"]);
  await answerCard(v, "d-x");
  await answerCard(v, "d-y");
  check(sends().length === 0, "nothing sent while not running");
  g.__shared["project-agent.tabs"] = alive("t4w");
  v.resend(cx); await flush();
  v.resend(cx); await flush();
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "decision d-x answered"], ["send", "t4w", "decision d-y answered"]]), `sent once each, in order, to the record's terminal (got ${JSON.stringify(sends())})`);
  check(!/Answer saved|hasn't been told/.test(look(v).text), "no message left");
};

cases["R8-stale"] = async () => { // agent.json names a dead t30; the alive record names t4w: only t4w
  const v = await cardsWorld({ agentTerminal: "t30", records: alive("t4w") });
  await answerCard(v, "d-x");
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "decision d-x answered"]]), `sent to t4w only (got ${JSON.stringify(g.__hcs)})`);
};

cases["R8-rule"] = async () => { // a rule decided in the Rules view behaves the same
  const v = await cardsWorld({}, []);
  const space = g.__spaces[0];
  await v.ruleDecided(space, "r-1", "approved", cx); await flush();
  check(sends().length === 0, `nothing sent while not running (got ${JSON.stringify(g.__hcs)})`);
  check(/Answer saved\. The Project Agent is not running\./.test(look(v).text), "plain not-running message");
  g.__shared["project-agent.tabs"] = alive("t4w");
  v.resend(cx); await flush();
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "rule r-1 approved"]]), `sent once (got ${JSON.stringify(sends())})`);
};

// ---- t-003 review round (REVIEW.md M2, M4, M5, M6, N2) ----
cases["P-stale-keeper"] = async () => { // M2: an alive record from a keeper that is not the lease holder's: not trusted
  const v = await cardsWorld({ records: alive(), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "working" }] });
  g.__shared["project-agent.keeper"] = { id: "an-old-copy", at: T0 - 3600000 };
  const { text, clicks } = look(v);
  check(!/working/.test(text) && /unknown/i.test(text), `status unknown, not the stale one (got ${text.slice(0, 200)})`);
  check(!clicks["pa-open"], "no Open while nobody has judged (no keeper would take it)");
  await answerCard(v, "d-x");
  v.resend(cx); await flush();
  check(sends().length === 0, `no notice sent on a stale alive mark (got ${JSON.stringify(g.__hcs)})`);
  g.__shared["project-agent.keeper"] = { id: "K", at: T0 + 1000 };
  v.resend(cx); await flush();
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "decision d-x answered"]]), `sent once the lease holder's keeper has judged (got ${JSON.stringify(sends())})`);
};

cases["P-offline"] = async () => { // M6: the space's machine is offline: "Machine offline", no Open
  world({ machine: "m" });
  const v = await mount();
  check(/Not running/.test(look(v).text), "loaded while online, no record: Not running");
  g.__machines[1].layout = "unavailable"; g.__offline = ["m"];
  const { text, clicks } = look(v);
  check(/machine offline/i.test(text) && !/Not running/.test(text), `says the machine is offline (got ${text.slice(0, 200)})`);
  check(!clicks["pa-open"], "no Open while its machine is offline");
};

cases["P-open-starting"] = async () => { // M4: the tab opened but its launcher waits on an error: keep Opening…, show the error
  world();
  const v = await mount();
  await click(v, "pa-open");
  delete g.__shared[REQ][S]; // taken by the keeper
  g.__shared["project-agent.tabs"] = { tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal: "t4w" } };
  g.__spaces[0].tabs.push(paTab); g.__panes.push({ ...paPane(), status: null, program: { key: "program", name: "Python", shell: false, agent: false } });
  g.__terminals.push({ address: "t4w", state: "live", argv: ["pa-session"], labels: { "herdr.pa.error": "project folder does not exist: /proj" }, status: null, program: { key: "program", name: "Python", shell: false, agent: false } });
  advance(31000);
  const { text, clicks } = look(v);
  check(!/did not appear/.test(text), `no wrong "did not appear" (got ${text.slice(0, 400)})`);
  check(/project folder does not exist/.test(text) && /Enter/.test(text), `shows the launcher's error (got ${text.slice(0, 400)})`);
  check(!clicks["pa-open"], "no second Open while its launcher waits");
};

const lookAlikeWorld = (pane) => world({ extraTabs: [{ id: "tb3", index: 2, name: pane.tabName ?? "claude", named: true }],
  extraPanes: [{ pane: "p3", terminal: "t3", tab: "tb3", title: pane.title, cwd: pane.cwd, status: "idle", program: claude }],
  extraTerms: [{ address: "t3", state: "live", argv: ["claude", "--resume"], labels: {}, status: "idle", program: claude }], agents: [{ address: "t3", status: "idle" }] });
cases["P-lookalike-tabname"] = async () => { // M5: Claude sets its own pane title; the tab name says PA: <name>
  lookAlikeWorld({ tabName: "PA: proj", title: "✳ Claude Code", cwd: "/elsewhere" });
  const v = await mount();
  await click(v, "pa-open");
  check(/seems to run in tab/.test(look(v).text) && !(g.__shared[REQ] ?? {})[S], "confirm first");
};
cases["P-lookalike-subfolder"] = async () => { // M5: run by hand from a subfolder of the project (or with a trailing slash)
  for (const cwd of ["/proj/src", "/proj/"]) {
    lookAlikeWorld({ title: "✳ Claude Code", cwd });
    const v = await mount();
    await click(v, "pa-open");
    check(/seems to run in tab/.test(look(v).text) && !(g.__shared[REQ] ?? {})[S], `confirm first for cwd ${cwd}`);
  }
  lookAlikeWorld({ title: "✳ Claude Code", cwd: "/project-other" });
  const v = await mount();
  await click(v, "pa-open");
  check(Boolean((g.__shared[REQ] ?? {})[S]), "a sibling folder with the same prefix is not a match");
};

cases["R8-retry"] = async () => { // N2: a live target whose hc send fails: kept, said, retried, then sent once
  const v = await cardsWorld({ records: alive("t4w"), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "blocked" }] }, ["d-x", "d-y"]);
  g.__hcFail = ["t4w"];
  await answerCard(v, "d-x");
  await answerCard(v, "d-y");
  check(/hasn't been told yet \(refused\)\. Trying again every 10 s\./.test(look(v).text), `retry message (got ${look(v).text.slice(0, 400)})`);
  check(sends().length >= 1 && !sends().some((x) => x[2] === "decision d-y answered"), `the second notice waits behind the first (got ${JSON.stringify(sends())})`);
  g.__hcFail = []; g.__hcs = [];
  v.resend(cx); await flush();
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "decision d-x answered"], ["send", "t4w", "decision d-y answered"]]), `both sent, in order (got ${JSON.stringify(sends())})`);
};

// ---- t-003 verifier round (VERIFY.md F2, F3, §4 surviving mutations) ----
cases["P-open-why-kept"] = async () => { // F2: the keeper gives the request up first: the panel keeps its reason
  world();
  const v = await mount();
  await click(v, "pa-open");
  g.__shared[REQ][S].why = "its machine m is offline";
  advance(10000); look(v);
  delete g.__shared[REQ][S]; // the keeper expired it (and notified)
  advance(21000);
  const { text, clicks } = look(v);
  check(/offline/.test(text) && !/no new tab/.test(text), `the keeper's reason, not a guess (got ${text.slice(0, 300)})`);
  check(Boolean(clicks["pa-open"]), "the button is back");
};

const keptShell = { tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal: "t1" } };
cases["P-kept-shell"] = async () => { // M3b: a record that is not alive (kept shell): Not running, Open, nothing sent
  const v = await cardsWorld({ records: keptShell, extraTabs: [paTab], extraPanes: [{ pane: "p1", terminal: "t1", tab: "tb1", title: "zsh", cwd: "/home/u", status: null, program: shell }],
    extraTerms: [{ address: "t1", state: "live", argv: ["zsh"], labels: { "herdr.pa": S }, status: null, program: shell }] });
  const { text, clicks } = look(v);
  check(/Not running/.test(text) && Boolean(clicks["pa-open"]), `Not running + Open (got ${text.slice(0, 200)})`);
  await answerCard(v, "d-x");
  v.resend(cx); await flush();
  check(sends().length === 0, `nothing typed into the kept shell (got ${JSON.stringify(g.__hcs)})`);
};

cases["P-restoring"] = async () => { // F3: while the keeper restores (or reopens) the record: no Open
  world({ records: { tb1: { ...keptShell.tb1, recovering: { since: T0, from: "t1" } } }, extraTabs: [paTab],
    extraPanes: [{ pane: "p1", terminal: "t1", tab: "tb1", title: "zsh", cwd: "/proj", status: null, program: shell }], extraTerms: [{ address: "t1", state: "live", argv: ["zsh"], labels: {}, status: null, program: shell }] });
  let v = await mount();
  check(/Restoring/.test(look(v).text) && !look(v).clicks["pa-open"], `Restoring…, no Open (got ${look(v).text.slice(0, 200)})`);
  world({ records: keptShell }); // its tab vanished: the keeper reopens it
  v = await mount();
  check(/Reopening/.test(look(v).text) && !look(v).clicks["pa-open"], `Reopening…, no Open (got ${look(v).text.slice(0, 200)})`);
};

cases["P-lookalike-negative"] = async () => { // M7d, M7e: a worker, or an unrelated agent elsewhere, is no look-alike
  lookAlikeWorld({ tabName: "claude", title: "✳ Claude Code", cwd: "/elsewhere" });
  let v = await mount();
  await click(v, "pa-open");
  check(Boolean((g.__shared[REQ] ?? {})[S]) && !/seems to run/.test(look(v).text), "an unrelated agent: no confirm");
  world({ extraTabs: [{ id: "tb3", index: 2, name: "PA: proj worker", named: true }], extraPanes: [{ pane: "p3", terminal: "t3", tab: "tb3", title: "PA: proj", cwd: "/proj", status: "working", program: claude }],
    extraTerms: [{ address: "t3", state: "live", argv: ["claude"], parent: "t1", labels: {}, status: "working", program: claude }] });
  v = await mount();
  await click(v, "pa-open");
  check(Boolean((g.__shared[REQ] ?? {})[S]) && !/seems to run/.test(look(v).text), "a worker (parent set): no confirm");
};

cases["R8-order"] = async () => { // M20 (D10): a kept notice goes before one queued while it was being sent
  const v = await cardsWorld({ records: alive("t4w"), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "idle" }] }, []);
  v.unsent = [{ spaceId: S, message: "m1" }];
  g.__hcFail = ["t4w"];
  g.__onHc = () => { g.__onHc = null; v.unsent.push({ spaceId: S, message: "m2" }); };
  v.resend(cx); await flush();
  g.__hcFail = []; g.__hcs = [];
  v.resend(cx); await flush();
  check(JSON.stringify(sends().map((x) => x[2])) === JSON.stringify(["m1", "m2"]), `m1 then m2 (got ${JSON.stringify(sends())})`);
};

// ---- t-003 review round 2 (REVIEW.md R2-I2, R2-M2, R2-N2) ----
cases["P-stopped-vanish"] = async () => { // R2-I2: the keeper stopped and the tab vanished: Open, not "Reopening…"
  world({ records: { tb1: { ...keptShell.tb1, stopped: true } } });
  const v = await mount();
  const { text, clicks } = look(v);
  check(/Not running/.test(text) && !/Reopening/.test(text) && Boolean(clicks["pa-open"]), `Open offered (got ${text.slice(0, 200)})`);
  await click(v, "pa-open");
  check(Boolean((g.__shared[REQ] ?? {})[S]), "the click writes a request");
  world({ records: keptShell }); // not stopped: the keeper reopens it by itself
  const v2 = await mount();
  check(/Reopening/.test(look(v2).text) && !look(v2).clicks["pa-open"], "not stopped: Reopening…, no Open");
};

cases["P-restoring-error"] = async () => { // R2-M2: a restore whose new launcher waits on an error: say why
  world({ records: { tb1: { ...keptShell.tb1, terminal: "t2", recovering: { since: T0, from: "t1", to: "t2" } } }, extraTabs: [paTab],
    extraPanes: [{ pane: "p2", terminal: "t2", tab: "tb1", title: "", cwd: "/proj", status: null, program: { key: "program", name: "Python", shell: false, agent: false } }],
    extraTerms: [{ address: "t2", state: "live", argv: ["pa-session"], labels: { "herdr.pa.error": "profile test is missing" }, status: null, program: { key: "program", name: "Python", shell: false, agent: false } }] });
  const v = await mount();
  const { text } = look(v);
  check(/Restoring/.test(text) && /profile test is missing/.test(text) && /Enter/.test(text), `Restoring… with the error (got ${text.slice(0, 300)})`);
};

cases["P-frozen-mark"] = async () => { // R2-N2: a mark the keeper has not refreshed for 20 s (a reload): alive not trusted
  const v = await cardsWorld({ records: alive(), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "working" }] });
  check(/working/.test(look(v).text), "trusted while fresh");
  g.__now += 25000; g.__shared["project-agent.timer"] = { id: "K", at: g.__now - 5000 }; // lease still fresh, the mark frozen
  check(/unknown/i.test(look(v).text) && !/working/.test(look(v).text), `frozen mark: Status unknown (got ${look(v).text.slice(0, 200)})`);
  await answerCard(v, "d-x");
  v.resend(cx); await flush();
  check(sends().length === 0, `no notice on frozen marks (got ${JSON.stringify(g.__hcs)})`);
};

// ---- t-003 verifier round 2 (VERIFY.md round 2 §4: N4d, N4b, N5c, N5d, N10) ----
cases["P-e2e-notice"] = async () => { // N4d: no hand-written lease or mark: this copy's own keeper judges, and a notice is delivered
  const v = await cardsWorld({ extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [{ ...paTerm(), created_ms: T0 - 600000, labels: { "herdr.pa": S, "herdr.pa.name": "proj", "herdr.pa.project": "/proj", "herdr.pa.profile": "test" } }], agents: [{ address: "t4w", status: "idle" }] });
  delete g.__shared["project-agent.timer"]; delete g.__shared["project-agent.keeper"];
  g.__files[`:${DIR}/heartbeat`] = `${Math.floor(T0 / 1000) - 2} t4w\n`;
  const { tick } = await import(path.join(dst, "timer.js"));
  await tick(v.me); // the lease is this copy's
  v.keeper.readBeats(cx); await flush();
  v.keeper.syncPA(cx); await flush();
  v.keeper.syncPA(cx); await flush();
  check(g.__shared["project-agent.keeper"]?.id === v.me, `this copy's keeper wrote its mark (got ${JSON.stringify(g.__shared["project-agent.keeper"])})`);
  check(Object.values(g.__shared["project-agent.tabs"]).some((r) => r.terminal === "t4w" && r.alive === true), `record alive (got ${JSON.stringify(g.__shared["project-agent.tabs"])})`);
  await answerCard(v, "d-x");
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "decision d-x answered"]]), `notice delivered (got ${JSON.stringify(g.__hcs)})`);
};

cases["P-dead-lease"] = async () => { // N4b: the mark matches a lease whose holder went silent over 50 s ago: not trusted
  const v = await cardsWorld({ records: alive(), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "working" }] });
  g.__shared["project-agent.timer"] = { id: "K", at: T0 - 60000 }; g.__shared["project-agent.keeper"] = { id: "K", at: T0 };
  check(/unknown/i.test(look(v).text) && !/working/.test(look(v).text), `dead lease: Status unknown (got ${look(v).text.slice(0, 200)})`);
};

const startingRecord = (labels) => {
  g.__shared["project-agent.tabs"] = { tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal: "t4w" } };
  g.__spaces[0].tabs.push(paTab); g.__panes.push({ ...paPane(), status: null, program: { key: "program", name: "Python", shell: false, agent: false } });
  g.__terminals.push({ address: "t4w", state: "live", argv: ["pa-session"], labels, status: null, program: { key: "program", name: "Python", shell: false, agent: false } });
};
cases["P-starting-noclick"] = async () => { // N5c, N5d: a recorded launcher that is starting (no click in this copy): Opening…, no Open, no "did not start" message
  world();
  startingRecord({ "herdr.pa.starting": S });
  const v = await mount();
  let { text, clicks } = look(v);
  check(/Opening…/.test(text) && !clicks["pa-open"], `Opening…, not clickable (got ${text.slice(0, 200)})`);
  g.__terminals.find((t) => t.address === "t4w").labels = { "herdr.pa.error": "profile test is missing" };
  advance(200000);
  ({ text, clicks } = look(v));
  check(/profile test is missing/.test(text) && !/has not started after 3 minutes|did not open/.test(text) && !clicks["pa-open"], `waiting on its error: no timeout message (got ${text.slice(0, 300)})`);
};

cases["P-open-slow"] = async () => { // N10: the new tab is recorded but its launcher is slow: Opening… for 3 minutes, then say so
  world();
  const v = await mount();
  await click(v, "pa-open");
  delete g.__shared[REQ][S];
  startingRecord({});
  advance(31000);
  let { text, clicks } = look(v);
  check(/Opening…/.test(text) && !/did not open|has not started/.test(text) && !clicks["pa-open"], `still Opening… at 31 s (got ${text.slice(0, 300)})`);
  advance(150000);
  ({ text, clicks } = look(v));
  check(/has not started after 3 minutes/.test(text) && Boolean(clicks["pa-open"]), `after 3 minutes it says so and Open is back (got ${text.slice(0, 300)})`);
};

cases["P-open-error-long"] = async () => { // N5d: clicked, the new launcher waits on an error for minutes: no timeout message, no Open
  world();
  const v = await mount();
  await click(v, "pa-open");
  delete g.__shared[REQ][S];
  startingRecord({ "herdr.pa.error": "profile test is missing" });
  advance(200000);
  const { text, clicks } = look(v);
  check(/profile test is missing/.test(text) && !/has not started after 3 minutes|did not open/.test(text) && !clicks["pa-open"], `the error, no timeout message (got ${text.slice(0, 300)})`);
};

// ---- t-003 review round 3 (REVIEW.md R3-M1) ----
cases["P-stale-starting"] = async () => { // a stale starting/error label on an idle shell does not block Open
  for (const labels of [{ "herdr.pa.starting": S }, { "herdr.pa.error": "profile test is missing" }]) {
    world({ records: { tb1: { ...keptShell.tb1, stopped: true } }, extraTabs: [paTab],
      extraPanes: [{ pane: "p1", terminal: "t1", tab: "tb1", title: "zsh", cwd: "/home/u", status: null, program: shell }],
      extraTerms: [{ address: "t1", state: "live", argv: ["zsh"], labels: { "herdr.pa": S, ...labels }, status: null, program: shell }] });
    const v = await mount();
    const { text, clicks } = look(v);
    check(/Not running/.test(text) && !/Opening…|profile test is missing/.test(text) && Boolean(clicks["pa-open"]), `${JSON.stringify(labels)}: Open offered, no stale state (got ${text.slice(0, 250)})`);
    await click(v, "pa-open");
    check(Boolean((g.__shared[REQ] ?? {})[S]), `${JSON.stringify(labels)}: the click writes a request`);
  }
};

// ---- t-008: card context line by line; closed and kept worker chips ----
cases["P1-context-lines"] = async () => { // a multi-line context renders one row per line; "  " lines are indented
  world();
  g.__files[`:${DIR}/decisions/d-wt-t-003.json`] = JSON.stringify({ id: "d-wt-t-003", task: "t-003", status: "open", question: "Delete the worktree?",
    context: "ctx-a\nctx-b\n  ctx-c", options: [{ label: "Delete" }, { label: "Keep" }], created: "2026-11-16T10:00:00Z" });
  g.__files[`:${DIR}/decisions/d-one.json`] = JSON.stringify({ id: "d-one", status: "open", question: "Q?", context: "one line of context",
    options: [{ label: "A" }], created: "2026-11-16T09:00:00Z" });
  g.__ls[`:${DIR}/decisions`] = ["d-wt-t-003.json", "d-one.json"];
  const v = await mount();
  const tree = v.render(cx);
  const [a, b, c] = ["ctx-a", "ctx-b", "ctx-c"].map((x) => stubs.findText(tree, x));
  check(a.length === 1 && b.length === 1 && c.length === 1, `one row per line (got ${a.length}, ${b.length}, ${c.length}; text ${inspect(tree).text.slice(0, 300)})`);
  check(JSON.stringify(c[0].style?.pl) === "[12]" && !a[0].style?.pl, `the "  " line is indented, the others not (got ${JSON.stringify(c[0].style)})`);
  const one = stubs.findText(tree, "one line of context");
  check(one.length === 1 && one[0].kind === "div", `a one-line context is one row, as before (got ${one.length})`);
};

cases["P2-worker-chips"] = async () => { // a closed worker is not drawn (t-011), a kept one "· kept"; others unchanged
  world({ extraTerms: [{ address: "t32", state: "live", argv: ["claude"], labels: {}, status: "idle", program: claude },
                       { address: "t33", state: "live", argv: ["claude"], labels: {}, status: "working", program: claude }] });
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-003", title: "T", state: "in-progress", workers: [
    { address: "t31", role: "build", name: "t-003-tests", finished: true, closed: "2026-11-16T10:00:00Z", closedBy: "agent", report: "FINDINGS.md" },
    { address: "t32", role: "reviewer", name: "t-003-review", keep: true, keptAt: "2026-11-16T10:00:00Z", keepQuote: "keep it" },
    { address: "t33", role: "verifier", name: "t-003-verify" },
    { address: "t34", role: "build", name: "t-003-old" }] }] });
  const v = await mount();
  await click(v, "view-tasks");
  await click(v, "task-head-t-003");
  const { text } = look(v);
  check(!/t31/.test(text), `no chip for the closed worker (got ${text.slice(0, 400)})`);
  check(/reviewer t32 · kept ↗/.test(text), `kept chip, still live (got ${text.slice(0, 400)})`);
  check(/verifier t33 ↗/.test(text) && !/verifier t33 · kept/.test(text), "a live worker unchanged");
  check(/build t34 \(ended\)/.test(text), "a gone worker that was not closed still reads (ended)");
};

// ---- t-012 Part P: a slow remote, a cache per space, timeouts, and the panel while hidden ----
const M = "m1", RS = `${M}/${S}`, RDIR = `.config/herdr-pa/spaces/${S}`;
const D = 50; // ms per remote call
async function settle() { for (let i = 0; i < 25; i++) await new Promise((r) => setTimeout(r, 2)); await flush(); }
async function step(ms) { advance(ms); await settle(); }
const remoteCalls = (kind) => g.__issued.filter((x) => x.on === M && (!kind || x.kind === kind));
const reads = (name) => g.__issued.filter((x) => x.on === M && x.kind === "read" && x.path.endsWith(`/${name}`));
const ledgerOf = (n, prefix = "t") => JSON.stringify({ tasks: Array.from({ length: n }, (_, i) => ({ id: `${prefix}-${i + 1}`, title: `${prefix} task ${i + 1}`, state: "in-progress", updated: `2026-11-16T10:0${i}:00Z` })) });
const openCard = (id, extra = {}) => JSON.stringify({ id, status: "open", question: `Question ${id}?`, options: [{ label: "Yes" }, { label: "No" }], created: "2026-11-16T10:00:00Z", ...extra });
// A remote space (m1/sp1) with tasks and n open cards; `id` and `dir` name another space on m1.
function remoteCards(n, { id = S, prefix = "d" } = {}) {
  const dir = `.config/herdr-pa/spaces/${id}/decisions`, names = [];
  for (let i = 1; i <= n; i++) { const c = `${prefix}-${String(i).padStart(2, "0")}`; names.push(`${c}.json`); g.__files[`${M}:${dir}/${c}.json`] = openCard(c); }
  g.__ls[`${M}:${dir}`] = names;
}
function addRemoteSpace(id, name, tasks) {
  g.__spaces.push({ id: `${M}/${id}`, name, machine: M, folder: `/${name}`, active: false, tabs: [{ id: `tb-${id}`, index: 1, name: "1", named: false }] });
  g.__files[`${M}:.config/herdr-pa/spaces/${id}/agent.json`] = JSON.stringify({ space: id, project: `/${name}`, profile: "test", name });
  g.__files[`${M}:.config/herdr-pa/spaces/${id}/ledger.json`] = tasks;
}
const show = (id) => { for (const sp of g.__spaces) sp.active = sp.id === id; };
const panelLayout = (shownChild, { hidden = false, timerCopy = false } = {}) => ({ type: "row", id: "root", children: [
  { type: "block", id: "main", content: { tab: "active" } },
  { type: "column", id: "panel", hidden, children: [{ type: "stack", id: "panel-body", active: shownChild, children: [
    { type: "block", id: "changes", content: { mod: "my-changes" } },
    { type: "block", id: "project-agent", content: { mod: "project-agent" } }] }] },
  ...(timerCopy ? [{ type: "block", id: "pa-timer", content: { mod: "project-agent", props: { timer: true } } }] : []),
] });
const fire = async (ms) => { for (const t of g.__timers.filter((x) => x.ms === ms)) t.fn(cx); await settle(); };

cases["P-first-paint"] = async () => { // AC1.1, AC1.2: header and tasks after 2 round trips, cards after; never "No open decisions" while loading
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(27);
  g.__remoteDelay = D;
  const v = await mount();
  check(/Loading…/.test(look(v).text), "Loading… before any read returns");
  await step(D); await step(D);
  let { text } = look(v);
  check(/PA: proj/.test(text) && /Tasks · 2/.test(text) && !/(^| )Loading…/.test(text), `header and tasks by 2·d (got ${text.slice(0, 300)})`);
  await click(v, "view-decisions");
  ({ text } = look(v));
  check(/Loading decisions…/.test(text) && !/No open decisions/.test(text), `"Loading decisions…", not "No open decisions" (got ${text.slice(0, 400)})`);
  await step(D);
  ({ text } = look(v));
  check(/Decisions · 27/.test(text) && !/Loading decisions…/.test(text), `cards by 3·d (got ${text.slice(0, 300)})`);
};

cases["P-no-agent-remote"] = async () => { // AC1.3: no agent.json: today's view after one read
  world({ machine: M, agent: false });
  g.__remoteDelay = D;
  const v = await mount();
  await step(D);
  check(/has no Project Agent yet/.test(look(v).text), `no-agent view after 1 read (got ${look(v).text.slice(0, 200)})`);
  check(remoteCalls().length === 1, `one remote call (got ${remoteCalls().length})`);
};

cases["P-total-time"] = async () => { // AC3.1, AC2.1: the whole load settles by 3·d; the cards are one remote call
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(27);
  g.__remoteDelay = D;
  const v = await mount();
  await step(D); await step(D); await step(D);
  check(/Decisions · 27/.test(look(v).text), `settled by 3·d (got ${look(v).text.slice(0, 200)})`);
  check(!v.loads?.[RS], "no load in flight after 3·d");
  check(remoteCalls("batch").length === 1 && remoteCalls("ls").length === 0 && g.__issued.filter((x) => x.on === M && x.kind === "read" && x.path.includes("/decisions/")).length === 0, `cards from 1 batch (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
};

// A then B then A: both on m1; A has 2 tasks and 2 cards, B 3 tasks and 1 card.
async function twoSpaces() {
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2, "a");
  remoteCards(2, { prefix: "da" });
  addRemoteSpace("sp2", "other", ledgerOf(3, "b"));
  remoteCards(1, { id: "sp2", prefix: "db" });
  g.__files[`${M}:.config/herdr-pa/spaces/sp2/ledger.json`] = JSON.stringify({ tasks: [{ id: "b-1", title: "b", state: "in-progress", worktree: "/wt/b" }, { id: "b-2", state: "queued" }, { id: "b-3", state: "queued" }] });
  const v = await mount(); await settle();
  check(/Tasks · 2/.test(look(v).text) && /Decisions · 2/.test(look(v).text), `A loaded (got ${look(v).text.slice(0, 200)})`);
  return v;
}

cases["P-cache-switch-back"] = async () => { // AC4.1: back to A with every read hung: A's tasks and cards at once
  const v = await twoSpaces();
  show(`${M}/sp2`); look(v); await settle();
  check(/Tasks · 3/.test(look(v).text), `B loaded (got ${look(v).text.slice(0, 200)})`);
  g.__remoteDelay = 1e12; // nothing returns any more
  show(RS);
  const { text } = look(v);
  check(/PA: proj/.test(text) && /Tasks · 2/.test(text) && /Decisions · 2/.test(text) && !/(^| )Loading…/.test(text), `A from the cache at once (got ${text.slice(0, 300)})`);
};

cases["P-no-cross-space"] = async () => { // AC4.2: B's late load draws nothing and writes none of B's shared keys while A is shown
  const v = await twoSpaces();
  g.__remoteDelay = D;
  show(`${M}/sp2`); look(v); await settle();
  check(/(^| )Loading…/.test(look(v).text), "B loading");
  show(RS); look(v); await settle(); // back to A before B's first read returns
  await step(D); await step(D); await step(D); await step(D);
  const { text } = look(v);
  check(/PA: proj/.test(text) && /Tasks · 2/.test(text) && !/PA: other|Tasks · 3/.test(text), `only A's data (got ${text.slice(0, 300)})`);
  for (const k of ["project-agent.worktrees", "my-files.places", "project-agent.workers", "project-agent.open", "project-agent.mainBranches"]) check(!(g.__shared[k] ?? {})[`${M}/sp2`], `${k} has nothing for B (got ${JSON.stringify(g.__shared[k])})`);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 2, `A's open count kept (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
};

cases["P-updating-note"] = async () => { // AC4.3: a cached result over 60 s old says "updating…" while its refresh runs; a fresh one does not
  const v = await twoSpaces();
  show(`${M}/sp2`); look(v); await settle();
  g.__remoteDelay = D;
  show(RS);
  check(!/updating…/.test(look(v).text), `fresh cache: no note (got ${look(v).text.slice(0, 300)})`);
  await step(D); await step(D); await step(D); await step(D);
  show(`${M}/sp2`); look(v); await step(D); await step(D); await step(D); await step(D);
  advance(61000);
  show(RS);
  check(/updating…/.test(look(v).text) && /Tasks · 2/.test(look(v).text), `61 s old: "updating…" (got ${look(v).text.slice(0, 300)})`);
  await step(D); await step(D); await step(D); await step(D);
  check(!/updating…/.test(look(v).text), "the note goes when the refresh commits");
};

cases["P-answer-from-cache"] = async () => { // AC4.4: answering a cached card reads the file now; a card answered meanwhile is refused
  const v = await twoSpaces();
  show(`${M}/sp2`); look(v); await settle();
  g.__files[`${M}:${RDIR}/decisions/da-01.json`] = openCard("da-01", { status: "answered", answer: "No" });
  g.__remoteDelay = 1e12;
  show(RS); look(v); await settle(); // A's refresh is out, and hangs
  g.__remoteDelay = 0; // the answer's own read returns
  await click(v, "view-decisions");
  await click(v, "opt-da-01-0");
  await click(v, "submit-da-01");
  await settle();
  check(/This card was already answered\./.test(look(v).text), `refused with today's message (got ${look(v).text.slice(0, 500)})`);
  check(JSON.parse(g.__files[`${M}:${RDIR}/decisions/da-01.json`]).answer === "No", "the file was not overwritten");
};

cases["P-hung-agent-json"] = async () => { // AC5.1: a hung agent.json leaves "Loading…" within 10 s, with "agent.json: timed out"
  world({ machine: M });
  g.__hang = [`${M}:${RDIR}/agent.json`];
  const places = [{ label: "All evidence", path: "/x", group: "Project Agent" }];
  g.__shared["project-agent.open"] = { [RS]: 3 }; g.__shared["my-files.places"] = { [RS]: places };
  const v = await mount();
  await step(9000);
  check(/(^| )Loading…/.test(look(v).text), "still loading at 9 s");
  await step(1000);
  const { text } = look(v);
  check(!/(^| )Loading…/.test(text) && /agent\.json: timed out/.test(text), `timed out at 10 s (got ${text.slice(0, 300)})`);
  check(!/has no Project Agent yet/.test(text) && !look(v).clicks["pa-start"], `a timeout is not "no Project Agent" (got ${text.slice(0, 300)})`);
  check(g.__shared["project-agent.open"][RS] === 3 && JSON.stringify(g.__shared["my-files.places"][RS]) === JSON.stringify(places), `a timeout clears no shared entry (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
};

cases["P-hung-agent-json-kept"] = async () => { // AC5.1: a hung agent.json with a kept load: the kept tasks and cards stay, with the error line
  const v = await twoSpaces();
  show(`${M}/sp2`); look(v); await settle();
  show(RS); look(v); await settle();
  g.__hang = [`${M}:${RDIR}/agent.json`];
  advance(11000); v.reload(cx); await settle();
  await step(10000);
  const { text } = look(v);
  check(/Tasks · 2/.test(text) && /Decisions · 2/.test(text) && /agent\.json: timed out/.test(text), `kept data and the error (got ${text.slice(0, 400)})`);
  check(v.cache.get(RS)?.cards?.length === 2, `the kept entry is not overwritten (got ${v.cache.get(RS)?.cards?.length})`);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 2, `the count stays (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
};

cases["P-abandoned-load"] = async () => { // AC5.3: a load given up after 11 s commits nothing when it ends; the newer one does
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(3);
  const v = await mount(); await settle();
  check(/Decisions · 3/.test(look(v).text), "loaded");
  g.__execHang = [`${M}:batch`]; g.__remoteDelay = 1500; // the batch hangs, every other call takes 1.5 s
  g.__files[`${M}:${RDIR}/decisions/d-04.json`] = openCard("d-04"); g.__ls[`${M}:${RDIR}/decisions`].push("d-04.json");
  v.reload(cx); await settle(); // load 1: its batch times out at 13 s
  for (let t = 0; t < 23; t++) await step(500);
  v.reload(cx); await settle(); // 11.5 s: load 1 is given up, load 2 starts
  for (let t = 0; t < 5; t++) await step(500);
  check(/Decisions · 3/.test(look(v).text), `14 s: load 1 ended without committing (got ${look(v).text.slice(0, 300)})`);
  for (let t = 0; t < 40; t++) await step(500);
  check(/Decisions · 4/.test(look(v).text), `load 2 committed (got ${look(v).text.slice(0, 300)})`);
};

cases["P-cache-cap"] = async () => { // limits: at most 8 spaces kept, the least recently shown dropped
  world({ machine: M });
  for (let i = 2; i <= 9; i++) addRemoteSpace(`sp${i}`, `p${i}`, ledgerOf(i, `p${i}`));
  const v = await mount(); await settle();
  for (let i = 2; i <= 9; i++) { show(`${M}/sp${i}`); look(v); await settle(); check(new RegExp(`Tasks · ${i}`).test(look(v).text), `sp${i} loaded`); }
  check(v.cache.size === 8 && !v.cache.has(RS), `8 kept, the first dropped (got ${[...v.cache.keys()]})`);
  g.__remoteDelay = 1e12;
  show(`${M}/sp2`); check(/Tasks · 2/.test(look(v).text), "sp2 still kept");
  show(RS); check(/(^| )Loading…/.test(look(v).text), "sp1 was dropped: Loading…");
};

cases["P-hung-card-fallback"] = async () => { // AC5.2: batch fails, one card read hangs: tasks and the other cards show
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(3);
  g.__batchFail = { [M]: { code: 1, stdout: "" } };
  g.__hang = [`${M}:${RDIR}/decisions/d-02.json`];
  const v = await mount();
  await step(10000); await settle();
  const { text } = look(v);
  check(/Tasks · 2/.test(text) && /Decisions · 2/.test(text) && /d-01/.test(text) && /d-03/.test(text) && !/d-02/.test(text), `2 cards and the tasks (got ${text.slice(0, 400)})`);
};

cases["P-stuck-load-recovers"] = async () => { // AC5.3: a pending load blocks later reloads for at most 10 s + ε
  world({ machine: M });
  g.__hang = [`${M}:${RDIR}/agent.json`];
  const v = await mount();
  await step(2000); v.reload(cx); await settle();
  check(reads("agent.json").length === 1, `no second load while the first is young (got ${reads("agent.json").length})`);
  g.__hang = [];
  await step(9500); await settle();
  check(reads("agent.json").length === 2, `the queued reload ran after the stuck one ended (got ${reads("agent.json").length})`);
  check(/PA: proj/.test(look(v).text), `loaded (got ${look(v).text.slice(0, 200)})`);
};

cases["P-one-in-flight"] = async () => { // AC5.4, AC5.3: one load per space in flight; another space's load does not restart it
  const v = await twoSpaces();
  g.__remoteDelay = 3000;
  g.__issued = [];
  show(`${M}/sp2`); look(v); await settle();
  for (let t = 0; t < 6; t++) { v.reload(cx); look(v); await step(1500); }
  const bReads = g.__issued.filter((x) => x.kind === "read" && x.path.endsWith("spaces/sp2/agent.json"));
  check(bReads.length <= 1 || bReads[1].at - bReads[0].at >= 9000, `B's loads do not overlap (agent.json read at ${bReads.map((x) => x.at - T0)})`);
  // A load of A that ends after the switch to B must not clear B's in-flight state (B's load is the longer one).
  world({ machine: M });
  remoteCards(2, { prefix: "da" });
  addRemoteSpace("sp2", "other", ledgerOf(3, "b"));
  remoteCards(5, { id: "sp2", prefix: "db" });
  g.__remoteDelay = 3000;
  const w = await mount(); look(w); await step(1000);
  show(`${M}/sp2`); look(w); await step(1000);
  for (let t = 0; t < 40; t++) { look(w); await step(1000); } // A's abandoned load ends meanwhile (today: about 30 s)
  const b2 = g.__issued.filter((x) => x.kind === "read" && x.path.endsWith("spaces/sp2/agent.json")).map((x) => x.at - T0);
  check(b2.length === 1, `one B load (agent.json read at ${b2})`);
};

// ---- R8: while hidden, only the open-card count ----
cases["P-onscreen"] = async () => { // how "shown" is judged: stack child, hidden node, the timer copy, not in the layout
  const runs = [["stack shows changes", panelLayout("changes"), false], ["stack shows project-agent", panelLayout("project-agent"), true],
    ["panel hidden", panelLayout("project-agent", { hidden: true }), false], ["timer copy shown, panel hidden", panelLayout("changes", { timerCopy: true }), false],
    ["not in the layout", {}, true], ["stack with no active: first child", { type: "stack", id: "s", children: [{ type: "block", id: "pa", content: { mod: "project-agent" } }, { type: "block", id: "c", content: { mod: "my-changes" } }] }, true]];
  for (const [what, layout, shown] of runs) {
    world({ machine: M }); remoteCards(1); g.__layout = layout;
    const v = await mount(); await settle();
    const full = reads("rules.json").length > 0; // only a full load reads rules (the hidden panel reads agent.json once, for the profile)
    check(full === shown, `${what}: ${shown ? "full load" : "count only"} (rules.json reads ${reads("rules.json").length}, calls ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
    void v;
  }
};

cases["P-hidden-one-call"] = async () => { // AC8.1 (d-039): hidden: exactly 2 remote calls per 10 s interval, the batched card read and the ledger
  world({ machine: M });
  remoteCards(4);
  g.__layout = panelLayout("changes");
  const v = await mount();
  for (let t = 1; t <= 60; t++) { // 60 s: every timer the panel set, and folder-watch reloads every 3 s
    advance(1000);
    for (const tm of g.__timers) if (t * 1000 % tm.ms === 0) tm.fn(cx);
    if (t % 3 === 0) v.reload(cx);
    look(v);
    await settle();
  }
  check(reads("agent.json").length === 1 && reads("profile.json").length === 1 && reads("agent.json")[0].at === T0, `the profile read once, at the start (agent.json at ${reads("agent.json").map((x) => x.at - T0)})`);
  const kinds = [...new Set(remoteCalls().filter((x) => x.at > T0).map((x) => (x.kind === "read" ? `read ${x.path.split("/").pop()}` : x.kind)))].sort();
  check(JSON.stringify(kinds) === '["batch","read ledger.json"]', `only the batched card read and the ledger (got ${JSON.stringify(remoteCalls().map((x) => `${x.kind}${x.path ? ` ${x.path}` : ""}`).slice(0, 12))})`);
  const per = []; for (let w = 0; w < 6; w++) per.push(remoteCalls().filter((x) => x.at > T0 + w * 10000 && x.at <= T0 + (w + 1) * 10000));
  check(per.every((l) => l.length === 2 && l[0].at === l[1].at), `2 calls, issued together, in each 10 s interval (got ${per.map((l) => l.map((x) => x.at - T0))})`);
};

cases["P-hidden-count"] = async () => { // AC8.2: the button's count follows the cards on disk while hidden
  world({ machine: M });
  remoteCards(2);
  g.__layout = panelLayout("changes");
  const v = await mount(); await settle();
  check((g.__shared["project-agent.open"] ?? {})[RS] === 2, `2 open (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  g.__files[`${M}:${RDIR}/decisions/d-03.json`] = openCard("d-03"); g.__ls[`${M}:${RDIR}/decisions`].push("d-03.json");
  await step(5000); await fire(10000);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 2, "not before the interval");
  await step(5000); await fire(10000);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 3, `3 after the interval (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  for (const c of ["d-01", "d-02", "d-03"]) g.__files[`${M}:${RDIR}/decisions/${c}.json`] = openCard(c, { status: "answered", answer: "Yes" });
  await step(10000); await fire(10000);
  check(!(RS in (g.__shared["project-agent.open"] ?? {})), `none open: the key is gone (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  g.__batchFail = { [M]: { code: 1, stdout: "" } };
  g.__files[`${M}:${RDIR}/decisions/d-01.json`] = openCard("d-01");
  await step(10000); await fire(10000);
  check(!(RS in (g.__shared["project-agent.open"] ?? {})), "a failed read leaves the count as it was");
  check(reads("rules.json").length === 0 && reads("agent.json").length === 1 && remoteCalls("ls").length === 0, `nothing but the batch, the ledger and the profile once (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
};

cases["P-hidden-notice"] = async () => { // AC8.3: a new card's notice still comes while the panel is hidden
  world({ machine: M, records: { tb1: { space: RS, project: "/proj", profile: "test", name: "proj", terminal: "t4w", alive: true } } });
  g.__spaces[0].tabs.push({ id: "tb1", index: 2, name: "PA: proj", named: true });
  remoteCards(1);
  g.__layout = panelLayout("changes");
  delete g.__shared["project-agent.timer"]; delete g.__shared["project-agent.keeper"]; // this copy holds the lease
  const v = await mount(); await settle();
  await fire(15000); // the first look records what is there
  g.__files[`${M}:${RDIR}/decisions/d-new.json`] = openCard("d-new", { task: "t-1" }); g.__ls[`${M}:${RDIR}/decisions`].push("d-new.json");
  g.__now += 15000; await fire(15000);
  check(g.__notes.some((n) => /^Decision needed \(t-1\): d-new$/.test(n.title)), `notice fired while hidden (got ${JSON.stringify(g.__notes.map((n) => n.title))})`);
  // (agent.json and the ledger are also read by this copy's keeper and timer, which hold the lease here.)
  check(reads("rules.json").length === 0 && remoteCalls("evidence").length === 0 && remoteCalls("stale").length === 0, `and the panel did no full load (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
  void v;
};

cases["P-shown-again"] = async () => { // AC8.4: shown again: the kept data at once, one full reload started at once
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(2);
  g.__layout = panelLayout("project-agent");
  const v = await mount(); await settle();
  check(/Tasks · 2/.test(look(v).text), "loaded while shown");
  g.__layout = panelLayout("changes");
  for (let t = 0; t < 30; t++) { await step(10000); await fire(10000); await fire(15000); }
  check(reads("agent.json").length === 1, `no full load in 5 minutes hidden (agent.json reads ${reads("agent.json").length})`);
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(3);
  g.__remoteDelay = D;
  g.__layout = panelLayout("project-agent");
  const { text } = look(v); // render runs as it is shown
  check(/Tasks · 2/.test(text) && !/(^| )Loading…/.test(text), `kept data at once (got ${text.slice(0, 200)})`);
  await settle(); // the clock stands still: nothing settles
  check(reads("agent.json").length === 2, `one full reload started before any read settled (agent.json reads ${reads("agent.json").length})`);
  look(v); await fire(1000);
  check(reads("agent.json").length === 2, "not a second one");
  await step(D); await step(D); await step(D);
  check(/Tasks · 3/.test(look(v).text), `then refreshed (got ${look(v).text.slice(0, 200)})`);
  // Shown again with no render: the 1 s timer starts the reload.
  g.__layout = panelLayout("changes"); await step(10000); await fire(10000);
  const before = reads("agent.json").length;
  g.__layout = panelLayout("project-agent");
  await fire(1000);
  check(reads("agent.json").length === before + 1, `the 1 s timer wakes it (agent.json reads ${before} -> ${reads("agent.json").length})`);
};

// ---- t-012 Part P fix round (review P-I1, P-M1..P-M5, d-039, verifier N1-N2) ----
cases["P-slow-progress-commits"] = async () => { // AC5.3-AC5.5 (review P-I1): a slow but working load (over 11 s) commits with reloads every 2 s; loads do not overlap
  for (const [label, delay, rounds, batchFails] of [["4 s per call", 4000, 3, false], ["2.5 s per call, batch failing", 2500, 5, true], ["6 s per call", 6000, 3, false]]) {
    world({ machine: M });
    g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
    remoteCards(3);
    const v = await mount(); await settle();
    check(/Decisions · 3/.test(look(v).text), `${label}: loaded`);
    g.__remoteDelay = delay;
    if (batchFails) g.__batchFail = { [M]: { code: 1, stdout: "" } };
    g.__files[`${M}:${RDIR}/decisions/d-04.json`] = openCard("d-04"); g.__ls[`${M}:${RDIR}/decisions`].push("d-04.json");
    const start = g.__now, L = delay * rounds;
    let seen = null;
    for (let t = 0; t < 90; t++) { if (t % 2 === 0) v.reload(cx); look(v); await step(1000); if (seen === null && /Decisions · 4/.test(look(v).text)) seen = t + 1; }
    check(seen !== null && seen * 1000 <= 2 * L + 2000, `${label}: d-04 shown within two loads (${(2 * L) / 1000} s; first shown at ${seen} s)`);
    const at = reads("agent.json").filter((x) => x.at > start).map((x) => x.at - start); // after the mount's own load
    const gaps = at.slice(1).map((x, i) => x - at[i]);
    check(at.length >= 2 && gaps.every((gp) => gp >= L - 1000), `${label}: one load at a time, each about ${L / 1000} s (agent.json read at ${at.map((x) => x / 1000)} s)`);
  }
};

cases["P-given-up-commits"] = async () => { // AC5.3a: a given-up load (a read that ignores its limit) commits when it ends first; not once a newer load has committed
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(3);
  let v = await mount(); await settle();
  check(/Decisions · 3/.test(look(v).text), "loaded");
  const AJ = `${M}:${RDIR}/agent.json`, addCard = (c) => { g.__files[`${M}:${RDIR}/decisions/${c}.json`] = openCard(c); g.__ls[`${M}:${RDIR}/decisions`].push(`${c}.json`); };
  g.__sleepHang = true; g.__delay[AJ] = 20000; // load 1's agent.json answers after 20 s, past every limit
  addCard("d-04");
  v.reload(cx); await settle();
  await step(6000); v.reload(cx); await settle();
  check(reads("agent.json").length === 2, `6 s: no second load yet (agent.json reads ${reads("agent.json").length})`);
  const load1 = v.loads[RS]?.token;
  await step(6000); v.reload(cx); await settle(); // 12 s, no progress: load 1 is given up; load 2 starts
  // t-053 (R2): load 2 starts no second agent.json read while load 1's is pending; it waits for it, then reads again.
  check(v.loads[RS]?.token > load1 && reads("agent.json").length === 2, `12 s: load 1 given up, load 2 started, no second read yet (agent.json reads ${reads("agent.json").length})`);
  for (let t = 0; t < 9; t++) await step(1000); // 21 s: load 1 has ended, load 2 has not
  check(/Decisions · 4/.test(look(v).text), `load 1 committed: no newer load had (got ${look(v).text.slice(0, 300)})`);
  // The other order: the newer load commits first; the older one, ending later, commits nothing.
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(3);
  v = await mount(); await settle();
  // t-053 (R2): a newer load never re-reads what an older one still waits on, so the older one ending last is one whose
  // slow call the newer load skipped: here its evidence listing (the newer load shows the last known evidence).
  g.__sleepHang = true; g.__execHang = [`${M}:evidence`];
  v.reload(cx); await settle(); // load A: its evidence listing hangs
  await step(12000); addCard("d-04");
  await fire(10000); // load B (background): given A up; the evidence listing still pending is skipped; it ends at once
  check(/Decisions · 4/.test(look(v).text) && v.cache.get(RS)?.cards?.length === 4, `load B committed (got ${look(v).text.slice(0, 300)})`);
  g.__execHang = []; await settle(); // A ends now, with the 3 cards it read at 0 s
  for (let t = 0; t < 3; t++) await step(1000);
  check(/Decisions · 4/.test(look(v).text) && v.cache.get(RS)?.cards?.length === 4, `A committed nothing after B (got ${look(v).text.slice(0, 300)})`);
};

cases["P-late-first-paint"] = async () => { // AC4.1 (verifier N1): B's first paint after the switch back to A changes nothing shown
  const v = await twoSpaces();
  g.__remoteDelay = D;
  show(`${M}/sp2`); look(v); await settle(); // B's load starts (nothing kept for B)
  g.__delay[`${M}:${RDIR}/agent.json`] = 20 * D; // A's refresh is slow
  show(RS); look(v); await settle(); // back to A before B's first read returns
  await step(D); await step(D); // B's first paint would land now
  check(v.space === RS, `the panel stays on A (got ${v.space})`);
  const { text } = look(v);
  check(/PA: proj/.test(text) && /Tasks · 2/.test(text) && /Decisions · 2/.test(text) && !/(^| )Loading…/.test(text), `A's kept data (got ${text.slice(0, 300)})`);
};

cases["P-hung-agent-json-kept-entry"] = async () => { // AC4.6 (verifier N2): a timed-out agent.json never writes the kept entry
  const v = await twoSpaces();
  show(`${M}/sp2`); look(v); await settle();
  show(RS); look(v); await settle();
  const before = v.cache.get(RS);
  g.__hang = [`${M}:${RDIR}/agent.json`];
  advance(11000); v.reload(cx); await settle();
  await step(10000);
  check(/agent\.json: timed out/.test(look(v).text), "timed out");
  const kept = v.cache.get(RS);
  check(kept?.at === before.at && !(kept?.data?.errors ?? []).some((e) => /timed out/.test(e)), `the kept entry is the last good load (errors ${JSON.stringify(kept?.data?.errors)}, at ${kept?.at - before?.at})`);
};

const tasksWith = (list) => JSON.stringify({ tasks: list });
const t1 = { id: "t-1", title: "one", state: "in-progress", worktree: "/wt/1", updated: "2026-11-16T10:00:00Z" };
const evSpots = [{ label: "All evidence", path: `${RDIR}/evidence`, group: "Project Agent" }, { label: "t-1 evidence · 2 files", detail: "one", path: `${RDIR}/evidence/t-1`, group: "Project Agent", task: "t-1" }];
const wtSpot = (x) => ({ label: `${x.id} worktree`, detail: x.title, path: x.worktree, group: "Project Agent", task: x.id });
cases["P-timeout-keeps-evidence"] = async () => { // AC4.6 (review P-M2): an evidence read that times out never drops the evidence shortcuts
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([t1]);
  remoteCards(1);
  g.__shared["my-files.places"] = { [RS]: [evSpots[0], evSpots[1], { ...wtSpot(t1), path: "/wt/old" }] }; // written earlier (another copy, or before a reload)
  g.__execHang = [`${M}:evidence`];
  const v = await mount(); await step(10000); await settle();
  let spots = g.__shared["my-files.places"]?.[RS];
  check(JSON.stringify(spots) === JSON.stringify([evSpots[0], evSpots[1], wtSpot(t1)]), `nothing kept: the evidence shortcuts stay, the worktree is rebuilt (got ${JSON.stringify(spots)})`);
  // Shown with evidence, then the kept entry is gone (more than 8 spaces) and the evidence read times out.
  g.__execHang = []; g.__execOut = { [`${M}:evidence`]: { stdout: "t-1\ta.md\nt-1\tb.md\n" } };
  g.__shared["my-files.places"] = {};
  advance(12000); v.reload(cx); await settle();
  check(JSON.stringify(g.__shared["my-files.places"]?.[RS]) === JSON.stringify([evSpots[0], evSpots[1], wtSpot(t1)]), `evidence read (got ${JSON.stringify(g.__shared["my-files.places"]?.[RS])})`);
  v.cache.clear(); g.__execHang = [`${M}:evidence`];
  advance(12000); v.reload(cx); await settle(); await step(10000); await settle();
  spots = g.__shared["my-files.places"]?.[RS];
  check(JSON.stringify(spots) === JSON.stringify([evSpots[0], evSpots[1], wtSpot(t1)]) && v.data?.evidence?.get?.("t-1")?.length === 2, `the shown evidence is kept (got ${JSON.stringify(spots)}, ${JSON.stringify([...(v.data?.evidence ?? new Map())])})`);
};

cases["P-timeout-keeps-stale"] = async () => { // AC4.6 (review P-M2): a stale-instructions read that times out keeps what is shown for the space
  world({ machine: M });
  remoteCards(1);
  g.__execOut = { [`${M}:stale`]: { stdout: "core/worker.md\n" } };
  const v = await mount(); await settle();
  check(JSON.stringify(v.data?.stale) === '["worker.md"]', `stale read (got ${JSON.stringify(v.data?.stale)})`);
  v.cache.clear(); g.__execHang = [`${M}:stale`];
  advance(12000); v.reload(cx); await settle(); await step(10000); await settle();
  check(!v.loads[RS] && JSON.stringify(v.data?.stale) === '["worker.md"]', `kept after the timeout (got ${JSON.stringify(v.data?.stale)})`);
};

cases["P-cache-after-switch"] = async () => { // AC4.5 (review P-M5): a load that ends after a switch away is kept and shown on the way back; it writes no shared key
  const v = await twoSpaces();
  show(`${M}/sp2`); look(v); await settle();
  show(RS); look(v); await settle();
  g.__remoteDelay = D;
  g.__files[`${M}:${RDIR}/decisions/da-03.json`] = openCard("da-03"); g.__ls[`${M}:${RDIR}/decisions`].push("da-03.json");
  v.reload(cx); await settle(); // A's load starts
  show(`${M}/sp2`); look(v); await settle(); // and B is shown before it ends
  for (let i = 0; i < 6; i++) await step(D);
  check(!v.loads[RS], "A's load has ended");
  check((g.__shared["project-agent.open"] ?? {})[RS] === 2, `A's count is not written while B is shown (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  g.__remoteDelay = 1e12;
  show(RS);
  check(/Decisions · 3/.test(look(v).text), `back on A: its latest load at once (got ${look(v).text.slice(0, 300)})`);
};

const hiddenTask = { id: "t-9", title: "new", state: "in-progress", worktree: "/elsewhere/wt-9", updated: "2026-11-16T11:00:00Z", workers: [{ address: "w9", role: "build" }] };
cases["P-hidden-ledger"] = async () => { // AC8.5 (d-039): while hidden, the ledger refreshes worktrees, workers and the Files shortcuts; evidence and main branches from the last full load
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([{ ...t1, worktree: undefined }]);
  remoteCards(1);
  g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`] = JSON.stringify({ name: "test", mainBranches: ["trunk"] });
  g.__execOut = { [`${M}:evidence`]: { stdout: "t-1\ta.md\n" } };
  g.__layout = panelLayout("project-agent");
  const v = await mount(); await settle();
  const ev1 = { label: "t-1 evidence · 1 file", detail: "one", path: `${RDIR}/evidence/t-1`, group: "Project Agent", task: "t-1" };
  check(JSON.stringify(g.__shared["my-files.places"]?.[RS]) === JSON.stringify([evSpots[0], ev1]) && JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', `full load (got ${JSON.stringify(g.__shared["my-files.places"]?.[RS])})`);
  g.__layout = panelLayout("changes");
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([{ ...t1, worktree: undefined }, hiddenTask]);
  g.__execOut = { [`${M}:evidence`]: { stdout: "t-1\ta.md\nt-1\tb.md\nt-9\tx.md\n" } }; // not read while hidden
  g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`] = JSON.stringify({ name: "test", mainBranches: ["other"] }); // not read either
  const mark = g.__issued.length;
  await step(10000); await fire(10000);
  check(JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS]) === JSON.stringify([{ task: "t-9", path: "/elsewhere/wt-9", title: "new" }]), `worktrees (got ${JSON.stringify(g.__shared["project-agent.worktrees"])})`);
  check(JSON.stringify(g.__shared["project-agent.workers"]?.[RS]) === JSON.stringify({ "m1/w9": { task: "t-9", title: "new", role: "build", state: "in-progress" } }), `workers (got ${JSON.stringify(g.__shared["project-agent.workers"])})`);
  check(JSON.stringify(g.__shared["my-files.places"]?.[RS]) === JSON.stringify([evSpots[0], wtSpot(hiddenTask), ev1]), `Files shortcuts: the new worktree, the evidence as last loaded (got ${JSON.stringify(g.__shared["my-files.places"]?.[RS])})`);
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', `main branches from the last full load (got ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
  const since = g.__issued.slice(mark).filter((x) => x.on === M).map((x) => (x.kind === "read" ? x.path.split("/").pop() : x.kind)).sort();
  check(JSON.stringify(since) === '["batch","ledger.json"]', `only the batch and the ledger (got ${JSON.stringify(since)})`);
  // A space never shown in full: the evidence shortcuts already there stay; main branches are left alone.
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([t1]);
  remoteCards(1);
  g.__shared["my-files.places"] = { [RS]: [evSpots[0], evSpots[1]] }; g.__shared["project-agent.mainBranches"] = { [RS]: ["kept"] };
  g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`] = JSON.stringify({ name: "test", mainBranches: ["from-profile"] });
  g.__layout = panelLayout("changes");
  await mount(); await settle();
  check(JSON.stringify(g.__shared["my-files.places"]?.[RS]) === JSON.stringify([evSpots[0], evSpots[1], wtSpot(t1)]), `nothing kept: the evidence shortcuts stay (got ${JSON.stringify(g.__shared["my-files.places"]?.[RS])})`);
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["from-profile"]' && JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS]) === JSON.stringify([{ task: "t-1", path: "/wt/1", title: "one" }]), `worktrees written, main branches from the profile read once (got ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
};

cases["P-hidden-ledger-fails"] = async () => { // AC8.5: a ledger that fails, hangs, is missing or is not JSON leaves every key as it was
  const keys = ["project-agent.worktrees", "my-files.places", "project-agent.workers", "project-agent.mainBranches"];
  for (const how of ["invalid", "hung", "missing", "thrown"]) {
    world({ machine: M });
    remoteCards(1);
    const before = { "project-agent.worktrees": { [RS]: [{ task: "t-0", path: "/wt/0", title: "" }] }, "my-files.places": { [RS]: [evSpots[0]] }, "project-agent.workers": { [RS]: { "m1/w0": { task: "t-0" } } }, "project-agent.mainBranches": { [RS]: ["kept"] } };
    for (const k of keys) g.__shared[k] = before[k];
    g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`] = JSON.stringify({ name: "test", mainBranches: ["kept"] }); // the profile read gives the same
    const L = `${M}:${RDIR}/ledger.json`;
    if (how === "invalid") g.__files[L] = "{bad";
    if (how === "hung") g.__hang = [L];
    if (how === "missing") delete g.__files[L];
    if (how === "thrown") g.__fail = [L];
    g.__layout = panelLayout("changes");
    await mount(); await settle(); await step(10000); await settle();
    for (const k of keys) check(JSON.stringify(g.__shared[k]) === JSON.stringify(before[k]), `${how}: ${k} unchanged (got ${JSON.stringify(g.__shared[k])})`);
    check((g.__shared["project-agent.open"] ?? {})[RS] === 1, `${how}: the count is still written`);
  }
};

// ---- t-020 J6 (AC9.1, VP14): every recorded, unremoved worktree of a task, with its job ----
const t5 = { id: "t-5", title: "five", state: "in-progress", updated: "2026-11-16T12:00:00Z",
  worktree: "/p/worktrees/t-5-main/repo",
  worktrees: [
    { path: "/p/worktrees/t-5-main/repo/", job: "first", created: "2026-11-16T09:00:00Z" }, // the main one named: counted once, its entry unchanged
    { path: "/p/worktrees/t-5-billing/repo", job: "Billing Script", created: "2026-11-16T09:10:00Z" },
    { path: "/p/worktrees/t-5-gone/repo", job: "gone", created: "2026-11-16T09:20:00Z", removed: "2026-11-16T10:00:00Z" },
    { path: "/p/worktrees/t-5-again/repo", job: "billing-script", created: "2026-11-16T09:30:00Z" }, // the job taken: -2
    { path: "", job: "blank" }, "not an entry"],
  extraWorktrees: ["/p/worktrees/t-5-gone/repo", "/p/worktrees/t-5-billing/repo", "/p/worktrees/t-5-tool/repo", "/b/t-5-guard", 7] };
const t6 = { id: "t-6", title: "six", state: "in-progress", updated: "2026-11-16T11:30:00Z", worktree: null, worktrees: [{ path: "/q/wt-6", job: "panel" }] };
const t7 = { id: "t-7", title: "seven", state: "done", updated: "2026-11-16T11:20:00Z", worktree: null, worktrees: [{ path: "/q/wt-7", job: "x", removed: "2026-11-16T11:00:00Z" }], extraWorktrees: ["/q/wt-7"] };
const t5Trees = [
  { task: "t-5", path: "/p/worktrees/t-5-main/repo", title: "five" },
  { task: "t-5", path: "/p/worktrees/t-5-billing/repo", title: "five", job: "billing-script" },
  { task: "t-5", path: "/p/worktrees/t-5-again/repo", title: "five", job: "billing-script-2" },
  { task: "t-5", path: "/p/worktrees/t-5-tool/repo", title: "five", job: "tool" },
  { task: "t-5", path: "/b/t-5-guard", title: "five", job: "guard" }];
const jobSpot = (x, path, job) => ({ label: `${x.id} worktree · ${job}`, detail: x.title, path, group: "Project Agent", task: x.id, job });
cases["P-worktrees-jobs"] = async () => { // AC9.1: project-agent.worktrees lists the R1 set: main first, exactly {task, path, title}; others with job
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([t5, t6, t7, t1]);
  remoteCards(1);
  await mount(); await settle();
  const want = [...t5Trees, { task: "t-6", path: "/q/wt-6", title: "six", job: "panel" }, { task: "t-1", path: "/wt/1", title: "one" }];
  check(JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS]) === JSON.stringify(want), `worktrees (got ${JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS])})`);
  // Only the old field: the main one, nothing else.
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([{ ...t7, extraWorktrees: [] }, { ...t1, worktrees: [] }]);
  remoteCards(1);
  await mount(); await settle();
  check(JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS]) === JSON.stringify([{ task: "t-1", path: "/wt/1", title: "one" }]), `an old ledger is as before (got ${JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS])})`);
};

cases["P-files-job-shortcuts"] = async () => { // AC9.1: a Files shortcut per worktree, the job ones after the task's main one, shown and hidden
  const trees = (spots) => (spots ?? []).filter((x) => / worktree/.test(x.label));
  const want = [wtSpot({ ...t5 }), jobSpot(t5, "/p/worktrees/t-5-billing/repo", "billing-script"), jobSpot(t5, "/p/worktrees/t-5-again/repo", "billing-script-2"),
    jobSpot(t5, "/p/worktrees/t-5-tool/repo", "tool"), jobSpot(t5, "/b/t-5-guard", "guard"), jobSpot(t6, "/q/wt-6", "panel"), wtSpot(t1)];
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([t1, t7, t6, t5]);
  remoteCards(1);
  g.__execOut = { [`${M}:evidence`]: { stdout: "t-5\ta.md\n" } };
  await mount(); await settle();
  const spots = g.__shared["my-files.places"]?.[RS] ?? [];
  check(JSON.stringify(trees(spots)) === JSON.stringify(want), `shown: worktree shortcuts (got ${JSON.stringify(trees(spots))})`);
  const at = (label) => spots.findIndex((x) => x.label === label);
  check(at("t-5 evidence · 1 file") >= 0 && at("t-5 evidence · 1 file") < at("t-5 worktree") && at("t-5 worktree · guard") < at("t-6 worktree · panel"), `each task's evidence, then its worktrees (got ${JSON.stringify(spots.map((x) => x.label))})`);
  // While hidden, the ledger alone writes them the same way.
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([t1]);
  remoteCards(1);
  g.__layout = panelLayout("changes");
  await mount(); await settle();
  g.__files[`${M}:${RDIR}/ledger.json`] = tasksWith([t1, t7, t6, t5]);
  await step(10000); await fire(10000);
  check(JSON.stringify(trees(g.__shared["my-files.places"]?.[RS])) === JSON.stringify(want), `hidden: worktree shortcuts (got ${JSON.stringify(trees(g.__shared["my-files.places"]?.[RS]))})`);
  check(JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS]) === JSON.stringify([...t5Trees, { task: "t-6", path: "/q/wt-6", title: "six", job: "panel" }, { task: "t-1", path: "/wt/1", title: "one" }].sort((a, b) => ["t-1", "t-6", "t-5"].indexOf(a.task) - ["t-1", "t-6", "t-5"].indexOf(b.task))), `hidden: worktrees in ledger order (got ${JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS])})`);
};

cases["P-hidden-overlap"] = async () => { // AC8.2 (review P-M3): of two overlapping hidden reads, the older one ending last writes nothing
  world({ machine: M });
  remoteCards(3);
  g.__layout = panelLayout("changes");
  const v = await mount(); await settle();
  check((g.__shared["project-agent.open"] ?? {})[RS] === 3, "3 open");
  g.__sleepHang = true; g.__remoteDelay = 30000;
  await step(10000); await fire(10000); // read A at 10 s, answers at 40 s
  g.__remoteDelay = 0;
  for (const c of ["d-01", "d-02"]) g.__files[`${M}:${RDIR}/decisions/${c}.json`] = openCard(c, { status: "answered", answer: "Yes" });
  const batches = () => remoteCalls("batch").length, before = batches();
  await step(12000); await fire(10000); // 22 s: A has been busy for 12 s: read B
  // t-053 (R2): B starts no second card read while A's is pending (BUSY): nothing is written.
  check(batches() === before && (g.__shared["project-agent.open"] ?? {})[RS] === 3, `read B: no second card read, nothing written (batches ${batches() - before}, ${JSON.stringify(g.__shared["project-agent.open"])})`);
  await step(20000); // A answers at 40 s, the newest read: it writes
  check((g.__shared["project-agent.open"] ?? {})[RS] === 1, `A, the newest read, wrote (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  void v;
};

cases["P-onscreen-props"] = async () => { // R8 (review P-M4): only this copy's blocks (same props) decide whether it is shown
  const extra = (props, shownBlock) => ({ type: "row", id: "root", children: [panelLayout("changes"), { type: "block", id: "pane-pa", hidden: !shownBlock, content: { mod: "project-agent", props } }] });
  const runs = [["this copy (other props) is not in the layout; the side panel copy is hidden", { pane: "p5" }, panelLayout("changes"), true],
    ["the side panel copy (this one) is hidden; another copy with other props is shown", {}, extra({ profile: "other" }, true), false],
    ["this copy's own block is shown elsewhere", { profile: "other" }, extra({ profile: "other" }, true), true]];
  for (const [what, props, layout, shown] of runs) {
    world({ machine: M }); remoteCards(1); g.__layout = layout; g.__props = props;
    await mount(); await settle();
    const full = reads("rules.json").length > 0;
    check(full === shown, `${what}: ${shown ? "full load" : "count only"} (rules.json reads ${reads("rules.json").length})`);
  }
};

// ---- follow-up (t2r): the profile once per space while hidden ----
const PROFILE = `${M}:.config/herdr-pa/profiles/test/profile.json`;
cases["P-hidden-profile"] = async () => { // AC8.5: no kept load: the hidden panel reads the profile once per session for the main branches
  world({ machine: M }); remoteCards(1);
  g.__files[PROFILE] = JSON.stringify({ name: "test", mainBranches: ["trunk"] });
  g.__layout = panelLayout("changes");
  await mount(); await settle();
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', `main branches written (got ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
  for (let i = 0; i < 9; i++) { await step(10000); await fire(10000); }
  check(reads("agent.json").length === 1 && reads("profile.json").length === 1, `read once in 90 s (agent.json ${reads("agent.json").length}, profile.json ${reads("profile.json").length})`);
  check(reads("rules.json").length === 0, "no full load");
  // agent.json names no profile: config.json, then its default profile.
  world({ machine: M }); remoteCards(1);
  g.__files[`${M}:${RDIR}/agent.json`] = JSON.stringify({ space: S, project: "/proj", name: "proj" });
  g.__files[`${M}:.config/herdr-pa/config.json`] = JSON.stringify({ defaultProfile: "test" });
  g.__files[PROFILE] = JSON.stringify({ name: "test", mainBranches: ["c"] });
  g.__layout = panelLayout("changes");
  await mount(); await settle();
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["c"]' && reads("config.json").length === 1, `through config.json (got ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
  // No agent.json: nothing written, not read again.
  world({ machine: M, agent: false });
  g.__shared["project-agent.mainBranches"] = { [RS]: ["kept"] };
  g.__layout = panelLayout("changes");
  await mount(); await settle();
  for (let i = 0; i < 9; i++) { await step(10000); await fire(10000); }
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["kept"]' && reads("agent.json").length === 1, `no agent: left, read once (agent.json ${reads("agent.json").length})`);
};

cases["P-hidden-profile-retry"] = async () => { // AC8.5: a profile read that times out writes nothing and is tried again no sooner than 60 s after it started
  world({ machine: M }); remoteCards(1);
  g.__files[PROFILE] = JSON.stringify({ name: "test", mainBranches: ["trunk"] });
  g.__hang = [`${M}:${RDIR}/agent.json`];
  g.__layout = panelLayout("changes");
  await mount(); await settle();
  await step(10000); // 10 s: timed out
  for (let i = 0; i < 4; i++) { await step(10000); await fire(10000); } // 20-50 s
  check(!(RS in (g.__shared["project-agent.mainBranches"] ?? {})) && reads("agent.json").length === 1, `nothing written, no retry before 60 s (agent.json at ${reads("agent.json").map((x) => x.at - T0)})`);
  g.__hang = [];
  await step(10000); await fire(10000); // 60 s
  check(reads("agent.json").length === 2 && JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', `retried at 60 s and written (agent.json at ${reads("agent.json").map((x) => x.at - T0)}; ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
  for (let i = 0; i < 9; i++) { await step(10000); await fire(10000); }
  check(reads("agent.json").length === 2, `never again once answered (agent.json reads ${reads("agent.json").length})`);
};

// ---- fix round 2 (REVIEW-2 I1, M1-M3, N3; VERIFY-2 M14, M17) ----
const KEYS = ["project-agent.worktrees", "my-files.places", "project-agent.workers", "project-agent.mainBranches", "project-agent.open"];
const snap = (keys = KEYS) => JSON.stringify(keys.map((k) => g.__shared[k] ?? null));
const LEDGER = `${M}:${RDIR}/ledger.json`;
function goodRemote() {
  world({ machine: M });
  g.__files[LEDGER] = tasksWith([hiddenTask]);
  g.__files[PROFILE] = JSON.stringify({ name: "test", mainBranches: ["trunk"] });
  remoteCards(2);
}
const noteOf = (v, id) => Object.entries(v.notes).find(([k]) => k === id || k.endsWith(`:${id}`) || k.endsWith(`\0${id}`))?.[1];

cases["P-full-ledger-timeout"] = async () => { // I1 (AC4.6): a ledger or profile read that times out, fails or is not JSON in a full load keeps what is shown, the kept entry and every shared key
  for (const [how, error] of [["hung", /ledger\.json: timed out/], ["invalid", /ledger\.json: /], ["thrown", /read failed/]]) {
    goodRemote();
    const v = await mount(); await settle();
    check(/Tasks · 1/.test(look(v).text) && g.__shared["project-agent.worktrees"]?.[RS]?.length === 1 && JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', `${how}: loaded`);
    noteOf(v, "d-01").set("typed");
    const before = snap();
    if (how === "hung") g.__hang = [LEDGER, PROFILE];
    if (how === "invalid") { g.__files[LEDGER] = "{bad"; g.__files[PROFILE] = "{bad"; }
    if (how === "thrown") g.__fail = [LEDGER];
    advance(12000); v.reload(cx); await settle();
    for (let t = 0; t < 12; t++) await step(1000);
    check(!v.loads[RS], `${how}: the load ended`);
    const { text } = look(v);
    check(snap() === before, `${how}: every shared key as it was (before ${before}, after ${snap()})`);
    check(/Tasks · 1/.test(text) && /in progress 1/.test(text) && /Decisions · 2/.test(text), `${how}: the last known tasks and cards are shown (got ${text.slice(0, 400)})`);
    check(error.test(text), `${how}: the error is shown (got ${text.slice(0, 400)})`);
    check(v.cache.get(RS)?.data?.ledger?.tasks?.length === 1, `${how}: the kept entry keeps the ledger (got ${JSON.stringify(v.cache.get(RS)?.data?.ledger)})`);
    check(JSON.stringify(v.cache.get(RS)?.data?.profile?.mainBranches) === '["trunk"]', `${how}: the kept entry keeps the profile`);
    check(noteOf(v, "d-01")?.value() === "typed", `${how}: the typed note is kept`);
  }
  // A missing ledger did answer: it replaces the tasks and clears the keys, as today.
  goodRemote();
  const v = await mount(); await settle();
  delete g.__files[LEDGER];
  advance(12000); v.reload(cx); await settle();
  check(!(RS in (g.__shared["project-agent.worktrees"] ?? {})) && !(RS in (g.__shared["project-agent.workers"] ?? {})) && /no active tasks/.test(look(v).text), `a missing ledger clears (got ${JSON.stringify(g.__shared["project-agent.worktrees"])}, ${look(v).text.slice(0, 300)})`);
};

cases["P-full-profile-config-timeout"] = async () => { // I1: config.json (needed for the profile name) timing out keeps the main branches
  goodRemote();
  g.__files[`${M}:${RDIR}/agent.json`] = JSON.stringify({ space: S, project: "/proj", name: "proj" });
  g.__files[`${M}:.config/herdr-pa/config.json`] = JSON.stringify({ defaultProfile: "test" });
  const v = await mount(); await settle();
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', "loaded through config.json");
  const before = snap();
  g.__hang = [`${M}:.config/herdr-pa/config.json`];
  advance(12000); v.reload(cx); await settle();
  for (let t = 0; t < 12; t++) await step(1000);
  check(!v.loads[RS] && snap() === before, `the keys as they were (before ${before}, after ${snap()})`);
  check(JSON.stringify(v.data?.profile?.mainBranches) === '["trunk"]' && JSON.stringify(v.cache.get(RS)?.data?.profile?.mainBranches) === '["trunk"]', `the profile shown and kept (got ${JSON.stringify(v.data?.profile)})`);
};

cases["P-full-schedule-timeout"] = async () => { // I1 / M3: schedule.json and pass-request.json timing out keep the schedule bar
  goodRemote();
  g.__files[`${M}:${RDIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  g.__files[`${M}:${RDIR}/pass-request.json`] = JSON.stringify({ pending: true, at: "2026-11-16T10:00:00Z" });
  const v = await mount(); await settle();
  check(!/Passes · !/.test(look(v).text) && /Passes · queued/.test(look(v).text), `loaded (got ${look(v).text.slice(0, 400)})`);
  g.__hang = [`${M}:${RDIR}/schedule.json`, `${M}:${RDIR}/pass-request.json`];
  advance(12000); v.reload(cx); await settle();
  for (let t = 0; t < 12; t++) await step(1000);
  const { text } = look(v);
  check(!v.loads[RS] && !/Passes · !/.test(text) && /Passes · queued/.test(text), `the schedule and the queued pass are kept (got ${text.slice(0, 400)})`);
  check(v.cache.get(RS)?.data?.schedule?.everyMinutes === 15 && v.cache.get(RS)?.data?.request?.pending === true, "and kept");
};

cases["P-full-nothing-known"] = async () => { // I1: a ledger timing out on a space's first load (nothing kept or shown) writes none of the keys it feeds
  goodRemote();
  const others = { "project-agent.worktrees": { [RS]: [{ task: "t-0", path: "/wt/0", title: "" }] }, "my-files.places": { [RS]: [evSpots[0]] }, "project-agent.workers": { [RS]: { "m1/w0": { task: "t-0" } } }, "project-agent.mainBranches": { [RS]: ["kept"] } };
  Object.assign(g.__shared, others); // written by another copy
  const keys = Object.keys(others), before = snap(keys);
  g.__hang = [LEDGER, PROFILE];
  const v = await mount(); await settle();
  for (let t = 0; t < 12; t++) await step(1000);
  check(!v.loads[RS] && snap(keys) === before, `the keys as they were (before ${before}, after ${snap(keys)})`);
  check(/ledger\.json: timed out/.test(look(v).text), `the error is shown (got ${look(v).text.slice(0, 300)})`);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 2, "the cards answered: the count is written");
  g.__hang = [];
  advance(12000); v.reload(cx); await settle();
  check(JSON.stringify(g.__shared["project-agent.worktrees"]?.[RS]) === JSON.stringify([{ task: "t-9", path: "/elsewhere/wt-9", title: "new" }]) && JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', `the next load that answers writes them (got ${snap()})`);
};

cases["P-cards-timeout"] = async () => { // I1 / M3: the batch and the fallback listing both timing out or failing keep the cards, the count and the typed notes
  for (const how of ["hung", "thrown"]) {
    goodRemote();
    const v = await mount(); await settle();
    check(/Decisions · 2/.test(look(v).text), `${how}: loaded`);
    noteOf(v, "d-01").set("typed");
    if (how === "hung") g.__execHang = [`${M}:batch`, `${M}:ls`];
    else { g.__batchFail = { [M]: { code: 1 } }; g.__execFail = [`${M}:ls`]; }
    advance(12000); v.reload(cx); await settle();
    for (let t = 0; t < 25; t++) await step(1000);
    check(!v.loads[RS], `${how}: the load ended`);
    const { text } = look(v);
    check(/Decisions · 2/.test(text) && /Question d-01/.test(text), `${how}: the cards are kept (got ${text.slice(0, 400)})`);
    check((g.__shared["project-agent.open"] ?? {})[RS] === 2, `${how}: the count is kept (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
    check(noteOf(v, "d-01")?.value() === "typed", `${how}: the typed note is kept`);
    check(v.cache.get(RS)?.cards?.length === 2, `${how}: the kept entry keeps the cards (got ${v.cache.get(RS)?.cards?.length})`);
    check(/decision cards/i.test(text), `${how}: the error is shown (got ${text.slice(0, 400)})`);
  }
  // Nothing known: "Loading decisions…", never "No open decisions"; the count another copy wrote stays.
  goodRemote();
  g.__shared["project-agent.open"] = { [RS]: 5 };
  g.__execHang = [`${M}:batch`, `${M}:ls`];
  const v = await mount(); await settle();
  for (let t = 0; t < 25; t++) await step(1000);
  await click(v, "view-decisions");
  const { text } = look(v);
  check(!v.loads[RS] && /Loading decisions…/.test(text) && !/No open decisions/.test(text), `nothing known: still loading (got ${text.slice(0, 400)})`);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 5, `nothing known: the count as it was (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  g.__execHang = [];
  advance(12000); v.reload(cx); await settle();
  check(/Decisions · 2/.test(look(v).text) && (g.__shared["project-agent.open"] ?? {})[RS] === 2, "the next load that answers shows and counts them");
};

cases["P-card-hang-keeps-note"] = async () => { // M2: a card read that hangs on the fallback path keeps its last known copy, its typed note and the count
  goodRemote();
  const v = await mount(); await settle();
  noteOf(v, "d-01").set("typed");
  g.__batchFail = { [M]: { code: 1 } }; g.__hang = [`${M}:${RDIR}/decisions/d-01.json`];
  advance(12000); v.reload(cx); await settle();
  for (let t = 0; t < 15; t++) await step(1000);
  check(!v.loads[RS] && v.cards.map((c) => c.id).sort().join() === "d-01,d-02", `both cards (got ${v.cards.map((c) => c.id)})`);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 2 && noteOf(v, "d-01")?.value() === "typed", `count and note kept (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
};

cases["P-hidden-vs-full"] = async () => { // M1: a hidden read and a full load of a space write the shared keys in start order: the older never overwrites the newer
  const trees = () => JSON.stringify((g.__shared["project-agent.worktrees"] ?? {})[RS] ?? null);
  const newTrees = JSON.stringify([{ task: "t-9", path: "/elsewhere/wt-9", title: "new" }]);
  // 1. A hidden read (slow card batch, old ledger) ends after the full load that followed it committed.
  world({ machine: M }); remoteCards(1);
  g.__files[LEDGER] = tasksWith([t1]);
  g.__layout = panelLayout("changes");
  g.__execHang = [`${M}:batch`];
  const v = await mount(); await settle(); // hidden read H: ledger read now (old), batch hung (ends at 10 s)
  g.__execHang = []; g.__files[LEDGER] = tasksWith([hiddenTask]);
  g.__layout = panelLayout("project-agent"); await step(1000); await fire(1000); // shown: the full load commits the new ledger
  check(trees() === newTrees, `the full load wrote the new ledger (got ${trees()})`);
  g.__layout = panelLayout("changes"); v.reload(cx); await settle(); // hidden again (H still busy: no new read)
  for (let t = 0; t < 10; t++) await step(1000); // H ends
  check(trees() === newTrees, `the older hidden read wrote nothing (got ${trees()})`);
  // 2. A full load (slow card batch, old ledger) commits after a newer hidden read wrote.
  world({ machine: M }); remoteCards(1);
  g.__files[LEDGER] = tasksWith([t1]);
  g.__layout = panelLayout("project-agent");
  const w = await mount(); await settle();
  g.__execHang = [`${M}:batch`];
  advance(12000); w.reload(cx); await settle(); // full load L: old ledger read now, batch hung
  g.__execHang = []; g.__files[LEDGER] = tasksWith([hiddenTask]);
  g.__layout = panelLayout("changes"); w.reload(cx); await settle(); // hidden read H: the new ledger
  check(trees() === newTrees, `the hidden read wrote the new ledger (got ${trees()})`);
  for (let t = 0; t < 15; t++) await step(1000); // L ends and commits
  check(!w.loads[RS] && trees() === newTrees, `the older full load did not write over it (got ${trees()})`);
};

cases["P-given-up-keeps-successor"] = async () => { // VERIFY-2 M17 (AC5.3a, AC5.4): a given-up load that ends never clears its successor's in-flight entry
  world({ machine: M }); g.__files[LEDGER] = ledgerOf(2); remoteCards(3);
  const v = await mount(); await settle();
  const AJ = `${M}:${RDIR}/agent.json`;
  g.__sleepHang = true; g.__delay[AJ] = 20000;
  v.reload(cx); await settle(); // load 1: agent.json answers at 20 s
  await step(12000); v.reload(cx); await settle(); // 12 s: load 1 given up; load 2 (answers at 32 s)
  for (let t = 0; t < 10; t++) await step(1000); // 22 s: load 1 has ended
  v.reload(cx); await settle();
  check(reads("agent.json").length === 3, `no duplicate load (agent.json reads ${reads("agent.json").length}: the mount's, load 1, load 2)`);
};

cases["P-hung-agent-json-after-switch"] = async () => { // VERIFY-2 M14 (AC4.6): a timed-out agent.json that ends after a switch away writes no kept entry
  const v = await twoSpaces();
  show(`${M}/sp2`); look(v); await settle();
  show(RS); look(v); await settle();
  const before = v.cache.get(RS);
  g.__hang = [`${M}:${RDIR}/agent.json`];
  advance(12000); v.reload(cx); await settle(); // A's load: agent.json hung
  show(`${M}/sp2`); look(v); await settle(); // B shown before it ends
  await step(10000); await settle();
  check(!v.loads[RS], "A's load has ended");
  check(v.cache.get(RS) === before, `A's kept entry is the last good load (errors ${JSON.stringify(v.cache.get(RS)?.data?.errors)})`);
};

cases["P-notes-per-space"] = async () => { // REVIEW-2 N3: a note typed on one space's card never shows on, or goes with, another space's card of the same id
  world({ machine: M });
  g.__files[LEDGER] = ledgerOf(1, "a");
  remoteCards(2, { prefix: "dc" });
  addRemoteSpace("sp2", "other", ledgerOf(1, "b"));
  remoteCards(1, { id: "sp2", prefix: "dc" }); // B also has dc-01
  const v = await mount(); await settle();
  noteOf(v, "dc-01").set("for A");
  show(`${M}/sp2`); look(v); await settle();
  check(/PA: other/.test(look(v).text), "B shown");
  await answerCard(v, "dc-01"); await settle();
  const b = JSON.parse(g.__files[`${M}:.config/herdr-pa/spaces/sp2/decisions/dc-01.json`]);
  check(b.status === "answered" && b.note === null, `B's card got no note of A's (got ${JSON.stringify(b.note)})`);
  show(RS); look(v); await settle();
  const a = Object.entries(v.notes).filter(([k]) => k.endsWith("dc-01")).map(([, n]) => n.value());
  check(a.includes("for A"), `A's note is still there (got ${JSON.stringify(a)})`);
};

cases["P-hung-agent-json-keeps-keys"] = async () => { // I1 / round-1 verifier N5: a timed-out agent.json writes no shared key, not even from the kept data
  goodRemote();
  const v = await mount(); await settle();
  g.__shared["project-agent.worktrees"] = { [RS]: [{ task: "t-7", path: "/wt/7", title: "newer" }] }; g.__shared["project-agent.open"] = { [RS]: 4 }; // written since (a hidden refresh)
  const before = snap();
  g.__hang = [`${M}:${RDIR}/agent.json`];
  advance(12000); v.reload(cx); await settle(); await step(10000); await settle();
  check(!v.loads[RS] && /agent\.json: timed out/.test(look(v).text), "timed out");
  check(snap() === before, `the keys as they were (before ${before}, after ${snap()})`);
};

cases["P-unknown-profile-hidden"] = async () => { // I1 with AC8.5: a full load whose profile never answered leaves the main branches; while hidden they are not cleared from it, and the profile is read
  goodRemote();
  g.__shared["project-agent.mainBranches"] = { [RS]: ["kept"] }; // written by another copy
  g.__hang = [PROFILE];
  g.__layout = panelLayout("project-agent");
  const v = await mount(); await settle();
  for (let t = 0; t < 12; t++) await step(1000);
  check(!v.loads[RS] && JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["kept"]', `full load: left (got ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
  g.__layout = panelLayout("changes");
  advance(12000); v.reload(cx); await settle(); await step(1000); // hidden, the profile still hung: the ledger refresh leaves the main branches
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["kept"]', `hidden: not cleared from the unknown kept profile (got ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
  g.__hang = [];
  advance(60000); await settle(); v.reload(cx); await settle(); await step(1000); // the profile read is tried again and answers
  check(JSON.stringify(g.__shared["project-agent.mainBranches"]?.[RS]) === '["trunk"]', `hidden: the profile is read and written (got ${JSON.stringify(g.__shared["project-agent.mainBranches"])})`);
};

// Fix round 3 (REVIEW-3 I1): a part that did not answer and is shown from its last known value writes
// none of its shared keys, the open count or the kept entry, so a newer hidden refresh is not rolled back.
cases["P3-hidden-then-unknown-ledger"] = async () => {
  const keys = () => JSON.stringify(["project-agent.worktrees", "project-agent.workers", "my-files.places"].map((k) => (g.__shared[k] ?? {})[RS] ?? null));
  world({ machine: M }); remoteCards(1);
  g.__files[LEDGER] = tasksWith([t1]);
  g.__layout = panelLayout("project-agent");
  const v = await mount(); await settle();
  const old = keys();
  g.__files[LEDGER] = tasksWith([hiddenTask]);
  g.__layout = panelLayout("changes"); advance(12000); v.reload(cx); await settle();
  const hid = keys();
  check(hid !== old && /w9/.test(hid), `hidden wrote the newer ledger (${old} -> ${hid})`);
  g.__hang = [LEDGER];
  g.__layout = panelLayout("project-agent"); advance(12000); v.reload(cx); await settle();
  for (let t = 0; t < 12; t++) await step(1000);
  check(!v.loads[RS], "ended");
  check(v.data?.ledger?.tasks?.[0]?.id === "t-1" && /ledger\.json: timed out/.test(look(v).text), "the last known tasks are shown, with the error");
  check(keys() === hid, `the keys keep the hidden refresh's ledger (hidden ${hid}, now ${keys()})`);
};
cases["P3-hidden-then-unknown-cards"] = async () => {
  const open = () => g.__shared["project-agent.open"]?.[RS];
  world({ machine: M }); remoteCards(1);
  g.__layout = panelLayout("project-agent");
  const v = await mount(); await settle();
  check(open() === 1, `first load: 1 (got ${open()})`);
  remoteCards(3);
  g.__layout = panelLayout("changes"); advance(12000); v.reload(cx); await settle();
  check(open() === 3, `hidden: 3 (got ${open()})`);
  g.__execHang = [`${M}:batch`, `${M}:ls`];
  g.__layout = panelLayout("project-agent"); advance(12000); v.reload(cx); await settle();
  for (let t = 0; t < 25; t++) await step(1000);
  check(!v.loads[RS], "ended");
  check(/the last known are shown/.test(look(v).text), "the last known cards are shown");
  check(open() === 3, `the count keeps the hidden refresh's 3 (got ${open()})`);
};
cases["P3-filled-not-kept"] = async () => { // filled from what is shown (no kept entry): the kept entry takes none of it
  world({ machine: M }); remoteCards(1);
  g.__files[LEDGER] = tasksWith([t1]);
  g.__layout = panelLayout("project-agent");
  const v = await mount(); await settle();
  v.cache.clear();
  g.__hang = [LEDGER]; g.__execHang = [`${M}:batch`, `${M}:ls`];
  advance(12000); v.reload(cx); await settle();
  for (let t = 0; t < 40; t++) await step(1000);
  check(!v.loads[RS], "ended");
  check(v.data?.ledger?.tasks?.[0]?.id === "t-1" && v.cards.length === 1, `shown: the last known tasks and cards (${JSON.stringify(v.data?.ledger)}, ${v.cards.length})`);
  const k = v.cache.get(RS);
  check(k && (k.data.unknown ?? []).includes("ledger"), `kept: the ledger unknown (got ${JSON.stringify(k?.data?.unknown)})`);
  check(k && k.cardsUnknown === true, `kept: the cards unknown (got ${k?.cardsUnknown})`);
};

// ---- t-011: the task card's Delete can be backed out of ----
// A press: the mouse-down goes from the element with `id` up to the root, each "left" handler in
// turn, until one stops it; then that element's click (if it has one). `id` null: a press on the
// root itself (empty space in the panel).
function pathTo(root, id) {
  const walk = (x, trail) => {
    if (x === null || typeof x !== "object") return null;
    const here = [...trail, x];
    if (x.key === id) return here;
    for (const k of x.kids ?? []) { const f = walk(k, here); if (f) return f; }
    return null;
  };
  return id === null ? [root] : walk(root, []);
}
async function press(v, id) {
  const route = pathTo(v.render(cx), id);
  check(Boolean(route), `an element ${id} to press`);
  let stopped = false;
  const ecx = { ...cx, stop_propagation() { stopped = true; } };
  for (const el of [...route].reverse()) {
    const h = el.style?.on_mouse_down;
    if (h && h[0] === "left") h[1]({}, ecx);
    if (stopped) break;
  }
  // gpui draws the view again between the mouse-down and the click (t-028 A3): the click is that of the element
  // with this id in the tree drawn after the mouse-down (none: it is gone, nothing is clicked).
  const again = id === null ? null : pathTo(v.render(cx), id), target = again?.[again.length - 1];
  if (target?.click) target.click({}, cx);
  await flush();
}
const ARMED = "Delete the task, its evidence and its cards?";
const deletes = () => (g.__execs ?? []).filter(([c, a]) => c === "pa-task" && a[0] === "delete");
async function armWorld() {
  world();
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [
    { id: "t-1", title: "One", state: "in-progress", updated: "2026-11-16T10:02:00Z" },
    { id: "t-2", title: "Two", state: "in-progress", updated: "2026-11-16T10:01:00Z" }] });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  const v = await mount();
  await press(v, "view-tasks");
  await press(v, "task-head-t-1");
  return v;
}
const armedText = (v) => look(v).text.includes(ARMED);

cases["A1-two-clicks-delete"] = async () => { // V1.1: one click arms (with a Cancel beside it); the second click on it deletes
  const v = await armWorld();
  check(/Delete…/.test(look(v).text) && !look(v).clicks["delete-cancel-t-1"], "unarmed: Delete…, no Cancel");
  await press(v, "delete-t-1");
  check(armedText(v), `one click arms (got ${look(v).text.slice(-300)})`);
  check(Boolean(look(v).clicks["delete-cancel-t-1"]), `a Cancel button beside it (have ${Object.keys(look(v).clicks)})`);
  check(deletes().length === 0, "one click deletes nothing");
  await press(v, "delete-t-1");
  check(deletes().length === 1 && deletes()[0][1][1] === "t-1", `the second click deletes t-1 once (got ${JSON.stringify(deletes())})`);
};

cases["A2-cancel"] = async () => { // V1.2, V1.7: Cancel disarms; one click after that only arms again
  const v = await armWorld();
  await press(v, "delete-t-1");
  await press(v, "delete-cancel-t-1");
  check(!armedText(v) && /Delete…/.test(look(v).text), "Cancel: back to Delete…");
  check(!look(v).clicks["delete-cancel-t-1"], "no Cancel once disarmed");
  await press(v, "delete-t-1");
  check(armedText(v) && deletes().length === 0, "after a Cancel, one click arms again and deletes nothing");
};

cases["A3-press-elsewhere"] = async () => { // V1.3: a press anywhere else in the panel disarms
  const v = await armWorld();
  await press(v, "delete-t-1");
  await press(v, null); // empty space in the panel
  check(!armedText(v), "a press on empty space disarms");
  await press(v, "delete-t-1");
  await press(v, "awake-toggle"); // another control (t-145: the schedule line is in its tab)
  check(!armedText(v), "a press on another control disarms");
  await press(v, "delete-t-1");
  await press(v, "task-head-t-2"); // another task's header (expands it)
  check(!armedText(v), "a press on another task disarms");
  check(deletes().length === 0, "nothing deleted");
};

cases["A4-collapse"] = async () => { // V1.4: collapsing the task disarms; expanding it again shows Delete…
  const v = await armWorld();
  await press(v, "delete-t-1");
  look(v).clicks["task-head-t-1"]({}, cx); await flush(); // the header's click alone (no press on the root)
  look(v).clicks["task-head-t-1"]({}, cx); await flush();
  check(!armedText(v), "collapse and expand: not armed");
};

cases["A5-view-and-space"] = async () => { // V1.5: a view switch or a space switch disarms
  const v = await armWorld();
  await click(v, "delete-t-1");
  await click(v, "view-decisions"); await click(v, "view-tasks"); // the tabs' clicks alone
  check(!armedText(v), "view switched and back: not armed");
  await click(v, "delete-t-1");
  check(armedText(v), "armed again");
  const S2 = "sp2", D2 = "/home/u/.config/herdr-pa/spaces/sp2";
  g.__spaces.push({ id: S2, name: "other", machine: null, folder: "/other", active: false, tabs: [{ id: "tb2", index: 1, name: "1", named: false }] });
  g.__files[`:${D2}/agent.json`] = JSON.stringify({ space: S2, project: "/other", profile: "test", name: "other" });
  g.__files[`:${D2}/ledger.json`] = JSON.stringify({ tasks: [] });
  show(S2); v.reload(cx); await settle();
  show(S); v.reload(cx); await settle();
  check(look(v).text.includes("t-1"), "back on the first space with its task shown");
  check(!armedText(v), "space switched and back: not armed");
  check(deletes().length === 0, "nothing deleted");
};

cases["A6-times-out"] = async () => { // V1.6: by itself after 10 s; still armed at 9 s
  const v = await armWorld();
  await click(v, "delete-t-1");
  advance(9000); await fire(1000);
  check(armedText(v), "still armed at 9 s");
  advance(1500); await fire(1000);
  check(!armedText(v), "disarmed after 10 s");
  await click(v, "delete-t-1");
  check(armedText(v) && deletes().length === 0, "one click after the time-out arms again and deletes nothing");
};

cases["A7-default-tasks-view"] = async () => { // V1.1: the Tasks view shown by default (no open cards, no tab clicked) arms and deletes as well
  world();
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-1", title: "One", state: "in-progress" }] });
  const v = await mount();
  check(v.view === null, "no view picked");
  await press(v, "task-head-t-1");
  await press(v, "delete-t-1");
  check(armedText(v), "one click arms");
  await press(v, "delete-t-1");
  check(deletes().length === 1, `the second click deletes (got ${JSON.stringify(deletes())})`);
};

// ---- t-011: quiet hours "Set" with untouched fields; messages that say what is wrong ----
const SCHED = `:${DIR}/schedule.json`;
async function quietWorld(from = "", to = "") {
  world();
  g.__files[SCHED] = JSON.stringify({ everyMinutes: 15, quietHours: null });
  const v = await mount();
  await click(v, "sched-summary");
  v.schedule.quietFrom.set(from); v.schedule.quietTo.set(to);
  await click(v, "quiet-set"); await settle();
  return { v, quiet: JSON.parse(g.__files[SCHED]).quietHours, text: look(v).text, error: v.schedule.error };
}

cases["Q1-untouched-sets-shown"] = async () => { // V2.1: both fields untouched set the times they show
  const { quiet, error } = await quietWorld();
  check(!error, `no error (got ${error})`);
  check(JSON.stringify(quiet) === '{"from":"19:00","to":"08:00"}', `19:00 to 08:00 written (got ${JSON.stringify(quiet)})`);
};
cases["Q2-one-typed"] = async () => { // V2.2: one typed, the other untouched: the typed one and the shown default
  const a = await quietWorld("22:00", "");
  check(!a.error && JSON.stringify(a.quiet) === '{"from":"22:00","to":"08:00"}', `22:00 to 08:00 (got ${JSON.stringify(a.quiet)}, ${a.error})`);
  const b = await quietWorld("", "06:30");
  check(!b.error && JSON.stringify(b.quiet) === '{"from":"19:00","to":"06:30"}', `19:00 to 06:30 (got ${JSON.stringify(b.quiet)}, ${b.error})`);
};
cases["Q3-bad-start"] = async () => { // V2.3: the message names the field and the value; nothing written
  const { quiet, error, text } = await quietWorld("7pm", "");
  check(quiet === null, `nothing written (got ${JSON.stringify(quiet)})`);
  check(/start/i.test(error ?? "") && /7pm/.test(error ?? "") && /HH:MM/.test(error ?? ""), `names the start, "7pm" and the form (got ${error})`);
  check(text.includes(error), "the message is shown");
};
cases["Q4-bad-end"] = async () => { // V2.4
  const { quiet, error } = await quietWorld("", "25:00");
  check(quiet === null && /end/i.test(error ?? "") && /25:00/.test(error ?? "") && !/start/i.test(error ?? ""), `names the end and "25:00" (got ${error}, ${JSON.stringify(quiet)})`);
};
cases["Q5-same-time"] = async () => { // V2.5
  const { quiet, error } = await quietWorld("08:00", "");
  check(quiet === null && /same/i.test(error ?? "") && /08:00/.test(error ?? ""), `says both are 08:00 (got ${error}, ${JSON.stringify(quiet)})`);
};
cases["Q6-short-hour"] = async () => { // V2.6: 7:30 is a time, written as 07:30
  const { quiet, error } = await quietWorld("7:30", "21:15");
  check(!error && JSON.stringify(quiet) === '{"from":"07:30","to":"21:15"}', `07:30 to 21:15 (got ${JSON.stringify(quiet)}, ${error})`);
};

// ---- t-011: workers closed with pa-worker close leave the task card ----
const closedW = (address) => ({ address, role: "build", name: `w-${address}`, finished: true, closed: "2026-11-16T10:00:00Z", closedBy: "agent", report: "FINDINGS.md" });
cases["W1-closed-not-listed"] = async () => { // V3.1: closed ones go; working, blocked, kept and ended-not-closed stay, labels as before
  world({ extraTerms: [{ address: "t41", state: "live", argv: ["claude"], labels: {}, status: "working", program: claude },
                       { address: "t42", state: "live", argv: ["claude"], labels: {}, status: "blocked", program: claude },
                       { address: "t43", state: "live", argv: ["claude"], labels: {}, status: "idle", program: claude }] });
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-5", title: "T", state: "in-progress", workers: [
    closedW("t40"),
    { address: "t41", role: "build", name: "a" }, { address: "t42", role: "reviewer", name: "b" },
    { address: "t43", role: "verifier", name: "c", keep: true, keptAt: "2026-11-16T10:00:00Z" },
    { address: "t44", role: "build", name: "d" }, closedW("t45")] }] });
  const v = await mount();
  await click(v, "view-tasks"); await click(v, "task-head-t-5");
  const { text, clicks } = look(v);
  check(!/t40/.test(text) && !/t45/.test(text) && !/\(closed\)/.test(text), `closed workers not drawn (got ${text.slice(-500)})`);
  check(/build t41 ↗/.test(text) && /reviewer t42 ↗/.test(text) && /verifier t43 · kept ↗/.test(text) && /build t44 \(ended\)/.test(text), `the others as before (got ${text.slice(-500)})`);
  const chips = Object.keys(clicks).filter((k) => k.startsWith("worker-x-t-5-"));
  check(chips.length === 4, `four worker × (got ${chips})`);
};
cases["W2-all-closed-no-row"] = async () => { // V3.2, V3.3: every worker closed: no workers row at all; the ledger is not written
  world();
  const ledger = JSON.stringify({ tasks: [
    { id: "t-6", title: "All closed", state: "in-progress", updated: "2026-11-16T10:02:00Z", workers: [closedW("t50"), closedW("t51")] },
    { id: "t-7", title: "No workers", state: "in-progress", updated: "2026-11-16T10:01:00Z" }] });
  g.__files[`:${DIR}/ledger.json`] = ledger;
  const v = await mount();
  await click(v, "view-tasks"); await click(v, "task-head-t-6"); await click(v, "task-head-t-7");
  const tree = v.render(cx), body = (id) => pathTo(tree, `task-${id}`)?.at(-1)?.kids?.[1];
  const a = body("t-6"), b = body("t-7");
  check(a && b, "both tasks expanded");
  const kinds = (x) => x.kids.map((k) => (typeof k === "object" ? `${k.kind}${k.key ? `#${k.key}` : ""}:${k.kids.length}` : "text")).join(",").replace(/t-[67]/g, "t");
  check(kinds(a) === kinds(b), `the all-closed task's body is the no-workers body (got ${kinds(a)} vs ${kinds(b)})`);
  check(!Object.keys(look(v).clicks).some((k) => k.startsWith("worker-")), "no worker chip");
  check(g.__files[`:${DIR}/ledger.json`] === ledger && !(g.__writes ?? []).some((k) => k.endsWith("/ledger.json")), "ledger.json unchanged, never written");
};

// ---- t-011: the Rules view's Delete can be backed out of (as the task Delete) ----
const RULES = `:${DIR}/rules.json`;
const rule = (id, title) => ({ id, title, text: `${title} text`, status: "active", order: 10, source: "person" });
async function ruleWorld() {
  world();
  g.__files[RULES] = JSON.stringify({ rules: [rule("r-one", "One"), rule("r-two", "Two")] });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  const v = await mount(); await settle();
  await press(v, "view-rules");
  await press(v, "rule-head-r-one");
  return v;
}
const ruleArmed = (v) => look(v).text.includes("Delete for good?");
const ruleIds = () => JSON.parse(g.__files[RULES]).rules.map((r) => r.id).join(",");

cases["R1-two-clicks-delete"] = async () => { // one click arms, with a Cancel beside it; the second deletes the rule
  const v = await ruleWorld();
  check(!ruleArmed(v) && !look(v).clicks["rule-delete-cancel-r-one"], "unarmed, no Cancel");
  await press(v, "rule-delete-r-one");
  check(ruleArmed(v) && Boolean(look(v).clicks["rule-delete-cancel-r-one"]), `armed with a Cancel (have ${Object.keys(look(v).clicks)})`);
  check(ruleIds() === "r-one,r-two", "one click deletes nothing");
  await press(v, "rule-delete-r-one"); await settle();
  check(ruleIds() === "r-two", `the second click deletes r-one (got ${ruleIds()})`);
};
cases["R2-cancel"] = async () => {
  const v = await ruleWorld();
  await press(v, "rule-delete-r-one");
  await press(v, "rule-delete-cancel-r-one");
  check(!ruleArmed(v) && !look(v).clicks["rule-delete-cancel-r-one"], "Cancel disarms");
  await press(v, "rule-delete-r-one");
  check(ruleArmed(v) && ruleIds() === "r-one,r-two", "after Cancel one click only arms");
};
cases["R3-press-elsewhere"] = async () => { // empty space, another Rules control (they stop the press), the coffee button (t-145)
  const v = await ruleWorld();
  for (const where of [null, "pa-rules", "rule-export", "rule-edit-r-one", "rule-toggle-r-two", "awake-toggle"]) {
    await press(v, "rule-delete-r-one");
    check(ruleArmed(v), `armed before ${where}`);
    await press(v, where);
    if (v.rules.form) { v.rules.closeForm(); }
    check(!ruleArmed(v), `a press on ${where ?? "the panel"} disarms`);
  }
  await settle();
  check(ruleIds() === "r-one,r-two", `nothing deleted (got ${ruleIds()})`);
};
cases["R4-view-space-time"] = async () => { // a view switch, a space switch, 10 s
  const v = await ruleWorld();
  await click(v, "rule-delete-r-one");
  await click(v, "view-tasks"); await click(v, "view-rules");
  check(!ruleArmed(v), "view switched and back: not armed");
  await click(v, "rule-delete-r-one");
  const S2 = "sp2", D2 = "/home/u/.config/herdr-pa/spaces/sp2";
  g.__spaces.push({ id: S2, name: "other", machine: null, folder: "/other", active: false, tabs: [{ id: "tb2", index: 1, name: "1", named: false }] });
  g.__files[`:${D2}/agent.json`] = JSON.stringify({ space: S2, project: "/other", profile: "test", name: "other" });
  g.__files[`:${D2}/ledger.json`] = JSON.stringify({ tasks: [] });
  g.__files[`:${D2}/rules.json`] = JSON.stringify({ rules: [rule("r-one", "Other one")] });
  show(S2); v.reload(cx); await settle();
  show(S); v.reload(cx); await settle();
  check(/One text|One/.test(look(v).text) && !ruleArmed(v), "space switched and back: not armed");
  await click(v, "rule-delete-r-one");
  advance(9000); await fire(1000);
  check(ruleArmed(v), "still armed at 9 s");
  advance(1500); await fire(1000);
  check(!ruleArmed(v), "disarmed after 10 s");
  check(ruleIds() === "r-one,r-two", "nothing deleted");
};

// ---- t-011 fix round 2: every control that stops the mouse-down disarms an armed Delete ----
// down(): the mouse-down only (bubbling until stopped), no click: a press that is not released on the control.
async function down(v, id) {
  const route = pathTo(v.render(cx), id);
  check(Boolean(route), `an element ${id} to press`);
  let stopped = false;
  const ecx = { ...cx, stop_propagation() { stopped = true; } };
  for (const el of [...route].reverse()) {
    const h = el.style?.on_mouse_down;
    if (h && h[0] === "left") h[1]({}, ecx);
    if (stopped) break;
  }
  await flush();
}
async function crewWorld() {
  world();
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [
    { id: "t-1", title: "One", state: "in-progress", updated: "2026-11-16T10:02:00Z", workers: [{ address: "w1", role: "build", name: "a" }] },
    { id: "t-2", title: "Two", state: "in-progress", updated: "2026-11-16T10:01:00Z", workers: [{ address: "w2", role: "build", name: "b" }] }] });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  const v = await mount();
  await press(v, "view-tasks");
  await press(v, "task-head-t-1"); await press(v, "task-head-t-2");
  return v;
}
cases["X1-worker-x-while-armed"] = async () => { // REVIEW M1: a press on a worker × (same card) disarms; the dialog still opens
  const v = await crewWorld();
  await press(v, "delete-t-1");
  check(armedText(v), "armed");
  const before = (g.__overlays ?? []).length;
  await press(v, "worker-x-t-1-0");
  check(!armedText(v), "press on worker x disarms");
  check((g.__overlays ?? []).length === before + 1, "the delete-worker dialog still opens");
  await press(v, "delete-t-1");
  check(armedText(v) && deletes().length === 0, "after it, one click only arms again");
};
cases["X2-worker-x-down-only"] = async () => { // the mouse-down alone on a ×, this card's or another card's, disarms
  const v = await crewWorld();
  for (const where of ["worker-x-t-1-0", "worker-x-t-2-0"]) {
    await press(v, "delete-t-1");
    check(armedText(v), `armed before ${where}`);
    await down(v, where);
    check(!armedText(v), `a mouse-down on ${where} disarms`);
  }
  check(deletes().length === 0, "nothing deleted");
};
cases["X3-rules-controls-down-only"] = async () => { // the Rules view's stopping controls: a button, the On/Off toggle, the drag grip
  const v = await ruleWorld();
  for (const where of ["rule-export", "rule-edit-r-one", "rule-toggle-r-two", "rule-grip-r-two"]) {
    await press(v, "rule-delete-r-one");
    check(ruleArmed(v), `armed before ${where}`);
    await down(v, where);
    v.rules.pressed = null;
    check(!ruleArmed(v), `a mouse-down on ${where} disarms`);
  }
  await settle();
  check(ruleIds() === "r-one,r-two", `nothing deleted (got ${ruleIds()})`);
};
cases["X4-own-buttons-keep"] = async () => { // the armed Delete and its Cancel: a mouse-down on them does not disarm
  const v = await crewWorld();
  await press(v, "delete-t-1");
  await down(v, "delete-t-1"); await down(v, "delete-cancel-t-1");
  check(armedText(v) && deletes().length === 0, "task Delete: still armed, nothing deleted");
  const r = await ruleWorld();
  await press(r, "rule-delete-r-one");
  await down(r, "rule-delete-r-one"); await down(r, "rule-delete-cancel-r-one");
  check(ruleArmed(r), "rule Delete: still armed");
};

// ---- t-007b Job C: the push card (spec §8, plan §4.3, §4.7). pa-push-main and pa-push-allow are
// stand-ins (__execFn): the real tool never runs here. ----
const PID = "d-push-t-1-1", PKEY = `${S}\0${PID}`;
const PC = "cc11" + "0".repeat(36), PR = "aa22" + "0".repeat(36), PL = "bb33" + "0".repeat(36), PE = "ee44" + "0".repeat(36);
const PURL = "file:///tmp/pa-scratch/origin.git";
const pushCardText = (extra = {}) => JSON.stringify({ id: PID, task: "t-1", kind: "push", question: "Push repo main (2 commits)?",
  context: "CONTEXT FROM THE CARD FILE", options: [{ label: "Push", detail: "DETAIL FROM THE CARD FILE" }, { label: "Not now", detail: "x" }],
  recommendation: "Push", status: "open", answer: null, note: null, created: "2026-11-16T10:00:00Z", answered: null,
  push: { version: 1, space: S, task: "t-1", path: "/repo", url: PURL, branch: "main", taskBranch: "t-1-x", commit: PC, localBefore: PL, remoteBefore: PR, merge: true,
    commits: [], allowSha: "a".repeat(64), allowMtime: "2026-11-16T08:00:00Z", gateAt: "2026-11-16T09:00:00Z", done: false, result: null, attempts: [] }, ...extra });
const PCMDS = [["git", "-C", "/repo", "-c", "core.hooksPath=<an empty folder>", "-c", "core.fsmonitor=false", "merge", "--ff-only", "--no-edit", PC],
  ["git", "-C", "/repo", "-c", "core.hooksPath=<an empty folder>", "-c", "core.fsmonitor=false", "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", PURL, `${PC}:refs/heads/main`]];
const checkReply = (over = {}, facts = {}) => ({ card: PID, ok: true, refusal: null, caller: { ok: true, why: null, ancestors: ["herdr"] }, status: "open", done: false, result: null,
  facts: { path: "/repo", url: PURL, branch: "main", originUrl: PURL, remoteNow: PR, localNow: PL, commit: PC, taskBranch: "t-1-x", branchTip: PC, merge: true,
    gate: { at: "2026-11-16T09:00:00Z", phase: "review", passed: true, commit: PC, uncommitted: 0 },
    commits: [{ sha: PC, subject: "the task's change", author: "A <a@x>", fromTask: true }, { sha: PE, subject: "an earlier local merge", author: "B <b@x>", fromTask: false }], count: 2,
    allow: { file: "/home/u/.config/herdr-pa/push-allow.json", sha: "a".repeat(64), mtime: "2026-11-16T08:00:00Z", changedSinceCard: false, entry: { path: "/repo", url: PURL, branch: "main", space: S, enabled: true } },
    commands: PCMDS, checkedAt: "2026-11-16T10:00:00Z", ...facts }, seen: "seen-token-1", ...over });
const runPushed = { ok: true, done: true, answer: "Push", refusal: null, message: `pushed ${PURL} main aa22000..cc11000 (2 commits)`,
  result: { at: "2026-11-16T10:01:00Z", from: PR, to: PC, url: PURL, branch: "main", merged: true, output: ["To file:///tmp/pa-scratch/origin.git", "Done"] } };
const json = (code, value) => ({ code, stdout: JSON.stringify(value), stderr: "" });
// The tool's stand-in: check, run and show answer from these (each may return a promise).
function pushTool({ check: onCheck = () => json(0, checkReply()), run: onRun = () => json(0, runPushed), show = () => json(0, { exists: false, valid: true, problem: null, enabled: false, repos: [], file: "/home/u/.config/herdr-pa/push-allow.json", state: "off (the default): no push allowlist" }) } = {}) {
  g.__execFn = (command, args) => {
    if (command === "pa-push-main" && args[0] === "check") return onCheck(args);
    if (command === "pa-push-main" && args[0] === "run") return onRun(args);
    if (command === "pa-push-allow" && args[0] === "show") return show(args);
    if (/^pa-push|pa_push/.test(command)) throw new Error(`unexpected ${command} ${args.join(" ")}`);
    return undefined;
  };
}
const pushExecs = (sub) => g.__execs.filter(([c, a]) => c === "pa-push-main" && (!sub || a[0] === sub));
const allExecsOfTool = () => g.__execs.filter(([c]) => /pa-push|pa_push/.test(c));
function pushFiles({ machine = null, card = pushCardText() } = {}) {
  const dir = machine ? `${machine}:${RDIR}` : `:${DIR}`;
  g.__files[`${dir}/decisions/${PID}.json`] = card; g.__ls[`${dir}/decisions`] = [`${PID}.json`];
  g.__files[`${dir}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-1", title: "One", state: "review", updated: "2026-11-16T10:02:00Z" }] });
  g.__files[`${dir}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  return `${dir}/decisions/${PID}.json`;
}
async function pushWorld({ machine = null, tool = {}, card, opts = {}, layout = null, props = null } = {}) {
  world({ machine, ...opts });
  const file = pushFiles({ machine, card });
  pushTool(tool);
  if (layout) g.__layout = layout;
  if (props) g.__props = props;
  const v = await mount(); await settle();
  return { v, file };
}
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };

// ---- t-140 Part A (R4): a queue card that pushes is answered only through the push tool, like a push card ----
const QID = "d-queue-1", QLABEL = "Merge all in order and push";
const queueCardText = (push = true, extra = {}) => JSON.stringify({ id: QID, task: "t-1", kind: "queue",
  question: "Merge 1 queued change into main of repo, in order, and push main?", context: "CONTEXT FROM THE CARD FILE",
  options: [{ label: push ? QLABEL : "Merge all in order", detail: "Merges t-1 into local main", count: 1 }, { label: "Not now", detail: "Nothing merges or is pushed.", count: 0 }],
  recommendation: push ? QLABEL : "Merge all in order", status: "open", answer: null, note: null, created: "2026-11-16T10:00:00Z", answered: null,
  queue: { version: 1, repo: "/repo", common: "/repo/.git", mainBefore: PR, entries: [{ task: "t-1", job: null, tip: PC }],
    ...(push ? { push: { version: 1, path: "/repo", url: PURL, branch: "main" } } : {}) }, ...extra });
const queueCheck = (over = {}) => ({ card: QID, kind: "queue", ok: true, refusal: null, caller: { ok: true, why: null, ancestors: ["herdr"] }, status: "open", done: false, result: null,
  facts: { path: "/repo", url: PURL, branch: "main", localNow: PR, remoteNow: PR, checkedAt: "2026-11-16T10:00:00Z",
    allow: { file: "/home/u/.config/herdr-pa/push-allow.json", sha: "a".repeat(64), mtime: "2026-11-16T08:00:00Z", entry: { path: "/repo", url: PURL, branch: "main", space: S, enabled: true } } },
  options: [{ label: QLABEL, count: 1, entries: [{ task: "t-1", job: null, tip: PC }], mergeNow: ["t-1"], waits: null, extra: [],
    push: { ok: true, refusal: null, commit: PC, count: 1, commits: [{ sha: PC, subject: "the task's change", author: "A <a@x>" }] } }],
  seen: "queue-seen-1", ...over });
async function qpushWorld({ card = queueCardText(), tool = {} } = {}) {
  world();
  const file = `:${DIR}/decisions/${QID}.json`;
  g.__files[file] = card; g.__ls[`:${DIR}/decisions`] = [`${QID}.json`];
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-1", title: "One", state: "review", updated: "2026-11-16T10:02:00Z" }] });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  pushTool({ check: () => json(0, queueCheck()), ...tool });
  const v = await mount(); await settle();
  return { v, file };
}

cases["P-qpush-run"] = async () => { // A4.1: Submit of a merge option runs the push tool once with the card's own label; never answerDecision
  let file;
  const { v, file: f } = await qpushWorld({ tool: { run: () => { g.__files[file] = queueCardText(true, { status: "answered", answer: QLABEL, answered: "2026-11-16T10:01:00Z", answeredBy: "person, in the panel" });
    return json(0, { ok: true, done: true, answer: QLABEL, refusal: null, message: `d-queue-1 answered ${QLABEL}: merged t-1 (main aa22000..cc11000); pushed ${PURL} main aa22000..cc11000`,
      result: { at: "2026-11-16T10:01:00Z", state: "pushed", from: PR, to: PC, commits: 1, merged: ["t-1"], check: null, message: "pushed" } }); } } });
  file = f;
  const checks = pushExecs("check");
  check(checks.length === 1 && JSON.stringify(checks[0][1]) === JSON.stringify(["check", QID, "--space", S, "--json"]), `one check of the queue card (got ${JSON.stringify(checks.map((x) => x[1]))})`);
  let { text } = look(v);
  for (const want of ["Read now by pa-push-main check (not from the card file)", `Pushes to: ${PURL}`, QLABEL, "Merges now: t-1", "Commits to push (1):", "cc11000 the task's change"])
    check(text.includes(want), `shows "${want}" (got ${text.slice(0, 1500)})`);
  check(!text.includes("CONTEXT FROM THE CARD FILE"), "the card file's context is not shown");
  const writes = (g.__writes ?? []).length;
  await click(v, `opt-${QID}-0`); await settle();
  check(pushExecs("check").length === 2, `picking the merge option checks again (got ${pushExecs("check").length})`);
  await click(v, `submit-${QID}`); await settle(); await settle();
  const runs = pushExecs("run");
  check(runs.length === 1, `one run (got ${runs.length})`);
  check(JSON.stringify(runs[0][1]) === JSON.stringify(["run", QID, "--answer", QLABEL, "--space", S, "--seen", "queue-seen-1", "--json"]), `its argv (got ${JSON.stringify(runs[0][1])})`);
  check(!(g.__writes ?? []).slice(writes).some((k) => String(k).includes("/decisions/")), "the panel wrote no card file (never answerDecision)");
  text = look(v).text;
  check(text.includes(`answered: ${QLABEL}`), `the card says answered (got ${text.slice(0, 1200)})`);
};

cases["P-qpush-check-failed"] = async () => { // A4.2: no passed check: a merge option cannot be submitted, and says why; Not now and Other can
  const { v } = await qpushWorld({ tool: { check: () => { throw new Error("no such tool"); } } });
  await click(v, `opt-${QID}-0`); await settle();
  let { text, clicks } = look(v);
  check(!clicks[`submit-${QID}`], `Submit is off for the merge option (got ${Object.keys(clicks)})`);
  check(text.includes("pa-push-main check could not run"), `says why (got ${text.slice(0, 1500)})`);
  await click(v, `opt-${QID}-1`); await settle();
  ({ clicks } = look(v));
  check(Boolean(clicks[`submit-${QID}`]), "Not now can be submitted");
  await click(v, `submit-${QID}`); await settle();
  const runs = pushExecs("run");
  check(runs.length === 1 && JSON.stringify(runs[0][1]) === JSON.stringify(["run", QID, "--answer", "Not now", "--space", S, "--json"]), `Not now runs the tool, without --seen (got ${JSON.stringify(runs.map((x) => x[1]))})`);
};

cases["P-qpush-plain-queue-card"] = async () => { // A4.3: a queue card without queue.push is answered as today
  const { v, file } = await qpushWorld({ card: queueCardText(false) });
  check(pushExecs().length === 0, `no check (got ${JSON.stringify(pushExecs())})`);
  check(look(v).text.includes("CONTEXT FROM THE CARD FILE"), "its own context is shown");
  await click(v, `opt-${QID}-0`); await click(v, `submit-${QID}`); await settle();
  check(pushExecs().length === 0, "no run");
  check(JSON.parse(g.__files[file]).status === "answered", "answerDecision wrote the answer");
};

cases["P-push-facts"] = async () => { // §8.1: the block shows the facts of the last check, not the card file's own text
  const { v } = await pushWorld();
  const { text } = look(v);
  for (const want of ["Read now by pa-push-main check (not from the card file)", "Repository: /repo", `Pushes to: ${PURL}, on your allowlist (entry on`,
    "Remote main now: aa22000", "Local main: bb33000", "Commits (2):", "cc11000 the task's change — A <a@x>",
    "ee44000 an earlier local merge — B <b@x> · already on local main, not from this task", "Gated commit: cc11000, full gate passed at", "task t-1, branch t-1-x",
    "What will run:", `git -C /repo -c core.hooksPath=<an empty folder> -c core.fsmonitor=false push --porcelain --no-follow-tags --recurse-submodules=no ${PURL} ${PC}:refs/heads/main`,
    "No force, no tags, no other branches.", "This panel may push: yes", "Check again"]) check(text.includes(want), `shows "${want}" (got ${text.slice(0, 1500)})`);
  check(!text.includes("CONTEXT FROM THE CARD FILE") && !text.includes("DETAIL FROM THE CARD FILE"), "the card file's context and option details are not shown");
  check(text.includes("Push") && text.includes("Not now"), "the two options");
};

cases["P-push-facts-warnings"] = async () => { // §8.1: red lines for a changed allowlist, another origin URL, more than 20 commits
  const many = Array.from({ length: 23 }, (_, i) => ({ sha: `${String(i).padStart(4, "0")}${"0".repeat(36)}`, subject: `s${i}`, author: "A <a@x>", fromTask: true }));
  const { v } = await pushWorld({ tool: { check: () => json(0, checkReply({}, { originUrl: "git@other:x.git", commits: many, count: 23,
    allow: { ...checkReply().facts.allow, changedSinceCard: true } })) } });
  const { text } = look(v);
  check(/The allowlist changed at .*, after this card was made/.test(text), `allowlist changed line (got ${text.slice(0, 1200)})`);
  check(text.includes("origin's URL is git@other:x.git, not the allowlist's URL"), "origin URL line");
  check(text.includes("Commits (23):") && text.includes("… and 3 more") && !text.includes(" s20 "), "20 commits, then … and 3 more");
};

cases["P-push-check-on-first-show"] = async () => { // AC6.1: one check after the first load commits; none from render or later reloads
  const { v } = await pushWorld();
  let checks = pushExecs("check");
  check(checks.length === 1, `one check (got ${JSON.stringify(checks.map((x) => x[1]))})`);
  check(JSON.stringify(checks[0][1]) === JSON.stringify(["check", PID, "--space", S, "--json"]) && checks[0][2] === null && checks[0][3]?.timeout === 60000, `its argv, on and timeout (got ${JSON.stringify(checks[0])})`);
  for (let i = 0; i < 5; i++) look(v);
  await settle();
  for (let i = 0; i < 6; i++) { advance(10000); await fire(10000); }
  checks = pushExecs("check");
  check(checks.length === 1, `still one check after renders and 6 reloads (got ${checks.length})`);
  check(pushExecs("run").length === 0, "no run");
};

cases["P-push-pick-checks"] = async () => { // AC6.1: picking Push checks again; so does Check again; Not now does not
  const { v } = await pushWorld();
  await click(v, `opt-${PID}-0`); await settle();
  check(pushExecs("check").length === 2, `picking Push checks (got ${pushExecs("check").length})`);
  await click(v, `opt-${PID}-1`); await settle();
  check(pushExecs("check").length === 2, "picking Not now does not");
  await click(v, `push-check-${PID}`); await settle();
  check(pushExecs("check").length === 3, "Check again does");
  check(pushExecs("run").length === 0, "no run");
};

// The card as pa-push-main run writes it on a push (plan §4.7, Job A): answered, with push.done and push.result.
// Its reply prints the same result it wrote (toolPushed).
const toolResult = { ...runPushed.result, how: "pushed", output: "To file:///tmp/pa-scratch/origin.git\n   aa22000..cc11000  cc11 -> main" };
const toolPushed = { ...runPushed, result: toolResult };
const answeredByTool = (over = {}) => { const base = JSON.parse(pushCardText()); return JSON.stringify({ ...base, status: "answered", answer: "Push", note: null,
  answered: "2026-11-16T10:01:00Z", answeredBy: "person, in the panel", push: { ...base.push, done: true, result: toolResult }, ...over }); };

cases["P-push-run-once"] = async () => { // AC6.2, AC5.3: Submit Push runs pa-push-main run once per click; the card file is written by the tool only
  const gate = deferred();
  let file;
  // As the real tool does: the run writes the card answered, then prints its JSON.
  const { v, file: f } = await pushWorld({ tool: { run: () => gate.promise.then((reply) => { g.__files[file] = answeredByTool(); return reply; }) } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const { text, clicks } = look(v);
  check(text.includes("Pushing…") && !clicks[`submit-${PID}`], `Pushing…, no second Submit while it runs (got ${Object.keys(clicks)})`);
  const runs = pushExecs("run");
  check(runs.length === 1, `one run (got ${runs.length})`);
  check(JSON.stringify(runs[0][1]) === JSON.stringify(["run", PID, "--answer", "Push", "--space", S, "--seen", "seen-token-1", "--json"]), `its argv (got ${JSON.stringify(runs[0][1])})`);
  check(runs[0][2] === null && runs[0][3]?.timeout === 600000, `on null, timeout 600 s (got ${JSON.stringify(runs[0][3])})`);
  const writes = (g.__writes ?? []).length, checks = pushExecs("check").length;
  gate.resolve(json(0, toolPushed)); await settle(); await settle();
  check(pushExecs("run").length === 1, "still one run");
  check(!(g.__writes ?? []).slice(writes).some((k) => String(k).includes("/decisions/")), "the panel wrote no card file");
  // The panel's own reload has read the card answered: the result and the output tail stay on screen (spec §8.2, AC6.2).
  check(v.cards.some((c) => c.id === PID && c.card?.status === "answered"), "the reload read the card answered");
  const after = look(v);
  check(v.viewShown === "decisions" && after.text.includes(PID), `still the Decisions view with the card (view ${v.viewShown}, got ${after.text.slice(0, 600)})`);
  check(after.text.includes(`Pushed ${PURL} main aa22000..cc11000`) && after.text.includes("cc11 -> main"), `the result and the output tail after the reload (got ${after.text.slice(0, 1500)})`);
  check(after.text.includes("answered: Push"), "the card says answered");
  // Answered now: no option to pick, no second Push, no new check.
  check(!after.clicks[`opt-${PID}-0`] && !after.clicks[`submit-${PID}`] && pushExecs("run").length === 1, "pushed: no option, no Submit");
  for (let i = 0; i < 3; i++) { advance(10000); await fire(10000); }
  check(pushExecs("check").length === checks, `no check for the answered card (got ${pushExecs("check").length - checks} more)`);
  check(look(v).text.includes(`Pushed ${PURL} main aa22000..cc11000`), "still shown after more reloads");
};

cases["P-push-result-kept-until-left"] = async () => { // §8.2: the pushed card's result stays until the person leaves it (another view, or Done)
  let file;
  const { v, file: f } = await pushWorld({ tool: { run: () => { g.__files[file] = answeredByTool(); return json(0, toolPushed); } } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle(); await settle();
  check(look(v).text.includes("cc11 -> main"), `shown after the reload (got ${look(v).text.slice(0, 1200)})`);
  // While kept the card is not also in the answered fold (VERIFY-7b-C-recheck L1, the answered filter).
  let t = look(v).text;
  check(!t.includes("1 answered") && t.split("cc11 -> main").length === 2, `kept: not in the answered fold, the tail drawn once (got ${t.slice(0, 1200)})`);
  await click(v, "view-tasks"); await click(v, "view-decisions");
  t = look(v).text;
  check(!t.includes("cc11 -> main") && t.includes("No open decisions") && t.includes("1 answered"), `left: no longer kept, the card is with the answered ones (got ${t.slice(0, 1200)})`);
  // The same with its Done link.
  const { v: w, file: f2 } = await pushWorld({ tool: { run: () => { g.__files[file] = answeredByTool(); return json(0, toolPushed); } } });
  file = f2;
  await click(w, `opt-${PID}-0`); await settle();
  await click(w, `submit-${PID}`); await settle(); await settle();
  check(look(w).text.includes("cc11 -> main"), "shown after the reload");
  await click(w, `push-done-${PID}`);
  t = look(w).text;
  check(!t.includes("cc11 -> main") && t.includes("1 answered"), `Done: no longer kept (got ${t.slice(0, 1200)})`);
};

cases["P-push-click-stale"] = async () => { // C-D5: Submit drawn while the check was fresh, clicked after 120 s with no redraw: no run
  const { v } = await pushWorld();
  await click(v, `opt-${PID}-0`); await settle();
  const { clicks } = look(v);
  check(Boolean(clicks[`submit-${PID}`]), "Submit drawn while fresh");
  advance(121000); // no render between
  clicks[`submit-${PID}`]({}, cx); await settle();
  check(pushExecs("run").length === 0, `no run (got ${JSON.stringify(pushExecs("run").map((x) => x[1]))})`);
  check(look(v).text.includes("Push needs a check that passed, under 2 minutes old: Check again."), `says Check again (got ${look(v).text.slice(0, 1500)})`);
};

cases["P-push-run-crash"] = async () => { // a run that exits non-zero with no JSON from the tool (it crashed, maybe after the push): result unknown, one check
  // Also JSON that is not the tool's refusal (VERIFY-7b-C-recheck L1, the refusal shape): only ok:false is a refusal.
  for (const [code, stderr, stdout = ""] of [[1, "Traceback (most recent call last):\n  File \"pa_push.py\", line 900, in finish\nOSError: [Errno 28] No space left on device"], [2, ""],
    [1, "", JSON.stringify({ ok: true, done: false, answer: "Push", refusal: null, result: null, message: "odd" })],
    [1, "", JSON.stringify({ done: false, answer: "Push", refusal: { check: "C9", message: "no ok field" }, message: "no ok field" })]]) {
    const { v } = await pushWorld({ opts: { records: alive() }, tool: { run: () => ({ code, stdout, stderr }) } });
    await click(v, `opt-${PID}-0`); await settle();
    const checks = pushExecs("check").length;
    await click(v, `submit-${PID}`); await settle();
    const { text } = look(v);
    check(/The result is unknown \([\s\S]*\); checking the card again\./.test(text) && !text.includes("Not pushed"), `exit ${code}: result unknown, not "Not pushed" (got ${text.slice(0, 1500)})`);
    check(pushExecs("check").length === checks + 1, `exit ${code}: one check after it (got ${pushExecs("check").length - checks})`);
    check(pushExecs("run").length === 1, `exit ${code}: one run`);
    const said = sends().map((x) => x[2]);
    check(said.length === 1 && said[0].startsWith(`decision ${PID} answered Push; result unknown (`) && !said[0].includes("refused"), `exit ${code}: the orchestrator is told the result is unknown (got ${JSON.stringify(said)})`);
  }
};

// The tool's check of a card it has already pushed (check_card: exit 1, C4, done true, the recorded result).
const C4_DONE = `${PID} was already pushed at 2026-11-16T10:01:00Z (aa22000..cc11000); nothing to do`;
const checkDone = () => json(1, checkReply({ ok: false, refusal: { check: "C4", message: C4_DONE }, status: "answered", done: true, result: toolResult, seen: null }, { commands: [] }));

cases["P-push-run-crash-after-save"] = async () => { // REVIEW-7b-C-recheck M1: the tool saved the card answered, then crashed (no JSON): result unknown and the check's verdict stay shown
  let file, ran = false;
  const crash = "Traceback (most recent call last):\n  File \"pa_push.py\", line 1174, in finish\nOSError: [Errno 28] No space left on device";
  // As finish() can: save_card writes the card answered and done, then append_journal raises: exit 1, no JSON.
  const { v, file: f } = await pushWorld({ opts: { records: alive() }, tool: {
    check: () => (ran ? checkDone() : json(0, checkReply())),
    run: () => { ran = true; g.__files[file] = answeredByTool(); return { code: 1, stdout: "", stderr: crash }; } } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  const checks = pushExecs("check").length;
  await click(v, `submit-${PID}`); await settle(); await settle();
  check(v.cards.some((c) => c.id === PID && c.card?.status === "answered"), "the reload read the card answered");
  let { text, clicks } = look(v);
  check(v.viewShown === "decisions" && text.includes(PID), `still the Decisions view with the card (view ${v.viewShown}, got ${text.slice(0, 800)})`);
  const unknownAt = text.search(/The result is unknown \([\s\S]*No space left on device[\s\S]*\); checking the card again\./), foundAt = text.indexOf(C4_DONE);
  check(unknownAt >= 0, `the result unknown line (got ${text.slice(0, 1500)})`);
  check(foundAt > unknownAt, `then the check's verdict (got ${text.slice(0, 1500)})`);
  check(!text.includes("Not pushed") && !text.includes("Pushed "), "neither Not pushed nor Pushed");
  check(pushExecs("check").length === checks + 1 && pushExecs("run").length === 1, `one check after it, one run (got ${pushExecs("check").length - checks} checks)`);
  check(Boolean(clicks[`push-done-${PID}`]) && !clicks[`opt-${PID}-0`] && !clicks[`submit-${PID}`], "Done drawn; no option, no Submit");
  const said = sends().map((x) => x[2]);
  check(said.length === 1 && said[0].startsWith(`decision ${PID} answered Push; result unknown (`), `the orchestrator is told the result is unknown (got ${JSON.stringify(said)})`);
  for (let i = 0; i < 3; i++) { advance(10000); await fire(10000); }
  text = look(v).text;
  check(text.includes(C4_DONE) && /The result is unknown/.test(text), "still shown after more reloads");
  check(pushExecs("check").length === checks + 1 && pushExecs("run").length === 1, "no more checks or runs");
  await click(v, "view-tasks"); await click(v, "view-decisions");
  text = look(v).text;
  check(!/The result is unknown/.test(text) && text.includes("No open decisions") && text.includes("1 answered"), `left: no longer kept (got ${text.slice(0, 1200)})`);
  // The same, left with Done.
  ran = false;
  const { v: w, file: f2 } = await pushWorld({ tool: {
    check: () => (ran ? checkDone() : json(0, checkReply())),
    run: () => { ran = true; g.__files[file] = answeredByTool(); return { code: 1, stdout: "", stderr: crash }; } } });
  file = f2;
  await click(w, `opt-${PID}-0`); await settle();
  await click(w, `submit-${PID}`); await settle(); await settle();
  check(look(w).text.includes(C4_DONE), `shown (got ${look(w).text.slice(0, 1200)})`);
  await click(w, `push-done-${PID}`);
  text = look(w).text;
  check(!text.includes(C4_DONE) && text.includes("1 answered"), `Done: no longer kept (got ${text.slice(0, 1200)})`);
};

cases["P-push-run-exit0-unknown"] = async () => { // REVIEW-7b-C-recheck RL-1: exit 0 without JSON that says the push happened is result unknown, one check, never Pushed
  const shapes = [["no stdout", ""], ["broken JSON", "{\"ok\": tr"], ["a JSON array", "[1]"],
    ["ok false", JSON.stringify({ ok: false, done: false, answer: "Push", refusal: null, result: null, message: "" })],
    ["ok, not done", JSON.stringify({ ok: true, done: false, answer: "Push", refusal: null, result: null, message: "" })],
    ["done, no result", JSON.stringify({ ok: true, done: true, answer: "Push", refusal: null, result: null, message: "" })]];
  for (const [name, stdout] of shapes) {
    const { v } = await pushWorld({ opts: { records: alive() }, tool: { run: () => ({ code: 0, stdout, stderr: "" }) } });
    await click(v, `opt-${PID}-0`); await settle();
    const checks = pushExecs("check").length;
    await click(v, `submit-${PID}`); await settle();
    const { text } = look(v);
    check(/The result is unknown \([\s\S]*\); checking the card again\./.test(text) && !text.includes("Pushed ") && !text.includes("Not pushed"), `${name}: result unknown, never Pushed (got ${text.slice(0, 1500)})`);
    check(pushExecs("check").length === checks + 1 && pushExecs("run").length === 1, `${name}: one check after it, one run (got ${pushExecs("check").length - checks})`);
    const said = sends().map((x) => x[2]);
    check(said.length === 1 && said[0].startsWith(`decision ${PID} answered Push; result unknown (`) && !said[0].includes("result: pushed"), `${name}: told result unknown (got ${JSON.stringify(said)})`);
  }
};

cases["P-push-note-passed"] = async () => { // the note goes with --note
  const { v } = await pushWorld();
  v.notes[PKEY].set("  ship it  ");
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const a = pushExecs("run")[0]?.[1] ?? [];
  check(JSON.stringify(a.slice(-2)) === JSON.stringify(["--note", "ship it"]), `--note ship it (got ${JSON.stringify(a)})`);
};

cases["P-push-refused"] = async () => { // §8.2: a refusal is shown on the card, which stays open; a fresh check follows
  const msg = "remote main moved since the card (was aa22000, now dd55000); ask for a new card";
  const { v } = await pushWorld({ tool: { run: () => json(1, { ok: false, done: false, answer: "Push", refusal: { check: "C11b", message: msg }, result: null, message: msg }) } });
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const { text } = look(v);
  check(text.includes(`Not pushed: ${msg}`), `the refusal (got ${text.slice(0, 1500)})`);
  check(text.includes("open") && !text.includes("answered: Push"), "the card stays open");
  check(pushExecs("check").length === 3, `a fresh check after the refusal (got ${pushExecs("check").length})`);
  // The tool's C0 refusal, as its JSON (with --json every refusal prints one): its message is shown.
  const c0 = "pa-push-main run is the person's command (HC_TERMINAL_ID is set)";
  const { v: w } = await pushWorld({ tool: { run: () => json(1, { ok: false, done: false, answer: "Push", refusal: { check: "C0", message: c0 }, result: null, message: c0 }) } });
  await click(w, `opt-${PID}-0`); await settle();
  await click(w, `submit-${PID}`); await settle();
  check(look(w).text.includes(`Not pushed: ${c0}`), `C0 shown (got ${look(w).text.slice(0, 1500)})`);
};

cases["P-push-notnow"] = async () => { // AC6.3: Not now runs run --answer "Not now" (no --seen), even when the check refused
  const { v } = await pushWorld({ tool: { check: () => json(1, checkReply({ ok: false, seen: null, refusal: { check: "C6a", message: "no push allowlist (x); main-branch pushes are off" } })),
    run: () => json(0, { ok: true, done: false, answer: "Not now", refusal: null, result: null, message: "answered Not now" }) } });
  await click(v, `opt-${PID}-1`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const runs = pushExecs("run");
  check(runs.length === 1 && JSON.stringify(runs[0][1]) === JSON.stringify(["run", PID, "--answer", "Not now", "--space", S, "--json"]), `run --answer "Not now" (got ${JSON.stringify(runs.map((r) => r[1]))})`);
};

cases["P-push-disabled"] = async () => { // AC6.5: Push is disabled after a failed check, an old check, a refused caller, a done card, no seen token
  const variants = {
    refused: { check: () => json(1, checkReply({ ok: false, seen: null, refusal: { check: "C6a", message: "no push allowlist (x); main-branch pushes are off" } })), says: "Push is not possible now (C6a): no push allowlist (x); main-branch pushes are off" },
    caller: { check: () => json(0, checkReply({ caller: { ok: false, why: "HC_TERMINAL_ID is set", ancestors: ["zsh"] } })), says: "This panel may push: no (HC_TERMINAL_ID is set)" },
    done: { check: () => json(1, checkReply({ ok: false, done: true, refusal: { check: "C4", message: `${PID} was already pushed` } })), says: `${PID} was already pushed` },
    noseen: { check: () => json(0, checkReply({ seen: null })), says: "Check again" },
    bad: { check: () => ({ code: 0, stdout: "not json", stderr: "" }), says: "pa-push-main check printed no result" },
  };
  for (const [name, x] of Object.entries(variants)) {
    const { v } = await pushWorld({ tool: { check: x.check } });
    await click(v, `opt-${PID}-0`); await settle();
    const { text, clicks } = look(v);
    check(!clicks[`submit-${PID}`], `${name}: no Submit (have ${Object.keys(clicks)})`);
    check(text.includes(x.says), `${name}: says "${x.says}" (got ${text.slice(0, 1500)})`);
  }
  const { v } = await pushWorld();
  await click(v, `opt-${PID}-0`); await settle();
  check(Boolean(look(v).clicks[`submit-${PID}`]), "fresh ok check: Submit");
  advance(119000);
  check(Boolean(look(v).clicks[`submit-${PID}`]), "at 119 s: Submit");
  advance(2000);
  const { text, clicks } = look(v);
  check(!clicks[`submit-${PID}`] && text.includes("This check is over 2 minutes old: Check again before pushing."), `over 120 s: disabled (got ${text.slice(0, 1500)})`);
  const slow = deferred();
  const { v: w } = await pushWorld({ tool: { check: () => slow.promise } });
  await click(w, `opt-${PID}-0`); await settle();
  check(look(w).text.includes("Checking…") && !look(w).clicks[`submit-${PID}`], "Checking… while the check runs, no Submit");
  slow.resolve(json(0, checkReply())); await settle();
  check(Boolean(look(w).clicks[`submit-${PID}`]), "then Submit");
};

cases["P-push-timer-copy"] = async () => { // AC6.4: the timer copy (props.timer) never runs pa-push-main
  const { v } = await pushWorld({ props: { timer: true } });
  for (let i = 0; i < 10; i++) { advance(15000); for (const t of [...g.__timers]) t.fn(cx); await settle(); v.render(cx); }
  check(allExecsOfTool().length === 0, `no pa-push-* exec (got ${JSON.stringify(allExecsOfTool().map((x) => x[1]))})`);
};

cases["P-push-hidden"] = async () => { // AC6.4: a hidden copy reloading 10 times runs no pa-push-main of any kind
  const { v } = await pushWorld({ layout: panelLayout("changes") });
  for (let i = 0; i < 10; i++) { advance(10000); await fire(10000); await fire(1000); v.resend(cx); await settle(); }
  check(g.__issued.some((x) => x.kind === "batch" || x.kind === "ls"), "the hidden copy did read the cards");
  check(allExecsOfTool().length === 0, `no pa-push-* exec (got ${JSON.stringify(allExecsOfTool().map((x) => x[1]))})`);
  // Shown first (its card loaded and checked, Push picked), then hidden: the hidden reloads add nothing.
  const { v: w } = await pushWorld({ layout: panelLayout("project-agent") });
  await click(w, `opt-${PID}-0`); await settle();
  const before = allExecsOfTool().length;
  check(before === 2 && w.cards.length === 1, `shown: two checks, the card loaded (got ${before}, ${w.cards.length} cards)`);
  g.__layout = panelLayout("changes");
  for (let i = 0; i < 10; i++) { advance(10000); await fire(10000); await fire(1000); w.resend(cx); await settle(); }
  check(w.asleep && allExecsOfTool().length === before, `hidden: no new pa-push-* exec (got ${JSON.stringify(allExecsOfTool().slice(before).map((x) => x[1]))})`);
};

cases["P-push-reloads"] = async () => { // AC6.4: a shown copy with Push picked, 10 reloads and resends: no run
  const { v } = await pushWorld({ opts: { records: alive() } });
  await click(v, `opt-${PID}-0`); await settle();
  for (let i = 0; i < 10; i++) { advance(10000); await fire(10000); await fire(15000); await fire(1000); v.resend(cx); await settle(); }
  check(pushExecs("run").length === 0, `no run (got ${pushExecs("run").length})`);
};

cases["P-push-remote-on"] = async () => { // §7.6: on a remote space every exec runs on its machine, with the local space id
  const { v } = await pushWorld({ machine: M });
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  await click(v, "view-rules"); await settle();
  const mine = allExecsOfTool();
  check(mine.length >= 4 && mine.every(([, a, on]) => on === M), `every exec on ${M} (got ${JSON.stringify(mine.map((x) => [x[1][0], x[2]]))})`);
  check(mine.every(([, a]) => a[a.indexOf("--space") + 1] === S), "--space is the local id");
};

cases["P-push-exec-timeout"] = async () => { // D8: a run that never settles: "result unknown" after 600 s, then one check
  const { v } = await pushWorld({ tool: { run: () => new Promise(() => {}) } });
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const checks = pushExecs("check").length;
  await step(590000);
  check(look(v).text.includes("Pushing…"), "still running at 590 s");
  await step(20000); await settle();
  const { text } = look(v);
  check(/The result is unknown \(.*\); checking the card again\./.test(text), `result unknown (got ${text.slice(0, 1500)})`);
  check(pushExecs("check").length === checks + 1, `one check after it (got ${pushExecs("check").length - checks})`);
  check(pushExecs("run").length === 1, "one run");
};

cases["P-push-lastknown"] = async () => { // D7: a card shown from its last known copy still checks and runs on a click; reloads run nothing
  const { v } = await pushWorld();
  g.__execFail = [":batch", ":ls"]; // the card listing stops answering: the last known card stays shown (t-053: the batch runs here too)
  for (let i = 0; i < 3; i++) { advance(10000); await fire(10000); }
  check(look(v).text.includes("the last known are shown"), `shown from the last known copy (got ${look(v).text.slice(0, 600)})`);
  check(pushExecs("run").length === 0, "reloads ran no run");
  await click(v, `push-check-${PID}`); await settle();
  check(pushExecs("check").length === 2, "Check again checks");
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  check(pushExecs("run").length === 1, "Submit runs once");
};

cases["P-push-notice"] = async () => { // §8.2: the orchestrator is told the result through the notice queue
  const msg = "the push allowlist changed since this card was made; ask for a new card";
  const outcomes = [
    [json(0, runPushed), `decision ${PID} answered Push; result: pushed ${PURL} main aa22000..cc11000 (2 commits)`, 0],
    [json(1, { ok: false, done: false, answer: "Push", refusal: { check: "C6i", message: msg }, result: null, message: msg }), `decision ${PID} answered Push; refused: ${msg} (the card stays open)`, 0],
    [json(0, { ok: true, done: false, answer: "Not now", refusal: null, result: null, message: "answered Not now" }), `decision ${PID} answered Not now`, 1],
  ];
  for (const [reply, want, opt] of outcomes) {
    const { v } = await pushWorld({ opts: { records: alive() }, tool: { run: () => reply } });
    await click(v, `opt-${PID}-${opt}`); await settle();
    await click(v, `submit-${PID}`); await settle();
    check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", want]]), `notice "${want}" (got ${JSON.stringify(sends())})`);
  }
};

cases["P-push-tool-replies"] = async () => { // the tool's own success shapes (Job A): "already on the remote", output as one string, a message led by the card id
  const already = { ok: true, done: true, answer: "Push", refusal: null, message: `${PID}: already on the remote: ${PURL} main is at cc11000.`,
    result: { at: "2026-11-16T10:01:00Z", from: PR, to: PC, url: PURL, branch: "main", merged: false, how: "already", output: "" } };
  const pushed = { ...runPushed, message: `${PID}: pushed ${PURL} main aa22000..cc11000 (2 commits); local main fast-forwarded.`, result: { ...runPushed.result, how: "pushed", output: "line one\nline two" } };
  const outcomes = [[already, "Already on the remote: file:///tmp/pa-scratch/origin.git main is at cc11000", `decision ${PID} answered Push; result: already on the remote: ${PURL} main is at cc11000.`],
    [pushed, "line two", `decision ${PID} answered Push; result: pushed ${PURL} main aa22000..cc11000 (2 commits); local main fast-forwarded.`]];
  for (const [reply, shows, notice] of outcomes) {
    const { v } = await pushWorld({ opts: { records: alive() }, tool: { run: () => json(0, reply) } });
    await click(v, `opt-${PID}-0`); await settle();
    await click(v, `submit-${PID}`); await settle();
    check(look(v).text.includes(shows), `shows "${shows}" (got ${look(v).text.slice(0, 1500)})`);
    check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", notice]]), `notice "${notice}" (got ${JSON.stringify(sends())})`);
  }
};

cases["P-push-answered"] = async () => { // an answered push card shows its answer; no check runs for it
  const { v } = await pushWorld({ card: pushCardText({ status: "answered", answer: "Push", answered: "2026-11-16T10:01:00Z", answeredBy: "person, in the panel" }) });
  await click(v, "view-decisions"); await click(v, "pa-older");
  check(pushExecs().length === 0, `no pa-push-main for an answered card (got ${pushExecs().length})`);
  check(look(v).text.includes("answered: Push"), "its answer");
};

const allowShow = (over = {}) => json(0, { file: "/home/u/.config/herdr-pa/push-allow.json", exists: true, valid: true, problem: null, sha: "a".repeat(64), mtime: "2026-11-16T08:00:00Z", enabled: true, space: S, state: "on",
  repos: [{ path: "/repo", url: PURL, branch: "main", space: S, enabled: true, prePush: ["/bin/check", "{path}"], note: "scratch", thisSpace: true },
    { path: "/other", url: "git@host:me/other.git", branch: "main", space: "sp9", enabled: false, thisSpace: false }], ...over });
const shows = () => g.__execs.filter(([c, a]) => c === "pa-push-allow" && a[0] === "show");

cases["P-push-rules-off"] = async () => { // AC3.5: no file: "off (the default)"; read with pa-push-allow show --json
  const { v } = await pushWorld();
  check(shows().length === 0, "not read before the Rules view opens");
  await click(v, "view-rules"); await settle();
  check(shows().length === 1 && JSON.stringify(shows()[0][1]) === JSON.stringify(["show", "--json", "--space", S]), `one show (got ${JSON.stringify(shows().map((x) => x[1]))})`);
  check(look(v).text.includes("Push allowlist: off (the default): main-branch pushes are not possible."), `off (got ${look(v).text.slice(0, 1500)})`);
};

cases["P-push-rules-entries"] = async () => { // AC3.5 / D15: the entries, this space marked; reread on reloads at most every 30 s and on Refresh
  const { v } = await pushWorld({ tool: { show: () => allowShow() } });
  await click(v, "view-rules"); await settle();
  const { text } = look(v);
  for (const want of ["Push allowlist: on", "/home/u/.config/herdr-pa/push-allow.json", "/repo → file:///tmp/pa-scratch/origin.git main", "this space", "prePush: /bin/check {path}", "scratch",
    "/other → git@host:me/other.git main", "space sp9", "off", "Read only: change it in your own terminal with pa-push-allow, or by hand."]) check(text.includes(want), `shows "${want}" (got ${text.slice(0, 2000)})`);
  advance(10000); await fire(10000); advance(10000); await fire(10000);
  check(shows().length === 1, `reloads within 30 s: no new read (got ${shows().length})`);
  advance(11000); await fire(10000);
  check(shows().length === 2, `a reload after 30 s reads again (got ${shows().length})`);
  await click(v, "push-allow-refresh"); await settle();
  check(shows().length === 3, "Refresh reads again");
  await click(v, "view-tasks"); advance(40000); await fire(10000);
  check(shows().length === 3, "not read while another view is shown");
};

cases["P-push-rules-invalid"] = async () => { // AC3.2: an invalid file says why
  const { v } = await pushWorld({ tool: { show: () => allowShow({ valid: false, problem: "repos[0].url is not an allowed URL", enabled: false, repos: [], state: "not valid, so off: repos[0].url is not an allowed URL" }) } });
  await click(v, "view-rules"); await settle();
  check(look(v).text.includes("Push allowlist: not valid, so nothing is allowed: repos[0].url is not an allowed URL. Fix it by hand."), `the reason (got ${look(v).text.slice(0, 1500)})`);
  const { v: w } = await pushWorld({ tool: { show: () => ({ code: 127, stdout: "", stderr: "pa-push-allow: command not found" }) } });
  await click(w, "view-rules"); await settle();
  check(look(w).text.includes("Push allowlist: could not read it (pa-push-allow: command not found)"), `a failed read says so (got ${look(w).text.slice(0, 1500)})`);
};

// ---- t-009 Costs view (step 9c): R9, R11, R12, R14; every pa-costs call is answered by costsTool (an unanswered one is recorded) ----
const isoAt = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
// Where the panel's zone table starts (t-036 round 2): 3653 days (10 years) before now, at that day's UTC midnight.
const FROM0 = Math.floor((T0 - 3653 * 86400000) / 86400000) * 86400;
const UTC_ARGS = ["--zone-offsets", "0:0", "--zone-name", "UTC", "--zone-from", String(FROM0)]; // the zone every world's collect passes (t-036)
const CUR = "XTS"; // the ISO code kept for tests: generic code and tests name no real currency
function costsSummary(over = {}) {
  const from = isoAt(T0 - 7200000), to = isoAt(T0 - 3600000);
  return {
    v: 1, space: S, updated: isoAt(T0 - 120000), account: "acct1",
    prices: { profile: "test", source: "test table", asOf: "2027-01-01", currency: CUR, missing: [] },
    totals: { tokens: { in: 10, out: 20, cw: 30, cr: 940, all: 1000 }, money: 12.5, unpricedTokens: 0, reconstructedMoney: 2.5, calls: 7 },
    hours: [[isoAt(T0).slice(0, 13), { tokens: 100, money: 1.25 }]],
    byActivity: [
      { key: "build", tokens: 600, money: 7.5, calls: 3, reconstructed: 2.5, tasks: [{ key: "t-1", tokens: 600, money: 7.5 }] },
      { key: "cache-rebuild", tokens: 300, money: 4, calls: 0, tasks: [{ key: "t-1", tokens: 300, money: 4 }] },
      { key: "unattributed", tokens: 100, money: 1, calls: 1, tasks: [{ key: "(no task)", tokens: 100, money: 1 }], reasons: [{ key: "pa-no-journal", tokens: 100, money: 1 }] }],
    byTask: [
      { key: "t-1", title: "One", state: "in-progress", tokens: 900, money: 11.5, calls: 5, phases: [{ key: "build", tokens: 900, money: 11.5 }], sessions: ["aaaa1111", "bbbb2222"], split: true },
      { key: "t-0", title: "Gone", state: "deleted", tokens: 0, money: 0, calls: 0, phases: [], sessions: [] },
      { key: "(no task)", state: null, tokens: 100, money: 1, calls: 1, phases: [], sessions: ["cccc3333"] }],
    byPhase: [{ key: "build", tokens: 1000, money: 12.5, calls: 7 }],
    byKind: {
      kind: [{ key: "worker", tokens: 900, money: 11.5, calls: 5 }, { key: "pa", tokens: 100, money: 1, calls: 2 }],
      role: [{ key: "build", tokens: 900, money: 11.5, calls: 5 }, { key: "(none)", tokens: 100, money: 1, calls: 2 }],
      program: [{ key: "claude", tokens: 1000, money: 12.5, calls: 7 }],
      model: [{ key: "model-a", tokens: 1000, money: 12.5, calls: 7 }],
      machine: [{ key: "host-one.example", tokens: 600, money: 7, calls: 4 }, { key: "host-two.example", tokens: 400, money: 5.5, calls: 3 }] },
    byDay: [{ key: isoAt(T0).slice(0, 10), tokens: 1000, money: 12.5, calls: 7, byActivity: { build: 7.5, "cache-rebuild": 4, unattributed: 1 } }],
    byDayModel: [{ day: isoAt(T0).slice(0, 10), model: "model-a", tokens: { in: 10, out: 20, cw5m: 30, cw1h: 0, cr: 940 }, money: 12.5 }],
    passes: [{ pass: 3, trigger: "report", from, money: 4, tokens: 300, rebuild: true }, { pass: 4, trigger: "message", from, money: null, tokens: 50, rebuild: false }],
    sessions: [
      { key: "aaaa1111", tokens: 600, money: 7.5, calls: 3, kind: "worker", role: "build", program: "claude", task: "t-1", model: "model-a", from, to, state: "closed", source: "reconstructed", match: "cwd+start" },
      { key: "bbbb2222", tokens: 300, money: 4, calls: 2, kind: "worker", role: "build", program: "claude", task: "t-1", model: "model-a", from, to, state: "gone", source: "recorded", match: "launch-record" },
      { key: "cccc3333", tokens: 100, money: 1, calls: 2, kind: "pa", role: null, program: "claude", task: null, model: "model-a", from, to, state: "open", source: "recorded", match: "launch-record" }],
    unmatched: { sessions: 0, tokens: 0 }, problems: [], ...over };
}
const statusOff = { config: null, state: {}, line: "Billing query: off", feePerRequest: 0.01, feeCurrency: CUR, maxPages: 3, runsPerDay: 3 };
const statusOn = (state = {}) => ({ ...statusOff, config: { query: "q1", enabled: true }, state, line: "Billing query: on [Turn off: pa-costs billed disable]" });
const noRows = (status = statusOff) => ({ comparison: { account: "acct1", currency: CUR, accountWide: true, models: [], covered: null, other: 0, total: { thisSpace: null, allSpaces: null, billed: 0 }, calibrated: null, noPrice: [], separate: [], sources: [] }, status, rows: 0 });
const withRows = (status = statusOn({ lastRun: "2027-01-15T06:00:00Z", result: "ok", requests: 1, nextAfter: "2027-01-16T06:00:00Z" })) => ({ status, rows: 4, comparison: {
  account: "acct1", currency: CUR, accountWide: true, covered: { from: "2027-01-10", to: "2027-01-14", days: 5, of: 6, missing: ["2027-01-15"] }, sources: ["query:q1"], fetchedAt: "2027-01-15T06:00:00Z",
  models: [
    { model: "model-a", thisSpace: 10, allSpaces: 20, thisSpacePartial: false, allSpacesPartial: false, billed: 25, factor: { kind: "price", value: 1.25, applied: true, few: false, note: "from quantities" }, notFinal: false, days: 5 },
    { model: "model-b", thisSpace: null, allSpaces: 3, thisSpacePartial: false, allSpacesPartial: true, billed: 2.4, factor: { kind: "scope", value: 0.8, applied: false, few: true, partial: true, note: "not applied; below 1.0: the table over-prices" }, notFinal: true, days: 2 }],
  other: 1.5, total: { thisSpace: 10, allSpaces: 23, thisSpacePartial: true, allSpacesPartial: true, billed: 28.9 }, calibrated: 12.5, calibratedPartial: true, noPrice: ["model-b"],
  separate: [{ currency: "XXX", amount: 3, rows: 1 }] } });
// Answers every pa-costs call: replies { "<sub>": (args, on, options) => reply }; sub is the first word, or "billed <action>".
function costsTool(replies = {}) {
  g.__costsUnexpected = [];
  const base = { collect: () => ({ code: 0, stdout: "", stderr: "" }), reconstruct: () => ({ code: 0, stdout: "", stderr: "" }), "billed compare": () => ({ code: 0, stdout: JSON.stringify(noRows()), stderr: "" }),
    "billed disable": () => ({ code: 0, stdout: "Billing query: off. Billed rows are kept.\n", stderr: "" }), report: () => ({ code: 0, stdout: JSON.stringify({ v: 1, account: "acct1", spaces: [S], byDayModel: [] }), stderr: "" }) };
  const all = { ...base, ...replies };
  g.__execFn = (command, args, on, options) => {
    if (command !== "pa-costs") return undefined;
    const sub = args[0] === "billed" ? `billed ${args[1]}` : args[0];
    if (!all[sub]) { g.__costsUnexpected.push(args); return { code: 9, stdout: "", stderr: "unexpected in the test" }; }
    return all[sub](args, on, options);
  };
}
const json0 = (v) => ({ code: 0, stdout: JSON.stringify(v), stderr: "" });
const costsExecs = (sub) => g.__execs.filter(([c, a]) => c === "pa-costs" && (!sub || (a[0] === "billed" ? `billed ${a[1]}` : a[0]) === sub));
const costsReads = () => g.__issued.filter((x) => (x.kind === "read" && /\/costs\//.test(x.path ?? "")) || (x.kind === "ls" && false));
const costsLs = () => g.__execs.filter(([c, a]) => c === "ls" && /\/costs$/.test(String(a[1] ?? "")));
function costsFiles(summary, { machine = null } = {}) {
  const dir = machine ? `${machine}:${RDIR}` : `:${DIR}`;
  if (summary !== undefined && summary !== null) g.__files[`${dir}/costs/summary.json`] = typeof summary === "string" ? summary : JSON.stringify(summary);
  g.__files[`${dir}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-1", title: "One", state: "in-progress", updated: "2027-01-15T07:00:00Z" }] });
  return `${dir}/costs/summary.json`;
}
async function costsWorld({ machine = null, summary = costsSummary(), tool = {}, open = true } = {}) {
  world({ machine });
  const file = costsFiles(summary, { machine });
  costsTool(tool);
  const v = await mount(); await settle();
  if (open) { await click(v, "view-costs"); await settle(); }
  return { v, file };
}
// The Costs view's own subtree (id pa-costs): its text and clicks.
function costsView(v) {
  let found = null;
  const walk = (x) => { if (found || !x || typeof x !== "object") return; if (x.key === "pa-costs") { found = x; return; } for (const k of x.kids ?? []) walk(k); };
  walk(v.render(cx));
  return found ? inspect(found) : { text: "", clicks: {} };
}
// Every text segment of the view that holds a money amount says what it is: est., billed, a stated fee or a factor.
function moneyLabelled(v) {
  const segs = costsView(v).text.split(" | ");
  return segs.filter((x) => /\d\.\d\d\b|<0\.01/.test(x) && !/est\.|billed|fee|factor|ratio/.test(x));
}

cases["P-costs-tab"] = async () => { // AC9.1: a 4th tab after Rules; "Costs" before any read, "Costs · <money> est." after; nothing read until it is clicked
  const { v } = await costsWorld({ open: false });
  let { text, clicks } = look(v);
  check(Boolean(clicks["view-costs"]), `a Costs tab (have ${Object.keys(clicks)})`);
  const order = ["| Decisions |", "| Tasks · 1 |", "| Rules", "| Costs |"].map((x) => text.indexOf(x));
  check(order.every((x, i) => x >= 0 && (i === 0 || x > order[i - 1])), `Costs comes after Rules (got ${order}; ${text.slice(0, 600)})`);
  check(text.includes("| Costs |"), `the label is Costs before any read (got ${text.slice(0, 600)})`);
  check(costsReads().length === 0 && costsExecs().length === 0, `nothing read before the click (got ${JSON.stringify(costsReads())})`);
  await click(v, "view-costs"); await settle();
  ({ text } = look(v));
  check(/Costs · [^|]*12\.50 est\. \|/.test(text), `the tab shows the total est. (got ${text.slice(0, 700)})`);
  check(costsReads().length === 1, `one read on the click (got ${costsReads().length})`);
};

cases["P-costs-tabidle"] = async () => { // R11: another view shown: no costs read, no pa-costs exec; Costs shown: at most one read per 10 s
  const { v } = await costsWorld({ open: false });
  for (let s = 1; s <= 60; s++) { advance(1000); for (const tm of g.__timers) if (s * 1000 % tm.ms === 0) tm.fn(cx); if (s % 3 === 0) v.reload(cx); look(v); await settle(); }
  check(costsReads().length === 0 && costsExecs().length === 0 && costsLs().length === 0, `Tasks/Decisions: nothing (got ${JSON.stringify(costsReads())}, ${JSON.stringify(costsExecs())})`);
  await click(v, "view-costs"); await settle();
  const start = g.__now;
  for (let s = 1; s <= 60; s++) { advance(1000); for (const tm of g.__timers) if (s * 1000 % tm.ms === 0) tm.fn(cx); if (s % 3 === 0) v.reload(cx); look(v); await settle(); }
  const reads = costsReads().filter((x) => x.at > start);
  for (let w = 0; w < 6; w++) { const n = reads.filter((x) => x.at > start + w * 10000 && x.at <= start + (w + 1) * 10000).length; check(n <= 1, `at most one read per 10 s (window ${w}: ${n})`); }
  check(reads.length >= 4, `but it does keep reading while shown (got ${reads.length})`);
};

cases["P-costs-hidden"] = async () => { // R11 (d-039): Costs selected and the panel hidden: no costs read or exec; P-hidden-one-call's 2 calls per 10 s still hold
  const { v } = await costsWorld({ machine: M });
  const before = { reads: costsReads().length, execs: costsExecs().length };
  g.__layout = panelLayout("changes");
  for (let s = 1; s <= 60; s++) { advance(1000); for (const tm of g.__timers) if (s * 1000 % tm.ms === 0) tm.fn(cx); if (s % 3 === 0) v.reload(cx); look(v); await settle(); }
  check(costsReads().length === before.reads && costsExecs().length === before.execs && v.view === "costs", `hidden: nothing (reads ${before.reads} -> ${costsReads().length}, execs ${before.execs} -> ${costsExecs().length})`);
  // P-hidden-one-call with view = "costs".
  world({ machine: M });
  remoteCards(4); costsFiles(costsSummary(), { machine: M }); costsTool();
  g.__layout = panelLayout("changes");
  const w = await mount();
  w.view = "costs";
  for (let s = 1; s <= 60; s++) { advance(1000); for (const tm of g.__timers) if (s * 1000 % tm.ms === 0) tm.fn(cx); if (s % 3 === 0) w.reload(cx); look(w); await settle(); }
  const kinds = [...new Set(remoteCalls().filter((x) => x.at > T0).map((x) => (x.kind === "read" ? `read ${x.path.split("/").pop()}` : x.kind)))].sort();
  check(JSON.stringify(kinds) === '["batch","read ledger.json"]', `only the batched card read and the ledger (got ${JSON.stringify(kinds)})`);
  const per = []; for (let i = 0; i < 6; i++) per.push(remoteCalls().filter((x) => x.at > T0 + i * 10000 && x.at <= T0 + (i + 1) * 10000));
  check(per.every((l) => l.length === 2), `2 calls in each 10 s interval (got ${per.map((l) => l.length)})`);
  // Shown again: the view reads at once.
  g.__layout = panelLayout("project-agent"); advance(1000); await fire(1000); await settle();
  check(costsReads().length >= 1, "shown again: read");
};

cases["P-costs-timercopy"] = async () => { // R11: the timer copy (props.timer) reads no costs and runs no pa-costs view command
  world(); costsFiles(costsSummary()); costsTool();
  g.__props = { timer: true };
  const v = await mount();
  v.view = "costs";
  for (let i = 0; i < 8; i++) { advance(15000); for (const t of [...g.__timers]) t.fn(cx); await settle(); v.render(cx); }
  check(costsReads().length === 0 && costsLs().length === 0 && costsExecs().filter(([, a]) => a[0] !== "collect" && !(a[0] === "billed" && a[1] === "fetch")).length === 0, `nothing (got ${JSON.stringify(costsReads())}, ${JSON.stringify(costsExecs())})`);
};

cases["P-costs-empty"] = async () => { // AC9.3, §5.5: no costs/ folder: the text and Reconstruct
  const { v } = await costsWorld({ summary: null });
  const { text, clicks } = costsView(v);
  check(text.includes("No cost records yet. Spend is recorded for sessions started from now on.") && Boolean(clicks["costs-reconstruct"]) && text.includes("Reconstruct from session files"), `empty state (got ${text})`);
  check(costsLs().length === 1, `one ls of costs/ (got ${JSON.stringify(costsLs())})`);
  check(!/0\.00/.test(text) && !/error/i.test(text), "no zero, no error");
};

cases["P-costs-pending"] = async () => { // §5.5: records but no summary yet
  world(); costsFiles(undefined); costsTool();
  g.__ls[`:${DIR}/costs`] = ["sessions.jsonl", ".collect.lock"];
  const v = await mount(); await settle(); await click(v, "view-costs"); await settle();
  const { text, clicks } = costsView(v);
  check(text.includes("Collecting… (first run)") && Boolean(clicks["costs-refresh"]), `pending (got ${text})`);
};

cases["P-costs-invalid"] = async () => { // §5.5: an invalid summary: the error and Refresh
  const { v } = await costsWorld({ summary: "{ not json" });
  const { text, clicks } = costsView(v);
  check(/costs\/summary\.json: .*JSON|costs\/summary\.json: Unexpected|costs\/summary\.json: Expected/.test(text) && Boolean(clicks["costs-refresh"]), `error and Refresh (got ${text})`);
};

cases["P-costs-notinstalled"] = async () => { // §5.5 (D5): the exec cannot start (rejects) or exits 127: install hints, local and remote
  for (const [machine, want] of [[null, "pa-costs is not installed here; run ./install.sh."], [M, `pa-costs is not installed on ${M}; run pa-sync --on ${M}.`]]) {
    const { v } = await costsWorld({ machine, tool: { collect: () => { throw new Error("spawn pa-costs ENOENT"); }, "billed compare": () => ({ code: 127, stdout: "", stderr: "pa-costs: command not found" }) } });
    await click(v, "costs-refresh"); await settle();
    const { text } = costsView(v);
    check(text.includes(`Refresh failed: ${want}`), `${machine ?? "local"}: ${want} (got ${text.slice(0, 1500)})`);
    check(text.includes("Billed amounts could not be read (pa-costs is not installed here; run ./install.sh.)"), `the local compare says so too (got ${text.slice(-600)})`);
  }
};

cases["P-costs-full"] = async () => { // AC9.1, AC4.2, AC7.2: header, tiles, sections, marks; every money figure is labelled
  const { v } = await costsWorld();
  const { text } = costsView(v);
  for (const want of ["Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago", "Refresh", "Total", "Today", "Last 7 days (UTC)", "1k tokens · 94% cache read",
    "By activity", "build ◌", "cache rebuild after idle", "unattributed", "By task", "| t-1 | One ≈ |", "in-progress", "t-0 (deleted) Gone", "(no task)", "By session type", "By day (UTC)",
    "◌ Includes", "est. reconstructed (best effort) from session files still on disk."]) check(text.includes(want), `shows "${want}" (got ${text.slice(0, 2500)})`);
  check(/12\.50.* est\./.test(text), "the total est.");
  const bad = moneyLabelled(v);
  check(bad.length === 0, `every money figure says est. or billed (unlabelled: ${JSON.stringify(bad)})`);
};

cases["P-costs-noprice"] = async () => { // AC4.4: no price table: tokens only, never 0.00
  const s = costsSummary({ prices: { profile: "test", source: null, asOf: null, currency: null, missing: ["model-a"] } });
  s.totals = { ...s.totals, money: null, reconstructedMoney: null, unpricedTokens: 1000 };
  for (const e of [...s.byActivity, ...s.byTask, ...s.byDay, ...s.sessions, ...s.byKind.machine, ...s.byKind.kind]) e.money = null;
  for (const e of s.byActivity) for (const x of [...(e.tasks ?? []), ...(e.reasons ?? [])]) x.money = null;
  s.hours = [[isoAt(T0).slice(0, 13), { tokens: 100, money: null }]]; s.passes[0].money = null;
  for (const d of s.byDay) d.byActivity = {};
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-activity-0"); await click(v, "costs-dim-machine"); await settle();
  const { text } = costsView(v);
  check(text.includes("No price table in profile test: tokens only.") && text.includes("1k tokens") && !/\d\.\d\d/.test(text), `tokens only, no amount (got ${text.slice(0, 2000)})`);
  check(look(v).text.includes("Costs · 1k tokens · no price"), "the tab says no price");
};

cases["P-costs-unpriced"] = async () => { // decision (2): a partial entry has a mark and is never complete; an unpriced one is "no price", never 0.00
  const s = costsSummary({ prices: { profile: "test", source: "test table", asOf: "2027-01-01", currency: CUR, missing: ["model-z"] } });
  s.totals = { ...s.totals, unpricedTokens: 77 };
  s.byActivity[0].unpricedTokens = 77;
  s.byActivity.push({ key: "review", tokens: 77, money: null, calls: 1, tasks: [{ key: "t-1", tokens: 77, money: null }] });
  s.hours = [[isoAt(T0).slice(0, 13), { tokens: 100, money: null }]];
  s.byTask = s.byTask.filter((x) => x.key !== "t-0"); // its real 0.00 is not what this case looks at
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-activity-3"); await settle();
  const { text } = costsView(v);
  check(text.includes("77 tokens of model-z have no price: shown as tokens only."), `the partial line (got ${text.slice(0, 1500)})`);
  check(/12\.50 est\. \+ unpriced/.test(text.replace(/[^\x20-\x7e]+/g, " ").replace(/XTS ?/g, "")) || /est\. \+ unpriced/.test(text), "the total is partial");
  check(text.includes("partly no price") && text.includes("— est.") && text.includes("no price"), `the marks (got ${text.slice(0, 2500)})`);
  const segs = text.split(" | ");
  const today = segs[segs.indexOf("Today") + 1];
  check(today === "— est.", `Today with only unpriced use is — est. (got ${today})`);
  check(!segs.some((x) => /^0\.00|\b0\.00 est/.test(x)), "nothing unpriced reads 0.00");
  check(look(v).text.includes("+ unpriced"), "the tab marks the partial total");
};

cases["P-costs-problems"] = async () => { // §4: the count line, the expanded list (20), truncated, the collector's failure
  const problems = Array.from({ length: 25 }, (_, i) => `problem ${i + 1}`);
  const { v } = await costsWorld({ summary: costsSummary({ problems, truncated: true }) });
  g.__shared["project-agent.costs"] = { [S]: { at: isoAt(T0), ok: false, code: 2, error: "pa-costs: the lock is busy\nsecond line" } };
  let { text } = costsView(v);
  check(text.includes("⚠ 25 problems reading the records ▸") && !text.includes("problem 1 |"), `the count line, folded (got ${text.slice(0, 800)})`);
  check(text.includes("Lists shortened to fit; pa-costs report shows all."), "truncated");
  check(/Collector: last run .* failed: pa-costs: the lock is busy/.test(text) && !text.includes("second line"), `the collector line (got ${text.slice(0, 1200)})`);
  await click(v, "costs-problems");
  ({ text } = costsView(v));
  check(text.includes("problem 20") && !text.includes("problem 21"), `the first 20 (got ${text.slice(0, 1500)})`);
};

cases["P-costs-nototals"] = async () => { // decision (1): a summary without totals: "Nothing collected yet" and the reason, never an error or a zero
  const s = { v: 1, space: S, updated: isoAt(T0 - 60000), account: "default", collect: { ok: false, at: isoAt(T0 - 60000), why: "prices.json is not valid JSON" }, problems: ["collect failed: prices.json is not valid JSON"] };
  const { v } = await costsWorld({ summary: s });
  const { text } = costsView(v);
  check(text.includes("Nothing collected yet. prices.json is not valid JSON") && !/\d\.\d\d/.test(text) && !/costs\/summary\.json:/.test(text), `nothing collected and why (got ${text})`);
  check(look(v).text.includes("| Costs |"), "the tab has no figure");
};

cases["P-costs-timeout"] = async () => { // D3: a hung read keeps the copy with a line; without one, "did not answer"; the rest of the panel still loads
  const { v, file } = await costsWorld();
  g.__hang = [file.replace(/^:/, ":")];
  advance(11000); v.reload(cx); await settle();
  advance(11000); await settle();
  let { text } = costsView(v);
  check(text.includes("summary.json did not answer; showing the copy read at") && text.includes("By activity"), `kept copy and the line (got ${text.slice(0, 600)})`);
  check(/PA: proj/.test(look(v).text) && /Tasks · 1/.test(look(v).text), "the rest of the panel is there");
  world(); costsFiles(costsSummary()); costsTool();
  g.__hang = [`:${DIR}/costs/summary.json`];
  const w = await mount(); await settle(); await click(w, "view-costs");
  advance(11000); await settle();
  ({ text } = costsView(w));
  check(text.includes("Costs did not answer. Trying again shortly.") && Boolean(costsView(w).clicks["costs-refresh"]), `no copy (got ${text})`);
  check(/Tasks · 1/.test(look(w).text), "the tasks still committed");
};

cases["P-costs-kept"] = async () => { // D2: switch space and back: the kept copy at once, no Loading, one new read
  world({ machine: M });
  costsFiles(costsSummary(), { machine: M }); costsTool();
  addRemoteSpace("sp2", "other", ledgerOf(3, "b"));
  g.__files[`${M}:.config/herdr-pa/spaces/sp2/costs/summary.json`] = JSON.stringify(costsSummary({ space: "sp2", totals: { ...costsSummary().totals, money: 99 } }));
  const v = await mount(); await settle(); await click(v, "view-costs"); await settle();
  check(costsView(v).text.includes("By activity"), "sp1 shown");
  show(`${M}/sp2`); advance(11000); look(v); await settle(); look(v); await settle();
  check(/99\.00/.test(costsView(v).text), `sp2 shown (got ${costsView(v).text.slice(0, 300)})`);
  const n = costsReads().length;
  show(RS); advance(11000);
  const at = look(v), mine = costsView(v);
  check(!/Loading/.test(at.text) && /12\.50/.test(mine.text) && !/99\.00/.test(mine.text), `sp1's kept copy at once (got ${mine.text.slice(0, 300)})`);
  await settle();
  check(costsReads().length === n + 1, `one new read (got ${costsReads().length - n})`);
};

cases["P-costs-remote"] = async () => { // R6, AC6.2, AC9.4: reads and execs on m1, except the billed compare (here); never a session file
  const { v } = await costsWorld({ machine: M, tool: { "billed compare": (args) => json0(args.includes("--here-json") ? withRows() : { ...withRows(), comparison: null }) } });
  await click(v, "costs-refresh"); await settle(); await click(v, "costs-reconstruct").catch(() => {});
  const reads = costsReads();
  check(reads.length >= 1 && reads.every((x) => x.on === M), `costs reads on m1 (got ${JSON.stringify(reads)})`);
  check(!g.__issued.some((x) => /\/projects\/|\/sessions\/|\.claude|\.codex/.test(x.path ?? "")), "no session file read");
  for (const [, a, on] of costsExecs()) {
    const sub = a[0] === "billed" ? `billed ${a[1]}` : a[0];
    check(sub === "billed compare" ? on === null : on === M, `${sub} on ${on}`);
  }
  check(costsExecs("collect").some(([, a]) => JSON.stringify(a) === JSON.stringify(["collect", "--space", S, ...UTC_ARGS])), `collect --space sp1 (got ${JSON.stringify(costsExecs("collect"))})`);
};

cases["P-costs-refresh"] = async () => { // R12: Refresh runs collect on the space's machine, one at a time, Refreshing…, then one reread
  let release; const held = new Promise((r) => { release = r; });
  const { v } = await costsWorld({ machine: M, tool: { collect: () => held.then(() => ({ code: 0, stdout: "", stderr: "" })) } });
  const n = costsReads().length;
  await click(v, "costs-refresh"); await settle();
  check(costsView(v).text.includes("Refreshing…"), "Refreshing…");
  await click(v, "costs-refresh"); await settle();
  check(costsExecs("collect").length === 1, `one at a time (got ${costsExecs("collect").length})`);
  const [, args, on, opts] = costsExecs("collect")[0];
  check(JSON.stringify(args) === JSON.stringify(["collect", "--space", S, ...UTC_ARGS]) && on === M && opts?.timeout >= 30000, `its argv and machine (got ${JSON.stringify(costsExecs("collect")[0])})`);
  release(); await settle();
  check(costsReads().length === n + 1 && !costsView(v).text.includes("Refreshing…"), `one reread (got ${costsReads().length - n})`);
};

cases["P-costs-reconstruct"] = async () => { // AC9.3: Reconstruct runs reconstruct --space on the machine, a long timeout; exit 2 shows its stderr
  const { v } = await costsWorld({ machine: M, summary: null, tool: { reconstruct: () => ({ code: 2, stdout: "", stderr: "pa-costs: no session files found for this space\n" }) } });
  await click(v, "costs-reconstruct"); await settle();
  const r = costsExecs("reconstruct");
  check(r.length === 1 && JSON.stringify(r[0][1]) === JSON.stringify(["reconstruct", "--space", S]) && r[0][2] === M && r[0][3]?.timeout >= 120000, `its argv, machine, timeout (got ${JSON.stringify(r)})`);
  check(costsView(v).text.includes("Reconstruct failed: pa-costs: no session files found for this space"), `stderr shown (got ${costsView(v).text})`);
};

cases["P-costs-drill"] = async () => { // AC9.2: task -> phases and sessions; activity -> tasks; unattributed -> reasons; cache rebuild -> passes; day -> activities; one open per section
  const { v } = await costsWorld();
  await click(v, "costs-task-0");
  let { text } = costsView(v);
  check(text.includes("Phases:") && text.includes("Sessions:") && /◌ aaaa1111 · worker · build · claude · model-a .* closed · reconstructed/.test(text) && /bbbb2222 .* gone · recorded/.test(text) && text.includes("≈ shared passes split by journal entries"), `task drill (got ${text.slice(0, 2500)})`);
  await click(v, "costs-activity-0");
  ({ text } = costsView(v));
  check(text.includes("Tasks:") && /t-1 \| · 600 tokens · .*7\.50.* est\./.test(text), `activity -> tasks (got ${text.slice(0, 2500)})`);
  await click(v, "costs-activity-2");
  ({ text } = costsView(v));
  check(text.includes("Reasons:") && text.includes("pa-no-journal") && !/t-1 \| · 600 tokens/.test(text), `unattributed -> reasons, the other closed (got ${text.slice(0, 2500)})`);
  await click(v, "costs-activity-1");
  ({ text } = costsView(v));
  check(text.includes("Passes with a cache rebuild:") && /pass 3 · report .* cache rebuild/.test(text) && !text.includes("pass 4"), `cache rebuild -> passes (got ${text.slice(0, 2500)})`);
  check(text.includes("Phases:"), "the task section keeps its own open row");
  await click(v, "costs-day-0");
  ({ text } = costsView(v));
  check(/cache rebuild after idle · .*4\.00.* est\./.test(text) && /build · .*7\.50.* est\./.test(text), `day -> activities (got ${text.slice(0, 3000)})`);
  await click(v, "costs-dim-role"); await click(v, "costs-kind-0");
  ({ text } = costsView(v));
  check(/aaaa1111/.test(text) && !/cccc3333 ·/.test(text.split("By day")[0].split("By session type")[1] ?? ""), `role build -> its sessions (got ${text.slice(0, 3000)})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
};

// ---- t-143 the Costs tab: long lists scroll, sort, filter, task links (evidence/t-143/plan.md) ----
// The element with this id in the Costs view (null: none).
function costsEl(v, key) {
  let found = null;
  const walk = (x) => { if (found || !x || typeof x !== "object") return; if (x.key === key) { found = x; return; } for (const k of x.kids ?? []) walk(k); };
  walk(v.render(cx));
  return found;
}
// The By task section's text segments (between its head and the next section's).
const taskSegs = (v) => { const segs = costsView(v).text.split(" | "); const a = segs.indexOf("By task"), b = segs.indexOf("By session type"); return segs.slice(a, b); };
// Three tasks whose cost, id and recent orders differ: cost t-2, t-10, t-3; id t-2, t-3, t-10; recent t-10, t-2, t-3.
function sortSummary() {
  const s = costsSummary();
  s.byTask = [
    { key: "t-2", title: "Two", state: "in-progress", tokens: 500, money: 5, calls: 2, phases: [], sessions: ["s2"] },
    { key: "t-10", title: "Ten", state: "done", tokens: 300, money: 3, calls: 1, phases: [], sessions: ["s10"] },
    { key: "t-3", title: "Three", state: "queued", tokens: 100, money: 1, calls: 1, phases: [], sessions: [] }];
  s.sessions = [
    { key: "s2", tokens: 500, money: 5, calls: 2, kind: "worker", role: "build", program: "claude", task: "t-2", model: "model-a", from: isoAt(T0 - 7200000), to: isoAt(T0 - 3600000), state: "closed", source: "recorded" },
    { key: "s10", tokens: 300, money: 3, calls: 1, kind: "worker", role: "review", program: "claude", task: "t-10", model: "model-a", from: isoAt(T0 - 1800000), to: isoAt(T0 - 600000), state: "closed", source: "recorded" }];
  s.byDay = [
    { key: isoAt(T0).slice(0, 10), tokens: 100, money: 1, calls: 1, byActivity: {} },
    { key: isoAt(T0 - 86400000).slice(0, 10), tokens: 800, money: 8, calls: 3, byActivity: {} }];
  return s;
}
const orderOf = (segs, keys) => keys.map((k) => segs.indexOf(k));
const ascending = (xs) => xs.every((x, i) => x >= 0 && (i === 0 || x > xs[i - 1]));

cases["P143-scroll"] = async () => { // I2: a long By task list sits in a fixed-height box that scrolls; a short one draws at its own height
  const s = costsSummary();
  s.byTask = Array.from({ length: 30 }, (_, i) => ({ key: `t-${i + 1}`, title: `Task ${i + 1}`, state: "queued", tokens: 10, money: 0.1, calls: 1, phases: [], sessions: [] }));
  const { v } = await costsWorld({ summary: s });
  const box = costsEl(v, "costs-list-task");
  check(Boolean(box), "a costs-list-task box");
  check(Boolean(box?.style?.overflow_y_scrollbar) && Array.isArray(box?.style?.h), `it scrolls at a fixed height (style ${JSON.stringify(box?.style)})`);
  const text = costsView(v).text;
  check(text.includes("Task 1") && text.includes("Task 30"), "every row is still drawn inside it");
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
  const short = (await costsWorld()).v, sbox = costsEl(short, "costs-list-task");
  check(Boolean(sbox) && !sbox.style?.h, `three tasks: no fixed height (style ${JSON.stringify(sbox?.style)})`);
};

cases["P143-drill-scroll"] = async () => { // I2: a task with many workers: its open drill scrolls in its own box, every session listed
  const s = costsSummary();
  const keys = Array.from({ length: 25 }, (_, i) => `w${String(i).padStart(3, "0")}aaaa`);
  s.sessions = keys.map((key) => ({ key, tokens: 10, money: 0.1, calls: 1, kind: "worker", role: "build", program: "claude", task: "t-1", model: "model-a", from: isoAt(T0 - 7200000), to: isoAt(T0 - 3600000), state: "closed", source: "recorded" }));
  s.byTask[0].sessions = keys;
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-task-0"); await settle();
  const box = costsEl(v, "costs-drill-task");
  check(Boolean(box?.style?.overflow_y_scrollbar) && Array.isArray(box?.style?.h), `the drill scrolls at a fixed height (style ${JSON.stringify(box?.style)})`);
  const text = inspect(box ?? {}).text;
  check(keys.every((k) => text.includes(k)), "every session is in the drill box");
};

cases["P143-sort"] = async () => { // I3: By task sorts by cost (default: the summary's order), task id, recent; By day by day (default) or cost
  const { v } = await costsWorld({ summary: sortSummary() });
  const keys = ["t-2", "t-10", "t-3"];
  check(ascending(orderOf(taskSegs(v), keys)), `cost order by default (got ${taskSegs(v).join(" | ")})`);
  await click(v, "costs-sort-task-task"); await settle();
  check(ascending(orderOf(taskSegs(v), ["t-2", "t-3", "t-10"])), `task order (got ${taskSegs(v).join(" | ")})`);
  await click(v, "costs-sort-task-recent"); await settle();
  check(ascending(orderOf(taskSegs(v), ["t-10", "t-2", "t-3"])), `recent order (got ${taskSegs(v).join(" | ")})`);
  await click(v, "costs-task-0"); await settle(); // ids stay the summary's index: costs-task-0 is t-2 in any order
  check(costsView(v).text.includes("s2 · worker"), "costs-task-0 is still t-2");
  await click(v, "costs-sort-task-cost"); await settle();
  check(ascending(orderOf(taskSegs(v), keys)), `back to cost (got ${taskSegs(v).join(" | ")})`);
  const days = sortSummary().byDay.map((d) => d.key), segs = () => costsView(v).text.split(" | ");
  check(ascending(orderOf(segs(), days)), "By day newest first by default");
  await click(v, "costs-sort-day-cost"); await settle();
  check(ascending(orderOf(segs(), [days[1], days[0]])), `By day by cost (got ${segs().slice(-20).join(" | ")})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
};

cases["P143-filter"] = async () => { // I3: a filter field (made on its click) narrows By task by id, title or state; none matching says so; clear ends it
  const { v } = await costsWorld({ summary: sortSummary() });
  check(!costsView(v).text.includes("of 3 tasks"), "no filter before the click");
  await click(v, "costs-filter-task"); await settle();
  const st = v.costs.peek(S), field = st?.filter;
  check(Boolean(field) && typeof field.set === "function", "a filter field");
  field.set("ten"); field.fire("change"); await settle();
  let segs = taskSegs(v);
  check(segs.includes("t-10") && !segs.includes("t-2") && !segs.includes("t-3") && segs.join(" | ").includes("1 of 3 tasks"), `title match (got ${segs.join(" | ")})`);
  field.set("QUEUED"); field.fire("change"); await settle();
  segs = taskSegs(v);
  check(segs.includes("t-3") && !segs.includes("t-10"), `state match, any case (got ${segs.join(" | ")})`);
  field.set("zzz"); field.fire("change"); await settle();
  check(taskSegs(v).join(" | ").includes("No task matches"), `none matching (got ${taskSegs(v).join(" | ")})`);
  await click(v, "costs-filter-task-clear"); await settle();
  segs = taskSegs(v);
  check(["t-2", "t-10", "t-3"].every((k) => segs.includes(k)) && !segs.join(" | ").includes("of 3 tasks"), `cleared (got ${segs.join(" | ")})`);
};

cases["P143-link"] = async () => { // I4: a task id is a link: the Tasks tab opens with that task; the row does not toggle; no link for (no task) or a deleted task
  const { v } = await costsWorld();
  const { clicks } = costsView(v);
  check(Boolean(clicks["costs-link-task-0"]) && !clicks["costs-link-task-1"] && !clicks["costs-link-task-2"], `a link for t-1 only (have ${Object.keys(clicks).filter((k) => k.startsWith("costs-link"))})`);
  await click(v, "costs-link-task-0"); await settle();
  check(g.__shared["project-agent.show"]?.task === "t-1" && g.__shared["project-agent.show"]?.space === S, `the show request (got ${JSON.stringify(g.__shared["project-agent.show"])})`);
  look(v);
  check(v.view === "tasks" && v.open.has("t-1"), `the Tasks tab with t-1 open (view ${v.view}, open ${[...v.open]})`);
  check(!v.costs.peek(S).open.task, "the row did not toggle");
  // The By activity drill's Tasks lines link too.
  v.open.delete("t-1");
  await click(v, "view-costs"); await settle();
  await click(v, "costs-activity-0"); await settle();
  check(Boolean(costsView(v).clicks["costs-link-activity-0-0"]), `a link in the activity drill (have ${Object.keys(costsView(v).clicks).filter((k) => k.startsWith("costs-link"))})`);
  await click(v, "costs-link-activity-0-0"); await settle();
  look(v);
  check(v.view === "tasks" && v.open.has("t-1"), `opened again from the drill (view ${v.view}, open ${[...v.open]})`);
  // (no task) in the unattributed drill is not a link.
  await click(v, "view-costs"); await settle(); await click(v, "costs-activity-2"); await settle();
  check(!costsView(v).clicks["costs-link-activity-2-0"], "no link for (no task)");
};

// ---- t-151 By session type: a row opens to its sessions (task links, the drill box), or shows no fold icon ----
// The fold mark of a By session type row: "▸" / "▾" when it can open, nothing else.
const kindFold = (v, i) => { const el = costsEl(v, `costs-kind-${i}`); return el ? (/[▸▾]/.exec(inspect(el).text)?.[0] ?? "") : null; };
// The By session type section's text segments (between its head and the next section's).
const kindSegs = (v) => { const segs = costsView(v).text.split(" | "); const a = segs.indexOf("By session type"), b = segs.findIndex((x, i) => i > a && /^(Accounts|By scope|By day)/.test(x)); return segs.slice(a, b < 0 ? undefined : b); };

cases["P151-sessions"] = async () => { // a session-type row with sessions opens to them, in the drill box, in the By task drill's order; each task id is a link
  const { v } = await costsWorld();
  check(kindFold(v, 0) === "▸", `kind worker can open (got ${JSON.stringify(kindFold(v, 0))})`);
  await click(v, "costs-kind-0"); await settle();
  check(kindFold(v, 0) === "▾", "open: ▾");
  const box = costsEl(v, "costs-drill-kind"), text = inspect(box ?? {}).text;
  check(Boolean(box) && /aaaa1111/.test(text) && /bbbb2222/.test(text) && !/cccc3333/.test(text), `worker -> its two sessions in costs-drill-kind (got ${text.slice(0, 1500)})`);
  check(text.indexOf("aaaa1111") < text.indexOf("bbbb2222"), "the summary's order, as in the By task drill");
  const { clicks } = costsView(v);
  check(Boolean(clicks["costs-link-kind-0-0"]) && Boolean(clicks["costs-link-kind-0-1"]), `a task link on each session line (have ${Object.keys(clicks).filter((k) => k.startsWith("costs-link"))})`);
  check(/t-1/.test(inspect(costsEl(v, "costs-link-kind-0-0") ?? {}).text), "the link names its task");
  await click(v, "costs-link-kind-0-0"); await settle();
  check(g.__shared["project-agent.show"]?.task === "t-1" && g.__shared["project-agent.show"]?.space === S, `the show request (got ${JSON.stringify(g.__shared["project-agent.show"])})`);
  look(v);
  check(v.view === "tasks" && v.open.has("t-1"), `the Tasks tab with t-1 open (view ${v.view}, open ${[...v.open]})`);
  check(v.costs.peek(S).open.kind === "worker", "a link click does not toggle the row");
  // A session with no task is listed, without a link.
  await click(v, "view-costs"); await settle();
  await click(v, "costs-kind-1"); await settle();
  const pa = costsView(v);
  check(/cccc3333/.test(inspect(costsEl(v, "costs-drill-kind") ?? {}).text) && !pa.clicks["costs-link-kind-1-0"], `pa -> cccc3333, no link (have ${Object.keys(pa.clicks).filter((k) => k.startsWith("costs-link"))})`);
  // Many sessions of a type: the drill scrolls in its fixed-height box, every one listed.
  const s = costsSummary();
  const keys = Array.from({ length: 25 }, (_, i) => `w${String(i).padStart(3, "0")}bbbb`);
  s.sessions = keys.map((key) => ({ key, tokens: 10, money: 0.1, calls: 1, kind: "worker", role: "build", program: "claude", task: "t-1", model: "model-a", from: isoAt(T0 - 7200000), to: isoAt(T0 - 3600000), state: "closed", source: "recorded" }));
  const many = (await costsWorld({ summary: s })).v;
  await click(many, "costs-kind-0"); await settle();
  const mbox = costsEl(many, "costs-drill-kind");
  check(Boolean(mbox?.style?.overflow_y_scrollbar) && Array.isArray(mbox?.style?.h), `the drill scrolls at a fixed height (style ${JSON.stringify(mbox?.style)})`);
  check(keys.every((k) => inspect(mbox ?? {}).text.includes(k)) && Boolean(costsView(many).clicks["costs-link-kind-0-24"]), "every session listed, each with its link");
  check(moneyLabelled(many).length === 0, `labelled (${JSON.stringify(moneyLabelled(many))})`);
};

cases["P151-nofold"] = async () => { // a session-type row with nothing to show has no fold icon, and a click on it opens nothing
  const s = costsSummary();
  s.byKind = { ...s.byKind, kind: [...s.byKind.kind, { key: "helper", tokens: 5, money: 0.05, calls: 1 }], model: [{ key: "model-a", tokens: 900, money: 11, calls: 6 }, { key: "model-b", tokens: 100, money: 1.5, calls: 1 }] };
  let { v } = await costsWorld({ summary: s });
  check(kindFold(v, 0) === "▸" && kindFold(v, 1) === "▸", `worker and pa have sessions: ▸ (got ${kindFold(v, 0)}, ${kindFold(v, 1)})`);
  check(kindFold(v, 2) === "", `helper has no sessions: no fold icon (got ${JSON.stringify(kindFold(v, 2))})`);
  const before = kindSegs(v).join(" | ");
  if (costsView(v).clicks["costs-kind-2"]) { await click(v, "costs-kind-2"); await settle(); }
  check(!costsEl(v, "costs-drill-kind") && kindFold(v, 2) === "", `nothing opens under helper (got ${kindSegs(v).join(" | ")}; before ${before})`);
  // By model: model-b is no session's model (a session is listed under its main model): no fold icon.
  await click(v, "costs-dim-model"); await settle();
  check(kindFold(v, 0) === "▸" && kindFold(v, 1) === "", `model-a ▸, model-b none (got ${JSON.stringify(kindFold(v, 0))}, ${JSON.stringify(kindFold(v, 1))})`);
  // A summary whose session list was shrunk away: no session-type row shows a fold icon.
  ({ v } = await costsWorld({ summary: costsSummary({ sessions: [] }) }));
  for (const d of ["kind", "role", "program", "model", "machine"]) {
    await click(v, `costs-dim-${d}`); await settle();
    const n = d === "machine" ? 1 : costsSummary().byKind[d].length;
    const marks = Array.from({ length: n }, (_, i) => kindFold(v, i));
    check(marks.every((m) => m === ""), `${d}: no fold icons without sessions (got ${JSON.stringify(marks)})`);
  }
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
};

cases["P-costs-machine"] = async () => { // §5.1: the machine row is Herdr's machine name or "this computer", never a host key
  for (const [machine, want] of [[null, "this computer"], [M, M]]) {
    const { v } = await costsWorld({ machine });
    await click(v, "costs-dim-machine"); await click(v, "costs-kind-0");
    const { text } = costsView(v);
    check(text.includes(`${want} |`) && /12\.50/.test(text.split("By session type")[1] ?? "") && !/host-(one|two)/.test(look(v).text), `${want}, one row, no host name (got ${text.slice(0, 2500)})`);
  }
};

cases["P-costs-billed-off"] = async () => { // R14 state 1, R15: off and no rows: one line, no enable control
  const { v } = await costsWorld();
  const { text, clicks } = costsView(v);
  check(text.includes("Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README)."), `state 1 (got ${text.slice(-500)})`);
  check(!Object.keys(clicks).some((k) => /enable|billing-on/.test(k)) && !clicks["costs-billing-off"], `no enable control (got ${Object.keys(clicks)})`);
  check(JSON.stringify(costsExecs("billed compare")[0]?.[1]) === JSON.stringify(["billed", "compare", "--json", "--space", S]) && costsExecs("billed compare")[0][2] === null, "the local compare of this space");
};

cases["P-costs-billed-enabled-empty"] = async () => { // R14 state 2: on, no rows: the status line with Turn off, and no rows yet
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(noRows(statusOn({ nextAfter: "2027-01-15T09:00:00Z" }))) } });
  const { text, clicks } = costsView(v);
  check(/Billing query: on · not run yet · next after .* · cap 1\/day \(≤ 3 requests, stated fee ≈ 0\.01 XTS each\)/.test(text) && Boolean(clicks["costs-billing-off"]), `status line and Turn off (got ${text.slice(-700)})`);
  check(text.includes("No billed rows for account acct1 yet."), "no rows yet");
};

cases["P-costs-billed-data"] = async () => { // R14 state 3, AC14.1-14.4: the comparison; every billed figure says account-wide
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(withRows()) } });
  const { text } = costsView(v);
  for (const want of ["Billed (account-wide: includes use outside this space and outside Herdr)", "Covered 2027-01-10 – 2027-01-14 (5 of 6 days; not billed: 2027-01-15) · source query:q1, fetched",
    "this space", "all spaces here", "model-a", "price factor 1.25 (from quantities)", "model-b", "— est. (no price)", "scope ratio 0.8 (not applied) below 1.0: the table over-prices",
    "(from a partial estimate)", "(few days)", "(not final)", "other / unmapped labels", "total", "calibrated (est. × price factor, where one applies): this space", "+ unpriced",
    "In XXX (not compared):", "No price for model-b"]) check(text.includes(want), `shows "${want}" (got ${text.slice(text.indexOf("Billed"), text.indexOf("Billed") + 2500)})`);
  const segs = text.split(" | ").filter((x) => /billed/.test(x) && /\d\.\d\d/.test(x));
  check(segs.length >= 4 && segs.every((x) => x.includes("billed · account-wide")), `every billed figure says account-wide (got ${JSON.stringify(segs)})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
  check(/Billing query: on · last run .* \(ok, 1 requests\)/.test(text), "the status line when on");
};

cases["P-costs-billed-rejected"] = async () => { // R14 state 4: rejected rows, refused, other failure
  for (const [state, want] of [
    [{ result: "failed", lastError: "batch rejected: row 3 has no amount", lastRun: "2027-01-15T06:00:00Z", nextAfter: "2027-01-15T10:00:00Z" }, "The last query's rows were rejected; nothing was added: row 3 has no amount."],
    [{ result: "refused", lastError: "refused: cannot show the configured identity is read-only (x)" }, "Billing query refused: refused: cannot show the configured identity is read-only (x)"],
    [{ result: "failed", lastError: "the query script exited 4", nextAfter: "2027-01-15T10:00:00Z" }, "Billing query failed: the query script exited 4; next try after"]]) {
    const { v } = await costsWorld({ tool: { "billed compare": () => json0(withRows(statusOn(state))) } });
    const { text } = costsView(v);
    check(text.includes(want) && text.includes("Billed (account-wide"), `${state.result}: ${want} (got ${text.slice(-1500)})`);
  }
};

cases["P-costs-billed-refused"] = async () => { // R14 state 5: the compare call fails or times out: says so, keeps a copy
  const { v } = await costsWorld({ tool: { "billed compare": () => ({ code: 2, stdout: "", stderr: "pa-costs: no Project Agent state for this space\n" }) } });
  check(costsView(v).text.includes("Billed amounts could not be read (pa-costs: no Project Agent state for this space)"), `the error (got ${costsView(v).text.slice(-400)})`);
  let mode = "ok";
  const { v: w } = await costsWorld({ tool: { "billed compare": () => (mode === "ok" ? json0(withRows()) : new Promise(() => {})) } });
  mode = "hang"; advance(61000); w.reload(cx); await settle(); advance(36000); await settle();
  const { text } = costsView(w);
  check(/Billed amounts could not be read \(pa-costs billed did not answer in 35 s\); showing the copy read earlier\./.test(text) && text.includes("price factor 1.25"), `kept copy (got ${text.slice(-1500)})`);
};

cases["P-costs-billed-currency"] = async () => { // AC14.4: rows only in another currency; no price table
  const other = noRows(statusOn()); other.rows = 1; other.comparison.separate = [{ currency: "XXX", amount: 9, rows: 1 }];
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(other) } });
  const { text } = costsView(v);
  check(text.includes("No billed rows in XTS for account acct1.") && text.includes("In XXX (not compared):") && text.includes("billed · account-wide, 1 row(s)"), `other currency (got ${text.slice(-800)})`);
  const none = JSON.parse(JSON.stringify(other)); none.comparison.currency = null;
  const { v: w } = await costsWorld({ tool: { "billed compare": () => json0(none) } });
  check(costsView(w).text.includes("No price table, so no currency to compare: billed rows for account acct1 are not compared."), "no table");
};

cases["P-costs-billed-remote"] = async () => { // D9: a remote space: the local compare with the trimmed summary; with rows, the remote's account-wide report, then the compare with it
  const { v } = await costsWorld({ machine: M, tool: { "billed compare": (args) => json0(args.includes("--here-json") ? withRows() : { ...noRows(statusOn()), rows: 4 }),
    report: () => json0({ v: 1, account: "acct1", spaces: [S, "sp7"], byDayModel: [{ day: "2027-01-14", model: "model-a", tokens: {}, money: 20 }] }) } });
  const cmp = costsExecs("billed compare"), rep = costsExecs("report");
  check(cmp.length === 2 && cmp.every(([, , on]) => on === null), `two local compares (got ${JSON.stringify(cmp.map((x) => [x[1].slice(0, 4), x[2]]))})`);
  const arg = JSON.parse(cmp[0][1][cmp[0][1].indexOf("--summary-json") + 1]);
  check(JSON.stringify(Object.keys(arg).sort()) === JSON.stringify(["account", "byDayModel", "prices", "space", "updated", "v"]), `the trimmed summary (got ${Object.keys(arg)})`);
  check(rep.length === 1 && JSON.stringify(rep[0][1]) === JSON.stringify(["report", "--account-wide"]) && rep[0][2] === M, `the remote report (got ${JSON.stringify(rep)})`);
  check(JSON.parse(cmp[1][1][cmp[1][1].indexOf("--here-json") + 1]).spaces.includes("sp7"), "the remote's figure passed on");
  check(costsView(v).text.includes("price factor 1.25"), "the comparison shown");
  // No rows: no remote report at all.
  const { v: w } = await costsWorld({ machine: M });
  check(costsExecs("report").length === 0 && costsExecs("billed compare").length === 1 && costsView(w).text.includes("Billed amounts: none."), "no rows: no report");
};

cases["P-costs-turnoff"] = async () => { // R15, d-066: Turn off is one click: billed disable here, then the status again
  let off = false;
  const { v } = await costsWorld({ machine: M, tool: { "billed compare": () => json0(off ? noRows() : noRows(statusOn())), "billed disable": () => { off = true; return { code: 0, stdout: "Billing query: off.", stderr: "" }; } } });
  const n = costsExecs("billed compare").length;
  await click(v, "costs-billing-off"); await settle();
  const d = costsExecs("billed disable");
  check(d.length === 1 && JSON.stringify(d[0][1]) === JSON.stringify(["billed", "disable"]) && d[0][2] === null, `one click, here (got ${JSON.stringify(d)})`);
  check(costsExecs("billed compare").length === n + 1 && costsView(v).text.includes("The billing query is off"), `the status again (got ${costsView(v).text.slice(-400)})`);
};

cases["P-costs-lastquery"] = async () => { // R15: the visible last-queried line
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(withRows(statusOn({ lastRun: "2027-01-15T06:00:00Z", result: "ok", requests: 2, nextAfter: "2027-01-16T06:00:00Z" }))) } });
  const line = costsView(v).text.split(" | ").find((x) => x.startsWith("Billing query: on"));
  check(/^Billing query: on · last run .+ \(ok, 2 requests\) · next after .+ · cap 1\/day \(≤ 3 requests, stated fee ≈ 0\.01 XTS each\)$/.test(line ?? ""), `the line (got ${line})`);
  check(!/\[Turn off/.test(costsView(v).text), "the tool's own [Turn off: …] text is not shown");
};

cases["P-costs-actions"] = async () => { // R12: across every click the only pa-costs commands are these; nothing is written
  const { v } = await costsWorld({ machine: M, tool: { "billed compare": (args) => json0(args.includes("--here-json") ? withRows() : { ...noRows(statusOn()), rows: 4 }) } });
  // t-143: a task link leaves the view (P143-link); every other click here, then the links last: they run and write nothing.
  const others = () => Object.keys(costsView(v).clicks).filter((id) => !id.startsWith("costs-link-"));
  for (const id of others()) { const c = costsView(v).clicks[id]; if (c) { c({}, cx); await settle(); } }
  await click(v, "costs-reconstruct").catch(() => {});
  for (const id of others()) { const c = costsView(v).clicks[id]; if (c) { c({}, cx); await settle(); } }
  const links = Object.keys(costsView(v).clicks).filter((x) => x.startsWith("costs-link-"));
  check(links.length > 0, "a task link to click");
  for (const id of links) { await click(v, "view-costs"); await settle(); const c = costsView(v).clicks[id]; if (c) { c({}, cx); await settle(); } } // the checks below hold for them too
  const subs = [...new Set(costsExecs().map(([, a]) => (a[0] === "billed" ? `billed ${a[1]}` : a[0] === "report" ? `report ${a[1]}` : a[0])))].sort();
  const allowed = ["billed compare", "billed disable", "collect", "reconstruct", "report --account-wide"];
  check(subs.every((x) => allowed.includes(x)) && subs.includes("collect") && subs.includes("billed disable"), `only ${allowed} (got ${JSON.stringify(subs)})`);
  check(!(g.__writes ?? []).length && !g.__execs.some(([c, a]) => c === "python3" && a[0] === "-c"), `nothing written (got ${JSON.stringify(g.__writes)})`);
  check(!g.__costsUnexpected.length, `no unanswered pa-costs call (got ${JSON.stringify(g.__costsUnexpected)})`);
  // The collect's zone (t-036) is the one value besides them: a checked offset table and zone name.
  const zoneValue = (a, i) => (a[i - 1] === "--zone-offsets" && /^\d+:-?\d+(,\d+:-?\d+)*$/.test(a[i])) || (a[i - 1] === "--zone-name" && /^[A-Za-z0-9_+\-/:.]{1,64}$/.test(a[i])) || (a[i - 1] === "--zone-from" && /^\d{1,12}$/.test(a[i]));
  for (const [, a] of costsExecs()) a.forEach((x, i) => check(typeof x === "string" && (x.startsWith("{") || /^[a-z-]+$|^sp1$/.test(x) || zoneValue(a, i)), `fixed argv words only (got ${x.slice(0, 80)})`));
};

// ---- t-009 round 2 (MF-1): a run or a read that ends after the Costs view was left reads and runs nothing ----
// Leaves the Costs view: "hide" hides the panel (the reloads see it off screen), "tasks" chooses the Tasks tab.
async function leaveCosts(v, how) { if (how === "hide") g.__layout = panelLayout("changes"); else await click(v, "view-tasks"); await settle(); }
async function backToCosts(v, how) {
  if (how === "hide") { g.__layout = panelLayout("project-agent"); await fire(1000); await settle(); }
  else { await click(v, "view-costs"); await settle(); }
}
const costsSeen = () => ({ reads: costsReads().length, ls: costsLs().length, execs: costsExecs().map(([, a]) => a.slice(0, 2).join(" ")) });
async function runLeft(what, how, secs) {
  let release; const held = new Promise((r) => { release = r; });
  // Reconstruct is offered while there is no summary; it writes one (so a read after it would chain the compare).
  const done = () => held.then(() => { if (what === "reconstruct") costsFiles(costsSummary(), { machine: M }); return { code: 0, stdout: "", stderr: "" }; });
  g.__layout = panelLayout("project-agent");
  const { v } = await costsWorld({ machine: M, summary: what === "reconstruct" ? null : costsSummary(), tool: { [what]: done } });
  const button = what === "collect" ? "costs-refresh" : "costs-reconstruct";
  await click(v, button); await settle();
  await leaveCosts(v, how);
  const left = costsSeen(), readAt = Math.max(...costsReads().map((x) => x.at));
  for (let s = 1; s <= secs; s++) { advance(1000); v.reload(cx); look(v); await settle(); }
  release(); await settle(); await settle();
  for (let s = 1; s <= 2; s++) { advance(1000); v.reload(cx); look(v); await settle(); }
  const after = costsSeen();
  check(JSON.stringify(after) === JSON.stringify(left), `${what}, ${how}, ${secs} s: nothing read or run after it ends while the view is left (${JSON.stringify(left)} -> ${JSON.stringify(after)})`);
  check(costsExecs("billed compare").length === left.execs.filter((x) => x === "billed compare").length, `${what}, ${how}, ${secs} s: no billed compare while left`);
  const n = costsReads().length, at = g.__now;
  await backToCosts(v, how);
  check(costsReads().length === n + 1 && costsReads().at(-1).at === g.__now, `${what}, ${how}, ${secs} s: shown again, the summary is read at once (reads ${n} -> ${costsReads().length}; ${Math.round((at - readAt) / 1000)} s after the last read)`);
  check(!g.__costsUnexpected.length, `no unanswered pa-costs call (got ${JSON.stringify(g.__costsUnexpected)})`);
}

cases["P-costs-run-hidden"] = async () => { // MF-1: Refresh/Reconstruct ends after the panel was hidden: no read, ls or exec until shown; then a read at once
  for (const what of ["collect", "reconstruct"]) for (const secs of [3, 70]) await runLeft(what, "hide", secs);
};

cases["P-costs-run-othertab"] = async () => { // MF-1: the same with the Tasks tab chosen instead of the panel hidden
  for (const what of ["collect", "reconstruct"]) for (const secs of [3, 70]) await runLeft(what, "tasks", secs);
};

cases["P-costs-read-hidden"] = async () => { // MF-1: a summary read in flight when the view is left ends and starts no billed compare
  for (const how of ["hide", "tasks"]) {
    g.__layout = panelLayout("project-agent");
    const { v } = await costsWorld({ machine: M, open: false });
    g.__remoteDelay = 5000;
    await click(v, "view-costs"); await settle();
    check(costsReads().length === 1 && costsExecs().length === 0, `${how}: the read is in flight (reads ${costsReads().length}, execs ${JSON.stringify(costsExecs())})`);
    await leaveCosts(v, how);
    for (let s = 1; s <= 8; s++) { advance(1000); await settle(); }
    await settle();
    check(costsExecs().length === 0, `${how}: the read ended while the view was left and ran nothing (got ${JSON.stringify(costsExecs().map(([, a]) => a.slice(0, 3)))})`);
    g.__remoteDelay = 0;
  }
};

// ---- t-009 round 3 (MF-2): every step after an await asks again whether the view is shown ----
const hold = () => { let release; const p = new Promise((r) => { release = r; }); return { p, release }; };
// Reloads while the view is left (so the hidden copy is asleep), as the panel's timers would.
async function whileLeft(v, secs) { for (let s = 1; s <= secs; s++) { advance(1000); v.reload(cx); look(v); await settle(); } }
// A remote space whose first compare finds billed rows; `held` holds the first compare or the remote report.
async function chainLeft(held, how) {
  g.__layout = panelLayout("project-agent");
  const h = hold(); let first = true;
  const { v } = await costsWorld({ machine: M, open: false, tool: {
    "billed compare": (args) => {
      if (args.includes("--here-json")) return json0(withRows());
      if (held === "compare" && first) { first = false; return h.p.then(() => json0({ ...noRows(statusOn()), rows: 4 })); }
      return json0({ ...noRows(statusOn()), rows: 4 });
    },
    report: () => { const r = json0({ v: 1, account: "acct1", spaces: [S], byDayModel: [] }); if (held === "report" && first) { first = false; return h.p.then(() => r); } return r; } } });
  await click(v, "view-costs"); await settle();
  const inFlight = held === "compare" ? costsExecs("billed compare").length === 1 && costsExecs("report").length === 0 : costsExecs("report").length === 1 && costsExecs("billed compare").length === 1;
  check(inFlight, `${held}, ${how}: the ${held} is in flight (got ${JSON.stringify(costsSeen())})`);
  await leaveCosts(v, how);
  const left = costsSeen();
  await whileLeft(v, 2);
  h.release(); await settle(); await settle();
  await whileLeft(v, 3);
  const after = costsSeen();
  check(JSON.stringify(after) === JSON.stringify(left), `${held}, ${how}: no remote report or second compare after the ${held} ends while the view is left (${JSON.stringify(left)} -> ${JSON.stringify(after)})`);
  const reads = costsReads().length, reps = costsExecs("report").length, here = costsExecs("billed compare").filter(([, a]) => a.includes("--here-json")).length;
  await backToCosts(v, how); await settle(); await settle();
  check(costsReads().length === reads + 1 && costsReads().at(-1).at === g.__now, `${held}, ${how}: shown again, the summary is read at once (reads ${reads} -> ${costsReads().length})`);
  const here2 = costsExecs("billed compare").filter(([, a]) => a.includes("--here-json")).length;
  check(costsExecs("report").length === reps + 1 && here2 === here + 1, `${held}, ${how}: then the full chain runs (reports ${reps} -> ${costsExecs("report").length}, --here-json compares ${here} -> ${here2})`);
  check(/Billed \(account-wide/.test(costsView(v).text), `${held}, ${how}: the comparison is shown (got ${costsView(v).text.slice(-300)})`);
  check(!g.__costsUnexpected.length, `no unanswered pa-costs call (got ${JSON.stringify(g.__costsUnexpected)})`);
}

cases["P-costs-chain-left"] = async () => { // MF-2 (a), A6: the first compare of a remote space ends after the view was left
  for (const how of ["hide", "tasks"]) await chainLeft("compare", how);
};

cases["P-costs-chain-report-left"] = async () => { // MF-2 (a), A7: the remote report ends after the view was left: no second compare
  for (const how of ["hide", "tasks"]) await chainLeft("report", how);
};

cases["P-costs-ls-left"] = async () => { // MF-2 (b), B1: no summary on a slow remote; the read ends after the view was left: no ls
  for (const how of ["hide", "tasks"]) {
    g.__layout = panelLayout("project-agent");
    const { v } = await costsWorld({ machine: M, summary: null, open: false });
    g.__remoteDelay = 3000;
    await click(v, "view-costs"); await settle();
    check(costsReads().length === 1 && costsLs().length === 0, `${how}: the read is in flight (reads ${costsReads().length}, ls ${costsLs().length})`);
    await leaveCosts(v, how);
    const left = costsSeen();
    for (let s = 1; s <= 8; s++) { advance(1000); await settle(); }
    await settle();
    check(costsLs().length === 0 && JSON.stringify(costsSeen()) === JSON.stringify(left), `${how}: the null read ended while the view was left: no ls (${JSON.stringify(left)} -> ${JSON.stringify(costsSeen())})`);
    g.__remoteDelay = 0;
    await whileLeft(v, 1);
    const reads = costsReads().length;
    await backToCosts(v, how); await settle(); await settle();
    check(costsReads().length === reads + 1 && costsLs().length === 1, `${how}: shown again, read and ls at once (reads ${reads} -> ${costsReads().length}, ls ${costsLs().length})`);
    check(costsView(v).text.includes("No cost records yet"), `${how}: the empty state is drawn (got ${costsView(v).text.slice(0, 300)})`);
  }
};

cases["P-costs-run-otherspace"] = async () => { // RL-V5 N11: Refresh on A ends while space B is active: no read of A; A active again: read at once
  g.__layout = panelLayout("project-agent");
  const h = hold();
  const { v } = await costsWorld({ machine: M, tool: { collect: () => h.p.then(() => ({ code: 0, stdout: "", stderr: "" })) } });
  await click(v, "costs-refresh"); await settle();
  g.__spaces.push({ id: "sp2", name: "other", machine: null, folder: "/other", active: false, tabs: [{ id: "tbx", index: 1, name: "1", named: false }] });
  show("sp2");
  const aReads = () => costsReads().filter((x) => x.on === M).length, before = aReads(), cmp = costsExecs("billed compare").length;
  h.release(); await settle(); await settle();
  check(aReads() === before && costsExecs("billed compare").length === cmp, `no read or compare of A after its run ends while B is active (reads ${before} -> ${aReads()})`);
  show(RS); v.reload(cx); await settle();
  check(aReads() === before + 1, `A active again: read at once (reads ${before} -> ${aReads()})`);
};

cases["P-costs-stale-once"] = async () => { // RL-V5 N4: a stale space is reread once when shown, then at most once per COSTS_EVERY
  g.__layout = panelLayout("project-agent");
  const h = hold();
  const { v } = await costsWorld({ tool: { collect: () => h.p.then(() => ({ code: 0, stdout: "", stderr: "" })) } });
  await click(v, "costs-refresh"); await settle();
  await leaveCosts(v, "hide"); await whileLeft(v, 1);
  h.release(); await settle(); await settle();
  const n = costsReads().length;
  await backToCosts(v, "hide");
  const n1 = costsReads().length;
  await whileLeft(v, 8); // shown now: the same reloads, inside COSTS_EVERY of that read
  check(n1 === n + 1 && costsReads().length === n1, `one reread when shown, then none inside COSTS_EVERY (${n} -> ${n1} -> ${costsReads().length})`);
};

cases["P-costs-turnoff-left"] = async () => { // RL-V5 N7: Turn off ends while Tasks is chosen: nothing while left; the Costs click compares at once
  g.__layout = panelLayout("project-agent");
  const h = hold(); let off = false;
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(off ? noRows() : noRows(statusOn())), "billed disable": () => h.p.then(() => { off = true; return { code: 0, stdout: "off", stderr: "" }; }) } });
  await click(v, "costs-billing-off"); await settle();
  await leaveCosts(v, "tasks");
  const left = costsSeen();
  h.release(); await settle(); await settle();
  await whileLeft(v, 3);
  check(JSON.stringify(costsSeen()) === JSON.stringify(left), `nothing while left (${JSON.stringify(left)} -> ${JSON.stringify(costsSeen())})`);
  const n = costsExecs("billed compare").length;
  await backToCosts(v, "tasks");
  check(costsExecs("billed compare").length === n + 1 && costsView(v).text.includes("The billing query is off"), `Costs chosen again: the status compare at once (compares ${n} -> ${costsExecs("billed compare").length})`);
};

cases["P-costs-currency-fallback"] = async () => { // K6: a currency code Intl does not take: "12.50 CODE"
  const s = costsSummary({ prices: { profile: "test", source: "test table", asOf: "2027-01-01", currency: "XTSX", missing: [] } });
  const { v } = await costsWorld({ summary: s });
  check(costsView(v).text.includes("12.50 XTSX est."), `the fallback (got ${costsView(v).text.slice(0, 800)})`);
};

// ---- t-029 Job 2 (R7): Claude Code's own figure (CC) beside est.; only where the summary carries totals.cc ----
// The summary's CC fields as Job 1's summarize writes them: totals.cc, sessions[].cc/ccAt, byTask[].cc/ccSessions/ccOf.
function ccSummary(base = costsSummary(), cc = {}) {
  const s = JSON.parse(JSON.stringify(base));
  s.totals.cc = { money: 9.4, sessions: 2, claudeSessions: 3, allSessions: 3, unmatched: { sessions: 0, money: null }, ccCurrency: "USD", ...cc };
  if (s.totals.cc.money !== null) {
    Object.assign(s.sessions[0], { cc: 7.8, ccAt: "2027-01-14T10:00:00Z" });
    Object.assign(s.sessions[2], { cc: 1.6, ccAt: isoAt(T0 - 3600000) });
    Object.assign(s.byTask[2], { cc: 1.6, ccSessions: 1, ccOf: 1 });
  }
  return s;
}
// The no-price shapes of P-costs-noprice: no price table (none), or a table without these models (missing).
function noPriceSummary(shape) {
  const s = costsSummary({ prices: shape === "none" ? { profile: "test", source: null, asOf: null, currency: null, missing: ["model-a"] } : { profile: "test", source: "test table", asOf: "2027-01-01", currency: CUR, missing: ["model-a"] } });
  s.totals = { ...s.totals, money: null, reconstructedMoney: null, unpricedTokens: 1000 };
  for (const e of [...s.byActivity, ...s.byTask, ...s.byDay, ...s.sessions, ...s.byKind.machine, ...s.byKind.kind]) e.money = null;
  for (const e of s.byActivity) for (const x of [...(e.tasks ?? []), ...(e.reasons ?? [])]) x.money = null;
  for (const e of s.byTask) for (const x of e.phases ?? []) x.money = null; // the task drill's phases (opened here)
  s.hours = [[isoAt(T0).slice(0, 13), { tokens: 100, money: null }]]; s.passes[0].money = null;
  for (const d of s.byDay) d.byActivity = {};
  return s;
}
const segsOf = (v) => costsView(v).text.split(" | ");
// Every segment with an amount says what it is: est., billed, a fee, a factor, or Claude Code's (CC) figure.
const ccLabelled = (v) => segsOf(v).filter((x) => /\d\.\d\d\b|<0\.01/.test(x) && !/est\.|billed|fee|factor|ratio|\bCC \d|Claude Code: \d[\d.]* USD \(list price\)|USD list price/.test(x));
const LEGEND = "CC = Claude Code's own list-price estimate (USD), per session; not the bill";
const COVER = "Claude Code's figure covers sessions started from 2027-01-14; older, Codex and killed-at-the-end sessions have none or a partial one.";

cases["P-costs-cc-priced"] = async () => { // AC7.1, AC7.2, Q2: est. first; the CC line in the Total tile; CC on session lines and task rows; the legend
  const { v } = await costsWorld({ summary: ccSummary() });
  let segs = segsOf(v);
  const total = segs.indexOf("Total");
  check(total >= 0 && /^XTS\s12\.50 est\.$/.test(segs[total + 1]) && segs[total + 2] === "1k tokens · 94% cache read" && segs[total + 3] === "Claude Code: 9.40 USD (list price) · 2 of 3 Claude sessions",
    `the Total tile: est., its tokens, then Claude Code's line (got ${JSON.stringify(segs.slice(total, total + 5))})`);
  check(segs.includes(LEGEND), `the legend (got ${segs.join(" | ").slice(0, 2500)})`);
  check(segs.includes("CC 1.60 (1 of 1 session)") && segs.filter((x) => /^CC \d/.test(x)).length === 1, `a CC cell on the (no task) row only (t-1 is split; got ${JSON.stringify(segs.filter((x) => /CC/.test(x)))})`);
  const noTask = segs.indexOf("(no task)");
  check(/^XTS\s1\.00 est\. · 8%$/.test(segs[noTask + 2]) && segs[noTask + 3] === "CC 1.60 (1 of 1 session)", `est. then CC on the row (got ${JSON.stringify(segs.slice(noTask, noTask + 4))})`);
  await click(v, "costs-task-0"); await settle();
  segs = segsOf(v);
  check(segs.some((x) => /^◌ aaaa1111 · .* · XTS\s7\.50 est\. · CC 7\.80 · closed · reconstructed$/.test(x)), `a session line with CC after est. (got ${JSON.stringify(segs.filter((x) => /aaaa|bbbb/.test(x)))})`);
  check(segs.some((x) => /^bbbb2222 · .* · XTS\s4\.00 est\. · gone · recorded$/.test(x)), `a session without a figure: no CC, no "CC —" (got ${JSON.stringify(segs.filter((x) => /bbbb/.test(x)))})`);
  await click(v, "costs-dim-kind"); await click(v, "costs-kind-1"); await settle();
  check(segsOf(v).some((x) => /^cccc3333 · .* · XTS\s1\.00 est\. · CC 1\.60 · open · recorded$/.test(x)), "the session-type drill too");
  check(ccLabelled(v).length === 0, `every amount labelled (unlabelled: ${JSON.stringify(ccLabelled(v))})`);
  check(/Costs · [^|]*12\.50 est\. \|/.test(look(v).text), "the tab still shows est.");
  check(!segsOf(v).some((x) => /17\.20|21\.90|13\.90/.test(x)), "CC is never added to est. (12.50 + 9.40, 7.50 + 7.80, …)");
};

cases["P-costs-cc-noprice"] = async () => { // AC7.1, answer 2: no est. -> the Total tile's money is Claude Code's, never "est."; no price is never 0.00
  for (const shape of ["none", "missing"]) {
    const { v } = await costsWorld({ summary: ccSummary(noPriceSummary(shape)) });
    await click(v, "costs-task-0"); await settle();
    const segs = segsOf(v), total = segs.indexOf("Total");
    check(segs[total + 1] === "Claude Code: 9.40 USD (list price)" && segs[total + 2] === "1k tokens · 94% cache read" && segs[total + 3] === "Claude Code's figure: 2 of 3 Claude sessions",
      `${shape}: the tile's money is Claude Code's (got ${JSON.stringify(segs.slice(total, total + 5))})`);
    check(!segs.some((x) => /\d\.\d\d/.test(x) && !/Claude Code|\bCC \d/.test(x)), `${shape}: no amount but Claude Code's (got ${JSON.stringify(segs.filter((x) => /\d\.\d\d/.test(x)))})`);
    check(!segs.some((x) => /\b0\.00\b/.test(x)), `${shape}: nothing reads 0.00`);
    check(segs.some((x) => /^◌ aaaa1111 · .* · — est\. · CC 7\.80 · closed/.test(x)), `${shape}: a session line (got ${JSON.stringify(segs.filter((x) => /aaaa/.test(x)))})`);
    check(segs.includes(LEGEND) && ccLabelled(v).length === 0, `${shape}: legend, labelled (${JSON.stringify(ccLabelled(v))})`);
    if (shape === "none") check(look(v).text.includes("Costs · 1k tokens · no price"), "the tab is today's");
  }
};

cases["P-costs-cc-nofigure"] = async () => { // AC7.1: records but no figure joined yet: "no figure", never 0.00; no CC on rows
  for (const base of [costsSummary(), noPriceSummary("none")]) {
    const { v } = await costsWorld({ summary: ccSummary(base, { money: null, sessions: 0 }) });
    const segs = segsOf(v), total = segs.indexOf("Total");
    const figure = base.totals.money === null ? "1k tokens" : null;
    check((figure ? segs[total + 1] === figure : /^XTS\s12\.50 est\.$/.test(segs[total + 1])) && segs[total + 3] === "Claude Code: no figure · 0 of 3 Claude sessions",
      `the tile (got ${JSON.stringify(segs.slice(total, total + 5))})`);
    check(!segs.some((x) => /\bCC \d|0\.00 USD/.test(x)), `no CC amount anywhere (got ${JSON.stringify(segs.filter((x) => /CC|USD/.test(x)))})`);
    check(segs.includes("Claude Code's figure covers none of these sessions yet; older, Codex and killed-at-the-end sessions have none or a partial one."), `the coverage note (got ${segs.join(" | ").slice(0, 2500)})`);
  }
};

cases["P-costs-cc-coverage"] = async () => { // AC7.3: the note only when fewer sessions have the figure than Claude sessions; from the first ccAt day
  let { v } = await costsWorld({ summary: ccSummary() });
  check(segsOf(v).includes(COVER), `the note (got ${segsOf(v).join(" | ").slice(0, 2500)})`);
  ({ v } = await costsWorld({ summary: ccSummary(costsSummary(), { sessions: 3 }) }));
  check(!segsOf(v).some((x) => /covers/.test(x)), `complete: no note (got ${JSON.stringify(segsOf(v).filter((x) => /covers/.test(x)))})`);
  check(segsOf(v).includes("Claude Code: 9.40 USD (list price) · 3 of 3 Claude sessions"), "the count");
};

cases["P-costs-cc-unmatched"] = async () => { // AC7.4: unmatched figures: one dim line with count and sum; none at 0
  let { v } = await costsWorld({ summary: ccSummary(costsSummary(), { unmatched: { sessions: 2, money: 3.1 } }) });
  check(segsOf(v).includes("Claude Code figures matching no session (not in the totals): 2, 3.10 USD list price"), `the line (got ${segsOf(v).join(" | ").slice(0, 2500)})`);
  check(segsOf(v).includes("Claude Code: 9.40 USD (list price) · 2 of 3 Claude sessions"), "not added to the total");
  ({ v } = await costsWorld({ summary: ccSummary() }));
  check(!segsOf(v).some((x) => /matching no session/.test(x)), "none: no line");
};

cases["P-costs-cc-absent"] = async () => { // AC7.5: without totals.cc nothing changes, even with stray cc fields on rows
  const plain = await costsWorld();
  await click(plain.v, "costs-task-2"); await settle();
  const want = costsView(plain.v).text;
  clipReset(); await press(plain.v, "costs-copy-totals"); const wantCopy = lastClip();
  clipReset(); await press(plain.v, "costs-copy-task"); const wantTask = lastClip();
  const s = costsSummary();
  Object.assign(s.sessions[2], { cc: 1.6, ccAt: isoAt(T0) }); Object.assign(s.byTask[2], { cc: 1.6, ccSessions: 1, ccOf: 1 });
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-task-2"); await settle();
  const got = costsView(v).text;
  check(got === want, `the same text (got ${got.slice(0, 2500)})`);
  check(!/Claude Code|\bCC\b/.test(got), "no CC text");
  clipReset(); await press(v, "costs-copy-totals");
  check(lastClip() === wantCopy && lastClip().split("\n").length === 4, `the totals Copy is today's (got ${JSON.stringify(lastClip())})`);
  clipReset(); await press(v, "costs-copy-task");
  check(lastClip() === wantTask, `the task Copy is today's (got ${JSON.stringify(lastClip())})`);
};

cases["P-costs-cc-copy"] = async () => { // the Copy of the totals and of the task section carry the CC figures as drawn; new text is selectable; Copy runs nothing
  const { v } = await costsWorld({ summary: ccSummary(costsSummary(), { unmatched: { sessions: 2, money: 3.1 } }) });
  await click(v, "costs-task-2"); await settle();
  const execs = g.__execs.length;
  clipReset(); await press(v, "costs-copy-totals");
  let lines = lastClip().split("\n");
  check(/^Total: XTS\s12\.50 est\. · 1k tokens · 94% cache read$/.test(lines[1]) && lines[2] === "Claude Code: 9.40 USD (list price) · 2 of 3 Claude sessions" && lines[3].startsWith("Today: ") && lines[4].startsWith("Last 7 days (UTC): "),
    `the tiles with Claude Code's line (got ${JSON.stringify(lines)})`);
  for (const want of [LEGEND, COVER, "Claude Code figures matching no session (not in the totals): 2, 3.10 USD list price"]) check(lines.includes(want), `"${want}" copied (got ${JSON.stringify(lines)})`);
  const shown = costsView(v).text;
  for (const l of lines.slice(2)) if (!/^(Today|Last 7)/.test(l)) check(shown.includes(l), `"${l}" is drawn`);
  clipReset(); await press(v, "costs-copy-task");
  lines = lastClip().split("\n");
  check(lines.some((l) => /^\(no task\) · 100 tokens · XTS\s1\.00 est\. · 8% · CC 1\.60 \(1 of 1 session\)$/.test(l)), `the row with its CC cell (got ${JSON.stringify(lines)})`);
  check(lines.some((l) => /^  cccc3333 · .* · XTS\s1\.00 est\. · CC 1\.60 · open · recorded$/.test(l)), `the open session line (got ${JSON.stringify(lines)})`);
  check(g.__execs.length === execs, "Copy runs nothing");
  const texts = safeViews(v.render(cx), "costs").map((x) => x.text);
  for (const want of ["Claude Code: 9.40 USD (list price) · 2 of 3 Claude sessions", LEGEND, COVER]) check(texts.includes(want), `"${want}" is selectable (have ${JSON.stringify(texts)})`);
  check(new Set(safeViews(v.render(cx), "costs").map((x) => x.id)).size === texts.length, "ids unique");
};

cases["P-costs-cc-hidden"] = async () => { // R11 holds with CC: hidden, nothing is read or run; the CC figures add no read and no exec
  const { v } = await costsWorld({ summary: ccSummary() });
  const before = { reads: costsReads().length, execs: costsExecs().length, ls: costsLs().length };
  check(before.reads === 1 && costsReads().every((x) => /\/costs\/summary\.json$/.test(x.path)), `shown: the summary only (got ${JSON.stringify(costsReads())})`);
  g.__layout = panelLayout("changes");
  for (let s = 1; s <= 60; s++) { advance(1000); for (const tm of g.__timers) if (s * 1000 % tm.ms === 0) tm.fn(cx); if (s % 3 === 0) v.reload(cx); look(v); await settle(); }
  check(costsReads().length === before.reads && costsExecs().length === before.execs && costsLs().length === before.ls, `hidden: nothing (${JSON.stringify(before)} -> ${costsReads().length}, ${costsExecs().length}, ${costsLs().length})`);
};

// ---- t-009 Costs view (step 10): the timer's collection and the scheduled billing query (R6, R11, R15) ----
const timerMod = async () => import(path.join(dst, "timer.js"));
const fetchOk = () => ({ code: 0, stdout: "", stderr: "" });
// A Project Agent record for sp1 (or m1/sp1) that the keeper ("K", the lease holder) has judged; alive: its launcher runs.
function timerWorld({ machine = null, alive: on = false, tool = {} } = {}) {
  world({ machine, records: { tb1: { space: machine ? `${machine}/${S}` : S, project: "/proj", profile: "test", name: "proj", terminal: "t4w", alive: on } },
    extraTabs: [paTab], extraPanes: on ? [paPane()] : [], extraTerms: on ? [{ ...paTerm(), status: "idle" }] : [], agents: on ? [{ address: "t4w", status: "idle" }] : [] });
  costsTool({ "billed fetch": fetchOk, ...tool });
}
const collects = () => costsExecs("collect");
async function tickAt(tick, ms, id = "K") { g.__now = ms; for (const k of ["project-agent.timer", "project-agent.keeper"]) if (g.__shared[k]?.id === "K") g.__shared[k] = { id: "K", at: ms }; await tick(id); await settle(); }

cases["K-costs-collect"] = async () => { // R11, D6: one collect --quiet per space, also for a record that is not alive; none again within 30 min
  timerWorld();
  const { tick } = await timerMod();
  await tickAt(tick, T0);
  const c = collects();
  check(c.length === 1 && JSON.stringify(c[0][1]) === JSON.stringify(["collect", "--space", S, "--quiet", ...UTC_ARGS]) && c[0][2] === null, `one collect for the not-alive record (got ${JSON.stringify(c)})`);
  for (let t = 15000; t < 1800000; t += 15000) await tickAt(tick, T0 + t);
  check(collects().length === 1, `none again within COLLECT_SLOW (got ${collects().length})`);
  await tickAt(tick, T0 + 1800000);
  check(collects().length === 2, `again after 30 min (got ${collects().length})`);
  check(!g.__hcs.some((a) => a[0] === "send"), "nothing typed into a terminal");
};

cases["K-costs-fast"] = async () => { // Q1 (b): while some copy shows this space's Costs view, every 120 s
  timerWorld();
  const { tick } = await timerMod();
  for (let t = 0; t <= 240000; t += 15000) {
    g.__shared["project-agent.costsShown"] = { [S]: T0 + t };
    await tickAt(tick, T0 + t);
    if (t === 105000) check(collects().length === 1, `not before 120 s (got ${collects().length})`);
    if (t === 120000) check(collects().length === 2, `again at 120 s (got ${collects().length})`);
  }
  check(collects().length === 3, `every 120 s (got ${collects().length})`);
  // A stamp older than 30 s does not count: back to 30 min.
  g.__shared["project-agent.costsShown"] = { [S]: T0 + 240000 };
  for (let t = 255000; t <= 600000; t += 15000) await tickAt(tick, T0 + t);
  check(collects().length === 3, `a stale stamp: the slow cadence (got ${collects().length})`);
};

cases["K-costs-noawait"] = async () => { // R11, VP4: a hung collect never delays a pass, and no second one while it hangs (< 90 s)
  timerWorld({ alive: true });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15, lastPass: isoAt(T0 - 20 * 60000), onlyWhileActive: false });
  g.__execHang = [":pa-costs"];
  const { tick } = await timerMod();
  let done = false;
  tick("K").then(() => { done = true; });
  await settle();
  check(done, "the tick ends while the collect hangs");
  check(g.__hcs.some((a) => a[0] === "send" && a[1] === "t4w" && a[2] === "scheduled pass"), `the due pass is sent in the same tick (got ${JSON.stringify(g.__hcs)})`);
  const hung = () => g.__execs.filter(([c, a]) => c === "pa-costs" && a[0] === "collect").length;
  check(hung() === 1, `one collect (got ${hung()})`);
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15, lastPass: isoAt(T0), onlyWhileActive: false });
  g.__shared["project-agent.costsShown"] = { [S]: T0 + 75000 };
  for (let t = 15000; t <= 75000; t += 15000) await tickAt(tick, T0 + t);
  check(hung() === 1, `no second collect while the first hangs (got ${hung()})`);
  for (let t = 90000; t <= 135000; t += 15000) { g.__shared["project-agent.costsShown"] = { [S]: T0 + t }; await tickAt(tick, T0 + t); }
  check(hung() === 2, `one older than 90 s counts as gone (got ${hung()})`);
};

cases["K-costs-remote"] = async () => { // R6, AC6.1: a remote space's collect runs on its machine with its local id
  timerWorld({ machine: M });
  const { tick } = await timerMod();
  await tickAt(tick, T0);
  const c = collects();
  check(c.length === 1 && c[0][2] === M && JSON.stringify(c[0][1]) === JSON.stringify(["collect", "--space", S, "--quiet", ...UTC_ARGS]), `on m1, --space sp1 (got ${JSON.stringify(c)})`);
  const f = costsExecs("billed fetch");
  check(f.length === 1 && f[0][2] === null, `the billing query only here (got ${JSON.stringify(f)})`);
};

cases["K-costs-error"] = async () => { // a failed collect: shared project-agent.costs says why, the view shows it; a missing tool: the install text
  timerWorld({ tool: { collect: () => ({ code: 2, stdout: "", stderr: "pa-costs: the price table is not valid JSON\nmore\n" }) } });
  const { tick } = await timerMod();
  await tickAt(tick, T0);
  const rec = (g.__shared["project-agent.costs"] ?? {})[S];
  check(rec && rec.ok === false && rec.code === 2 && rec.error === "pa-costs: the price table is not valid JSON", `the record (got ${JSON.stringify(g.__shared["project-agent.costs"])})`);
  g.__files[`:${DIR}/costs/summary.json`] = JSON.stringify(costsSummary());
  const v = await mount(); await settle(); await click(v, "view-costs"); await settle();
  check(/Collector: last run .* failed: pa-costs: the price table is not valid JSON/.test(costsView(v).text), `the view says so (got ${costsView(v).text.slice(0, 800)})`);
  const before = JSON.stringify(g.__shared["project-agent.costs"]);
  await tickAt(tick, T0 + 1800000);
  check(JSON.stringify(g.__shared["project-agent.costs"]) === before, "the same outcome is not written again");
  timerWorld({ tool: { collect: () => { throw new Error("spawn pa-costs ENOENT"); } } });
  await tickAt(tick, T0 + 3600000 * 5);
  check((g.__shared["project-agent.costs"] ?? {})[S]?.missing === true, `not installed (got ${JSON.stringify(g.__shared["project-agent.costs"])})`);
  g.__files[`:${DIR}/costs/summary.json`] = JSON.stringify(costsSummary());
  const w = await mount(); await settle(); await click(w, "view-costs"); await settle();
  check(costsView(w).text.includes("pa-costs is not installed here; run ./install.sh."), `the install text (got ${costsView(w).text.slice(0, 800)})`);
  timerWorld({ tool: { collect: () => ({ code: 0, stdout: "", stderr: "" }) } });
  g.__shared["project-agent.costs"] = { [S]: { at: isoAt(T0), ok: false, code: 2, error: "x" } };
  await tickAt(tick, T0 + 3600000 * 9);
  check((g.__shared["project-agent.costs"] ?? {})[S]?.ok === true, "a good run clears it");
};

cases["K-billing-tick"] = async () => { // R15: the lease holder only, here, billed fetch --scheduled --quiet at most once an hour (a shared stamp)
  timerWorld();
  const { tick } = await timerMod();
  await tickAt(tick, T0);
  let f = costsExecs("billed fetch");
  check(f.length === 1 && JSON.stringify(f[0][1]) === JSON.stringify(["billed", "fetch", "--scheduled", "--quiet"]) && f[0][2] === null, `one scheduled fetch here (got ${JSON.stringify(f)})`);
  for (let t = 15000; t < 3600000; t += 15000) await tickAt(tick, T0 + t);
  check(costsExecs("billed fetch").length === 1, `none again within the hour (got ${costsExecs("billed fetch").length})`);
  await tickAt(tick, T0 + 3600000);
  check(costsExecs("billed fetch").length === 2, "again after an hour");
  // A copy without the lease runs none; one that takes the lease over inside the hour runs none either.
  await tickAt(tick, T0 + 3600000 + 7200000 - 1, "X");
  check(costsExecs("billed fetch").length === 2, "a copy without the lease: none");
  g.__shared["project-agent.timer"] = { id: "K", at: T0 + 3600000 - 60000 };
  g.__now = T0 + 3600000 + 1800000;
  await tick("K2"); await settle();
  check(g.__shared["project-agent.timer"]?.id === "K2", "K2 took the lease");
  check(costsExecs("billed fetch").length === 2, `a takeover inside the hour runs none (got ${costsExecs("billed fetch").length})`);
};

// ---- t-022: a cx is valid only for the call that gave it (gpui-kit.d.ts Context: "a cx kept across [an
// await] reports a stale-context error"). callCx() models Herdr's: a cx for one call whose spawn, notify and
// timers throw "stale context" once end() is called. staleClick() renders with one (ended when render
// returns, so a callback render built closes over a dead cx) and calls the handler with another, ended
// when the handler returns. Work the handler spawns gets the world's task cx, live as Herdr's AsyncContext. ----
function callCx() {
  let live = true;
  const base = cx;
  const dead = (what) => { throw new Error(`stale context: ${what} on a cx kept past its call`); };
  const c = { ...base, spawn(f) { if (!live) dead("spawn"); return base.spawn(f); }, notify() { if (!live) dead("notify"); return base.notify(); },
    timer: { every(...a) { if (!live) dead("timer.every"); return base.timer.every(...a); }, after(...a) { if (!live) dead("timer.after"); return base.timer.after(...a); } } };
  return { cx: c, end() { live = false; } };
}
const staleLook = (v) => { const r = callCx(); try { return inspect(v.render(r.cx)); } finally { r.end(); } };
async function staleClick(v, id) {
  const { clicks } = staleLook(v);
  check(Boolean(clicks[id]), `a click target ${id} (have ${Object.keys(clicks).join(", ")})`);
  const c = callCx(); try { clicks[id]({}, c.cx); } finally { c.end(); }
  await settle();
}
const untold = (v) => /hasn't been told yet/.test(look(v).text);
const stuck = (v) => `sending ${v.sending}, unsent ${JSON.stringify(v.unsent)}, rejected ${JSON.stringify(g.__rejected)}`;
const pushWant = `decision ${PID} answered Push; result: pushed ${PURL} main aa22000..cc11000 (2 commits)`;
const live4w = () => ({ records: alive(), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "idle" }] });

cases["C22-push-push"] = async () => { // a push card answered Push: its notice reaches the Project Agent; the line clears
  const { v } = await pushWorld({ opts: live4w() });
  await staleClick(v, `opt-${PID}-0`);
  await staleClick(v, `submit-${PID}`);
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", pushWant]]), `the notice is sent once (got ${JSON.stringify(sends())}; ${stuck(v)})`);
  check(v.sending === false && v.unsent.length === 0, `nothing left sending or queued (${stuck(v)})`);
  check(!untold(v), `no "hasn't been told yet" line (got ${look(v).text.slice(0, 600)})`);
};

cases["C22-push-notnow"] = async () => { // a push card answered Not now, with a note: its notice reaches the Project Agent
  const { v } = await pushWorld({ opts: live4w(), tool: { run: () => json(0, { ok: true, done: false, answer: "Not now", refusal: null, result: null, message: "answered Not now" }) } });
  v.notes[PKEY].set("after the release");
  await staleClick(v, `opt-${PID}-1`);
  await staleClick(v, `submit-${PID}`);
  const ran = pushExecs("run")[0]?.[1] ?? [];
  check(ran.includes("Not now") && ran.includes("after the release"), `ran Not now with the note (got ${JSON.stringify(ran)})`);
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", `decision ${PID} answered Not now`]]), `the notice is sent once (got ${JSON.stringify(sends())}; ${stuck(v)})`);
  check(v.sending === false && !untold(v), `the line clears (${stuck(v)})`);
};

cases["C22-after-push"] = async () => { // after a push card, an ordinary card's and a worktree card's answers are still sent
  world(live4w());
  pushFiles();
  card("d-x");
  g.__files[`:${DIR}/decisions/d-wt-t-1.json`] = JSON.stringify({ id: "d-wt-t-1", task: "t-1", status: "open", question: "Delete the worktree?",
    options: [{ label: "Delete" }, { label: "Keep" }], created: "2026-11-16T10:00:00Z" });
  g.__ls[`:${DIR}/decisions`] = [`${PID}.json`, "d-x.json", "d-wt-t-1.json"];
  pushTool();
  const v = await mount(); await settle();
  await staleClick(v, `opt-${PID}-0`); await staleClick(v, `submit-${PID}`);
  await staleClick(v, "opt-d-x-0"); await staleClick(v, "submit-d-x");
  await staleClick(v, "opt-d-wt-t-1-0"); await staleClick(v, "submit-d-wt-t-1");
  await fire(10000);
  check(JSON.parse(g.__files[`:${DIR}/decisions/d-wt-t-1.json`]).status === "answered", "the worktree card is answered");
  const want = [pushWant, "decision d-x answered", "decision d-wt-t-1 answered"];
  check(JSON.stringify(sends().map((x) => x[2])) === JSON.stringify(want), `all three sent once, in order, within one 10 s tick (got ${JSON.stringify(sends())}; ${stuck(v)})`);
  check(v.sending === false && !untold(v), `the line clears (${stuck(v)})`);
};

cases["C22-resend-throws"] = async () => { // a send that throws once is retried, and the sending mark is not left set
  const v = await cardsWorld(live4w(), []);
  v.unsent = [{ spaceId: S, message: "m1" }];
  const bad = { ...cx, spawn() { throw new Error("stale context"); } };
  try { v.resend(bad); } catch {}
  check(v.sending !== true, `a spawn that throws leaves sending clear (${stuck(v)})`);
  check(JSON.stringify(v.unsent.map((x) => x.message)) === JSON.stringify(["m1"]), `the notice is kept (${stuck(v)})`);
  await fire(10000);
  check(JSON.stringify(sends().map((x) => x[2])) === JSON.stringify(["m1"]), `sent at the next 10 s tick (got ${JSON.stringify(sends())}; ${stuck(v)})`);
  check(!untold(v), "the line clears");
  // A throw inside the send task (here the target lookup, once): the mark is cleared and the notice kept.
  g.__hcs = [];
  v.unsent = [{ spaceId: S, message: "m2" }];
  const tabs = g.__shared["project-agent.tabs"];
  Object.defineProperty(g.__shared, "project-agent.tabs", { configurable: true, enumerable: true, get() {
    Object.defineProperty(g.__shared, "project-agent.tabs", { configurable: true, enumerable: true, writable: true, value: tabs });
    throw new Error("lookup failed once");
  } });
  v.resend(cx); await settle();
  g.__rejected = g.__rejected.filter((m) => !/lookup failed once/.test(m)); // this case's own throw, not a stale cx
  check(v.sending === false, `a throw in the task leaves sending clear (${stuck(v)})`);
  check(JSON.stringify(v.unsent.map((x) => x.message)) === JSON.stringify(["m2"]), `the notice is kept (${stuck(v)})`);
  await fire(10000);
  check(JSON.stringify(sends().map((x) => x[2])) === JSON.stringify(["m2"]), `sent at the next 10 s tick (got ${JSON.stringify(sends())}; ${stuck(v)})`);
};

cases["C22-line-clears"] = async () => { // a push notice whose send fails: the line says why; delivered next tick, it clears
  const { v } = await pushWorld({ opts: live4w() });
  g.__hcFail = ["t4w"];
  await staleClick(v, `opt-${PID}-0`);
  await staleClick(v, `submit-${PID}`);
  check(/hasn't been told yet \(refused\)\. Trying again every 10 s\./.test(look(v).text), `the line names the failure (got ${look(v).text.slice(0, 600)}; ${stuck(v)})`);
  g.__hcFail = []; g.__hcs = [];
  await fire(10000);
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", pushWant]]), `delivered at the next tick (got ${JSON.stringify(sends())}; ${stuck(v)})`);
  check(!untold(v) && v.sending === false, `the line is gone (${stuck(v)})`);
};

cases["C22-rules-view"] = async () => { // Approve in the Rules view (render's cx long gone): its card is answered and the Project Agent told
  world(live4w());
  g.__files[RULES] = JSON.stringify({ rules: [{ ...rule("r-p", "Proposed"), status: "proposed", source: "agent" }] });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  g.__files[`:${DIR}/decisions/d-r.json`] = JSON.stringify({ id: "d-r", status: "open", rule: "r-p", question: "Adopt rule r-p?",
    options: [{ label: "Approve" }, { label: "Decline" }], created: "2026-11-16T10:00:00Z" });
  g.__ls[`:${DIR}/decisions`] = ["d-r.json"];
  const v = await mount(); await settle();
  await staleClick(v, "view-rules");
  await staleClick(v, "rule-head-r-p");
  await staleClick(v, "rule-approve-r-p");
  check(JSON.parse(g.__files[RULES]).rules[0].status === "active", "the rule is approved");
  check(!v.rules.error, `no Rules view error (got ${v.rules.error})`);
  check(JSON.parse(g.__files[`:${DIR}/decisions/d-r.json`]).status === "answered", "its card is answered");
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "decision d-r answered"]]), `the Project Agent is told once (got ${JSON.stringify(sends())}; ${stuck(v)})`);
};

// ---- t-026 Job 1: a Copy control on each card, task, rule and Costs block. It writes plain text lines to the
// clipboard (stub: __clipboard), shows "Copied" for COPIED_FOR, and changes nothing else. ----
const COPIED_FOR = 2000;
const clipReset = () => { g.__clipboard = []; g.__afters = []; };
const lastClip = () => (g.__clipboard ?? [])[(g.__clipboard ?? []).length - 1];
// What a click could change: files, writes, runs, execs, sends, overlays, shared state.
const sideEffects = () => JSON.stringify([g.__files, g.__writes, g.__runs, (g.__execs ?? []).length, g.__hcs, g.__overlays, g.__shared, g.__notes]);
async function copyCardWorld(cardJson) {
  world();
  g.__files[`:${DIR}/decisions/${cardJson.id}.json`] = JSON.stringify(cardJson);
  g.__ls[`:${DIR}/decisions`] = [`${cardJson.id}.json`];
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  const v = await mount(); await settle();
  clipReset();
  return v;
}
const openCopyCard = { id: "d-c", task: "t-9", status: "open", question: "Q?", context: "line one\n  indented", recommendation: "A",
  options: [{ label: "A", detail: "do a" }, { label: "B" }], created: "2026-11-16T10:00:00Z" };

cases["T26-copy-open-card"] = async () => { // the card as plain text lines; nothing picked, answered or written
  const v = await copyCardWorld(openCopyCard);
  check(/\bCopy\b/.test(look(v).text) && Boolean(look(v).clicks["copy-d-c"]), `a Copy control on the card (clicks ${Object.keys(look(v).clicks)})`);
  const before = sideEffects();
  await press(v, "copy-d-c"); await settle();
  check((g.__clipboard ?? []).length === 1, `one clipboard write (got ${JSON.stringify(g.__clipboard)})`);
  const lines = lastClip().split("\n");
  check(lines[0].startsWith("d-c · t-9 · open · "), `line 1: id, task, status, date (got ${lines[0]})`);
  const want = ["Q?", "line one", "  indented", "Options:", "  A: do a (recommended)", "  B"];
  check(JSON.stringify(lines.slice(1)) === JSON.stringify(want), `the rest (got ${JSON.stringify(lines.slice(1))})`);
  check(sideEffects() === before, "nothing written, run, sent or shared");
  check(!v.choice["d-c"], `no option picked (got ${v.choice["d-c"]})`);
  check(JSON.parse(g.__files[`:${DIR}/decisions/d-c.json`]).status === "open", "the card is still open");
  // The card still answers as before, after a Copy.
  await answerCard(v, "d-c");
  check(JSON.parse(g.__files[`:${DIR}/decisions/d-c.json`]).answer === "A", "picking and Submit still answer");
};

cases["T26-copy-answered-card"] = async () => { // answered: the answer, who answered and the note, as drawn
  const v = await copyCardWorld({ ...openCopyCard, status: "answered", answer: "B", note: "because", answeredBy: "conversation", answered: "2026-11-16T11:00:00Z" });
  await click(v, "pa-older");
  await click(v, "copy-d-c");
  const lines = lastClip().split("\n");
  check(lines[0].startsWith("d-c · t-9 · answered: B · "), `line 1 (got ${lines[0]})`);
  const want = ["Q?", "line one", "  indented", "Options:", "  A: do a (recommended)", "  B", "Answer: B", "Answered by the conversation", "You said: “because”"];
  check(JSON.stringify(lines.slice(1)) === JSON.stringify(want), `the rest (got ${JSON.stringify(lines.slice(1))})`);
};

cases["T26-copy-evidence-name"] = async () => { // the evidence name the card's link shows
  world();
  g.__files[`:${DIR}/decisions/d-e.json`] = JSON.stringify({ id: "d-e", task: "t-1", status: "open", question: "E?", evidence: "REVIEW.md", options: [{ label: "A" }], created: "2026-11-16T10:00:00Z" });
  g.__ls[`:${DIR}/decisions`] = ["d-e.json"];
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-1", title: "One", state: "review" }] });
  const v = await mount(); await settle();
  const shown = Object.keys(look(v).clicks).includes("card-task-d-e");
  clipReset();
  await click(v, "copy-d-e");
  const lines = lastClip().split("\n");
  if (shown) {
    const link = look(v).text.split(" | ").find((x) => x.endsWith(" ↗") && /REVIEW|t-1/.test(x));
    check(lines.includes(`Evidence: ${link.replace(/ ↗$/, "")}`), `the evidence line as linked (link ${link}; got ${JSON.stringify(lines)})`);
  } else check(!lines.some((l) => l.startsWith("Evidence:")), `no evidence line without the link (got ${JSON.stringify(lines)})`);
};

cases["T26-copied-mark"] = async () => { // "Copied" for COPIED_FOR, then "Copy"; the redraw is asked for by a timer, not a sleep
  const v = await copyCardWorld(openCopyCard);
  const label = () => { const route = pathTo(v.render(cx), "copy-d-c"); return route[route.length - 1].kids.join(""); };
  check(label() === "Copy", `before: Copy (got ${label()})`);
  await click(v, "copy-d-c");
  check(label() === "Copied", `after the click: Copied (got ${label()})`);
  check((g.__afters ?? []).length === 1 && g.__afters[0].ms >= COPIED_FOR, `one redraw timer of COPIED_FOR (got ${JSON.stringify((g.__afters ?? []).map((x) => x.ms))})`);
  advance(COPIED_FOR - 500);
  check(label() === "Copied", "still Copied before COPIED_FOR");
  advance(600);
  g.__afters[0].fn(cx); await settle();
  check(label() === "Copy", `back to Copy (got ${label()})`);
};

cases["T26-copy-stale-cx"] = async () => { // a click's cx is not kept: copying with a cx that ends after the click throws nothing
  const v = await copyCardWorld(openCopyCard);
  await staleClick(v, "copy-d-c");
  check((g.__clipboard ?? []).length === 1, `copied (got ${JSON.stringify(g.__clipboard)})`);
  check(!g.__rejected.length, `nothing threw (got ${JSON.stringify(g.__rejected)})`);
};

cases["T26-copy-push-card"] = async () => { // the push card's facts as drawn; Copy runs no check and no run; Push and Not now as before
  const { v, file } = await pushWorld();
  clipReset();
  const checks = pushExecs("check").length, before = sideEffects();
  await press(v, `copy-${PID}`); await settle();
  const text = lastClip(), lines = text.split("\n");
  check(lines[0].startsWith(`${PID} · t-1 · open · `) && lines[1] === "Push repo main (2 commits)?", `id line and question (got ${JSON.stringify(lines.slice(0, 2))})`);
  for (const want of ["Repository: /repo", "Commits (2):", "  cc11000 the task's change — A <a@x>", "What will run:", "No force, no tags, no other branches.",
    "This panel may push: yes", "Options:", "  Push: Push exactly what is listed above, as a fast-forward. No force. (recommended)", "  Not now: Nothing is pushed. The card closes."])
    check(lines.includes(want), `has "${want}" (got ${JSON.stringify(lines)})`);
  check(!text.includes("CONTEXT FROM THE CARD FILE") && !text.includes("DETAIL FROM THE CARD FILE") && !text.includes("Check again"), `not the card file's own text, nor the links (got ${text})`);
  check(pushExecs("check").length === checks && pushExecs("run").length === 0 && sideEffects() === before, "no check, no run, nothing written");
  check(!v.choice[PID], "nothing picked");
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  check(pushExecs("run").length === 1, `Push then Submit runs once, as before (got ${pushExecs("run").length})`);
  check(Boolean(file), "the card file");
};

cases["T26-copy-push-notnow"] = async () => { // Not now still answers after a Copy
  const { v } = await pushWorld({ tool: { run: (args) => json(0, { ok: true, done: true, answer: "Not now", refusal: null, message: "answered Not now", result: null }) } });
  clipReset();
  await click(v, `copy-${PID}`);
  await click(v, `opt-${PID}-1`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const run = pushExecs("run");
  check(run.length === 1 && run[0][1].includes("Not now"), `Not now runs once (got ${JSON.stringify(run.map((x) => x[1]))})`);
};

cases["T26-copy-task"] = async () => { // the task: id, title, state, goal, workers with addresses, links; nothing opened
  world({ extraTerms: [{ address: "t32", state: "live", argv: ["claude"], labels: {}, status: "idle", program: claude },
                       { address: "t33", state: "live", argv: ["claude"], labels: {}, status: "working", program: claude }] });
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-003", title: "T", goal: "Goal text", type: "build", tier: "normal", state: "in-progress", phase: "build",
    links: [{ label: "CR-1", url: "https://example.test/cr/1" }], workers: [
    { address: "t31", role: "build", name: "t-003-tests", finished: true, closed: "2026-11-16T10:00:00Z", closedBy: "agent" },
    { address: "t32", role: "reviewer", name: "t-003-review", keep: true },
    { address: "t33", role: "verifier", name: "t-003-verify" },
    { address: "t34", role: "build", name: "t-003-old" }] }] });
  const v = await mount();
  await click(v, "view-tasks");
  await click(v, "task-head-t-003");
  clipReset();
  const before = sideEffects();
  await press(v, "copy-task-t-003"); await settle();
  const want = ["t-003 T", "State: in-progress · build", "Goal text", "build · normal tier", "Workers:", "  reviewer t32 · kept", "  verifier t33", "  build t34 (ended)", "Links:", "  CR-1: https://example.test/cr/1"];
  check(lastClip() === want.join("\n"), `the task text (got ${JSON.stringify(lastClip())})`);
  check(sideEffects() === before && v.open.has("t-003"), "nothing opened, run or folded");
};

cases["T26-copy-task-remote"] = async () => { // a worker on another machine is copied with its machine-qualified address
  world({ machine: M });
  const dir = `${M}:${RDIR}`;
  g.__files[`${dir}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-1", title: "One", state: "queued", workers: [{ address: "t50", role: "build" }] }] });
  g.__files[`${dir}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  const v = await mount(); await settle();
  await click(v, "view-tasks"); await click(v, "task-head-t-1");
  clipReset();
  await click(v, "copy-task-t-1");
  check((lastClip() ?? "").split("\n").includes(`  build ${M}/t50 (ended)`), `the machine-qualified address (got ${JSON.stringify(lastClip())})`);
};

cases["T26-copy-armed-delete"] = async () => { // a press on Copy is a press elsewhere: it disarms the armed Delete, deletes nothing
  const v = await armWorld();
  await press(v, "delete-t-1");
  check(armedText(v), "armed");
  clipReset();
  await press(v, "copy-task-t-1");
  check(!armedText(v) && deletes().length === 0, `disarmed, nothing deleted (deletes ${JSON.stringify(deletes())})`);
  check(lastClip()?.startsWith("t-1 One\n"), `copied (got ${JSON.stringify(lastClip())})`);
  await press(v, "delete-t-1"); await press(v, "delete-t-1"); await settle();
  check(deletes().length === 1, "two clicks on Delete still delete");
};

cases["T26-copy-rule"] = async () => { // the rule as shown; its Delete is not armed, nothing saved
  const v = await ruleWorld();
  clipReset();
  const before = g.__files[RULES];
  await press(v, "rule-copy-r-one"); await settle();
  check(lastClip() === ["One · active · all", "One text", "r-one · from person · order 10 · 0 changes"].join("\n"), `the rule text (got ${JSON.stringify(lastClip())})`);
  check(!ruleArmed(v) && g.__files[RULES] === before, "nothing armed or saved");
  await press(v, "rule-delete-r-one");
  check(ruleArmed(v), "the rule Delete still arms");
};

cases["T26-copy-costs"] = async () => { // the totals, a section (with its open drill rows) and the billed table, as shown
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(withRows()) } });
  await settle();
  clipReset();
  const execs = g.__execs.length;
  const { clicks } = costsView(v);
  for (const id of ["costs-copy-totals", "costs-copy-activity", "costs-copy-task", "costs-copy-kind", "costs-copy-day", "costs-copy-billed"]) check(Boolean(clicks[id]), `${id} (have ${Object.keys(clicks)})`);
  await press(v, "costs-copy-totals");
  let lines = lastClip().split("\n");
  check(/^Estimated from test table prices as of 2027-01-01 \(XTS\) · updated /.test(lines[0]), `the header (got ${lines[0]})`);
  check(lines.length === 4 && /^Total: .*est\..* · 1k tokens · 94% cache read$/.test(lines[1]) && lines[2].startsWith("Today: ") && lines[3].startsWith("Last 7 days (UTC): "), `the tiles (got ${JSON.stringify(lines)})`);
  const shown = costsView(v).text;
  for (const l of lines.slice(1)) for (const part of l.replace(/^[^:]+: /, "").split(" · ")) check(shown.includes(part.split(" · ")[0]), `"${part}" is shown`);
  await press(v, "costs-copy-activity");
  lines = lastClip().split("\n");
  check(lines[0] === "By activity" && lines.length === 4 && lines.slice(1).every((l) => / tokens · /.test(l)), `head and 3 rows (got ${JSON.stringify(lines)})`);
  await click(v, "costs-activity-0");
  await press(v, "costs-copy-activity");
  lines = lastClip().split("\n");
  check(lines.includes("  Tasks:") && lines.some((l) => /^  t-1 · 600 tokens · /.test(l)), `the open drill rows, indented (got ${JSON.stringify(lines)})`);
  await press(v, "costs-copy-billed");
  lines = lastClip().split("\n");
  check(lines[0] === "Billed (account-wide: includes use outside this space and outside Herdr)", `the billed head (got ${lines[0]})`);
  check(lines.some((l) => /^model-a \| .* est\. \| .* billed · account-wide$/.test(l)) && lines.some((l) => /^total \| /.test(l)) && lines.some((l) => l.startsWith("Billing query: on · ")), `table rows and the status (got ${JSON.stringify(lines)})`);
  check(g.__execs.length === execs, "Copy runs nothing");
};

// ---- t-026 Job 2: text with no click handler is selectable (a TextView with .selectable(true)); option rows,
// buttons and anything inside a click handler stay plain. The stub keeps a TextView's text as its wrapper's. ----
// Every TextView in a tree: { id, text, html, selectable, underClick (an ancestor or itself has a click), inOption }.
function textViews(root) {
  const out = [];
  const walk = (x, underClick, inOption) => {
    if (!x || typeof x !== "object") return;
    const click = underClick || Boolean(x.click), opt = inOption || /^opt-/.test(String(x.key ?? ""));
    if (x.kind === "textview") out.push({ id: x.key, text: x.text, html: x.html, selectable: JSON.stringify(x.style?.selectable) === "[true]", underClick: click, inOption: opt });
    for (const k of x.kids ?? []) walk(k, click, opt);
  };
  walk(root, false, false);
  return out;
}
const tvText = (root) => textViews(root).map((x) => x.text);
const safeViews = (root, what) => {
  const all = textViews(root);
  const bad = all.filter((x) => x.underClick || x.inOption || !x.selectable);
  check(!bad.length, `${what}: no TextView under a click handler or in an option row, each selectable (bad ${JSON.stringify(bad)})`);
  return all;
};

cases["T26-sel-card"] = async () => { // the question, context rows and notes are selectable; option rows and the id row are not
  const v = await copyCardWorld(openCopyCard);
  const tree = v.render(cx);
  const all = safeViews(tree, "open card");
  for (const want of ["Q?", "line one", "indented"]) check(tvText(tree).includes(want), `"${want}" is selectable (have ${JSON.stringify(tvText(tree))})`);
  for (const not of ["A", "B", "do a", "d-c", "Submit answer"]) check(!tvText(tree).includes(not), `"${not}" is not a TextView`);
  const ind = stubs.findText(tree, "indented");
  check(ind.length === 1 && ind[0].kind === "div" && JSON.stringify(ind[0].style?.pl) === "[12]", `the indented row is still the styled div (got ${JSON.stringify(ind.map((x) => [x.kind, x.style]))})`);
  check(new Set(all.map((x) => x.id)).size === all.length, `ids are unique (got ${JSON.stringify(all.map((x) => x.id))})`);
  const again = textViews(v.render(cx)).map((x) => x.id);
  check(JSON.stringify(again) === JSON.stringify(all.map((x) => x.id)), "ids are the same on the next render");
  await click(v, "opt-d-c-1");
  check(v.choice["d-c"] === "B" && JSON.parse(g.__files[`:${DIR}/decisions/d-c.json`]).status === "open", "an option click still only picks");
};

cases["T26-sel-answered"] = async () => { // an answered card's "Answered by" and note lines are selectable
  const v = await copyCardWorld({ ...openCopyCard, status: "answered", answer: "B", note: "because", answeredBy: "conversation", answered: "2026-11-16T11:00:00Z" });
  await click(v, "pa-older");
  const tree = v.render(cx);
  safeViews(tree, "answered card");
  for (const want of ["Q?", "Answered by the conversation", "You said: “because”"]) check(tvText(tree).includes(want), `"${want}" is selectable (have ${JSON.stringify(tvText(tree))})`);
};

cases["T26-sel-escape"] = async () => { // text is escaped into the TextView's html, never read as markup
  const v = await copyCardWorld({ ...openCopyCard, question: `a <b>bold</b> & "q"`, context: "<script>x</script>" });
  const tree = v.render(cx);
  const q = textViews(tree).find((x) => x.text === `a <b>bold</b> & "q"`);
  check(q && q.html === "<span>a &lt;b&gt;bold&lt;/b&gt; &amp; &quot;q&quot;</span>", `escaped html (got ${JSON.stringify(q)})`);
  const c = textViews(tree).find((x) => x.text === "<script>x</script>");
  check(c && !/<script>/.test(c.html), `the context is escaped too (got ${JSON.stringify(c)})`);
};

cases["T26-sel-push"] = async () => { // the push card's facts are selectable; its options and Check again are not
  const { v } = await pushWorld();
  const tree = v.render(cx);
  safeViews(tree, "push card");
  for (const want of ["Repository: /repo", "Commits (2):", "cc11000 the task's change — A <a@x>", "No force, no tags, no other branches.", "This panel may push: yes"])
    check(tvText(tree).includes(want), `"${want}" is selectable (have ${JSON.stringify(tvText(tree))})`);
  check(!tvText(tree).includes("Check again") && !tvText(tree).some((x) => /^Push exactly/.test(x)), "Check again and the options stay plain");
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  check(pushExecs("run").length === 1, "Push then Submit still runs once");
};

cases["T26-sel-task"] = async () => { // a task's goal and kind line are selectable; its head and worker chips are not
  world();
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-1", title: "One", goal: "Goal text", type: "build", tier: "low", state: "in-progress", workers: [{ address: "t50", role: "build" }] }] });
  const v = await mount(); await settle();
  await click(v, "view-tasks"); await click(v, "task-head-t-1");
  const tree = v.render(cx);
  safeViews(tree, "task");
  check(tvText(tree).includes("Goal text") && tvText(tree).includes("build · low tier"), `goal and kind selectable (have ${JSON.stringify(tvText(tree))})`);
  check(!tvText(tree).some((x) => /One|t50/.test(x)), "the head and worker chips stay plain");
};

cases["T26-sel-rule"] = async () => { // an open rule's text and its id line are selectable; its head is not
  const v = await ruleWorld();
  const tree = v.render(cx);
  safeViews(tree, "rules");
  check(tvText(tree).includes("One text") && tvText(tree).includes("r-one · from person · order 10 · 0 changes"), `rule text selectable (have ${JSON.stringify(tvText(tree))})`);
  check(!tvText(tree).includes("One"), "the head stays plain");
  await press(v, "rule-delete-r-one");
  check(ruleArmed(v), "Delete still arms");
};

cases["T26-sel-costs"] = async () => { // the Costs figures are selectable; the section rows (clickable drill) are not
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(withRows()) } });
  await settle();
  await click(v, "costs-activity-0");
  const tree = v.render(cx);
  const all = safeViews(tree, "costs");
  const texts = all.map((x) => x.text);
  for (const want of ["Total", "Today", "Billed (account-wide: includes use outside this space and outside Herdr)", "Tasks:", "model-a"]) check(texts.includes(want), `"${want}" is selectable (have ${JSON.stringify(texts)})`);
  check(texts.some((x) => /^Estimated from test table prices/.test(x)) && texts.some((x) => /^XTS\s12\.50 est\.$/.test(x)) && texts.some((x) => /^Billing query: on · /.test(x)), `the header, a figure and the status (have ${JSON.stringify(texts)})`);
  check(!texts.includes("By activity") || all.find((x) => x.text === "By activity"), "heads may be selectable");
  check(new Set(all.map((x) => x.id)).size === all.length, `ids unique (got ${JSON.stringify(all.map((x) => x.id))})`);
};

cases["T26-sel-every-view"] = async () => { // no TextView under a click anywhere: decisions, tasks, rules and costs of one world
  const v = await copyCardWorld(openCopyCard);
  for (const view of ["decisions", "tasks", "rules", "costs"]) { await click(v, `view-${view}`); await settle(); safeViews(v.render(cx), view); }
  safeViews(v.render(cx), "root");
};

// ---- t-028 group A: the armed Delete (t-018 items 1-4) ----
cases["A8-live-arm"] = async () => { // A1: a click on a Delete drawn armed after its arm ran out (no tick, no redraw) only arms again
  const v = await armWorld();
  await press(v, "delete-t-1");
  check(armedText(v), "task Delete armed");
  let { clicks } = look(v); // the tree on screen, drawn armed
  advance(10500); // past ARM_WAIT; no 1 s tick, no render
  clicks["delete-t-1"]({}, cx); await flush();
  check(deletes().length === 0, `task: nothing deleted (got ${JSON.stringify(deletes())})`);
  check(armedText(v), "task: the click arms it again");
  const r = await ruleWorld();
  await press(r, "rule-delete-r-one");
  check(ruleArmed(r), "rule Delete armed");
  ({ clicks } = look(r));
  advance(10500);
  clicks["rule-delete-r-one"]({}, cx); await settle();
  check(ruleIds() === "r-one,r-two", `rule: nothing deleted (got ${ruleIds()})`);
  check(ruleArmed(r), "rule: the click arms it again");
};
cases["A9-field-focus-disarms"] = async () => { // A2: focus in a panel text field disarms (whether or not the Input stops the press)
  const v = await armWorld();
  await click(v, "sched-summary"); await click(v, "view-tasks"); // the schedule opens in its tab (t-145), then back to the task
  for (const k of ["quietFrom", "quietTo", "custom"]) {
    await press(v, "delete-t-1");
    check(armedText(v), `armed before ${k}`);
    const f = v.schedule[k];
    check(f && f.handlers("focus") > 0, `${k} has a focus handler`);
    f.fire("focus"); await flush();
    check(!armedText(v), `focus in ${k} disarms the task Delete`);
  }
  check(deletes().length === 0, "nothing deleted");
  const r = await ruleWorld();
  look(r).clicks["rule-import"]({}, cx); await flush(); // the import path field
  await press(r, "rule-delete-r-one");
  check(ruleArmed(r), "rule Delete armed");
  r.rules.importPath.fire("focus"); await flush();
  check(!ruleArmed(r), "focus in the import path disarms the rule Delete");
  r.rules.edit(null);
  for (const [k, f] of Object.entries(r.rules.form.inputs)) {
    await press(r, "rule-delete-r-one");
    check(ruleArmed(r), `armed before the form's ${k}`);
    f.fire("focus"); await flush();
    check(!ruleArmed(r), `focus in the form's ${k} disarms`);
  }
  await settle();
  check(ruleIds() === "r-one,r-two", `no rule deleted (got ${ruleIds()})`);
  const w = await cardsWorld({}, ["d-x"]); await settle();
  const note = noteOf(w, "d-x");
  check(note && note.handlers("focus") > 0, "a card's note field has a focus handler");
};
cases["A11-redraw-at-10s"] = async () => { // A4: the 1 s tick asks for a redraw when an arm runs out (and not before)
  const v = await armWorld();
  await press(v, "delete-t-1");
  let n0 = g.__notifies ?? 0;
  advance(9000); await fire(1000);
  check((g.__notifies ?? 0) === n0, `no redraw at 9 s (got ${(g.__notifies ?? 0) - n0})`);
  advance(1500); n0 = g.__notifies ?? 0; await fire(1000);
  check((g.__notifies ?? 0) - n0 === 1, `one redraw at 10.5 s (got ${(g.__notifies ?? 0) - n0})`);
  const r = await ruleWorld();
  await press(r, "rule-delete-r-one");
  advance(10500); n0 = g.__notifies ?? 0; await fire(1000);
  check((g.__notifies ?? 0) - n0 === 1, `rule: one redraw at 10.5 s (got ${(g.__notifies ?? 0) - n0})`);
};

// ---- t-028 group B: push card edge cases (t-022 items 1-4). The push tool is the stand-in (__execFn) only. ----
const MISMATCH = (fileId, own) => `This card's id (${own}) is not its file name (${fileId}): shown only, never checked or run.`;
cases["B1-push-id-from-file-name"] = async () => { // item 1: a push card's id is its file name; one whose own id differs is shown, never checked or run
  const { v } = await pushWorld({ card: pushCardText({ id: "d-push-t-9-9" }) });
  check(v.cards.some((c) => c.id === PID), `the entry's id is the file name (got ${JSON.stringify(v.cards.map((c) => c.id))})`);
  look(v).clicks[`opt-${PID}-1`]?.({}, cx); await settle(); // Not now
  const { text, clicks } = look(v);
  check(text.includes(MISMATCH(PID, "d-push-t-9-9")), `says why it is shown only (got ${text.slice(0, 1500)})`);
  check(!clicks[`submit-${PID}`] && !clicks[`push-check-${PID}`], `no Submit, no Check again (have ${Object.keys(clicks)})`);
  look(v).clicks[`opt-${PID}-0`]?.({}, cx); await settle(); // picking Push checks nothing
  const space = g.__spaces.find((x) => x.active), entry = v.cards.find((c) => c.id === PID);
  v.answer(space, entry, "Not now", cx); v.answer(space, entry, "Push", cx); await settle(); // even a direct answer runs nothing
  check(allExecsOfTool().length === 0, `the push tool never ran (got ${JSON.stringify(allExecsOfTool().map((x) => x[1]))})`);
  // A file name that reads as an option is refused the same way.
  world(); g.__files[`:${DIR}/decisions/-x.json`] = pushCardText({ id: "-x" }); g.__ls[`:${DIR}/decisions`] = ["-x.json"];
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [] }); pushTool();
  const w = await mount(); await settle();
  check(look(w).text.includes("This card's file name (-x) starts with a dash: shown only, never checked or run."), `a leading dash (got ${look(w).text.slice(0, 1200)})`);
  check(allExecsOfTool().length === 0, `nothing ran (got ${JSON.stringify(allExecsOfTool().map((x) => x[1]))})`);
  // The same file id and name: checked as before.
  const { v: u } = await pushWorld();
  check(pushExecs("check").length === 1 && !look(u).text.includes("never checked or run"), "a matching card is checked");
};
cases["B2-push-notnow-while-checking"] = async () => { // item 2: Not now during a running check says so; Submit again after it runs it once
  const hold = deferred();
  let n = 0;
  const { v } = await pushWorld({ tool: { check: () => (++n === 1 ? hold.promise : json(0, checkReply())), run: () => json(0, { ok: true, done: false, answer: "Not now", refusal: null, result: null, message: "answered Not now" }) } });
  check(look(v).text.includes("Checking…"), "the first check runs");
  await click(v, `opt-${PID}-1`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const SAID = "Not now was not saved: a check was running. Submit it again.";
  check(pushExecs("run").length === 0, "no run while the check runs");
  check(look(v).text.includes(SAID), `says so (got ${look(v).text.slice(0, 1500)})`);
  hold.resolve(json(0, checkReply())); await settle();
  check(look(v).text.includes(SAID) && pushExecs("run").length === 0, "still said after the check, nothing run by itself");
  if (!v.choice[PID]) { await click(v, `opt-${PID}-1`); await settle(); }
  await click(v, `submit-${PID}`); await settle();
  check(pushExecs("run").length === 1 && pushExecs("run")[0][1].includes("Not now"), `Submit again runs Not now once (got ${JSON.stringify(pushExecs("run").map((x) => x[1]))})`);
  check(!look(v).text.includes(SAID), "the line goes");
};
cases["B3-push-unknown-then-closed"] = async () => { // item 3: unknown result, the check found the card open, then it closed by another route: no stale "checking again"
  let file;
  const { v, file: f } = await pushWorld({ opts: { records: alive() }, tool: { run: () => ({ code: 1, stdout: "", stderr: "Traceback (most recent call last):\nOSError: boom" }) } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle(); await settle();
  check(/The result is unknown/.test(look(v).text) && /Ready: Push pushes exactly this\./.test(look(v).text), `unknown, then the check found it open (got ${look(v).text.slice(0, 1500)})`);
  g.__files[file] = JSON.stringify({ ...JSON.parse(pushCardText()), status: "withdrawn", note: "not needed", answered: "2026-11-16T10:05:00Z", answeredBy: "Project Agent" });
  advance(10000); await fire(10000); await settle();
  const text = look(v).text;
  check(v.cards.some((c) => c.id === PID && c.card?.status === "withdrawn"), "the reload read it withdrawn");
  check(!text.includes("checking the card again") && !text.includes("The check found the card open."), `no stale line (got ${text.slice(0, 1500)})`);
  check(/The result was unknown \([\s\S]*boom[\s\S]*\); the card was open when checked and has closed since\./.test(text), `says what is known (got ${text.slice(0, 1500)})`);
  check(pushExecs("run").length === 1, "one run");
};
cases["B4-push-result-shapes"] = async () => { // item 4: the four shapes the tests did not pin
  // ok:true, done:false with a result object, exit 0; and a result that is not an object: unknown, never Pushed.
  const res = { at: "2026-11-16T10:01:00Z", from: PR, to: PC, url: PURL, branch: "main" };
  for (const [name, reply] of [["ok, not done, a result", { ok: true, done: false, answer: "Push", refusal: null, result: res, message: "" }],
    ["result not an object", { ok: true, done: true, answer: "Push", refusal: null, result: "pushed", message: "" }]]) {
    const { v } = await pushWorld({ opts: { records: alive() }, tool: { run: () => json(0, reply) } });
    await click(v, `opt-${PID}-0`); await settle();
    await click(v, `submit-${PID}`); await settle();
    const { text } = look(v);
    check(/The result is unknown \(/.test(text) && !text.includes("Pushed "), `${name}: unknown, never Pushed (got ${text.slice(0, 1500)})`);
    // The exit-0 reason's own words.
    const why = "run exited with 0 without a result that says it pushed";
    check(text.includes(why), `${name}: the reason (got ${text.slice(0, 1500)})`);
    check(sends().map((x) => x[2]).some((m) => m.startsWith(`decision ${PID} answered Push; result unknown (`) && m.includes(why)), `${name}: told so (got ${JSON.stringify(sends())})`);
  }
  // "Checking…" on the answered card while the follow-up check runs, then its verdict.
  let file, ran = false;
  const hold = deferred();
  const { v, file: f } = await pushWorld({ tool: { check: () => (ran ? hold.promise : json(0, checkReply())),
    run: () => { ran = true; g.__files[file] = answeredByTool(); return { code: 1, stdout: "", stderr: "crash" }; } } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle(); await settle();
  let text = look(v).text;
  check(v.cards.some((c) => c.id === PID && c.card?.status === "answered"), "read answered");
  const u = text.indexOf("The result is unknown"), c = text.indexOf("Checking…");
  check(u >= 0 && c > u && !text.includes(C4_DONE), `unknown, then Checking… (got ${text.slice(0, 1500)})`);
  hold.resolve(checkDone()); await settle();
  text = look(v).text;
  check(text.includes(C4_DONE) && !text.includes("Checking…"), `then the verdict (got ${text.slice(0, 1500)})`);
};

// ---- t-028 group C: Costs view display (t-027 items 1, 3, 4, 5, 11). Every pa-costs call is the stand-in (costsTool). ----
const dayKey = (ms) => isoAt(ms).slice(0, 10);
cases["C1-day-activities"] = async () => { // item 1: the day drill's activities carry the unpriced mark; a wholly unpriced one is listed
  const day = dayKey(T0);
  const s = costsSummary({ byDay: [{ key: day, tokens: 1077, money: 7, unpricedTokens: 127, calls: 7, byActivity: { build: 3, "cache-rebuild": 4 },
    activities: [{ key: "cache-rebuild", tokens: 400, money: 4 }, { key: "build", tokens: 600, money: 3, unpricedTokens: 50 }, { key: "review", tokens: 77, money: null }] }] });
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-day-0");
  const text = costsView(v).text, drill = text.split("By day")[1] ?? "";
  check(/build · 600 tokens · [^|]*3\.00[^|]* est\. \+ unpriced · partly no price/.test(drill), `a partly unpriced activity is marked (got ${drill.slice(0, 1200)})`);
  check(/review · 77 tokens · — est\. · no price/.test(drill), `a wholly unpriced activity is listed (got ${drill.slice(0, 1200)})`);
  check(/cache rebuild after idle · 400 tokens · [^|]*4\.00[^|]* est\. \|/.test(`${drill} |`) , `a priced one has no mark (got ${drill.slice(0, 1200)})`);
  check(!/(^|[^\d.])0\.00\b/.test(drill), `no zero (got ${drill.slice(0, 1200)})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
};
cases["C3-turnoff-race"] = async () => { // item 3: a compare from before Turn off that ends after the one after it never shows the query on again
  const h = hold(); let n = 0, off = false;
  const { v } = await costsWorld({ tool: {
    "billed compare": () => { n++; if (off) return json0(noRows()); if (n === 2) return h.p.then(() => json0(noRows(statusOn()))); return json0(noRows(statusOn())); },
    "billed disable": () => { off = true; return { code: 0, stdout: "off", stderr: "" }; } } });
  check(costsView(v).text.includes("Billing query: on"), `on at first (got ${costsView(v).text.slice(-400)})`);
  advance(61000); v.reload(cx); await settle(); // a later read: its compare (the 2nd) is held
  check(n === 2, `the 2nd compare is in flight (got ${n})`);
  await click(v, "costs-billing-off"); await settle();
  // t-053 (R2): the compare after Turn off waits for the one in flight (the same call), then asks again.
  check(n === 2, `the compare after Turn off waits for the 2nd (got ${n})`);
  h.release(); await settle(); await settle();
  check(n === 3 && costsView(v).text.includes("The billing query is off"), `off after Turn off (got ${n}, ${costsView(v).text.slice(-400)})`);
  const text = costsView(v).text;
  check(text.includes("The billing query is off") && !text.includes("Billing query: on"), `the older compare does not bring "on" back (got ${text.slice(-400)})`);
};
cases["C4-billed-due-on-show"] = async () => { // item 4 (RL-R5): a read that ends while hidden: shown again, the billed compare starts at once
  g.__layout = panelLayout("project-agent");
  const { v } = await costsWorld({ machine: M, open: false });
  g.__remoteDelay = 3000;
  await click(v, "view-costs"); await settle();
  await leaveCosts(v, "hide");
  for (let s = 1; s <= 4; s++) { advance(1000); await settle(); }
  await settle();
  g.__remoteDelay = 0;
  check(costsExecs("billed compare").length === 0, "no compare while hidden");
  await backToCosts(v, "hide"); await settle();
  check(costsExecs("billed compare").length === 1, `shown again: the compare at once (got ${costsExecs("billed compare").length})`);
};
cases["C4-stale-read-on-show"] = async () => { // item 4 (RL-R6): hidden and shown again before a reload saw it hidden: the stale read starts at once
  g.__layout = panelLayout("project-agent");
  const h = hold();
  const { v } = await costsWorld({ tool: { collect: () => h.p.then(() => ({ code: 0, stdout: "", stderr: "" })) } });
  await click(v, "costs-refresh"); await settle();
  await leaveCosts(v, "hide"); // no reload while hidden
  h.release(); await settle(); await settle();
  const n = costsReads().length;
  check(v.costs.peek(S)?.stale === true, "the run ended while hidden: stale");
  g.__layout = panelLayout("project-agent"); advance(1000); await fire(1000); await settle();
  check(costsReads().length === n + 1, `shown again: read at the next 1 s tick (reads ${n} -> ${costsReads().length})`);
};
cases["C4-refresh-during-read"] = async () => { // item 4 (RL-R6, RL-V5 N5): a Refresh that ends while a read is in flight: the reread starts when that read ends
  const { v } = await costsWorld({ machine: M });
  advance(11000); g.__remoteDelay = 3000;
  await click(v, "costs-refresh"); await settle(); // the collect: ends in 3 s
  advance(1000); v.reload(cx); await settle(); // a read: in flight when the collect ends, ends 1 s after it
  const n = costsReads().length;
  for (let s = 1; s <= 5; s++) { advance(1000); await settle(); } // no reload, no tick
  check(costsReads().length === n + 1, `one reread when the in-flight read ends, without a 10 s reload (reads ${n} -> ${costsReads().length})`);
  g.__remoteDelay = 0;
};
cases["C4-turnoff-hidden"] = async () => { // item 4 (RL-V4): Turn off ends while hidden: shown again, the status compare at once
  g.__layout = panelLayout("project-agent");
  const h = hold(); let off = false;
  const { v } = await costsWorld({ tool: { "billed compare": () => json0(off ? noRows() : noRows(statusOn())), "billed disable": () => h.p.then(() => { off = true; return { code: 0, stdout: "off", stderr: "" }; }) } });
  await click(v, "costs-billing-off"); await settle();
  await leaveCosts(v, "hide");
  h.release(); await settle(); await settle();
  const n = costsExecs("billed compare").length;
  await backToCosts(v, "hide"); await settle();
  check(costsExecs("billed compare").length === n + 1 && costsView(v).text.includes("The billing query is off"), `shown again: the status compare at once (compares ${n} -> ${costsExecs("billed compare").length}; ${costsView(v).text.slice(-300)})`);
};
cases["C5-unreachable-not-missing"] = async () => { // item 5: an unreachable or unknown machine is not "not installed"
  const { v } = await costsWorld({ machine: M, tool: { collect: () => { throw new Error(`machine ${M} not found`); } } });
  await click(v, "costs-refresh"); await settle();
  let text = costsView(v).text;
  check(text.includes(`Refresh failed: machine ${M} not found`) && !text.includes("not installed"), `the machine's error, not the install hint (got ${text.slice(0, 1200)})`);
  timerWorld({ machine: M, tool: { collect: () => { throw new Error(`machine ${M} not found`); } } });
  const { tick } = await timerMod();
  await tickAt(tick, T0);
  const rec = (g.__shared["project-agent.costs"] ?? {})[RS];
  check(rec && rec.ok === false && rec.missing !== true && rec.error === `machine ${M} not found`, `the timer's record (got ${JSON.stringify(g.__shared["project-agent.costs"])})`);
  // Still "not installed" when the tool is what is missing.
  const { v: w } = await costsWorld({ machine: M, tool: { collect: () => { throw new Error("spawn pa-costs ENOENT"); } } });
  await click(w, "costs-refresh"); await settle();
  text = costsView(w).text;
  check(text.includes(`Refresh failed: pa-costs is not installed on ${M}`), `the install hint (got ${text.slice(0, 1200)})`);
};
cases["C6-tile-ranges"] = async () => { // item 11 (RL-V2): Today is this local day's hours only; Last 7 days is today and the 6 UTC days before
  const utc = new Date(T0), d = (back) => new Date(Date.UTC(utc.getUTCFullYear(), utc.getUTCMonth(), utc.getUTCDate() - back)).toISOString().slice(0, 10);
  const s = costsSummary({
    hours: [[isoAt(T0 - 30 * 3600000).slice(0, 13), { tokens: 1000, money: 50 }], [isoAt(T0).slice(0, 13), { tokens: 100, money: 1.25 }]],
    byDay: [{ key: d(0), tokens: 100, money: 12.5, calls: 1, byActivity: {} }, { key: d(6), tokens: 10, money: 2, calls: 1, byActivity: {} }, { key: d(7), tokens: 10, money: 100, calls: 1, byActivity: {} }] });
  const { v } = await costsWorld({ summary: s });
  const text = costsView(v).text;
  check(/Today \| [^|]*1\.25[^|]* est\. \| 100 tokens/.test(text), `Today: this local day's hour only (got ${text.slice(0, 1500)})`);
  check(/Last 7 days \(UTC\) \| [^|]*14\.50[^|]* est\. \| 110 tokens/.test(text), `Last 7 days: today and 6 days back, not 7 (got ${text.slice(0, 1500)})`);
};
cases["C6-chain-left-no-partial"] = async () => { // item 11 (RL-V3-1): a chain stopped because the view was left commits no partial comparison
  g.__layout = panelLayout("project-agent");
  const h = hold(); let first = true;
  const { v } = await costsWorld({ machine: M, open: false, tool: {
    "billed compare": (args) => { if (args.includes("--here-json")) return json0(withRows()); if (first) { first = false; return h.p.then(() => json0({ ...noRows(statusOn()), rows: 4 })); } return json0({ ...noRows(statusOn()), rows: 4 }); } } });
  await click(v, "view-costs"); await settle();
  const before = JSON.stringify(v.costs.peek(RS).billing.data);
  await leaveCosts(v, "tasks");
  h.release(); await settle(); await settle();
  check(JSON.stringify(v.costs.peek(RS).billing.data) === before, `no partial result kept (got ${JSON.stringify(v.costs.peek(RS).billing.data)})`);
  check(!/Billed \(account-wide/.test(costsView(v).text), "nothing drawn from it");
};
cases["C6-left-keeps-copy"] = async () => { // item 11 (RL-V3-2): summary gone, read in flight when the view is left: the kept copy stays
  g.__layout = panelLayout("project-agent");
  const { v, file } = await costsWorld({ machine: M });
  check(v.costs.peek(RS)?.summary?.totals, "a summary read");
  delete g.__files[file];
  advance(11000); g.__remoteDelay = 3000;
  v.reload(cx); await settle(); // the read of the now missing file is in flight
  await leaveCosts(v, "tasks");
  for (let s = 1; s <= 4; s++) { advance(1000); await settle(); }
  g.__remoteDelay = 0; await settle();
  const st = v.costs.peek(RS);
  check(st.summary?.totals?.money === 12.5 && st.missing === null && st.stale === true, `the kept copy stays, marked stale (got ${JSON.stringify({ money: st.summary?.totals?.money, missing: st.missing, stale: st.stale })})`);
  check(costsLs().length === 0, "no ls while left");
};

// ---- t-028 group D: the notice sent when a card is answered has a time limit (t-022 REVIEW RL 3, INVESTIGATION §6) ----
cases["D1-send-timeout"] = async () => { // a hung hc send is given up after 90 s: the line says so, sending clears, later notices go out
  const v = await cardsWorld({ records: alive("t4w"), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "idle" }] }, ["d-x", "d-y"]);
  g.__hcHang = ["t4w"];
  await answerCard(v, "d-x"); await settle();
  check(v.sending === true && sends().length === 1, `the send is in flight (sending ${v.sending}, ${JSON.stringify(sends())})`);
  for (let s = 0; s < 80; s++) { advance(1000); await settle(); }
  check(v.sending === true, "still waiting at 80 s (hc itself waits up to 60 s for a draft)");
  for (let s = 0; s < 12; s++) { advance(1000); await settle(); }
  check(v.sending === false, `given up after 90 s (sending ${v.sending})`);
  check(/hasn't been told yet \(hc send did not answer in 90 s; it may still arrive\)\. Trying again every 10 s\./.test(look(v).text), `the line says why (got ${look(v).text.slice(0, 500)})`);
  g.__hcHang = []; g.__hcs = [];
  await answerCard(v, "d-y"); await settle();
  check(JSON.stringify(sends().map((x) => x[2])) === JSON.stringify(["decision d-x answered", "decision d-y answered"]), `sent again, then the next one, in order (got ${JSON.stringify(sends())})`);
  check(v.sending === false && !/hasn't been told yet/.test(look(v).text), "the line is gone");
};

// ---- t-036: days and times in the person's own timezone ----------------------------------------------------------
// A fake America/Los_Angeles (the panel machine's zone in these cases): minutes east of UTC from each change on.
const LA_CHANGES = [[1772964000, -420], [1793523600, -480], [1805018400, -420], [1825578000, -480]];
const laOffset = (ms) => { let m = -480; for (const [e, o] of LA_CHANGES) if (ms >= e * 1000) m = o; return m; };
const LA_TABLE = [[0, -480], [1772964000, -420], [1793523600, -480]]; // at T0 (2027-01-15T08:00Z = 00:00 PST): now - 10 years .. now + 35 d
const LA_DAYS = { zone: "America/Los_Angeles", offsets: LA_TABLE };
const H20 = 20 * 3600000; // T0 + 20 h = 2027-01-16T04:00Z = 2027-01-15 20:00 PST: the local and the UTC day differ
function zoneDays(days) {
  return costsSummary({ days,
    hours: [[isoAt(T0 - 3600000).slice(0, 13), { tokens: 1000, money: 50 }], [isoAt(T0 + 12 * 3600000).slice(0, 13), { tokens: 7, money: 0.7 }]],
    byDay: [{ key: "2027-01-15", tokens: 100, money: 3, calls: 1, byActivity: {} }, { key: "2027-01-09", tokens: 10, money: 2, calls: 1, byActivity: {} },
      { key: "2027-01-08", tokens: 10, money: 100, calls: 1, byActivity: {} }] });
}

cases["Z1-zone-table"] = async () => { // W2: the panel's zone as a table of offset changes (DST included), its collect args, local dates
  check(zoneMod, "mods/project-agent/zone.js exists");
  const { zone, zoneTable, zoneArgs, localDate, daysBefore, summaryZone, oldTool } = zoneMod;
  setZone(laOffset, "America/Los_Angeles");
  check(JSON.stringify(zoneTable(T0)) === JSON.stringify(LA_TABLE), `the LA table at T0 (got ${JSON.stringify(zoneTable(T0))})`);
  check(JSON.stringify(zoneTable(T0 + 60 * 86400000)) === JSON.stringify([...LA_TABLE, [1805018400, -420]]), `the spring change once it is within 35 days (got ${JSON.stringify(zoneTable(T0 + 60 * 86400000))})`);
  check(JSON.stringify(zoneArgs(T0)) === JSON.stringify(["--zone-offsets", "0:-480,1772964000:-420,1793523600:-480", "--zone-name", "America/Los_Angeles", "--zone-from", String(FROM0)]), `the args (got ${JSON.stringify(zoneArgs(T0))})`);
  zone.name = () => null;
  check(zoneArgs(T0)[3] === "UTC-08:00", `no zone name: the offset now (got ${zoneArgs(T0)[3]})`);
  setZone(() => 330, null);
  check(zoneArgs(T0)[3] === "UTC+05:30" && zoneArgs(T0)[1] === "0:330", `a half-hour zone (got ${JSON.stringify(zoneArgs(T0))})`);
  setZone(laOffset, "America/Los_Angeles");
  check(localDate(T0) === "2027-01-15" && localDate(T0 - 1) === "2027-01-14" && localDate(T0 + H20) === "2027-01-15", "local dates cut at local midnight");
  check(daysBefore("2027-01-15", 6) === "2027-01-09" && daysBefore("2027-03-01", 1) === "2027-02-28", "days before");
  const same = summaryZone({ days: LA_DAYS }, T0), other = summaryZone({ days: { zone: "UTC", offsets: [[0, 0]] } }, T0), none = summaryZone({}, T0);
  const stale = summaryZone({ days: { zone: "America/Los_Angeles", offsets: [[0, -480]] } }, T0); // no Nov 1 change: other days before it
  check(same.local === true && other.local === false && other.label === "UTC" && none.local === false && none.label === "UTC" && stale.local === false && stale.label === "America/Los_Angeles",
    `same / other / none / stale (got ${JSON.stringify([same, other, none, stale])})`);
  check(summaryZone({ days: { zone: "x", offsets: "bad" } }, T0).local === false, "a bad table is not the panel's");
  setZone(() => 0, "UTC");
  check(summaryZone({ days: { zone: "UTC", offsets: [[0, 0]] } }, T0).local === true, "UTC days on a UTC panel are local");
  const refused = { code: 2, stdout: "", stderr: "usage: pa-costs [-h] ...\npa-costs: error: unrecognized arguments: --zone-offsets 0:0 --zone-name UTC\n" };
  check(oldTool(refused) && !oldTool({ code: 2, stdout: "", stderr: "pa-costs: no Project Agent state" }) && !oldTool({ code: 0, stdout: "", stderr: "" }), "an old pa-costs is told apart");
};

cases["Z2-local-days"] = async () => { // W1, W2: a summary in the panel's zone: Today, Last 7 days and By day are local days, no "(UTC)"
  const { v } = await costsWorld({ summary: zoneDays(LA_DAYS), open: false });
  setZone(laOffset, "America/Los_Angeles");
  await click(v, "view-costs"); await settle();
  g.__now = T0 + H20;
  const text = costsView(v).text;
  check(/Today \| [^|]*3\.00[^|]* est\. \| 100 tokens/.test(text), `Today: the local day's byDay entry (got ${text.slice(0, 1500)})`);
  check(/Last 7 days \| [^|]*5\.00[^|]* est\. \| 110 tokens/.test(text), `Last 7 days: local 01-09 .. 01-15 (got ${text.slice(0, 1500)})`);
  check(!/\(UTC\)/.test(text) && /By day \|/.test(text), `no UTC label (got ${text.slice(0, 2500)})`);
};

cases["Z3-other-zone-labelled"] = async () => { // W2: days of another zone, or an older summary without days: labelled, Today from the hours
  for (const [days, label] of [[{ zone: "UTC", offsets: [[0, 0]] }, "UTC"], [undefined, "UTC"], [{ zone: "Europe/Paris", offsets: [[0, 60]] }, "Europe/Paris"]]) {
    const s = zoneDays(days); if (days === undefined) delete s.days;
    const { v } = await costsWorld({ summary: s, open: false });
    setZone(laOffset, "America/Los_Angeles");
    await click(v, "view-costs"); await settle();
    g.__now = T0 + H20;
    const text = costsView(v).text;
    check(text.includes(`Last 7 days (${label}) |`) && text.includes(`By day (${label})`), `labelled ${label} (got ${text.slice(0, 2500)})`);
    check(/Today \| [^|]*0\.70[^|]* est\. \| 7 tokens/.test(text), `Today: the local day of the hours (got ${text.slice(0, 1500)})`);
  }
};

cases["Z4-collect-zone"] = async () => { // W2: the timer's and Refresh's collect pass the panel's zone
  const la = ["--zone-offsets", "0:-480,1772964000:-420,1793523600:-480", "--zone-name", "America/Los_Angeles", "--zone-from", String(FROM0)];
  timerWorld({ machine: M });
  setZone(laOffset, "America/Los_Angeles");
  const { tick } = await timerMod();
  await tickAt(tick, T0);
  check(JSON.stringify(collects().map((c) => c[1])) === JSON.stringify([["collect", "--space", S, "--quiet", ...la]]), `the timer's collect (got ${JSON.stringify(collects())})`);
  const { v } = await costsWorld({ machine: M });
  setZone(laOffset, "America/Los_Angeles");
  await click(v, "costs-refresh"); await settle();
  check(JSON.stringify(collects().map((c) => c[1])) === JSON.stringify([["collect", "--space", S, ...la]]), `Refresh's collect (got ${JSON.stringify(collects())})`);
};

cases["Z4-old-pa-costs"] = async () => { // an old pa-costs on the space's machine refuses the zone: the collect runs again without it
  const old = (args) => (args.includes("--zone-offsets") ? { code: 2, stdout: "", stderr: "usage: pa-costs [-h] {collect,report} ...\npa-costs: error: unrecognized arguments: --zone-offsets 0:0 --zone-name UTC\n" } : { code: 0, stdout: "", stderr: "" });
  timerWorld({ machine: M, tool: { collect: old } });
  const { tick } = await timerMod();
  await tickAt(tick, T0);
  check(JSON.stringify(collects().map((c) => c[1])) === JSON.stringify([["collect", "--space", S, "--quiet", ...UTC_ARGS], ["collect", "--space", S, "--quiet"]]), `again without the zone (got ${JSON.stringify(collects())})`);
  check(g.__shared["project-agent.costs"]?.[`${M}/${S}`]?.ok === true || g.__shared["project-agent.costs"]?.[S]?.ok === true, `the collect counts as done (got ${JSON.stringify(g.__shared["project-agent.costs"])})`);
  const { v } = await costsWorld({ machine: M, tool: { collect: old } });
  await click(v, "costs-refresh"); await settle();
  check(JSON.stringify(collects().map((c) => c[1])) === JSON.stringify([["collect", "--space", S, ...UTC_ARGS], ["collect", "--space", S]]), `Refresh: again without the zone (got ${JSON.stringify(collects())})`);
  check(!/Refresh failed/.test(costsView(v).text), "no failure shown");
};

cases["Z5-cc-date-and-billed-days"] = async () => { // W1: the CC cover date is the local date; the billed days say they are UTC days
  const s = ccSummary(); s.sessions[0].ccAt = "2027-01-14T03:00:00Z"; // 2027-01-13 19:00 PST
  const { v } = await costsWorld({ summary: s, open: false, tool: { "billed compare": () => json0(withRows()) } });
  setZone(laOffset, "America/Los_Angeles");
  await click(v, "view-costs"); await settle();
  const text = costsView(v).text;
  check(text.includes("Claude Code's figure covers sessions started from 2027-01-13;"), `the local date (got ${text.slice(0, 3000)})`);
  check(/Covered 2027-01-10 – 2027-01-14 \(5 of 6 days; not billed: 2027-01-15\) · source query:q1, fetched [^|]* \(billing days are UTC days\)/.test(text), `billed days labelled (got ${text.slice(text.indexOf("Covered"), text.indexOf("Covered") + 300)})`);
};

cases["Z6-old-days"] = async () => { // round 2 (REVIEW MF1): a day older than a table is never drawn as a local day unlabelled
  const { zoneArgs, summaryZone } = zoneMod;
  setZone(laOffset, "America/Los_Angeles");
  // A summary cut by a table that reaches back 100 days only, with a local day from before it: not this panel's days.
  const short = { zone: "America/Los_Angeles", offsets: [[0, -420], [1793523600, -480]] };
  const old = summaryZone({ days: short, byDay: [{ key: "2027-01-15" }, { key: "2026-02-09" }] }, T0);
  check(old.local === false && old.label === "America/Los_Angeles", `an old local day from a short table: labelled (got ${JSON.stringify(old)})`);
  check(summaryZone({ days: LA_DAYS, byDay: [{ key: "2027-01-15" }, { key: "2026-02-09" }] }, T0).local === true, "the same old day from a table like this panel's: local");
  // The day pa-costs cut in UTC (before the table's start) says so beside its date; the others are local days.
  const s = zoneDays({ ...LA_DAYS, from: FROM0 });
  s.byDay.push({ key: "2016-12-01", tokens: 5, money: 1, calls: 1, byActivity: {}, utc: true });
  const { v } = await costsWorld({ summary: s, open: false });
  setZone(laOffset, "America/Los_Angeles");
  await click(v, "view-costs"); await settle();
  const text = costsView(v).text;
  check(/By day \|/.test(text) && text.includes("2016-12-01 (UTC)") && !text.includes("2027-01-15 (UTC)"), `the UTC day labelled, the local ones not (got ${text.slice(0, 3000)})`);
  // A zone with more changes than pa-costs takes (64): the newest kept, the table starts at the first kept one.
  setZone((ms) => (Math.floor(ms / (20 * 86400000)) % 2 ? 60 : 0), "Test/Flip");
  const a = zoneArgs(T0), parts = a[1].split(","), from = Number(a[5]);
  check(a[4] === "--zone-from" && parts.length === 64 && parts[0].startsWith("0:") && from === Number(parts[1].split(":")[0]) - 20 * 86400 && from > FROM0,
    `capped at 64 (got ${parts.length} parts, from ${a[5]}, second ${parts[1]})`);
};

// ---- t-032 J1d: the Costs view shows scopes and accounts (R-P4, R-A3, A-V3; d-117 limits 6 and 9). Only new summary keys draw
// anything new: an older summary draws exactly as before (J1D_OLD: its text captured from the c03c3e9 code). ----
const J1D_CLOCK = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }; // costs.js clock(): replaced by {clock}
function j1dText(v, s) {
  let text = costsView(v).text;
  const isos = [...(s.sessions ?? []).flatMap((x) => [x.from, x.to]), ...(s.passes ?? []).map((p) => p.from)].filter(Boolean);
  for (const iso of [...new Set(isos)]) text = text.split(new Date(iso).toLocaleString(undefined, J1D_CLOCK)).join("{clock}");
  // costs.js money(): the locale's currency affix around the amount becomes {CUR} (the existing cases assume a "." decimal too)
  let probe; try { probe = new Intl.NumberFormat(undefined, { style: "currency", currency: CUR }).format(7.5); } catch { probe = `7.50 ${CUR}`; }
  const [pre, post] = probe.split("7.50");
  const esc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (post !== undefined) text = text.replace(new RegExp(`${esc(pre)}(<?[\\d,]*\\.?\\d+)${esc(post)}`, "g"), "{CUR}$1");
  return text;
}
// The view's text in nine states: default, each session type, and the first drill of model, task, activity and day.
async function j1dStates(v, s) {
  const out = { default: j1dText(v, s) };
  for (const d of ["kind", "role", "program", "model", "machine"]) { await click(v, `costs-dim-${d}`); await settle(); out[`dim-${d}`] = j1dText(v, s); }
  await click(v, "costs-dim-model"); await click(v, "costs-kind-0"); await settle(); out["model-open"] = j1dText(v, s); await click(v, "costs-kind-0");
  await click(v, "costs-dim-kind");
  for (const k of ["task", "activity", "day"]) { await click(v, `costs-${k}-0`); await settle(); out[`${k}-open`] = j1dText(v, s); await click(v, `costs-${k}-0`); }
  await settle();
  return out;
}
const J1D_OLD = { // t-143: the same text in the new layout (section counts, Sort/Filter, the task id a link); every figure as before J1d
  "default": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-kind": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-role": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | build | 900 tokens | {CUR}11.50 est. · 92% | ▸ | (none) | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-program": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | claude | 1k tokens | {CUR}12.50 est. · 100% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-model": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | model-a | 1k tokens | {CUR}12.50 est. · 100% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-machine": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | this computer | 1k tokens | {CUR}12.50 est. · 100% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "model-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▾ | model-a | 1k tokens | {CUR}12.50 est. · 100% | t-1 | ◌ aaaa1111 · worker · build · claude · model-a · {clock} – {clock} · {CUR}7.50 est. · closed · reconstructed | t-1 | bbbb2222 · worker · build · claude · model-a · {clock} – {clock} · {CUR}4.00 est. · gone · recorded | cccc3333 · pa · claude · model-a · {clock} – {clock} · {CUR}1.00 est. · open · recorded | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "task-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▾ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ≈ shared passes split by journal entries | Phases: | build · 900 tokens · {CUR}11.50 est. | Sessions: | ◌ aaaa1111 · worker · build · claude · model-a · {clock} – {clock} · {CUR}7.50 est. · closed · reconstructed | bbbb2222 · worker · build · claude · model-a · {clock} – {clock} · {CUR}4.00 est. · gone · recorded | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "activity-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▾ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | Tasks: | t-1 | · 600 tokens · {CUR}7.50 est. | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Sort: | day | cost | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "day-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | 3 activities | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | 3 tasks | Sort: | cost | task | recent | Filter | Copy | ▸ | t-1 | One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | kind | role | program | model | machine | Copy | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Sort: | day | cost | Copy | ▾ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | build · {CUR}7.50 est. | cache rebuild after idle · {CUR}4.00 est. | unattributed · {CUR}1.00 est. | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
};
cases["P-costs-j1d-old"] = async () => { // V-J1d-1: a summary without the J1 keys draws exactly as before J1d, in every state
  const s = costsSummary();
  const { v } = await costsWorld({ summary: s });
  const got = await j1dStates(v, s);
  for (const [k, want] of Object.entries(J1D_OLD)) check(got[k] === want, `${k}: the same text as before J1d (got ${got[k]}\nwant ${want})`);
  for (const [k, text] of Object.entries(got)) check(!/By scope|Accounts|priced as|priced from the price file|· account /.test(text), `${k}: nothing of J1d (got ${text})`);
};

// A J1 summary: three scopes (global with the region unknown, priced from the default scope; regional priced as its own
// facts; another provider without a price), one account.
const J1D_GLOBAL = ["provider-a", "global", null, "acct1"], J1D_REGIONAL = ["provider-a", "regional", "region-a", "acct1"], J1D_OTHER = ["provider-b", "global", null, "acct1"];
const j1dKey = (f) => f.map((x) => x ?? "?").join(" · ");
const j1dFacts = (f) => ({ provider: f[0], endpoint: f[1], region: f[2], account: f[3] });
function j1dSummary(over = {}) {
  const base = costsSummary(), day = isoAt(T0).slice(0, 10);
  const asDefault = { scope: ["provider-a", "global", "*", "*"], validFrom: "2026-01-01" }, asRegional = { scope: J1D_REGIONAL, validFrom: "2026-01-01" };
  return costsSummary({
    totals: { ...base.totals, money: 12.5, unpricedTokens: 100 },
    byScope: [
      { key: j1dKey(J1D_GLOBAL), scope: J1D_GLOBAL, tokens: 600, money: 7.5, calls: 3, pricedAs: [asDefault], by: { providerBy: "launch", endpointBy: "rule", regionBy: "unknown", accountBy: "env" } },
      { key: j1dKey(J1D_REGIONAL), scope: J1D_REGIONAL, tokens: 300, money: 5, calls: 2, pricedAs: [asRegional], by: { providerBy: "launch", endpointBy: "rule", regionBy: "machine", accountBy: "env" } },
      { key: j1dKey(J1D_OTHER), scope: J1D_OTHER, tokens: 100, money: null, calls: 2, pricedAs: [], by: { providerBy: "rule", endpointBy: "rule", regionBy: "refused", accountBy: "env" } }],
    scopes: 3, account: "acct1", accountBy: "env",
    byAccount: [{ key: "acct1", tokens: 1000, money: 12.5, unpricedTokens: 100, calls: 7 }],
    byDayModel: [
      { day, model: "model-a", tokens: { in: 6, out: 12, cw5m: 18, cw1h: 0, cr: 564 }, money: 7.5, ...j1dFacts(J1D_GLOBAL), accountBy: "env", pricedAs: asDefault },
      { day, model: "model-a", tokens: { in: 3, out: 6, cw5m: 9, cw1h: 0, cr: 282 }, money: 5, ...j1dFacts(J1D_REGIONAL), accountBy: "env", pricedAs: asRegional },
      { day, model: "model-a", tokens: { in: 1, out: 2, cw5m: 3, cw1h: 0, cr: 94 }, money: null, ...j1dFacts(J1D_OTHER), accountBy: "env", pricedAs: null }],
    sessions: [
      { ...base.sessions[0], ...j1dFacts(J1D_GLOBAL), pricedAs: asDefault },
      { ...base.sessions[1], ...j1dFacts(J1D_REGIONAL), pricedAs: asRegional },
      { ...base.sessions[2], money: null, ...j1dFacts(J1D_OTHER), pricedAs: null }],
    ...over });
}
const section = (text, head, next) => { const at = text.indexOf(head); if (at < 0) return ""; const rest = text.slice(at + head.length); const end = next.map((n) => rest.indexOf(n)).filter((x) => x >= 0); return rest.slice(0, end.length ? Math.min(...end) : undefined); };
const AFTER = ["| By day", "| Billed", "Billed amounts", "| Accounts", "| By scope"];

cases["P-costs-j1d-one"] = async () => { // R-A3: one account: its label beside Total (and in the totals Copy); one scope priced as its own facts: no By scope, no tags, no note
  const s = j1dSummary({ byScope: [{ key: j1dKey(J1D_REGIONAL), scope: J1D_REGIONAL, tokens: 1000, money: 12.5, calls: 7, pricedAs: [{ scope: J1D_REGIONAL, validFrom: "2026-01-01" }], by: {} }],
    scopes: 1, byAccount: [{ key: "acct1", tokens: 1000, money: 12.5, calls: 7 }], totals: costsSummary().totals,
    sessions: costsSummary().sessions.map((x) => ({ ...x, ...j1dFacts(J1D_REGIONAL), pricedAs: { scope: J1D_REGIONAL, validFrom: "2026-01-01" } })) });
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-task-0"); await settle();
  const text = costsView(v).text;
  check(/1k tokens · 94% cache read · account acct1 \|/.test(text), `the account beside Total (got ${text.slice(0, 900)})`);
  check(!/By scope|\| Accounts|priced as|priced from the price file|provider-a/.test(text), `one scope, one account: no section, no tag (got ${text})`);
  const copied = await j1dCopy(v, "costs-copy-totals");
  check(/Total: [^\n]* · account acct1/.test(copied), `the totals Copy says it (got ${copied})`);
};
// The text a Copy button puts on the clipboard (the stub's __clipboard): a click on it, then the last write.
async function j1dCopy(v, id) { clipReset(); await press(v, id); await settle(); return lastClip() ?? ""; }

cases["P-costs-j1d-one-note"] = async () => { // A-V3 "always a priced as note": one scope priced from another: said once under the tiles
  const facts = ["provider-a", "global", null, "acct1"], as = { scope: ["*", "*", "*", "*"], validFrom: "2026-01-01" };
  const s = j1dSummary({ byScope: [{ key: j1dKey(facts), scope: facts, tokens: 1000, money: 12.5, calls: 7, pricedAs: [as], by: { regionBy: "unknown" } }],
    scopes: 1, byAccount: [{ key: "acct1", tokens: 1000, money: 12.5, calls: 7 }], totals: costsSummary().totals,
    sessions: costsSummary().sessions.map((x) => ({ ...x, ...j1dFacts(facts), pricedAs: as })) });
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-task-0"); await settle();
  const text = costsView(v).text;
  const note = "All costs priced from the price file's default prices valid from 2026-01-01 (no region recorded) · provider provider-a, endpoint global, account acct1"; // t-050: plain words
  check(text.includes(`| ${note} |`), `the one scope's note (got ${text.slice(0, 1200)})`);
  check((text.match(/priced from/g) ?? []).length === 1 && !/By scope|priced as/.test(text), `said once, no section, no session tags (got ${text})`);
  const copied = await j1dCopy(v, "costs-copy-totals");
  check(copied.includes(note), `in the totals Copy (got ${copied})`);
};

cases["P-costs-j1d-scopes"] = async () => { // A-V3 AC: two or more scopes: By scope, rows summing to the total; ? with its …By; the priced-as note; tags on session lines
  const s = j1dSummary();
  const { v } = await costsWorld({ summary: s });
  let text = costsView(v).text;
  const sc = section(text, "| By scope (provider · endpoint · region · account) | Copy", ["| By day"]);
  check(sc, `a By scope section with Copy (got ${text})`);
  check(sc.includes("| provider provider-a, endpoint global, account acct1, no region recorded |"), `the unknown region in words (got ${sc})`);
  check(sc.includes("| provider provider-b, endpoint global, account acct1, region value refused |"), `a refused region in words (got ${sc})`);
  check(sc.includes("provider provider-a, endpoint global, account acct1, no region recorded | priced from the price file's prices for provider provider-a, endpoint global valid from 2026-01-01 |"), `the default scope's row says how it was priced (got ${sc})`);
  check(sc.includes("provider provider-a, endpoint regional, region region-a, account acct1 | 300 tokens"), `a row priced as its own facts: no note (got ${sc})`);
  check(/provider provider-b, endpoint global, account acct1, region value refused \| no price \| 100 tokens \| — est\./.test(sc), `no price (got ${sc})`);
  check(!/\?|\*|\(default\)|priced as/.test(sc), `no ?, *, (default) or priced as in By scope (got ${sc})`);
  const money = [...sc.matchAll(/(\d+\.\d\d) est\./g)].map((m) => Number(m[1])), toks = [...sc.matchAll(/\| (\d+) tokens/g)].map((m) => Number(m[1]));
  check(Math.abs(money.reduce((a, b) => a + b, 0) - 12.5) < 1e-9 && toks.reduce((a, b) => a + b, 0) === 1000, `the rows sum to the Total (got ${money}, ${toks})`);
  check(!/\| Accounts \|/.test(text) && /account acct1 \|/.test(text), `one account: beside Total, no Accounts (got ${text.slice(0, 900)})`);
  // the drill: models of the scope (from byDayModel), then its sessions
  await click(v, "costs-scope-0"); await settle(); text = costsView(v).text;
  const d = section(text, "| By scope (provider · endpoint · region · account) | Copy", ["| By day"]);
  check(/Models \(from \d{4}-\d{2}-\d{2}\): \| model-a · 600 tokens · [^|]*7\.50[^|]* est\. · priced from the price file's prices for provider provider-a, endpoint global \|/.test(d), `the scope's models (got ${d})`);
  check(/Sessions: \| ◌ aaaa1111 /.test(d) && !d.includes("bbbb2222"), `the scope's sessions (got ${d})`);
  const copied = await j1dCopy(v, "costs-copy-scope");
  check(copied.startsWith("By scope (provider · endpoint · region · account)\nprovider provider-a, endpoint global, account acct1, no region recorded · priced from the price file's prices"), `the section's Copy (got ${copied})`);
  // session lines are tagged, with the note where it applies
  await click(v, "costs-task-0"); await settle(); text = costsView(v).text;
  check(/aaaa1111 · worker · build · claude · model-a \(provider provider-a, endpoint global, account acct1, no region recorded\) · [^|]* est\. · priced from the price file's prices for provider provider-a, endpoint global · closed/.test(text), `a tagged session line with its note (got ${text})`);
  check(/bbbb2222 · worker · build · claude · model-a \(provider provider-a, endpoint regional, region region-a, account acct1\) · [^|]* est\. · gone/.test(text), `no note where priced as its facts (got ${text})`);
  // a model row's drill starts with its scopes
  await click(v, "costs-task-0"); await click(v, "costs-dim-model"); await click(v, "costs-kind-0"); await settle(); text = costsView(v).text;
  const m = section(text, "| By session type", ["| By day"]);
  check(/Scopes \(from \d{4}-\d{2}-\d{2}\): \| model-a \(provider provider-a, endpoint global, account acct1, no region recorded\) · 600 tokens · [^|]* est\. · priced from the price file's prices for provider provider-a, endpoint global \| model-a \(provider provider-a, endpoint regional, region region-a, account acct1\) · 300 tokens · [^|]* est\. \| model-a \(provider provider-b, endpoint global, account acct1, no region recorded\) · 100 tokens · — est\. · no price \|/.test(m), `the model's scopes (got ${m})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
};

cases["P-costs-j1d-accounts"] = async () => { // R-A3 AC: two accounts: an Accounts section summing to the total, its drill; none beside Total
  const s = j1dSummary({ byAccount: [{ key: "acct-a", tokens: 900, money: 9.5, calls: 5 }, { key: "acct-b", tokens: 100, money: 3, calls: 2 }],
    sessions: j1dSummary().sessions.map((x, i) => ({ ...x, account: i === 2 ? "acct-b" : "acct-a" })) });
  const { v } = await costsWorld({ summary: s });
  let text = costsView(v).text;
  check(!/· account /.test(text), `no single account beside Total (got ${text.slice(0, 900)})`);
  const a = section(text, "| Accounts | Copy", ["| By scope", "| By day"]);
  check(/acct-a \| 900 tokens \| [^|]*9\.50[^|]* est\. · 76% \|/.test(a) && /acct-b \| 100 tokens \| [^|]*3\.00[^|]* est\. · 24% \|/.test(`${a}|`), `one row per account (got ${a})`); // section() ends before the next head's |
  check(text.indexOf("| Accounts |") > text.indexOf("| By session type") && text.indexOf("| Accounts |") < text.indexOf("| By scope"), "after session type, before By scope");
  await click(v, "costs-account-1"); await settle(); text = costsView(v).text;
  const d = section(text, "| Accounts | Copy", ["| By scope", "| By day"]);
  check(/Sessions: \| cccc3333 /.test(d) && !d.includes("aaaa1111"), `the account's sessions (got ${d})`);
};

cases["P-costs-j1d-spans"] = async () => { // d-117 limit 9: a session whose rows span scopes says so
  const s = j1dSummary(); s.sessions[0].scopes = 2;
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-task-0"); await settle();
  const text = costsView(v).text;
  check(/aaaa1111 [^|]* · rows in 2 scopes \(the largest shown\)/.test(text), `rows in 2 scopes (got ${text})`);
  check(!/bbbb2222 [^|]*rows in/.test(text), `not on a one-scope session (got ${text})`);
};

cases["P-costs-j1d-reads"] = async () => { // V-J1d-4: the same reads and execs as an older summary; malformed keys draw nothing and throw nothing
  const count = async (summary) => { const { v } = await costsWorld({ summary }); for (let i = 0; i < 3; i++) { advance(11000); v.reload(cx); look(v); await settle(); } return { v, reads: costsReads().map((x) => x.path), execs: costsExecs().map(([, a]) => a.join(" ")) }; };
  const old = await count(costsSummary()), now = await count(j1dSummary());
  check(JSON.stringify(old.reads) === JSON.stringify(now.reads) && JSON.stringify(old.execs) === JSON.stringify(now.execs), `the same reads and execs (old ${JSON.stringify(old)} new ${JSON.stringify({ reads: now.reads, execs: now.execs })})`);
  const bad = costsSummary({ byScope: { a: 1 }, scopes: "2", byAccount: [null, 5, "x"], byDayModel: [7, { model: "model-a", pricedAs: 7, provider: {} }],
    sessions: costsSummary().sessions.map((x) => ({ ...x, pricedAs: 7, provider: { x: 1 }, scopes: "many" })) });
  const { v } = await costsWorld({ summary: bad });
  await click(v, "costs-task-0"); await click(v, "costs-dim-model"); await click(v, "costs-kind-0"); await settle();
  const text = costsView(v).text;
  check(!/By scope|\| Accounts|priced as|priced from the price file|\[object|rows in|· account /.test(text), `malformed keys draw nothing (got ${text})`);
};

cases["P-costs-t050-words"] = async () => { // t-050: the one-scope note in plain words: all facts unknown (the person's line, two price dates); every fact known, priced from a wider row
  const none = [null, null, null, "work"], all = ["*", "*", "*", "*"];
  const s = j1dSummary({ byScope: [{ key: j1dKey(none), scope: none, tokens: 1000, money: 12.5, calls: 7, pricedAs: [{ scope: all, validFrom: "2026-09-01" }, { scope: all, validFrom: "2026-11-01" }],
    by: { providerBy: "default", endpointBy: "default", regionBy: "unknown", accountBy: "rule" } }],
    scopes: 1, account: "work", byAccount: [{ key: "work", tokens: 1000, money: 12.5, calls: 7 }], totals: costsSummary().totals,
    sessions: costsSummary().sessions.map((x) => ({ ...x, ...j1dFacts(none), pricedAs: { scope: all, validFrom: "2026-09-01" } })) });
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-task-0"); await settle();
  const text = costsView(v).text;
  const note = "All costs priced from the price file's default prices valid from 2026-09-01 and 2026-11-01 (no provider, endpoint or region recorded) · account work";
  check(text.includes(`| ${note} |`), `all unknown: the note in words (got ${text.slice(0, 1200)})`);
  check(!/\? \(|\* ·|· \*|\(default\)|priced as/.test(text), `no ?, *, (default) or priced as anywhere (got ${text})`);
  check((await j1dCopy(v, "costs-copy-totals")).includes(note), "in the totals Copy");
  const known = ["provider-a", "regional", "region-a", "acct1"];
  const s2 = j1dSummary({ byScope: [{ key: j1dKey(known), scope: known, tokens: 1000, money: 12.5, calls: 7, pricedAs: [{ scope: ["provider-a", "*", "*", "*"], validFrom: "2026-01-01" }], by: {} }],
    scopes: 1, byAccount: [{ key: "acct1", tokens: 1000, money: 12.5, calls: 7 }], totals: costsSummary().totals });
  const w2 = await costsWorld({ summary: s2 });
  const t2 = costsView(w2.v).text;
  check(t2.includes("| All costs priced from the price file's prices for provider provider-a valid from 2026-01-01 · provider provider-a, endpoint regional, region region-a, account acct1 |"), `every fact known: named, no parentheses (got ${t2.slice(0, 1200)})`);
};

// ---- t-045: a decision card stays answered once the person submits (Wanted 1-4) ----
// The card's write on m1 (python3 -c REMOTE_WRITE) held until release(); `after` runs when it is called.
function holdWrite({ reply = null, after = null } = {}) {
  let release; const held = new Promise((r) => { release = r; });
  const prior = g.__execFn;
  g.__execFn = (command, args, on, options) => {
    if (command === "python3" && args[0] === "-c" && args.length === 5) {
      after?.();
      return held.then(() => {
        if (reply) return reply;
        g.__files[`${on ?? ""}:${args[2]}`] = args[4]; (g.__writes ??= []).push(`${on ?? ""}:${args[2]}`);
        return { code: 0, stdout: "", stderr: "" };
      });
    }
    return prior ? prior(command, args, on, options) : undefined;
  };
  return () => release();
}
// Whether a tree has a text field (a note input) anywhere.
const hasInput = (x) => Boolean(x && typeof x === "object" && (x.kind === "input" || (x.kids ?? []).some(hasInput)));
const optClicks = (clicks, id) => Object.keys(clicks).filter((k) => k.startsWith(`opt-${id}-`) || k === `submit-${id}`);
const R1 = `${M}:${RDIR}/decisions/d-01.json`;
async function remoteCardWorld() {
  world({ machine: M }); remoteCards(1);
  const v = await mount(); await settle();
  await click(v, "view-decisions");
  return v;
}
const locked = (v, id, label, what) => {
  const tree = v.render(cx), { text, clicks } = inspect(tree);
  check(text.includes(`answered: ${label}`), `${what}: the chip says answered: ${label} (got ${text.slice(0, 600)})`);
  check(!/\| open \|/.test(text), `${what}: no open chip (got ${text.slice(0, 600)})`);
  check(optClicks(clicks, id).length === 0, `${what}: no option or Submit to click (got ${optClicks(clicks, id)})`);
  check(!hasInput(tree), `${what}: no note field`);
  return text;
};

cases["P45-locked-while-saving"] = async () => { // W1: from the Submit click: answered, no option, no Submit, no note; also after the write while the reload is slow
  const v = await remoteCardWorld();
  const release = holdWrite();
  await click(v, "opt-d-01-0"); await click(v, "submit-d-01"); await settle();
  const t = locked(v, "d-01", "Yes", "write in flight");
  check(t.includes("Saving…"), `says Saving… (got ${t.slice(0, 600)})`);
  check(JSON.parse(g.__files[R1]).status === "open", "the file is not written yet");
  g.__remoteDelay = 1e9; // the reload after the write: every read on m1 waits
  release(); await settle(); await settle();
  check(JSON.parse(g.__files[R1]).answer === "Yes", "the file is answered Yes");
  const t2 = locked(v, "d-01", "Yes", "written, reload slow");
  check(!t2.includes("Saving…"), `no Saving… once written (got ${t2.slice(0, 600)})`);
  g.__remoteDelay = 0; await step(1e9); await settle(); await settle(); await settle();
  for (let i = 0; i < 4 && !look(v).text.includes("1 answered"); i++) await step(D);
  const t3 = look(v).text;
  check(t3.includes("1 answered") && !/\| open \|/.test(t3) && optClicks(look(v).clicks, "d-01").length === 0, `the read confirms: in the answered fold (got ${t3.slice(0, 600)})`);
};

cases["P45-old-read-after-write"] = async () => { // W2: a refresh that read the open card before the write commits after it; the timer; a space switch and back
  world({ machine: M }); remoteCards(1); addRemoteSpace("sp2", "other", ledgerOf(1, "o"));
  const v = await mount(); await settle();
  await click(v, "view-decisions");
  let releaseRead; const heldRead = new Promise((r) => { releaseRead = r; });
  g.__execFn = (command, args) => {
    if (stubs.execKind(command, args) !== "batch" || !String(args[3]).includes(`/${S}/`)) return undefined;
    const old = g.__files[R1];
    return heldRead.then(() => ({ code: 0, stdout: `d-01.json\0${old}\0`, stderr: "" }));
  };
  advance(11000); v.reload(cx); await settle(); // the 10 s refresh: its card read holds the open copy
  await click(v, "opt-d-01-0"); await click(v, "submit-d-01"); await settle();
  check(JSON.parse(g.__files[R1]).answer === "Yes", "the file is answered Yes");
  locked(v, "d-01", "Yes", "before the old read lands");
  g.__remoteDelay = D; releaseRead(); await settle(); await settle();
  locked(v, "d-01", "Yes", "the old read landed");
  // Another old read, from the timer, while the mark is not yet resolved.
  let releaseRead2; const heldRead2 = new Promise((r) => { releaseRead2 = r; });
  g.__execFn = (command, args) => (stubs.execKind(command, args) === "batch" && String(args[3]).includes(`/${S}/`)
    ? heldRead2.then(() => ({ code: 0, stdout: `d-01.json\0${openCard("d-01")}\0`, stderr: "" })) : undefined);
  await fire(10000);
  locked(v, "d-01", "Yes", "timer refresh in flight");
  // A space switch away and back: the kept copy of sp1 (read open) is shown at once.
  show(`${M}/sp2`); v.reload(cx); await settle();
  show(RS); v.reload(cx); await settle();
  await click(v, "view-decisions");
  locked(v, "d-01", "Yes", "back from another space (the kept copy)");
  releaseRead2(); await settle(); await settle();
  locked(v, "d-01", "Yes", "the timer's old read landed");
  g.__execFn = null;
  for (let i = 0; i < 12 && !look(v).text.includes("1 answered"); i++) { advance(11000); v.reload(cx); await step(D); await step(D); await step(D); }
  const t = look(v).text;
  check(t.includes("1 answered") && !/\| open \|/.test(t), `a read after the write: in the answered fold (got ${t.slice(0, 600)})`);
};

cases["P45-second-window"] = async () => { // W2: a second panel window draws the card answered from the submit, and refuses a click
  const v = await cardsWorld({}, ["d-x"]);
  const w = await mount();
  await click(v, "opt-d-x-0"); await click(v, "submit-d-x");
  check(JSON.parse(g.__files[`:${DIR}/decisions/d-x.json`]).answer === "A", "written A");
  const t = locked(w, "d-x", "A", "the other window, before its own read");
  check(t.includes("Answered in another panel window."), `says where (got ${t.slice(0, 600)})`);
  w.reload(cx); await settle();
  const t2 = look(w).text;
  check(t2.includes("1 answered") && !/\| open \|/.test(t2), `its own read: in the answered fold (got ${t2.slice(0, 600)})`);
  // The shared mark ends: after ANSWERING_TTL (120 s) a window that never read the card shows the file it has.
  const u = await mount();
  u.cards = u.cards.map((c) => (c.id === "d-x" ? { ...c, card: { ...c.card, status: "open", answer: null } } : c));
  advance(121000);
  check(/\| open \|/.test(look(u).text), `after the TTL the shared mark no longer locks (got ${look(u).text.slice(0, 400)})`);
};

cases["P45-reopened"] = async () => { // W2: a real change after the answer (the card reopened with new text) is shown by a newer read
  const v = await cardsWorld({}, ["d-x"]);
  g.__delay = { [`:${DIR}/agent.json`]: 5000 };
  await click(v, "opt-d-x-0"); await click(v, "submit-d-x");
  locked(v, "d-x", "A", "reload slow");
  g.__files[`:${DIR}/decisions/d-x.json`] = JSON.stringify({ id: "d-x", status: "open", question: "Q d-x, asked again?", options: [{ label: "A" }, { label: "B" }], created: "2026-11-16T11:00:00Z" });
  await step(5000); await settle();
  const { text, clicks } = look(v);
  check(/\| open \|/.test(text) && text.includes("Q d-x, asked again?") && Boolean(clicks["opt-d-x-1"]), `the reopened card is drawn open (got ${text.slice(0, 600)})`);
};

cases["P45-write-failed"] = async () => { // W3: only a write that really failed unlocks the card, saying so
  const v = await remoteCardWorld();
  const text0 = g.__files[R1];
  const release = holdWrite({ reply: { code: 3, stdout: "", stderr: "" } }); // the compare failed: nothing written
  await click(v, "opt-d-01-0"); await click(v, "submit-d-01"); await settle();
  locked(v, "d-01", "Yes", "in flight");
  release(); await settle(); await settle();
  const { text, clicks } = look(v);
  check(g.__files[R1] === text0, "the file is unchanged");
  check(/Not saved: .*Pick an option and submit again\./.test(text), `says Not saved (got ${text.slice(0, 800)})`);
  check(/\| open \|/.test(text) && Boolean(clicks["opt-d-01-1"]), `the card is open again (got ${Object.keys(clicks)})`);
  // A failure the panel cannot tell: the write failed and the re-read failed too.
  const v2 = await remoteCardWorld();
  const release2 = holdWrite({ reply: { code: 1, stdout: "", stderr: "disk full" }, after: () => { g.__fail = [R1]; } });
  await click(v2, "opt-d-01-0"); await click(v2, "submit-d-01"); await settle();
  g.__remoteDelay = 1e9; release2(); await settle(); await settle();
  await step(10000); // the re-read on the slow machine reaches its 10 s cap: the panel cannot tell
  const t2 = locked(v2, "d-01", "Yes", "unsure");
  check(t2.includes("Could not tell whether the answer was saved; reading the card again."), `says it could not tell (got ${t2.slice(0, 800)})`);
  g.__fail = []; g.__remoteDelay = 0; await step(1e9); await settle(); await settle();
  for (let i = 0; i < 4 && !/Not saved/.test(look(v2).text); i++) await step(D);
  const t3 = look(v2).text;
  check(/Not saved: disk full/.test(t3) && Boolean(look(v2).clicks["opt-d-01-1"]), `a fresh open read: Not saved, open again (got ${t3.slice(0, 800)})`);
};

cases["P45-rule-change-fails"] = async () => { // W3: the answer saved, the rule not changed: the card stays answered with its own message
  world();
  g.__files[RULES] = "{ not json";
  g.__files[`:${DIR}/decisions/d-r.json`] = JSON.stringify({ id: "d-r", status: "open", rule: "r-p", question: "Adopt rule r-p?",
    options: [{ label: "Approve" }, { label: "Decline" }], created: "2026-11-16T10:00:00Z" });
  g.__ls[`:${DIR}/decisions`] = ["d-r.json"];
  const v = await mount(); await settle();
  g.__delay = { [`:${DIR}/agent.json`]: 5000 };
  await click(v, "opt-d-r-0"); await click(v, "submit-d-r");
  check(JSON.parse(g.__files[`:${DIR}/decisions/d-r.json`]).answer === "Approve", "the answer is written");
  const t = locked(v, "d-r", "Approve", "rule change failed");
  check(t.includes("The answer was saved; the rule was not changed:"), `its own message (got ${t.slice(0, 800)})`);
  check(!/Not saved/.test(t), "not called Not saved");
};

cases["P45-push-in-flight"] = async () => { // W1 (t124): while pa-push-main run is in flight, no option is clickable and no Submit
  const gate = deferred();
  const { v } = await pushWorld({ tool: { run: () => gate.promise } });
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const { text, clicks } = look(v);
  check(text.includes("Pushing…") && optClicks(clicks, PID).length === 0, `no option or Submit while it runs (got ${optClicks(clicks, PID)})`);
  gate.resolve(json(1, { ok: false, refusal: { check: "C9", message: "remote moved" } })); await settle(); await settle();
  check(Boolean(look(v).clicks[`opt-${PID}-1`]), "a refusal: the card is open again");
};

// ---- t-100: a push card keeps the person's chosen answer selected and locked while its tool runs. The stub theme gives
// every colour one value, so these draw the card with a theme whose colours are their own names. ----
const named = new Proxy({}, { get: (_t, k) => String(k) });
const byKey = (x, key) => { if (!x || typeof x !== "object") return null; if (x.key === key) return x; for (const k of x.kids ?? []) { const f = byKey(k, key); if (f) return f; } return null; };
// Each option row of the push card: drawn chosen (bg selected, border accent) or not, and clickable or not.
const pushRows = (v) => {
  const tree = v.card(named, v.cards.find((c) => c.id === PID), g.__spaces[0]);
  return Object.fromEntries(["0", "1", "other"].map((i) => { const r = byKey(tree, `opt-${PID}-${i}`); return [i, { chosen: r?.style?.bg?.[0] === "selected" && r?.style?.border_color?.[0] === "accent", click: Boolean(r?.click) }]; }));
};

cases["P100-push-keeps-choice"] = async () => { // t-100: while pa-push-main run runs, Push stays chosen and every option is locked; after it, still Push
  const gate = deferred();
  let file;
  const { v, file: f } = await pushWorld({ tool: { run: () => gate.promise.then((reply) => { g.__files[file] = answeredByTool(); return reply; }) } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  check(pushRows(v)["0"].chosen, "Push chosen before Submit");
  await click(v, `submit-${PID}`); await settle();
  const rows = pushRows(v), { text, clicks } = look(v);
  check(text.includes("Pushing…") && !clicks[`submit-${PID}`], `Pushing…, Submit off (got ${Object.keys(clicks)})`);
  check(rows["0"].chosen, `Push still chosen while it runs (got ${JSON.stringify(rows)})`);
  check(!rows["1"].chosen && !rows.other.chosen, `Not now and Other not chosen (got ${JSON.stringify(rows)})`);
  check(!rows["0"].click && !rows["1"].click && !rows.other.click, `every option locked (got ${JSON.stringify(rows)})`);
  gate.resolve(json(0, toolPushed)); await settle();
  const after = pushRows(v);
  check(after["0"].chosen && !after["1"].chosen && !after["0"].click && !after["1"].click, `after the push: Push chosen, locked (got ${JSON.stringify(after)})`);
  await settle();
  check(pushRows(v)["0"].chosen, "after the reload: Push chosen (the card file's answer)");
};

cases["P100-push-refused-clears"] = async () => { // t-100: a refused push leaves no option chosen and every option open, so the person chooses again
  const gate = deferred();
  const { v } = await pushWorld({ tool: { run: () => gate.promise } });
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  check(pushRows(v)["0"].chosen, "Push chosen while it runs");
  gate.resolve(json(1, { ok: false, refusal: { check: "C9", message: "remote moved" } })); await settle(); await settle();
  const rows = pushRows(v);
  check(!rows["0"].chosen && !rows["1"].chosen && !rows.other.chosen, `refused: nothing chosen (got ${JSON.stringify(rows)})`);
  check(rows["0"].click && rows["1"].click && rows.other.click, `refused: every option open again (got ${JSON.stringify(rows)})`);
  await click(v, `opt-${PID}-1`); await settle();
  check(pushRows(v)["1"].chosen, "the person can choose again");
};

cases["P100-other-keeps-words"] = async () => { // t-100: Other on a push card stays chosen, with its words as they are, while its run runs
  const gate = deferred();
  const { v } = await pushWorld({ tool: { run: () => gate.promise } });
  await click(v, `opt-${PID}-other`); typeInto(v, PID, "do X first");
  await click(v, `submit-${PID}`); await settle();
  const rows = pushRows(v);
  check(rows.other.chosen && !rows["0"].chosen && !rows["1"].chosen, `Other chosen while it runs (got ${JSON.stringify(rows)})`);
  check(!rows.other.click && !rows["0"].click, "locked");
  check(v.notes[PKEY].value() === "do X first", `its words as they are (got ${JSON.stringify(v.notes[PKEY].value())})`);
  gate.resolve(json(1, { ok: false, refusal: { check: "C9", message: "remote moved" } })); await settle(); await settle();
};

cases["P45-push-no-second-run"] = async () => { // W1/W4: after a Push from here, while the reload is slow: no option, no Submit, one run
  let file;
  const { v, file: f } = await pushWorld({ tool: { run: () => { g.__files[file] = answeredByTool(); return json(0, toolPushed); } } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  g.__delay = { [`:${DIR}/agent.json`]: 5000 };
  await click(v, `submit-${PID}`); await settle(); await settle();
  const { text, clicks } = look(v);
  check(JSON.parse(g.__files[file]).answer === "Push", "the tool wrote Push");
  check(text.includes("answered: Push") && optClicks(clicks, PID).length === 0, `answered: Push, no option or Submit (got ${optClicks(clicks, PID)}; ${text.slice(0, 600)})`);
  // Even called directly (a stale click), this copy runs pa-push-main once.
  v.push.run(g.__spaces[0], v.cards.find((c) => c.id === PID), "Not now", "", cx, () => {}); await settle();
  check(pushExecs("run").length === 1, `one run (got ${pushExecs("run").length})`);
};

cases["P45-push-answer-differs"] = async () => { // W4: a run whose exit 0 says another answer is shown and told as that answer, never as the label asked
  let file;
  const already = { ...toolPushed, message: `${PID} was already pushed at 2026-11-16T10:01:00Z (aa22000..cc11000); nothing to do.` };
  const { v, file: f } = await pushWorld({ tool: { run: () => { g.__files[file] = answeredByTool(); return json(0, already); } } });
  file = f;
  await click(v, `opt-${PID}-1`); await settle();
  await click(v, `submit-${PID}`); await settle(); await settle();
  const { text } = look(v);
  check(!text.includes("Answered Not now"), `not shown as Not now (got ${text.slice(0, 1200)})`);
  check(text.includes(`Pushed ${PURL} main aa22000..cc11000`), `shown as pushed (got ${text.slice(0, 1200)})`);
  const told = v.unsent.map((x) => x.message);
  check(told.length === 1 && told[0].startsWith(`decision ${PID} was already answered Push; `) && !told.some((m) => /answered Not now/.test(m)), `the notice (got ${JSON.stringify(told)})`);
};

// ---- t-045 round 2: the mark's resolution (REVIEW MF2, VERIFY MF1), push unlock (REVIEW MF1 = VERIFY MF2), push across windows (REVIEW MF3) ----
cases["P45-saved-then-fallback-read"] = async () => { // REVIEW MF2: the write landed, its exec said an error, the re-read found it answered; a later load's card read falls back to the open copy
  const v = await remoteCardWorld();
  g.__execFn = (command, args, on) => {
    if (command === "python3" && args[0] === "-c" && args.length === 5) { g.__files[`${on}:${args[2]}`] = args[4]; return { code: 1, stdout: "", stderr: "connection reset" }; }
    return undefined;
  };
  g.__delay = { [`${M}:${RDIR}/agent.json`]: 5000 }; // the reload after the write is slow
  await click(v, "opt-d-01-0"); await click(v, "submit-d-01"); await settle(); await settle();
  check(JSON.parse(g.__files[R1]).answer === "Yes", "the file is answered Yes");
  locked(v, "d-01", "Yes", "the re-read found it answered");
  g.__execFn = null; g.__batchFail = { [M]: { code: 1 } }; g.__fail = [R1]; // the batch fails; R1's own read fails: its last known (open) copy
  await step(5000); await settle(); await settle(); g.__delay = {};
  for (let i = 0; i < 4; i++) { advance(11000); v.reload(cx); await step(D); await step(D); await step(D); }
  const t = locked(v, "d-01", "Yes", "a load whose card read fell back to the open copy");
  check(!/Not saved/.test(t), `not called Not saved (got ${t.slice(0, 800)})`);
  g.__batchFail = {}; g.__fail = [];
  for (let i = 0; i < 6 && !look(v).text.includes("1 answered"); i++) { advance(11000); v.reload(cx); await step(D); await step(D); await step(D); }
  const t2 = look(v).text;
  check(t2.includes("1 answered") && !/\| open \|/.test(t2), `a real read: in the answered fold (got ${t2.slice(0, 600)})`);
};

cases["P45-unsure-then-fallback-read"] = async () => { // VERIFY MF1 (V-unsure-then-fallback-read): the write landed, its exec errored, the re-read failed; a fallback copy is not a read
  const v = await remoteCardWorld();
  g.__execFn = (command, args, on) => {
    if (command === "python3" && args[0] === "-c" && args.length === 5) { g.__files[`${on}:${args[2]}`] = args[4]; g.__fail = [R1]; g.__batchFail = { [M]: { code: 1 } }; return { code: 1, stdout: "", stderr: "connection reset" }; }
    return undefined;
  };
  await click(v, "opt-d-01-0"); await click(v, "submit-d-01"); await settle(); await settle();
  check(JSON.parse(g.__files[R1]).answer === "Yes", "the file is answered Yes");
  const t0 = locked(v, "d-01", "Yes", "unsure");
  check(t0.includes("Could not tell whether the answer was saved"), `says it could not tell (got ${t0.slice(0, 800)})`);
  g.__execFn = null;
  for (let i = 0; i < 4; i++) { advance(11000); v.reload(cx); await step(D); await step(D); await step(D); }
  const t = locked(v, "d-01", "Yes", "a load whose card read fell back to the open copy");
  check(!/Not saved/.test(t) && t.includes("Could not tell whether the answer was saved"), `still unsure, not Not saved (got ${t.slice(0, 800)})`);
  g.__fail = []; g.__batchFail = {};
  for (let i = 0; i < 6 && !look(v).text.includes("1 answered"); i++) { advance(11000); v.reload(cx); await step(D); await step(D); await step(D); }
  const t2 = look(v).text;
  check(t2.includes("1 answered") && !/\| open \|/.test(t2) && !/Not saved/.test(t2), `a real read: in the answered fold (got ${t2.slice(0, 600)})`);
};

// An unknown run result (exit 2, no JSON), then the check `after` gives: whether Not now can be picked and submitted.
async function pushUnknownThen(after) {
  let ran = false;
  const { v } = await pushWorld({ tool: { run: () => { ran = true; return { code: 2, stdout: "", stderr: "crashed" }; }, check: () => (!ran ? json(0, checkReply()) : after()) } });
  await click(v, `opt-${PID}-0`); await settle(); await click(v, `submit-${PID}`); await settle(); await settle(); await settle();
  return v;
}
cases["P45-push-unknown-refused-open"] = async () => { // REVIEW MF1 = VERIFY MF2: after an unknown result, a check that reads the card open but refuses unlocks it
  for (const refusal of [{ check: "C9", message: "remote moved" }, { check: "C8", message: "the remote did not answer" }]) {
    const v = await pushUnknownThen(() => json(1, checkReply({ ok: false, refusal, seen: null })));
    const { text, clicks } = look(v);
    check(text.includes("The result is unknown") && Boolean(clicks[`opt-${PID}-1`]), `${refusal.check}: Not now can be picked (got ${optClicks(clicks, PID)})`);
    await click(v, `opt-${PID}-1`); await settle();
    check(Boolean(look(v).clicks[`submit-${PID}`]), `${refusal.check}: Not now can be submitted`);
  }
  // Not known open: a check that could not run, one that reads the card answered, one that finds it pushed (C4): locked.
  for (const [what, reply] of [["no check", () => { throw new Error("unreachable"); }],
    ["answered", () => json(1, checkReply({ ok: false, status: "answered", refusal: { check: "C4", message: "answered" }, seen: null }))],
    ["pushed", () => json(1, checkReply({ ok: false, done: true, refusal: { check: "C4", message: "already pushed" }, seen: null }))]]) {
    const v = await pushUnknownThen(reply);
    check(optClicks(look(v).clicks, PID).length === 0, `${what}: still locked (got ${optClicks(look(v).clicks, PID)})`);
  }
};

cases["P45-push-second-window"] = async () => { // REVIEW MF3: a push answered in one window is locked in another, which never runs the tool
  const gate = deferred();
  let file;
  const { v, file: f } = await pushWorld({ tool: { run: () => gate.promise.then((reply) => { g.__files[file] = answeredByTool(); return reply; }) } });
  file = f;
  const w = await mount(); await settle();
  await click(v, `opt-${PID}-0`); await settle();
  await click(v, `submit-${PID}`); await settle();
  const t = locked(w, PID, "Push", "the other window, run in flight");
  check(t.includes("Answered in another panel window."), `says where (got ${t.slice(0, 600)})`);
  w.answer(g.__spaces[0], w.cards.find((c) => c.id === PID), "Not now", cx); await settle(); // a stale click there
  advance(130000); // a long push: still locked past the 120 s mark
  locked(w, PID, "Push", "the run still in flight after 130 s");
  gate.resolve(json(0, toolPushed)); await settle(); await settle();
  locked(w, PID, "Push", "the run ended, before its own read");
  check(pushExecs("run").length === 1, `one run (got ${pushExecs("run").length})`);
  // A refusal leaves the card open: the other window may answer it again.
  const gate2 = deferred();
  const { v: v2 } = await pushWorld({ tool: { run: () => gate2.promise } });
  const w2 = await mount(); await settle();
  await click(v2, `opt-${PID}-0`); await settle(); await click(v2, `submit-${PID}`); await settle();
  locked(w2, PID, "Push", "second world, in flight");
  gate2.resolve(json(1, { ok: false, refusal: { check: "C9", message: "remote moved" } })); await settle(); await settle();
  check(Boolean(look(w2).clicks[`opt-${PID}-1`]), "refused: the other window can answer again");
};

// Round 3 (VERIFY MF-A): a push whose result is unknown. Every window keeps the card locked until a check from that
// window, asked after the unknown result, reads it open; the shared mark's 120 s never unlocks it.
const W3 = (w) => optClicks(look(w).clicks, PID);
async function pushUnknownTwoWindows(checkMode) {
  let ran = 0;
  const { v } = await pushWorld({ tool: { run: () => { ran++; return ran === 1 ? new Promise(() => {}) : json(0, { ...runPushed, answer: "Not now", done: false, result: null, message: "answered Not now" }); },
    check: () => (!ran ? json(0, checkReply()) : checkMode.now === "fail" ? (() => { throw new Error("unreachable"); })() : json(1, checkReply({ ok: false, refusal: { check: "C9", message: "remote moved" }, seen: null }))) } });
  return v;
}
cases["P45-push-unknown-other-window"] = async () => { // V2-push-unknown-other-window-runs: w never unlocks on the mark's expiry
  const mode = { now: "fail" };
  const v = await pushUnknownTwoWindows(mode);
  const w = await mount(); await settle();
  await click(v, `opt-${PID}-0`); await settle(); await click(v, `submit-${PID}`); await settle();
  await step(600000); await step(5000); await step(1000); await settle(); await settle();
  check(W3(v).length === 0 && W3(w).length === 0, `both locked after the unknown result (v ${W3(v)}, w ${W3(w)})`);
  for (const ms of [125000, 600000, 3600000]) {
    advance(ms); await fire(10000); w.reload(cx); await settle();
    check(W3(w).length === 0, `w locked ${ms / 1000} s later, no check read it open (got ${W3(w)})`);
  }
  check(look(w).text.includes("unknown"), `w says the result is unknown (got ${look(w).text.slice(0, 800)})`);
  w.answer(g.__spaces[0], w.cards.find((c) => c.id === PID), "Not now", cx); await settle(); // a stale click there
  await click(w, `push-check-${PID}`); await settle(); await settle(); // w's check cannot run: still locked
  check(W3(w).length === 0, `w's check could not run: locked (got ${W3(w)})`);
  check(pushExecs("run").length === 1, `one run (got ${pushExecs("run").length})`);
  mode.now = "open"; await click(w, `push-check-${PID}`); await settle(); await settle(); // w's own check reads it open (C9)
  check(W3(w).includes(`opt-${PID}-1`), `w's check read it open: Not now can be picked (got ${W3(w)})`);
  check(W3(v).length === 0, `v keeps its own rule: no check of its read it open (got ${W3(v)})`);
  await click(w, `opt-${PID}-1`); await settle(); await click(w, `submit-${PID}`); await settle(); await settle();
  check(pushExecs("run").length === 2, `a run only after w's check read it open (got ${pushExecs("run").length})`);
};

cases["P45-push-unknown-owner-gone"] = async () => { // the same with the owner window gone: a later window applies the rule itself
  const mode = { now: "fail" };
  let v = await pushUnknownTwoWindows(mode);
  await click(v, `opt-${PID}-0`); await settle(); await click(v, `submit-${PID}`); await settle();
  await step(600000); await step(5000); await step(1000); await settle(); await settle();
  v = null; // the owner window is gone; its shared mark stays
  advance(600000);
  const w = await mount(); await settle(); await settle(); // a window opened later: its first check cannot run
  check(look(w).text.includes("check could not run") && W3(w).length === 0, `w locked, its first check could not run (got ${W3(w)})`);
  for (let i = 0; i < 4; i++) { advance(1800000); await fire(10000); w.reload(cx); await settle(); }
  check(W3(w).length === 0, `w locked 2 h later (got ${W3(w)})`);
  w.answerPush(g.__spaces[0], w.cards.find((c) => c.id === PID), "Not now", cx); await settle(); // a stale call
  check(pushExecs("run").length === 1, `one run (got ${pushExecs("run").length})`);
  mode.now = "open"; await click(w, `push-check-${PID}`); await settle(); await settle();
  check(W3(w).includes(`opt-${PID}-1`), `w's check read it open: unlocked (got ${W3(w)})`);
  check(!g.__shared["project-agent.answering"]?.[`${S}\0${PID}`], "the shared mark is gone");
};

// ---- t-046 J3: every card takes an answer in the person's own words (Other) ----
const OTHER_DETAIL = "Your own answer, in your words. The Project Agent acts on it as written; no tool's action runs on it.";
const NEEDS_WORDS = "Write your answer above to submit Other.", AS_WORDS = "Write your answer above; it is sent as your own words.";
const cardFile = (id) => `:${DIR}/decisions/${id}.json`;
// Local cards from { id: card object }: each gets status open, a question and A/B unless given.
async function otherWorld(cards, opts = {}) {
  world(opts);
  for (const [id, c] of Object.entries(cards)) g.__files[cardFile(id)] = JSON.stringify({ id, status: "open", question: `Q ${id}?`, options: [{ label: "A" }, { label: "B" }], created: "2026-11-16T10:00:00Z", ...c });
  g.__ls[`:${DIR}/decisions`] = Object.keys(cards).map((id) => `${id}.json`);
  const v = await mount(); await settle();
  return v;
}
// Types into a card's field as the person would: the value, then the field's change event.
const fieldOf = (v, id) => Object.entries(v.notes).find(([k]) => k.endsWith(`\0${id}`))?.[1] ?? null;
const typeInto = (v, id, words) => { const f = fieldOf(v, id); check(Boolean(f), `a field for ${id}`); f.set(words); f.fire("change"); };
// The text of the selectable row with this id (copy.js sel), or null.
const selText = (tree, id) => { let found = null; const walk = (x) => { if (!x || typeof x !== "object" || found !== null) return; if (x.kind === "textview" && x.key === id) found = x.text; for (const k of x.kids ?? []) walk(k); }; walk(tree); return found; };
async function answerOther(v, id, words) { await click(v, `opt-${id}-other`); typeInto(v, id, words); await click(v, `submit-${id}`); await settle(); }
const queued = (v) => v.unsent.map((x) => x.message);

cases["P46-other-row"] = async () => { // R1 AC1-AC2: every kind of card draws the Other row after its options
  const v = await otherWorld({ "d-x": { recommendation: "Other" }, "d-r": { rule: "r-p", options: [{ label: "Approve" }, { label: "Decline" }] },
    "d-wt-t-1": { options: [{ label: "Delete" }, { label: "Keep" }] }, "d-leftovers": { kind: "leftovers", options: [{ label: "Close all listed" }, { label: "Keep them" }] } });
  for (const id of ["d-x", "d-r", "d-wt-t-1", "d-leftovers"]) {
    const { clicks } = look(v);
    check(Boolean(clicks[`opt-${id}-other`]) && Boolean(clicks[`opt-${id}-0`]) && Boolean(clicks[`opt-${id}-1`]), `${id}: its two options and Other (got ${optClicks(clicks, id)})`);
    const rowText = inspect(pathTo(v.render(cx), `opt-${id}-other`)?.at(-1) ?? null).text;
    check(rowText.startsWith("Other | "), `${id}: the Other row is labelled Other (got ${rowText})`);
    check(rowText.includes(OTHER_DETAIL) && !rowText.includes("recommended"), `${id}: its detail, never recommended (got ${rowText})`);
  }
  const ids = Object.keys(look(v).clicks).filter((k) => k.startsWith("opt-d-x-"));
  check(ids.indexOf("opt-d-x-other") === ids.length - 1, `Other is the last row (got ${ids})`);
  await click(v, "opt-d-x-other");
  check(v.choice["d-x"] === "Other", "picking Other selects it");
  await click(v, "opt-d-x-other");
  check(!v.choice["d-x"], "picking it again unselects it");
  const { v: p } = await pushWorld();
  const checks = pushExecs("check").length;
  check(Boolean(look(p).clicks[`opt-${PID}-other`]), `a push card has Other (got ${optClicks(look(p).clicks, PID)})`);
  await click(p, `opt-${PID}-other`); await settle();
  check(pushExecs("check").length === checks && pushExecs("run").length === 0, "picking Other on a push card checks nothing, runs nothing");
};

cases["P46-other-needs-words"] = async () => { // R1 AC3-AC4: Submit for Other only with words
  const v = await otherWorld({ "d-x": {} });
  await click(v, "opt-d-x-other");
  let { text, clicks } = look(v);
  check(!clicks["submit-d-x"] && text.includes("Submit answer: Other") && text.includes(NEEDS_WORDS), `empty: no Submit, the hint (got ${text.slice(0, 900)})`);
  typeInto(v, "d-x", "   \n ");
  ({ text, clicks } = look(v));
  check(!clicks["submit-d-x"] && text.includes(NEEDS_WORDS), "spaces only: still no Submit");
  typeInto(v, "d-x", "do X first");
  ({ text, clicks } = look(v));
  check(Boolean(clicks["submit-d-x"]) && text.includes(AS_WORDS) && !text.includes(NEEDS_WORDS), `with words: Submit, the other hint (got ${text.slice(0, 900)})`);
  check(fieldOf(v, "d-x").handlers("change") === 1, "the field has a change handler (Submit follows the typing)");
  typeInto(v, "d-x", "x".repeat(2001));
  check(!look(v).clicks["submit-d-x"], "over 2000 characters: no Submit");
  await click(v, "opt-d-x-0");
  ({ text } = look(v));
  check(text.includes("Submit answer: A") && text.includes("Add a note above if you want; it is sent with the answer."), "a listed option: today's texts");
};

cases["P46-other-written"] = async () => { // R2 AC1, AC3: the file
  const v = await otherWorld({ "d-x": {}, "d-y": {} });
  await answerOther(v, "d-x", "  do X first  ");
  const c = JSON.parse(g.__files[cardFile("d-x")]);
  check(c.status === "answered" && c.answer === "Other" && c.other === true && c.note === "do X first", `written Other (got ${g.__files[cardFile("d-x")]})`);
  await click(v, "opt-d-y-0"); await click(v, "submit-d-y"); await settle();
  const d = JSON.parse(g.__files[cardFile("d-y")]);
  check(d.answer === "A" && !("other" in d), `a listed answer has no other key (got ${g.__files[cardFile("d-y")]})`);
  check(JSON.stringify(queued(v)) === JSON.stringify(['decision d-x answered Other, in the person\'s own words: "do X first"', "decision d-y answered"]), `the notices (got ${JSON.stringify(queued(v))})`);
};

cases["P46-other-notice"] = async () => { // R4 AC1, AC3: one line, cut at 400; the card keeps every word
  const v = await otherWorld({ "d-x": {}, "d-y": {} });
  await answerOther(v, "d-x", 'first line\nsecond "line"');
  const long = "w".repeat(600);
  await answerOther(v, "d-y", long);
  const q = queued(v);
  check(q[0] === 'decision d-x answered Other, in the person\'s own words: "first line second "line""', `one line (got ${q[0]})`);
  check(q[1] === `decision d-y answered Other, in the person's own words: "${"w".repeat(399)}… (the rest is on the card)"`, `cut (got ${q[1]?.length})`);
  check(JSON.parse(g.__files[cardFile("d-y")]).note === long, "the card keeps all 600");
  g.__shared["project-agent.tabs"] = alive("t4w"); v.resend(cx); await settle();
  const s = sends();
  check(s.length === 2 && s[0][1] === "t4w" && s[0][2] === q[0] && s.every((a) => a.length === 3), `sent as hc's message argument (got ${JSON.stringify(s)})`);
};

cases["P46-other-locked"] = async () => { // R1 AC6 (t-045): from the click the card is answered, never answerable again
  const v = await remoteCardWorld();
  const w = await mount();
  const release = holdWrite();
  await click(v, "opt-d-01-other"); typeInto(v, "d-01", "neither; ask me tomorrow"); await click(v, "submit-d-01"); await settle();
  let t = locked(v, "d-01", "Other", "write in flight");
  check(t.includes("Saving…") && t.includes("Your answer: “neither; ask me tomorrow”"), `Saving… and the words (got ${t.slice(0, 800)})`);
  const tw = locked(w, "d-01", "Other", "the other window");
  check(tw.includes("Answered in another panel window.") && tw.includes("Your answer: “neither; ask me tomorrow”"), `the other window says where, with the words (got ${tw.slice(0, 800)})`);
  g.__remoteDelay = 1e9;
  release(); await settle(); await settle();
  check(JSON.parse(g.__files[R1]).answer === "Other", "written Other");
  t = locked(v, "d-01", "Other", "written, reload slow");
  check(!t.includes("Saving…"), "no Saving… once written");
  // A write that really failed reopens the card, the words still in the field.
  const v2 = await remoteCardWorld();
  const text0 = g.__files[R1];
  const release2 = holdWrite({ reply: { code: 3, stdout: "", stderr: "" } });
  await click(v2, "opt-d-01-other"); typeInto(v2, "d-01", "my words"); await click(v2, "submit-d-01"); await settle();
  locked(v2, "d-01", "Other", "in flight");
  release2(); await settle(); await settle();
  const after = look(v2);
  check(g.__files[R1] === text0 && /Not saved: /.test(after.text) && Boolean(after.clicks["opt-d-01-other"]), `Not saved, open again (got ${after.text.slice(0, 800)})`);
  check(fieldOf(v2, "d-01")?.value() === "my words", "the words are still in the field");
};

cases["P46-other-shown"] = async () => { // R3 AC1-AC3: answered on file, from the panel and from conversation; Copy
  const v = await otherWorld({ "d-x": { status: "answered", answer: "Other", other: true, note: "do X first", answered: "2026-11-16T11:00:00Z" },
    "d-y": { status: "answered", answer: "Other", other: true, note: "use the second one", answered: "2026-11-16T11:00:00Z", answeredBy: "person, in conversation" } });
  v.older = true;
  const { text, clicks } = look(v);
  check(text.includes("answered: Other") && text.includes("Your answer: “do X first”"), `from the panel (got ${text.slice(0, 1200)})`);
  check(text.includes("You said: “use the second one”"), `from conversation (got ${text.slice(0, 1200)})`);
  check(!clicks["opt-d-x-other"] && !text.includes(OTHER_DETAIL), "no Other row on an answered card");
  check(selText(v.render(cx), "sel-note-d-x") === "Your answer: “do X first”", "selectable as the note line");
  clipReset(); await click(v, "copy-d-x");
  check(lastClip().includes("Answer: Other (the person's own words)") && lastClip().includes("Your answer: “do X first”"), `Copy (got ${JSON.stringify(lastClip())})`);
};

cases["P46-other-withdrawn"] = async () => { // R3 AC5: withdrawn after an Other answer: the earlier answer
  const v = await otherWorld({ "d-x": { status: "withdrawn", answer: null, previousAnswer: "Other", previousNote: "keep it for now", note: "asked again", answeredBy: "orchestrator: withdrawn", answered: "2026-11-16T11:00:00Z" } });
  v.older = true;
  const { text } = look(v);
  check(text.includes("withdrawn") && text.includes("Earlier answer: Other: “keep it for now”"), `the earlier answer (got ${text.slice(0, 1200)})`);
  clipReset(); await click(v, "copy-d-x");
  check(lastClip().includes("Earlier answer: Other: “keep it for now”"), `Copy (got ${JSON.stringify(lastClip())})`);
};

cases["P46-asked-again"] = async () => { // Q1 (card d-155): an open card asked again shows the earlier answer
  const v = await otherWorld({ "d-wt-t-1": { options: [{ label: "Delete" }, { label: "Keep" }], previousAnswer: "Other", previousNote: "delete it after the demo", previousAnswered: "2026-11-15T09:00:00Z" },
    "d-leftovers-2": { kind: "leftovers", options: [{ label: "Close all listed" }], previousAnswer: "Other", previousNote: "close all but t32", previousCard: "d-leftovers" },
    "d-new": {} });
  const { text, clicks } = look(v);
  check(text.includes("Asked again. Your earlier answer: Other: “delete it after the demo”"), `worktree card (got ${text.slice(0, 1500)})`);
  check(text.includes("Asked again (after d-leftovers). Your earlier answer: Other: “close all but t32”"), `leftovers card names the earlier card (got ${text.slice(0, 1500)})`);
  check((text.match(/Asked again/g) ?? []).length === 2, "a fresh card says nothing");
  check(Boolean(clicks["opt-d-wt-t-1-0"]) && Boolean(clicks["opt-d-wt-t-1-other"]), "still open and answerable");
  clipReset(); await click(v, "copy-d-wt-t-1");
  check(lastClip().includes("Asked again. Your earlier answer: Other: “delete it after the demo”"), `Copy (got ${JSON.stringify(lastClip())})`);
};

cases["P46-other-rule"] = async () => { // R6 AC1, AC3; R12 AC1: Other on a rule card changes no rule
  world();
  const rulesText = JSON.stringify({ rules: [{ ...rule("r-p", "Proposed"), status: "proposed", source: "agent" }] });
  g.__files[RULES] = rulesText;
  g.__files[cardFile("d-r")] = JSON.stringify({ id: "d-r", status: "open", rule: "r-p", question: "Adopt rule r-p?", options: [{ label: "Approve" }, { label: "Decline" }], created: "2026-11-16T10:00:00Z" });
  g.__ls[`:${DIR}/decisions`] = ["d-r.json"];
  const v = await mount(); await settle();
  g.__delay = { [`:${DIR}/agent.json`]: 5000 };
  await answerOther(v, "d-r", "Approve, but only for tests");
  const c = JSON.parse(g.__files[cardFile("d-r")]);
  check(c.answer === "Other" && c.other === true && c.note === "Approve, but only for tests", "answered Other");
  check(g.__files[RULES] === rulesText && !(g.__writes ?? []).some((k) => k.endsWith("/rules.json")), "rules.json unchanged, never written");
  const t = locked(v, "d-r", "Other", "rule card");
  check(t.includes("The rule stays proposed: Other is not Approve or Decline."), `the line (got ${t.slice(0, 900)})`);
  g.__delay = {}; await step(5000); await settle(); v.older = true;
  check(look(v).text.includes("The rule stays proposed: Other is not Approve or Decline."), "the line from the file too");
};

cases["P46-rules-view-unchanged"] = async () => { // R1 AC7: the Rules view has no Other; Approve there answers Approve
  world(live4w());
  g.__files[RULES] = JSON.stringify({ rules: [{ ...rule("r-p", "Proposed"), status: "proposed", source: "agent" }] });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  g.__files[cardFile("d-r")] = JSON.stringify({ id: "d-r", status: "open", rule: "r-p", question: "Adopt rule r-p?", options: [{ label: "Approve" }, { label: "Decline" }], created: "2026-11-16T10:00:00Z" });
  g.__ls[`:${DIR}/decisions`] = ["d-r.json"];
  const v = await mount(); await settle();
  await staleClick(v, "view-rules");
  await staleClick(v, "rule-head-r-p");
  check(!Object.keys(look(v).clicks).some((k) => /other/i.test(k)) && !look(v).text.includes(OTHER_DETAIL), "no Other in the Rules view");
  await staleClick(v, "rule-approve-r-p");
  const c = JSON.parse(g.__files[cardFile("d-r")]);
  check(c.answer === "Approve" && !("other" in c), `answered Approve, no other key (got ${g.__files[cardFile("d-r")]})`);
};

const pushOtherReply = { ok: true, done: false, answer: "Other", refusal: null, result: null, message: `${PID} answered Other; nothing was pushed.` };
const pushOtherCard = (words) => { const base = JSON.parse(pushCardText()); return JSON.stringify({ ...base, status: "answered", answer: "Other", other: true, note: words, answered: "2026-11-16T10:01:00Z", answeredBy: "person, in the panel" }); };

cases["P46-other-push"] = async () => { // R7 AC5-AC6, R4 AC2, R12 AC1/AC3: Other on a push card runs the tool with --answer Other, never --seen
  let file;
  const { v, file: f } = await pushWorld({ tool: { run: () => { g.__files[file] = pushOtherCard("do X first"); return json(0, pushOtherReply); } } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle(); // a passing check: a seen token is there
  check(v.push.canSubmit(S, PID), "a check passed");
  await click(v, `opt-${PID}-other`); typeInto(v, PID, "do X first");
  await click(v, `submit-${PID}`); await settle(); await settle();
  const runs = pushExecs("run");
  check(runs.length === 1, `one run (got ${runs.length})`);
  check(JSON.stringify(runs[0][1]) === JSON.stringify(["run", PID, "--answer", "Other", "--space", S, "--json", "--note", "do X first"]), `its argv (got ${JSON.stringify(runs[0][1])})`);
  check(!runs[0][1].includes("--seen"), "never --seen");
  await click(v, "view-decisions"); v.older = true; // answered Other, not kept (as Not now): in the answered fold
  const { text } = look(v);
  check(text.includes("answered: Other") && text.includes("Answered Other: nothing was pushed."), `shown (got ${text.slice(0, 1500)})`);
  check(!text.includes("Answered Not now"), "not called Not now");
  check(JSON.stringify(queued(v)) === JSON.stringify([`decision ${PID} answered Other (nothing was pushed), in the person's own words: "do X first"`]), `the notice (got ${JSON.stringify(queued(v))})`);
  check(!g.__execs.some(([c, a]) => c === "sh" && (a ?? []).some((x) => String(x).includes("do X first"))), "the words never reach a shell");
  // The tool says it was pushed already: shown and told as Push (t-045 R9).
  let file2;
  const { v: v2, file: f2 } = await pushWorld({ tool: { run: () => { g.__files[file2] = answeredByTool(); return json(0, { ...toolPushed, message: `${PID} was already pushed; nothing to do.` }); } } });
  file2 = f2;
  await click(v2, `opt-${PID}-other`); typeInto(v2, PID, "do X first"); await click(v2, `submit-${PID}`); await settle(); await settle();
  check(look(v2).text.includes(`Pushed ${PURL} main aa22000..cc11000`) && queued(v2)[0].startsWith(`decision ${PID} was already answered Push; `), `already pushed: shown as Push (got ${JSON.stringify(queued(v2))})`);
  // A refusal (C5): not saved, the card open.
  const { v: v3 } = await pushWorld({ tool: { run: () => json(2, { ok: false, answer: null, refusal: { check: "C5", message: "an Other answer needs --note with the person's own words" }, message: "refused" }) } });
  await click(v3, `opt-${PID}-other`); typeInto(v3, PID, "x"); await click(v3, `submit-${PID}`); await settle(); await settle();
  const t3 = look(v3);
  check(t3.text.includes("Not saved: an Other answer needs --note") && Boolean(t3.clicks[`opt-${PID}-other`]), `refused: Not saved, open (got ${t3.text.slice(0, 1500)})`);
  check(queued(v3)[0] === `decision ${PID} answered Other; refused: an Other answer needs --note with the person's own words (the card stays open)`, `its notice (got ${queued(v3)[0]})`);
  // Other while a check runs: not run, said.
  const gate = deferred();
  let checking = false;
  const { v: v4 } = await pushWorld({ tool: { check: () => (checking ? gate.promise : json(0, checkReply())) } });
  await click(v4, `opt-${PID}-other`); typeInto(v4, PID, "later");
  checking = true; await click(v4, `push-check-${PID}`); await settle();
  v4.answerPush(g.__spaces[0], v4.cards.find((c) => c.id === PID), "Other", cx); await settle();
  check(pushExecs("run").length === 0 && look(v4).text.includes("Other was not saved: a check was running. Submit it again."), `not run while checking (got ${look(v4).text.slice(0, 1500)})`);
  gate.resolve(json(0, checkReply())); await settle();
};

cases["P46-push-other-closed"] = async () => { // R3 AC4: a push card answered Other on file
  const { v } = await pushWorld({ card: pushOtherCard("not this week") });
  await click(v, "view-decisions"); v.older = true;
  const { text } = look(v);
  check(text.includes("Answered Other: nothing was pushed.") && text.includes("Your answer: “not this week”"), `shown (got ${text.slice(0, 1500)})`);
  check(!text.includes("Nothing was pushed from this card."), "not the plain line");
};

cases["P46-own-other-option"] = async () => { // R1 AC5, R12 AC2: a card's own Other option becomes the panel's Other row
  const v = await otherWorld({ "d-x": { options: [{ label: "A" }, { label: "other", detail: "something else" }, { label: "B" }] } });
  const { clicks } = look(v);
  check(Boolean(clicks["opt-d-x-0"]) && !clicks["opt-d-x-1"] && Boolean(clicks["opt-d-x-2"]) && Boolean(clicks["opt-d-x-other"]), `A, B keep their ids; one Other row (got ${optClicks(clicks, "d-x")})`);
  const rowText = inspect(pathTo(v.render(cx), "opt-d-x-other")?.at(-1) ?? null).text;
  check(rowText === "Other | something else", "the card's own detail under it");
  check(!look(v).text.includes("| other |"), "the card's own row is not drawn");
  await answerOther(v, "d-x", "neither of these");
  const c = JSON.parse(g.__files[cardFile("d-x")]);
  check(c.answer === "Other" && c.other === true && c.note === "neither of these", `written as Other (got ${g.__files[cardFile("d-x")]})`);
};

// ---- t-053: the panel far below the runtime's host-task limit (spec R1-R8) ------------------------------------
// tests/panel/stubs.mjs keeps the pool (__pool): a slot per pending spawn, sleep, timer, read, exec, hc and watch, the
// runtime's RangeError past the limit. The long cases (R7) drive every copy's timers on the clock and assert the pool
// never holds more than POOL_MAX.
const { AsyncLocalStorage } = await import("async_hooks");
const POOL_MAX = 128;
const turn = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 1)); };
const pool = () => g.__pool;
// n cards in the local space's decisions/ (answered: the old ones a long project keeps; the first `open` are open).
function localCards(n, { open = 0, dir = DIR, on = "" } = {}) {
  const names = [];
  for (let i = 1; i <= n; i++) {
    const id = `d-${String(i).padStart(3, "0")}`; names.push(`${id}.json`);
    g.__files[`${on}:${dir}/decisions/${id}.json`] = JSON.stringify({ id, status: i <= open ? "open" : "answered", answer: i <= open ? undefined : "Yes", question: `Q ${id}?`, options: [{ label: "Yes" }, { label: "No" }], created: `2026-11-${String(1 + (i % 28)).padStart(2, "0")}T10:00:00Z` });
  }
  g.__ls[`${on}:${dir}/decisions`] = names;
}
// Runs every registered timer when it is due (once per step at most), each in its own copy's props (__als); each(t)
// runs per step (t: ms since the start). Stops early once the pool holds more than POOL_MAX (the case then fails).
async function drive(ms, { stepMs = 1000, each = null, lease = true, settleTurns = 4 } = {}) {
  const next = new Map(), errors = [];
  for (let t = stepMs; t <= ms; t += stepMs) {
    if (lease) advance(stepMs); else g.__now += stepMs;
    for (const tm of [...g.__timers]) {
      if (!next.has(tm)) next.set(tm, g.__now - stepMs + tm.ms);
      if (g.__now < next.get(tm)) continue;
      next.set(tm, g.__now + tm.ms);
      const callIt = () => tm.fn(cx);
      try { if (tm.store && g.__als) g.__als.run(tm.store, callIt); else callIt(); } catch (e) { errors.push(String(e?.message ?? e)); }
    }
    if (each) await each(t);
    await turn(settleTurns);
    if ((pool()?.live ?? 0) > POOL_MAX) return { stopped: t, errors };
  }
  return { stopped: null, errors };
}
const poolSaid = () => JSON.stringify({ live: pool()?.live, peak: pool()?.peak, kinds: pool()?.kinds, refused: pool()?.refused ?? 0 });
const localIssued = (kind) => g.__issued.filter((x) => x.on === null && (!kind || x.kind === kind));
const loadsLocal = () => localIssued("read").filter((x) => x.path === `${DIR}/agent.json`).length; // a full load reads agent.json first
const liveIdle = (address, space = S) => ({ address, state: "live", argv: ["pa-session"], labels: { "herdr.pa": localId53(space) }, status: "idle", program: claude });
const localId53 = (id) => String(id).split("/").pop();
// A copy mounted in its own props (several copies in one case: the shown panel, a hidden one, the timer copy).
async function mountAs(copyProps) { return g.__als.run({ props: copyProps }, async () => { const v = new ProjectAgent(); v.init({}, cx); await flush(); return v; }); }
const layout53 = () => ({ type: "row", id: "root", children: [
  { type: "block", id: "main", content: { tab: "active" } },
  { type: "column", id: "panel", children: [{ type: "block", id: "project-agent", content: { mod: "project-agent" } }] },
  { type: "column", id: "side", hidden: true, children: [{ type: "block", id: "pa-side", content: { mod: "project-agent", props: { side: 1 } } }] },
  { type: "block", id: "pa-timer", content: { mod: "project-agent", props: { timer: true } } }] });

cases["P53-long-a-local"] = async () => { // R7 (a), AC1.1: local, 200 cards, 10 watch batches per 10 s, 30 min: never over POOL_MAX
  world({ records: alive(), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "working" }] });
  localCards(200, { open: 2 });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 30 });
  g.__pool = { limit: 1024 }; g.__watchers = [];
  const v = await mount(); await settle();
  const paths = [`${DIR}/heartbeat`, `${DIR}/evidence/t-1/build.log`, `${DIR}/keeper.lock`, `${DIR}/decisions/d-001.json`, `${DIR}/session.log`, `${DIR}/ledger.json`, `/home/u/.config/herdr-pa/spaces/sp9/heartbeat`];
  let k = 0;
  const r = await drive(30 * 60000, { each: () => { const w = g.__watchers.shift(); if (w) w.resolve([paths[k++ % paths.length]]); } });
  console.log(`  P53-long-a-local: peak ${pool().peak} in flight, refused ${pool().refused ?? 0}, ${poolSaid()}`);
  check(r.stopped === null && pool().peak <= POOL_MAX && !pool().refused, `in flight stays <= ${POOL_MAX} (stopped at ${r.stopped} ms; ${poolSaid()})`);
  check(/PA: proj/.test(look(v).text) && v.cards.length === 200, `the panel still shows its data (cards ${v.cards.length})`);
  check(loadsLocal() > 30, `loads still happen (got ${loadsLocal()})`);
};

cases["P53-long-b-three-spaces"] = async () => { // R7 (b), AC2.1: a hung remote, a slow remote (5 s), a local space; shown, hidden and timer copies in one pool; 2 h
  world({ extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [liveIdle("t4w")] });
  localCards(20, { open: 1 });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 30 });
  g.__machines.push({ name: "m1", layout: "ready" }, { name: "m2", layout: "ready" });
  const recs = { tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal: "t4w", alive: true } };
  for (const [m, id, name] of [["m1", "spH", "hung"], ["m2", "spS", "slow"]]) {
    const dir = `.config/herdr-pa/spaces/${id}`;
    g.__spaces.push({ id: `${m}/${id}`, name, machine: m, folder: `/${name}`, active: false, tabs: [{ id: `${m}/tb-${id}`, index: 1, name: `PA: ${name}`, named: true }] });
    g.__files[`${m}:${dir}/agent.json`] = JSON.stringify({ space: id, project: `/${name}`, profile: "test", name });
    g.__files[`${m}:${dir}/ledger.json`] = ledgerOf(2);
    g.__files[`${m}:${dir}/schedule.json`] = JSON.stringify({ everyMinutes: 30 });
    g.__files[`${m}:.config/herdr-pa/profiles/test/profile.json`] = JSON.stringify({ name: "test" });
    g.__files[`${m}:${dir}/heartbeat`] = "1 t\n";
    localCards(20, { open: 1, dir, on: m });
    g.__panes.push({ pane: `${m}/p-${id}`, terminal: `${m}/t-${id}`, tab: `${m}/tb-${id}`, title: `PA: ${name}`, cwd: `/${name}`, status: "idle", program: claude });
    g.__terminals.push(liveIdle(`${m}/t-${id}`, id));
    recs[`${m}/tb-${id}`] = { space: `${m}/${id}`, project: `/${name}`, profile: "test", name, terminal: `${m}/t-${id}`, alive: true };
  }
  g.__shared["project-agent.tabs"] = recs;
  delete g.__shared["project-agent.timer"]; delete g.__shared["project-agent.keeper"]; // one of these copies keeps time
  g.__layout = layout53(); g.__als = new AsyncLocalStorage();
  g.__pool = { limit: 1024 }; g.__hangOn = ["m1"]; g.__delayOn = { m2: 5000 }; g.__hcHang = ["m1/t-spH"];
  const shownCopy = await mountAs({}); await mountAs({ side: 1 }); await mountAs({ timer: true }); await settle();
  const order = ["m1/spH", "m2/spS", S];
  let slowSeen = false;
  const r = await drive(2 * 3600000, { stepMs: 5000, lease: false, settleTurns: 3, each: (t) => {
    show(order[Math.floor(t / 1200000) % 3]);
    if (shownCopy.space === "m2/spS" && shownCopy.data?.agent?.name === "slow") slowSeen = true;
  } });
  console.log(`  P53-long-b-three-spaces: peak ${pool().peak} in flight, refused ${pool().refused ?? 0}, ${poolSaid()}`);
  check(r.stopped === null && pool().peak <= POOL_MAX && !pool().refused, `in flight stays <= ${POOL_MAX} in one pool (stopped at ${r.stopped} ms; ${poolSaid()}; ${r.errors.slice(0, 2)})`);
  check(slowSeen, "the slow remote space still loads while the other one hangs");
};

cases["P53-long-c-costs"] = async () => { // R7 (c): the Costs view shown, a collect every 120 s and its writes seen by the watch; 30 min
  world({ records: alive(), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [liveIdle("t4w")] });
  localCards(200, { open: 1 });
  costsFiles(costsSummary()); costsTool({});
  delete g.__shared["project-agent.timer"]; delete g.__shared["project-agent.keeper"]; // this copy keeps time and collects
  g.__pool = { limit: 1024 }; g.__watchers = [];
  const v = await mount(); await settle(); await click(v, "view-costs"); await settle();
  let collects = 0;
  const r = await drive(30 * 60000, { lease: false, each: () => {
    const n = g.__issued.filter((x) => x.kind === "pa-costs").length;
    if (n > collects) { collects = n; const w = g.__watchers.shift(); if (w) w.resolve([`${DIR}/costs/sessions.jsonl`, `${DIR}/costs/summary.json`]); }
  } });
  console.log(`  P53-long-c-costs: peak ${pool().peak} in flight, refused ${pool().refused ?? 0}, ${poolSaid()}`);
  check(r.stopped === null && pool().peak <= POOL_MAX && !pool().refused, `in flight stays <= ${POOL_MAX} (stopped at ${r.stopped} ms; ${poolSaid()})`);
  check(collects >= 10, `collects every 120 s while shown (got ${collects} pa-costs calls)`);
};

cases["P53-local-200-no-sleeps"] = async () => { // AC1.1: a local load of 200 cards leaves at most the clock's sleep, none a second later
  world(); localCards(200, { open: 3 }); g.__pool = { limit: 1024 };
  const v = await mount(); await settle();
  check(v.cards.length === 200 && !v.loads[S], `the load committed (cards ${v.cards.length})`);
  check((pool().kinds.sleep ?? 0) <= 1, `at most one pending sleep at commit (${poolSaid()})`);
  advance(1000); await settle();
  check((pool().kinds.sleep ?? 0) === 0, `none a second later (${poolSaid()})`);
};

cases["P53-local-batch-one-exec"] = async () => { // AC3.1: the local cards are one exec, no readTextFile of a card
  world(); localCards(200, { open: 3 });
  const v = await mount(); await settle();
  const cardReads = localIssued("read").filter((x) => x.path.includes("/decisions/"));
  check(v.cards.length === 200 && localIssued("batch").length === 1 && cardReads.length === 0, `1 batch exec, 0 card reads (batch ${localIssued("batch").length}, reads ${cardReads.length})`);
};

cases["P53-fallback-peak-8"] = async () => { // AC3.2, AC1.2: the batch fails: at most 8 card reads pending; a hung fallback gives up at 10 s
  world({ machine: M }); localCards(40, { open: 40, dir: RDIR, on: M }); g.__batchFail = { [M]: { code: 1 } }; g.__pool = { limit: 1024 };
  for (let i = 1; i <= 40; i++) g.__delay[`${M}:${RDIR}/decisions/d-${String(i).padStart(3, "0")}.json`] = 500;
  g.__cardReads = { live: 0, peak: 0 };
  const v = await mount();
  for (let s = 0; s < 8; s++) await step(500);
  check(g.__cardReads.peak <= 8 && g.__cardReads.peak > 0, `at most 8 card reads at a time (peak ${g.__cardReads.peak})`);
  check(v.cards.length === 40, `every card read (got ${v.cards.length})`);
  world({ machine: M }); localCards(40, { open: 40, dir: RDIR, on: M }); g.__batchFail = { [M]: { code: 1 } }; g.__pool = { limit: 1024 };
  g.__hang = Array.from({ length: 40 }, (_, i) => `${M}:${RDIR}/decisions/d-${String(i + 1).padStart(3, "0")}.json`);
  g.__cardReads = { live: 0, peak: 0 };
  const w = await mount();
  await step(9000);
  check(Boolean(w.loads[RS]), "still reading at 9 s");
  await step(2000);
  check(!w.loads[RS] && g.__cardReads.peak <= 8 && (pool().kinds.sleep ?? 0) <= 1, `given up by 11 s, <= 8 card reads, <= 1 sleep (peak ${g.__cardReads.peak}; ${poolSaid()})`);
};

// ---- t-061: every open card is shown, whatever the count and the names -----------------------------------------
// 200 answered cards named a-* and 30 open ones named d-wt-* (sorting after every a-*), in `dir` on `on`.
function cards61({ dir = DIR, on = "" } = {}) {
  const names = [];
  for (let i = 0; i < 200; i++) {
    const id = `a-${String(i).padStart(3, "0")}`; names.push(`${id}.json`);
    g.__files[`${on}:${dir}/decisions/${id}.json`] = JSON.stringify({ id, status: "answered", answer: "Yes", question: `Q ${id}?`, options: [{ label: "Yes" }], created: `2026-10-${String(1 + Math.floor(i / 10)).padStart(2, "0")}T10:${String(i % 10).padStart(2, "0")}:00Z` });
  }
  for (let i = 0; i < 30; i++) {
    const id = `d-wt-t-${String(i).padStart(3, "0")}`; names.push(`${id}.json`);
    g.__files[`${on}:${dir}/decisions/${id}.json`] = JSON.stringify({ id, status: "open", question: `Keep ${id}?`, options: [{ label: "Keep" }, { label: "Remove" }], created: `2026-11-17T10:${String(i).padStart(2, "0")}:00Z` });
  }
  g.__ls[`${on}:${dir}/decisions`] = names;
}
const check61 = (v) => {
  const text = look(v).text;
  check(/Decisions · 30/.test(text), `the Decisions tab counts all 30 open cards (got ${text.match(/Decisions[^\n]{0,8}/)?.[0]})`);
  const shown = v.cards.filter((c) => c.card?.status === "open").map((c) => c.id);
  check(shown.length === 30 && shown.includes("d-wt-t-029"), `every open card listed, the last name too (got ${shown.length})`);
  check(/answered · 10 shown · 160 archived · 30 older not loaded/.test(text), `the answered line says shown, archived and not loaded (t-157; got ${text.match(/answered[^|]*/)?.[0]})`);
};

cases["P61-open-last-batch"] = async () => { // R1, R2, R3: local batch, 230 cards, the 30 open ones sorting last: all shown, the count said
  world(); cards61();
  const v = await mount(); await settle();
  check61(v);
};

cases["P61-open-last-fallback"] = async () => { // R1, R2, R3: another machine, the batch fails: the per-card fallback shows them all too
  world({ machine: M }); cards61({ dir: RDIR, on: M }); g.__batchFail = { [M]: { code: 1 } };
  const v = await mount();
  for (let s = 0; s < 6; s++) await step(500);
  check61(v);
};

// ---- t-054: the archive, on request ----------------------------------------------------------------------------
cases["P54-archive-on-request"] = async () => { // R5: reloads never read decisions-archive/; the Archive line reads it once per click
  world(); localCards(3, { open: 1 });
  g.__files[`:${DIR}/decisions-archive/d-000.json`] = JSON.stringify({ id: "d-000", status: "answered", answer: "Yes", question: "An archived question?", options: [{ label: "Yes" }], created: "2026-01-01T10:00:00Z" });
  g.__ls[`:${DIR}/decisions-archive`] = ["d-000.json"];
  const v = await mount(); await settle();
  const archiveCalls = () => [...g.__issued, ...(g.__execs ?? [])].filter((x) => JSON.stringify(x).includes("decisions-archive")).length; // __issued has no exec args
  advance(60000); await settle();
  check(archiveCalls() === 0, `no load reads the archive (got ${archiveCalls()})`);
  check(!/An archived question/.test(look(v).text), "not shown before it is asked for");
  await click(v, "pa-older");
  check(/Archive/.test(look(v).text) && archiveCalls() === 0, "the Archive line, still unread");
  await click(v, "pa-archive"); await settle();
  check(archiveCalls() === 1, `one read on the click (got ${archiveCalls()})`);
  check(/An archived question\?/.test(look(v).text), "the archived card is shown");
  await click(v, "pa-archive"); await settle();
  check(!/An archived question/.test(look(v).text) && archiveCalls() === 1, "folded: hidden, nothing read");
};

cases["P53-hidden-batchonly-local"] = async () => { // AC3.3: hidden, a local batch that fails: no card read
  world(); localCards(5, { open: 2 }); g.__batchFail = { "": { code: 1 } }; g.__layout = panelLayout("changes");
  await mount(); await settle();
  await drive(30000);
  const cardReads = localIssued("read").filter((x) => x.path.includes("/decisions/"));
  check(cardReads.length === 0 && localIssued("ls").length === 0, `no fallback while hidden (reads ${cardReads.length}, ls ${localIssued("ls").length})`);
};

cases["P53-watch-filter"] = async () => { // AC4.1: watch batches the panel does not show reload nothing; shown ones at most one load per 2 s
  world(); localCards(3, { open: 1 }); g.__watchers = [];
  await mount(); await settle();
  const fireOnly1s = () => { for (const tm of g.__timers.filter((x) => x.ms === 1000)) tm.fn(cx); };
  const batches = async (paths) => { for (let s = 0; s < 10; s++) { advance(1000); for (let i = 0; i < 10; i++) { const w = g.__watchers.shift(); if (w) w.resolve(paths); await turn(2); } fireOnly1s(); await settle(); } };
  const before = loadsLocal();
  await batches([`${DIR}/heartbeat`, `${DIR}/evidence/t-1/build.log`, `${DIR}/keeper.lock`, `${DIR}/session.log`, `${DIR}/costs/sessions.jsonl`, `/home/u/.config/herdr-pa/spaces/sp9/agent.json`]);
  check(loadsLocal() === before, `heartbeat and log batches start no load (got ${loadsLocal() - before})`);
  const mid = loadsLocal();
  await batches([`${DIR}/decisions/d-1.json`]);
  advance(3000); fireOnly1s(); await settle();
  check(loadsLocal() - mid >= 1 && loadsLocal() - mid <= 6, `100 card batches in 10 s: 1 to 6 loads (got ${loadsLocal() - mid})`);
  const end = loadsLocal();
  g.__watchers.shift()?.resolve([`/home/u/.config/herdr-pa/spaces`]); await turn(2); advance(3000); fireOnly1s(); await settle();
  check(loadsLocal() > end, "a batch naming the watched root reloads");
};

cases["P53-hung-recovers"] = async () => { // AC2.1, AC2.2: a hung machine: no new call of a pending kind; when it answers, the next round reads normally
  world({ machine: M }); g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2); remoteCards(3);
  const v = await mount(); await settle();
  g.__hangOn = [M];
  await drive(60000);
  const at = g.__issued.length;
  await drive(120000);
  const more = g.__issued.slice(at).filter((x) => x.on === M);
  check(more.length <= 2, `no new call while the old ones hang (got ${more.length}: ${JSON.stringify(more.slice(0, 6).map((x) => x.kind + (x.path ? ` ${x.path.split("/").pop()}` : "")))})`);
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(3); g.__hangOn = [];
  await drive(25000);
  check(/Tasks · 3/.test(look(v).text), `read normally once it answers (got ${look(v).text.slice(0, 200)})`);
};

cases["P53-busy-quiet"] = async () => { // D4 (card t-053 approval): a call still pending answers BUSY: skipped quietly; a click waits for it and reads again
  world({ machine: M }); g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2); remoteCards(1);
  const v = await mount(); await settle();
  const L = `${M}:${RDIR}/ledger.json`;
  g.__delay[L] = 15000; advance(3000);
  await fire(10000); // a background load: its ledger read takes 15 s
  await step(10000); await step(1000); // timed out at 10 s: the last known tasks
  check(/ledger\.json: timed out/.test(look(v).text), `a real time-out is said (got ${look(v).text.slice(0, 300)})`);
  const reads0 = reads("ledger.json").length;
  await fire(10000); await step(500); // another background load: the ledger read still pending
  check(reads("ledger.json").length === reads0, "no second ledger read while the first is pending");
  const { text } = look(v);
  check(/Tasks · 2/.test(text) && !/ledger\.json: timed out/.test(text) && !/still waiting|busy/i.test(text), `BUSY shows nothing (got ${text.slice(0, 300)})`);
  g.__files[L] = ledgerOf(4);
  v.reload(cx); await settle(); // a click's reload: waits for the read in flight
  delete g.__delay[L];
  await step(2000); await step(2000); await step(1000);
  check(/Tasks · 4/.test(look(v).text) && !/timed out/.test(look(v).text), `the click read again after the old read settled (got ${look(v).text.slice(0, 300)})`);
};

cases["P53-tick-single-flight"] = async () => { // AC5.1: a hung machine for 1 h: tick and readBeats hold no more each round; the healthy space's passes go out
  world({ extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [liveIdle("t4w")] });
  g.__machines.push({ name: "m1", layout: "ready" });
  const hdir = ".config/herdr-pa/spaces/spH";
  g.__spaces.push({ id: "m1/spH", name: "hung", machine: "m1", folder: "/hung", active: false, tabs: [{ id: "m1/tb-spH", index: 1, name: "PA: hung", named: true }] });
  g.__files[`m1:${hdir}/agent.json`] = JSON.stringify({ space: "spH", project: "/hung", profile: "test", name: "hung" });
  g.__panes.push({ pane: "m1/p-spH", terminal: "m1/t-spH", tab: "m1/tb-spH", title: "PA: hung", cwd: "/hung", status: "idle", program: claude });
  g.__terminals.push(liveIdle("m1/t-spH", "spH"));
  g.__shared["project-agent.tabs"] = { "m1/tb-spH": { space: "m1/spH", project: "/hung", profile: "test", name: "hung", terminal: "m1/t-spH", alive: true },
    tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal: "t4w", alive: true } };
  delete g.__shared["project-agent.timer"]; delete g.__shared["project-agent.keeper"];
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 0 });
  g.__props = { timer: true }; g.__pool = { limit: 1024 }; g.__hangOn = ["m1"];
  await mount(); await settle();
  const passes = () => sends().filter((a) => a[1] === "t4w" && /manual pass/.test(a[2])).length;
  let asked = 0, live30 = null;
  const r = await drive(3600000, { stepMs: 5000, lease: false, each: (t) => {
    g.__files[`:${DIR}/heartbeat`] = `${Math.floor(g.__now / 1000)} t4w\n`; // the local launcher runs
    if (t % 600000 === 0) { g.__files[`:${DIR}/pass-request.json`] = JSON.stringify({ pending: true, at: "x", by: "panel" }); asked += 1; }
    if (t === 1800000) live30 = pool().live;
  } });
  check(r.stopped === null && pool().live <= (live30 ?? 0) + 2, `the pool does not grow while m1 hangs (30 min ${live30}, 60 min ${pool().live}; ${poolSaid()})`);
  check(passes() >= asked - 1 && asked >= 5, `the local space's passes still go out (${passes()} of ${asked})`);
};

cases["P53-hc-hung-one-slot"] = async () => { // AC5.2: a hung hc send: one call; the notice stays queued; no second send to that address
  const v = await cardsWorld({ records: alive("t4w"), extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [paTerm()], agents: [{ address: "t4w", status: "idle" }] }, ["d-x"]);
  g.__hcHang = ["t4w"];
  await answerCard(v, "d-x"); await settle();
  await drive(600000, { stepMs: 2000 });
  check(sends().length === 1, `one send in 10 min (got ${sends().length})`);
  check(v.unsent.length === 1 && /hasn't been told yet/.test(look(v).text), `the notice stays queued and says so (got ${look(v).text.slice(0, 300)})`);
};

cases["P53-pool-full"] = async () => { // AC6.1: the runtime refuses: one plain line, the last data kept, no call for a second; gone once it drains
  world(); localCards(3, { open: 1 }); g.__pool = { limit: 1024 };
  const v = await mount(); await settle();
  check(/PA: proj/.test(look(v).text), "loaded");
  pool().full = true;
  advance(10000);
  for (const t of g.__timers.filter((x) => x.ms === 10000)) { try { t.fn(cx); } catch (e) { (g.__thrown ??= []).push(String(e)); } }
  await settle();
  let { text } = look(v);
  check(/Too many calls waiting \(\d+\): .+ was skipped; showing what was read at \d\d:\d\d/.test(text) && !/host task limit/.test(text) && /PA: proj/.test(text) && !(g.__thrown ?? []).length,
    `the plain line with the last data (got ${text.slice(0, 400)}; thrown ${g.__thrown})`);
  const issued = g.__issued.length, refused = pool().refused;
  advance(500); for (const t of g.__timers.filter((x) => x.ms === 1000)) { try { t.fn(cx); } catch {} } v.reload(cx, { background: true }); await settle();
  check(g.__issued.length === issued && pool().refused === refused, `no call in the next second (issued ${g.__issued.length - issued}, refused ${pool().refused - refused})`);
  pool().full = false;
  advance(10000); await fire(10000); await settle();
  ({ text } = look(v));
  check(!/Too many calls waiting/.test(text) && /PA: proj/.test(text), `the line goes at the next load (got ${text.slice(0, 300)})`);
};

cases["P53-hidden-older-ends-last"] = async () => { // AC8.2 (review P-M3; t-053 round 2, the verifier's case): A's ledger read slow, its cards answered; B reads cards (fresh), ledger BUSY; A ends last: it must write nothing
  world({ machine: M });
  remoteCards(3);
  g.__layout = panelLayout("changes");
  const v = await mount(); await settle();
  check((g.__shared["project-agent.open"] ?? {})[RS] === 3, "3 open");
  g.__sleepHang = true; g.__delay[`${M}:${RDIR}/ledger.json`] = 30000;
  await step(10000); await fire(10000); // read A at 10 s: cards at once (3 open), ledger answers at 40 s
  for (const c of ["d-01", "d-02"]) g.__files[`${M}:${RDIR}/decisions/${c}.json`] = openCard(c, { status: "answered", answer: "Yes" });
  await step(12000); await fire(10000); // 22 s: read B: its card batch answers (1 open), its ledger read is BUSY
  check((g.__shared["project-agent.open"] ?? {})[RS] === 1, `read B wrote 1 open (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  await step(20000); // A ends at 40 s, older than B
  check((g.__shared["project-agent.open"] ?? {})[RS] === 1, `A, ending last, wrote nothing (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  void v;
};

cases["P53-tick-write-hung"] = async () => { // round 2 (review MF-1): m1's reads and send answer, its stamp writes hang: the pass sends once, the local passes and the lease go on
  world({ extraTabs: [paTab], extraPanes: [paPane()], extraTerms: [liveIdle("t4w")] });
  g.__machines.push({ name: "m1", layout: "ready" });
  const hdir = ".config/herdr-pa/spaces/spH";
  g.__spaces.push({ id: "m1/spH", name: "hung", machine: "m1", folder: "/hung", active: false, tabs: [{ id: "m1/tb-spH", index: 1, name: "PA: hung", named: true }] });
  g.__files[`m1:${hdir}/agent.json`] = JSON.stringify({ space: "spH", project: "/hung", profile: "test", name: "hung" });
  g.__files[`m1:${hdir}/schedule.json`] = JSON.stringify({ everyMinutes: 0 });
  g.__files[`m1:${hdir}/pass-request.json`] = JSON.stringify({ pending: true, at: "x", by: "panel" });
  g.__panes.push({ pane: "m1/p-spH", terminal: "m1/t-spH", tab: "m1/tb-spH", title: "PA: hung", cwd: "/hung", status: "idle", program: claude });
  g.__terminals.push(liveIdle("m1/t-spH", "spH"));
  g.__shared["project-agent.tabs"] = { "m1/tb-spH": { space: "m1/spH", project: "/hung", profile: "test", name: "hung", terminal: "m1/t-spH", alive: true },
    tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal: "t4w", alive: true } };
  delete g.__shared["project-agent.timer"]; delete g.__shared["project-agent.keeper"];
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 0 });
  g.__props = { timer: true }; g.__pool = { limit: 1024 }; g.__execHang = ["m1:python3"]; // every write on m1 hangs
  await mount(); await settle();
  const passes = (to) => sends().filter((a) => a[1] === to && /manual pass/.test(a[2])).length;
  let asked = 0, live10 = null;
  const r = await drive(1200000, { stepMs: 5000, lease: false, each: (t) => {
    g.__files[`:${DIR}/heartbeat`] = `${Math.floor(g.__now / 1000)} t4w\n`; g.__files[`m1:${hdir}/heartbeat`] = `${Math.floor(g.__now / 1000)} t-spH\n`;
    if (t % 300000 === 0) { g.__files[`:${DIR}/pass-request.json`] = JSON.stringify({ pending: true, at: "x", by: "panel" }); asked += 1; }
    if (t === 600000) live10 = pool().live;
  } });
  check(r.stopped === null && pool().live <= (live10 ?? 0) + 2, `the pool does not grow while m1's writes hang (10 min ${live10}, 20 min ${pool().live}; ${poolSaid()})`);
  check(passes("m1/t-spH") === 1, `m1's pass is sent once, never again while its stamp write is pending (got ${passes("m1/t-spH")})`);
  check(passes("t4w") >= asked - 1 && asked >= 3, `the local space's passes still go out (${passes("t4w")} of ${asked})`);
  const lease = g.__shared["project-agent.timer"];
  check(lease && g.__now - lease.at < 50000, `the lease is still renewed (${lease ? g.__now - lease.at : "none"} ms old)`);
};

cases["P53-click-after-refusal"] = async () => { // round 2 (review MF-2), R6: in the quiet second after a refusal a click's reload still loads
  world(); g.__files[`:${DIR}/ledger.json`] = ledgerOf(2); g.__pool = { limit: 1024 };
  const v = await mount(); await settle();
  check(/Tasks · 2/.test(look(v).text), "loaded");
  pool().full = true;
  advance(10000); await fire(10000); await settle();
  check(/Too many calls waiting/.test(look(v).text), `refused (got ${look(v).text.slice(0, 300)})`);
  pool().full = false; g.__files[`:${DIR}/ledger.json`] = ledgerOf(4);
  advance(300);
  const before = loadsLocal();
  v.reload(cx); await settle(); await settle();
  check(loadsLocal() === before + 1, `the click's load started (loads ${loadsLocal() - before})`);
  check(/Tasks · 4/.test(look(v).text), `the click's load shows the new tasks (got ${look(v).text.slice(0, 300)})`);
};

cases["P53-grep-host-calls"] = async () => { // AC1.3, AC6.2: no cx.sleep outside host.js; every host call goes through host.js
  const files = fs.readdirSync(src).filter((n) => n.endsWith(".js") && n !== "host.js");
  const code = (f) => fs.readFileSync(path.join(src, f), "utf8").split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
  const sleeps = files.filter((f) => /cx\.sleep\(/.test(code(f)));
  check(!sleeps.length, `no cx.sleep( outside host.js (got ${sleeps})`);
  const calls = files.flatMap((f) => [...code(f).matchAll(/(?<![.\w])(readTextFile|writeTextFile|exec|hc|watchFolder)\(/g)].map((m) => `${f}: ${m[1]}`));
  check(!calls.length, `no host call outside host.js (got ${calls.slice(0, 8)})`);
  const imports = files.filter((f) => /import\s*\{[^}]*\b(readTextFile|writeTextFile|exec|hc|watchFolder)\b[^}]*\}\s*from\s*"herdr"/.test(code(f)));
  check(!imports.length && fs.existsSync(path.join(src, "host.js")), `only host.js imports them (got ${imports})`);
};

// t-060 keep awake: the cases for mods/project-agent/awake.js. No case runs the real caffeinate or holds a real
// assertion: exec is stubbed (__execFn), and A7 runs awake.js's own script with a fake caffeinate (a sh script in a
// temp folder that only waits for its -w pid). Defined here, not in a module of their own, so --list, --shard and
// tests/test_panel_shards.py see them as they see every other case (t-060 round 2).
const { execFileSync } = await import("child_process");
const AW = await import(path.join(dst, "awake.js"));
// A Map-backed stand-in for the runtime's bare `localStorage` (a file that survives a restart).
function awFakeStorage(init = {}) {
  const m = new Map(Object.entries(init));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, removeItem: (k) => { m.delete(k); }, clear: () => m.clear(), get length() { return m.size; }, key: (i) => [...m.keys()][i] ?? null, flush: async () => {} };
}

const awCalls = () => (g.__execs ?? []).filter(([c, a]) => c === "sh" && a?.[1] === AW.SCRIPT).map(([, a]) => a.slice(3));
// A fresh world, the given storage and platform, exec answering the keep-awake script as a working Mac would.
const awSetup = ({ storage = awFakeStorage(), platform = "macos", reply = null } = {}) => {
  world();
  g.localStorage = storage; g.__platform = platform;
  g.__execFn = (command, args) => {
    if (command !== "sh" || args?.[1] !== AW.SCRIPT) return undefined;
    if (reply) return reply(args[3]);
    return { code: 0, stdout: args[3] === "start" ? "held 5001 4001\n" : "off\n", stderr: "" };
  };
};
const awTip = (v) => P145.tip(v, "awake-toggle"); // t-145: the state and limits are the coffee button's tooltip
const awHold = (v) => { g.__shared["project-agent.timer"] = { id: v.me, at: g.__now }; }; // v holds the timer lease
const awDone = () => { g.__platform = undefined; };
const awWrap = (fn) => async () => { try { await fn(); } finally { awDone(); } };

cases["A1-one-hold"] = awWrap(async () => { // VP1, VP2: the holder starts one caffeinate -d -i -u -w <Herdr>; ticks add none
  awSetup();
  const v = await mount(); awHold(v);
  await fire(1000);
  let c = awCalls();
  check(c.length === 1 && c[0][0] === "start" && c[0][1] === "Herdr" && c[0][2] === "/usr/bin/caffeinate", `one start for Herdr (got ${JSON.stringify(c)})`);
  check(/-d -i -u -w "\$pid"/.test(AW.SCRIPT), "the script holds -d -i -u -w <Herdr's pid>");
  await fire(1000); await fire(1000);
  check(awCalls().length === 1, `no second start while it holds (got ${awCalls().length})`);
  check(/Keep this Mac awake while Herdr runs: on/.test(awTip(v)) && /holding/.test(awTip(v)), `bar says on, holding (got ${awTip(v)})`);
  advance(31000); await fire(1000);
  c = awCalls();
  check(c.length === 2 && c[1][0] === "start", `after 30 s it checks again with an idempotent start (got ${JSON.stringify(c)})`);
});

cases["A1-two-copies"] = awWrap(async () => { // VP2: two panel copies, one hold
  awSetup();
  const v = await mount(), w = await mount(); awHold(v);
  await fire(1000); await fire(1000);
  check(awCalls().length === 1, `one start for two copies (got ${JSON.stringify(awCalls())})`);
  check(/holding/.test(awTip(w)), `the other copy shows the hold (got ${awTip(w)})`);
});

cases["A2-toggle"] = awWrap(async () => { // VP2, VP3: off ends it at once, on starts it again
  awSetup();
  const v = await mount(); awHold(v);
  await fire(1000);
  await click(v, "awake-toggle"); await settle();
  let c = awCalls();
  check(c.length === 2 && c[1][0] === "stop", `Turn off stops at once (got ${JSON.stringify(c)})`);
  check(g.localStorage.getItem("project-agent.awake") === "off", `kept off (got ${g.localStorage.getItem("project-agent.awake")})`);
  check(/Keep this Mac awake while Herdr runs: off/.test(awTip(v)) && Boolean(look(v).clicks["awake-toggle"]) && /turn it on/.test(awTip(v)), `bar says off with Turn on (got ${awTip(v)})`);
  await fire(1000);
  check(awCalls().length === 2, "no call while off");
  await click(v, "awake-toggle"); await settle();
  c = awCalls();
  check(c.length === 3 && c[2][0] === "start", `Turn on starts it again (got ${JSON.stringify(c)})`);
  check(g.localStorage.getItem("project-agent.awake") === "on", "kept on");
});

cases["A2-toggle-other-copy"] = awWrap(async () => { // VP2: a click in a copy that does not hold the lease: the holder acts
  awSetup();
  const v = await mount(), w = await mount(); awHold(v);
  await fire(1000);
  await click(w, "awake-toggle");
  check(awCalls().length === 1, "the clicked copy runs nothing itself");
  check(/runs: off/.test(awTip(w)), `the clicked copy shows off at once (got ${awTip(w)})`);
  await fire(1000);
  const c = awCalls();
  check(c.length === 2 && c[1][0] === "stop", `the holder stops it within its 1 s tick (got ${JSON.stringify(c)})`);
});

cases["A3-default-and-restart"] = awWrap(async () => { // VP3: on by default; the choice survives a restart
  const storage = awFakeStorage();
  awSetup({ storage });
  let v = await mount(); awHold(v);
  check(/runs: on/.test(awTip(v)), `on by default (got ${awTip(v)})`);
  await click(v, "awake-toggle"); await settle();
  awSetup({ storage }); // Herdr restarted: new shared state, the same storage file
  v = await mount(); awHold(v);
  check(/runs: off/.test(awTip(v)), `still off after a restart (got ${awTip(v)})`);
  await fire(1000);
  check(awCalls().every((x) => x[0] === "stop"), `nothing started while off (got ${JSON.stringify(awCalls())})`);
});

cases["A4-says-limits"] = awWrap(async () => { // VP1, VP4: the button's tooltip says the manual lock, the lid, and that remote machines are not affected (t-145)
  awSetup();
  const v = await mount();
  const text = awTip(v);
  check(/still lock the screen by hand/.test(text), `manual lock (got ${text.slice(0, 600)})`);
  check(/Closing the lid still sleeps the Mac/.test(text), "the lid");
  check(/Remote machines you work on are not affected/.test(text), "remote machines");
});

cases["A5-not-macos"] = awWrap(async () => { // VP5: does nothing (t-145: and draws no button)
  awSetup({ platform: "linux" });
  const v = await mount(); awHold(v);
  await fire(1000); advance(31000); await fire(1000);
  check(awCalls().length === 0, `no call off macOS (got ${JSON.stringify(awCalls())})`);
  check(!look(v).clicks["awake-toggle"] && !/Keep this Mac/.test(look(v).text), `no button, no text (t-145) (got ${look(v).text.slice(0, 400)})`);
});

cases["A6-not-holder"] = awWrap(async () => { // VP2: a copy without the timer lease never holds
  awSetup();
  const v = await mount();
  await fire(1000); advance(31000); await fire(1000);
  check(awCalls().length === 0, `no call without the lease (got ${JSON.stringify(awCalls())})`);
  check(Boolean(v), "mounted");
});

cases["A8-failure"] = awWrap(async () => { // a hold that could not start says so, and is tried again after 30 s, not every second
  awSetup({ reply: () => ({ code: 3, stdout: "no Herdr process above this one\n", stderr: "" }) });
  const v = await mount(); awHold(v);
  await fire(1000);
  check(/could not keep it awake/.test(awTip(v)), `says it could not (got ${awTip(v)})`);
  await fire(1000); advance(5000); await fire(1000);
  check(awCalls().length === 1, `no retry within 30 s (got ${awCalls().length})`);
  advance(31000); await fire(1000);
  check(awCalls().length === 2, `retried after 30 s (got ${awCalls().length})`);
});

cases["A7-script-fake-caffeinate"] = async () => { // VP1, VP2: the real script, a fake caffeinate: one hold, idempotent, stop ends it
  if (process.platform !== "darwin") { // t-089 R23: caffeinate and awake.js serve Herdr on macOS only (node on Linux shows comm= as node-MainThread)
    console.log(`SKIP A7-script-fake-caffeinate: caffeinate and awake.js serve Herdr on macOS only (here ${process.platform})`);
    return;
  }
  const T = fs.mkdtempSync(path.join(os.tmpdir(), "t060-"));
  const fake = path.join(T, "caffeinate");
  fs.writeFileSync(fake, '#!/bin/sh\n# a fake caffeinate: holds nothing, waits for its -w pid\nwhile kill -0 "$5" 2>/dev/null; do sleep 0.2; done\n');
  fs.chmodSync(fake, 0o755);
  const owner = path.basename(process.execPath); // the script's ancestor here is this node process, standing for Herdr
  const pat = `caffeinate -d -i -u -w ${process.pid}$`;
  const run = (act) => { try { return { code: 0, out: execFileSync("sh", ["-c", AW.SCRIPT, "sh", act, owner, fake], { encoding: "utf8", timeout: 15000 }) }; } catch (e) { return { code: e.status, out: String(e.stdout ?? "") }; } };
  const running = () => { try { return execFileSync("pgrep", ["-f", pat], { encoding: "utf8" }).trim().split(/\s+/).filter(Boolean); } catch { return []; } };
  try {
    check(running().length === 0, "no fake hold before");
    const a = run("start");
    check(a.code === 0 && new RegExp(`^held \\d+ ${process.pid}$`).test(a.out.trim()), `start holds for this pid (got ${JSON.stringify(a)})`);
    check(running().length === 1, `one fake running (got ${running()})`);
    const b = run("start");
    check(b.code === 0 && b.out.trim() === a.out.trim() && running().length === 1, `start again keeps the one (got ${JSON.stringify(b)}, ${running()})`);
    const c = run("stop");
    check(c.code === 0 && c.out.trim() === "off", `stop (got ${JSON.stringify(c)})`);
    for (let i = 0; i < 50 && running().length; i++) await new Promise((r) => setTimeout(r, 100));
    check(running().length === 0, `nothing left running (got ${running()})`);
    const d = (() => { try { execFileSync("sh", ["-c", AW.SCRIPT, "sh", "start", "no-such-owner-t060", fake], { encoding: "utf8", timeout: 15000 }); return 0; } catch (e) { return e.status; } })();
    check(d === 3 && running().length === 0, `no owner found: exit 3, nothing started (got ${d})`);
  } finally {
    try { execFileSync("pkill", ["-f", pat]); } catch { /* none left */ }
    fs.rmSync(T, { recursive: true, force: true });
  }
};

// ---- t-037 J4: the Restart button (R13), older code in the panel (R14), the header during a restart (R15) ----
// The button runs only the restart tool in place, on the space's machine: a dry run for the confirm, then one
// `--json --wait 180 --expect-instance <instance> --by panel` run. It never ends a terminal itself.
const TOOL = ["pa", "restart"].join("-"); // spelled so a guard reading commands is not confused
const T64 = "T".repeat(64), C64 = "C".repeat(64);
const LREC = (over = {}) => ({ version: 2, accepts: ["restart"], instance: "i-1", terminal: "t4w", space: S, pid: 77,
  code: { tools: T64, commit: "c".repeat(40) }, profile: { name: "test", readAt: "2026-11-18T10:05:00Z" },
  session: { id: "s1", resume: true, prompt: "rebuilt", promptCore: C64, core: C64 }, ...over });
const INSTALLED = { version: 1, tools: T64, core: C64 };
const DRY_OK = { code: 0, stdout: "Project Agent proj (space sp1) in terminal t4w, pid 77.\nRuns now: tools TTTT (commit cccc), core text: current (CCCC).\nInstalled: tools TTTT, core CCCC.\nWorkers (1), each keeps running; its reports still reach t4w:\n  w1 t-1-build: keeps running\nIt restarts when this turn ends (at once when it is idle).\nDry run: nothing written.\n", stderr: "" };
const OK_REPLY = (launcher = LREC({ instance: "i-2", restartedFrom: "i-1", code: { tools: "abcdef1234", commit: "1234567890" }, session: { prompt: "rebuilt", promptCore: "fedcba9876" } })) =>
  ({ code: 0, stdout: `Restarted proj in place: terminal t4w, pid 77 (unchanged).\n${JSON.stringify({ request: "r1", outcome: "ok", launcher })}\n`, stderr: "" });
// A live local (or remote) Project Agent with launcher.json and installed.json; `tool(args, on)` answers the tool.
function liveWorld({ launcher = LREC(), installed = INSTALLED, machine = null, status = "idle", tool = null, stale = null } = {}) {
  const sid = machine ? `${machine}/${S}` : S, addr = machine ? `${machine}/t4w` : "t4w";
  world({ agentTerminal: "t4w", machine, records: { tb1: { space: sid, project: "/proj", profile: "test", name: "proj", terminal: addr, alive: true } },
    extraTabs: [paTab], extraPanes: [{ ...paPane(addr), status }], extraTerms: [{ ...paTerm(addr), status }], agents: [{ address: addr, status }] });
  const dir = machine ? `${machine}:.config/herdr-pa/spaces/${S}` : `:${DIR}`, home = machine ? `${machine}:.config/herdr-pa` : ":/home/u/.config/herdr-pa";
  if (launcher !== undefined) g.__files[`${dir}/launcher.json`] = typeof launcher === "string" ? launcher : JSON.stringify(launcher);
  if (installed) g.__files[`${home}/installed.json`] = JSON.stringify(installed);
  if (stale) g.__execOut = { [`${machine ?? ""}:stale`]: { stdout: stale } };
  g.__execFn = (command, args, on) => (command === TOOL ? (tool ? tool(args, on) : (args.includes("--dry-run") ? DRY_OK : OK_REPLY())) : undefined);
  return { sid, addr };
}
const toolRuns = () => g.__execs.filter(([c]) => c === TOOL);
const realRuns = () => toolRuns().filter(([, a]) => !a.includes("--dry-run"));
async function liveMount() { const v = await mount(); await settle(); await settle(); return v; }
// Nothing in the click path ends a terminal or a process (AC13.1).
const noEnding = () => {
  check(!g.__runs.some(([n]) => /^(pane\.close|terminal\.kill|tab\.close)$/.test(n)), `no pane.close / terminal.kill / tab.close (got ${JSON.stringify(g.__runs)})`);
  check(!g.__hcs.some((a) => a[0] === "kill"), `no hc kill (got ${JSON.stringify(g.__hcs)})`);
  check(!g.__execs.some(([c, a]) => (a ?? []).some((x) => ["--end-terminal", "--anyway", "kill"].includes(String(x)))), `no exec that ends anything (got ${JSON.stringify(g.__execs)})`);
};

cases["P37-button-live-only"] = async () => { // R13: Restart only while it runs
  liveWorld();
  let v = await liveMount();
  check(Boolean(look(v).clicks["pa-restart"]) && /Restart/.test(look(v).text), `live: Restart (clicks ${Object.keys(look(v).clicks)})`);
  world(); // not running
  v = await liveMount();
  check(!look(v).clicks["pa-restart"], "down: no Restart");
  world({ machine: M }); g.__machines = [{ name: null, layout: "ready" }, { name: M, layout: "offline" }];
  v = await liveMount();
  check(!look(v).clicks["pa-restart"], "offline: no Restart");
};

cases["P37-confirm-dry-run"] = async () => { // R13: the click asks first: one dry run, its lines, the choices
  let release;
  liveWorld({ tool: (args) => (args.includes("--dry-run") ? new Promise((r) => { release = () => r(DRY_OK); }) : OK_REPLY()) });
  const v = await liveMount();
  await click(v, "pa-restart");
  check(/Checking/.test(look(v).text), `checking while the dry run runs (got ${look(v).text.slice(0, 400)})`);
  if (look(v).clicks["pa-restart"]) await click(v, "pa-restart"); // a second click while it runs adds nothing
  check(toolRuns().length === 1, `one dry run (got ${JSON.stringify(toolRuns())})`);
  check(JSON.stringify(toolRuns()[0][1]) === JSON.stringify(["--space", S, "--dry-run"]) && toolRuns()[0][2] === null, `its argv (got ${JSON.stringify(toolRuns()[0])})`);
  release(); await settle(); await settle();
  const { text, clicks } = look(v);
  check(text.includes("w1 t-1-build: keeps running") && text.includes("restarts when this turn ends"), `its lines shown (got ${text.slice(0, 900)})`);
  check(Boolean(clicks["pa-restart-go"]) && Boolean(clicks["pa-restart-now"]) && Boolean(clicks["pa-restart-cancel"]), `Restart, Restart now, Cancel (got ${Object.keys(clicks)})`);
  check(!clicks["pa-restart-fresh"], "no new-conversation choice while the prompt is current");
  check(realRuns().length === 0, "nothing restarted yet");
  await click(v, "pa-restart-cancel");
  check(!look(v).clicks["pa-restart-go"] && realRuns().length === 0, "Cancel: gone, nothing run");
  noEnding();
};

cases["P37-confirm-refused"] = async () => { // R13: a refused dry run: its text, no Restart choice
  liveWorld({ tool: () => ({ code: 2, stdout: "", stderr: "cannot restart: the heartbeat is 40 s old: its launcher is not running\n" }) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  const { text, clicks } = look(v);
  check(text.includes("the heartbeat is 40 s old"), `the refusal (got ${text.slice(0, 600)})`);
  check(!clicks["pa-restart-go"] && !clicks["pa-restart-now"] && Boolean(clicks["pa-restart-cancel"]), `only Cancel (got ${Object.keys(clicks)})`);
  check(realRuns().length === 0, "nothing restarted");
};

cases["P37-restart-ok"] = async () => { // R13: the one run, its argv, Restarting…, then what it loaded (60 s)
  let release;
  liveWorld({ tool: (args) => (args.includes("--dry-run") ? DRY_OK : new Promise((r) => { release = () => r(OK_REPLY()); })) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  await click(v, "pa-restart-go");
  check(realRuns().length === 1, `one run (got ${JSON.stringify(realRuns())})`);
  const [, args, on] = realRuns()[0];
  check(JSON.stringify(args) === JSON.stringify(["--space", S, "--json", "--wait", "180", "--expect-instance", "i-1", "--by", "panel", "--why", "the Restart button in the Project Agent panel"]) && on === null, `its argv (got ${JSON.stringify(realRuns()[0])})`);
  check(/Restarting…/.test(look(v).text) && !look(v).clicks["pa-restart"], `Restarting…, no button (got ${look(v).text.slice(0, 500)})`);
  release(); await settle(); await settle();
  const text = look(v).text;
  check(text.includes("Restarted: tools abcdef1 (1234567), core fedcba9, profile test read 10:05 UTC"), `what it loaded (got ${text.slice(0, 700)})`);
  check(Boolean(look(v).clicks["pa-restart"]), "the button is back");
  advance(61000); await settle();
  check(!look(v).text.includes("Restarted: tools"), "gone after 60 s");
  noEnding();
};

cases["P37-restart-now"] = async () => { // R13: Restart now adds --now and nothing else
  liveWorld({ status: "working" });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  await click(v, "pa-restart-now"); await settle();
  check(realRuns().length === 1, "one run");
  const args = realRuns()[0][1];
  check(args[args.length - 1] === "--now" && args.filter((x) => x === "--now").length === 1 && !args.includes("--fresh"), `--now (got ${JSON.stringify(args)})`);
};

cases["P37-restart-waiting"] = async () => { // R13: mid-turn, the wait says so
  liveWorld({ status: "working", tool: (args) => (args.includes("--dry-run") ? DRY_OK : new Promise(() => {})) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  await click(v, "pa-restart-go");
  check(/Restarting… \(waiting for its turn to end\)/.test(look(v).text), `waiting for its turn (got ${look(v).text.slice(0, 500)})`);
};

const outcome = async (reply, want) => {
  liveWorld({ tool: (args) => (args.includes("--dry-run") ? DRY_OK : reply) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  await click(v, "pa-restart-go"); await settle(); await settle();
  const text = look(v).text;
  check(text.includes(want), `says ${want} (got ${text.slice(0, 700)})`);
  check(realRuns().length === 1, "one run");
  return v;
};
cases["P37-restart-failed"] = () => outcome({ code: 1, stdout: `${JSON.stringify({ request: "r1", outcome: "failed", why: "installed.json is missing" })}\n`, stderr: "x" }, "The restart did not happen: installed.json is missing");
cases["P37-restart-timeout"] = () => outcome({ code: 1, stdout: "Requested (r1); waiting up to 180 s.\n", stderr: "The restart did not happen within 180 s (it is mid-turn; the request was withdrawn).\n" }, "The restart did not happen within 180 s (it is mid-turn; the request was withdrawn).");
cases["P37-instance-changed"] = async () => { // the click is bound to the launcher it showed: a refusal is said, nothing run again
  const v = await outcome({ code: 2, stdout: "", stderr: "cannot restart: its launcher restarted since you looked (now instance i-2); look again\n" }, "restarted since you looked");
  await settle();
  check(realRuns().length === 1, "no second run");
  check(Boolean(look(v).clicks["pa-restart"]), "the button is back (a new click asks again)");
};
cases["P37-missing-tool"] = async () => { // pa-sync on a remote machine
  liveWorld({ machine: M, tool: () => { throw new Error(`exec: ${TOOL}: not found`); } });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  check(look(v).text.includes(`${TOOL} is not installed on ${M}; run pa-sync --on ${M}.`), `says so (got ${look(v).text.slice(0, 600)})`);
};

cases["P37-restart-unknown"] = async () => { // the run never answers: given up after its limit, said, a reload
  liveWorld({ tool: (args) => (args.includes("--dry-run") ? DRY_OK : new Promise(() => {})) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  await click(v, "pa-restart-go");
  for (let i = 0; i < 25; i++) { advance(10000); await settle(); }
  check(/result is not known/.test(look(v).text), `not known (got ${look(v).text.slice(0, 600)})`);
  check(Boolean(look(v).clicks["pa-restart"]), "the button is back");
};

cases["P37-remote-on"] = async () => { // R8: a space on m1: reads and runs there, nothing local
  liveWorld({ machine: M });
  const v = await liveMount();
  check(g.__reads.includes(`${M}:${RDIR}/launcher.json`) && g.__reads.includes(`${M}:.config/herdr-pa/installed.json`), `read on ${M} (got ${JSON.stringify(g.__reads.filter((r) => /launcher|installed/.test(r)))})`);
  check(!g.__reads.some((r) => /launcher\.json|installed\.json/.test(r) && !r.startsWith(`${M}:`)), "no local read of either");
  await click(v, "pa-restart"); await settle();
  await click(v, "pa-restart-go"); await settle();
  check(toolRuns().length === 2 && toolRuns().every(([, , on]) => on === M), `both runs on ${M} (got ${JSON.stringify(toolRuns())})`);
  check(!realRuns()[0][1].includes("--on"), "no --on flag: the exec runs there");
};

cases["P37-r10"] = async () => { // R13: a launcher from before restart support: Restart (ends workers), the line to copy, never run
  const r10 = `This Project Agent's launcher started before restart support (2026-11-16T09:00:00Z). It cannot take a restart request; it can only be restarted by ending its terminal, which also ends its 1 worker(s): w1 t-1-build. Run \`${TOOL} --space sp1 --end-terminal\` when they are done, or add \`--anyway\`.`;
  liveWorld({ launcher: LREC({ version: 1, accepts: undefined }), tool: (args) => (args.includes("--dry-run") ? { code: 2, stdout: "", stderr: `${r10}\n` } : OK_REPLY()) });
  const v = await liveMount();
  check(/started before restart support/.test(look(v).text) && /Restart \(ends workers\)/.test(look(v).text), `the badge and the button (got ${look(v).text.slice(0, 500)})`);
  await click(v, "pa-restart"); await settle();
  const { text, clicks } = look(v);
  check(text.includes("w1 t-1-build") && text.includes(`${TOOL} --space ${S} --end-terminal`), `the R10 text and the line (got ${text.slice(0, 900)})`);
  check(Boolean(clicks["copy-end-terminal"]) && !clicks["pa-restart-go"] && !clicks["pa-restart-now"], `Copy, no Restart (got ${Object.keys(clicks)})`);
  await click(v, "copy-end-terminal");
  check((g.__clipboard ?? []).includes(`${TOOL} --space ${S} --end-terminal`), `copied (got ${JSON.stringify(g.__clipboard)})`);
  check(toolRuns().length === 1 && toolRuns()[0][1].includes("--dry-run"), `only the dry run ran (got ${JSON.stringify(toolRuns())})`);
  noEnding();
};

cases["P37-r10-remote-line"] = async () => { // the line to copy names the machine (J3's --on form)
  liveWorld({ machine: M, launcher: null, tool: () => ({ code: 2, stdout: "", stderr: "This Project Agent's launcher started before restart support (x).\n" }) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  check(look(v).text.includes(`${TOOL} --space ${S} --end-terminal --on ${M}`), `with --on (got ${look(v).text.slice(0, 700)})`);
};

cases["P37-double-click"] = async () => { // never twice: two clicks on the confirm's Restart in one frame: one run
  liveWorld({ tool: (args) => (args.includes("--dry-run") ? DRY_OK : new Promise(() => {})) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  const { clicks } = look(v);
  clicks["pa-restart-go"]({}, cx); clicks["pa-restart-go"]({}, cx); clicks["pa-restart-now"]({}, cx); await flush(); await settle();
  check(realRuns().length === 1, `one run (got ${JSON.stringify(realRuns())})`);
};

cases["P37-other-copy"] = async () => { // never twice: another panel window's fresh mark holds the button; an old one does not
  liveWorld();
  g.__shared["project-agent.restarting"] = { [S]: { by: "other", at: T0, instance: "i-1" } };
  const v = await liveMount();
  check(!look(v).clicks["pa-restart"] && /Restarting… \(from another panel window\)/.test(look(v).text), `held (got ${look(v).text.slice(0, 500)})`);
  advance(250000); await settle();
  check(Boolean(look(v).clicks["pa-restart"]), "a mark past its time: the button is back");
};

cases["P37-shares-its-mark"] = async () => { // the run takes the shared mark, and gives it back when it answers
  let release;
  liveWorld({ tool: (args) => (args.includes("--dry-run") ? DRY_OK : new Promise((r) => { release = () => r(OK_REPLY()); })) });
  const v = await liveMount();
  await click(v, "pa-restart"); await settle();
  await click(v, "pa-restart-go");
  const m = g.__shared["project-agent.restarting"]?.[S];
  check(m && m.by === v.me && m.instance === "i-1", `the mark (got ${JSON.stringify(g.__shared["project-agent.restarting"])})`);
  release(); await settle(); await settle();
  check(!g.__shared["project-agent.restarting"]?.[S], "given back");
};

cases["P37-badge-four-cases"] = async () => { // R14 AC14.1: the header's words
  const want = [
    [LREC(), INSTALLED, null],
    [LREC({ code: { tools: "X" } }), INSTALLED, "older code (tools)"],
    [LREC({ session: { promptCore: "Y" } }), INSTALLED, "older code (core)"],
    [LREC({ code: { tools: "X" }, session: { promptCore: "Y" } }), INSTALLED, "older code (tools, core)"],
    [null, INSTALLED, "started before restart support"], // null: launcher.json holds no record (missing reads the same)
    [LREC(), null, "code unknown"],
  ];
  for (const [launcher, installed, word] of want) {
    liveWorld({ launcher, installed });
    const v = await liveMount();
    const text = look(v).text;
    if (word) check(text.includes(word), `${word} (got ${text.slice(0, 400)})`);
    else check(!/older code|started before restart support|code unknown/.test(text), `current: no badge (got ${text.slice(0, 400)})`);
  }
};

cases["P37-notice-text"] = async () => { // R14: the notice says tools too, offers Restart; the old /exit-only text is gone
  liveWorld({ stale: "core/orchestrator.md\n" });
  let v = await liveMount();
  let text = look(v).text;
  check(text.includes("Its instructions or tools changed since it started (orchestrator.md). Restart reloads them (or /exit in its tab).") && Boolean(look(v).clicks["pa-restart-notice"]), `stale files (got ${text.slice(0, 700)})`);
  check(!/Type \/exit in its tab/.test(text), "the old text is gone");
  liveWorld({ launcher: LREC({ code: { tools: "X" } }) });
  v = await liveMount();
  text = look(v).text;
  check(text.includes("Its instructions or tools changed since it started (tools). Restart reloads them (or /exit in its tab)."), `older tools alone (got ${text.slice(0, 700)})`);
  await click(v, "pa-restart-notice"); await settle();
  check(toolRuns().length === 1 && toolRuns()[0][1].includes("--dry-run"), "the notice's button asks first, as the header's");
  liveWorld();
  v = await liveMount();
  check(!/changed since it started/.test(look(v).text), "current and nothing stale: no notice");
};

cases["P37-recorded-core"] = async () => { // R14 + Q-J4-3: a recorded prompt: the notice says so; the confirm offers a new conversation
  liveWorld({ launcher: LREC({ session: { prompt: "recorded", promptCore: "Y" } }) });
  const v = await liveMount();
  const text = look(v).text;
  check(text.includes("Restart in place keeps the core text this conversation began with; Restart in a new conversation loads it."), `the sentence (got ${text.slice(0, 800)})`);
  await click(v, "pa-restart"); await settle();
  check(Boolean(look(v).clicks["pa-restart-fresh"]), `a new-conversation choice (got ${Object.keys(look(v).clicks)})`);
  check(/previous one stays in \/resume/.test(look(v).text), "it says the previous conversation stays");
  await click(v, "pa-restart-fresh"); await settle();
  const args = realRuns()[0][1];
  check(args[args.length - 1] === "--fresh" && !args.includes("--now"), `--fresh (got ${JSON.stringify(args)})`);
};

cases["P37-header-restarting"] = async () => { // R15: the keeper marks a record restarting (not alive): Restarting…, no Open
  world({ agentTerminal: "t4w", records: { tb1: { space: S, project: "/proj", profile: "test", name: "proj", terminal: "t4w", restarting: true } }, extraTabs: [paTab], extraPanes: [paPane()],
    extraTerms: [{ ...paTerm(), labels: { "herdr.pa": S, "herdr.pa.restarting": "r1" } }], agents: [{ address: "t4w", status: null }] });
  const v = await liveMount();
  const { text, clicks } = look(v);
  check(/Restarting…/.test(text) && !/Not running/.test(text), `Restarting… (got ${text.slice(0, 400)})`);
  check(!clicks["pa-open"] && !clicks["pa-restart"], `no Open, no Restart (got ${Object.keys(clicks)})`);
};

// Which cases run (t-031): every case, or the named ones; --shard=<k>/<n> keeps every n-th case by definition
// order (index mod n == k - 1), so n shards together run each case once, and a case added later lands in one.
// ---- t-055: the Queue tab (queue.json, read only) ----
const QA = "/w/herdr-project-agent", QB = "/w/other";
const sha = (c) => c.repeat(40);
const qEntry = (task, extra = {}) => ({ task, job: null, branch: `${task}-b`, worktree: `/wt/${task}`, onto: "main", dependsOn: null, state: "checking",
  added: "2026-11-18T10:00:00Z", base: sha("a"), tip: sha("b"), checked: {}, needs: null, suite: null, review: null, rebases: [], approval: null, ...extra });
const qRepo = (entries, extra = {}) => ({ common: "/x/.git", main: "main", mainAt: sha("c"), planned: [], entries, left: [], merged: [], ...extra });
const qDoc = (repos) => JSON.stringify({ version: 1, repos });
const QPATH = `:${DIR}/queue.json`;
const qCard = (id, status, entries, extra = {}) => JSON.stringify({ id, status, kind: "queue", question: "Merge these?", options: [{ label: "Merge all in order" }, { label: "Not now" }],
  created: "2026-11-18T10:00:00Z", queue: { version: 1, repo: QA, entries }, ...extra });
// A world with queue.json (text, or undefined: none), a ledger, and cards { id: text }; the view opened on Queue.
async function queueWorld(text, { ledger = null, cards = {}, open = true, opts = {} } = {}) {
  world(opts);
  if (text !== undefined) g.__files[QPATH] = text;
  if (ledger) g.__files[`:${DIR}/ledger.json`] = JSON.stringify(ledger);
  for (const [id, c] of Object.entries(cards)) g.__files[`:${DIR}/decisions/${id}.json`] = c;
  g.__ls[`:${DIR}/decisions`] = Object.keys(cards).map((id) => `${id}.json`);
  const v = await mount(); await settle();
  if (open) await click(v, "view-queue");
  return v;
}
const twoRepos = () => qDoc({
  [QA]: qRepo([
    qEntry("t-A", { state: "passed", suite: { exit: 0, ended: "2026-11-18T10:05:00Z", worker: "t40", tree: sha("d") } }),
    qEntry("t-B", { onto: "t-A-b", dependsOn: { task: "t-A", job: null } }),
    qEntry("t-C", { onto: "t-B-b", needs: { shortReview: { tip: sha("b"), file: "SHORT-REVIEW-t-C-1.md", since: "2026-11-18T10:01:00Z", files: [] } },
      suite: { exit: 0, ended: "2026-11-18T10:07:00Z", worker: "t42", tree: sha("e") } }),
  ]),
  [QB]: qRepo([]),
});

cases["Q55-1-tab-order"] = async () => { // R1: Decisions, Tasks, Queue, Rules, Costs; the count over every repository
  const v = await queueWorld(qDoc({ [QA]: qRepo([qEntry("t-A"), qEntry("t-B", { onto: "t-A-b" })]), [QB]: qRepo([qEntry("t-X")]) }), { open: false });
  const { text, clicks } = look(v);
  const at = (s) => text.indexOf(s);
  check(Boolean(clicks["view-queue"]), `a Queue tab (clicks ${Object.keys(clicks).join(", ")})`);
  check(at("Queue · 3") > at("Tasks · ") && at("Rules · ") > at("Queue · 3") && at("Tasks · ") > at("Decisions") && at("Decisions") >= 0, `order and count (got ${text.slice(0, 600)})`);
};

cases["Q55-2-entries"] = async () => { // R2: entries in merge order (t-154: slots, no numbers; Q154-3), state, stack, depends on, waiting, suite
  const v = await queueWorld(twoRepos());
  const { text } = look(v);
  const order = ["t-A", "t-B", "t-C"].map((s) => text.indexOf(s));
  check(order.every((i) => i >= 0) && order[0] < order[1] && order[1] < order[2], `entries in order (got ${text})`);
  check(/herdr-project-agent/.test(text) && /\bother\b/.test(text), `each repository by its folder name (got ${text})`);
  check(/passed/.test(text) && /checking/.test(text), "states");
  check(/on main/.test(text) && /on t-A-b \(t-A\)/.test(text) && /on t-B-b \(t-B\)/.test(text), `stack lines (got ${text})`);
  check(/depends on t-A/.test(text), "depends on");
  check(/waits for its suite/.test(text), `t-B waits for its suite (got ${text})`);
  check(/waits for its short review \(SHORT-REVIEW-t-C-1\.md\)/.test(text), `t-C waits for its short review (got ${text})`);
  check(/suite passed 11-18 10:05 by t40/.test(text), `t-A's suite line (got ${text})`);
  check(/As pa-queue last wrote it; pa-queue show reads each state now\./.test(text), "the stored-state line");
  check(/Queue empty\./.test(text), "the empty repository says so");
};

cases["Q55-3-restack"] = async () => { // R2: an onto that is not the entry ahead; invalidated waits for a restack; a failed suite
  const v = await queueWorld(qDoc({ [QA]: qRepo([
    qEntry("t-A", { suite: { exit: 0, ended: "2026-11-18T10:04:00Z", worker: "t44" } }),
    qEntry("t-B", { onto: "t-gone-b", state: "invalidated" }),
    qEntry("t-C", { onto: "t-B-b", state: "failed", suite: { exit: 1, ended: "2026-11-18T10:09:00Z", worker: "t43" } }),
  ]) }));
  const { text } = look(v);
  check(/on t-gone-b · restack pending/.test(text), `restack pending (got ${text})`);
  check(/invalidated/.test(text) && /waits for pa-queue restack/.test(text), `invalidated waits for a restack (got ${text})`);
  check(/failed/.test(text) && /suite failed \(exit 1\) 11-18 10:09 by t43/.test(text), `the failed suite (got ${text})`);
  check(/waits for pa-gate at its tip, or the next pa-queue refresh/.test(text), `t-A: nothing stored to wait for (got ${text})`);
};

cases["Q55-4-approval"] = async () => { // R2, R6: J3b's approval shapes and queue cards
  const v = await queueWorld(qDoc({ [QA]: qRepo([
    qEntry("t-A", { approval: { card: "d-queue-1", option: "Merge all in order", answeredAt: "2026-11-18T10:10:00Z" } }),
    qEntry("t-B", { onto: "t-A-b", approval: { card: "d-queue-1", option: "Merge all in order", answeredAt: "2026-11-18T10:10:00Z", void: "content changed" } }),
    qEntry("t-C", { onto: "t-B-b", approval: { card: "d-queue-0", option: "Merge all in order", carried: "doc-only, short review SHORT-REVIEW-t-C-1.md" } }),
    qEntry("t-D", { onto: "t-C-b" }),
    qEntry("t-E", { onto: "t-D-b" }),
    qEntry("t-F", { onto: "t-E-b" }),
  ]) }), { cards: {
    "d-queue-2": qCard("d-queue-2", "open", [{ task: "t-D", job: null }]),
    "d-queue-3": qCard("d-queue-3", "answered", [{ task: "t-E", job: null }], { answer: "Merge all in order" }),
  } });
  const { text } = look(v);
  check(/approval: approved: Merge all in order \(d-queue-1, 11-18 10:10\)/.test(text), `approved (got ${text})`);
  check(/approval: voided \(content changed\)/.test(text), `voided (got ${text})`);
  check(/approval: approved, carried over \(doc-only, short review SHORT-REVIEW-t-C-1\.md\)/.test(text), `carried over (got ${text})`);
  check(/approval: card asked \(d-queue-2\)/.test(text), `card asked (got ${text})`);
  check(/approval: answered on d-queue-3, applied at the next pa-queue merge/.test(text), `answered, not applied (got ${text})`);
  check(/approval: not asked yet/.test(text), `not asked yet (got ${text})`);
};

cases["Q55-5-left"] = async () => { // R3: left newest first, why, void, the fix round it went to
  const left = Array.from({ length: 12 }, (_, i) => ({ task: `t-L${i + 1}`, job: null, branch: `t-L${i + 1}-b`, state: "failed", why: `why ${i + 1}`,
    at: `2026-11-18T09:${String(10 + i).padStart(2, "0")}:00Z`, tip: sha("f"), approval: null }));
  left[11] = { ...left[11], task: "t-B", why: "the full suite exited 1 on its tree", at: "2026-11-18T10:20:00Z", approval: { card: "d-queue-1", void: "the full suite exited 1 on its tree" } };
  left[10] = { ...left[10], task: "t-C", why: "conflict", at: "2026-11-18T10:15:00Z" };
  const ledger = { tasks: [
    { id: "t-B", state: "in-progress", workers: [{ address: "t14", role: "build", round: 1, started: "2026-11-18T09:00:00Z" }, { address: "t15", role: "fix", round: 2, started: "2026-11-18T10:25:00Z" }] },
    { id: "t-C", state: "in-progress", workers: [{ address: "t16", role: "build", round: 1, started: "2026-11-18T09:00:00Z" }] },
  ] };
  const v = await queueWorld(qDoc({ [QA]: qRepo([], { left }) }), { ledger });
  const { text } = look(v);
  check(/Left · 12/.test(text), `the left count (got ${text})`);
  check(/t-B \| \(untitled\) \| in-progress \| left 11-18 10:20: the full suite exited 1 on its tree/.test(text) && text.indexOf("left 11-18 10:20") < text.indexOf("left 11-18 10:15"), `newest first, with why; t-154: a task item (got ${text})`);
  check(/went to fix round 2 \(t15, fix\)/.test(text), `t-B's fix round (got ${text})`);
  check(/t-C \| \(untitled\) \| in-progress \| left 11-18 10:15: conflict/.test(text) && /no fix round yet/.test(text), `t-C: no fix round yet (got ${text})`);
  check(/approval voided: the full suite exited 1 on its tree/.test(text), "the void");
  check(/and 2 earlier/.test(text) && !/t-L1 \| left/.test(text) && !/t-L2 \| left/.test(text) && /t-L3 \| left/.test(text), `10 shown (got ${text})`);
};

cases["Q55-6-merged"] = async () => { // R3: the last merged (t-145: 10, the 11th behind Archived; P145-10), newest first, by hand or by the queue with its card
  const merged = Array.from({ length: 6 }, (_, i) => ({ task: `t-M${i + 1}`, job: null, tip: `${i + 1}`.repeat(40), from: sha("a"), to: sha("b"), at: `2026-11-18T08:0${i}:00Z`, by: "hand" }));
  merged[5] = { ...merged[5], by: "queue", card: "d-queue-1" };
  merged.unshift(...Array.from({ length: 5 }, (_, i) => ({ task: `t-old${i}`, job: null, tip: sha("f"), from: sha("a"), to: sha("b"), at: `2026-11-17T08:0${i}:00Z`, by: "hand" }))); // t-145: 11 in all
  const v = await queueWorld(qDoc({ [QA]: qRepo([], { merged }) }));
  const { text } = look(v);
  check(/t-M6 \| merged 11-18 08:05 at 6666666 \(queue, d-queue-1\)/.test(text), `by the queue (got ${text})`);
  check(/t-M2 \| merged 11-18 08:01 at 2222222 \(hand\)/.test(text) && /t-M1 \| merged/.test(text) && /t-old1 \| merged/.test(text) && !/t-old0 \| merged/.test(text), `the last 10 (got ${text})`);
  check(text.indexOf("t-M6 | merged") < text.indexOf("t-M2 | merged"), "newest first");
};

cases["Q55-7-missing"] = async () => { // R7: no queue.json
  const v = await queueWorld(undefined);
  const { text } = look(v);
  check(/No merge queue in this space yet\. pa-queue add starts one\./.test(text), `the empty sentence (got ${text})`);
  check(/(^| )Queue( |$)/.test(text) && !/Queue · /.test(text), `the tab has no count (got ${text.slice(0, 400)})`);
};

cases["Q55-8-invalid"] = async () => { // R7: an invalid file is said, flagged on the tab, never written
  for (const [body, why] of [["{not json", /queue\.json is not valid JSON/], [JSON.stringify({ version: 1, repos: [] }), /queue\.json needs a "repos" object/],
    [JSON.stringify({ version: 2, repos: {} }), /queue\.json is version 2; this panel reads version 1/]]) {
    const v = await queueWorld(body);
    const { text, clicks } = look(v);
    check(why.test(text) && /Queue · !/.test(text), `error and the tab flag for ${body} (got ${text})`);
    for (const id of Object.keys(clicks).filter((k) => /^queue-/.test(k))) { clicks[id]({}, cx); await flush(); }
    check(g.__files[QPATH] === body && !g.__writes.includes(QPATH), "never written");
  }
};

cases["Q55-9-hung"] = async () => { // R5: a hung read is never "no queue"; after a good read, a hung one keeps the last known
  world(); g.__files[QPATH] = twoRepos(); g.__hang = [QPATH];
  const v = await mount(); await settle(); await click(v, "view-queue");
  let { text } = look(v);
  check(/Loading queue…/.test(text) && !/No merge queue/.test(text), `loading, not none (got ${text})`);
  await step(11000);
  ({ text } = look(v));
  check(!/No merge queue/.test(text), `a timeout is not "no queue" (got ${text})`);
  g.__hang = [];
  advance(3000); v.reload(cx); await settle();
  ({ text } = look(v));
  check(/\| t-A \| passed/.test(text), `read once it answers (got ${text})`);
  g.__hang = [QPATH];
  advance(3000); v.reload(cx); await settle(); await step(11000);
  ({ text } = look(v));
  check(/\| t-A \| passed/.test(text) && /Queue · 3/.test(text), `the last known stays (got ${text})`);
};

cases["Q55-10-read-only"] = async () => { // R4: the view writes nothing and runs nothing
  const v = await queueWorld(twoRepos(), { open: false });
  const writes = g.__writes.length, execs = g.__execs.length, hcs = g.__hcs.length;
  await click(v, "view-queue");
  for (const id of Object.keys(look(v).clicks).filter((k) => /^queue-/.test(k))) { look(v).clicks[id]?.({}, cx); await flush(); }
  check(g.__writes.length === writes && g.__execs.length === execs && g.__hcs.length === hcs, `no write, exec or hc (got ${JSON.stringify([g.__writes.slice(writes), g.__execs.slice(execs), g.__hcs.slice(hcs)])})`);
  check(g.__writes.every((w) => !/queue\.json/.test(w)), "queue.json never written");
};

cases["Q55-11-one-read"] = async () => { // R5: one queue.json read per load; a background load while it is pending starts no second one
  const v = await queueWorld(twoRepos());
  const reads = () => g.__issued.filter((x) => x.kind === "read" && x.path === `${DIR}/queue.json`).length;
  const before = reads();
  check(before >= 1, `read by the load (got ${before})`);
  g.__hang = [QPATH];
  advance(3000); v.reload(cx); await settle();
  const mid = reads();
  check(mid === before + 1, `one read for that load (got ${mid - before})`);
  advance(3000); v.reload(cx, { background: true }); await settle();
  advance(3000); v.reload(cx, { background: true }); await settle();
  check(reads() === mid, `no second read while one is pending (got ${reads() - mid})`);
  g.__hang = [];
};

cases["Q55-12-remote"] = async () => { // R5: a remote space's queue.json is read on its machine
  world({ machine: M });
  g.__files[`${M}:${RDIR}/queue.json`] = twoRepos();
  const v = await mount(); await settle();
  await click(v, "view-queue");
  check(g.__issued.some((x) => x.kind === "read" && x.on === M && x.path === `${RDIR}/queue.json`), `read on ${M} (got ${JSON.stringify(g.__issued.filter((x) => /queue/.test(x.path ?? "")))})`);
  check(/\| t-A \| /.test(look(v).text) && !/1\. t-A/.test(look(v).text), "shown");
};

cases["Q55-13-watch"] = async () => { // R5: a watch batch naming queue.json reloads; the queue's lock reloads nothing
  world(); g.__files[QPATH] = twoRepos(); g.__watchers = [];
  await mount(); await settle();
  const fireOnly1s = () => { for (const tm of g.__timers.filter((x) => x.ms === 1000)) tm.fn(cx); };
  const batch = async (paths) => { advance(3000); const w = g.__watchers.shift(); if (w) w.resolve(paths); await turn(2); advance(3000); fireOnly1s(); await settle(); };
  const before = loadsLocal();
  await batch([`${DIR}/.queue.lock`]);
  check(loadsLocal() === before, `the lock reloads nothing (got ${loadsLocal() - before})`);
  await batch([`${DIR}/queue.json`]);
  check(loadsLocal() > before, "queue.json reloads");
};

cases["Q55-14-titles-skipped"] = async () => { // R2, R7: two repositories with one folder name; an item that cannot be read
  const v = await queueWorld(qDoc({ "/w/a/proj": qRepo([qEntry("t-A"), 7, { branch: "x" }]), "/w/b/proj": qRepo([qEntry("t-B")]) }));
  const { text } = look(v);
  check(/\/w\/a\/proj/.test(text) && /\/w\/b\/proj/.test(text), `full paths when the names clash (got ${text})`);
  check(/2 entries could not be read/.test(text) && /t-A/.test(text) && !/1\. t-A/.test(text), `the rest shown, the skipped counted (got ${text})`);
  check(/Queue · 2/.test(text), `only readable entries counted (got ${text.slice(0, 400)})`);
};

// ---- t-084: the Queue tab's worker and evidence links, its header, a merge card's approval; Places holder links ----
const q84Term = (address) => ({ address, state: "live", argv: ["claude"], labels: {}, status: "working", program: claude });
const q84Ledger = () => ({ tasks: [{ id: "t-A", title: "A", state: "in-progress", workers: [
  { address: "t70", role: "queue", name: "t-A-queue", worktree: "/wt/t-A", started: "2026-11-18T10:00:00Z" },
  { address: "t68", role: "build", name: "t-A-other", worktree: "/wt/elsewhere", started: "2026-11-18T10:00:00Z" },
  { address: "t67", role: "verifier", name: "t-A-gone", worktree: "/wt/t-A", started: "2026-11-18T10:00:00Z" }] }] });
const q84Doc = () => qDoc({ [QA]: qRepo([qEntry("t-A", { needs: { shortReview: { tip: sha("b"), file: "SHORT-REVIEW-t-A-1.md" } }, heldApproval: { file: "SHORT-REVIEW-t-A-0.md" } })]) });
const queueClicks = (v, re) => Object.keys(look(v).clicks).filter((k) => re.test(k));

cases["Q84-1-worker"] = async () => { // R1: the entry's open worker on its worktree, a link to the Agents view; one ended: no link
  const v = await queueWorld(q84Doc(), { ledger: q84Ledger(), opts: { extraTerms: [q84Term("t70"), q84Term("t68")] } });
  const { text } = look(v);
  check(text.includes("queue t70 ↗"), `the worker chip (got ${text})`);
  check(text.includes("verifier t67 (ended)"), `an unfinished worker whose terminal is gone: dim (got ${text})`);
  check(!/t68/.test(text), `a worker on another worktree is not the entry's (got ${text})`);
  const ids = queueClicks(v, /^queue-worker-/);
  check(ids.length === 1, `one link: the live worker (got ${ids})`);
  g.__runs = [];
  await click(v, ids[0]);
  check(g.__runs.some(([n, a]) => n === "terminal.peek" && a?.terminal === "t70"), `opens t70 (got ${JSON.stringify(g.__runs)})`);
};

cases["Q84-2-files"] = async () => { // R2: a listed evidence file is a link to the Files view; one not written yet: "(not yet)"
  const v = await queueWorld(q84Doc(), { ledger: q84Ledger() });
  g.__execOut = { ":evidence": { stdout: "t-A\tSHORT-REVIEW-t-A-1.md\nt-A\tFINDINGS.md\n" } };
  advance(3000); v.reload(cx); await settle();
  const { text } = look(v);
  check(text.includes("SHORT-REVIEW-t-A-1.md ↗") && text.includes("SHORT-REVIEW-t-A-0.md (not yet)"), `the link and the not-yet file (got ${text})`);
  const ids = queueClicks(v, /^queue-file-/);
  check(ids.length === 1, `one file link (got ${ids})`);
  await click(v, ids[0]);
  const root = g.__shared["my-files.root"];
  check(root?.file === "SHORT-REVIEW-t-A-1.md" && root?.path === `${DIR}/evidence/t-A` && root?.task === "t-A", `opens the file in Files (got ${JSON.stringify(root)})`);
};

cases["Q84-3-header"] = async () => { // R3: the repository header says its main branch once, labelled
  const v = await queueWorld(twoRepos());
  const { text } = look(v);
  check(/into main/.test(text) && !/main main/.test(text), `into main, never main main (got ${text})`);
};

cases["Q84-4-approval"] = async () => { // R4: an answered merge card for the task: approved on it, not "not asked yet"
  const merge = (id, task, answer) => JSON.stringify({ id, task, status: "answered", answer, question: `Merge ${task} into main?`, options: [{ label: "Merge" }, { label: "Hold" }],
    created: "2026-11-19T02:20:50Z", answered: "2026-11-19T02:21:07Z" });
  const v = await queueWorld(qDoc({ [QA]: qRepo([qEntry("t-A"), qEntry("t-B", { onto: "t-A-b" })]) }), { cards: { "d-194": merge("d-194", "t-A", "Merge"), "d-195": merge("d-195", "t-B", "Hold") } });
  const { text } = look(v);
  check(/approval: approved on d-194 \(Merge, 11-19 02:21\)/.test(text), `approved on d-194 (got ${text})`);
  check((text.match(/approval: not asked yet/g) ?? []).length === 1, `only t-B (Hold) is not asked yet (got ${text})`);
};

cases["Q84-5-remote"] = async () => { // R1: on a remote space the worker opens through its machine's address
  world({ machine: M, extraTerms: [q84Term(`${M}/t70`)] });
  g.__files[`${M}:${RDIR}/queue.json`] = q84Doc();
  g.__files[`${M}:${RDIR}/ledger.json`] = JSON.stringify(q84Ledger());
  const v = await mount(); await settle(); await click(v, "view-queue");
  const ids = queueClicks(v, /^queue-worker-/);
  check(ids.length === 1, `the live worker links (got ${ids}; ${look(v).text})`);
  g.__runs = [];
  await click(v, ids[0]);
  check(g.__runs.some(([n, a]) => n === "terminal.peek" && a?.terminal === `${M}/t70`), `opens ${M}/t70 (got ${JSON.stringify(g.__runs)})`);
};

// ---- t-154: the Queue tab's task links, worker items (status, gone once closed), slots, Left without later merges ----
const Q154 = {
  ledger: () => ({ tasks: [
    { id: "t-A", title: "A", state: "in-progress", workers: [
      { address: "t70", role: "queue", name: "t-A-queue", worktree: "/wt/t-A", started: "2026-11-18T10:00:00Z" },
      { address: "t71", role: "reviewer", name: "t-A-review", worktree: "/wt/rev", started: "2026-11-18T10:00:00Z", finished: true, closed: "2026-11-18T10:30:00Z" },
      { address: "t73", role: "verifier", name: "t-A-gone", worktree: "/wt/t-A", started: "2026-11-18T10:00:00Z" },
      { address: "t74", role: "reviewer", name: "t-A-done", worktree: "/wt/rev2", started: "2026-11-18T10:00:00Z", finished: true }] },
    { id: "t-L", title: "L", state: "in-progress", workers: [] },
    { id: "t-M", title: "M", state: "done", workers: [] },
  ] }),
  key: (i) => `${QA}#${i}`,
};

cases["Q154-1-task-links"] = async () => { // R1: the task id of an entry, a Left item and a Merged item is a link to the task; one the ledger lacks is not
  const left = [{ task: "t-L", job: null, branch: "t-L-b", state: "failed", why: "conflict", at: "2026-11-18T09:00:00Z", tip: sha("f"), approval: null },
    { task: "t-gone", job: null, branch: "t-gone-b", state: "failed", why: "conflict", at: "2026-11-18T09:01:00Z", tip: sha("f"), approval: null }];
  const merged = [{ task: "t-M", job: null, tip: sha("1"), from: sha("a"), to: sha("b"), at: "2026-11-18T08:00:00Z", by: "hand" }];
  const v = await queueWorld(qDoc({ [QA]: qRepo([qEntry("t-A"), qEntry("t-Z", { onto: "t-A-b" })], { left, merged }) }), { ledger: Q154.ledger() });
  const { clicks, text } = look(v);
  check(Boolean(clicks[`queue-task-${Q154.key(0)}`]) && !clicks[`queue-task-${Q154.key(1)}`], `the entry t-A links, t-Z (not in the ledger) does not (have ${Object.keys(clicks).filter((k) => k.startsWith("queue-"))})`);
  check(Boolean(clicks["queue-left-task-0-0"]) !== Boolean(clicks["queue-left-task-0-1"]), `one Left link: t-L, not t-gone (have ${Object.keys(clicks).filter((k) => k.startsWith("queue-left"))})`);
  check(Boolean(clicks["queue-merged-task-0-0"]), `the Merged t-M links (have ${Object.keys(clicks).filter((k) => k.startsWith("queue-merged"))})`);
  check(/t-Z/.test(text) && /t-gone/.test(text), `the unlinked ids still drawn (got ${text})`);
  await click(v, `queue-task-${Q154.key(0)}`); await settle();
  check(g.__shared["project-agent.show"]?.task === "t-A" && g.__shared["project-agent.show"]?.space === S, `the show request (got ${JSON.stringify(g.__shared["project-agent.show"])})`);
  look(v);
  check(v.view === "tasks" && v.open.has("t-A"), `the Tasks tab with t-A open (view ${v.view}, open ${[...v.open]})`);
  await click(v, "view-queue"); await settle();
  const lid = look(v).clicks["queue-left-task-0-0"] ? "queue-left-task-0-0" : "queue-left-task-0-1";
  await click(v, lid); await settle(); look(v);
  check(g.__shared["project-agent.show"]?.task === "t-L" && v.view === "tasks" && v.open.has("t-L"), `Left t-L opens (got ${JSON.stringify(g.__shared["project-agent.show"])}, view ${v.view})`);
  await click(v, "view-queue"); await settle();
  await click(v, "queue-merged-task-0-0"); await settle(); look(v);
  check(g.__shared["project-agent.show"]?.task === "t-M" && v.view === "tasks" && v.open.has("t-M"), `Merged t-M opens (got ${JSON.stringify(g.__shared["project-agent.show"])}, view ${v.view})`);
};

cases["Q154-2-worker-items"] = async () => { // R2: the entry's workers are the task card's worker item with its status color; a closed one is not drawn
  const doc = qDoc({ [QA]: qRepo([qEntry("t-A", { suite: { exit: 0, ended: "2026-11-18T10:05:00Z", worker: "t72" }, review: { verdict: "pass", worker: "t71", tip: sha("b") } }), qEntry("t-A", { job: "two", branch: "t-A2-b", worktree: "/wt/t-A2", onto: "t-A-b", review: { verdict: "pass", worker: "t74", tip: sha("b") } })]) });
  const v = await queueWorld(doc, { ledger: Q154.ledger(), opts: { extraTerms: [q84Term("t70")] } });
  g.__themeNames = true;
  P142.status("t70", "blocked");
  const root = v.render(cx), { text } = inspect(root);
  const item = P142.find(root, `queue-worker-${Q154.key(0)}-0`);
  check(Boolean(item) && text.includes("queue t70 ↗"), `the live worker drawn first (got ${text})`);
  check(P142.dot(item)?.bg === "#blocked", `its dot the terminal's status (got ${JSON.stringify(P142.dot(item))})`);
  check(!/reviewer t71/.test(text) && !P142.find(root, `queue-worker-${Q154.key(0)}-3`), `the closed reviewer t71 is not drawn (got ${text})`);
  check(!/worker t72|t72 \(ended\)|t72 \(closed\)/.test(text), `t72 (no ledger record, no terminal) is not drawn (got ${text})`);
  check(text.includes("verifier t73 (ended)"), `an unfinished recorded worker with no terminal stays, dim (got ${text})`);
  check(text.includes("reviewer t74 (closed)"), `finished, no closed stamp: (closed), as the task card (got ${text})`);
  const dim = P142.find(root, `queue-worker-${Q154.key(0)}-1`);
  check(P142.dot(dim)?.bg === "#dim", `not live: the dim dot (got ${JSON.stringify(P142.dot(dim))})`);
  check(queueClicks(v, /^queue-worker-/).length === 1, `one link: the live worker (got ${queueClicks(v, /^queue-worker-/)})`);
  await click(v, "view-tasks"); await click(v, "task-head-t-A");
  const card = P142.find(v.render(cx), "worker-t-A-0");
  check(Boolean(card) && JSON.stringify(P142.shape(item)) === JSON.stringify(P142.shape(card)) && JSON.stringify(P142.dot(item)) === JSON.stringify(P142.dot(card)), `the task card's box and dot (queue ${JSON.stringify(P142.shape(item))} ${JSON.stringify(P142.dot(item))}, card ${JSON.stringify(P142.shape(card))} ${JSON.stringify(P142.dot(card))})`);
};

cases["Q154-3-slots"] = async () => { // R3: no numbers; each entry a slot, the top one entries[0] (stacked on main, merging first)
  const v = await queueWorld(twoRepos());
  const root = v.render(cx), { text } = inspect(root);
  check(!/(^|\| )\d+\. t-/.test(text), `no "1." numbers (got ${text})`);
  const slots = [0, 1, 2].map((i) => P142.find(root, `queue-slot-${Q154.key(i)}`));
  check(slots.every((s) => s && JSON.stringify(s.style?.border) === "[1]" && s.style?.rounded), `each entry its own bordered slot (got ${slots.map((s) => JSON.stringify(s?.style ?? null)).join(" ; ")})`);
  const seq = slots.map((s) => inspect(s).text);
  check(/t-A/.test(seq[0]) && /on main/.test(seq[0]) && /t-B/.test(seq[1]) && /t-C/.test(seq[2]), `top slot t-A on main, then t-B, t-C (got ${seq.join(" ; ")})`);
  check(text.indexOf(seq[0]) < text.indexOf(seq[1]) && text.indexOf(seq[1]) < text.indexOf(seq[2]), "slots drawn top to bottom in merge order");
};

cases["Q154-4-left-then-merged"] = async () => { // the PA's item: an entry that left and merged later leaves the Left list (and its count)
  const l = (task, at, job = null) => ({ task, job, branch: `${task}-b`, state: "failed", why: "conflict", at, tip: sha("f"), approval: null });
  const m = (task, at, job = null) => ({ task, job, tip: sha("1"), from: sha("a"), to: sha("b"), at, by: "queue", card: "d-1" });
  const left = [l("t-130", "2026-11-18T09:00:00Z"), l("t-B", "2026-11-18T12:00:00Z"), l("t-C", "2026-11-18T09:30:00Z", "api")];
  const merged = [m("t-130", "2026-11-18T11:00:00Z"), m("t-B", "2026-11-18T11:30:00Z"), m("t-C", "2026-11-18T11:40:00Z", "web")];
  const v = await queueWorld(qDoc({ [QA]: qRepo([], { left, merged }) }));
  const { text } = look(v);
  const leftPart = text.slice(text.indexOf("Left · "), text.indexOf("Merged"));
  check(/Left · 2/.test(text), `two left (got ${text})`);
  check(!/t-130/.test(leftPart) && /t-130/.test(text.slice(text.indexOf("Merged"))), `t-130 merged after it left: only under Merged (got ${leftPart})`);
  check(/t-B/.test(leftPart), `t-B left after its merge: stays (got ${leftPart})`);
  check(/t-C/.test(leftPart), `t-C api: another job merged, it stays (got ${leftPart})`);
  const v2 = await queueWorld(qDoc({ [QA]: qRepo([], { left: [l("t-130", "2026-11-18T09:00:00Z")], merged: [m("t-130", "2026-11-18T11:00:00Z")] }) }));
  check(!/Left · /.test(look(v2).text), `every left one merged: no Left heading (got ${look(v2).text})`);
};

cases["Q154-5-left-merged-items"] = async () => { // Added (d-270): Left and Merged entries are task items as in the Tasks tab (id, title, state), the facts as the detail line
  const left = [{ task: "t-L", job: "api", branch: "t-L-b", state: "failed", why: "conflict", at: "2026-11-18T09:00:00Z", tip: sha("f"), approval: null },
    { task: "t-gone", job: null, branch: "t-gone-b", state: "failed", why: "conflict", at: "2026-11-18T09:01:00Z", tip: sha("f"), approval: null }];
  const merged = [{ task: "t-M", job: null, tip: sha("1"), from: sha("a"), to: sha("b"), at: "2026-11-18T08:00:00Z", by: "queue", card: "d-9" }];
  const v = await queueWorld(qDoc({ [QA]: qRepo([], { left, merged }) }), { ledger: Q154.ledger() });
  g.__themeNames = true;
  const root = v.render(cx);
  const lItem = P142.find(root, "queue-left-0-1"), gItem = P142.find(root, "queue-left-0-0"), mItem = P142.find(root, "queue-merged-0-0");
  const lt = inspect(lItem ?? {}).text, gt = inspect(gItem ?? {}).text, mt = inspect(mItem ?? {}).text;
  check(/^t-L \| L \| in-progress \| api · left 11-18 09:00: conflict/.test(lt), `Left: id, title, state, then the facts (got ${lt})`);
  check(/^t-gone \| left 11-18 09:01: conflict/.test(gt), `a task the ledger lacks: its id and the facts (got ${gt})`);
  check(/^t-M \| M \| done \| merged 11-18 08:00 at 1111111 \(queue, d-9\)/.test(mt), `Merged: id, title, state, then the facts (got ${mt})`);
  const box = (el) => el && { rounded: JSON.stringify(el.style?.rounded ?? null), border: JSON.stringify(el.style?.border ?? null), bg: el.style?.bg?.[0] ?? null };
  check(JSON.stringify(box(lItem)) === JSON.stringify({ rounded: "[7]", border: "[1]", bg: "#surface" }) && JSON.stringify(box(mItem)) === JSON.stringify(box(lItem)), `each a bordered item, as a task card (got ${JSON.stringify(box(lItem))} ${JSON.stringify(box(mItem))})`);
  const chipOf = (el, text) => { const [c] = stubs.findText(el, text); return c && Object.fromEntries(["px", "h", "rounded", "bg", "text_size", "text_color"].map((k) => [k, JSON.stringify(c.style?.[k] ?? null)])); };
  const qChip = chipOf(lItem, "in-progress");
  await click(v, "view-tasks");
  const card = P142.find(v.render(cx), "task-head-t-L");
  const tChip = chipOf(card, "in-progress");
  check(Boolean(qChip) && JSON.stringify(qChip) === JSON.stringify(tChip), `the state chip is the Tasks tab's (queue ${JSON.stringify(qChip)}, tasks ${JSON.stringify(tChip)})`);
};

cases["P84-1-holder"] = async () => { // R5: a Places holder line opens its worker in the Agents view
  const { v } = await placesWorld();
  check(look(v).text.includes("t53 · t-020 verify · verifier · suite step · since 11-19 10:02 ↗"), `the holder line is a link (got ${look(v).text.slice(0, 900)})`);
  g.__runs = [];
  await click(v, "places-holder-t53");
  check(g.__runs.some(([n, a]) => n === "terminal.peek" && a?.terminal === "t53"), `opens t53 (got ${JSON.stringify(g.__runs)})`);
};

cases["P84-2-holder-remote"] = async () => { // R5: on a remote space, through the machine's address
  const { v } = await placesWorld({ machine: M });
  g.__runs = [];
  await click(v, "places-holder-t53");
  check(g.__runs.some(([n, a]) => n === "terminal.peek" && a?.terminal === `${M}/t53`), `opens ${M}/t53 (got ${JSON.stringify(g.__runs)})`);
};

// ---- t-032 J3g: the billed block compares every account label in the space (limit 3, R-A5) and, where billed rows carry
// endpoint/region, each pair (D8, A-V3). A comparison without `others` or `byScope` draws exactly as before (J3G_OLD:
// captured from the 1ed3c5e code). ----
const j3gBilled = async (reply) => { const { v } = await costsWorld({ tool: { "billed compare": () => json0(reply) } }); const s = costsSummary(); const text = j1dText(v, s); // the status clocks in the machine's own zone and locale become {clock}
  const billed = text.slice(text.search(/Billed amounts|Billed \(|No billed rows|Billing query/)).replace(/fetched .*? \(billing days/g, "fetched {clock} (billing days")
    .replace(/last run .*? \(/g, "last run {clock} (").replace(/next after .*? · cap/g, "next after {clock} · cap");
  return { v, text, billed }; };
const j3gStates = () => ({ rows: withRows(), "rows-off": withRows(statusOff), "no-rows-on": noRows(statusOn()), "no-rows-off": noRows() });
const J3G_OLD = {
  "rows": "Billed (account-wide: includes use outside this space and outside Herdr) | Copy | Covered 2027-01-10 – 2027-01-14 (5 of 6 days; not billed: 2027-01-15) · source query:q1, fetched {clock} (billing days are UTC days) |  | this space | all spaces here | billed | model-a | {CUR}10.00 est. | {CUR}20.00 est. | {CUR}25.00 billed · account-wide | price factor 1.25 (from quantities) | model-b | — est. (no price) | {CUR}3.00 est. + unpriced | {CUR}2.40 billed · account-wide | scope ratio 0.8 (not applied) below 1.0: the table over-prices (from a partial estimate) (few days) (not final) | other / unmapped labels |  |  | {CUR}1.50 billed · account-wide | total | {CUR}10.00 est. + unpriced | {CUR}23.00 est. + unpriced | {CUR}28.90 billed · account-wide | calibrated (est. × price factor, where one applies): this space {CUR}12.50 + unpriced | No price for model-b: its estimate is the priced part only. | In XXX (not compared): ¤3.00 billed · account-wide, 1 row(s) | Billing query: on · last run {clock} (ok, 1 requests) · next after {clock} · cap 1/day (≤ 3 requests, stated fee ≈ 0.01 XTS each) | Turn off",
  "rows-off": "Billed (account-wide: includes use outside this space and outside Herdr) | Copy | Covered 2027-01-10 – 2027-01-14 (5 of 6 days; not billed: 2027-01-15) · source query:q1, fetched {clock} (billing days are UTC days) |  | this space | all spaces here | billed | model-a | {CUR}10.00 est. | {CUR}20.00 est. | {CUR}25.00 billed · account-wide | price factor 1.25 (from quantities) | model-b | — est. (no price) | {CUR}3.00 est. + unpriced | {CUR}2.40 billed · account-wide | scope ratio 0.8 (not applied) below 1.0: the table over-prices (from a partial estimate) (few days) (not final) | other / unmapped labels |  |  | {CUR}1.50 billed · account-wide | total | {CUR}10.00 est. + unpriced | {CUR}23.00 est. + unpriced | {CUR}28.90 billed · account-wide | calibrated (est. × price factor, where one applies): this space {CUR}12.50 + unpriced | No price for model-b: its estimate is the priced part only. | In XXX (not compared): ¤3.00 billed · account-wide, 1 row(s)",
  "no-rows-on": "Billing query: on · not run yet · next after {clock} · cap 1/day (≤ 3 requests, stated fee ≈ 0.01 XTS each) | Turn off | No billed rows for account acct1 yet.",
  "no-rows-off": "Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
};
const j3gOther = (over = {}) => ({ ...withRows().comparison, account: "acct-c", models: [], other: 0, separate: [], noPrice: [], calibrated: 4.5, calibratedPartial: false,
  total: { thisSpace: 4, allSpaces: 6, thisSpacePartial: false, allSpacesPartial: false, billed: 5.5 }, rows: 2, spaceEstimate: { money: 7, partial: false }, ...over });
const j3gNone = (over = {}) => ({ ...noRows().comparison, account: "acct-d", rows: 0, spaceEstimate: { money: 2, partial: true }, ...over });
cases["P-costs-j3-old"] = async () => { // guard: a comparison without others/byScope draws the same billed block as before J3g, in each state
  for (const [k, reply] of Object.entries(j3gStates())) {
    const { billed } = await j3gBilled(reply);
    check(billed === J3G_OLD[k], `${k}: the same text as before J3g (got ${billed}\nwant ${J3G_OLD[k]})`);
    check(!/Account acct|By endpoint|· region/.test(billed), `${k}: nothing of J3g (got ${billed})`);
  }
  // one byScope entry only (a bill under one pair): no per-pair lines
  const one = withRows(); one.comparison.byScope = [{ endpoint: "global", region: "*", billed: 28.9, thisSpace: 10, allSpaces: 23, thisSpacePartial: false, allSpacesPartial: false }];
  check((await j3gBilled(one)).billed === J3G_OLD.rows, "one pair: drawn as before");
  const empty = withRows(); empty.others = [];
  check((await j3gBilled(empty)).billed === J3G_OLD.rows, "an empty others: drawn as before");
};

cases["P-costs-j3-others"] = async () => { // limit 3: one dim line per other account label, after this account's table; never added to its figures
  const r = withRows(); r.others = [j3gOther(), j3gNone()];
  const { v, billed } = await j3gBilled(r);
  check(billed.startsWith(J3G_OLD.rows.slice(0, J3G_OLD.rows.indexOf("Billing query: on"))), `this account's block first, unchanged (got ${billed})`);
  check(billed.includes("Account acct-c: this space {CUR}4.00 est. · {CUR}5.50 billed · account-wide"), `acct-c's line (got ${billed})`);
  check(billed.includes("Account acct-d: this space {CUR}2.00 est. + unpriced; no billed rows for it"), `acct-d's line (got ${billed})`);
  check(billed.indexOf("Account acct-c") < billed.indexOf("Account acct-d") && billed.indexOf("Account acct-d") < billed.indexOf("Billing query: on"), `in order, before the status (got ${billed})`);
  check(billed.includes("total | {CUR}10.00 est. + unpriced | {CUR}23.00 est. + unpriced | {CUR}28.90 billed · account-wide"), `this account's total untouched (got ${billed})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
  const copy = await j1dCopy(v, "costs-copy-billed");
  check(copy.includes("Account acct-c: this space") && copy.includes("Account acct-d: this space"), `in the billed Copy (got ${copy})`);
  // this account has no billed rows, another has: its lines still show, after the "no rows" line
  const n = noRows(statusOn()); n.others = [j3gOther()];
  const { billed: b2 } = await j3gBilled(n);
  check(b2.includes("No billed rows for account acct1 yet.") && b2.includes("Account acct-c: this space {CUR}4.00 est. · {CUR}5.50 billed · account-wide"), `no own rows (got ${b2})`);
  const off = noRows(); off.others = [j3gOther()];
  const { billed: b3 } = await j3gBilled(off);
  check(b3.includes("Billed amounts: none.") && b3.includes("Account acct-c: this space {CUR}4.00 est. · {CUR}5.50 billed · account-wide"), `off, no own rows (got ${b3})`);
  // malformed others draw nothing and throw nothing
  const bad = withRows(); bad.others = [7, null, { account: {} }, "x"];
  const { billed: b4 } = await j3gBilled(bad);
  check(!/Account |\[object/.test(b4), `malformed others draw nothing (got ${b4})`);
};

cases["P-costs-j3-scopes"] = async () => { // D8: two or more endpoint · region pairs: one line each, est. beside billed
  const r = withRows(); r.comparison.byScope = [
    { endpoint: "global", region: "*", billed: 20, thisSpace: 7, allSpaces: 15, thisSpacePartial: false, allSpacesPartial: false },
    { endpoint: "regional", region: "region-a", billed: 8.9, thisSpace: 3, allSpaces: 8, thisSpacePartial: false, allSpacesPartial: false },
    { endpoint: "regional", region: "?", billed: 0, thisSpace: null, allSpaces: null, thisSpacePartial: false, allSpacesPartial: false }];
  const { v, billed } = await j3gBilled(r);
  for (const want of ["By endpoint · region (billed rows that name an endpoint):", "global: this space {CUR}7.00 est. · {CUR}20.00 billed · account-wide",
    "regional · region-a: this space {CUR}3.00 est. · {CUR}8.90 billed · account-wide", "regional · region unknown: this space — est. (no price) · {CUR}0.00 billed · account-wide"])
    check(billed.includes(want), `shows "${want}" (got ${billed})`);
  check(billed.indexOf("By endpoint") > billed.indexOf("calibrated") && billed.indexOf("By endpoint") < billed.indexOf("In XXX"), `after this account's table (got ${billed})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
  const bad = withRows(); bad.comparison.byScope = [7, { endpoint: {} }, null];
  check(!/By endpoint|\[object/.test((await j3gBilled(bad)).billed), "malformed byScope draws nothing");
};

cases["P-costs-j3-reads"] = async () => { // the view reads and runs nothing new for others and byScope
  const count = async (reply) => { const { v } = await costsWorld({ tool: { "billed compare": () => json0(reply) } }); for (let i = 0; i < 3; i++) { advance(61000); v.reload(cx); look(v); await settle(); } return { reads: costsReads().map((x) => x.path), execs: costsExecs().map(([, a]) => a.join(" ")) }; };
  const r = withRows(); r.others = [j3gOther(), j3gNone()]; r.comparison.byScope = [{ endpoint: "global", region: "*", billed: 1, thisSpace: 1, allSpaces: 1 }, { endpoint: "regional", region: "region-a", billed: 1, thisSpace: 1, allSpaces: 1 }];
  const old = await count(withRows()), now = await count(r);
  check(JSON.stringify(old) === JSON.stringify(now), `the same reads and execs (old ${JSON.stringify(old)} new ${JSON.stringify(now)})`);
};

// ---- t-083 J2: worker places and the start order in the Tasks tab ----
const P83 = {
  rules: JSON.stringify({ version: 1, rules: [{ id: "r-places", title: "Worker places", text: "x", status: "active", order: 50, places: { working: 4, "no-test": 1, suite: 2 } }] }),
  worker: (address, name, role, extra = {}) => ({ address, name, role, round: 1, started: "2026-11-19T10:02:00Z", ...extra }),
  job: (id, kind, extra = {}) => { const [task, job] = id.split("/"); return { id, task, job, kind, role: kind === "no-test" ? "design" : "verifier", source: "pa", added: "2026-11-19T10:00:00Z", blockedBy: null, ...extra }; },
};
const plTerm = (address) => ({ address, state: "live", argv: ["claude"], labels: {}, status: "working", program: claude });
const plJobs = () => [P83.job("t-020/verify-2", "suite"), P83.job("t-060/merge-check", "suite", { source: "queue" }), P83.job("t-083/design", "no-test")];
const plLedger = () => ({ tasks: [
  { id: "t-020", title: "Panel worktrees", state: "in-progress", workers: [P83.worker("t51", "t-020-build", "build", { place: "working" }), P83.worker("t53", "t-020-verify", "verifier", { place: "suite" })] },
  { id: "t-041", title: "Fresh reset", state: "in-progress", workers: [P83.worker("t54", "t-041-review", "reviewer", { place: "no-test" }), P83.worker("t59", "t-041-old", "build", { place: "working", finished: "2026-11-19T09:00:00Z" })] },
  { id: "t-060", title: "Merge queue cards", state: "in-progress" },
  { id: "t-083", title: "Worker places", state: "in-progress" },
  { id: "t-090", title: "Nothing here", state: "in-progress" },
] });
// A world with the places files (null: no such file; waiting text or doc) and the Tasks view open.
async function placesWorld({ waiting = { version: 1, jobs: plJobs() }, order, ledger = plLedger(), rules = P83.rules, machine = null, terms = ["t51", "t53", "t54", "t59x"], exec = null } = {}) {
  world({ machine, extraTerms: terms.map((a) => plTerm(machine ? `${machine}/${a}` : a)) });
  if (exec) g.__execFn = exec; // t-119: a case's answer for pa-full-suite --places --json
  const dir = machine ? `${machine}:${RDIR}` : `:${DIR}`;
  g.__files[`${dir}/ledger.json`] = JSON.stringify(ledger);
  if (rules !== undefined) g.__files[`${dir}/rules.json`] = rules;
  if (waiting !== null) g.__files[`${dir}/waiting.json`] = typeof waiting === "string" ? waiting : JSON.stringify(waiting);
  if (order !== undefined) g.__files[`${dir}/start-order.json`] = order;
  const v = await mount(); await settle();
  await click(v, "view-tasks"); await settle();
  return { v, dir };
}

cases["P83-1-places"] = async () => { // AC1.1, AC1.2: the kinds, their limits and holders; finished or ended workers hold nothing
  const { v } = await placesWorld();
  const { text } = look(v);
  check(/Places · 3 of 4 working · 3 waiting/.test(text), `the section head (got ${text.slice(0, 600)})`);
  check(text.includes("Working · 3 of 4") && text.includes("No-test extra · 0 of 1") && text.includes("Suite step · 1 of 2  (held within Working)"), `AC1.1 the three kinds (got ${text.slice(0, 900)})`);
  check(text.includes("t53 · t-020 verify · verifier · suite step · since 11-19 10:02") && text.includes("t54 · t-041 review · reviewer · since 11-19 10:02"), `AC1.1 holder lines (got ${text.slice(0, 900)})`);
  check(text.includes("limits: r-places"), "the limits' rule");
  check(!/t59 ·/.test(text), "AC1.2: a finished worker is not drawn");
  check(text.indexOf("Places ·") < text.indexOf("| in progress |"), "the section comes before the task sections");
};

cases["P83-2-waiting"] = async () => { // R9: effective order, kind labels, reasons, the queue job without buttons; AC9.1
  const { v } = await placesWorld();
  const { text, clicks } = look(v);
  const at = ["1. t-020 verify-2 · verifier · suite step", "2. t-060 merge-check · verifier · suite step", "3. t-083 design · design · no-test"].map((s) => text.indexOf(s));
  check(at.every((i) => i >= 0) && at[0] < at[1] && at[1] < at[2], `the jobs in order (got ${text})`);
  check(text.includes("Waiting · 3") && text.includes("merge queue order (Queue tab)") && text.includes("waits for a suite place (2 of 2)") && text.includes("next to start: the no-test extra place is free"), `reasons (got ${text})`);
  check(/order: the Project Agent's/.test(text), "no order file: the Project Agent's order");
  check(!clicks["place-up-t-060/merge-check"] && !clicks["place-down-t-060/merge-check"] && !clicks["place-top-t-060/merge-check"], "the queue job has no buttons");
  check(!clicks["place-up-t-020/verify-2"] && !clicks["place-top-t-020/verify-2"] && Boolean(clicks["place-down-t-020/verify-2"]), "AC9.1: the top movable job: ↓ only");
  check(Boolean(clicks["place-up-t-083/design"]) && Boolean(clicks["place-top-t-083/design"]) && !clicks["place-down-t-083/design"], "AC9.1: the last: ↑ and ⤒");
};

cases["P83-3-move"] = async () => { // AC6.1, AC6.3: a move writes the whole order by compare-and-write; three moves, one notice
  const { v, dir } = await placesWorld();
  const path = `${dir}/start-order.json`;
  await click(v, "place-top-t-083/design"); await settle();
  const pinsOf = (text) => JSON.parse(text).pins; // t-097 R10: a Places move writes one pin for the job (version 2)
  check(typeof g.__files[path] === "string" && JSON.stringify(pinsOf(g.__files[path])) === JSON.stringify([{ id: "t-083/design", slot: 0 }]), `the new pin (got ${g.__files[path]})`);
  const doc = JSON.parse(g.__files[path]);
  check(doc.version === 2 && doc.by === "person" && doc.moves.length === 1 && doc.moves[0].id === "t-083/design" && doc.moves[0].move === "top", `the file (got ${g.__files[path]})`);
  check(/order: yours/.test(look(v).text) && look(v).text.indexOf("1. t-083 design") >= 0, `drawn in the new order (got ${look(v).text})`);
  await click(v, "place-down-t-083/design"); await settle();
  check(JSON.stringify(pinsOf(g.__files[path])) === JSON.stringify([{ id: "t-083/design", slot: 1 }]), `down: its pin replaced (got ${g.__files[path]})`);
  await click(v, "place-up-t-083/design"); await settle();
  check(JSON.stringify(pinsOf(g.__files[path])) === JSON.stringify([{ id: "t-083/design", slot: 0 }]), `after down and up (got ${g.__files[path]})`);
  check(JSON.parse(g.__files[path]).moves.length === 3, "three moves recorded");
  const n = v.unsent.filter((u) => u.message === "start order changed").length;
  check(n === 1, `one notice for three moves (got ${JSON.stringify(v.unsent)})`);
};

cases["P83-4-stale"] = async () => { // AC6.1: a file changed since the read is refused, nothing written, the view reloads
  const { v, dir } = await placesWorld({ order: JSON.stringify({ version: 1, order: [] }) });
  const path = `${dir}/start-order.json`, other = JSON.stringify({ version: 1, order: ["t-020/verify-2"], by: "person" });
  g.__files[path] = other;
  const before = g.__reads.filter((r) => r.endsWith("start-order.json")).length;
  await click(v, "place-top-t-083/design"); await settle();
  check(g.__files[path] === other, `nothing written (got ${g.__files[path]})`);
  check(/changed on disk.*reloaded, try again/.test(look(v).text), `the refusal is said (got ${look(v).text.slice(0, 800)})`);
  check(g.__reads.filter((r) => r.endsWith("start-order.json")).length > before, "the view reloads");
  check(!v.unsent.some((u) => u.message === "start order changed"), "no notice for a refused write");
};

cases["P83-5-remote"] = async () => { // AC6.2: on another machine: read there, written there by the python compare-and-write
  const { v } = await placesWorld({ machine: M });
  check(g.__issued.some((x) => x.on === M && x.kind === "read" && x.path === `${RDIR}/waiting.json`) && g.__issued.some((x) => x.on === M && x.kind === "read" && x.path === `${RDIR}/start-order.json`), "both read on the space's machine");
  check(/Working · 3 of 4/.test(look(v).text), `holders through the machine's terminals (got ${look(v).text.slice(0, 600)})`);
  await click(v, "place-top-t-083/design"); await settle();
  const k = `${M}:${RDIR}/start-order.json`;
  check(g.__writes.includes(k) && JSON.stringify(JSON.parse(g.__files[k]).pins) === JSON.stringify([{ id: "t-083/design", slot: 0 }]), `written on the machine: one pin (t-097 R10) (got ${JSON.stringify(g.__writes)})`);
};

cases["P83-6-busy"] = async () => { // AC9.3: while a write is in flight a click does nothing
  const { v, dir } = await placesWorld();
  v.placesBusy[S] = true;
  await click(v, "place-top-t-083/design"); await settle();
  check(g.__files[`${dir}/start-order.json`] === undefined && !g.__writes.some((w) => w.endsWith("start-order.json")), "nothing written");
};

cases["P83-7-waiting-missing"] = async () => { // AC9.2: no waiting list (a worker holds a place): its line; invalid: its error, no buttons
  let { v } = await placesWorld({ waiting: null });
  check(look(v).text.includes("No waiting list yet: the Project Agent writes it with pa-places."), `missing (got ${look(v).text.slice(0, 800)})`);
  ({ v } = await placesWorld({ waiting: "{x" }));
  const { text, clicks } = look(v);
  check(/waiting\.json is not valid JSON/.test(text) && !Object.keys(clicks).some((k) => k.startsWith("place-")), `invalid (got ${text.slice(0, 800)})`);
};

cases["P83-8-hidden"] = async () => { // D3, AC11.1: no waiting list and no holder: no section, no badges
  const { v } = await placesWorld({ waiting: null, terms: [] });
  const { text, clicks } = look(v);
  check(!/Places ·/.test(text) && !Object.keys(clicks).some((k) => k.startsWith("badge-")), `nothing drawn (got ${text.slice(0, 600)})`);
};

cases["P83-9-unreadable"] = async () => { // AC8.2: an unreadable order: said, the PA's order used, Reset order writes over that text
  const { v, dir } = await placesWorld({ order: "{bad" });
  const { text } = look(v);
  check(text.includes("start order unreadable: start-order.json is not JSON; the Project Agent's order is used"), `the line (got ${text.slice(0, 900)})`);
  await click(v, "places-reset"); await settle();
  const doc = JSON.parse(g.__files[`${dir}/start-order.json`]);
  check(doc.version === 2 && JSON.stringify(doc.pins) === "[]" && doc.moves.at(-1).move === "reset", `t-097 R10: reset writes no pins (got ${JSON.stringify(doc)})`);
};

cases["P83-10-limits"] = async () => { // AC2.3, AC2.4, AC1.3, AC1.4: defaults, a bad value named, unrecorded and over the limit
  const ledger = { tasks: [{ id: "t-1", title: "One", state: "in-progress", workers: ["a1", "a2", "a3", "a4", "a5"].map((x) => P83.worker(x, `t-1-${x}`, "build", x === "a5" ? {} : { place: "working" })) }] };
  let { v } = await placesWorld({ ledger, terms: ["a1", "a2", "a3", "a4", "a5"], rules: JSON.stringify({ version: 1, rules: [{ id: "r-x", text: "x", status: "active", order: 9, places: { gpu: 2 } }] }) });
  const { text } = look(v);
  check(text.includes("Working · 5 of 4 (default)") && !text.includes("No-test extra") && !text.includes("Suite step"), `AC2.4, AC1.4 (got ${text.slice(0, 900)})`);
  check(text.includes("r-x: places.gpu is not a known kind; ignored"), "AC2.3 the note");
  check(text.includes("a5 · t-1 a5 · build · kind not recorded · since"), `AC1.3 (got ${text.slice(0, 900)})`);
  check(text.includes("One"), "the tab is still drawn");
};

cases["P133-places-hung"] = async () => { // t-083 J2 RL2: both places reads hang on the second load: the last known Waiting view stays
  const { v, dir } = await placesWorld({ order: JSON.stringify({ version: 1, order: [] }) });
  let { text, clicks } = look(v);
  check(text.includes("Waiting · 3") && Boolean(clicks["place-down-t-020/verify-2"]), `first load (got ${text.slice(0, 600)})`);
  g.__hang = [`${dir}/waiting.json`, `${dir}/start-order.json`];
  advance(3000); v.reload(cx); await settle(); await step(11000);
  ({ text, clicks } = look(v));
  check(text.includes("Waiting · 3") && text.includes("1. t-020 verify-2") && text.includes("3. t-083 design"), `the Waiting lines stay (got ${text.slice(0, 900)})`);
  check(Boolean(clicks["place-down-t-020/verify-2"]) && Boolean(clicks["place-up-t-083/design"]), "the buttons stay");
  check(!text.includes("No waiting list yet") && !text.includes("Loading waiting list"), `never missing or loading again (got ${text.slice(0, 900)})`);
  g.__hang = [];
};

cases["P83-11-badges"] = async () => { // R11: badges on task rows; a click unfolds Places and marks the line for 3 s
  const { v } = await placesWorld();
  const { text, clicks } = look(v);
  check(text.includes("running · working") && text.includes("running · suite step") && text.includes("next #1 · suite step") && text.includes("next #3 · no-test"), `badges (got ${text})`);
  check(!Object.keys(clicks).some((k) => k.startsWith("badge-t-090")), "AC11.1: no badge without a job");
  await click(v, "places-head"); // fold the section
  check(!look(v).text.includes("Working · 3 of 4"), "folded");
  const id = Object.keys(look(v).clicks).find((k) => k.startsWith("badge-t-083"));
  check(Boolean(id), "a badge for t-083");
  await click(v, id);
  check(look(v).text.includes("Working · 3 of 4") && v.placeMark?.key === "job:t-083/design", `unfolded and marked (got ${JSON.stringify(v.placeMark)})`);
  advance(3500); await fire(1000);
  check(!v.placeMark, "the mark ends after 3 s");
};

// ---- t-097 J2: the person's task order and the category moves in the Tasks tab ----
// The shared cases files, read when a case runs (test_panel_shards copies this file without tests/samples and only lists it).
const sample = (name) => JSON.parse(fs.readFileSync(path.join(here, "..", "samples", name), "utf8"));
const tq = (id, state, n, extra = {}) => ({ id, title: `Task ${id}`, state, created: `2026-11-${String(n).padStart(2, "0")}T00:00:00Z`, ...extra });
const tOrder = (order, last = [], updated = "U1") => JSON.stringify({ version: 1, updated, by: "person", order, last, moves: [] });
// A world with the ledger's tasks (and its taskOrder), task-order.json's text (undefined: no file), open cards and the Tasks view open.
async function orderWorld({ tasks, taskOrder, order, machine = null, cards = [], waiting, startOrder, queue, terms = [], unfold = [], hang = [], execHang = [] } = {}) {
  world({ machine, extraTerms: terms.map((a) => plTerm(machine ? `${machine}/${a}` : a)) });
  const dir = machine ? `${machine}:${RDIR}` : `:${DIR}`;
  g.__files[`${dir}/ledger.json`] = JSON.stringify({ tasks, ...(taskOrder !== undefined ? { taskOrder } : {}) });
  if (order !== undefined) g.__files[`${dir}/task-order.json`] = order;
  if (waiting !== undefined) g.__files[`${dir}/waiting.json`] = JSON.stringify(waiting);
  if (startOrder !== undefined) g.__files[`${dir}/start-order.json`] = startOrder;
  if (queue !== undefined) g.__files[`${dir}/queue.json`] = JSON.stringify(queue);
  if (cards.length) { g.__ls[`${dir}/decisions`] = cards.map((c) => `${c.id}.json`); for (const c of cards) g.__files[`${dir}/decisions/${c.id}.json`] = JSON.stringify({ question: "Q?", options: [{ label: "A" }], created: "2026-11-16T10:00:00Z", status: "open", ...c }); }
  g.__hang = hang.map((f) => `${dir}/${f}`); g.__execHang = execHang; // t-120: reads that do not answer on the first load
  const v = await mount(); await settle();
  await click(v, "view-tasks"); await settle();
  for (const s of unfold) v.folded.delete(s);
  return { v, dir, path: `${dir}/task-order.json` };
}
const heads = (v) => Object.keys(look(v).clicks).filter((k) => k.startsWith("task-head-")).map((k) => k.slice(10));
const paTask = (action) => (g.__execs ?? []).filter(([c, a]) => c === "pa-task" && (!action || a[0] === action));

cases["P97-1-shared-rank"] = async () => { // R3, R5: every shared rank case drawn: each ranked section in its ranking's order
  for (const c of sample("task-order-cases.json").rankCases) {
    const tasks = c.tasks.map((t) => ({ title: `Task ${t.id}`, ...t }));
    const { v } = await orderWorld({ tasks, taskOrder: c.taskOrder ?? undefined, order: c.fileText ?? undefined, unfold: ["parked"] });
    const shown = heads(v);
    for (const state of ["needs-decision", "review", "in-progress", "investigating", "queued"]) {
      const want = c.expect.map(([id]) => id).filter((id) => tasks.find((t) => t.id === id)?.state === state);
      const got = shown.filter((id) => tasks.find((t) => t.id === id)?.state === state);
      check(JSON.stringify(got) === JSON.stringify(want), `${c.name}: ${state} ${JSON.stringify(want)} (got ${JSON.stringify(got)})`);
    }
  }
};

cases["P97-2-line-markers"] = async () => { // R2, R13, R15: the order line, the per-section numbers, the PA marker, the chat quote
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2), tq("t-3", "queued", 3), tq("t-4", "in-progress", 4)];
  let { v } = await orderWorld({ tasks });
  let text = look(v).text;
  check(text.includes("order: the Project Agent's") && (text.match(/ \| PA \| /g) ?? []).length === 4, `no file: every ranked row is PA (got ${text})`);
  check(/ \| 1 \| t-1 \| /.test(text) && / \| 3 \| t-3 \| /.test(text) && / \| 1 \| t-4 \| /.test(text), `R15 numbers per section (got ${text})`);
  ({ v } = await orderWorld({ tasks, order: tOrder(["t-2"]), taskOrder: { order: [], chat: [{ task: "t-3", quote: "do t-3 now", over: "U1" }] } }));
  text = look(v).text;
  check(text.includes("order: yours · moves are saved to task-order.json and tell the Project Agent"), `readable: yours (got ${text})`);
  check(/ \| 1 \| t-3 \| /.test(text) && / \| 2 \| t-2 \| /.test(text) && text.includes("PA · your words") && (text.match(/ \| PA \| /g) ?? []).length === 2, `chat first, the person's unmarked (got ${text})`);
  await click(v, "task-head-t-3");
  check(look(v).text.includes('You asked in chat: "do t-3 now"'), `the quote when expanded (got ${look(v).text})`);
};

cases["P97-3-move"] = async () => { // R7, R8: a move writes task-order.json by compare-and-write; drawn in the new order; one notice
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2), tq("t-3", "queued", 3), tq("t-9", "done", 9)];
  const { v, path } = await orderWorld({ tasks });
  const c1 = (await hoverTask(v, "t-1")).clicks, c3 = (await hoverTask(v, "t-3")).clicks, c = { ...Object.fromEntries(Object.entries(c1).filter(([k]) => k.endsWith("-t-1"))), ...c3 };
  check(!c["tmove-up-t-1"] && !c["tmove-top-t-1"] && c["tmove-down-t-1"] && c["tmove-bottom-t-1"] && c["tmove-up-t-3"] && c["tmove-top-t-3"] && !c["tmove-down-t-3"] && !c["tmove-bottom-t-3"], `buttons only where a move does something (have ${Object.keys(c).filter((k) => k.startsWith("tmove"))})`);
  await click(v, "tmove-top-t-3"); await settle();
  const doc = JSON.parse(g.__files[path]);
  check(doc.version === 1 && doc.by === "person" && JSON.stringify(doc.order) === '["t-3"]' && JSON.stringify(doc.last) === "[]" && doc.moves.length === 1 && doc.moves[0].task === "t-3" && doc.moves[0].move === "top" && doc.moves[0].section === "queued", `the file (got ${g.__files[path]})`);
  check(JSON.stringify(heads(v).slice(0, 3)) === '["t-3","t-1","t-2"]', `drawn in the new order (got ${JSON.stringify(heads(v))})`);
  await click(v, "tmove-bottom-t-1"); await settle();
  const d2 = JSON.parse(g.__files[path]);
  check(JSON.stringify(d2.order) === '["t-3"]' && JSON.stringify(d2.last) === '["t-1"]' && d2.moves.length === 2, `bottom: to last (got ${g.__files[path]})`);
  check(JSON.stringify(heads(v).slice(0, 3)) === '["t-3","t-2","t-1"]', `drawn (got ${JSON.stringify(heads(v))})`);
  check(v.unsent.filter((u) => u.message === "task order changed").length === 1, `one notice for two moves (got ${JSON.stringify(v.unsent)})`);
  check(paTask().length === 0, "a move runs no pa-task");
};

cases["P97-4-stale-busy"] = async () => { // R8: a file changed since the read is refused and said; nothing while a write is in flight
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2)];
  let { v, path } = await orderWorld({ tasks, order: tOrder([]) });
  const other = tOrder(["t-2"], [], "U9");
  g.__files[path] = other;
  await click(v, "tmove-up-t-2"); await settle();
  check(g.__files[path] === other && /changed on disk/.test(look(v).text), `refused and said (got ${look(v).text.slice(0, 900)})`);
  check(!v.unsent.some((u) => u.message === "task order changed"), "no notice for a refused write");
  ({ v, path } = await orderWorld({ tasks }));
  v.taskOrderBusy[S] = true;
  await click(v, "tmove-up-t-2"); await settle();
  check(g.__files[path] === undefined, "busy: nothing written");
};

cases["P97-5-remote"] = async () => { // R1: read and written on the space's machine
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2)];
  const { v, path } = await orderWorld({ tasks, machine: M });
  check(g.__issued.some((x) => x.on === M && x.kind === "read" && x.path === `${RDIR}/task-order.json`), "read on the machine");
  await click(v, "tmove-up-t-2"); await settle();
  check(g.__writes.includes(path) && JSON.stringify(JSON.parse(g.__files[path]).order) === '["t-2","t-1"]', `written there (got ${g.__files[path]})`);
};

cases["P97-6-unreadable"] = async () => { // R2: the line, the Project Agent's order, Reset order writes over that text
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2)];
  const { v, path } = await orderWorld({ tasks, order: "{bad" });
  const text = look(v).text;
  check(text.includes("task order unreadable: task-order.json is not JSON; the Project Agent's order is used") && (text.match(/ \| PA \| /g) ?? []).length === 2, `the line (got ${text})`);
  await click(v, "task-order-reset"); await settle();
  const doc = JSON.parse(g.__files[path]);
  check(doc.version === 1 && JSON.stringify(doc.order) === "[]" && JSON.stringify(doc.last) === "[]" && doc.moves.at(-1).move === "reset", `reset (got ${g.__files[path]})`);
  check(look(v).text.includes("order: yours"), "readable now");
};

cases["P97-7-start-next"] = async () => { // R18: the rank first (to the very top), then pa-task next; a refused write runs nothing
  const tasks = [tq("t-1", "in-progress", 1), tq("t-2", "queued", 2), tq("t-3", "queued", 3, { after: ["t-1"] })];
  let { v, path } = await orderWorld({ tasks });
  check(look(v).text.includes("waits for t-1 (after)"), `the row says what it waits for (got ${look(v).text})`);
  await hoverTask(v, "t-3");
  check(Boolean(look(v).clicks["next-t-3"]) && !look(v).clicks["next-t-1"] && Boolean(look(v).clicks["pause-t-1"]) && !look(v).clicks["pause-t-3"], "Start next on Queued, Pause on In progress");
  await click(v, "next-t-3"); await settle();
  check(JSON.stringify(JSON.parse(g.__files[path]).order) === '["t-3"]' && JSON.parse(g.__files[path]).moves.at(-1).move === "next", `the rank (got ${g.__files[path]})`);
  const runs = paTask("next");
  check(runs.length === 1 && JSON.stringify(runs[0][1]) === '["next","t-3","--space","sp1"]', `pa-task next (got ${JSON.stringify(g.__execs)})`);
  ({ v, path } = await orderWorld({ tasks, order: tOrder([]) }));
  g.__files[path] = tOrder(["t-2"], [], "U9");
  await click(v, "next-t-3"); await settle();
  check(paTask().length === 0 && /changed on disk/.test(look(v).text), `refused: nothing runs (got ${JSON.stringify(g.__execs)})`);
};

cases["P97-8-pause"] = async () => { // R19: Pause runs pa-task pause, the rank unchanged; a paused row: the chip, Start next and Unpause
  const tasks = [tq("t-1", "in-progress", 1), tq("t-4", "investigating", 4), tq("t-2", "queued", 2, { paused: { at: "2026-11-19T10:00:00Z", by: "person" } }), tq("t-3", "review", 3)];
  const { v, path } = await orderWorld({ tasks });
  check(Boolean(look(v).clicks["pause-t-4"]) && !look(v).clicks["pause-t-3"] && !look(v).clicks["next-t-3"], "Pause on investigating; review: neither (R21)");
  await click(v, "pause-t-1"); await settle();
  check(paTask("pause").length === 1 && paTask("pause")[0][1][1] === "t-1" && g.__files[path] === undefined, `pa-task pause, no order write (got ${JSON.stringify(g.__execs)})`);
  await hoverTask(v, "t-2");
  check(look(v).text.includes(" | paused | ") && Boolean(look(v).clicks["unpause-t-2"]) && Boolean(look(v).clicks["next-t-2"]), `the paused row (got ${look(v).text})`);
  await click(v, "unpause-t-2"); await settle();
  check(paTask("unpause").length === 1 && paTask("unpause")[0][1][1] === "t-2", "pa-task unpause");
};

cases["P97-9-open-card"] = async () => { // R17: a needs-decision task with an open card: no moves out, the line; Delete stays
  const tasks = [tq("t-1", "needs-decision", 1), tq("t-2", "needs-decision", 2)];
  const { v } = await orderWorld({ tasks, cards: [{ id: "d-231", task: "t-1" }, { id: "d-232", task: "t-2", status: "answered", answer: "A" }] });
  await click(v, "task-head-t-1"); await click(v, "task-head-t-2");
  const { text, clicks } = look(v);
  check(text.includes("Answer d-231 first, or ask the Project Agent to withdraw it."), `the line (got ${text})`);
  check(!clicks["done-t-1"] && !clicks["park-t-1"] && !clicks["pause-t-1"] && !clicks["next-t-1"] && Boolean(clicks["delete-t-1"]), `no move out, Delete stays (have ${Object.keys(clicks)})`);
  check(Boolean(clicks["done-t-2"]) && Boolean(clicks["park-t-2"]), "an answered card: Park and Mark done as usual");
  check(!Object.keys(clicks).some((k) => /^tmove-.*t-[12]$/.test(k)) && !/ \| 1 \| t-1 \| /.test(text), "hidden rank: no number, no moves");
};

cases["P97-10-done-confirm"] = async () => { // R20: Mark done takes two clicks; the first says what stays
  const tasks = [tq("t-1", "in-progress", 1, { changeRequest: { id: "CR-123" }, worktree: "/wt/t-1-a", worktrees: [{ path: "/wt/t-1-b", job: "b" }], workers: [{ address: "t14", role: "build", name: "t-1-build" }] }), tq("t-2", "queued", 2)];
  const queue = { version: 1, repos: { "/r": { main: "main", entries: [{ task: "t-1", branch: "t-1-a", state: "checking" }] } } };
  const { v } = await orderWorld({ tasks, queue, terms: ["t14"] });
  await click(v, "task-head-t-1");
  await click(v, "done-t-1"); await settle();
  check(paTask("done").length === 0, "one click runs nothing");
  const text = look(v).text;
  check(text.includes("Done merges nothing: change request CR-123 stays unmerged · 2 worktrees stay · 1 merge-queue entry stays · t14 keeps running until you close it."), `the line (got ${text})`);
  await click(v, "done-cancel-t-1");
  check(!look(v).text.includes("Done merges nothing") && paTask("done").length === 0, "Cancel");
  await click(v, "done-t-1"); await click(v, "done-confirm-t-1"); await settle();
  check(paTask("done").length === 1 && paTask("done")[0][1][1] === "t-1", `confirmed: pa-task done (got ${JSON.stringify(g.__execs)})`);
  await click(v, "task-head-t-2"); await click(v, "done-t-2");
  check(look(v).text.includes("Done merges nothing: nothing is left open."), "nothing open");
};

cases["P97-11-fold"] = async () => { // R14: the follow-ups of one origin fold into one line at the first member's place
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2, { followUpOf: "t-8" }), tq("t-3", "queued", 3, { followUpOf: "t-8" }), tq("t-4", "queued", 4, { followUpOf: "t-8" }), tq("t-5", "queued", 5)];
  const { v } = await orderWorld({ tasks });
  let text = look(v).text;
  check(text.includes("▸ 3 follow-ups of t-8 · first #2") && JSON.stringify(heads(v)) === '["t-1","t-5"]', `folded (got ${text}; ${JSON.stringify(heads(v))})`);
  await click(v, "fold-queued-t-8");
  text = look(v).text; await hoverTask(v, "t-3");
  check(text.includes("▾ 3 follow-ups of t-8 · first #2") && JSON.stringify(heads(v)) === '["t-1","t-2","t-3","t-4","t-5"]' && Boolean(look(v).clicks["tmove-up-t-3"]), `unfolded: normal rows (got ${JSON.stringify(heads(v))})`);
};

cases["P97-12-places-rank"] = async () => { // R9, R27: the waiting list follows the task order only once one exists
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2)];
  const waiting = { version: 1, jobs: [P83.job("t-2/build", "working"), P83.job("t-1/build", "working")] };
  let { v } = await orderWorld({ tasks, waiting });
  let text = look(v).text;
  check(text.indexOf("1. t-2 build") >= 0 && text.indexOf("2. t-1 build") >= 0, `no task order: the added order (got ${text})`);
  ({ v } = await orderWorld({ tasks, waiting, order: tOrder([]) }));
  text = look(v).text;
  check(text.indexOf("1. t-1 build") >= 0 && text.indexOf("2. t-2 build") >= 0, `a task order: by rank (got ${text})`);
  ({ v } = await orderWorld({ tasks, waiting, taskOrder: { order: ["t-2"], chat: [] } }));
  check(look(v).text.indexOf("1. t-2 build") >= 0, "the ledger's taskOrder counts");
  ({ v } = await orderWorld({ tasks, waiting: { version: 1, jobs: [P83.job("t-1/build", "working"), P83.job("t-2/build", "working")] } }));
  check(look(v).text.indexOf("1. t-1 build") >= 0, "before the move");
  await click(v, "tmove-up-t-2"); await settle();
  text = look(v).text;
  check(text.indexOf("1. t-2 build") >= 0, `a task move moves its jobs (got ${text})`);
};

cases["P97-13-shared-places"] = async () => { // R9, R10: the shared rank and pin cases drawn in the Places list
  const label = (id) => { const [task, job] = id.split("/"); return `${task} ${job}`; };
  const PSH = sample("places-cases.json");
  for (const c of [...PSH.rankCases, ...PSH.pinCases]) {
    const jobs = c.jobs.map((j) => { const x = typeof j === "string" ? { id: j } : j; return P83.job(x.id, "working", { source: x.source ?? "pa" }); });
    const tasks = c.rank.map((id, i) => tq(id, "queued", i + 1));
    const { v } = await orderWorld({ tasks, waiting: { version: 1, jobs }, order: tOrder(c.rank), startOrder: c.orderText ?? undefined });
    const text = look(v).text;
    const at = c.effective.map((id, i) => text.indexOf(`${i + 1}. ${label(id)} `));
    check(at.every((x) => x >= 0), `${c.name}: ${JSON.stringify(c.effective)} (got ${text.slice(text.indexOf("Waiting ·"), text.indexOf("Waiting ·") + 400)})`);
  }
};

// ---- t-120: t-097 J2's Recorded limits ----
cases["P120-1-prune"] = async () => { // t-097 VERIFY J2 RL-1 (R4): a task-order write drops done and gone ids from order and last
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2), tq("t-9", "done", 9)];
  const { v, path } = await orderWorld({ tasks, order: tOrder(["t-9", "t-1", "t-77"], ["t-9", "t-77"]) });
  await click(v, "tmove-up-t-2"); await settle();
  const doc = JSON.parse(g.__files[path]);
  check(JSON.stringify(doc.order) === '["t-2","t-1"]' && JSON.stringify(doc.last) === "[]", `pruned (got ${g.__files[path]})`);
};

cases["P120-2-next-fails"] = async () => { // t-097 VERIFY J2 RL-2 (R18): (b) failing after (a): the rank stays written, the row says why
  const tasks = [tq("t-1", "in-progress", 1), tq("t-2", "queued", 2), tq("t-3", "queued", 3)];
  const { v, path } = await orderWorld({ tasks });
  g.__execFn = (c, a) => (c === "pa-task" && a[0] === "next" ? { code: 1, stdout: "", stderr: "pa-task: t-3 is waiting on decision d-9." } : undefined);
  await click(v, "task-head-t-3");
  await click(v, "next-t-3"); await settle();
  g.__execFn = null;
  check(paTask("next").length === 1, `pa-task next ran once (got ${JSON.stringify(g.__execs)})`);
  check(JSON.stringify(JSON.parse(g.__files[path]).order) === '["t-3"]', `the rank stays (got ${g.__files[path]})`);
  check(look(v).text.includes("pa-task: t-3 is waiting on decision d-9."), `the row shows the error (got ${look(v).text})`);
};

cases["P120-3-places-rank-unknown"] = async () => { // t-097 REVIEW J2 RL-2 (R9): no Places move while the task order is not known
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2)];
  const waiting = { version: 1, jobs: [P83.job("t-2/build", "working"), P83.job("t-1/build", "working")] };
  const { v } = await orderWorld({ tasks, waiting, order: tOrder(["t-1"]), hang: ["task-order.json"] });
  await step(11000); await step(11000);
  let { text, clicks } = look(v);
  check(text.includes("Waiting · 2"), `the waiting list is drawn (got ${text.slice(0, 900)})`);
  check(!Object.keys(clicks).some((k) => k.startsWith("place-")), `no Places moves (have ${Object.keys(clicks).filter((k) => k.startsWith("place-"))})`);
  check(text.includes("moves wait for the task order to be read"), `the line says why (got ${text.slice(0, 1200)})`);
  g.__hang = [];
  advance(3000); v.reload(cx); await settle(); await step(1000);
  ({ text, clicks } = look(v));
  check(Boolean(clicks["place-down-t-1/build"]) && text.indexOf("1. t-1 build") >= 0, `read: by rank, the moves back (got ${text.slice(0, 900)})`);
};

cases["P120-4-cards-not-read"] = async () => { // t-097 REVIEW J2 RL-3 (R17): no Park or Mark done on a needs-decision task until its cards are read
  const tasks = [tq("t-1", "needs-decision", 1), tq("t-2", "queued", 2)];
  const { v } = await orderWorld({ tasks, cards: [{ id: "d-231", task: "t-1" }], execHang: [":batch"], hang: ["decisions/d-231.json"] });
  await click(v, "task-head-t-1"); await click(v, "task-head-t-2");
  let { text, clicks } = look(v);
  check(v.cardsFor !== S, "the cards are not read yet");
  check(!clicks["done-t-1"] && !clicks["park-t-1"] && Boolean(clicks["delete-t-1"]), `no move out yet, Delete stays (have ${Object.keys(clicks)})`);
  check(text.includes("Checking for open decisions…"), `the line (got ${text})`);
  check(Boolean(clicks["done-t-2"]) && Boolean(clicks["park-t-2"]), "a queued task is not held back");
  g.__execHang = []; g.__hang = [];
  advance(3000); v.reload(cx); await settle(); await step(1000);
  ({ text, clicks } = look(v));
  check(v.cardsFor === S && text.includes("Answer d-231 first") && !clicks["done-t-1"] && !text.includes("Checking for open decisions…"), `read: as P97-9 (got ${text})`);
};

cases["P120-5-reopened"] = async () => { // t-097 VERIFY J3 rebase RL-R1 (R5): a reopened done task starts at the bottom of Queued and a move does not bring it back
  const tasks = [tq("t-1", "queued", 1, { reopened: "2026-11-19T10:00:00Z" }), tq("t-2", "queued", 2), tq("t-3", "queued", 3), tq("t-4", "queued", 4)];
  const { v, path } = await orderWorld({ tasks, order: tOrder(["t-1", "t-2"], [], "2026-11-19T09:00:00Z") });
  check(JSON.stringify(heads(v)) === '["t-2","t-3","t-4","t-1"]', `at the bottom (got ${JSON.stringify(heads(v))})`);
  await click(v, "tmove-down-t-2"); await settle();
  check(!JSON.parse(g.__files[path]).order.includes("t-1"), `its old place is not written back (got ${g.__files[path]})`);
  check(JSON.stringify(heads(v)) === '["t-3","t-2","t-4","t-1"]', `still at the bottom (got ${JSON.stringify(heads(v))})`);
};

// ---- t-041 J5: the context line (R15), the Fresh context button (R6), the mode switch (R13), the reset line (C6) ----
// The button runs only the context tool on the space's machine: a dry run for the confirm, then one request (or a
// withdraw) with --by panel. The switch runs its `set` action. The panel writes neither file itself.
const CTX = ["pa", "context"].join("-"); // spelled so a guard reading commands is not confused
const DAY41 = new Date(T0).toISOString().slice(0, 10);
const SUMMARY41 = { updated: `${DAY41}T07:59:00Z`, paContext: [{ day: DAY41, calls: 40, mean: 158000, peak: 254000, peakExact: true, resets: { fresh: 6, compact: 0 } }] };
const DRY41 = (over = {}) => ({ code: 0, stdout: `mode fresh  taskCap 150,000  passCap 250,000\ncontext: 260k (transcript)\nDry run: nothing written.\n${JSON.stringify({ outcome: "dry-run", ctx: 260000, source: "transcript", due: "pass-cap", request: null, expires: null, reasons: [], ...over })}\n`, stderr: "" });
const ASKED41 = { code: 0, stdout: `Requested (rq-1): a fresh context at the Project Agent's next clean point.\n${JSON.stringify({ outcome: "requested", request: "rq-1", expires: `${DAY41}T10:00:00Z` })}\n`, stderr: "" };
const SET41 = (mode) => ({ code: 0, stdout: `mode ${mode}\n${JSON.stringify({ v: 1, mode, taskCap: 150000, passCap: 250000, net: 400000, enabledAt: null, by: "panel" })}\n`, stderr: "" });
// liveWorld with a launcher that takes context requests, agent.json's sessionId, context.json and the summary (read
// only by the Costs view: J5-D1).
// fresh(args, on): the context tool's answer (default: the dry run, a request, or the set's settings).
function ctxWorld({ machine = null, launcher = LREC({ accepts: ["restart", "context"] }), context, summary = SUMMARY41, sessionId = "s1", status = "idle", fresh = null } = {}) {
  const w = liveWorld({ launcher, machine, status });
  const dir = machine ? `${machine}:${RDIR}` : `:${DIR}`;
  g.__files[`${dir}/agent.json`] = JSON.stringify({ space: S, project: "/proj", profile: "test", name: "proj", terminal: "t4w", sessionId });
  if (context !== undefined) g.__files[`${dir}/context.json`] = typeof context === "string" ? context : JSON.stringify(context);
  if (summary) g.__files[`${dir}/costs/summary.json`] = JSON.stringify(summary);
  const restartFn = g.__execFn;
  g.__execFn = (command, args, on, options) => (command === CTX
    ? (fresh ? fresh(args, on) : args.includes("--dry-run") ? DRY41() : args[0] === "set" ? SET41(args[args.indexOf("--mode") + 1]) : ASKED41)
    : restartFn(command, args, on, options));
  return { ...w, dir };
}
const ctxRuns = () => g.__execs.filter(([c]) => c === CTX);
const ctxWrites = () => ctxRuns().filter(([, a]) => !a.includes("--dry-run"));
const summaryReads = (dir) => g.__reads.filter((r) => r === `${dir}/costs/summary.json`);

cases["P41-1-context-line"] = async () => { // R15, J5-D1: the line from the Costs view's own read (as of its time), in the Costs tab (t-145); the switch
  const { dir } = ctxWorld();
  const v = await liveMount();
  const { text, clicks } = look(v);
  check(!text.includes("PA context"), `no context line outside the Costs tab (got ${text.slice(0, 900)})`);
  check(summaryReads(dir).length === 0, `no summary.json read outside the Costs view (got ${summaryReads(dir).length})`);
  check(Boolean(clicks["pa-context-mode"]) && /Fresh contexts: on/.test(P145.tip(v, "pa-context-mode")), "the switch, on");
  check(Boolean(clicks["pa-restart"]), "Restart still there");
  check(ctxRuns().length === 0, "nothing run without a click");
  await click(v, "view-costs"); await settle();
  check(summaryReads(dir).length === 1, `the Costs view's one read (got ${summaryReads(dir).length})`);
  check(look(v).text.includes("PA context today (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0) (as of 07:59)"), `the line, as of the summary's updated (MF-J5r2-1) (got ${look(v).text.slice(0, 900)})`);
};

cases["P41-2-no-summary"] = async () => { // R15, J5-D1: no summary: no line once the Costs view has read; the rest stays
  ctxWorld({ summary: null });
  const v = await liveMount();
  await click(v, "view-costs"); await settle();
  const { text, clicks } = look(v);
  check(!text.includes("PA context today") && !text.includes("open the Costs tab"), `no line (got ${text.slice(0, 600)})`);
  check(Boolean(clicks["pa-context-mode"]) && Boolean(clicks["pa-restart"]), `switch, Restart (got ${Object.keys(clicks)})`);
};

cases["P41-3-button-when"] = async () => { // R13 (t-145): the switch while not offline, whatever the launcher accepts; its state in its tooltip
  ctxWorld({ launcher: LREC({ accepts: ["restart"] }) });
  let v = await liveMount();
  check(Boolean(look(v).clicks["pa-context-mode"]) && !look(v).clicks["pa-fresh"], "accepts restart only: the switch, no force control");
  ctxWorld({ context: { v: 1, mode: "off", taskCap: 150000, passCap: 250000, net: 400000, enabledAt: null, by: "person" } });
  v = await liveMount();
  check(/Fresh contexts: off/.test(P145.tip(v, "pa-context-mode")) && /turn them on/.test(P145.tip(v, "pa-context-mode")), `mode off (got ${P145.tip(v, "pa-context-mode")})`);
  world(); // not running
  v = await liveMount();
  check(Boolean(look(v).clicks["pa-context-mode"]), "down: the switch stays");
  world({ machine: M }); g.__machines = [{ name: null, layout: "ready" }, { name: M, layout: "offline" }];
  v = await liveMount();
  check(!look(v).clicks["pa-context-mode"], "offline: none");
};

cases["P41-10-mode-switch"] = async () => { // R13: one click each way; its line; the reload shows the new mode
  const { dir } = ctxWorld({ fresh: (args) => {
    if (args[0] !== "set") return args.includes("--dry-run") ? DRY41() : ASKED41;
    const mode = args[args.indexOf("--mode") + 1];
    g.__files[`${dir}/context.json`] = JSON.stringify({ v: 1, mode, taskCap: 150000, passCap: 250000, net: 400000, enabledAt: null, by: "panel" }); // the tool writes it
    return SET41(mode);
  } });
  const v = await liveMount();
  await click(v, "pa-context-mode"); await settle(); await settle();
  check(ctxWrites().length === 1 && JSON.stringify(ctxWrites()[0][1]) === JSON.stringify(["set", "--space", S, "--by", "panel", "--mode", "off", "--json"]), `off argv (got ${JSON.stringify(ctxWrites())})`);
  check(P145.tip(v, "pa-context-mode").includes("Context mode off: no fresh contexts from now; the 400k net ends at its next start. Turn fresh contexts on undoes it."), `its line, in the tooltip (t-145) (got ${P145.tip(v, "pa-context-mode")})`);
  check(/Fresh contexts: off/.test(P145.tip(v, "pa-context-mode")), "after the reload: off");
  await click(v, "pa-context-mode"); await settle(); await settle();
  check(ctxWrites().length === 2 && JSON.stringify(ctxWrites()[1][1]) === JSON.stringify(["set", "--space", S, "--by", "panel", "--mode", "fresh", "--json"]), `fresh argv (got ${JSON.stringify(ctxWrites())})`);
  check(/Fresh contexts: on/.test(P145.tip(v, "pa-context-mode")), "on again");
  check(!g.__writes.some((w) => /context\.json/.test(JSON.stringify(w))), "the panel wrote no context.json itself");
};

cases["P41-11-invalid-settings"] = async () => { // R13: an invalid context.json: its note; the switch still runs the tool
  ctxWorld({ context: "{not json" });
  const v = await liveMount();
  check(/context\.json is not valid \(.*\): the launcher uses the defaults/.test(P145.tip(v, "pa-context-mode")), `the note, in the tooltip (t-145) (got ${P145.tip(v, "pa-context-mode")})`);
  check(/Fresh contexts: on/.test(P145.tip(v, "pa-context-mode")), "the defaults: mode fresh");
  await click(v, "pa-context-mode"); await settle();
  check(ctxWrites().length === 1 && ctxWrites()[0][1][0] === "set", "the switch ran the tool");
};

cases["P41-12-reset-line"] = async () => { // C6: the line while this conversation is the one a reset started; copy /resume
  const lastReset = { at: `${DAY41}T07:02:00Z`, from: "3f2a9c1e-aaaa-bbbb-cccc-dddddddddddd", to: "s1", reason: "pass-cap", ctx: 260000 };
  ctxWorld({ launcher: LREC({ accepts: ["restart", "context"], lastReset }) });
  let v = await liveMount();
  await click(v, "view-costs"); await settle(); // t-145: in the Costs tab
  check(look(v).text.includes("Fresh context at 07:02 (pass cap, 260k) · previous session 3f2a9c1e…"), `the line (got ${look(v).text.slice(0, 900)})`);
  await click(v, "copy-previous-session");
  check((g.__clipboard ?? []).includes(`/resume ${lastReset.from}`), `copied (got ${JSON.stringify(g.__clipboard)})`);
  ctxWorld({ launcher: LREC({ accepts: ["restart", "context"], lastReset }), sessionId: "s2" });
  v = await liveMount();
  await click(v, "view-costs"); await settle();
  check(!look(v).text.includes("Fresh context at 07:02") && !look(v).clicks["copy-previous-session"], "another session: no line");
};

cases["P41-13-no-summary-read"] = async () => { // J5-D1, t-009 R11: no summary.json read outside the Costs view across reloads; context.json each load
  const { dir } = ctxWorld();
  const v = await liveMount();
  const ctxReads = () => g.__reads.filter((r) => r === `${dir}/context.json`).length;
  const c0 = ctxReads();
  check(c0 >= 1, `context.json read at mount (got ${c0})`);
  for (let i = 0; i < 3; i++) { advance(31000); await fire(10000); await settle(); }
  check(ctxReads() > c0, "context.json read again");
  check(summaryReads(dir).length === 0, `no summary.json read (got ${summaryReads(dir).length})`);
  check(!look(v).text.includes("PA context"), `no line outside the Costs tab (t-145) (got ${look(v).text.slice(0, 700)})`);
};

cases["P41-14-remote"] = async () => { // R6, R13: a space on m1: reads and runs there; no --on
  const { dir } = ctxWorld({ machine: M });
  const v = await liveMount();
  check(g.__reads.includes(`${dir}/context.json`) && summaryReads(dir).length === 0, `context.json on ${M}, no summary read (got ${JSON.stringify(g.__reads.filter((r) => /context|summary/.test(r)))})`);
  check(!g.__reads.some((r) => /context\.json|summary\.json/.test(r) && !r.startsWith(`${M}:`)), "nothing local");
  await click(v, "pa-context-mode"); await settle();
  check(ctxRuns().length === 1 && ctxRuns().every(([, a, on]) => on === M && !a.includes("--on")), `all on ${M}, no --on (got ${JSON.stringify(ctxRuns())})`);
};

cases["P41-16-cross-midnight"] = async () => { // MF-J5-1: a read from an earlier UTC day names its day, never "no calls yet"
  ctxWorld({ summary: { ...SUMMARY41, updated: `${DAY41}T23:50:00Z` } }); // written at the read's time (MF-J5r2-1)
  const v = await liveMount();
  g.__now = T0 + (15 * 60 + 50) * 60000; // 23:50 UTC on DAY41
  await click(v, "view-costs"); await settle();
  check(look(v).text.includes("PA context today (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0) (as of 23:50)"), `at read time (got ${look(v).text.slice(0, 900)})`);
  await click(v, "view-tasks"); await settle();
  advance(20 * 60000); await fire(1000); await settle(); // 00:10 UTC the next day; the Costs view is not shown
  v.view = "costs"; // t-145: the line is in the Costs tab; drawn from the read already made
  const text = look(v).text;
  check(!text.includes("no Project Agent calls yet") && !text.includes("PA context today"), `not today's, not "no calls" (got ${text.slice(0, 900)})`);
  check(text.includes(`PA context ${DAY41} (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0) (as of ${DAY41.slice(5)} 23:50)`), `the read's day named (got ${text.slice(0, 900)})`);
};

cases["P41-17-summary-yesterday"] = async () => { // MF-J5r2-1: the summary's own time, not the read's: written DAY41 23:50, read 00:10
  ctxWorld({ summary: { ...SUMMARY41, updated: `${DAY41}T23:50:00Z` } });
  const v = await liveMount();
  g.__now = T0 + (16 * 60 + 10) * 60000; // 00:10 UTC the day after DAY41
  await click(v, "view-costs"); await settle();
  await click(v, "view-tasks"); await settle();
  v.view = "costs"; // t-145: the line is in the Costs tab; drawn from the read already made
  const text = look(v).text;
  check(!text.includes("no Project Agent calls yet") && !text.includes("PA context today"), `not today's, not "no calls" (got ${text.slice(0, 900)})`);
  check(text.includes(`PA context ${DAY41} (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0) (as of ${DAY41.slice(5)} 23:50)`), `the summary's day and time (got ${text.slice(0, 900)})`);
};

cases["P41-18-no-updated"] = async () => { // MF-J5r2-1: no updated, or one that is not a time: the Costs view's read time, as before
  for (const updated of [undefined, "not a time", 1800000000]) {
    const summary = { ...SUMMARY41 }; if (updated === undefined) delete summary.updated; else summary.updated = updated;
    ctxWorld({ summary });
    const v = await liveMount();
    g.__now = T0 + (15 * 60 + 50) * 60000; // 23:50 UTC on DAY41
    await click(v, "view-costs"); await settle();
    check(look(v).text.includes("PA context today (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0) (as of 23:50)"), `${updated}: at read time (got ${look(v).text.slice(0, 900)})`);
    await click(v, "view-tasks"); await settle();
    advance(20 * 60000); await fire(1000); await settle(); // 00:10 UTC the next day
    v.view = "costs"; // t-145: the line is in the Costs tab; drawn from the read already made
    check(look(v).text.includes(`PA context ${DAY41} (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0) (as of ${DAY41.slice(5)} 23:50)`), `${updated}: the read's day (got ${look(v).text.slice(0, 900)})`);
  }
};

// ---- t-119: the suite machines' slots in the Places section (pa-full-suite --places --json) ----
const FSUITE = ["pa", "full", "suite"].join("-"); // spelled so a guard reading this file's commands is not confused
const P119 = {
  holder: (task, worker, extra = {}) => ({ task, worker, pid: 4242, start: "2026-11-19T10:05:00Z", what: "sh tests/run-all.sh", dir: "/wt/x", ...extra }),
  doc: (over = {}) => ({ version: 1, notes: [], machines: [
    { machine: "box-1", cap: 2, reachable: true, reason: null, os: "Linux", arch: "x86_64", seconds: 0.4, holders: [P119.holder("t-020", "t53")] },
    { machine: "box-2", cap: 1, reachable: false, reason: "needs_login", os: null, arch: null, seconds: 0.1, holders: [] },
  ], local: { cap: 1, holders: [P119.holder("t-041", "t77", { what: null })] }, ...over }),
};
const suiteCalls = () => (g.__execs ?? []).filter(([c]) => c === FSUITE);
// An exec stand-in: pa-full-suite answers `reply()` (a doc: printed as JSON; else a reply as it is).
const suiteExec = (reply) => (command, args) => {
  if (command !== FSUITE) return undefined;
  const r = typeof reply === "function" ? reply(args) : reply;
  return r && r.version !== undefined ? { code: 0, stdout: `${JSON.stringify(r)}\n`, stderr: "" } : r;
};

cases["P119-1-lines"] = async () => { // R1, R4: each machine, reachable or why not, its slots and holders; this Mac
  const { v } = await placesWorld({ exec: suiteExec(P119.doc()) });
  const { text } = look(v);
  const calls = suiteCalls();
  check(calls.length === 1 && JSON.stringify(calls[0][1]) === JSON.stringify(["--places", "--json"]) && calls[0][2] === null, `one read here (got ${JSON.stringify(calls)})`);
  check(text.includes("Suite machines"), `the block (got ${text.slice(0, 1200)})`);
  check(text.includes("box-1 · reachable · 1 of 2 slots held"), `box-1 (got ${text.slice(0, 1200)})`);
  check(text.includes("t53 · t-020 verify · verifier · since 11-19 10:05 ↗"), `box-1's holder, a link (t-155: the worker item, no command) (got ${text.slice(0, 1200)})`);
  check(text.includes("box-2 · not reachable (needs_login) · 0 of 1 slots held"), `box-2 (got ${text.slice(0, 1200)})`);
  check(text.includes("this Mac · 1 of 1 slots held") && text.includes("t77 · t-041 · ? · since 11-19 10:05"), `this Mac (t-155: no ledger record, the role ?) (got ${text.slice(0, 1200)})`);
  check(!text.includes("t77 · t-041 · ? · since 11-19 10:05 ↗"), "a worker whose terminal is not live: no link");
  check(!text.includes("sh tests/run-all.sh"), "t-155: no command text");
  const box2 = text.indexOf("box-2 ·"), mac = text.indexOf("this Mac ·"), free = text.indexOf("free", box2);
  check(free > box2 && free < mac, "box-2: free");
  check(text.indexOf("Suite machines") > text.indexOf("Working ·") && text.indexOf("Suite machines") < text.indexOf("Waiting ·"), "after the kinds, before the waiting list");
};

cases["P119-2-link"] = async () => { // R5: a holder opens its worker; its own id; one without a live worker has none
  const { v } = await placesWorld({ exec: suiteExec(P119.doc()) });
  const { clicks } = look(v);
  check(Boolean(clicks["places-slot-box-1-0"]) && !clicks["places-slot-local-0"], `ids (got ${Object.keys(clicks).filter((k) => k.startsWith("places-")).join(", ")})`);
  g.__runs = [];
  await click(v, "places-slot-box-1-0");
  check(g.__runs.some(([n, a]) => n === "terminal.peek" && a?.terminal === "t53"), `opens t53 (got ${JSON.stringify(g.__runs)})`);
  const none = P119.doc({ local: { cap: 1, holders: [P119.holder("t-041", null)] } });
  const w = await placesWorld({ exec: suiteExec(none) });
  check(look(w.v).text.includes("t-041 · no worker recorded · since 11-19 10:05") && !look(w.v).text.includes("sh tests/run-all.sh") && !look(w.v).clicks["places-slot-local-0"], `no worker: plain text, no command (t-155) (got ${look(w.v).text.slice(0, 1200)})`);
};

cases["P119-3-remote"] = async () => { // R2, R5: on the space's machine; the holder through the machine's address
  const { v } = await placesWorld({ machine: M, exec: suiteExec(P119.doc()) });
  const calls = suiteCalls();
  check(calls.length === 1 && calls[0][2] === M, `read on ${M} (got ${JSON.stringify(calls)})`);
  check(look(v).text.includes(`${M} (the space's machine) · 1 of 1 slots held`), `the space's machine's own slot (got ${look(v).text.slice(0, 1200)})`);
  g.__runs = [];
  await click(v, "places-slot-box-1-0");
  check(g.__runs.some(([n, a]) => n === "terminal.peek" && a?.terminal === `${M}/t53`), `opens ${M}/t53 (got ${JSON.stringify(g.__runs)})`);
};

cases["P119-4-throttle"] = async () => { // R3: at most every 60 s, never two at once, never folded or on another view
  const { v } = await placesWorld({ exec: suiteExec(P119.doc()) });
  check(suiteCalls().length === 1, "one read");
  advance(10000); await fire(10000); await settle();
  check(suiteCalls().length === 1, `inside 60 s: none (got ${suiteCalls().length})`);
  advance(51000); await fire(10000); await settle();
  check(suiteCalls().length === 2, `after 60 s: one more (got ${suiteCalls().length})`);
  await click(v, "places-head"); await settle(); // fold
  advance(61000); await fire(10000); await settle();
  check(suiteCalls().length === 2, `folded: none (got ${suiteCalls().length})`);
  await click(v, "places-head"); await settle(); // unfold: read now
  check(suiteCalls().length === 3, `unfolded: one (got ${suiteCalls().length})`);
  await click(v, "view-queue"); await settle();
  advance(61000); await fire(10000); await settle();
  check(suiteCalls().length === 3, `another view: none (got ${suiteCalls().length})`);
  await click(v, "view-tasks"); await settle();
  check(suiteCalls().length === 4, `back on Tasks: one (got ${suiteCalls().length})`);
  g.__execHang = [`:${FSUITE}`];
  advance(61000); await fire(10000); await settle();
  advance(61000); await fire(10000); await settle();
  check(suiteCalls().length === 5, `one still pending: no second (got ${suiteCalls().length})`);
  g.__execHang = []; await settle();
};

cases["P119-5-error"] = async () => { // R2, R4: an error line; a later failure keeps the last answer with the error
  let reply = { code: 2, stdout: "", stderr: "usage: pa-full-suite [-h]\npa-full-suite: error: unrecognized arguments: --json\n" };
  const { v } = await placesWorld({ exec: suiteExec(() => reply) });
  check(look(v).text.includes("Suite machines: could not read them (usage: pa-full-suite [-h])"), `the error (got ${look(v).text.slice(0, 1200)})`);
  reply = P119.doc();
  advance(61000); await fire(10000); await settle();
  check(look(v).text.includes("box-1 · reachable · 1 of 2 slots held") && !look(v).text.includes("could not read them"), `read again (got ${look(v).text.slice(0, 1200)})`);
  reply = { code: 0, stdout: "not json", stderr: "" };
  advance(61000); await fire(10000); await settle();
  const { text } = look(v);
  check(text.includes("box-1 · reachable · 1 of 2 slots held") && /Suite machines: could not read them \(.*not JSON/.test(text), `kept, with the error (got ${text.slice(0, 1200)})`);
};

cases["P119-6-checking"] = async () => { // R4: before the first answer
  world({ extraTerms: ["t51", "t53", "t54"].map(plTerm) });
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify(plLedger()); g.__files[`:${DIR}/rules.json`] = P83.rules;
  g.__execHang = [`:${FSUITE}`];
  const v = await mount(); await settle();
  await click(v, "view-tasks"); await settle();
  check(look(v).text.includes("Suite machines: checking…"), `checking (got ${look(v).text.slice(0, 1200)})`);
  g.__execHang = []; await settle();
};

cases["P119-7-shown"] = async () => { // R6: shown for the machines alone; as today with none and no holder
  const empty = { tasks: [{ id: "t-090", title: "Nothing here", state: "in-progress" }] };
  let w = await placesWorld({ waiting: null, ledger: empty, exec: suiteExec(P119.doc()) });
  check(look(w.v).text.includes("box-1 · reachable · 1 of 2 slots held"), `shown (got ${look(w.v).text.slice(0, 900)})`);
  w = await placesWorld({ waiting: null, ledger: empty, exec: suiteExec(P119.doc({ machines: [], local: { cap: 1, holders: [] } })) });
  check(!look(w.v).text.includes("Places ·") && !look(w.v).text.includes("Suite machines"), `not shown (got ${look(w.v).text.slice(0, 900)})`);
  w = await placesWorld({ exec: suiteExec(P119.doc({ notes: ["entry 1 ignored: machine 'box 1' is not a machine name"] })) });
  check(look(w.v).text.includes("entry 1 ignored: machine 'box 1' is not a machine name"), "the config notes");
};

cases["P119-8-read-only"] = async () => { // R7: nothing in the block runs a tool or writes
  const { v } = await placesWorld({ exec: suiteExec(P119.doc()) });
  g.__runs = []; g.__writes = [];
  await click(v, "places-slot-box-1-0"); await settle();
  check(!g.__writes.length && g.__runs.every(([n]) => n.startsWith("terminal.") || n.startsWith("layout.")), `only the worker opened (got ${JSON.stringify(g.__runs)})`);
  check(suiteCalls().every(([, a]) => JSON.stringify(a) === JSON.stringify(["--places", "--json"])), "only --places --json");
};

// ---- t-101: every rule change the person makes in the Rules view tells the Project Agent, once, naming the rules ----
const rulesNotices = (v) => v.unsent.filter((u) => u.spaceId === S && /^rules changed/.test(u.message)).map((u) => u.message);
async function dropOn(v, id) { // the mouse-up of a drag, on the rule row `id`
  const el = pathTo(v.render(cx), id)?.at(-1);
  check(Boolean(el?.style?.on_mouse_up), `a drop target ${id}`);
  el.style.on_mouse_up[1]({}, cx); await settle();
}
async function ruleAction(v, kind) {
  if (kind === "delete") { await press(v, "rule-delete-r-one"); await press(v, "rule-delete-r-one"); }
  else if (kind === "off") await press(v, "rule-toggle-r-two");
  else if (kind === "on") { await press(v, "rule-toggle-r-two"); await settle(); v.unsent = []; await press(v, "rule-toggle-r-two"); }
  else if (kind === "edit") { await press(v, "rule-edit-r-one"); v.rules.form.inputs.text.set("One, changed"); await press(v, "rule-save"); }
  else if (kind === "add") { await press(v, "rule-add"); v.rules.form.inputs.title.set("Fresh"); v.rules.form.inputs.text.set("Fresh text"); await press(v, "rule-save"); }
  else if (kind === "reorder") { await press(v, "rule-grip-r-two"); await dropOn(v, "rule-r-one"); }
  else if (kind === "import") {
    g.__files[":/home/u/exp.json"] = JSON.stringify({ version: 1, rules: [rule("r-new", "New"), rule("r-one", "One again")] });
    await press(v, "rule-import"); v.rules.importPath.set("/home/u/exp.json"); await press(v, "rule-import");
  }
  await settle();
}

cases["P101-1-each-change"] = async () => { // R1, R2: add, edit, delete, on/off, reorder, import: one notice naming the rule
  const want = { delete: "r-one", off: "r-two", on: "r-two", edit: "r-one", add: "r-fresh", reorder: "r-two", import: "r-new" };
  for (const [kind, id] of Object.entries(want)) {
    const v = await ruleWorld();
    const before = g.__files[RULES];
    await ruleAction(v, kind);
    check(g.__files[RULES] !== before, `${kind}: rules.json written`);
    check(JSON.stringify(rulesNotices(v)) === JSON.stringify([`rules changed: ${id}`]), `${kind}: one notice naming ${id} (got ${JSON.stringify(v.unsent)})`);
  }
};

cases["P101-2-merged"] = async () => { // R3: while unsent, later changes join the one notice; an id is named once
  const v = await ruleWorld();
  await press(v, "rule-toggle-r-two"); await settle();
  await press(v, "rule-delete-r-one"); await press(v, "rule-delete-r-one"); await settle();
  await press(v, "rule-toggle-r-two"); await settle();
  check(JSON.stringify(rulesNotices(v)) === JSON.stringify(["rules changed: r-two, r-one"]), `one notice, both rules once (got ${JSON.stringify(v.unsent)})`);
  check(v.unsent.length === 1, `nothing else queued (got ${JSON.stringify(v.unsent)})`);
};

cases["P101-3-nothing-written"] = async () => { // R4: a refused write, an import adding nothing: no notice
  let v = await ruleWorld();
  g.__files[RULES] = "{ not json";
  await press(v, "rule-toggle-r-two"); await settle();
  check(g.__files[RULES] === "{ not json", "the invalid file is not written over");
  check(rulesNotices(v).length === 0, `no notice for a refused write (got ${JSON.stringify(v.unsent)})`);
  v = await ruleWorld();
  g.__files[":/home/u/same.json"] = JSON.stringify({ version: 1, rules: [rule("r-one", "One"), rule("r-two", "Two")] });
  await press(v, "rule-import"); v.rules.importPath.set("/home/u/same.json"); await press(v, "rule-import"); await settle();
  check(/Imported 0 new rules/.test(look(v).text), `the import says 0 (got ${look(v).text.slice(0, 400)})`);
  check(rulesNotices(v).length === 0, `no notice for an import that added none (got ${JSON.stringify(v.unsent)})`);
};

cases["P101-4-not-the-agents"] = async () => { // R5: the agent's own proposal sends nothing; approving one keeps its own notice only
  const v = await ruleWorld();
  const doc = JSON.parse(g.__files[RULES]);
  doc.rules.push({ id: "r-p", title: "Proposed", text: "Proposed text", status: "proposed", order: 5, source: "orchestrator" });
  g.__files[RULES] = JSON.stringify(doc);
  v.reload(cx); await settle();
  check(look(v).text.includes("Proposed by the Project Agent"), `the proposal is shown (got ${look(v).text.slice(0, 400)})`);
  check(v.unsent.length === 0, `a proposal read on a reload queues nothing (got ${JSON.stringify(v.unsent)})`);
  await press(v, "rule-head-r-p"); await press(v, "rule-approve-r-p"); await settle();
  check(JSON.parse(g.__files[RULES]).rules.find((r) => r.id === "r-p").status === "active", "approved");
  check(rulesNotices(v).length === 0 && v.unsent.some((u) => u.message === "rule r-p approved"), `only the approval's notice (got ${JSON.stringify(v.unsent)})`);
};

cases["P101-5-delivered"] = async () => { // R3: sent by hc send to the live launcher's terminal, once
  const v = await ruleWorld();
  g.__shared["project-agent.tabs"] = alive("t4w");
  await press(v, "rule-toggle-r-two"); await settle();
  v.resend(cx); await flush(); await settle();
  const got = g.__hcs.filter((a) => a[0] === "send");
  check(JSON.stringify(got) === JSON.stringify([["send", "t4w", "rules changed: r-two"]]), `sent once (got ${JSON.stringify(got)})`);
  check(v.unsent.length === 0, `nothing left (got ${JSON.stringify(v.unsent)})`);
};

cases["P101-6-long-list"] = async () => { // R2: more than 10 rules named: the first 10 and how many more
  const v = await ruleWorld();
  const many = Array.from({ length: 12 }, (_, i) => rule(`r-m${i + 1}`, `M${i + 1}`));
  g.__files[":/home/u/many.json"] = JSON.stringify({ version: 1, rules: many });
  await press(v, "rule-import"); v.rules.importPath.set("/home/u/many.json"); await press(v, "rule-import"); await settle();
  const want = `rules changed: ${many.slice(0, 10).map((r) => r.id).join(", ")} and 2 more`;
  check(JSON.stringify(rulesNotices(v)) === JSON.stringify([want]), `capped (got ${JSON.stringify(v.unsent)})`);
};

// ---- t-085: a new rule's id can be named in the Rules view; t-101's recorded limits ----
const ruleDoc = () => JSON.parse(g.__files[RULES]);
async function newRule(v, id, title = "Fresh") {
  await press(v, "rule-add");
  check(Boolean(v.rules.form?.inputs?.id), "the new-rule form has an Id field");
  v.rules.form.inputs.title.set(title); v.rules.form.inputs.text.set(`${title} text`); v.rules.form.inputs.id.set(id);
  await press(v, "rule-save"); await settle();
}

cases["P085-1-named-id"] = async () => { // I1: an id typed in the Id field is the new rule's id; the notice names it
  const v = await ruleWorld();
  await newRule(v, "r-my-own");
  check(ruleIds() === "r-my-own,r-one,r-two", `made with the typed id (got ${ruleIds()})`);
  check(JSON.stringify(rulesNotices(v)) === JSON.stringify(["rules changed: r-my-own"]), `the notice names it (got ${JSON.stringify(v.unsent)})`);
  const r = ruleDoc().rules.find((x) => x.id === "r-my-own");
  check(r.title === "Fresh" && r.status === "active" && r.source === "person", `the rest as before (got ${JSON.stringify(r)})`);
  check(!("id" in r.history.at(-1)) && r.history.at(-1).change === "created", "history as before");
};

cases["P085-2-empty-id"] = async () => { // I1: left empty (or spaces), the id comes from the title as before
  const v = await ruleWorld();
  await newRule(v, "   ", "One");
  check(ruleIds() === "r-one-2,r-one,r-two", `from the title (got ${ruleIds()})`);
};

cases["P085-3-bad-shape"] = async () => { // I1: not r- + lowercase hyphen-joined words: said, nothing written, the form stays
  for (const bad of ["one", "R-one", "r-One", "r-", "r--a", "r-a-", "r-a--b", "r-a b", "r-a_b", "r-ü", `r-${"a".repeat(59)}`]) {
    const v = await ruleWorld();
    const before = g.__files[RULES];
    await newRule(v, bad);
    check(g.__files[RULES] === before, `${JSON.stringify(bad)}: nothing written`);
    check(/An id starts with r- and has only lowercase letters, digits and single hyphens/.test(look(v).text), `${JSON.stringify(bad)}: said (got ${look(v).text.slice(0, 300)})`);
    check(Boolean(v.rules.form) && v.rules.form.inputs.id.value() === bad, `${JSON.stringify(bad)}: the form stays, with what was typed`);
    check(rulesNotices(v).length === 0, `${JSON.stringify(bad)}: no notice`);
  }
  const v = await ruleWorld();
  await newRule(v, `r-${"a".repeat(58)}`); // 60 characters: the longest
  check(ruleIds().startsWith(`r-${"a".repeat(58)},`), `60 characters are allowed (got ${ruleIds()})`);
  const w = await ruleWorld();
  await newRule(w, "r-2nd-try-3");
  check(ruleIds().startsWith("r-2nd-try-3,"), `digits and words (got ${ruleIds()})`);
};

cases["P085-4-taken"] = async () => { // I1: unique among all rules, declined ones too; checked again against the file when written
  let v = await ruleWorld();
  let before = g.__files[RULES];
  await newRule(v, "r-two");
  check(g.__files[RULES] === before, "an active rule's id: nothing written");
  check(/r-two is already a rule's id/.test(look(v).text) && Boolean(v.rules.form), `said, the form stays (got ${look(v).text.slice(0, 300)})`);
  const doc = ruleDoc(); doc.rules.push({ ...rule("r-gone", "Gone"), status: "declined" }); g.__files[RULES] = JSON.stringify(doc);
  v = await ruleWorld(); g.__files[RULES] = JSON.stringify(doc); v.reload(cx); await settle();
  before = g.__files[RULES];
  await newRule(v, "r-gone");
  check(g.__files[RULES] === before, "a declined rule's id: nothing written");
  check(/r-gone is already a rule's id/.test(look(v).text), `said (got ${look(v).text.slice(0, 300)})`);
  v = await ruleWorld();
  await press(v, "rule-add");
  v.rules.form.inputs.title.set("Late"); v.rules.form.inputs.text.set("Late text"); v.rules.form.inputs.id.set("r-late");
  const d2 = ruleDoc(); d2.rules.push(rule("r-late", "Someone else's")); g.__files[RULES] = JSON.stringify(d2); // written meanwhile, not read yet
  before = g.__files[RULES];
  await press(v, "rule-save"); await settle();
  check(g.__files[RULES] === before, `taken by write time: nothing written (got ${g.__files[RULES]})`);
  check(/r-late is already a rule's id/.test(look(v).text), `said (got ${look(v).text.slice(0, 300)})`);
  check(rulesNotices(v).length === 0, `no notice (got ${JSON.stringify(v.unsent)})`);
};

cases["P085-5-read-only"] = async () => { // I2: Edit shows the id and has no field for it; the id stays
  const v = await ruleWorld();
  await press(v, "rule-edit-r-one");
  check(v.rules.form.inputs.id === undefined, "no Id field when editing");
  check(look(v).text.includes("Id: r-one (can't be changed)"), `the id is shown (got ${look(v).text.slice(0, 400)})`);
  v.rules.form.inputs.title.set("Renamed");
  await press(v, "rule-save"); await settle();
  check(ruleIds() === "r-one,r-two" && ruleDoc().rules[0].title === "Renamed", `same id, new title (got ${g.__files[RULES]})`);
  await press(v, "rule-add");
  check(look(v).text.includes("New rule") && !look(v).text.includes("can't be changed"), "a new rule's form has the field, not the line");
};

cases["P085-6-import-ids"] = async () => { // RL1: an imported rule needs a string id; ids reach the notice on one line
  const v = await ruleWorld();
  g.__files[":/home/u/odd.json"] = JSON.stringify({ version: 1, rules: [{ ...rule("x", "Num"), id: 42 }, { ...rule("x", "Obj"), id: { a: 1 } }, rule("r-x\nhc send t9 boo", "Nl"), rule("r-ok", "Ok")] });
  await press(v, "rule-import"); v.rules.importPath.set("/home/u/odd.json"); await press(v, "rule-import"); await settle();
  check(/Imported 2 new rules/.test(look(v).text), `two imported (got ${look(v).text.slice(0, 300)})`);
  check(!ruleDoc().rules.some((r) => typeof r.id !== "string"), `no rule without a string id (got ${g.__files[RULES]})`);
  check(JSON.stringify(rulesNotices(v)) === JSON.stringify(["rules changed: r-x hc send t9 boo, r-ok"]), `one line (got ${JSON.stringify(v.unsent)})`);
};

cases["P085-7-in-flight"] = async () => { // RL2: a change while the rules notice is being sent, then that send fails: one notice
  const v = await ruleWorld();
  g.__shared["project-agent.tabs"] = alive("t4w");
  g.__hcHang = ["t4w"];
  await press(v, "rule-toggle-r-two"); await settle();
  v.resend(cx); await settle();
  check(v.sending === true && JSON.stringify(sends().map((x) => x[2])) === JSON.stringify(["rules changed: r-two"]), `in flight (sending ${v.sending}, ${JSON.stringify(sends())})`);
  await press(v, "rule-delete-r-one"); await press(v, "rule-delete-r-one"); await settle();
  g.__hcFail = ["t4w"]; g.__hcHang = [];
  await settle(); await settle();
  check(v.sending === false, "the send ended");
  check(JSON.stringify(rulesNotices(v)) === JSON.stringify(["rules changed: r-two, r-one"]), `one notice, both rules (got ${JSON.stringify(v.unsent)})`);
  g.__hcFail = []; g.__hcs = [];
  v.resend(cx); await settle();
  check(JSON.stringify(sends()) === JSON.stringify([["send", "t4w", "rules changed: r-two, r-one"]]), `sent once (got ${JSON.stringify(sends())})`);
};

cases["P085-8-gone-meanwhile"] = async () => { // nit (t-101 REVIEW): a rule gone from the file by write time is not named
  const v = await ruleWorld();
  await press(v, "rule-edit-r-one"); v.rules.form.inputs.text.set("One, changed");
  g.__files[RULES] = JSON.stringify({ rules: [rule("r-two", "Two")] }); // r-one deleted meanwhile, not read yet
  await press(v, "rule-save"); await settle();
  check(ruleIds() === "r-two", `nothing brought back (got ${ruleIds()})`);
  check(rulesNotices(v).length === 0, `edit: no notice (got ${JSON.stringify(v.unsent)})`);
  const w = await ruleWorld();
  g.__files[RULES] = JSON.stringify({ rules: [rule("r-one", "One")] }); // r-two deleted meanwhile
  await press(w, "rule-toggle-r-two"); await settle();
  check(rulesNotices(w).length === 0, `on/off: no notice (got ${JSON.stringify(w.unsent)})`);
};

// ---- t-131: limits from t-100, t-061/t-054 (t-064), t-046 J3 (t-075) and t-020 J6 ----------------------------------
cases["P131-push-chosen-slow-reload"] = async () => { // t-100 RL1: after a Push from here, before the slow reload reads it answered, Push is drawn chosen
  let file;
  const { v, file: f } = await pushWorld({ tool: { run: () => { g.__files[file] = answeredByTool(); return json(0, toolPushed); } } });
  file = f;
  await click(v, `opt-${PID}-0`); await settle();
  g.__delay = { [`:${DIR}/agent.json`]: 5000 };
  await click(v, `submit-${PID}`); await settle(); await settle();
  check(v.cards.find((c) => c.id === PID)?.card?.status === "open", "the panel's copy still reads the card open (the reload is slow)");
  check(v.push.answeredHere(S, PID) === "Push" && !v.push.running(S, PID), "the run ended answered");
  const rows = pushRows(v);
  check(rows["0"].chosen && !rows["1"].chosen && !rows.other.chosen, `Push drawn chosen from the run's answer (got ${JSON.stringify(rows)})`);
  check(!rows["0"].click && !rows["1"].click && !rows.other.click, `locked (got ${JSON.stringify(rows)})`);
  await step(5000); await settle();
  check(pushRows(v)["0"].chosen, "after the reload: Push chosen (the card file's answer)");
};

cases["P131-push-unknown-nothing-chosen"] = async () => { // t-100 RL2: an unknown result: no option drawn chosen, every option locked
  const v = await pushUnknownThen(() => { throw new Error("unreachable"); });
  const rows = pushRows(v);
  check(look(v).text.includes("The result is unknown"), "says the result is unknown");
  check(!rows["0"].chosen && !rows["1"].chosen && !rows.other.chosen, `nothing chosen (got ${JSON.stringify(rows)})`);
  check(!rows["0"].click && !rows["1"].click && !rows.other.click, `every option locked (got ${JSON.stringify(rows)})`);
};

cases["P131-unread-cards-said"] = async () => { // t-061 R1: a card the fallback could not read, with no known copy, is never silently missing
  world(); localCards(3, { open: 3 });
  g.__batchFail = { "": { code: 1 } }; g.__fail = [`:${DIR}/decisions/d-002.json`];
  const v = await mount(); await settle(); await settle();
  const { text } = look(v);
  check(!text.includes("Q d-002?"), "d-002 not read");
  check(text.includes("1 card could not be read now; the next load reads it again."), `said (got ${text.slice(0, 900)})`);
  g.__batchFail = {}; g.__fail = [];
  advance(11000); v.reload(cx); await settle(); await settle();
  const t2 = look(v).text;
  check(t2.includes("Q d-002?") && !t2.includes("could not be read now"), `read again: shown, the line gone (got ${t2.slice(0, 900)})`);
};

cases["P131-archive-no-duplicate"] = async () => { // t-054 N2: a card id in decisions/ and in the archive is drawn once (decisions/ wins)
  world(); localCards(3, { open: 1 });
  g.__files[`:${DIR}/decisions-archive/d-002.json`] = JSON.stringify({ id: "d-002", status: "answered", answer: "No", question: "The archived copy?", options: [{ label: "Yes" }], created: "2026-01-01T10:00:00Z" });
  g.__files[`:${DIR}/decisions-archive/d-000.json`] = JSON.stringify({ id: "d-000", status: "answered", answer: "Yes", question: "An archived question?", options: [{ label: "Yes" }], created: "2026-01-01T10:00:00Z" });
  g.__ls[`:${DIR}/decisions-archive`] = ["d-000.json", "d-002.json"];
  const v = await mount(); await settle();
  await click(v, "pa-older"); await click(v, "pa-archive"); await settle();
  const { text } = look(v);
  const ids = []; const walk = (x) => { if (!x || typeof x !== "object") return; if (typeof x.key === "string" && x.key === "card-d-002") ids.push(x.key); for (const k of x.kids ?? []) walk(k); }; walk(v.render(cx));
  check(ids.length === 1, `one element card-d-002 (got ${ids.length})`);
  check(text.includes("Q d-002?") && !text.includes("The archived copy?") && text.includes("An archived question?"), `decisions/ wins; the other archived card shown (got ${text.slice(0, 1200)})`);
};

cases["P131-push-other-words-other-window"] = async () => { // t-046 J3 RL2: a push card answered Other shows the words in every window before its own read
  const gate = deferred();
  let file;
  const { v, file: f } = await pushWorld({ tool: { run: () => gate.promise.then((reply) => { g.__files[file] = pushOtherCard("do X first"); return reply; }) } });
  file = f;
  const w = await mount(); await settle();
  await click(v, `opt-${PID}-other`); typeInto(v, PID, "do X first");
  await click(v, `submit-${PID}`); await settle();
  check(look(w).text.includes("Your answer: “do X first”"), `the other window, run in flight (got ${look(w).text.slice(0, 900)})`);
  g.__delay = { [`:${DIR}/agent.json`]: 5000 };
  gate.resolve(json(0, pushOtherReply)); await settle(); await settle();
  const tw = locked(w, PID, "Other", "the other window, before its own read");
  check(tw.includes("Your answer: “do X first”"), `the other window: the words (got ${tw.slice(0, 900)})`);
  check(look(v).text.includes("Your answer: “do X first”"), `this window, before its slow reload (got ${look(v).text.slice(0, 900)})`);
};

cases["P131-submit-fallback-no-change-event"] = async () => { // t-046 J3 RL1: a field with no change event: Submit for Other stays on; an empty click is refused with the hint
  const real = stubs.InputState.new;
  stubs.InputState.new = (o) => { const s = real(o); delete s.on; return s; };
  try {
    const v = await otherWorld({ "d-x": {} });
    await click(v, "opt-d-x-other");
    check(Boolean(look(v).clicks["submit-d-x"]), "Submit on for Other with no words (no change event to follow the typing)");
    await click(v, "submit-d-x"); await settle();
    check(JSON.parse(g.__files[cardFile("d-x")]).status === "open" && look(v).text.includes(NEEDS_WORDS), `empty: refused with the hint, nothing written (got ${look(v).text.slice(0, 900)})`);
    fieldOf(v, "d-x").set("do X first"); // typed: no event fires
    await click(v, "submit-d-x"); await settle();
    const c = JSON.parse(g.__files[cardFile("d-x")]);
    check(c.status === "answered" && c.answer === "Other" && c.note === "do X first", `written with the words (got ${JSON.stringify(c)})`);
    const { v: p } = await pushWorld();
    await click(p, `opt-${PID}-other`);
    check(Boolean(look(p).clicks[`submit-${PID}`]), "a push card: Submit on too");
    await click(p, `submit-${PID}`); await settle();
    check(pushExecs("run").length === 0 && look(p).text.includes(NEEDS_WORDS), `a push card: no run, the hint (got ${look(p).text.slice(0, 900)})`);
  } finally { stubs.InputState.new = real; }
  const v2 = await otherWorld({ "d-x": {} }); // with the change event: as before
  await click(v2, "opt-d-x-other");
  check(!look(v2).clicks["submit-d-x"], "with a change event: Submit off until words are typed");
};

cases["P131-main-job-collides"] = async () => { // t-020 J6 RL-M2: the main worktree's own entry gives its job; another entry with that job gets -2
  world();
  g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [{ id: "t-8", title: "eight", state: "in-progress", worktree: "/w/t-8-a",
    worktrees: [{ path: "/w/t-8-a", job: "api" }, { path: "/w/t-8-b", job: "api" }] }] });
  localCards(1);
  await mount(); await settle();
  const got = JSON.stringify(g.__shared["project-agent.worktrees"]?.[S]);
  check(got === JSON.stringify([{ task: "t-8", path: "/w/t-8-a", title: "eight" }, { task: "t-8", path: "/w/t-8-b", title: "eight", job: "api-2" }]), `the other entry gets api-2 (got ${got})`);
};

// ---- t-142: Places worker lines drawn as the task card's worker item; a task's moves and Start next on hover or focus ----
const P142 = {
  find: (root, key) => { let hit = null; const walk = (x) => { if (hit || !x || typeof x !== "object") return; if (x.key === key) { hit = x; return; } for (const k of x.kids ?? []) walk(k); }; walk(root); return hit; },
  // The item's box (the task card's styles) and its status dot.
  shape: (el) => el && { kind: el.kind, box: Object.fromEntries(["flex_none", "px", "h", "gap", "items_center", "rounded", "border", "border_color", "text_size"].map((k) => [k, JSON.stringify(el.style?.[k] ?? null)])) },
  dot: (el) => { const d = (el?.kids ?? []).find((k) => k && typeof k === "object" && k.style?.size); return d ? { size: JSON.stringify(d.style.size), rounded: JSON.stringify(d.style.rounded), bg: d.style.bg?.[0] ?? null } : null; },
  status: (address, status) => { const x = g.__terminals.find((t) => t.address === address); x.status = status; },
};

cases["P142-1-places-item"] = async () => { // R1, R2: a Places holder is the task card's worker item, its dot the terminal's status color
  const { v } = await placesWorld();
  g.__themeNames = true;
  P142.status("t53", "blocked"); P142.status("t54", "idle"); // t51 stays working
  await click(v, "task-head-t-020"); // the task card's own worker items
  const root = v.render(cx), card = P142.find(root, "worker-t-020-0"), place = P142.find(root, "places-holder-t51");
  check(Boolean(card) && Boolean(place), `both items drawn (got card ${Boolean(card)}, place ${Boolean(place)})`);
  check(JSON.stringify(P142.shape(place)) === JSON.stringify(P142.shape(card)), `the same box (place ${JSON.stringify(P142.shape(place))}, card ${JSON.stringify(P142.shape(card))})`);
  check(JSON.stringify(P142.dot(place)) === JSON.stringify(P142.dot(card)) && P142.dot(place)?.bg === "#working", `the same dot, working (place ${JSON.stringify(P142.dot(place))}, card ${JSON.stringify(P142.dot(card))})`);
  check(P142.dot(P142.find(root, "places-holder-t53"))?.bg === "#blocked", `t53 blocked (got ${JSON.stringify(P142.dot(P142.find(root, "places-holder-t53")))})`);
  check(P142.dot(P142.find(root, "places-holder-t54"))?.bg === "#idle", `t54 idle (got ${JSON.stringify(P142.dot(P142.find(root, "places-holder-t54")))})`);
  const t53 = P142.find(root, "places-holder-t53"), card53 = P142.find(root, "worker-t-020-1");
  check(P142.dot(card53)?.bg === "#blocked" && JSON.stringify(P142.shape(t53)) === JSON.stringify(P142.shape(card53)), "the suite step holder too, as its card item");
  P142.status("t54", null);
  check(P142.dot(P142.find(v.render(cx), "places-holder-t54"))?.bg === "#idle", "no status: idle, as the task card draws it");
  check(look(v).text.includes("t53 · t-020 verify · verifier · suite step · since 11-19 10:02 ↗"), `the line's text and link mark kept (got ${look(v).text.slice(0, 900)})`);
};

cases["P142-2-slot-item"] = async () => { // R1, R2: a Suite machines holder is the same item; one with no live worker: the dim dot, no link
  const { v } = await placesWorld({ exec: suiteExec(P119.doc()) });
  g.__themeNames = true;
  P142.status("t53", "blocked");
  await click(v, "task-head-t-020");
  const root = v.render(cx), card = P142.find(root, "worker-t-020-1"), slot = P142.find(root, "places-slot-box-1-0");
  check(Boolean(slot) && JSON.stringify(P142.shape(slot)) === JSON.stringify(P142.shape(card)), `the same box (slot ${JSON.stringify(P142.shape(slot))}, card ${JSON.stringify(P142.shape(card))})`);
  check(P142.dot(slot)?.bg === "#blocked", `its worker's status (got ${JSON.stringify(P142.dot(slot))})`);
  const [local] = stubs.findText(root, "t77 · t-041 · ? · since 11-19 10:05"); // t-155: the worker item's text
  check(Boolean(local) && JSON.stringify(P142.shape(local)) === JSON.stringify(P142.shape(card)) && P142.dot(local)?.bg === "#dim", `not live: the item, dim (got ${JSON.stringify(P142.shape(local))} ${JSON.stringify(P142.dot(local))})`);
  check(!look(v).clicks["places-slot-local-0"], "not live: no link");
  check(look(v).text.includes("box-1 · reachable · 1 of 2 slots held") && look(v).text.includes("t53 · t-020 verify · verifier · since 11-19 10:05 ↗"), "heads and texts kept (t-155: no command)");
};

// ---- t-155: a Suite machines holder is the one-line worker item (worker, task and job, role, since), no command ----
const P155 = {
  cmd: `pa-run mutation: python3 -c 'import os, sys\nfor k in sorted(os.environ):\n    print(k)\n' ${"--and-a-very-long-argument ".repeat(12)}\nsh tests/run-all.sh --affected`,
  bits: ["python3", "import os", "print(k)", "--and-a-very-long-argument", "pa-run mutation", "run-all.sh", "/wt/x"],
  // The item's own text: its string kids (the dot is an element) and every string under it.
  strings: (el) => { const out = []; const walk = (x) => { if (x === null || x === undefined) return; if (typeof x !== "object") { out.push(String(x)); return; } if (x.kind === "textview") out.push(String(x.text)); for (const k of x.kids ?? []) walk(k); }; walk(el); return out; },
};

cases["P155-1-no-command"] = async () => { // a long, multi-line command: the one-line worker item, nothing of the command
  const doc = P119.doc({ local: { cap: 1, holders: [P119.holder("t-041", "t54", { what: P155.cmd })] } });
  doc.machines[0].holders = [P119.holder("t-020", "t53", { what: P155.cmd })];
  const { v } = await placesWorld({ exec: suiteExec(doc), terms: ["t51", "t53", "t59x"] }); // t54 not live
  const root = v.render(cx), { text } = look(v);
  const bad = P155.bits.filter((b) => text.includes(b));
  check(!bad.length, `no part of the command anywhere (found ${JSON.stringify(bad)} in ${text.slice(0, 1500)})`);
  const slot = P142.find(root, "places-slot-box-1-0");
  check(Boolean(slot) && JSON.stringify(P155.strings(slot)) === JSON.stringify(["t53 · t-020 verify · verifier · since 11-19 10:05 ↗"]),
    `box-1's holder: worker, task and job, role, since, a link (got ${JSON.stringify(P155.strings(slot))})`);
  const [local] = stubs.findText(root, "t54 · t-041 review · reviewer · since 11-19 10:05");
  check(Boolean(local) && JSON.stringify(P155.strings(local)) === JSON.stringify(["t54 · t-041 review · reviewer · since 11-19 10:05"]) && !look(v).clicks["places-slot-local-0"],
    `this Mac's holder, its worker not live: the ledger's job and role, no link (got ${JSON.stringify(local && P155.strings(local))})`);
  for (const el of [slot, local].filter(Boolean)) check(P155.strings(el).every((s) => !/[\n\r]/.test(s)), `one line (got ${JSON.stringify(P155.strings(el))})`);
  for (const head of ["box-1 · reachable · 1 of 2 slots held", "box-2 · not reachable (needs_login) · 0 of 1 slots held", "this Mac · 1 of 1 slots held"]) {
    const hit = stubs.findText(root, head);
    check(hit.length === 1 && JSON.stringify(P155.strings(hit[0])) === JSON.stringify([head]), `${head}: its own line (got ${JSON.stringify(hit.map(P155.strings))})`);
  }
  const at = (s) => text.indexOf(s, text.indexOf("Suite machines")); // inside the block: t53 is also drawn under Working above it
  check(at("Suite machines") >= 0 && at("box-1 · reachable") >= 0 && at("box-1 · reachable") < at("t53 · t-020 verify") && at("t53 · t-020 verify") < at("box-2 ·") && at("box-2 ·") < at("this Mac ·") && at("this Mac ·") < at("t54 · t-041 review"),
    `each holder under its machine (got ${text.slice(at("Suite machines"), at("Suite machines") + 600)})`);
};

cases["P155-2-remote"] = async () => { // on the space's machine: the same item, through the machine's address, no command
  const doc = P119.doc(); doc.machines[0].holders = [P119.holder("t-020", "t53", { what: P155.cmd })];
  const { v } = await placesWorld({ machine: M, exec: suiteExec(doc) });
  const { text } = look(v);
  check(text.includes("t53 · t-020 verify · verifier · since 11-19 10:05 ↗") && !P155.bits.some((b) => text.includes(b)), `the item, no command (got ${text.slice(0, 1500)})`);
  g.__runs = [];
  await click(v, "places-slot-box-1-0");
  check(g.__runs.some(([n, a]) => n === "terminal.peek" && a?.terminal === `${M}/t53`), `opens ${M}/t53 (got ${JSON.stringify(g.__runs)})`);
};

const moveIds = (clicks, id) => Object.keys(clicks).filter((k) => k.endsWith(`-${id}`) && /^(tmove-|next-)/.test(k)).sort();

cases["P142-3-hover"] = async () => { // R4: a task's moves and Start next only while the pointer is over it
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2), tq("t-3", "queued", 3), tq("t-4", "in-progress", 4)];
  const { v } = await orderWorld({ tasks });
  let { clicks } = look(v);
  check(!Object.keys(clicks).some((k) => /^(tmove-|next-)/.test(k)), `none without a hover (have ${Object.keys(clicks).filter((k) => /^(tmove|next)/.test(k))})`);
  check(Boolean(clicks["pause-t-4"]), "Pause is not hidden");
  clicks = (await hoverTask(v, "t-2")).clicks;
  check(JSON.stringify(moveIds(clicks, "t-2")) === JSON.stringify(["next-t-2", "tmove-bottom-t-2", "tmove-down-t-2", "tmove-top-t-2", "tmove-up-t-2"]), `t-2 hovered: its moves and Start next (have ${moveIds(clicks, "t-2")})`);
  check(!moveIds(clicks, "t-1").length && !moveIds(clicks, "t-3").length, "the others: none");
  clicks = (await hoverTask(v, "t-3")).clicks;
  check(moveIds(clicks, "t-3").length === 3 && !moveIds(clicks, "t-2").length, `the pointer moves on: t-3's only (have ${Object.keys(clicks).filter((k) => /^(tmove|next)/.test(k))})`);
  clicks = (await hoverTask(v, "t-3", false)).clicks;
  check(!Object.keys(clicks).some((k) => /^(tmove-|next-)/.test(k)), "the pointer leaves: none");
};

cases["P142-4-focus"] = async () => { // R4: and while the task or one of those buttons has keyboard focus
  const tasks = [tq("t-1", "queued", 1), tq("t-2", "queued", 2), tq("t-3", "queued", 3)];
  const { v } = await orderWorld({ tasks });
  look(v); await settle(); // the focus handles are made after the first draw
  const head = P142.find(v.render(cx), "task-head-t-2")?.style?.track_focus?.[0];
  check(Boolean(head?.focus) && Boolean(P142.find(v.render(cx), "task-head-t-2")?.style?.tab_index), "the task's head takes keyboard focus");
  head.focus();
  let { clicks } = look(v);
  check(moveIds(clicks, "t-2").length === 5 && !moveIds(clicks, "t-1").length, `the head focused: its buttons (have ${moveIds(clicks, "t-2")})`);
  const up = P142.find(v.render(cx), "tmove-up-t-2")?.style?.track_focus?.[0];
  check(Boolean(up?.focus) && up !== head, "a button takes keyboard focus of its own");
  up.focus();
  check(moveIds(look(v).clicks, "t-2").length === 5, "a button focused: they stay");
  const next = P142.find(v.render(cx), "next-t-2")?.style?.track_focus?.[0];
  next.focus();
  check(moveIds(look(v).clicks, "t-2").length === 5, "Start next focused: they stay");
  g.__focus = null;
  check(!Object.keys(look(v).clicks).some((k) => /^(tmove-|next-)/.test(k)), "focus elsewhere: none");
};

// ---- t-145: the top bar's two toggles, the Passes tab, the profile link, the counts, the archive trims ----
const P145 = {
  el: (v, id) => pathTo(v.render(cx), id)?.at(-1) ?? null,
  tip: (v, id) => String(P145.el(v, id)?.style?.tooltip?.[0] ?? ""),
  own: (v, id) => (P145.el(v, id)?.kids ?? []).filter((k) => typeof k !== "object").join(""),
  // The element is in the PA head row: an ancestor that names the agent and holds no view tab.
  inHead: (v, id) => (pathTo(v.render(cx), id) ?? []).some((x) => { const s = inspect(x).text; return /PA: proj/.test(s) && !/Decisions/.test(s); }),
  parts: (v) => look(v).text.split(" | "),
  ledger: (tasks, extra = {}) => { g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks, ...extra }); },
};

cases["P145-1-coffee"] = awWrap(async () => { // I1: one coffee button beside the PA status; no text; the state and limits in its tooltip
  awSetup(); g.__themeNames = true;
  const v = await mount(); awHold(v);
  await fire(1000);
  const { text, clicks } = look(v);
  check(Boolean(clicks["awake-toggle"]) && P145.inHead(v, "awake-toggle"), `the button in the PA head row (clicks ${Object.keys(clicks)})`);
  check(P145.own(v, "awake-toggle") === "☕", `a coffee glyph only (got ${P145.own(v, "awake-toggle")})`);
  check(!/Keep this Mac awake|Turn off|Turn on|Closing the lid|Remote machines/.test(text), `no keep-awake text in the panel (got ${text.slice(0, 600)})`);
  const tip = P145.tip(v, "awake-toggle");
  check(/Keep this Mac awake while Herdr runs: on/.test(tip) && /holding/.test(tip) && /lock the screen by hand/.test(tip) && /lid/.test(tip) && /Remote machines you work on are not affected/.test(tip), `the tooltip says the state and the limits (got ${tip})`);
  const onBg = JSON.stringify(P145.el(v, "awake-toggle").style?.bg);
  await click(v, "awake-toggle"); await settle();
  check(awCalls().at(-1)?.[0] === "stop", `a click stops it, as before (got ${JSON.stringify(awCalls())})`);
  check(/runs: off/.test(P145.tip(v, "awake-toggle")), `the tooltip says off (got ${P145.tip(v, "awake-toggle")})`);
  check(JSON.stringify(P145.el(v, "awake-toggle").style?.bg) !== onBg, `on and off look different (both ${onBg})`);
  awSetup({ platform: "linux" });
  const w = await mount();
  check(!look(w).clicks["awake-toggle"] && !/macOS only|Keep this Mac/.test(look(w).text), `off macOS: no button, no text (got ${look(w).text.slice(0, 400)})`);
});

cases["P145-2-fresh-toggle"] = async () => { // I2: one fresh-contexts switch beside the coffee button; no force control; the mode in its tooltip
  const { dir } = ctxWorld({ fresh: (args) => {
    if (args[0] !== "set") return args.includes("--dry-run") ? DRY41() : ASKED41;
    const mode = args[args.indexOf("--mode") + 1];
    g.__files[`${dir}/context.json`] = JSON.stringify({ v: 1, mode, taskCap: 150000, passCap: 250000, net: 400000, enabledAt: null, by: "panel" });
    return SET41(mode);
  } });
  g.__themeNames = true;
  const v = await liveMount();
  const { text, clicks } = look(v);
  check(Boolean(clicks["pa-context-mode"]) && P145.inHead(v, "pa-context-mode"), `the switch in the PA head row (clicks ${Object.keys(clicks)})`);
  check(P145.own(v, "pa-context-mode") === "↻", `a glyph only (got ${P145.own(v, "pa-context-mode")})`);
  check(!clicks["pa-fresh"] && !/Fresh context|Turn fresh contexts|mode fresh|PA context/.test(text), `no force control, no context text (got ${text.slice(0, 700)})`);
  check(/Fresh contexts: on/.test(P145.tip(v, "pa-context-mode")) && /turn them off/i.test(P145.tip(v, "pa-context-mode")), `the tooltip (got ${P145.tip(v, "pa-context-mode")})`);
  const onBg = JSON.stringify(P145.el(v, "pa-context-mode").style?.bg);
  await click(v, "pa-context-mode"); await settle(); await settle();
  check(ctxWrites().length === 1 && JSON.stringify(ctxWrites()[0][1]) === JSON.stringify(["set", "--space", S, "--by", "panel", "--mode", "off", "--json"]), `the same set (got ${JSON.stringify(ctxWrites())})`);
  check(/Fresh contexts: off/.test(P145.tip(v, "pa-context-mode")) && /turn them on/i.test(P145.tip(v, "pa-context-mode")), `off after the reload (got ${P145.tip(v, "pa-context-mode")})`);
  check(JSON.stringify(P145.el(v, "pa-context-mode").style?.bg) !== onBg, "on and off look different");
  check(!/Context mode off/.test(look(v).text), `no result line in the panel (got ${look(v).text.slice(0, 700)})`);
  await click(v, "pa-context-mode"); await settle(); await settle();
  check(ctxWrites().length === 2 && ctxWrites()[1][1].includes("fresh"), "and back on");
};

cases["P145-3-context-rows-in-costs"] = async () => { // I2: the context size line and the reset line live in the Costs tab; a failed switch says so; an invalid file in the tooltip
  const lastReset = { at: `${DAY41}T07:02:00Z`, from: "3f2a9c1e-aaaa-bbbb-cccc-dddddddddddd", to: "s1", reason: "pass-cap", ctx: 260000 };
  ctxWorld({ launcher: LREC({ accepts: ["restart", "context"], lastReset }) });
  let v = await liveMount();
  check(!look(v).text.includes("Fresh context at 07:02") && !look(v).clicks["copy-previous-session"], `not above the tabs (got ${look(v).text.slice(0, 700)})`);
  await click(v, "view-costs"); await settle();
  let text = look(v).text;
  check(text.includes("PA context today (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0) (as of 07:59)"), `the size line in Costs (got ${text.slice(0, 900)})`);
  check(text.includes("Fresh context at 07:02 (pass cap, 260k) · previous session 3f2a9c1e…") && Boolean(look(v).clicks["copy-previous-session"]), `the reset line and its Copy in Costs (got ${text.slice(0, 900)})`);
  await click(v, "view-tasks");
  check(!look(v).text.includes("PA context today"), "not in another tab");
  ctxWorld({ context: "{not json" });
  v = await liveMount();
  check(!/context\.json is not valid/.test(look(v).text) && /context\.json is not valid/.test(P145.tip(v, "pa-context-mode")), `the invalid file in the tooltip (got ${P145.tip(v, "pa-context-mode")})`);
  ctxWorld({ fresh: (args) => (args[0] === "set" ? { code: 2, stdout: "", stderr: `${CTX}: refused for the test\n` } : DRY41()) });
  v = await liveMount();
  await click(v, "pa-context-mode"); await settle(); await settle();
  text = look(v).text;
  check(text.includes(`${CTX}: refused for the test`), `a failed switch is said (got ${text.slice(0, 700)})`);
};

cases["P145-4-passes-tab"] = async () => { // I3: the schedule is a tab between Rules and Costs; its label says the state
  world();
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  const v = await mount(); await settle();
  let { text, clicks } = look(v);
  check(!clicks["sched-summary"] && !clicks["pass-now"] && !/Passes every 15 min/.test(text), `not shown outside its tab (got ${text.slice(0, 500)})`);
  const at = (x) => text.indexOf(x);
  check(at("| Passes · 15 min |") > at("| Rules") && at("| Passes · 15 min |") < at("| Costs") && at("| Rules") >= 0, `between Rules and Costs (got ${text.slice(0, 500)})`);
  await click(v, "view-schedule");
  ({ text, clicks } = look(v));
  check(Boolean(clicks["sched-summary"]) && Boolean(clicks["pass-now"]) && Boolean(clicks["pass-retro"]) && Boolean(clicks["sched-pause-top"]) && /Passes every 15 min/.test(text), `the bar in its tab (got ${text.slice(0, 500)})`);
  await click(v, "sched-summary");
  check(Boolean(look(v).clicks["every-15"]) && Boolean(look(v).clicks["quiet-set"]), "its settings unfold as before");
  await click(v, "every-30"); await settle();
  check(JSON.parse(g.__files[`:${DIR}/schedule.json`]).everyMinutes === 30 && /\| Passes · 30 min \|/.test(look(v).text), `a setting writes as before; the label follows (got ${look(v).text.slice(0, 400)})`);
  for (const [s, label] of [[{ everyMinutes: 0 }, "Passes · off"], [{ everyMinutes: 15, paused: true }, "Passes · paused"], [null, "Passes · !"]]) {
    world();
    if (s) g.__files[`:${DIR}/schedule.json`] = JSON.stringify(s);
    const w = await mount(); await settle();
    check(look(w).text.includes(`| ${label} |`), `${JSON.stringify(s)}: ${label} (got ${look(w).text.slice(0, 400)})`);
    check(!/schedule\.json is missing/.test(look(w).text), "the missing-file line only in its tab");
  }
};

cases["P145-5-profile-link"] = async () => { // I4: the profile name opens its profile.json in the Files view
  world();
  const v = await mount(); await settle();
  check(Boolean(look(v).clicks["pa-profile"]) && P145.own(v, "pa-profile") === "test", `the name is a link (got ${P145.own(v, "pa-profile")})`);
  await click(v, "pa-profile");
  const r = g.__shared["my-files.root"];
  check(r?.space === S && r?.path === "/home/u/.config/herdr-pa/profiles/test" && r?.file === "profile.json", `the Files view at profile.json (got ${JSON.stringify(r)})`);
  check(g.__runs.some(([n, a]) => n === "layout.select" && a.child === "files"), `Files selected (got ${JSON.stringify(g.__runs)})`);
};

cases["P145-6-tasks-count"] = async () => { // I5: the Tasks tab counts the tasks not parked and not done
  world();
  P145.ledger([tq("t-1", "queued", 1), tq("t-2", "queued", 2), tq("t-3", "in-progress", 3), tq("t-4", "done", 4), tq("t-5", "done", 5), tq("t-6", "done", 6), tq("t-7", "parked", 7), tq("t-8", "parked", 8)]);
  const v = await mount(); await settle();
  check(/\| Tasks · 3 \|/.test(look(v).text), `Tasks · 3 (got ${look(v).text.slice(0, 400)})`);
};

cases["P145-7-state-counts-pass"] = async () => { // I6, I7: a count per active state; the pass number only while passes are on
  world();
  P145.ledger([tq("t-1", "needs-decision", 1), tq("t-2", "in-progress", 2), tq("t-3", "in-progress", 3), tq("t-4", "review", 4), tq("t-5", "queued", 5), tq("t-6", "queued", 6), tq("t-7", "queued", 7), tq("t-8", "done", 8), tq("t-9", "parked", 9)], { passCount: 7 });
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 0 });
  const v = await mount(); await settle();
  let text = look(v).text;
  check(text.includes("needs decision 1 · in progress 2 · review 1 · queued 3") && !/active of|investigating|parked \d|done \d/.test(text), `the counts per active state (got ${text.slice(0, 500)})`);
  check(!/pass 7/.test(text), `no pass number while passes are off (got ${text.slice(0, 500)})`);
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15 });
  v.reload(cx); await settle();
  check(/queued 3 · pass 7/.test(look(v).text), `the pass number while on (got ${look(v).text.slice(0, 500)})`);
  g.__files[`:${DIR}/schedule.json`] = JSON.stringify({ everyMinutes: 15, paused: true });
  v.reload(cx); await settle();
  check(!/pass 7/.test(look(v).text), "none while paused");
  P145.ledger([tq("t-8", "done", 8)]);
  v.reload(cx); await settle();
  check(look(v).text.includes("no active tasks"), `none active (got ${look(v).text.slice(0, 500)})`);
};

cases["P145-8-decisions-archived"] = async () => { // I8a: the 10 most recent finished cards; the rest behind one Archived (n) row
  world(); localCards(15, { open: 1 }); // d-001 open; d-002..d-015 answered, d-015 the newest
  const v = await mount(); await settle();
  await click(v, "pa-older");
  let p = P145.parts(v);
  check(p.includes("Q d-015?") && p.includes("Q d-006?") && !p.includes("Q d-005?"), `the 10 most recent (got ${look(v).text.slice(0, 1500)})`);
  check(/^▸ Archived \(4\)/.test(P145.own(v, "pa-archive")), `one collapsed row (got ${P145.own(v, "pa-archive")})`);
  await click(v, "pa-archive"); await settle();
  p = P145.parts(v);
  check(p.includes("Q d-005?") && p.includes("Q d-002?") && /^▾ Archived \(4\)/.test(P145.own(v, "pa-archive")), `opened: the rest (got ${P145.own(v, "pa-archive")})`);
  await click(v, "pa-archive"); await settle();
  check(!P145.parts(v).includes("Q d-005?"), "folded again");
};

cases["P145-9-done-archived"] = async () => { // I8b: the done section draws its 10 most recent; the rest behind Archived (n)
  world();
  P145.ledger(Array.from({ length: 13 }, (_, i) => tq(`t-${i + 1}`, "done", i + 1)));
  const v = await mount(); await settle();
  await click(v, "view-tasks"); await click(v, "tasks-done");
  let p = P145.parts(v);
  check(p.includes("Task t-13") && p.includes("Task t-4") && !p.includes("Task t-3") && p.includes("10 shown · 3 archived"), `the 10 most recent, the section says 10 shown · 3 archived (t-157) (got ${look(v).text.slice(0, 1200)})`);
  check(/^▸ Archived \(3\)/.test(look(v).text.split(" | ").find((x) => /Archived/.test(x)) ?? "") && Boolean(look(v).clicks["tasks-done-archived"]), `the row (got ${look(v).text.slice(0, 1200)})`);
  await click(v, "tasks-done-archived");
  p = P145.parts(v);
  check(p.includes("Task t-3") && p.includes("Task t-1"), "opened: the rest");
  await click(v, "tasks-done-archived");
  check(!P145.parts(v).includes("Task t-1"), "folded again");
  g.__shared["project-agent.show"] = { space: S, task: "t-1", at: 1 };
  check(P145.parts(v).includes("Task t-1"), "a task shown from another view opens its archived row");
};

cases["P145-10-merged-archived"] = async () => { // I8c: the queue's Merged shows the 10 most recent; the rest behind Archived (n)
  const merged = Array.from({ length: 13 }, (_, i) => ({ task: `t-M${i + 1}`, job: null, tip: sha("e"), from: sha("a"), to: sha("b"), at: `2026-11-18T08:${String(i + 10)}:00Z`, by: "hand" }));
  const v = await queueWorld(qDoc({ [QA]: qRepo([], { merged }) }));
  let text = look(v).text;
  check(/t-M13 \| merged/.test(text) && /t-M4 \| merged/.test(text) && !/t-M3 \| merged/.test(text), `the 10 most recent (got ${text})`);
  check(Boolean(look(v).clicks["queue-merged-archived-0"]) && /▸ Archived \(3\)/.test(text), `the row (got ${text})`);
  await click(v, "queue-merged-archived-0");
  text = look(v).text;
  check(/t-M3 \| merged/.test(text) && /t-M1 \| merged/.test(text) && text.indexOf("t-M3 | merged") < text.indexOf("t-M1 | merged"), `opened: the rest, newest first (got ${text})`);
};

// ---- t-157: the finished headings say shown and archived, not the loaded total ---------------------------------
const P157 = { all: (v, id) => { const e = P145.el(v, id); return e ? inspect(e).text : ""; } };
cases["P157-1-decisions-heading"] = async () => { // R1: 14 answered: 10 shown, 4 archived, the number of the Archived (n) row
  world(); localCards(15, { open: 1 });
  const v = await mount(); await settle();
  check(P145.own(v, "pa-older") === "▸ answered · 10 shown · 4 archived", `folded (got ${P145.own(v, "pa-older")})`);
  await click(v, "pa-older");
  check(P145.own(v, "pa-older") === "▾ answered · 10 shown · 4 archived" && /^▸ Archived \(4\)/.test(P145.own(v, "pa-archive")), `opened (got ${P145.own(v, "pa-older")}; ${P145.own(v, "pa-archive")})`);
  check(!/14 answered/.test(look(v).text), "no loaded total");
};
cases["P157-2-done-heading"] = async () => { // R2: done with 13: 10 shown, 3 archived; no bare total
  world(); P145.ledger(Array.from({ length: 13 }, (_, i) => tq(`t-${i + 1}`, "done", i + 1)));
  const v = await mount(); await settle();
  await click(v, "view-tasks");
  const h = P157.all(v, "tasks-done");
  check(/done \| 10 shown · 3 archived/.test(h) && !/\b13\b/.test(h), `the heading (got ${h})`);
};
cases["P157-4-uncapped-headings"] = async () => { // R1, R2: nothing archived: the headings stay as they were
  world(); localCards(3, { open: 1 });
  P145.ledger(Array.from({ length: 3 }, (_, i) => tq(`t-${i + 1}`, "done", i + 1)));
  const v = await mount(); await settle();
  check(P145.own(v, "pa-older") === "▸ 2 answered", `decisions (got ${P145.own(v, "pa-older")})`);
  await click(v, "view-tasks");
  const h = P157.all(v, "tasks-done");
  check(/done \| 3$/.test(h) && !/shown|archived/.test(h), `done (got ${h})`);
};

// --list prints the names that would run and runs none. An unknown name or option exits 2.
const refuse = (why) => { fs.rmSync(dst, { recursive: true, force: true }); console.error(`run.mjs: ${why}`); process.exit(2); };
const pick = [];
let list = false, shard = null;
for (const arg of process.argv.slice(2)) {
  if (arg === "--list") list = true;
  else if (arg.startsWith("--shard=")) {
    const m = /^--shard=([0-9]+)\/([0-9]+)$/.exec(arg), k = Number(m?.[1]), n = Number(m?.[2]);
    if (!m || n < 1 || k < 1 || k > n) refuse(`bad ${arg} (want --shard=<k>/<n>, 1 <= k <= n)`);
    shard = { k, n };
  } else if (arg.startsWith("-")) refuse(`unknown option ${arg} (usage: run.mjs [--list] [--shard=<k>/<n>] [case ...])`);
  else if (!Object.hasOwn(cases, arg)) refuse(`no case named ${arg}`);
  else pick.push(arg);
}
const chosen = Object.entries(cases).filter(([name], i) => (!pick.length || pick.includes(name)) && (!shard || i % shard.n === shard.k - 1));
if (list) { for (const [name] of chosen) console.log(name); fs.rmSync(dst, { recursive: true, force: true }); process.exit(0); }
let failed = 0;
for (const [name, fn] of chosen) {
  g.__rejected = [];
  let why = null;
  try { await fn(); } catch (e) { why = e instanceof Failed ? e.message : e.stack; }
  // This case's views stop: their delayed reads end, every call they still make fails, and they start nothing new.
  g.__gen = (g.__gen ?? 0) + 1; g.__draining = true; await settle(); await settle();
  // A task of this case that threw (a stale cx, say) fails it; without this it would end the whole run.
  if (!why && g.__rejected.length) why = `a task threw: ${g.__rejected.join("; ")}`;
  if (why) { failed++; console.log(`FAIL ${name}: ${why}`); } else console.log(`PASS ${name}`);
}
fs.rmSync(dst, { recursive: true, force: true });
console.log(failed ? `${failed} case(s) failed` : "all panel cases passed");
process.exit(failed ? 1 : 0);
