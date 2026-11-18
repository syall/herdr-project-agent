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
async function click(v, id) {
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
  await step(6000); v.reload(cx); await settle(); // 12 s, no progress: load 1 is given up; load 2's agent.json also takes 20 s
  check(reads("agent.json").length === 3, `12 s: load 1 given up, load 2 started (agent.json reads ${reads("agent.json").length})`);
  for (let t = 0; t < 9; t++) await step(1000); // 21 s: load 1 has ended, load 2 has not
  check(/Decisions · 4/.test(look(v).text), `load 1 committed: no newer load had (got ${look(v).text.slice(0, 300)})`);
  // The other order: the newer load commits first; the older one, ending later, commits nothing.
  world({ machine: M });
  g.__files[`${M}:${RDIR}/ledger.json`] = ledgerOf(2);
  remoteCards(3);
  v = await mount(); await settle();
  g.__sleepHang = true; g.__delay[AJ] = 30000;
  v.reload(cx); await settle(); // load A: agent.json answers at 30 s
  await step(12000); delete g.__delay[AJ];
  v.reload(cx); await settle(); // load B: given A up, ends at once
  check(/Decisions · 3/.test(look(v).text) && v.cache.get(RS)?.cards?.length === 3, "load B committed");
  await step(8000); addCard("d-04"); // A will see it when it ends
  for (let t = 0; t < 12; t++) await step(1000); // 32 s: A has ended
  check(reads("agent.json").length === 3, `no other load ran (agent.json reads ${reads("agent.json").length})`);
  check(/Decisions · 3/.test(look(v).text) && v.cache.get(RS)?.cards?.length === 3, `A committed nothing after B (got ${look(v).text.slice(0, 300)})`);
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
  await step(12000); await fire(10000); // 22 s: A has been busy for 12 s: read B, answers at once
  check((g.__shared["project-agent.open"] ?? {})[RS] === 1, `read B: 1 open (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
  g.__batchFail = { [M]: { code: 0, stdout: ["d-01", "d-02", "d-03"].map((c) => `${c}.json\0${openCard(c)}\0`).join("") } }; // what A saw when it was issued
  await step(20000);
  check((g.__shared["project-agent.open"] ?? {})[RS] === 1, `A, ending after B, wrote nothing (got ${JSON.stringify(g.__shared["project-agent.open"])})`);
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
    check(/Tasks · 1/.test(text) && /1 active of 1 tasks/.test(text) && /Decisions · 2/.test(text), `${how}: the last known tasks and cards are shown (got ${text.slice(0, 400)})`);
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
  check(!(RS in (g.__shared["project-agent.worktrees"] ?? {})) && !(RS in (g.__shared["project-agent.workers"] ?? {})) && /0 active of 0 tasks/.test(look(v).text), `a missing ledger clears (got ${JSON.stringify(g.__shared["project-agent.worktrees"])}, ${look(v).text.slice(0, 300)})`);
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
  check(!/schedule\.json is missing/.test(look(v).text) && /Pass queued/.test(look(v).text), `loaded (got ${look(v).text.slice(0, 400)})`);
  g.__hang = [`${M}:${RDIR}/schedule.json`, `${M}:${RDIR}/pass-request.json`];
  advance(12000); v.reload(cx); await settle();
  for (let t = 0; t < 12; t++) await step(1000);
  const { text } = look(v);
  check(!v.loads[RS] && !/schedule\.json is missing/.test(text) && /Pass queued/.test(text), `the schedule and the queued pass are kept (got ${text.slice(0, 400)})`);
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
  await press(v, "sched-summary"); // another control
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
cases["R3-press-elsewhere"] = async () => { // empty space, another Rules control (they stop the press), the schedule line
  const v = await ruleWorld();
  for (const where of [null, "pa-rules", "rule-export", "rule-edit-r-one", "rule-toggle-r-two", "sched-summary"]) {
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
  g.__execFail = [":ls"]; // the card listing stops answering: the last known card stays shown
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
    "By activity", "build ◌", "cache rebuild after idle", "unattributed", "By task", "t-1 One ≈", "in-progress", "t-0 (deleted) Gone", "(no task)", "By session type", "By day (UTC)",
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
  check(text.includes("Tasks:") && /t-1 · 600 tokens · .*7\.50.* est\./.test(text), `activity -> tasks (got ${text.slice(0, 2500)})`);
  await click(v, "costs-activity-2");
  ({ text } = costsView(v));
  check(text.includes("Reasons:") && text.includes("pa-no-journal") && !/t-1 · 600 tokens/.test(text), `unattributed -> reasons, the other closed (got ${text.slice(0, 2500)})`);
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
  for (const id of Object.keys(costsView(v).clicks)) { const c = costsView(v).clicks[id]; if (c) { c({}, cx); await settle(); } }
  await click(v, "costs-reconstruct").catch(() => {});
  for (const id of Object.keys(costsView(v).clicks)) { const c = costsView(v).clicks[id]; if (c) { c({}, cx); await settle(); } }
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
  look(v).clicks["sched-summary"]({}, cx); await flush(); // the schedule opens (its click alone)
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
  check(costsView(v).text.includes("The billing query is off"), `off after Turn off (got ${costsView(v).text.slice(-400)})`);
  h.release(); await settle(); await settle();
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
const J1D_OLD = {
  "default": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-kind": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-role": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | build | 900 tokens | {CUR}11.50 est. · 92% | ▸ | (none) | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-program": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | claude | 1k tokens | {CUR}12.50 est. · 100% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-model": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | model-a | 1k tokens | {CUR}12.50 est. · 100% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "dim-machine": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | this computer | 1k tokens | {CUR}12.50 est. · 100% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "model-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▾ | model-a | 1k tokens | {CUR}12.50 est. · 100% | ◌ aaaa1111 · worker · build · claude · model-a · {clock} – {clock} · {CUR}7.50 est. · closed · reconstructed | bbbb2222 · worker · build · claude · model-a · {clock} – {clock} · {CUR}4.00 est. · gone · recorded | cccc3333 · pa · claude · model-a · {clock} – {clock} · {CUR}1.00 est. · open · recorded | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "task-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▾ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ≈ shared passes split by journal entries | Phases: | build · 900 tokens · {CUR}11.50 est. | Sessions: | ◌ aaaa1111 · worker · build · claude · model-a · {clock} – {clock} · {CUR}7.50 est. · closed · reconstructed | bbbb2222 · worker · build · claude · model-a · {clock} – {clock} · {CUR}4.00 est. · gone · recorded | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "activity-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▾ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | Tasks: | t-1 · 600 tokens · {CUR}7.50 est. | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Copy | ▸ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
  "day-open": "Estimated from test table prices as of 2027-01-01 (XTS) · updated 2 min ago | Refresh | Copy | Total | {CUR}12.50 est. | 1k tokens · 94% cache read | Today | {CUR}1.25 est. | 100 tokens | Last 7 days (UTC) | {CUR}12.50 est. | 1k tokens | ◌ Includes {CUR}2.50 est. reconstructed (best effort) from session files still on disk. | By activity | Copy | ▸ | build ◌ | 600 tokens | {CUR}7.50 est. · 60% | ▸ | cache rebuild after idle | 300 tokens | {CUR}4.00 est. · 32% | ▸ | unattributed | 100 tokens | {CUR}1.00 est. · 8% | By task | Copy | ▸ | t-1 One ≈ | in-progress | 900 tokens | {CUR}11.50 est. · 92% | ▸ | t-0 (deleted) Gone | 0 tokens | {CUR}0.00 est. · 0% | ▸ | (no task) | 100 tokens | {CUR}1.00 est. · 8% | By session type | Copy | kind | role | program | model | machine | ▸ | worker | 900 tokens | {CUR}11.50 est. · 92% | ▸ | pa | 100 tokens | {CUR}1.00 est. · 8% | By day (UTC) | Copy | ▾ | 2027-01-15 | 1k tokens | {CUR}12.50 est. · 100% | build · {CUR}7.50 est. | cache rebuild after idle · {CUR}4.00 est. | unattributed · {CUR}1.00 est. | Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README).",
};
cases["P-costs-j1d-old"] = async () => { // V-J1d-1: a summary without the J1 keys draws exactly as before J1d, in every state
  const s = costsSummary();
  const { v } = await costsWorld({ summary: s });
  const got = await j1dStates(v, s);
  for (const [k, want] of Object.entries(J1D_OLD)) check(got[k] === want, `${k}: the same text as before J1d (got ${got[k]}\nwant ${want})`);
  for (const [k, text] of Object.entries(got)) check(!/By scope|Accounts|priced as|· account /.test(text), `${k}: nothing of J1d (got ${text})`);
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

cases["P-costs-j1d-one"] = async () => { // R-A3: one account: its label beside Total (and in the totals Copy); one scope: no By scope, no tags
  const s = j1dSummary({ byScope: [{ key: j1dKey(J1D_GLOBAL), scope: J1D_GLOBAL, tokens: 1000, money: 12.5, calls: 7, pricedAs: [{ scope: J1D_GLOBAL, validFrom: "2026-01-01" }], by: {} }],
    scopes: 1, byAccount: [{ key: "acct1", tokens: 1000, money: 12.5, calls: 7 }], totals: costsSummary().totals,
    sessions: costsSummary().sessions.map((x) => ({ ...x, ...j1dFacts(J1D_GLOBAL), pricedAs: { scope: J1D_GLOBAL, validFrom: "2026-01-01" } })) });
  const { v } = await costsWorld({ summary: s });
  await click(v, "costs-task-0"); await settle();
  const text = costsView(v).text;
  check(/1k tokens · 94% cache read · account acct1 \|/.test(text), `the account beside Total (got ${text.slice(0, 900)})`);
  check(!/By scope|\| Accounts|priced as|provider-a/.test(text), `one scope, one account: no section, no tag (got ${text})`);
  const copied = await j1dCopy(v, "costs-copy-totals");
  check(/Total: [^\n]* · account acct1/.test(copied), `the totals Copy says it (got ${copied})`);
};
// The text a Copy button puts on the clipboard (the stub's __clipboard): a click on it, then the last write.
async function j1dCopy(v, id) { clipReset(); await press(v, id); await settle(); return lastClip() ?? ""; }

cases["P-costs-j1d-scopes"] = async () => { // A-V3 AC: two or more scopes: By scope, rows summing to the total; ? with its …By; the priced-as note; tags on session lines
  const s = j1dSummary();
  const { v } = await costsWorld({ summary: s });
  let text = costsView(v).text;
  const sc = section(text, "| By scope (provider · endpoint · region · account) | Copy", ["| By day"]);
  check(sc, `a By scope section with Copy (got ${text})`);
  check(sc.includes("provider-a · global · ? (unknown) · acct1"), `the unknown region as ? with its regionBy (got ${sc})`);
  check(sc.includes("provider-b · global · ? (refused) · acct1"), `a refused region (got ${sc})`);
  check(/provider-a · global · \? \(unknown\) · acct1 \| priced as provider-a · global · \* · \* \(from 2026-01-01\) \|/.test(sc), `the default scope's row says priced as (got ${sc})`);
  check(/provider-a · regional · region-a · acct1 \| 300 tokens/.test(sc), `a row priced as its own facts: no note (got ${sc})`);
  check(/provider-b · global · \? \(refused\) · acct1 \| no price \| 100 tokens \| — est\./.test(sc), `no price (got ${sc})`);
  const money = [...sc.matchAll(/(\d+\.\d\d) est\./g)].map((m) => Number(m[1])), toks = [...sc.matchAll(/\| (\d+) tokens/g)].map((m) => Number(m[1]));
  check(Math.abs(money.reduce((a, b) => a + b, 0) - 12.5) < 1e-9 && toks.reduce((a, b) => a + b, 0) === 1000, `the rows sum to the Total (got ${money}, ${toks})`);
  check(!/\| Accounts \|/.test(text) && /account acct1 \|/.test(text), `one account: beside Total, no Accounts (got ${text.slice(0, 900)})`);
  // the drill: models of the scope (from byDayModel), then its sessions
  await click(v, "costs-scope-0"); await settle(); text = costsView(v).text;
  const d = section(text, "| By scope (provider · endpoint · region · account) | Copy", ["| By day"]);
  check(/Models \(from \d{4}-\d{2}-\d{2}\): \| model-a · 600 tokens · [^|]*7\.50[^|]* est\. · priced as provider-a · global · \* · \* \|/.test(d), `the scope's models (got ${d})`);
  check(/Sessions: \| ◌ aaaa1111 /.test(d) && !d.includes("bbbb2222"), `the scope's sessions (got ${d})`);
  const copied = await j1dCopy(v, "costs-copy-scope");
  check(/^By scope[^\n]*\nprovider-a · global · \? \(unknown\) · acct1 · priced as/.test(copied), `the section's Copy (got ${copied})`);
  // session lines are tagged, with the note where it applies
  await click(v, "costs-task-0"); await settle(); text = costsView(v).text;
  check(/aaaa1111 · worker · build · claude · model-a \(provider-a · global · \? · acct1\) · [^|]* est\. · priced as provider-a · global · \* · \* · closed/.test(text), `a tagged session line with its note (got ${text})`);
  check(/bbbb2222 · worker · build · claude · model-a \(provider-a · regional · region-a · acct1\) · [^|]* est\. · gone/.test(text), `no note where priced as its facts (got ${text})`);
  // a model row's drill starts with its scopes
  await click(v, "costs-task-0"); await click(v, "costs-dim-model"); await click(v, "costs-kind-0"); await settle(); text = costsView(v).text;
  const m = section(text, "| By session type", ["| By day"]);
  check(/Scopes \(from \d{4}-\d{2}-\d{2}\): \| model-a \(provider-a · global · \? · acct1\) · 600 tokens · [^|]* est\. · priced as provider-a · global · \* · \* \| model-a \(provider-a · regional · region-a · acct1\) · 300 tokens · [^|]* est\. \| model-a \(provider-b · global · \? · acct1\) · 100 tokens · — est\. · no price \|/.test(m), `the model's scopes (got ${m})`);
  check(moneyLabelled(v).length === 0, `labelled (${JSON.stringify(moneyLabelled(v))})`);
};

cases["P-costs-j1d-accounts"] = async () => { // R-A3 AC: two accounts: an Accounts section summing to the total, its drill; none beside Total
  const s = j1dSummary({ byAccount: [{ key: "acct-a", tokens: 900, money: 9.5, calls: 5 }, { key: "acct-b", tokens: 100, money: 3, calls: 2 }],
    sessions: j1dSummary().sessions.map((x, i) => ({ ...x, account: i === 2 ? "acct-b" : "acct-a" })) });
  const { v } = await costsWorld({ summary: s });
  let text = costsView(v).text;
  check(!/· account /.test(text), `no single account beside Total (got ${text.slice(0, 900)})`);
  const a = section(text, "| Accounts | Copy", ["| By scope", "| By day"]);
  check(/acct-a \| 900 tokens \| [^|]*9\.50[^|]* est\. · 76% \|/.test(a) && /acct-b \| 100 tokens \| [^|]*3\.00[^|]* est\. · 24% \|/.test(a), `one row per account (got ${a})`);
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
  check(!/By scope|\| Accounts|priced as|\[object|rows in|· account /.test(text), `malformed keys draw nothing (got ${text})`);
};

// Which cases run (t-031): every case, or the named ones; --shard=<k>/<n> keeps every n-th case by definition
// order (index mod n == k - 1), so n shards together run each case once, and a case added later lands in one.
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
