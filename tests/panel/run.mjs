// Panel harness (t-003): renders mods/project-agent/main.js against stubbed Herdr data and
// clicks its controls. Usage: node tests/panel/run.mjs [case ...]
// Prints one line per case; exits non-zero when any case fails.
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
let cx = { ...baseCx, stop_propagation() {} }; // a new one per world (world())
const g = globalThis;

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
  cx = stubs.makeCx({ stop_propagation() {} }); g.__draining = false;
  g.__runs = []; g.__notes = []; g.__reads = []; g.__writes = []; g.__overlays = []; g.__offline = []; g.__hcs = []; g.__hcFail = []; g.__ls = {};
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
  const target = route[route.length - 1];
  if (id !== null && target.click) target.click({}, cx);
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

const pick = process.argv.slice(2);
let failed = 0;
for (const [name, fn] of Object.entries(cases)) {
  if (pick.length && !pick.includes(name)) continue;
  try { await fn(); console.log(`PASS ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}: ${e instanceof Failed ? e.message : e.stack}`); }
  // This case's views stop: their delayed reads end, every call they still make fails, and they start nothing new.
  g.__gen = (g.__gen ?? 0) + 1; g.__draining = true; await settle(); await settle();
}
fs.rmSync(dst, { recursive: true, force: true });
console.log(failed ? `${failed} case(s) failed` : "all panel cases passed");
process.exit(failed ? 1 : 0);
