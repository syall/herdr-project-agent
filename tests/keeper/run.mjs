// Keeper harness (t-001, spec §4.2): runs mods/project-agent/keeper.js against stubbed Herdr
// data and checks what it does. Usage: node tests/keeper/run.mjs [case ...]
// Prints one line per case; exits non-zero when any case fails.
import fs from "fs";
import os from "os";
import path from "path";

const here = path.dirname(new URL(import.meta.url).pathname);
const src = path.join(here, "..", "..", "mods", "project-agent");
const dst = fs.mkdtempSync(path.join(os.tmpdir(), "t001-keeper-"));
fs.copyFileSync(path.join(here, "stubs.mjs"), path.join(dst, "stubs.mjs"));
for (const f of ["keeper.js", "store.js", "timer.js"]) fs.writeFileSync(path.join(dst, f), fs.readFileSync(path.join(src, f), "utf8").replace(/from "herdr"/g, 'from "./stubs.mjs"'));
const stubs = await import(path.join(dst, "stubs.mjs"));
const { Keeper } = await import(path.join(dst, "keeper.js"));
const timer = await import(path.join(dst, "timer.js"));
const { cx, flush } = stubs;
const g = globalThis;

const T0 = 1_800_000_000_000; // ms
const S = "sp1";
const ARGV = (space = S) => ["pa-session", "--space", space, "--project", "/proj", "--profile", "test", "--name", "proj"];
const shell = { key: "zsh", name: "zsh", shell: true, agent: false };
const claude = { key: "claude", name: "Claude Code", shell: false, agent: true };
const launcher = { key: "program", name: "Python", shell: false, agent: false };

class Failed extends Error {}
const check = (ok, what) => { if (!ok) throw new Failed(what); };
const records = () => g.__shared["project-agent.tabs"] ?? {};
const runsOf = (...names) => g.__runs.filter(([n]) => names.includes(n));

// One space (local unless `machine`), its PA tab tb1 with pane p1 / terminal t1 running a shell
// that is still labelled as the space's PA, a record for it, agent.json, and a heartbeat.
function world({ machine = null, tabPresent = true, termLive = true, program = shell, status = null, config = "open", record = true, extraPanes = [], extraTerms = [],
  argv = ["pa-session"], title = "zsh", created = undefined, report = undefined, app = undefined, foreground = undefined } = {}) {
  const q = (id) => (machine ? `${machine}/${id}` : id);
  const tb = q("tb1"); // remote tab ids carry the machine prefix, like terminals and panes
  const dir = machine ? `.config/herdr-pa/spaces/${S}` : `/home/u/.config/herdr-pa/spaces/${S}`;
  const sid = machine ? `${machine}/${S}` : S;
  g.__now = T0;
  g.__runs = []; g.__notes = []; g.__reads = []; g.__writes = []; g.__overlays = []; g.__offline = [];
  g.__machines = [{ name: null, layout: "ready" }, ...(machine ? [{ name: machine, layout: "ready" }] : [])];
  const tabs = [...(tabPresent ? [{ id: tb, index: 1, name: "PA: proj", named: true }] : []), { id: q("tb9"), index: tabPresent ? 2 : 1, name: "2", named: false }];
  g.__spaces = [{ id: sid, name: "proj", machine, folder: "/proj", active: true, tabs }];
  g.__panes = [...(tabPresent && termLive ? [{ pane: q("p1"), terminal: q("t1"), tab: tb, title, status, program, app: app ?? null }] : []), ...extraPanes,
    { pane: q("p9"), terminal: q("t9"), tab: q("tb9"), title: "zsh", status: null, program: shell }];
  g.__terminals = [...(termLive ? [{ address: q("t1"), state: "live", argv, ...(created === undefined ? {} : { created_ms: created }), ...(report === undefined ? {} : { report }), ...(foreground === undefined ? {} : { foreground }), labels: { "herdr.pa": S, "herdr.pa.name": "proj", "herdr.pa.project": "/proj", "herdr.pa.profile": "test" }, status, program }] : []),
    ...extraTerms, { address: q("t9"), state: "live", argv: ["zsh"], labels: {}, status: null, program: shell }];
  g.__shared = { "project-agent.tabs": record ? { [tb]: { space: sid, project: "/proj", profile: "test", name: "proj", terminal: q("t1") } } : {} };
  g.__files = {}; g.__fail = []; g.__hang = [];
  const f = (name) => `${machine ?? ""}:${dir}/${name}`;
  if (config === "open") g.__files[f("agent.json")] = JSON.stringify({ space: S, project: "/proj", profile: "test", name: "proj", terminal: "t1" });
  if (config === "closed") g.__files[f("agent.json")] = JSON.stringify({ space: S, project: "/proj", profile: "test", name: "proj", closed: { at: "x", by: "person" } });
  g.__files[f("heartbeat")] = `${T0 / 1000 - 5} t1\n`;
  return { q, f, sid, dir, tb };
}

// Drives the keeper the way main.js does: a heartbeat/config read when the lease is taken and
// every 15 s, syncPA every 1.5 s, and every 250 ms while the keeper says it is busy.
async function start() {
  const k = new Keeper("me");
  k.readBeats(cx); await flush();
  return k;
}
async function sim(k, ms, each = () => {}) {
  for (let t = 0; t < ms; t += 250) {
    g.__now += 250;
    const at = g.__now - T0;
    if (at % 15000 === 0) { k.readBeats(cx); await flush(); }
    if (at % 1500 === 0 || k.busy?.()) { k.syncPA(cx); await flush(); each(); }
  }
}
const until = async (k, ms, cond, each) => { for (let t = 0; t < ms; t += 250) { if (cond()) return true; await sim(k, 250, each); } return cond(); };

const cases = {};

cases["K-1"] = async () => { // machine offline: nothing, record kept
  const w = world({ machine: "m" });
  g.__machines[1].layout = "unavailable"; g.__offline = ["m"];
  const before = JSON.stringify(records());
  const k = await start();
  await sim(k, 90000);
  check(g.__runs.length === 0, `no run calls (got ${JSON.stringify(g.__runs)})`);
  check(JSON.stringify(records()) === before, "record unchanged");
  void w;
};

cases["K-2"] = async () => { // agent.json not read yet: nothing, a read requested
  world({ tabPresent: false, termLive: false });
  const k = new Keeper("me");
  k.syncPA(cx); await flush();
  check(g.__runs.length === 0, `no run calls before agent.json is read (got ${JSON.stringify(g.__runs)})`);
  check(Boolean(records().tb1), "record kept");
  check(g.__reads.some((r) => r.endsWith("/agent.json")), "agent.json read requested");
};

cases["K-3"] = async () => { // no agent.json: no adoption, record dropped
  world({ config: "absent" });
  const k = await start();
  await sim(k, 60000);
  check(g.__runs.length === 0, `no run calls (got ${JSON.stringify(g.__runs)})`);
  check(!records().tb1, "record dropped");
  world({ config: "absent", record: false });
  const k2 = await start();
  await sim(k2, 6000);
  check(Object.keys(records()).length === 0 && g.__runs.length === 0, "labelled terminal not adopted, no rename/pin");
};

cases["K-4"] = async () => { // agent.json closed, tab gone: record dropped, no reopen
  world({ config: "closed", tabPresent: false, termLive: false });
  const k = await start();
  await sim(k, 90000);
  check(runsOf("tab.new", "pane.split").length === 0, `no tab.new / pane.split (got ${JSON.stringify(g.__runs)})`);
  check(!records().tb1, "record dropped");
};

cases["K-5"] = async () => { // healthy: only tab.move when not first
  const w = world({ program: claude, status: "idle" });
  g.__spaces[0].tabs[0].index = 2; g.__spaces[0].tabs[1].index = 1;
  const k = await start();
  let n = 0;
  await sim(k, 90000, () => { g.__files[w.f("heartbeat")] = `${Math.floor(g.__now / 1000)} t1\n`; n++; });
  check(g.__runs.length > 0 && g.__runs.every(([name, a]) => name === "tab.move" && a.id === "tb1" && a.index === 1), `only tab.move tb1 (got ${JSON.stringify(g.__runs.slice(0, 3))})`);
  check(Boolean(records().tb1), "record kept");
};

cases["K-sleep"] = async () => { // reads 5 min apart (the computer slept): never stale
  world();
  const k = new Keeper("me");
  for (let i = 0; i < 4; i++) { g.__now = T0 + i * 300000; k.readBeats(cx); await flush(); k.syncPA(cx); await flush(); }
  g.__now += 1500; k.syncPA(cx); await flush();
  check(runsOf("pane.split", "tab.new", "tab.close", "pane.close").length === 0, `no recovery (got ${JSON.stringify(g.__runs)})`);
};

// The in-place recovery: split beside the shell, adopt the new pane at once, close the shell's
// pane only once the new launcher is labelled and beating. Used by K-6, K-worker, K-remote.
async function recovery({ machine = null, worker = false } = {}) {
  const extraPanes = worker ? [{ pane: "p4", terminal: "t4", tab: "tb1", title: "worker", status: "working", program: claude }] : [];
  const extraTerms = worker ? [{ address: "t4", state: "live", argv: ["claude"], parent: "t1", labels: {}, status: "working", program: claude }] : [];
  const w = world({ machine, extraPanes, extraTerms });
  const keyKept = () => check(Boolean(records()[w.tb]) && Object.keys(records()).length === 1, `record ${w.tb} kept on every tick (got ${JSON.stringify(Object.keys(records()))})`);
  const k = await start();
  const began = g.__now;
  check(await until(k, 50000, () => runsOf("pane.split").length > 0, keyKept), `pane.split within 45 s (runs: ${JSON.stringify(g.__runs)})`);
  check(g.__now - began <= 45000, `detected within 45 s (took ${(g.__now - began) / 1000} s)`);
  const [, split] = runsOf("pane.split")[0];
  check(split.pane === w.q("p1") && split.direction === "right", `split p1 right (got ${JSON.stringify(split)})`);
  check(JSON.stringify(split.argv) === JSON.stringify(ARGV()), `argv pa-session --space ${S} … (got ${JSON.stringify(split.argv)})`);
  check(runsOf("tab.new", "tab.close").length === 0, "no tab.new / tab.close");
  if (machine) check(g.__reads.some((r) => r === `${machine}:${w.dir}/heartbeat`) && g.__reads.some((r) => r === `${machine}:${w.dir}/agent.json`), "remote reads use on: machine and relative paths");
  check(Boolean(records()[w.tb].recovering), "record.recovering set");
  // The new pane appears (not labelled yet).
  g.__panes.push({ pane: w.q("p2"), terminal: w.q("t2"), tab: w.tb, title: "pa-session", status: null, program: launcher });
  const t2 = { address: w.q("t2"), state: "live", argv: ARGV(), labels: {}, status: null, program: launcher };
  g.__terminals.push(t2);
  await sim(k, 250, keyKept);
  check(records()[w.tb].terminal === w.q("t2"), `record.terminal = new terminal on the next fast tick (got ${records()[w.tb].terminal})`);
  check(runsOf("pane.close").length === 0, "old pane not closed before the new launcher beats");
  // It labels itself and beats.
  t2.labels = { "herdr.pa": S, "herdr.pa.name": "proj", "herdr.pa.project": "/proj", "herdr.pa.profile": "test" };
  t2.program = claude; t2.status = "idle";
  g.__files[w.f("heartbeat")] = `${Math.floor(g.__now / 1000)} t2\n`;
  check(await until(k, 15000, () => runsOf("pane.close").length > 0, keyKept), "old pane closed after the new launcher beats");
  const closes = runsOf("pane.close");
  check(closes.length === 1 && closes[0][1].pane === w.q("p1"), `pane.close only the old PA pane (got ${JSON.stringify(closes)})`);
  check(!records()[w.tb].recovering && records()[w.tb].terminal === w.q("t2"), "recovering cleared, record on the new terminal");
  check(g.__notes.some((n) => /restored/i.test(n.title ?? "")), "notice: Project Agent restored");
  // The old pane goes; nothing more happens (no second recovery, no adoption flip-flop).
  g.__panes = g.__panes.filter((p) => p.pane !== w.q("p1"));
  g.__terminals = g.__terminals.filter((t) => t.address !== w.q("t1"));
  const n = g.__runs.length;
  await sim(k, 90000, () => { keyKept(); g.__files[w.f("heartbeat")] = `${Math.floor(g.__now / 1000)} t2\n`; });
  check(g.__runs.slice(n).every(([name]) => name === "tab.move" || name === "terminal.pin"), `quiet afterwards (got ${JSON.stringify(g.__runs.slice(n))})`);
  check(records()[w.tb].terminal === w.q("t2"), "record stays on the new terminal");
  const lock = Object.entries(g.__files).find(([key]) => key.endsWith("/keeper.lock"));
  check(lock && lock[0] === `${machine ?? ""}:${w.dir}/keeper.lock` && JSON.parse(lock[1]).released === true, `keeper.lock taken and released (got ${JSON.stringify(lock)})`);
}
cases["K-6"] = () => recovery();
cases["K-worker"] = () => recovery({ worker: true });
cases["K-remote"] = () => recovery({ machine: "m" });

cases["K-6b"] = async () => { // the PA terminal finished; another pane remains in the tab
  const w = world({ termLive: false, extraPanes: [{ pane: "p3", terminal: "t3", tab: "tb1", title: "zsh", status: null, program: shell }],
    extraTerms: [{ address: "t3", state: "live", argv: ["zsh"], labels: {}, status: null, program: shell }] });
  const k = await start();
  check(await until(k, 20000, () => runsOf("pane.split").length > 0, () => check(Boolean(records().tb1), "record kept")), `pane.split (runs: ${JSON.stringify(g.__runs)})`);
  const [, split] = runsOf("pane.split")[0];
  check(split.pane === "p3" && JSON.stringify(split.argv) === JSON.stringify(ARGV()), `split the remaining pane with pa-session (got ${JSON.stringify(split)})`);
  check(runsOf("tab.new", "tab.close").length === 0, "no tab.new / tab.close");
  g.__panes.push({ pane: "p2", terminal: "t2", tab: "tb1", title: "", status: null, program: launcher });
  const t2 = { address: "t2", state: "live", argv: ARGV(), labels: {}, status: null, program: launcher };
  g.__terminals.push(t2);
  await sim(k, 250);
  check(records().tb1.terminal === "t2", "record.terminal = new terminal");
  t2.labels = { "herdr.pa": S };
  g.__files[w.f("heartbeat")] = `${Math.floor(g.__now / 1000)} t2\n`;
  await until(k, 15000, () => !records().tb1.recovering);
  check(!records().tb1.recovering, "recovery finished");
  check(runsOf("pane.close").length === 0, `no pane.close (p3 is not the PA's) (got ${JSON.stringify(runsOf("pane.close"))})`);
};

cases["K-7"] = async () => { // a hand-started agent in the PA tab, launcher gone: hands off, one notice
  world({ program: claude, status: "idle" });
  const k = await start();
  await sim(k, 120000);
  check(runsOf("pane.split", "pane.close", "tab.new", "tab.close").length === 0, `no pane.* / tab.* (got ${JSON.stringify(g.__runs)})`);
  check(g.__notes.length === 1, `one notice (got ${g.__notes.length}: ${JSON.stringify(g.__notes)})`);
  check(Boolean(records().tb1), "record kept");
};

cases["K-8"] = async () => { // the PA tab vanished: reopened, protected throughout
  const w = world({ tabPresent: false, termLive: false });
  const k = await start();
  check(await until(k, 10000, () => runsOf("tab.new").length > 0, () => check(Boolean(records().tb1), "old record kept until a new tab exists")), `tab.new (runs: ${JSON.stringify(g.__runs)})`);
  const [, made] = runsOf("tab.new")[0];
  check(made.space === w.sid && JSON.stringify(made.argv) === JSON.stringify(ARGV()), `tab.new with pa-session (got ${JSON.stringify(made)})`);
  await sim(k, 1000);
  check(Boolean(records().tb1), "old record kept while the new tab is not there yet");
  g.__spaces[0].tabs.push({ id: "tb2", index: 2, name: "PA: proj", named: true });
  g.__panes.push({ pane: "p2", terminal: "t2", tab: "tb2", title: "", status: null, program: launcher });
  g.__terminals.push({ address: "t2", state: "live", argv: ARGV(), labels: {}, status: null, program: launcher });
  await sim(k, 250);
  check(records().tb2?.terminal === "t2", `new tab recorded within one fast tick (got ${JSON.stringify(records())})`);
  check(!records().tb1, "old record removed once the new one exists");
  check(!Object.values(g.__files).some((t) => /"closed"/.test(t)), "no closed written");
  check(runsOf("tab.close", "pane.close").length === 0, "nothing closed");
};

cases["K-11"] = async () => { // the split never yields a pane: abort, back off, stop after 3
  world();
  const k = await start();
  check(await until(k, 50000, () => runsOf("pane.split").length === 1), "first pane.split");
  await sim(k, 31000, () => check(Boolean(records().tb1), "record kept"));
  check(!records().tb1.recovering, "recovering cleared after 30 s with no new pane");
  check(records().tb1.terminal === "t1", "record back on the old terminal");
  await sim(k, 600000, () => check(Boolean(records().tb1), "record kept"));
  const splits = runsOf("pane.split").length;
  check(splits === 3, `3 attempts in 10 min, no 4th (got ${splits})`);
  check(g.__notes.some((n) => /needs you/i.test(`${n.title} ${n.body}`)), "notice: needs you");
  check(runsOf("tab.new", "tab.close", "pane.close").length === 0, "nothing else touched");
};

cases["K-lock"] = async () => { // another Herdr client holds the lock
  const w = world();
  g.__files[`:${w.dir}/keeper.lock`] = JSON.stringify({ client: "other", at: new Date(T0 + 30000).toISOString(), tab: "tb1" });
  const k = await start();
  await sim(k, 45000);
  check(runsOf("pane.split").length === 0, `no action while another client's lock is fresh (got ${JSON.stringify(g.__runs)})`);
  // Lock written at T0+30 s; at T0+100 s it is 70 s old.
  check(await until(k, 60000, () => runsOf("pane.split").length > 0), "proceeds once the lock is older than 60 s");
  check(g.__now - (T0 + 30000) >= 60000, `not before the lock is 60 s old (at ${(g.__now - T0 - 30000) / 1000} s)`);
  check(JSON.parse(g.__files[`:${w.dir}/keeper.lock`]).client === "me", "lock taken over by this keeper");
};

// ---- review round (REVIEW.md I2, I3, M1-M4, N1; VERIFY.md test gaps) ----
const splits = () => runsOf("pane.split");
const waitSplit = (k, n, ms = 120000) => until(k, ms, () => splits().length >= n);
const newPane = (id, term) => { g.__panes.push({ pane: id, terminal: term.address, tab: "tb1", title: "", status: null, program: term.program }); g.__terminals.push(term); return term; };
const wrapped = (space = S) => ["/bin/sh", "-c", `pa-session --space ${space} --project /proj --profile test --name proj; exec "$shell" -li`];
const noteText = () => g.__notes.map((n) => `${n.title} | ${n.body}`);

cases["K-2b"] = async () => { // agent.json unreadable for many ticks: still nothing
  const w = world({ tabPresent: false, termLive: false });
  g.__fail = [w.f("agent.json")];
  const k = await start();
  await sim(k, 20000);
  check(g.__runs.length === 0, `no run calls over 20 s while agent.json cannot be read (got ${JSON.stringify(g.__runs)})`);
  check(Boolean(records().tb1), "record kept");
  check(g.__reads.filter((r) => r.endsWith("/agent.json")).length >= 2, "agent.json read retried");
  g.__fail = [];
  check(await until(k, 20000, () => runsOf("tab.new").length > 0), "acts once agent.json is read");
};

cases["K-rate"] = async () => { // at most one restore a minute per space
  world();
  const k = await start();
  check(await waitSplit(k, 1, 50000), "first split");
  const first = g.__now;
  const t = newPane("p2", { address: "t2", state: "live", argv: ARGV(), labels: {}, status: null, program: launcher });
  await sim(k, 500);
  t.state = "finished"; g.__panes = g.__panes.filter((p) => p.pane !== "p2");
  check(await waitSplit(k, 2, 120000), "second split");
  check(g.__now - first >= 60000, `second attempt not within a minute of the first (after ${(g.__now - first) / 1000} s)`);
};

cases["K-wait"] = async () => { // the new launcher shows an error and waits for Enter
  world();
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  const t2 = newPane("p2", { address: "t2", state: "live", argv: ARGV(), labels: { "herdr.pa.error": "project folder does not exist: /proj" }, status: null, program: launcher });
  await sim(k, 35000);
  check(records().tb1.terminal === "t2" && records().tb1.recovering?.waiting, `waiting on the new terminal (got ${JSON.stringify(records().tb1)})`);
  const notes = noteText().filter((n) => /project folder does not exist/.test(n));
  check(notes.length === 1, `one notice naming the error (got ${JSON.stringify(noteText())})`);
  check(!/tries again/i.test(notes[0]) && /Enter/.test(notes[0]), `notice says to press Enter, not that it retries (got ${notes[0]})`);
  await sim(k, 300000);
  check(splits().length === 1 && runsOf("pane.close").length === 0, `no other split or close while it waits (got ${JSON.stringify(g.__runs.filter(([n]) => n !== "tab.move"))})`);
  t2.labels = { "herdr.pa": S }; t2.program = claude;
  g.__files[`:/home/u/.config/herdr-pa/spaces/${S}/heartbeat`] = `${Math.floor(g.__now / 1000)} t2\n`;
  check(await until(k, 15000, () => runsOf("pane.close").length > 0), "completes once it runs");
  check(runsOf("pane.close")[0][1].pane === "p1", "closes the old pane");
};

cases["K-wait-labelled"] = async () => { // labelled and beating, but showing an error: not "restored"
  world();
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  const t2 = newPane("p2", { address: "t2", state: "live", argv: ARGV(), labels: { "herdr.pa": S, "herdr.pa.error": "profile command not found: claude" }, status: null, program: launcher });
  g.__files[`:/home/u/.config/herdr-pa/spaces/${S}/heartbeat`] = `${Math.floor(g.__now / 1000)} t2\n`;
  await sim(k, 40000);
  check(!g.__notes.some((n) => /restored/i.test(n.title ?? "")), `no "restored" while herdr.pa.error is set (got ${JSON.stringify(noteText())})`);
  check(runsOf("pane.close").length === 0, "old pane kept while the new one shows an error");
  delete t2.labels["herdr.pa.error"]; t2.program = claude;
  g.__files[`:/home/u/.config/herdr-pa/spaces/${S}/heartbeat`] = `${Math.floor(g.__now / 1000)} t2\n`;
  check(await until(k, 15000, () => runsOf("pane.close").length > 0), "completes when the error clears");
  check(g.__notes.some((n) => /restored/i.test(n.title ?? "")), "then: restored");
};

cases["K-noshell"] = async () => { // the new pane is a plain shell (old pa-session refused, or Ctrl-C): give up, close it, retry
  world();
  const k = await start();
  for (let i = 1; i <= 3; i++) {
    check(await waitSplit(k, i, 150000), `split ${i}`);
    newPane(`p2${i}`, { address: `t2${i}`, state: "live", argv: wrapped(), labels: {}, status: null, program: shell });
    const at = g.__now;
    await sim(k, 35000);
    check(runsOf("pane.close").length === i - 1, `attempt ${i}: a plain shell with no launcher signal is not closed after 35 s (a slow login shell, R2-M3)`);
    check(await until(k, 100000, () => runsOf("pane.close").length >= i), `attempt ${i}: its own plain-shell pane closed (runs ${JSON.stringify(g.__runs.filter(([n]) => n !== "tab.move"))})`);
    check(g.__now - at <= 125000, `attempt ${i}: given up within about 2 minutes (after ${(g.__now - at) / 1000} s)`);
    const close = runsOf("pane.close")[i - 1][1];
    check(close.pane === `p2${i}`, `closes only the pane it made (got ${JSON.stringify(close)})`);
    g.__panes = g.__panes.filter((p) => p.pane !== `p2${i}`); g.__terminals = g.__terminals.filter((t) => t.address !== `t2${i}`);
    check(records().tb1.terminal === "t1" && !records().tb1.recovering, "record back on the old terminal");
  }
  const failNotes = noteText().filter((n) => /not restored/i.test(n));
  check(failNotes.length === 2 && failNotes.every((n) => /plain shell/i.test(n) && /pa-sync/.test(n) && /again in a minute/i.test(n)), `failure notices say why and that it retries (got ${JSON.stringify(noteText())})`);
  check(noteText().some((n) => /needs you/i.test(n)), "third failure: needs you");
  await sim(k, 600000);
  check(splits().length === 3, `no 4th attempt (got ${splits().length})`);
  check(!runsOf("pane.close").some(([, a]) => a.pane === "p1"), "old PA pane never closed");
};

const restores = async (opts, what) => { world(opts); const k = await start(); check(await waitSplit(k, 1, 50000), `${what}: restored (runs ${JSON.stringify(g.__runs)}, notes ${JSON.stringify(noteText())})`); };
const leftAlone = async (opts, what) => {
  world(opts); const k = await start(); await sim(k, 90000);
  check(runsOf("pane.split", "pane.close", "tab.new", "tab.close").length === 0, `${what}: no pane.* / tab.* (got ${JSON.stringify(g.__runs)})`);
  check(g.__notes.length === 1, `${what}: one notice (got ${JSON.stringify(noteText())})`);
};
// R2-I1: restore only on positive evidence of an idle shell; any agent report, or a foreground that is not a shell, is left alone.
cases["K-kept-stale"] = () => leftAlone({ argv: ["/bin/zsh"], program: shell, status: "idle", title: "PA: proj" }, "kept shell with an agent status still set (it may be a live agent: R2-I1)");
cases["K-kept-prompt"] = () => restores({ argv: ["/bin/zsh"], program: shell, status: null, title: "PA: proj", foreground: { name: "zsh", argv: ["-zsh"] } }, "kept shell at its prompt (foreground zsh), dead agent's title");
cases["K-kept-argv"] = () => restores({ argv: ["/bin/zsh"], program: { key: "program", name: "zsh", shell: false, agent: false }, status: null, title: "PA: proj" }, "kept shell whose program table entry only names zsh, no status");
cases["K-wrapper"] = () => restores({ argv: wrapped(), program: shell, status: null, title: "PA: proj" }, "wrapper shell after pa-session exited");
cases["K-nvim-kept"] = () => leftAlone({ argv: ["/bin/zsh"], program: { key: "nvim", name: "Neovim", shell: false, agent: false }, status: null, title: "nvim agent.json" }, "nvim in a kept shell");
cases["K-make-kept"] = () => leftAlone({ argv: ["/bin/zsh"], program: { key: "program", name: "make", shell: false, agent: false }, status: null, title: "make" }, "make (an unlisted program) in a kept shell");
cases["K-idle-agent-kept"] = () => leftAlone({ argv: ["/bin/zsh"], program: shell, status: "done", report: { app: "claude-code", state: "done" }, app: "claude-code", title: "PA: proj" },
  "an idle agent in a kept shell (status done with app set, 'PA: ' title; terminal-shapes.md item 1), even if the program table says zsh");
// Each agent signal alone is enough (the shape is not known per field: terminal-shapes.md).
const leftAloneW = async (change, what) => {
  world({ argv: ["/bin/zsh"], program: shell, status: null, title: "PA: proj" });
  change(g.__terminals.find((t) => t.address === "t1"), g.__panes.find((p) => p.pane === "p1"));
  const k = await start(); await sim(k, 90000);
  check(runsOf("pane.split", "pane.close", "tab.new", "tab.close").length === 0, `${what}: no pane.* / tab.* (got ${JSON.stringify(g.__runs)})`);
};
cases["K-signal-status"] = () => leftAloneW((t) => { t.status = "done"; }, "terminal status only");
cases["K-signal-report"] = () => leftAloneW((t) => { t.report = { app: "claude-code", state: "done" }; }, "agent report only");
cases["K-signal-app"] = () => leftAloneW((t, p) => { p.app = "claude-code"; }, "pane app only");
cases["K-foreground-nvim"] = () => leftAlone({ argv: ["/bin/zsh"], program: shell, status: null, foreground: { name: "nvim", argv: ["nvim", "agent.json"] } }, "the program table says zsh but the foreground process is nvim");
cases["K-close-recheck"] = async () => { // the old pane is closed only if it is still an idle shell when the new launcher beats
  world({ argv: ["/bin/zsh"], program: shell, status: null });
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  const t2 = newPane("p2", { address: "t2", state: "live", argv: ARGV(), labels: {}, status: null, program: launcher });
  await sim(k, 250);
  const old = g.__terminals.find((t) => t.address === "t1");
  old.program = { key: "nvim", name: "Neovim", shell: false, agent: false }; // the person opened an editor in it meanwhile
  g.__panes.find((p) => p.pane === "p1").program = old.program;
  t2.labels = { "herdr.pa": S }; t2.program = claude;
  g.__files[`:/home/u/.config/herdr-pa/spaces/${S}/heartbeat`] = `${Math.floor(g.__now / 1000)} t2\n`;
  await sim(k, 15000);
  check(runsOf("pane.close").length === 0, `old pane not closed while something runs in it (got ${JSON.stringify(runsOf("pane.close"))})`);
  check(!records().tb1.recovering && records().tb1.terminal === "t2", "restore completed onto the new terminal");
  check(g.__notes.some((n) => /left open/i.test(`${n.title} ${n.body}`)), `notice that the old pane was left open (got ${JSON.stringify(noteText())})`);
};
cases["K-slow-start"] = async () => { // a slow start: shell (rc files) for 40 s, then the launcher says it is starting; never closed
  world();
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  const t2 = newPane("p2", { address: "t2", state: "live", argv: wrapped(), labels: {}, status: null, program: shell });
  await sim(k, 40000);
  t2.labels = { "herdr.pa.starting": S }; t2.program = launcher; // pa-session began (hc calls, lock, another launcher's heartbeat)
  await sim(k, 150000);
  check(runsOf("pane.close").length === 0 && splits().length === 1, `a launcher that said it is starting is not closed or replaced (got ${JSON.stringify(g.__runs.filter(([n]) => n !== "tab.move"))})`);
  t2.labels = { "herdr.pa": S }; t2.program = claude;
  g.__files[`:/home/u/.config/herdr-pa/spaces/${S}/heartbeat`] = `${Math.floor(g.__now / 1000)} t2\n`;
  check(await until(k, 15000, () => runsOf("pane.close").length > 0), "completes once it runs");
  check(runsOf("pane.close").every(([, a]) => a.pane === "p1"), "only the old pane closed");
};
cases["K-agent-in-kept"] = () => leftAlone({ argv: ["/bin/zsh"], program: { key: "claude", name: "Claude Code", shell: false, agent: true }, status: "idle", title: "PA: proj" }, "hand-started agent in a kept shell");
cases["K-agent-shell"] = () => leftAlone({ argv: ["/bin/zsh"], program: { key: "claude", name: "Claude Code", shell: true, agent: true }, status: null }, "program.shell true but program.agent true");
cases["K-shell-working"] = () => leftAlone({ argv: wrapped(), program: shell, status: "working" }, "program.shell true but an agent status of working");

cases["K-busy"] = async () => { // the fast tick stops when the machine goes away mid-restore
  world({ machine: "m" });
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  check(k.busy(), "busy during the restore");
  g.__machines[1].layout = "unavailable"; g.__offline = ["m"];
  await sim(k, 2000);
  check(!k.busy(), "not busy while its machine is offline");
};

cases["K-spacing"] = async () => { // heartbeat reads closer than 12 s do not count as comparisons
  const w = world({ created: T0 - 3600000 });
  const k = new Keeper("me");
  for (let t = 0; t < 20000; t += 250) {
    g.__now += 250;
    if (t % 1000 === 0) { k.readBeats(cx); await flush(); }
    k.syncPA(cx); await flush();
  }
  check(splits().length === 0, `no restore from reads 1 s apart within 20 s (got ${JSON.stringify(g.__runs)})`);
  check(await until(k, 45000, () => splits().length > 0), "restored with normal spacing");
  void w;
};

cases["K-8-offline"] = async () => { // a reopen waiting while its machine is offline: no failure, record kept
  const w = world({ machine: "m", tabPresent: false, termLive: false });
  const k = await start();
  check(await until(k, 10000, () => runsOf("tab.new").length > 0), "tab.new");
  g.__machines[1].layout = "unavailable"; g.__offline = ["m"];
  await sim(k, 60000);
  check(Boolean(records()[w.tb]), `old record kept while offline (got ${JSON.stringify(records())})`);
  check(!g.__notes.some((n) => /not restored|needs you/i.test(`${n.title} ${n.body}`)), `no failure while offline (got ${JSON.stringify(noteText())})`);
  g.__machines[1].layout = "ready"; g.__offline = [];
  g.__spaces[0].tabs.push({ id: "m/tb2", index: 2, name: "PA: proj", named: true });
  g.__panes.push({ pane: "m/p2", terminal: "m/t2", tab: "m/tb2", title: "", status: null, program: launcher });
  g.__terminals.push({ address: "m/t2", state: "live", argv: ARGV(), labels: {}, status: null, program: launcher });
  await sim(k, 2000);
  check(records()["m/tb2"]?.terminal === "m/t2" && !records()[w.tb], `new tab recorded once back (got ${JSON.stringify(records())})`);
};

cases["K-foreign-again"] = async () => { // a second "runs something else" episode is told again
  const w = world({ program: claude, status: "idle" });
  const k = await start();
  await sim(k, 60000);
  check(g.__notes.length === 1, "first notice");
  await sim(k, 30000, () => { g.__files[w.f("heartbeat")] = `${Math.floor(g.__now / 1000)} t1\n`; }); // its launcher beats again
  await sim(k, 60000);
  check(g.__notes.length === 2, `told again for the second episode (got ${g.__notes.length})`);
};

// VERIFY round 2 survivors: the fast-tick caps (N8b, N8d).
cases["K-cap-restore"] = async () => { // a launcher that says it is starting, forever: the 250 ms tick stops after ~2 min
  world();
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  newPane("p2", { address: "t2", state: "live", argv: ARGV(), labels: { "herdr.pa.starting": S }, status: null, program: launcher });
  await sim(k, 30000);
  check(k.busy(), "busy early in the restore");
  await sim(k, 100000);
  check(!k.busy() && Boolean(records().tb1.recovering), `not busy after the cap, restore still waiting (busy ${k.busy()})`);
  check(runsOf("pane.close").length === 0, "nothing closed");
};
cases["K-cap-lock"] = async () => { // a lock read that never settles: the fast tick stops, the take is retried
  const w = world();
  g.__hang = [`:${w.dir}/keeper.lock`];
  const k = await start();
  check(await until(k, 50000, () => k.busy()), "busy while taking the lock");
  const n = g.__reads.filter((r) => r.endsWith("/keeper.lock")).length;
  await sim(k, 12000);
  check(!k.busy(), "not busy 10 s into a lock take that never ends");
  await sim(k, 20000);
  check(g.__reads.filter((r) => r.endsWith("/keeper.lock")).length > n, "the lock take is retried");
  check(splits().length === 0, "no restore without the lock");
};

// Round 4: REVIEW round 3 (R3-M1, R3-M2) and VERIFY round 3 (G1).
cases["K-script-kept"] = () => leftAlone({ argv: ["/bin/bash"], program: { key: "shell", name: "bash", shell: true, agent: false }, status: null,
  foreground: { name: "bash", argv: ["bash", "./deploy.sh"] } }, "a shell script (bash ./deploy.sh) running in a kept shell");
cases["K-kept-prompt-flags"] = () => restores({ argv: ["/bin/zsh"], program: shell, status: null, foreground: { name: "zsh", argv: ["-zsh", "-l", "-i"] } }, "kept shell at its prompt, login/interactive flags only");
const deadLauncher = async (labels, what) => { // a launcher that said it began, then died: its terminal is an idle shell again
  world();
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  newPane("p2", { address: "t2", state: "live", argv: wrapped(), labels, status: null, program: shell });
  const at = g.__now;
  check(await until(k, 90000, () => runsOf("pane.close").length > 0), `${what}: given up (runs ${JSON.stringify(g.__runs.filter(([n]) => n !== "tab.move"))}, notes ${JSON.stringify(noteText())})`);
  check(g.__now - at >= 30000 && g.__now - at <= 45000, `${what}: after it has been an idle shell for 30 s (after ${(g.__now - at) / 1000} s)`);
  check(runsOf("pane.close")[0][1].pane === "p2", "closes only its own pane");
  check(records().tb1.terminal === "t1" && !records().tb1.recovering, "record back on the old terminal");
  const note = noteText().find((n) => /not restored/i.test(n)) ?? "";
  check(/stopped/i.test(note) && /again in a minute/i.test(note) && /pa-start/.test(note), `notice says what happened and what to do (got ${note})`);
  g.__panes = g.__panes.filter((p) => p.pane !== "p2"); g.__terminals = g.__terminals.filter((t) => t.address !== "t2");
  check(await waitSplit(k, 2, 120000), `${what}: retried`);
};
cases["K-starting-dead"] = () => deadLauncher({ "herdr.pa.starting": S }, "launcher died after 'starting'");
cases["K-labelled-dead"] = () => deadLauncher({ "herdr.pa": S }, "launcher died after labelling itself");
cases["K-g1"] = async () => { // the 2-minute give-up never closes a new pane that is not an idle shell
  world();
  const k = await start();
  check(await waitSplit(k, 1, 50000), "split");
  newPane("p2", { address: "t2", state: "live", argv: wrapped(), labels: {}, status: null, program: { key: "nvim", name: "Neovim", shell: false, agent: false } });
  check(await until(k, 140000, () => !records().tb1.recovering), "given up after about 2 minutes");
  check(runsOf("pane.close").length === 0, `the new pane is not closed when something runs in it (got ${JSON.stringify(runsOf("pane.close"))})`);
  check(noteText().some((n) => /left open/i.test(n)), `notice says it was left open (got ${JSON.stringify(noteText())})`);
};

// ---- t-003: Open Project Agent requests (shared "project-agent.openAgent"), record-less adoption, alive ----
const REQ = "project-agent.openAgent";
const request = (sid = S, at = g.__now) => { g.__shared[REQ] = { ...(g.__shared[REQ] ?? {}), [sid]: { at, by: "panel" } }; };
const requests = () => g.__shared[REQ] ?? {};
const tabNews = () => runsOf("tab.new");
const beating = (w, address = "t1") => () => { g.__files[w.f("heartbeat")] = `${Math.floor(g.__now / 1000)} ${address}\n`; };
const appear = (machine, tab, pane, term, labels = {}) => {
  const q = (id) => (machine ? `${machine}/${id}` : id);
  g.__spaces[0].tabs.push({ id: q(tab), index: g.__spaces[0].tabs.length + 1, name: "PA: proj", named: true });
  g.__panes.push({ pane: q(pane), terminal: q(term), tab: q(tab), title: "", status: null, program: launcher });
  g.__terminals.push({ address: q(term), state: "live", argv: ARGV(), labels, status: null, program: launcher });
};

async function openFromRequest({ machine = null } = {}) {
  const w = world({ machine, tabPresent: false, termLive: false, record: false });
  request(w.sid);
  const k = await start();
  check(await until(k, 10000, () => tabNews().length > 0), `tab.new after the request (runs ${JSON.stringify(g.__runs)}, reads ${JSON.stringify(g.__reads)})`);
  const [, made] = tabNews()[0];
  check(made.space === w.sid && made.folder === "/proj" && made.name === "PA: proj" && made.focus === true, `tab.new space/folder/name/focus (got ${JSON.stringify(made)})`);
  check(JSON.stringify(made.argv) === JSON.stringify(ARGV()), `argv is the keeper's own pa-session line (got ${JSON.stringify(made.argv)})`);
  check(Boolean(k.pending[w.sid]), "a pending entry for the space");
  check(!requests()[w.sid], `request removed once started (got ${JSON.stringify(requests())})`);
  return { w, k };
}

cases["K-open"] = async () => { await openFromRequest(); };

cases["K-open-norecord"] = async () => { // empty project-agent.tabs: the new tab is recorded on the first fast tick, before it labels
  const { k } = await openFromRequest();
  check(Object.keys(records()).length === 0, "no record before the tab appears");
  await sim(k, 500);
  appear(null, "tb2", "p2", "t2");
  await sim(k, 250);
  check(records().tb2?.terminal === "t2" && records().tb2.space === S && records().tb2.project === "/proj" && records().tb2.profile === "test" && records().tb2.name === "proj",
    `new tab recorded within one fast tick from the request's agent.json (got ${JSON.stringify(records())})`);
  check(!k.pending[S], "pending cleared");
  check(tabNews().length === 1 && runsOf("tab.close", "pane.close", "pane.split").length === 0, `nothing else run (got ${JSON.stringify(g.__runs)})`);
};

cases["K-open-remote"] = async () => { // qualified ids, agent.json read on the machine with a relative path
  const { w, k } = await openFromRequest({ machine: "m" });
  check(g.__reads.includes(`m:.config/herdr-pa/spaces/${S}/agent.json`), `agent.json read on m with a relative path (reads ${JSON.stringify(g.__reads)})`);
  appear("m", "tb2", "p2", "t2");
  await sim(k, 250);
  check(records()["m/tb2"]?.terminal === "m/t2" && records()["m/tb2"].space === w.sid, `remote tab recorded (got ${JSON.stringify(records())})`);
};

cases["K-open-offline"] = async () => { // the space's machine is offline: nothing, request kept (until it expires)
  const w = world({ machine: "m", tabPresent: false, termLive: false, record: false });
  g.__machines[1].layout = "unavailable"; g.__offline = ["m"];
  request(w.sid);
  const k = await start();
  await sim(k, 20000);
  check(tabNews().length === 0, `no tab.new while offline (got ${JSON.stringify(g.__runs)})`);
  check(Boolean(requests()[w.sid]), "request kept while offline");
  await sim(k, 15000);
  check(!requests()[w.sid], "request given up after 30 s");
  check(noteText().some((n) => /offline|not reachable|unreachable/i.test(n)), `a notice says the machine is offline (got ${JSON.stringify(noteText())})`);
  check(tabNews().length === 0, "still no tab.new");
};

const refused = async (w, k, what) => {
  request(w.sid);
  await sim(k, 6000, beating(w));
  check(tabNews().length === 0, `${what}: no tab.new (got ${JSON.stringify(tabNews())})`);
  check(!requests()[w.sid], `${what}: request removed (got ${JSON.stringify(requests())})`);
  check(noteText().some((n) => /already running/i.test(n)), `${what}: notice "already running" (got ${JSON.stringify(noteText())})`);
};

cases["K-open-alive"] = async () => { // a record whose launcher beats in its terminal: refused
  const w = world({ program: claude, status: "idle", created: T0 - 600000 });
  const k = await start();
  await sim(k, 3000, beating(w));
  await refused(w, k, "alive record");
};

cases["K-open-labelled"] = async () => { // no record, a live labelled terminal whose fresh heartbeat names it: refused
  const w = world({ program: claude, status: "idle", record: false, created: T0 - 600000 });
  const k = new Keeper("me");
  await refused(w, k, "labelled terminal");
};

cases["K-open-pending"] = async () => { // a Change-1 reopen (or an in-place restore) under way: no second tab
  const w = world({ tabPresent: false, termLive: false });
  const k = await start();
  check(await until(k, 10000, () => tabNews().length === 1), "the keeper's own reopen");
  request(w.sid);
  await sim(k, 5000);
  check(tabNews().length === 1, `no second tab.new (got ${JSON.stringify(tabNews())})`);
  check(!requests()[w.sid], "request removed");
  const w2 = world();
  const k2 = await start();
  check(await waitSplit(k2, 1, 50000), "an in-place restore starts");
  newPane("p2", { address: "t2", state: "live", argv: ARGV(), labels: {}, status: null, program: launcher });
  await sim(k2, 500);
  check(Boolean(records().tb1.recovering), "restoring");
  request(w2.sid);
  await sim(k2, 5000);
  check(tabNews().length === 0, `no tab.new during a restore (got ${JSON.stringify(tabNews())})`);
  check(!requests()[w2.sid], "request removed");
};

cases["K-open-lock"] = async () => { // another client holds keeper.lock: waits; after 30 s the request goes, with a notice
  const w = world({ tabPresent: false, termLive: false, record: false });
  g.__files[`:${w.dir}/keeper.lock`] = JSON.stringify({ client: "other", at: new Date(T0).toISOString(), tab: "tbX" });
  request(w.sid);
  const k = await start();
  await sim(k, 20000);
  check(tabNews().length === 0, `no tab.new while another client's lock is fresh (got ${JSON.stringify(g.__runs)})`);
  check(Boolean(requests()[w.sid]), "request still waiting");
  await sim(k, 15000);
  check(!requests()[w.sid], "request removed after 30 s");
  check(noteText().some((n) => /lock|another Herdr/i.test(n)), `notice names the lock (got ${JSON.stringify(noteText())})`);
  await sim(k, 60000);
  check(tabNews().length === 0, `nothing opened later (got ${JSON.stringify(tabNews())})`);
};

cases["K-open-absent"] = async () => { // no agent.json: request removed, nothing done
  const w = world({ config: "absent", tabPresent: false, termLive: false, record: false });
  request(w.sid);
  const k = await start();
  await sim(k, 5000);
  check(g.__runs.length === 0, `no run calls (got ${JSON.stringify(g.__runs)})`);
  check(!requests()[w.sid], "request removed");
};

cases["K-open-twice"] = async () => { // a second request right after the first: one tab
  const { w, k } = await openFromRequest();
  g.__now += 10; request(w.sid);
  await sim(k, 5000);
  check(tabNews().length === 1, `one tab.new (got ${JSON.stringify(tabNews())})`);
  check(!requests()[w.sid], "second request removed");
};

cases["K-open-stopped"] = async () => { // the keeper gave up restoring (3 failures): a request still opens a new tab
  world();
  const k = await start();
  check(await until(k, 700000, () => splits().length === 3 && k.stopped[S]), "stopped after 3 failed restores");
  request(S);
  check(await until(k, 10000, () => tabNews().length === 1), `tab.new after the request (runs ${JSON.stringify(g.__runs.filter(([n]) => n !== "tab.move").slice(-3))})`);
  appear(null, "tb2", "p2", "t2");
  await sim(k, 250);
  check(records().tb2?.terminal === "t2" && !records().tb1, `the new tab is the record now (got ${JSON.stringify(records())})`);
};

cases["K-reopen-unchanged"] = async () => { // option A: a vanished recorded tab is still reopened after 4 s, without a request
  const w = world({ tabPresent: false, termLive: false });
  const k = await start();
  const t0 = g.__now;
  check(await until(k, 10000, () => tabNews().length > 0), "tab.new");
  check(g.__now - t0 >= 4000, `not before 4 s (after ${(g.__now - t0) / 1000} s)`);
  check(JSON.stringify(tabNews()[0][1].argv) === JSON.stringify(ARGV()), "pa-session argv");
  check(!Object.values(g.__files).some((t) => /"closed"/.test(t)) && !g.__writes.some((x) => /agent\.json$/.test(x)), "agent.json not written");
  check(g.__shared["project-agent.features"] === undefined, "no project-agent.features");
  void w;
};

cases["K-alive"] = async () => { // alive: true only for a beating record terminal
  let w = world({ program: claude, status: "idle", created: T0 - 600000 });
  let k = await start();
  await sim(k, 20000, beating(w));
  check(records().tb1.alive === true, `alive when its launcher beats in it (got ${JSON.stringify(records().tb1)})`);
  await sim(k, 40000, beating(w, "t7"));
  check(!records().tb1.alive, `not alive when the heartbeat names another terminal (got ${JSON.stringify(records().tb1)})`);
  // During a restore: never alive.
  w = world({ created: T0 - 600000 });
  k = await start();
  let seen = false;
  await sim(k, 60000, () => { const r = records().tb1; if (r?.recovering) { seen = true; check(!r.alive, `not alive while recovering (got ${JSON.stringify(r)})`); } });
  check(seen, "a restore ran");
  // Offline: not alive.
  w = world({ machine: "m", program: claude, status: "idle", created: T0 - 600000 });
  g.__files[w.f("heartbeat")] = `${T0 / 1000 - 5} t1\n`;
  k = await start();
  await sim(k, 20000, beating(w));
  check(records()["m/tb1"].alive === true, `remote record alive (got ${JSON.stringify(records())})`);
  g.__machines[1].layout = "unavailable"; g.__offline = ["m"];
  await sim(k, 3000);
  check(!records()["m/tb1"].alive, `not alive while its machine is offline (got ${JSON.stringify(records())})`);
};

// ---- t-003 R9: passes only to a live launcher terminal (timer.js) ----
const isoAt = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
const SDIR = `/home/u/.config/herdr-pa/spaces/${S}`;
function timerWorld({ alive = true, schedule = { everyMinutes: 5, lastPass: isoAt(T0 - 600000), onlyWhileActive: false }, request = null, extraTerms = [], extraPanes = [], terminal = "t1" } = {}) {
  const w = world({ argv: ARGV(), program: shell, status: null, extraTerms, extraPanes });
  g.__hcs = []; g.__ls = {}; g.__hcFail = [];
  if (terminal !== "t1") g.__shared["project-agent.tabs"].tb1.terminal = terminal;
  if (alive) g.__shared["project-agent.tabs"].tb1.alive = true;
  g.__files[`:${SDIR}/schedule.json`] = JSON.stringify(schedule);
  g.__files[`:${SDIR}/ledger.json`] = JSON.stringify({ tasks: [] });
  if (request) g.__files[`:${SDIR}/pass-request.json`] = JSON.stringify(request);
  return w;
}
const passes = () => g.__hcs.filter((a) => a[0] === "send");

cases["T-alive"] = async () => { // alive record, pass due: sent to the record's terminal
  timerWorld();
  await timer.tick("me");
  check(JSON.stringify(passes()) === JSON.stringify([["send", "t1", "scheduled pass"]]), `scheduled pass to t1 (got ${JSON.stringify(g.__hcs)})`);
};

cases["T-kept-shell"] = async () => { // a labelled kept shell, record not alive: no pass; new-card toasts still come
  timerWorld({ alive: false });
  g.__shared["project-agent.cards"] = { [SDIR]: [] };
  g.__ls[`:${SDIR}/decisions`] = ["d-1.json"];
  g.__files[`:${SDIR}/decisions/d-1.json`] = JSON.stringify({ id: "d-1", status: "open", question: "Which?", options: [{ label: "A" }] });
  await timer.tick("me");
  check(passes().length === 0, `nothing sent into the shell (got ${JSON.stringify(g.__hcs)})`);
  check(g.__notes.some((n) => /Decision needed/.test(n.title)), `card toast posted (got ${JSON.stringify(g.__notes)})`);
};

cases["T-two-labelled"] = async () => { // old and new terminal both labelled during a restore: only the alive record's
  timerWorld({ terminal: "t2", extraPanes: [{ pane: "p2", terminal: "t2", tab: "tb1", title: "", status: "idle", program: claude }],
    extraTerms: [{ address: "t2", state: "live", argv: ARGV(), labels: { "herdr.pa": S, "herdr.pa.name": "proj" }, status: "idle", program: claude }] });
  await timer.tick("me");
  check(JSON.stringify(passes()) === JSON.stringify([["send", "t2", "scheduled pass"]]), `one pass, to t2 (got ${JSON.stringify(g.__hcs)})`);
};

cases["T-manual"] = async () => { // a queued on-demand pass with no alive record: nothing sent, still pending
  timerWorld({ alive: false, request: { pending: true, retro: false, at: isoAt(T0), by: "pa-pass" } });
  await timer.tick("me");
  check(passes().length === 0, `nothing sent (got ${JSON.stringify(g.__hcs)})`);
  check(JSON.parse(g.__files[`:${SDIR}/pass-request.json`]).pending === true, "request still pending");
};

const pick = process.argv.slice(2);
let failed = 0;
for (const [name, fn] of Object.entries(cases)) {
  if (pick.length && !pick.includes(name)) continue;
  try { await fn(); console.log(`PASS ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}: ${e instanceof Failed ? e.message : e.stack}`); }
}
fs.rmSync(dst, { recursive: true, force: true });
console.log(failed ? `${failed} case(s) failed` : "all keeper cases passed");
process.exit(failed ? 1 : 0);
