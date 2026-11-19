// Reads one space's Project Agent state: ~/.config/herdr-pa/spaces/<space-id>/ on the
// space's machine. This mod writes nothing here except decision answers and rule
// changes (later milestones); the orchestrator owns the ledger and the journal.

import { home, shared, panes, terminals } from "herdr";
import { Host, TIMED_OUT, gaveUp, read, write, run, send } from "./host.js";
export { TIMED_OUT, gaveUp };

export const localId = (id) => String(id ?? "").split("/").pop();

// "Open Project Agent" requests: { "<space id>": { at, by, why? } }. The panel writes one, the
// keeper takes it and deletes it (why: what it is still waiting for).
export const OPEN_REQUESTS = "project-agent.openAgent";

// { id, at }: the keeper that wrote the records' alive marks. The marks are trusted only while
// that keeper is the one holding the timer lease, and the lease is fresh: after a reload or a
// takeover the marks are a stale copy's until the new keeper has judged (and rewritten this).
export const KEEPER_MARK = "project-agent.keeper";
const LEASE_STALE = 50000; // timer.js takes the lease over after this much silence
const MARK_STALE = 20000; // the keeper rewrites its mark every 10 s while it runs
export function keeperJudging() {
  const lease = shared("project-agent.timer"), mark = shared(KEEPER_MARK), now = Date.now();
  return Boolean(lease?.id && now - Number(lease.at) < LEASE_STALE && mark?.id === lease.id && now - Number(mark.at) < MARK_STALE);
}

// A launcher that has begun and not finished starting (or waits for Enter on an error).
export const startingLabels = (term) => (term?.labels?.["herdr.pa.starting"] || term?.labels?.["herdr.pa.error"] ? { error: term.labels["herdr.pa.error"] ?? null } : null);

// t-037 J4 (R15): a launcher restarting in place labels its terminal herdr.pa.restarting=<request id> while it ends its
// Claude and execs; pa.py clears it by setting it empty, so "" is absent. The keeper holds such a terminal (never
// restored, never opened again, not marked alive) for at most RESTART_HOLD from when it first saw the label.
export const restartingLabel = (term) => Boolean(term?.labels?.["herdr.pa.restarting"]);
export const RESTART_HOLD = 120000;

// The space's Project Agent terminal that its launcher is known to run in (the keeper's record
// with alive: true, from the current keeper), or null. Messages for the agent go only here.
export function liveTarget(spaceId) {
  if (!keeperJudging()) return null;
  const found = Object.entries(shared("project-agent.tabs") ?? {}).find(([, r]) => r?.space === spaceId && r.alive === true && r.terminal);
  return found ? { tab: found[0], address: found[1].terminal } : null;
}

// A live agent in the space that looks like this Project Agent but does not run under its live
// launcher (run by hand, say): its tab name or pane title is "PA: <name>", its folder is the
// project or inside it, or it still carries the space's label. Workers (terminals with a parent) are not counted. A guess:
// a false match only adds a confirm. Returns { tab, terminal } or null.
export function lookAlike(space, agent) {
  const tabs = new Map((space.tabs ?? []).map((t) => [t.id, t]));
  const ts = terminals(), skip = liveTarget(space.id)?.address;
  const norm = (p) => String(p ?? "").replace(/^~(?=\/|$)/, space.machine ? "~" : home()).replace(/\/+$/, "");
  const project = agent?.project ? norm(agent.project) : null;
  const inProject = (cwd) => Boolean(project && cwd && (norm(cwd) === project || norm(cwd).startsWith(`${project}/`)));
  for (const p of panes()) {
    const tab = tabs.get(p.tab);
    if (!tab || !p.terminal || p.terminal === skip) continue;
    const term = ts.find((t) => t.address === p.terminal);
    if (!term || term.state !== "live" || term.parent) continue;
    if (!(term.program?.agent || p.program?.agent || p.app)) continue;
    const label = term.labels?.["herdr.pa"];
    if (label && label !== localId(space.id)) continue;
    const title = String(p.title ?? ""), name = agent?.name ? `PA: ${agent.name}` : null;
    if (label || (name && (title.startsWith(name) || String(tab.name ?? "").startsWith(name))) || inProject(p.cwd)) return { tab: `${tab.index ?? ""}${tab.name ? ` (${tab.name})` : ""}`.trim(), terminal: p.terminal };
  }
  return null;
}

// On another machine, paths are relative to that machine's home.
export const paHome = (machine) => (machine ? ".config/herdr-pa" : `${home()}/.config/herdr-pa`);
export const stateDir = (space) => `${paHome(space.machine)}/spaces/${localId(space.id)}`;

// A remote read that has not answered by then is given up (a load must not stay stuck).
export const READ_LIMIT = 10000;
export const GRACE = 5000; // an exec still unsettled this long after its own timeout is given up here
// One capped call of a task (t-053, host.js): `start()` makes it; the answer, or a give-up (gaveUp: TIMED_OUT after
// `limit`, BUSY while a call of its key is still pending, SKIPPED over the budget, REFUSED by the runtime). With the
// copy's Host (cx.host, from Host-made task contexts: { host, origin, background, wait, spawn, sleep, progress }) the call is
// keyed `<origin>:<key>`; a cx without one gets a Host of its own (no key, the limit only); no cx: just the call.
// cx.progress() is called when it settles or is given up: the panel's load counts it as progress.
export function capped(cx, start, { limit = READ_LIMIT, key = null, what = "a call", onLate = null } = {}) {
  if (!cx) return start();
  const host = cx.host ?? new Host();
  return host.call(cx, cx.host && key ? `${cx.origin ?? "load"}:${key}` : null, start, { limit, what, background: cx.background !== false, wait: Boolean(cx.wait), onLate });
}
// `p` (already started), or TIMED_OUT after `ms` (no key): kept for callers that hold a started call.
export const withTimeout = (cx, p, ms = READ_LIMIT) => capped(cx ? { ...cx, host: undefined } : cx, () => p, { limit: ms });

// A read's give-up as a result: "timed out" only for a real timeout; BUSY, SKIPPED and REFUSED say nothing here (the
// last known value stays, and a refusal is said once by the panel).
const unanswered = (v) => ({ value: null, text: null, error: v === TIMED_OUT ? "timed out" : null, timedOut: true, ...(v.busy ? { busy: true } : {}), ...(v.refused || v.skipped ? { refused: true } : {}) });
async function readJson(path, on, cx) {
  const text = await capped(cx, () => read(path, { on: on ?? null }), { key: `read:${on ?? ""}:${path}`, what: `the ${path.split("/").pop()} read` });
  if (gaveUp(text)) return unanswered(text);
  if (text === null) return { value: null, text: null, error: null };
  try { return { value: JSON.parse(text), text, error: null }; }
  catch (e) { return { value: null, text, error: String(e.message ?? e) }; }
}

// The Costs view (costs.js): the space's cost summary, written by `pa-costs` on the space's machine.
// Only costs/summary.json is read (never a session file); when it is missing, one listing of costs/
// tells "no records" from "records, first collect still running". Each call capped at READ_LIMIT.
// { summary, error, timedOut, missing: "none" | "pending" | null }: a timed-out read is never "none".
// `live()` (the view's shown check) is asked again before the listing: false -> { left: true }, no ls.
export const costsDir = (space) => `${stateDir(space)}/costs`;
export async function loadCosts(space, cx, live = () => true) {
  const on = space.machine ?? null, r = await readJson(`${costsDir(space)}/summary.json`, on, cx);
  if (r.timedOut) return { summary: null, error: null, timedOut: true, missing: null, ...(r.busy || r.refused ? { busy: true } : {}) };
  if (r.error) return { summary: null, error: r.error, timedOut: false, missing: null };
  if (r.text !== null) {
    const ok = r.value && typeof r.value === "object" && !Array.isArray(r.value);
    return ok ? { summary: r.value, error: null, timedOut: false, missing: null } : { summary: null, error: "not a summary (not a JSON object)", timedOut: false, missing: null };
  }
  if (!live()) return { summary: null, error: null, timedOut: false, missing: null, left: true };
  const ls = await capped(cx, () => run("ls", ["-1", costsDir(space)], { on, timeout: READ_LIMIT }), { key: `ls:${on ?? ""}:${costsDir(space)}`, what: "the costs listing" });
  if (gaveUp(ls)) return { summary: null, error: null, timedOut: true, missing: null, ...(ls.busy || ls.refused || ls.skipped ? { busy: true } : {}) };
  const names = ls.code === 0 ? String(ls.stdout ?? "").split("\n") : [];
  return { summary: null, error: null, timedOut: false, missing: names.includes("sessions.jsonl") ? "pending" : "none" };
}

// The Queue view (queue.js, t-055): the space's queue.json as pa-queue last wrote it (t-052; pa-queue is its only
// writer). One keyed, capped read. { doc, error, missing }: missing when there is no file; error (doc null) when it is
// not JSON, has no "repos" object, or is not version 1. A give-up returns TIMED_OUT (never "missing").
export async function loadQueue(space, cx) {
  const r = await readJson(`${stateDir(space)}/queue.json`, space.machine ?? null, cx);
  if (r.timedOut) return TIMED_OUT;
  if (r.text === null) return { doc: null, error: null, missing: true };
  if (r.error) return { doc: null, error: `queue.json is not valid JSON: ${r.error}`, missing: false };
  const v = r.value;
  if (!v || typeof v !== "object" || Array.isArray(v) || !v.repos || typeof v.repos !== "object" || Array.isArray(v.repos)) return { doc: null, error: 'queue.json needs a "repos" object', missing: false };
  if (v.version !== 1) return { doc: null, error: `queue.json is version ${JSON.stringify(v.version ?? null)}; this panel reads version 1`, missing: false };
  return { doc: v, error: null, missing: false };
}

// Worker places (places.js, t-083 J2): the Project Agent's waiting.json (pa-places is its only writer) and the person's
// start-order.json (this panel is its only writer, writeStartOrder). One keyed, capped read each; a give-up returns
// TIMED_OUT (never "missing"). loadWaiting: { doc, error, missing }, each job checked as pa_places.load_waiting does.
// loadStartOrder: { text, missing }: the raw text, for the compare-and-write; places.js readOrder parses it.
const PLACE_KINDS = ["working", "no-test", "suite"];
export async function loadWaiting(space, cx) {
  const r = await readJson(`${stateDir(space)}/waiting.json`, space.machine ?? null, cx);
  if (r.timedOut) return TIMED_OUT;
  if (r.text === null) return { doc: null, error: null, missing: true };
  if (r.error) return { doc: null, error: `waiting.json is not valid JSON: ${r.error}`, missing: false };
  const v = r.value;
  if (!v || typeof v !== "object" || Array.isArray(v) || !Array.isArray(v.jobs)) return { doc: null, error: 'waiting.json needs a "jobs" list', missing: false };
  const bad = v.jobs.findIndex((j) => !j || typeof j !== "object" || typeof j.id !== "string" || !PLACE_KINDS.includes(j.kind) || !["pa", "queue"].includes(j.source ?? "pa"));
  if (bad >= 0) return { doc: null, error: `waiting.json: job ${bad + 1} needs an id, a kind (${PLACE_KINDS.join(", ")}) and a source (pa or queue)`, missing: false };
  return { doc: v, error: null, missing: false };
}
export async function loadStartOrder(space, cx) {
  const r = await readJson(`${stateDir(space)}/start-order.json`, space.machine ?? null, cx);
  if (r.timedOut) return TIMED_OUT;
  return { text: r.text, missing: r.text === null };
}
// The suite machines' slots (t-119, places.js machinesRows): `pa-full-suite --places --json` on the space's machine,
// the same probe and peek as `pa-full-suite --places` (one hc exec per configured machine: slow, so main.js asks at
// most every MACHINES_EVERY). One keyed, capped call. { doc, error }: error (doc null) on a non-zero exit (its first
// line), output that is not JSON, not version 1 or without machines and local; a give-up returns TIMED_OUT (never "no
// machines"). Read only: the tool takes no slot and writes nothing.
export const MACHINES_TOOL = "pa-full-suite";
export const MACHINES_TIMEOUT = 150000; // each machine's probe has 60 s
const firstLine = (text) => String(text ?? "").split("\n").map((l) => l.trim()).find(Boolean) ?? "";
export async function loadMachines(space, cx) {
  const on = space.machine ?? null;
  let r;
  try { r = await capped(cx, () => run(MACHINES_TOOL, ["--places", "--json"], { on, timeout: MACHINES_TIMEOUT }), { key: `machines:${on ?? ""}`, limit: MACHINES_TIMEOUT + GRACE, what: `${MACHINES_TOOL} --places` }); }
  catch (e) { return { doc: null, error: firstLine(e?.message ?? e) || "the call failed" }; }
  if (gaveUp(r)) return r;
  if (r.code !== 0) return { doc: null, error: firstLine(r.stderr) || firstLine(r.stdout) || `exit ${r.code}` };
  let v;
  try { v = JSON.parse(String(r.stdout ?? "")); } catch { return { doc: null, error: `${MACHINES_TOOL} --places --json: its output is not JSON` }; }
  if (!v || typeof v !== "object" || Array.isArray(v)) return { doc: null, error: `${MACHINES_TOOL} --places --json: not a JSON object` };
  if (v.version !== 1) return { doc: null, error: `${MACHINES_TOOL} --places --json: version ${JSON.stringify(v.version ?? null)}; this panel reads version 1` };
  if (!Array.isArray(v.machines) || !v.local || typeof v.local !== "object") return { doc: null, error: `${MACHINES_TOOL} --places --json: no machines list or local slot` };
  return { doc: v, error: null };
}
// The person's task order (t-097 J2, tasks.js): task-order.json, this panel its only writer (writeTaskOrder). One
// keyed, capped read: { text, missing } (the raw text, for the compare-and-write; tasks.js readTaskOrder parses it),
// or TIMED_OUT on a give-up (never "missing").
export async function loadTaskOrder(space, cx) {
  const r = await readJson(`${stateDir(space)}/task-order.json`, space.machine ?? null, cx);
  if (r.timedOut) return TIMED_OUT;
  return { text: r.text, missing: r.text === null };
}
export const writeTaskOrder = (space, text, expected) => replaceFile(`${stateDir(space)}/task-order.json`, text, expected, space.machine ?? null);
// expected: the text read (null: no file then). A file changed since is refused (replaceFile's "changed on disk").
export const writeStartOrder = (space, text, expected) => replaceFile(`${stateDir(space)}/start-order.json`, text, expected, space.machine ?? null);

// The space's ledger alone ({ value, text, error, timedOut? }), for the hidden panel's refresh.
export const loadLedger = (space, cx) => readJson(`${stateDir(space)}/ledger.json`, space.machine, cx);

// The space's profile alone, for the hidden panel's main branches: agent.json, then config.json when
// neither it nor `profileProp` names one, then the profile. { answered, profile }: answered is false when
// a read failed, timed out or was not valid JSON (try again later); no agent.json: answered, profile
// undefined (nothing to write); a missing profile file: profile null.
export async function loadProfile(space, profileProp, cx) {
  const on = space.machine, root = paHome(on), bad = (r) => r.timedOut || r.error;
  const agent = await readJson(`${stateDir(space)}/agent.json`, on, cx);
  if (bad(agent)) return { answered: false };
  if (!agent.value) return { answered: true, profile: undefined };
  let name = agent.value.profile || profileProp;
  if (!name) { const config = await readJson(`${root}/config.json`, on, cx); if (bad(config)) return { answered: false }; name = config.value?.defaultProfile; }
  if (!name) return { answered: true, profile: null };
  const profile = await readJson(`${root}/profiles/${name}/profile.json`, on, cx);
  return bad(profile) ? { answered: false } : { answered: true, profile: profile.value };
}

// { agent, ledger, profile, errors, unknown } for a space; agent is null when it has no Project Agent
// (timedOut: agent.json did not answer). With `cx`, each read gives up after READ_LIMIT. After
// agent.json, the other reads run at the same time; the error lines keep their order. unknown: the
// parts ("ledger", "profile", "schedule", "request") whose read did not answer with content (it timed
// out or was not valid JSON; for the profile, also config.json when it was needed): their value is
// null here, and the caller must not take it for "none". A missing file is an answer. A thrown read
// rejects the load, as before.
export async function loadSpace(space, profileProp, cx) {
  const on = space.machine, dir = stateDir(space), root = paHome(on);
  const errors = [];
  const agent = await readJson(`${dir}/agent.json`, on, cx);
  if (agent.error) errors.push(`agent.json: ${agent.error}`);
  if (!agent.value) return { agent: null, ledger: null, profile: null, errors, dir, ...(agent.timedOut ? { timedOut: true } : {}), ...(agent.busy ? { busy: true } : {}), ...(agent.refused ? { refused: true } : {}) };
  let name = agent.value.profile || profileProp, nameUnknown = false;
  const profileRead = async () => {
    if (!name) {
      const config = await readJson(`${root}/config.json`, on, cx);
      if (config.timedOut || config.error) nameUnknown = config.error === "timed out" ? "timed out" : true;
      name = config.value?.defaultProfile;
    }
    return name ? readJson(`${root}/profiles/${name}/profile.json`, on, cx) : { value: null };
  };
  const [ledger, profile, schedule, request] = await Promise.all([readJson(`${dir}/ledger.json`, on, cx), profileRead(), readJson(`${dir}/schedule.json`, on, cx), readJson(`${dir}/pass-request.json`, on, cx)]);
  if (ledger.error) errors.push(`ledger.json: ${ledger.error}`);
  if (schedule.error) errors.push(`schedule.json: ${schedule.error}`);
  if (profile.error) errors.push(`profile ${name}: ${profile.error}`);
  if (nameUnknown === "timed out") errors.push("config.json: timed out");
  const unanswered = (r) => Boolean(r.timedOut || r.error);
  const unknown = [["ledger", unanswered(ledger)], ["profile", Boolean(nameUnknown) || unanswered(profile)], ["schedule", unanswered(schedule)], ["request", unanswered(request)]].filter(([, u]) => u).map(([part]) => part);
  return { agent: agent.value, ledger: ledger.value, profile: profile.value, schedule: schedule.value, request: request.value, profileDir: `${root}/profiles/${name}`, errors, dir, unknown };
}

// Fills "{a.b}" from the task. Returns null when a field is missing or unsafe: a value
// may not look like an option, and a ".url" field must be an https link.
export function fill(template, task) {
  let ok = true;
  const out = String(template).replace(/\{([A-Za-z0-9_.]+)\}/g, (_m, path) => {
    const value = path.split(".").reduce((o, k) => (o == null ? o : o[k]), task);
    if (value == null || typeof value === "object") { ok = false; return ""; }
    const text = String(value);
    if (text.startsWith("-") || (/(^|\.)url$/i.test(path) && !/^https:\/\/[^\s]+$/.test(text))) { ok = false; return ""; }
    return text;
  });
  return ok ? out : null;
}

// A profile's declared actions that apply to this task: [{ label, argv, local }]. Two kinds:
//   { "open": "{changeRequest.url}" }   opens a link on this computer (wherever the space lives)
//   { "run": ["cmd", "{field}"] }       runs a command on the space's machine
// An action whose "when" field is empty, or whose {fields} the task can't fill, isn't offered:
// no button without the thing it acts on.
export function taskActions(profile, profileDir, task, platform) {
  const has = (path) => { const v = String(path).split(".").reduce((o, k) => (o == null ? o : o[k]), task); return v != null && v !== "" && v !== false; };
  return (profile?.actions ?? []).filter((a) => a.on === "task" && (!a.when || has(a.when))).map((a) => {
    if (typeof a.open === "string") {
      const url = fill(a.open, task);
      // Only an https link, opened by the system's own opener.
      if (!url || !/^https:\/\/[^\s]+$/.test(url) || platform === "windows") return null;
      return { label: a.label, argv: [platform === "macos" ? "open" : "xdg-open", url], local: true };
    }
    if (!Array.isArray(a.run) || !a.run.length) return null;
    const argv = a.run.map((part) => fill(part, task));
    if (argv.some((x) => x === null)) return null;
    if (/^hooks\//.test(argv[0])) argv[0] = `${profileDir}/${argv[0]}`;
    return { label: a.label, argv, local: false };
  }).filter(Boolean);
}

// Instruction files changed since the agent's session last started: the names, or []. The
// launcher writes session.log at each start, and the agent reads its instructions only then.
// With a cx: capped at READ_LIMIT; a give-up is returned as is (the caller keeps the last known).
export async function staleInstructions(space, profile, cx) {
  const root = paHome(space.machine), script = 'cd "$1" 2>/dev/null || exit 0; [ -f "$2/session.log" ] || exit 0; find core "profiles/$3/context.md" -name "*.md" -newer "$2/session.log" 2>/dev/null';
  try {
    const r = await capped(cx, () => run("sh", ["-c", script, "sh", root, `spaces/${localId(space.id)}`, profile ?? ""], { on: space.machine ?? null, timeout: 10000 }), { key: `stale:${space.machine ?? ""}:${localId(space.id)}`, what: "the instructions check" });
    if (gaveUp(r)) return r;
    return r.stdout.split("\n").filter(Boolean).map((p) => p.split("/").pop());
  } catch { return []; }
}

// ---- t-037 J4: what the Project Agent's launcher runs, and the Restart button (R13, R14) ---------------------------
// launcher.json (written by the launcher only) and installed.json (by install.sh / pa-sync), read on the space's
// machine. codeState is pa.code_state exactly (tests/samples/code-state-cases.json checks both), restartCapable is
// pa.restart_capable. The button runs only the restart tool in place on the space's machine (runRestart): a dry run for
// the confirm, then one run bound to the launcher instance the confirm showed. It never ends a terminal.
const isObj = (v) => Boolean(v) && typeof v === "object" && !Array.isArray(v);
const asObj = (v) => (isObj(v) ? v : {});
const asText = (v) => (typeof v === "string" ? v : null);
export const restartCapable = (rec) => {
  const r = asObj(rec), version = r.version;
  return typeof version === "number" && Number.isInteger(version) && version >= 2 && Array.isArray(r.accepts) && r.accepts.includes("restart");
};
// { word, detail, capable, instance, prompt }: word as pa.code_state (current | older (tools|core|tools, core) | unknown).
export function codeState(launcher, installed, terminal) {
  const capable = restartCapable(launcher), instance = asText(asObj(launcher).instance), prompt = asText(asObj(asObj(launcher).session).prompt);
  const out = (word, detail) => ({ word, detail, capable, instance, prompt });
  if (!isObj(installed) || !asText(installed.tools)) return out("unknown", "no installed.json on this machine (run install.sh or pa-sync)");
  if (!isObj(launcher)) return out("unknown", "started before restart support (no launcher.json)");
  if (terminal && launcher.terminal !== terminal) return out("unknown", `launcher.json is from another terminal (${launcher.terminal})`);
  const tools = asText(asObj(launcher.code).tools), session = asObj(launcher.session), recorded = session.prompt === "recorded";
  const core = asText("promptCore" in session ? session.promptCore : session.core);
  if (!tools || !core) {
    if (tools && recorded) return out("unknown", "the core text this resumed session runs with is not known (a new conversation loads the current one)");
    return out("unknown", `launcher.json has no ${!tools ? "tools" : "core"} hash`);
  }
  const older = [["tools", tools], ["core", core]].filter(([n, a]) => a !== installed[n]).map(([n]) => n);
  if (older.length) return out(`older (${older.join(", ")})`, `runs tools ${tools.slice(0, 12)} core ${core.slice(0, 12)}; installed tools ${String(asText(installed.tools) ?? "").slice(0, 12)} core ${String(asText(installed.core) ?? "").slice(0, 12)}`);
  return out("current", `tools ${tools.slice(0, 12)} core ${core.slice(0, 12)}`);
}
// The header's words: nothing for current (or nothing read yet).
export function codeBadge(state) {
  if (!state) return null;
  if (!state.capable) return "started before restart support";
  if (state.word === "current") return null;
  const m = /^older \((.+)\)$/.exec(state.word);
  return m ? `older code (${m[1]})` : "code unknown";
}
// { launcher, installed } (null where missing or not JSON, as read_json_safe), or a give-up when either did not answer.
export async function loadCode(space, cx) {
  const on = space.machine ?? null;
  const [l, i] = await Promise.all([readJson(`${stateDir(space)}/launcher.json`, on, cx), readJson(`${paHome(on)}/installed.json`, on, cx)]);
  if (l.timedOut) return TIMED_OUT;
  if (i.timedOut) return TIMED_OUT;
  return { launcher: isObj(l.value) ? l.value : null, installed: isObj(i.value) ? i.value : null };
}

export const RESTART_TOOL = "pa-restart";
export const RESTART_WAIT = 180; // seconds the tool waits for the new launcher (its default, spec R13's 3 minutes)
export const RESTART_TIMEOUT = 210000; // ms: its exec's timeout, the wait plus 30 s (an exec still unsettled GRACE later is given up)
export const DRY_TIMEOUT = 30000; // ms
export const RESTART_WHY = "the Restart button in the Project Agent panel"; // recorded only (a Herdr started from a Claude shell needs a why)
export const RESTART_MARK = "project-agent.restarting"; // shared: { "<space id>": { by, at, instance } } while a copy's run is in flight
export const RESTART_MARK_TTL = 240000;
export const RESTARTED_FOR = 60000; // "Restarted: …" stays this long
// The argv after the tool's name: the dry run, or the one run bound to `instance`. null without a usable instance.
export function restartArgs(space, { instance = null, now = false, fresh = false, dry = false } = {}) {
  const base = ["--space", localId(space.id)];
  if (dry) return [...base, "--dry-run"];
  if (typeof instance !== "string" || !instance || instance.startsWith("-")) return null;
  return [...base, "--json", "--wait", String(RESTART_WAIT), "--expect-instance", instance, "--by", "panel", "--why", RESTART_WHY, ...(now ? ["--now"] : []), ...(fresh ? ["--fresh"] : [])];
}
// The line the person copies for a launcher from before restart support (the panel never runs it): J3's form.
export const endTerminalLine = (space) => `${RESTART_TOOL} --space ${localId(space.id)} --end-terminal${space.machine ? ` --on ${space.machine}` : ""}`;
// One exec of the tool on the space's machine (timeout in ms): { r } | { error } | { busy } (one already pending) | { timedOut }.
export async function runRestart(hcx, space, args, timeout) {
  const on = space.machine ?? null;
  try {
    const r = await capped(hcx, () => run(RESTART_TOOL, args, { on, timeout }), { key: `restart:${on ?? ""}:${localId(space.id)}`, limit: timeout + GRACE, what: `${RESTART_TOOL} ${args.includes("--dry-run") ? "--dry-run" : "run"}` });
    if (r === TIMED_OUT) return { timedOut: true };
    if (gaveUp(r)) return { busy: true };
    return { r };
  } catch (e) { return { error: String(e?.message ?? e) }; }
}
const lastJson = (stdout) => {
  for (const line of String(stdout ?? "").split("\n").reverse()) {
    if (!line.trim().startsWith("{")) continue;
    try { const v = JSON.parse(line); if (isObj(v)) return v; } catch { /* not this line */ }
  }
  return null;
};
const toolSaid = (r) => String(r?.stderr || r?.stdout || "").trim().split("\n").filter(Boolean).slice(-3).join(" ").slice(0, 600);
const utcClock = (iso) => { const d = new Date(String(iso ?? "")); return Number.isNaN(d.getTime()) ? "?" : `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`; };
const h7 = (v) => (asText(v) || "unknown").slice(0, 7);
export function restartedLine(launcher) {
  const code = asObj(asObj(launcher).code), session = asObj(asObj(launcher).session), profile = asObj(asObj(launcher).profile);
  const core = "promptCore" in session ? session.promptCore : session.core, commit = asText(code.commit);
  return `Restarted: tools ${h7(code.tools)} (${commit ? commit.slice(0, 7) : "no commit"}), core ${h7(core)}${session.prompt === "recorded" ? " (still the text this conversation began with)" : ""}, profile ${asText(profile.name) ?? "?"} read ${utcClock(profile.readAt)}`;
}
const notInstalled = (space) => (space.machine ? `${RESTART_TOOL} is not installed on ${space.machine}; run pa-sync --on ${space.machine}.` : `${RESTART_TOOL} is not installed here; run ./install.sh.`);
// What runRestart's answer means, as one line: { ok, text, launcher?, unknown? }, or null (busy: nothing to say).
export function restartOutcome(res, space) {
  if (!res || res.busy) return null;
  if (res.timedOut) return { ok: false, unknown: true, text: `The restart's result is not known (${RESTART_TOOL} did not answer in ${(RESTART_TIMEOUT + GRACE) / 1000} s); the header shows what it runs now.` };
  if (res.error !== undefined) return { ok: false, text: missingTool(res.error, RESTART_TOOL) ? notInstalled(space) : `${RESTART_TOOL} could not run: ${String(res.error).slice(0, 300)}` };
  const r = res.r ?? {}, out = lastJson(r.stdout);
  if (r.code === 127 || (r.code !== 0 && missingTool(toolSaid(r), RESTART_TOOL))) return { ok: false, text: notInstalled(space) };
  if (r.code === 0) return out?.outcome === "ok" && isObj(out.launcher) ? { ok: true, text: restartedLine(out.launcher), launcher: out.launcher } : { ok: true, text: "Restarted; the header shows what it runs now." };
  if (out && (out.outcome === "refused" || out.outcome === "failed")) return { ok: false, text: `The restart did not happen: ${asText(out.why) ?? out.outcome}` };
  if (r.code === 2) return { ok: false, text: `Not restarted: ${toolSaid(r) || `exit ${r.code}`}` };
  return { ok: false, text: toolSaid(r) || `${RESTART_TOOL} exited with ${r.code}` };
}

// ---- t-041 J5: the context line (R15), the Fresh context button (R6), the mode switch (R13), the reset line (C6) ----
// context.json is read on the space's machine (the line's numbers come from the Costs view's own summary read); the button and the switch run only the context
// tool there (its `fresh` and `set` actions, with --by panel), never with --on, and the panel writes neither
// context-request.json nor context.json itself (the tool checks the mode, the launcher's accepts and the space).
export const CONTEXT_TOOL = ["pa", "context"].join("-");
export const CONTEXT_WHY = "the Fresh context button in the Project Agent panel"; // recorded in the request only
export const CONTEXT_DEFAULTS = Object.freeze({ v: 1, mode: "fresh", taskCap: 150000, passCap: 250000, net: 400000, enabledAt: null, by: null });
export const CONTEXT_FOR = 60000; // ms a result line stays
export const CONTEXT_TIMEOUT = 30000; // ms: each run's exec timeout (it is given up GRACE later)
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const kTokens = (n) => (isNum(n) ? `${Math.round(n / 1000)}k` : "—"); // pa_costs fmt_ctx
const utcHm = (iso) => { const d = new Date(String(iso ?? "")); return Number.isNaN(d.getTime()) ? "?" : d.toISOString().slice(11, 16); };
// pa.context_capable: a launcher that takes a context request.
export const contextCapable = (rec) => {
  const r = asObj(rec), version = r.version;
  return typeof version === "number" && Number.isInteger(version) && version >= 2 && Array.isArray(r.accepts) && r.accepts.includes("context");
};
// context.json over the defaults (missing: the defaults). Only the mode is checked here; the tool and the launcher
// check the caps. { mode, taskCap, passCap, net, enabledAt, by, problem }.
export function contextSettings(value, problem = null) {
  const out = (p) => ({ ...CONTEXT_DEFAULTS, problem: p });
  if (problem) return out(problem);
  if (value === null || value === undefined) return out(null);
  if (!isObj(value)) return out("not an object");
  if (!["fresh", "off"].includes(value.mode)) return out("mode must be fresh or off");
  const s = { ...CONTEXT_DEFAULTS, mode: value.mode, enabledAt: asText(value.enabledAt), by: asText(value.by), problem: null };
  for (const k of ["taskCap", "passCap", "net"]) if (isNum(value[k])) s[k] = value[k];
  return s;
}
// { settings }: context.json on the space's machine; TIMED_OUT when the read gave up (the caller keeps the last known).
// No costs file is read here: the context line uses the summary the Costs view read (t-009 R11, J5-D1).
export async function loadContext(space, cx) {
  const c = await readJson(`${stateDir(space)}/context.json`, space.machine ?? null, cx);
  if (c.timedOut) return TIMED_OUT;
  return { settings: contextSettings(c.value, c.error ? `not valid JSON: ${c.error}` : null) };
}
// Today's (UTC) paContext entry, checked, or null.
export function contextDay(paContext, now = Date.now()) {
  if (!Array.isArray(paContext)) return null;
  const day = new Date(now).toISOString().slice(0, 10);
  const e = paContext.find((x) => isObj(x) && x.day === day);
  if (!e || !isNum(e.calls) || !isNum(e.peak) || !(e.mean === null || isNum(e.mean)) || !isObj(e.resets) || !isNum(e.resets.fresh) || !isNum(e.resets.compact)) return null;
  return e;
}
// When the summary's figures are from (MF-J5r2-1): its own `updated`, as pa-costs report takes its day from it; the
// Costs view's read time when `updated` is missing or not a valid time.
export function contextAt(summary, readAt) {
  const ms = typeof summary?.updated === "string" ? Date.parse(summary.updated) : NaN;
  return Number.isFinite(ms) ? ms : readAt;
}
// The R15 line (as pa-costs report's), or null when there is no paContext. `at` is when paContext was read: its UTC
// day is the one shown, named when it is not `today`'s (MF-J5-1: a read from yesterday is not today's "no calls").
export function contextLine(paContext, at = Date.now(), today = at) {
  if (!Array.isArray(paContext)) return null;
  const day = new Date(at).toISOString().slice(0, 10), e = contextDay(paContext, at);
  const head = `PA context ${day === new Date(today).toISOString().slice(0, 10) ? "today" : day} (UTC):`;
  const resets = e ? `resets ${e.resets.fresh + e.resets.compact} (fresh ${e.resets.fresh}, compact ${e.resets.compact})` : null;
  if (!e || !e.calls) return e && e.resets.fresh + e.resets.compact ? `${head} no Project Agent calls · ${resets}` : `${head} no Project Agent calls yet`;
  return `${head} mean ${kTokens(e.mean)} · peak ${e.peakExact === false ? "≥" : ""}${kTokens(e.peak)} · ${resets}`;
}
// The argv after the tool's name: the dry run, the request, its withdrawal, or the mode switch (null for another mode).
export function contextArgs(space, { dry = false, withdraw = false, mode } = {}) {
  const id = localId(space.id);
  if (mode !== undefined) return ["fresh", "off"].includes(mode) ? ["set", "--space", id, "--by", "panel", "--mode", mode, "--json"] : null;
  if (dry) return ["fresh", "--space", id, "--dry-run", "--json"];
  return ["fresh", "--space", id, ...(withdraw ? ["--withdraw"] : []), "--by", "panel", "--json", "--why", CONTEXT_WHY];
}
// One exec of the tool on the space's machine: { r } | { error } | { busy } | { timedOut }, as runRestart.
export async function runContext(hcx, space, args, timeout = CONTEXT_TIMEOUT) {
  const on = space.machine ?? null;
  try {
    const r = await capped(hcx, () => run(CONTEXT_TOOL, args, { on, timeout }), { key: `context:${on ?? ""}:${localId(space.id)}`, limit: timeout + GRACE, what: `${CONTEXT_TOOL} ${args[0]}${args.includes("--dry-run") ? " --dry-run" : ""}` });
    if (r === TIMED_OUT) return { timedOut: true };
    if (gaveUp(r)) return { busy: true };
    return { r };
  } catch (e) { return { error: String(e?.message ?? e) }; }
}
const missingLine = (space, tool) => (space.machine ? `${tool} is not installed on ${space.machine}; run pa-sync --on ${space.machine}.` : `${tool} is not installed here; run ./install.sh.`);
// What a run's answer means, as one line: { ok, text }, or null (busy: nothing to say). clock: an ISO time -> "HH:MM".
export function contextOutcome(res, space, clock = utcHm) {
  if (!res || res.busy) return null;
  if (res.timedOut) return { ok: false, text: `${CONTEXT_TOOL} did not answer in ${(CONTEXT_TIMEOUT + GRACE) / 1000} s; the panel shows what it reads next.` };
  if (res.error !== undefined) return { ok: false, text: missingTool(res.error, CONTEXT_TOOL) ? missingLine(space, CONTEXT_TOOL) : `${CONTEXT_TOOL} could not run: ${String(res.error).slice(0, 300)}` };
  const r = res.r ?? {}, out = lastJson(r.stdout);
  if (r.code === 127 || (r.code !== 0 && missingTool(toolSaid(r), CONTEXT_TOOL))) return { ok: false, text: missingLine(space, CONTEXT_TOOL) };
  if (r.code !== 0) return { ok: false, text: `Not done: ${toolSaid(r) || `exit ${r.code}`}` };
  if (out?.outcome === "requested") return { ok: true, text: `Fresh context asked for: it is taken at the Project Agent's next clean point (the request waits until ${clock(out.expires)}).` };
  if (out?.outcome === "withdrawn") return { ok: true, text: "Request withdrawn." };
  if (out?.outcome === "none") return { ok: true, text: "No request was pending." };
  if (out && out.mode === "off") return { ok: true, text: `Context mode off: no fresh contexts from now; the ${kTokens(out.net)} net ends at its next start. Turn fresh contexts on undoes it.` };
  if (out && out.mode === "fresh") return { ok: true, text: `Context mode fresh: fresh contexts at ${kTokens(out.taskCap)} at a task boundary and ${kTokens(out.passCap)} at a pass end from its launcher's next check; the ${kTokens(out.net)} net at its next start. Turn fresh contexts off undoes it.` };
  return { ok: true, text: "Done; the panel shows what it reads next." };
}
// The dry run's JSON line, for the confirm: { ok: true, ctx, source, due, request, expires, reasons } or { ok: false, text }.
export function contextDry(res, space, clock = utcHm) {
  if (!res || res.busy) return null;
  const out = res.r?.code === 0 ? lastJson(res.r.stdout) : null;
  if (out?.outcome === "dry-run") {
    return { ok: true, ctx: isNum(out.ctx) ? out.ctx : null, source: asText(out.source), due: asText(out.due), request: asText(out.request), expires: asText(out.expires),
      reasons: Array.isArray(out.reasons) ? out.reasons.filter((x) => typeof x === "string") : [] };
  }
  const o = contextOutcome(res, space, clock);
  return { ok: false, text: o && !o.ok ? o.text : `${CONTEXT_TOOL} gave no dry-run line: ${toolSaid(res.r) || "nothing"}` };
}
const REASON_WORDS = { "task-cap": "task cap", "pass-cap": "pass cap", person: "asked for", orchestrator: "asked for by the Project Agent" };
// C6: the line while the conversation running now is the one a reset started; { text, previous } or null.
export function resetLine(launcher, sessionId, clock = utcHm) {
  const last = asObj(asObj(launcher).lastReset);
  if (typeof sessionId !== "string" || !sessionId || last.to !== sessionId || typeof last.from !== "string" || !last.from) return null;
  const why = [REASON_WORDS[last.reason] ?? asText(last.reason), isNum(last.ctx) ? kTokens(last.ctx) : null].filter(Boolean).join(", ");
  return { text: `Fresh context at ${clock(last.at)}${why ? ` (${why})` : ""} · previous session ${last.from.slice(0, 8)}…`, previous: last.from };
}
// Q7: the last request's outcome when it was not taken (launcher.json `context`), or null.
export function lastRequestLine(launcher, clock = utcHm) {
  const c = asObj(asObj(launcher).context);
  if (!["expired", "refused"].includes(c.outcome)) return null;
  return `Last request: ${c.outcome} at ${clock(c.at)}${asText(c.why) ? ` (${c.why})` : ""}`;
}

// A task's artifacts as links: [{ label, url }], https only, each once. From the task itself
// ("links": [{ label, url }] and "changeRequest": { id, url }), and from the profile's
// "linkTemplates" ([{ "match": "<regex>", "url": "https://.../{0}" }]): every id the pattern
// finds in the task's title, goal and change-request id becomes a link.
export function taskLinks(profile, task) {
  const out = [], seen = new Set();
  const add = (label, url) => {
    if (typeof url !== "string" || !/^https:\/\/[^\s]+$/.test(url) || seen.has(url) || out.length >= 12) return;
    seen.add(url); out.push({ label: String(label || url.replace(/^https:\/\//, "")).slice(0, 40), url });
  };
  for (const l of Array.isArray(task.links) ? task.links : []) add(l?.label, l?.url);
  const text = [task.title, task.goal, task.changeRequest?.id, ...(Array.isArray(task.links) ? task.links.map((l) => l?.label) : [])].filter((x) => typeof x === "string").join(" ");
  const claimed = new Set();
  for (const t of Array.isArray(profile?.linkTemplates) ? profile.linkTemplates : []) {
    let pattern;
    try { pattern = new RegExp(t.match, "g"); } catch { continue; }
    for (const m of text.matchAll(pattern)) {
      if (claimed.has(m[0])) continue; // the first template that matches an id owns it
      claimed.add(m[0]);
      add(m[0], String(t.url ?? "").replace("{0}", encodeURIComponent(m[0])));
    }
  }
  if (task.changeRequest?.url) add(task.changeRequest.id, task.changeRequest.url);
  return out;
}

// Each task's evidence files that are not empty, newest first: Map(task id -> [file names]).
// A task without such a file is not in the map.
export async function evidenceTasks(space, cx) {
  const script = 'cd "$1" 2>/dev/null || exit 0; for t in */; do [ -d "$t" ] || continue; ls -t "$t" 2>/dev/null | while read -r f; do [ -s "$t$f" ] && [ -f "$t$f" ] && printf "%s\\t%s\\n" "${t%/}" "$f"; done; done';
  const out = new Map();
  try {
    const r = await capped(cx, () => run("sh", ["-c", script, "sh", `${stateDir(space)}/evidence`], { on: space.machine ?? null, timeout: 10000 }), { key: `evidence:${space.machine ?? ""}:${localId(space.id)}`, what: "the evidence listing" });
    if (gaveUp(r)) return r; // with a cx: the caller keeps the last known
    for (const line of r.stdout.split("\n").filter(Boolean)) { const [task, file] = line.split("\t"); if (!out.has(task)) out.set(task, []); out.get(task).push(file); }
  } catch {}
  return out;
}

// The evidence file a decision card is about: the card's own "evidence" (a name or a list),
// else the first evidence file its question or context mentions, else the newest one.
export function cardEvidence(card, files) {
  if (!files?.length) return null;
  const named = [].concat(card?.evidence ?? []).map((f) => String(f).split("/").pop()).find((f) => files.includes(f));
  if (named) return named;
  const text = `${card?.question ?? ""} ${card?.context ?? ""}`;
  const mentioned = files.filter((f) => text.includes(f)).sort((a, b) => text.indexOf(a) - text.indexOf(b))[0];
  // Newest first, preferring something written for reading over a tool's output.
  return mentioned ?? files.find((f) => /\.(md|markdown|txt)$/i.test(f)) ?? files[0];
}

// ---- decision cards (milestone 2) ----------------------------------------------------------------

const CARD_NAME = /^[A-Za-z0-9_.-]+\.json$/;
// At most this many cards in the list (t-061), but never by leaving out a card that is not closed: every open card
// (and every card whose status is something else, or whose file is not a card) is kept whatever the count; only
// answered and withdrawn cards are dropped, the oldest first, and the list's `omitted` says how many.
export const MAX_CARDS = 200;
const closed = (c) => Boolean(c.card) && (c.card.status === "answered" || c.card.status === "withdrawn");
// parseCards' list (open first, newest first) capped as above. The order is kept.
export function capCards(list) {
  const room = Math.max(0, MAX_CARDS - list.filter((c) => !closed(c)).length);
  let seen = 0, omitted = 0;
  const out = list.filter((c) => { if (!closed(c)) return true; if (seen++ < room) return true; omitted += 1; return false; });
  out.omitted = omitted;
  return out;
}

// Cards from [{ name, path, text }] (text null: the file is gone, skipped):
// [{ id, path, text, card | null, error | null }], open first, newest first.
export function parseCards(files) {
  const cards = [];
  for (const { name, path, text, fallback } of files) {
    if (text === null || text === undefined) continue;
    let value = null, error = null;
    try { value = JSON.parse(text); } catch (e) { error = String(e.message ?? e); }
    const valid = value && typeof value === "object" && typeof value.question === "string" && Array.isArray(value.options);
    // A push card's id is its file name (t-028): the panel checks and runs that id, never the file's own `id`.
    // mismatch: its own id when that differs (shown only, never checked or run); dash: a name that reads as an option.
    const fileId = name.replace(/\.json$/, ""), push = Boolean(valid && isPush(value));
    const flags = !push ? {} : { ...(value.id !== undefined && String(value.id) !== fileId ? { mismatch: String(value.id) } : {}), ...(fileId.startsWith("-") ? { dash: true } : {}) };
    cards.push({ id: push ? fileId : value?.id ?? fileId, path, text, card: valid ? value : null, error: error ?? (valid ? null : "not a decision card (needs question and options)"), ...flags, ...(fallback ? { fallback: true } : {}) });
  }
  const when = (c) => String(c.card?.created ?? "");
  return cards.sort((a, b) => Number(b.card?.status === "open") - Number(a.card?.status === "open") || when(b).localeCompare(when(a)) || b.id.localeCompare(a.id));
}

// Every card in one call on the space's machine: for each *.json the name filter takes, in byte
// order, "name\0text\0". The folder goes in as $1, never into the script. Exit 3: no folder.
const CARDS_SCRIPT = 'export LC_ALL=C; cd "$1" 2>/dev/null || exit 3; for f in *.json; do case "$f" in *[!A-Za-z0-9_.-]*) continue ;; esac; [ -f "$f" ] || continue; printf "%s\\0" "$f"; cat -- "$f" 2>/dev/null; printf "\\0"; done';

// [{ name, text }] from the script's output, or null when it cannot be read as such.
function splitBatch(stdout) {
  const parts = String(stdout).split("\0");
  if (parts.length % 2 !== 1 || parts[parts.length - 1] !== "") return null;
  const out = [];
  for (let i = 0; i + 1 < parts.length; i += 2) { if (!CARD_NAME.test(parts[i])) return null; out.push({ name: parts[i], text: parts[i + 1] }); }
  return out;
}
const byteOrder = (a, b) => (a < b ? -1 : a > b ? 1 : 0); // names are ASCII: code units are bytes

// Every card in decisions/, capped (capCards: every open card, MAX_CARDS in all at most): see parseCards. One call reads them all, on this computer or another
// machine (t-053: here too); if that call fails, times out or prints something unreadable, they are read one by one,
// at most CARD_READS at a time, all within READ_LIMIT of the fallback's start.
// Options: cx (each call capped; with the copy's Host, keyed), batchOnly (no fallback: null when the call
// fails), live() (false: the caller no longer wants the result; the fallback is skipped and the
// result is null, which the caller must not take for "no cards"), known (the last known cards,
// [{ path, text }]: a card whose own read times out or fails on the fallback path is that copy, else
// left out; such a card has fallback: true, and the list's `unread` (a Set of paths) names every card not read now,
// t-045), out (an object: out.busy is set when the null came from a call of its kind still pending: say nothing).
// Only exit 0 is a whole batch (exit 3 with no output: no folder); a call killed part way
// can print whole-looking pairs. null also when the listing never answered (the batch gave no whole
// answer and `ls` timed out or failed): unknown, not "no cards". A listing that answered (no folder,
// an empty one) is [].
export const CARD_READS = 8;
// folder (t-054): "decisions-archive" reads the archive pa-task archive fills, on request only (main.js readArchive).
export async function loadDecisions(space, { cx, batchOnly = false, live, known = [], out = {}, folder = "decisions" } = {}) {
  const on = space.machine ?? null, dir = `${stateDir(space)}/${folder === "decisions-archive" ? folder : "decisions"}`;
  let r = null;
  try { r = await capped(cx, () => run("sh", ["-c", CARDS_SCRIPT, "sh", dir], { on, timeout: READ_LIMIT }), { key: `batch:${on ?? ""}:${dir}`, what: "the decision cards read" }); } catch { r = null; }
  if (r?.busy) { out.busy = true; return null; } // the last batch is still pending: skipped, quietly
  if (r && !gaveUp(r) && r.code === 3 && !r.stdout) return []; // no decisions folder: no cards (as a failing ls)
  const files = r && !gaveUp(r) && r.code === 0 ? splitBatch(r.stdout) : null;
  if (files) return capCards(parseCards(files.sort((a, b) => byteOrder(a.name, b.name)).map((f) => ({ ...f, path: `${dir}/${f.name}` }))));
  if (batchOnly || (live && !live())) return null;
  let names = [];
  try {
    const ls = await capped(cx, () => run("ls", ["-1", dir], { on, timeout: READ_LIMIT }), { key: `ls:${on ?? ""}:${dir}`, what: "the decision cards listing" });
    if (ls?.busy) { out.busy = true; return null; }
    if (gaveUp(ls)) return null;
    if (ls.code === 0) names = ls.stdout.split("\n").filter((n) => CARD_NAME.test(n));
  } catch { return null; }
  const last = new Map((known ?? []).filter((c) => c?.path && typeof c.text === "string").map((c) => [c.path, c.text]));
  // Every name is read (t-061); those whose last known copy is answered or withdrawn last, so that within READ_LIMIT
  // the reads that can show an open card come first.
  const knownClosed = (name) => { try { const k = JSON.parse(last.get(`${dir}/${name}`) ?? "null"); return closed({ card: k }); } catch { return false; } };
  const picked = [...names.filter((n) => !knownClosed(n)), ...names.filter(knownClosed)];
  const unread = new Set(), texts = new Array(picked.length), until = Date.now() + READ_LIMIT;
  let next = 0;
  const worker = async () => {
    while (next < picked.length) {
      const i = next++, path = `${dir}/${picked[i]}`, left = until - Date.now();
      let text = TIMED_OUT;
      if (left > 0) { try { text = await capped(cx, () => read(path, { on }), { key: `read:${on ?? ""}:${path}`, limit: left, what: `the ${picked[i]} read` }); } catch { text = TIMED_OUT; } }
      if (gaveUp(text)) unread.add(path);
      texts[i] = gaveUp(text) ? last.get(path) ?? null : text; // a read that hangs or fails: its last known copy, else left out
    }
  };
  await Promise.all(Array.from({ length: Math.min(CARD_READS, picked.length) }, worker));
  const list = capCards(parseCards(picked.map((name, i) => ({ name, path: `${dir}/${name}`, text: texts[i], fallback: unread.has(`${dir}/${name}`) && texts[i] !== null }))));
  list.unread = unread; // t-045: a card's mark is resolved only by a read made now (main.js resolveMarks)
  return list;
}

// Compare-and-write on the space's machine: the file must be unchanged since it was read. Under an exclusive flock
// on the file (t-045), the compare reads the path after the lock: a second writer waits for the first, then finds
// its answer and exits 3. Where no lock can be taken, the compare alone, as before.
export const REMOTE_WRITE = `
import json, os, sys, tempfile
path, expected, text = sys.argv[1], sys.argv[2], sys.argv[3]
lock = None
try:
    import fcntl
    if os.path.exists(path):
        lock = open(path, "rb")
        fcntl.flock(lock, fcntl.LOCK_EX)
except Exception:
    lock = None
if (open(path).read() if os.path.exists(path) else "") != expected: sys.exit(3)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as f: f.write(text)
os.replace(tmp, path)
`;

// Replaces a file only if it still reads `expected` (null: it must not exist yet, here only).
// cx (t-053 round 2, the timer's): the write is keyed `write:<machine>:<path>` and given up after WRITE_LIMIT, never
// skipped (a write is not a background read); a give-up throws. Without cx: as before (clicks, the keeper).
export const WRITE_LIMIT = 20000 + GRACE;
export async function replaceFile(path, text, expected, on, cx = null) {
  const name = path.split("/").pop();
  const opts = { key: `write:${on ?? ""}:${path}`, limit: WRITE_LIMIT, what: `the ${name} write` }, wcx = cx ? { ...cx, background: false, wait: false } : null;
  const given = (v) => { if (gaveUp(v)) throw new Error(v.busy ? `an earlier ${name} write is still waiting` : v.refused ? `the ${name} write was refused (too many calls waiting)` : `the ${name} write did not answer`); return v; };
  if (!on) {
    try { given(await capped(wcx, () => write(path, text, expected), opts)); }
    catch (e) { throw new Error(/stale|match|changed/i.test(String(e.message ?? e)) ? "The file changed on disk while you were editing; reloaded, try again." : String(e.message ?? e)); }
    return;
  }
  const r = given(await capped(wcx, () => run("python3", ["-c", REMOTE_WRITE, path, expected ?? "", text], { on, timeout: 20000 }), opts));
  if (r.code !== 0) throw new Error(r.code === 3 ? "The file changed on disk while you were editing; reloaded, try again." : r.stderr || "Could not write the file.");
}

// A push card (t-007b): it has a push object, or kind "push". Its answer runs pa-push-main (push.js), which
// writes the answer itself; answerDecision never writes one.
export const isPush = (card) => Boolean(card && typeof card === "object" && ((card.push && typeof card.push === "object") || card.kind === "push"));

// t-046: every card also takes an answer in the person's own words. It is written `answer: "Other", other: true`
// with the words in `note`; no tool acts on it. isOther is pa.is_other exactly (wide: the marker, or the label in any
// case, even on a card with its own option of that name), so it can only make a tool do less.
export const OTHER = "Other", OTHER_MAX = 2000;
export const OTHER_TOO_LONG = `Your answer is longer than ${OTHER_MAX} characters; shorten it, or write it in a file and give the path.`;
export const isOther = (card) => Boolean(card && typeof card === "object" && (card.other === true || String(card.answer ?? "").trim().toLowerCase() === "other"));
// The words for a notice typed by hc send: one line (every whitespace run one space), cut at `max` with a tail
// (as the tools cut theirs at 200). The card keeps them whole.
export const oneLine = (words, max = 400) => {
  const s = String(words ?? "").replace(/\s+/g, " ").trim();
  return s.length > max ? `${s.slice(0, max - 1)}… (the rest is on the card)` : s;
};

// Writes the person's answer into the card file. The only fields this mod ever changes. A push card (as
// read from disk now) is refused: only pa-push-main run, from its own Submit, answers it. A card that is not open
// (as read now) is refused, and the write is a compare-and-write against that open copy: an answer already written
// is never changed. Returns { card, before, after }: the answered card, the file text it replaced, the text written.
// label OTHER: `note` is the answer (trimmed, required, at most OTHER_MAX); a listed label gets no `other` key.
export async function answerDecision(space, entry, label, note) {
  const on = space.machine ?? null;
  const now = await read(entry.path, { on });
  if (now === null) throw new Error("The card file is gone.");
  let card;
  try { card = JSON.parse(now); } catch { throw new Error("The card file is not valid JSON; fix it by hand."); }
  if (isPush(card)) throw new Error("A push card is answered with its own Submit; its answer runs pa-push-main.");
  if (card.status !== "open") throw new Error("This card was already answered.");
  const stamp = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  let next;
  if (label === OTHER) {
    const words = String(note ?? "").trim();
    if (!words) throw new Error("An Other answer needs your own words.");
    if (words.length > OTHER_MAX) throw new Error(OTHER_TOO_LONG);
    next = { ...card, status: "answered", answer: OTHER, other: true, note: words, answered: stamp };
  } else {
    if (!(card.options ?? []).some((o) => o.label === label)) throw new Error("That option is no longer on the card.");
    next = { ...card, status: "answered", answer: label, note: note || null, answered: stamp };
  }
  const after = `${JSON.stringify(next, null, 2)}\n`;
  await replaceFile(entry.path, after, now, on);
  return { card: next, before: now, after };
}

// An exec's rejection that says `program` is not there (t-028): a not-found text that names the program and is not
// about the machine ("machine m1 not found", an unreachable machine: those are not a missing tool).
const NOT_FOUND = /not found|no such file|ENOENT|cannot (start|find)/i;
export const missingTool = (why, program) => NOT_FOUND.test(String(why)) && String(why).includes(program) && !/\bmachine\b|unreachable/i.test(String(why));

// Tells the orchestrator at `address` (machine-qualified, from liveTarget: chosen when sending).
// hc holds a message while someone is typing there, and refuses while the agent is blocked on a
// question; the caller keeps the notice and tries again later. With `cx` (the sending task's), a send
// that has not answered in SEND_LIMIT is given up (t-028): hc waits up to 60 s for a draft, so this is
// longer. Giving up stops only the waiting: the text may still be typed, so a retry can repeat it. With the copy's
// Host (t-053) no second send to an address starts while one is still pending there: the error has `busy: true`
// (the caller keeps the notice and says nothing new).
export const SEND_LIMIT = 90000;
export async function notifyOrchestrator(address, message, cx) {
  const r = await capped(cx, () => send(["send", address, message]), { key: `hc:${address}`, limit: SEND_LIMIT, what: "the hc send" });
  if (r?.busy) throw Object.assign(new Error("hc send: still waiting on the last send to this agent"), { busy: true });
  if (r === TIMED_OUT) throw new Error(`hc send did not answer in ${SEND_LIMIT / 1000} s; it may still arrive`);
  if (gaveUp(r)) throw new Error("hc send was not started: too many calls waiting; it is tried again later");
  if (r.code !== 0) throw new Error((r.stderr || r.stdout || "hc send failed").trim().slice(0, 300));
}

// ---- rules (milestone 3) -------------------------------------------------------------------------

export const RULE_STATUS = ["proposed", "active", "off", "declined"];
const stamp = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

// { path, text, rules, error }: error set (and rules empty) when the file is invalid. An invalid
// file is shown as such and never written over.
// With a cx (a load's): capped; a give-up returns TIMED_OUT (the caller keeps the last known).
export async function loadRules(space, cx) {
  const on = space.machine ?? null, path = `${stateDir(space)}/rules.json`;
  const r = await readJson(path, on, cx);
  if (r.timedOut) return TIMED_OUT;
  const { value, text, error } = r;
  if (text === null) return { path, text: null, rules: [], error: "rules.json is missing" };
  if (error) return { path, text, rules: [], error: `rules.json is not valid JSON: ${error}` };
  const rules = value?.rules;
  if (!Array.isArray(rules)) return { path, text, rules: [], error: 'rules.json needs a "rules" list' };
  const seen = new Set(), problems = [];
  rules.forEach((r, i) => {
    if (!r || typeof r !== "object" || typeof r.id !== "string" || !r.id) problems.push(`rule ${i + 1} has no id`);
    else if (seen.has(r.id)) problems.push(`two rules share the id ${r.id}`);
    else if (typeof r.text !== "string") problems.push(`${r.id} has no text`);
    else if (!RULE_STATUS.includes(r.status)) problems.push(`${r.id} has status ${JSON.stringify(r.status)}`);
    if (r?.id) seen.add(r.id);
  });
  if (problems.length) return { path, text, rules: [], error: `rules.json: ${problems.slice(0, 3).join("; ")}`, doc: value }; // t-133: doc for places.js limitsOf, as pa-places reads it
  return { path, text, rules: [...rules].sort((a, b) => (b.order ?? 0) - (a.order ?? 0)), error: null, doc: value };
}

// Applies `change(rules)` (returns the new list) to the file as it is on disk now, so an edit made
// by hand or by the orchestrator since the last load is kept. `what` goes into each touched rule's history.
export async function changeRules(space, change) {
  const fresh = await loadRules(space);
  if (fresh.error) throw new Error(`${fresh.error}. Fix the file by hand; it was not changed.`);
  const next = change(fresh.rules.map((r) => ({ ...r })));
  const doc = { ...fresh.doc, rules: next };
  await replaceFile(fresh.path, `${JSON.stringify(doc, null, 2)}\n`, fresh.text, space.machine ?? null);
}

export const touched = (rule, changeText) => ({ ...rule, history: [...(rule.history ?? []), { at: stamp(), by: "person", change: changeText }] });

// Order values that put `id` just before `beforeId` (or last when null). Higher order wins.
export function reorder(rules, id, beforeId) {
  const list = rules.filter((r) => r.id !== id), moved = rules.find((r) => r.id === id);
  if (!moved) return rules;
  const at = beforeId ? list.findIndex((r) => r.id === beforeId) : -1;
  list.splice(at < 0 ? list.length : at, 0, touched(moved, "reordered"));
  return list.map((r, i) => ({ ...r, order: (list.length - i) * 10 }));
}

export const newRuleId = (rules, title) => {
  const base = `r-${String(title).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40).replace(/-$/, "") || "rule"}`; // the cut can end on a hyphen
  let id = base, n = 2;
  while (rules.some((r) => r.id === id)) id = `${base}-${n++}`;
  return id;
};

export async function exportRules(space, rules) {
  const dir = `${paHome(space.machine)}/exports`, path = `${dir}/rules-${localId(space.id).slice(-8)}-${stamp().replace(/[:]/g, "")}.json`;
  await run("mkdir", ["-p", dir], { on: space.machine ?? null });
  await replaceFile(path, `${JSON.stringify({ version: 1, rules: rules.map(({ history, needsAttention, ...r }) => r) }, null, 2)}\n`, null, space.machine ?? null);
  return path;
}

// Rules from an exported file that this space doesn't have yet, as active rules of the person's. Returns how many
// were added; their ids are pushed onto `added` (t-101: the "rules changed" notice names them).
export async function importRules(space, path, added = []) {
  const { value, error } = await readJson(path.replace(/^~(?=\/)/, space.machine ? "." : home()), space.machine ?? null);
  if (error || !Array.isArray(value?.rules)) throw new Error(error ? `Not valid JSON: ${error}` : "No file with a \"rules\" list at that path.");
  const ids = [];
  await changeRules(space, (rules) => {
    ids.length = 0;
    for (const r of value.rules) {
      if (typeof r?.id !== "string" || !r.id || typeof r.text !== "string" || rules.some((x) => x.id === r.id)) continue; // t-085: a string id only
      rules.push(touched({ ...r, status: RULE_STATUS.includes(r.status) && r.status !== "proposed" ? r.status : "active", source: "import", order: 0 }, "imported"));
      ids.push(r.id);
    }
    return rules.sort((a, b) => (b.order ?? 0) - (a.order ?? 0)).map((r, i, all) => ({ ...r, order: (all.length - i) * 10 }));
  });
  added.push(...ids);
  return ids.length;
}
