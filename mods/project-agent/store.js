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

// A remote read that has not answered by then is given up (a load must not stay stuck).
export const READ_LIMIT = 10000;
export const TIMED_OUT = Object.freeze({ timedOut: true });
// `p`, or TIMED_OUT after `ms` on the cx of the task that waits (cx.sleep: mods have no setTimeout).
// Without a cx it is just `p`. When it settles either way, `cx.progress()` (if given) is called: the
// panel's load counts it as progress, so a slow but working load is never given up. The losing
// sleep is not cancelled (mods cannot cancel one): it runs out on its own, at most READ_LIMIT later,
// one per capped read (up to 200 on the card fallback); a pending sleep holds no call to the machine.
export function withTimeout(cx, p, ms = READ_LIMIT) {
  if (!cx?.sleep) return p;
  return Promise.race([p, cx.sleep(ms).then(() => TIMED_OUT, () => TIMED_OUT)]).then((v) => { cx.progress?.(); return v; }, (e) => { cx.progress?.(); throw e; });
}

async function readJson(path, on, cx) {
  const text = await withTimeout(cx, readTextFile(path, { on: on ?? null }));
  if (text === TIMED_OUT) return { value: null, text: null, error: "timed out", timedOut: true };
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
  if (r.timedOut) return { summary: null, error: null, timedOut: true, missing: null };
  if (r.error) return { summary: null, error: r.error, timedOut: false, missing: null };
  if (r.text !== null) {
    const ok = r.value && typeof r.value === "object" && !Array.isArray(r.value);
    return ok ? { summary: r.value, error: null, timedOut: false, missing: null } : { summary: null, error: "not a summary (not a JSON object)", timedOut: false, missing: null };
  }
  if (!live()) return { summary: null, error: null, timedOut: false, missing: null, left: true };
  const ls = await withTimeout(cx, exec("ls", ["-1", costsDir(space)], { on, timeout: READ_LIMIT }));
  if (ls === TIMED_OUT) return { summary: null, error: null, timedOut: true, missing: null };
  const names = ls.code === 0 ? String(ls.stdout ?? "").split("\n") : [];
  return { summary: null, error: null, timedOut: false, missing: names.includes("sessions.jsonl") ? "pending" : "none" };
}

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
  if (!agent.value) return { agent: null, ledger: null, profile: null, errors, dir, ...(agent.timedOut ? { timedOut: true } : {}) };
  let name = agent.value.profile || profileProp, nameUnknown = false;
  const profileRead = async () => {
    if (!name) {
      const config = await readJson(`${root}/config.json`, on, cx);
      if (config.timedOut || config.error) nameUnknown = config.timedOut ? "timed out" : true;
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

const CARD_NAME = /^[A-Za-z0-9_.-]+\.json$/;
export const MAX_CARDS = 200;

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

// Every card in decisions/ (the first 200 names): see parseCards. On another machine one call reads
// them all; if that call fails, times out or prints something unreadable, they are read one by one,
// in parallel (as before, but not in sequence). On this computer the files are read directly.
// Options: cx (each call gives up after READ_LIMIT), batchOnly (no fallback: null when the call
// fails), live() (false: the caller no longer wants the result; the fallback is skipped and the
// result is null, which the caller must not take for "no cards"), known (the last known cards,
// [{ path, text }]: a card whose own read times out or fails on the fallback path is that copy, else
// left out; such a card has fallback: true, and the list's `unread` (a Set of paths) names every card not read now,
// t-045). Only exit 0 is a whole batch (exit 3 with no output: no folder); a call killed part way
// can print whole-looking pairs. null also when the listing never answered (the batch gave no whole
// answer and `ls` timed out or failed): unknown, not "no cards". A listing that answered (no folder,
// an empty one) is [].
export async function loadDecisions(space, { cx, batchOnly = false, live, known = [] } = {}) {
  const on = space.machine ?? null, dir = `${stateDir(space)}/decisions`;
  if (on) {
    let r = null;
    try { r = await withTimeout(cx, exec("sh", ["-c", CARDS_SCRIPT, "sh", dir], { on, timeout: READ_LIMIT })); } catch { r = null; }
    if (r && r !== TIMED_OUT && r.code === 3 && !r.stdout) return []; // no decisions folder: no cards (as a failing ls)
    const files = r && r !== TIMED_OUT && r.code === 0 ? splitBatch(r.stdout) : null;
    if (files) return parseCards(files.sort((a, b) => byteOrder(a.name, b.name)).slice(0, MAX_CARDS).map((f) => ({ ...f, path: `${dir}/${f.name}` })));
    if (batchOnly || (live && !live())) return null;
  }
  let names = [];
  try {
    const r = await withTimeout(cx, exec("ls", ["-1", dir], { on, timeout: READ_LIMIT }));
    if (r === TIMED_OUT) return null;
    if (r.code === 0) names = r.stdout.split("\n").filter((n) => CARD_NAME.test(n));
  } catch { return null; }
  const picked = names.slice(0, MAX_CARDS);
  const last = new Map((known ?? []).filter((c) => c?.path && typeof c.text === "string").map((c) => [c.path, c.text]));
  const unread = new Set();
  const texts = await Promise.all(picked.map(async (name) => {
    let text;
    try { text = await withTimeout(cx, readTextFile(`${dir}/${name}`, { on })); } catch { text = TIMED_OUT; }
    if (text === TIMED_OUT) unread.add(`${dir}/${name}`);
    return text === TIMED_OUT ? last.get(`${dir}/${name}`) ?? null : text; // a read that hangs or fails: its last known copy, else left out
  }));
  const list = parseCards(picked.map((name, i) => ({ name, path: `${dir}/${name}`, text: texts[i], fallback: unread.has(`${dir}/${name}`) && texts[i] !== null })));
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
export async function replaceFile(path, text, expected, on) {
  if (!on) {
    try { await writeTextFile(path, text, expected); }
    catch (e) { throw new Error(/stale|match|changed/i.test(String(e.message ?? e)) ? "The file changed on disk while you were editing; reloaded, try again." : String(e.message ?? e)); }
    return;
  }
  const r = await exec("python3", ["-c", REMOTE_WRITE, path, expected ?? "", text], { on, timeout: 20000 });
  if (r.code !== 0) throw new Error(r.code === 3 ? "The file changed on disk while you were editing; reloaded, try again." : r.stderr || "Could not write the file.");
}

// A push card (t-007b): it has a push object, or kind "push". Its answer runs pa-push-main (push.js), which
// writes the answer itself; answerDecision never writes one.
export const isPush = (card) => Boolean(card && typeof card === "object" && ((card.push && typeof card.push === "object") || card.kind === "push"));

// Writes the person's answer into the card file. The only fields this mod ever changes. A push card (as
// read from disk now) is refused: only pa-push-main run, from its own Submit, answers it. A card that is not open
// (as read now) is refused, and the write is a compare-and-write against that open copy: an answer already written
// is never changed. Returns { card, before, after }: the answered card, the file text it replaced, the text written.
export async function answerDecision(space, entry, label, note) {
  const on = space.machine ?? null;
  const now = await readTextFile(entry.path, { on });
  if (now === null) throw new Error("The card file is gone.");
  let card;
  try { card = JSON.parse(now); } catch { throw new Error("The card file is not valid JSON; fix it by hand."); }
  if (isPush(card)) throw new Error("A push card is answered with its own Submit; its answer runs pa-push-main.");
  if (card.status !== "open") throw new Error("This card was already answered.");
  if (!(card.options ?? []).some((o) => o.label === label)) throw new Error("That option is no longer on the card.");
  const next = { ...card, status: "answered", answer: label, note: note || null, answered: new Date().toISOString().replace(/\.\d+Z$/, "Z") };
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
// longer. Giving up stops only the waiting: the text may still be typed, so a retry can repeat it.
export const SEND_LIMIT = 90000;
export async function notifyOrchestrator(address, message, cx) {
  const r = await withTimeout(cx, hc(["send", address, message]), SEND_LIMIT);
  if (r === TIMED_OUT) throw new Error(`hc send did not answer in ${SEND_LIMIT / 1000} s; it may still arrive`);
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
