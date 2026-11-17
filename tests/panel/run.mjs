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
  g.__remoteDelay = 0; g.__execHang = []; g.__batchFail = {}; g.__issued = []; g.__timers = []; g.__layout = {}; g.__execs = [];
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

cases["P2-worker-chips"] = async () => { // a closed worker reads (closed), a kept one "· kept"; others unchanged
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
  check(/build t31 \(closed\)/.test(text), `closed chip (got ${text.slice(0, 400)})`);
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
    const full = reads("agent.json").length > 0;
    check(full === shown, `${what}: ${shown ? "full load" : "count only"} (agent.json reads ${reads("agent.json").length}, calls ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
    void v;
  }
};

cases["P-hidden-one-call"] = async () => { // AC8.1: hidden: exactly 1 remote call per 10 s interval, the batched card read
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
  const kinds = [...new Set(remoteCalls().map((x) => x.kind))];
  check(JSON.stringify(kinds) === '["batch"]', `only the batched card read (got ${JSON.stringify(remoteCalls().map((x) => `${x.kind}${x.path ? ` ${x.path}` : ""}`).slice(0, 12))})`);
  const per = []; for (let w = 0; w < 6; w++) per.push(remoteCalls().filter((x) => x.at > T0 + w * 10000 && x.at <= T0 + (w + 1) * 10000).length);
  check(per.every((n) => n === 1), `1 call in each 10 s interval (got ${per})`);
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
  check(reads("agent.json").length === 0 && remoteCalls("ls").length === 0, `nothing but the batch (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
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
