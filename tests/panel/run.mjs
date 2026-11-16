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
const cx = { ...baseCx, stop_propagation() {} };
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
function world({ agent = true, agentTerminal = "t30", records = {}, extraPanes = [], extraTerms = [], extraTabs = [], agents = [] } = {}) {
  g.__now = T0;
  g.__runs = []; g.__notes = []; g.__reads = []; g.__writes = []; g.__overlays = []; g.__offline = []; g.__hcs = []; g.__hcFail = []; g.__ls = {};
  g.__machines = [{ name: null, layout: "ready" }];
  g.__spaces = [{ id: S, name: "proj", machine: null, folder: "/proj", active: true, tabs: [{ id: "tb9", index: 1, name: "2", named: false }, ...extraTabs] }];
  g.__panes = [{ pane: "p9", terminal: "t9", tab: "tb9", title: "zsh", cwd: "/proj", status: null, program: shell }, ...extraPanes];
  g.__terminals = [{ address: "t9", state: "live", argv: ["zsh"], labels: {}, status: null, program: shell }, ...extraTerms];
  g.__agents = agents;
  g.__props = {};
  g.__shared = { "project-agent.tabs": records };
  g.__files = {}; g.__fail = []; g.__hang = [];
  if (agent) {
    g.__files[`:${DIR}/agent.json`] = JSON.stringify({ space: S, project: "/proj", profile: "test", name: "proj", terminal: agentTerminal });
    g.__files[`:${DIR}/ledger.json`] = JSON.stringify({ tasks: [] });
    g.__files[`:/home/u/.config/herdr-pa/profiles/test/profile.json`] = JSON.stringify({ name: "test" });
  }
}

async function mount() {
  const v = new ProjectAgent();
  v.init({}, cx); await flush();
  return v;
}
const look = (v) => inspect(v.render(cx));
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

const pick = process.argv.slice(2);
let failed = 0;
for (const [name, fn] of Object.entries(cases)) {
  if (pick.length && !pick.includes(name)) continue;
  try { await fn(); console.log(`PASS ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}: ${e instanceof Failed ? e.message : e.stack}`); }
}
fs.rmSync(dst, { recursive: true, force: true });
console.log(failed ? `${failed} case(s) failed` : "all panel cases passed");
process.exit(failed ? 1 : 0);
