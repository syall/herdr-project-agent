// Reads one space's Project Agent state: ~/.config/herdr-pa/spaces/<space-id>/ on the
// space's machine. This mod writes nothing here except decision answers and rule
// changes (later milestones); the orchestrator owns the ledger and the journal.

import { home, readTextFile, writeTextFile, exec, hc, shared, panes, terminals } from "herdr";

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

async function readJson(path, on) {
  const text = await readTextFile(path, { on: on ?? null });
  if (text === null) return { value: null, text: null, error: null };
  try { return { value: JSON.parse(text), text, error: null }; }
  catch (e) { return { value: null, text, error: String(e.message ?? e) }; }
}

// { agent, ledger, profile, errors } for a space; agent is null when it has no Project Agent.
export async function loadSpace(space, profileProp) {
  const on = space.machine, dir = stateDir(space), root = paHome(on);
  const errors = [];
  const agent = await readJson(`${dir}/agent.json`, on);
  if (agent.error) errors.push(`agent.json: ${agent.error}`);
  if (!agent.value) return { agent: null, ledger: null, profile: null, errors, dir };
  const ledger = await readJson(`${dir}/ledger.json`, on);
  if (ledger.error) errors.push(`ledger.json: ${ledger.error}`);
  let name = agent.value.profile || profileProp;
  if (!name) name = (await readJson(`${root}/config.json`, on)).value?.defaultProfile;
  const profile = name ? await readJson(`${root}/profiles/${name}/profile.json`, on) : { value: null };
  const schedule = await readJson(`${dir}/schedule.json`, on);
  if (schedule.error) errors.push(`schedule.json: ${schedule.error}`);
  const request = await readJson(`${dir}/pass-request.json`, on);
  if (profile.error) errors.push(`profile ${name}: ${profile.error}`);
  return { agent: agent.value, ledger: ledger.value, profile: profile.value, schedule: schedule.value, request: request.value, profileDir: `${root}/profiles/${name}`, errors, dir };
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
export async function staleInstructions(space, profile) {
  const root = paHome(space.machine), script = 'cd "$1" 2>/dev/null || exit 0; [ -f "$2/session.log" ] || exit 0; find core "profiles/$3/context.md" -name "*.md" -newer "$2/session.log" 2>/dev/null';
  try {
    const r = await exec("sh", ["-c", script, "sh", root, `spaces/${localId(space.id)}`, profile ?? ""], { on: space.machine ?? null, timeout: 10000 });
    return r.stdout.split("\n").filter(Boolean).map((p) => p.split("/").pop());
  } catch { return []; }
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
export async function evidenceTasks(space) {
  const script = 'cd "$1" 2>/dev/null || exit 0; for t in */; do [ -d "$t" ] || continue; ls -t "$t" 2>/dev/null | while read -r f; do [ -s "$t$f" ] && [ -f "$t$f" ] && printf "%s\\t%s\\n" "${t%/}" "$f"; done; done';
  const out = new Map();
  try {
    const r = await exec("sh", ["-c", script, "sh", `${stateDir(space)}/evidence`], { on: space.machine ?? null, timeout: 10000 });
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

// Every card in decisions/: [{ id, path, text, card | null, error | null }], open first, newest first.
export async function loadDecisions(space) {
  const on = space.machine ?? null, dir = `${stateDir(space)}/decisions`;
  let names = [];
  try {
    const r = await exec("ls", ["-1", dir], { on, timeout: 10000 });
    if (r.code === 0) names = r.stdout.split("\n").filter((n) => /^[A-Za-z0-9_.-]+\.json$/.test(n));
  } catch { return []; }
  const cards = [];
  for (const name of names.slice(0, 200)) {
    const path = `${dir}/${name}`;
    const { value, text, error } = await readJson(path, on);
    if (text === null) continue;
    const valid = value && typeof value === "object" && typeof value.question === "string" && Array.isArray(value.options);
    cards.push({ id: value?.id ?? name.replace(/\.json$/, ""), path, text, card: valid ? value : null, error: error ?? (valid ? null : "not a decision card (needs question and options)") });
  }
  const when = (c) => String(c.card?.created ?? "");
  return cards.sort((a, b) => Number(b.card?.status === "open") - Number(a.card?.status === "open") || when(b).localeCompare(when(a)) || b.id.localeCompare(a.id));
}

// Compare-and-write on the space's machine: the file must be unchanged since it was read.
const REMOTE_WRITE = `
import json, os, sys, tempfile
path, expected, text = sys.argv[1], sys.argv[2], sys.argv[3]
if (open(path).read() if os.path.exists(path) else "") != expected: sys.exit(3)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as f: f.write(text)
os.replace(tmp, path)
`;

// Replaces a file only if it still reads `expected` (null: it must not exist yet, here only).
export async function replaceFile(path, text, expected, on) {
  if (!on) {
    try { await writeTextFile(path, text, expected); }
    catch (e) { throw new Error(/stale|match|changed/i.test(String(e.message ?? e)) ? "The file changed on disk while you were editing; reloaded, try again." : String(e.message ?? e)); }
    return;
  }
  const r = await exec("python3", ["-c", REMOTE_WRITE, path, expected ?? "", text], { on, timeout: 20000 });
  if (r.code !== 0) throw new Error(r.code === 3 ? "The file changed on disk while you were editing; reloaded, try again." : r.stderr || "Could not write the file.");
}

// Writes the person's answer into the card file. The only fields this mod ever changes.
export async function answerDecision(space, entry, label, note) {
  const on = space.machine ?? null;
  const now = await readTextFile(entry.path, { on });
  if (now === null) throw new Error("The card file is gone.");
  let card;
  try { card = JSON.parse(now); } catch { throw new Error("The card file is not valid JSON; fix it by hand."); }
  if (card.status !== "open") throw new Error("This card was already answered.");
  if (!(card.options ?? []).some((o) => o.label === label)) throw new Error("That option is no longer on the card.");
  const next = { ...card, status: "answered", answer: label, note: note || null, answered: new Date().toISOString().replace(/\.\d+Z$/, "Z") };
  await replaceFile(entry.path, `${JSON.stringify(next, null, 2)}\n`, now, on);
  return next;
}

// Tells the orchestrator at `address` (machine-qualified, from liveTarget: chosen when sending).
// hc holds a message while someone is typing there, and refuses while the agent is blocked on a
// question; the caller keeps the notice and tries again later.
export async function notifyOrchestrator(address, message) {
  const r = await hc(["send", address, message]);
  if (r.code !== 0) throw new Error((r.stderr || r.stdout || "hc send failed").trim().slice(0, 300));
}

// ---- rules (milestone 3) -------------------------------------------------------------------------

export const RULE_STATUS = ["proposed", "active", "off", "declined"];
const stamp = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

// { path, text, rules, error }: error set (and rules empty) when the file is invalid. An invalid
// file is shown as such and never written over.
export async function loadRules(space) {
  const on = space.machine ?? null, path = `${stateDir(space)}/rules.json`;
  const { value, text, error } = await readJson(path, on);
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
  if (problems.length) return { path, text, rules: [], error: `rules.json: ${problems.slice(0, 3).join("; ")}` };
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
  const base = `r-${String(title).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "rule"}`;
  let id = base, n = 2;
  while (rules.some((r) => r.id === id)) id = `${base}-${n++}`;
  return id;
};

export async function exportRules(space, rules) {
  const dir = `${paHome(space.machine)}/exports`, path = `${dir}/rules-${localId(space.id).slice(-8)}-${stamp().replace(/[:]/g, "")}.json`;
  await exec("mkdir", ["-p", dir], { on: space.machine ?? null });
  await replaceFile(path, `${JSON.stringify({ version: 1, rules: rules.map(({ history, needsAttention, ...r }) => r) }, null, 2)}\n`, null, space.machine ?? null);
  return path;
}

// Rules from an exported file that this space doesn't have yet, as active rules of the person's.
export async function importRules(space, path) {
  const { value, error } = await readJson(path.replace(/^~(?=\/)/, space.machine ? "." : home()), space.machine ?? null);
  if (error || !Array.isArray(value?.rules)) throw new Error(error ? `Not valid JSON: ${error}` : "No file with a \"rules\" list at that path.");
  let added = 0;
  await changeRules(space, (rules) => {
    for (const r of value.rules) {
      if (!r?.id || typeof r.text !== "string" || rules.some((x) => x.id === r.id)) continue;
      rules.push(touched({ ...r, status: RULE_STATUS.includes(r.status) && r.status !== "proposed" ? r.status : "active", source: "import", order: 0 }, "imported"));
      added += 1;
    }
    return rules.sort((a, b) => (b.order ?? 0) - (a.order ?? 0)).map((r, i, all) => ({ ...r, order: (all.length - i) * 10 }));
  });
  return added;
}
