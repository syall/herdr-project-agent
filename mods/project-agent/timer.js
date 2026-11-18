// The pass timer (spec section 10). Herdr has no scheduler, so one copy of this mod sends
// "scheduled pass" to each Project Agent when its schedule says so, and delivers on-demand
// passes queued by the panel or by `pa-pass`. It never interrupts: while the agent is
// working or waiting on the person, the pass stays due and goes out when the turn ends.
//
// schedule.json ownership: the person (this panel) and the orchestrator write the settings;
// only this timer writes lastPass and nextPass. pass-request.json is the on-demand queue.
//
// Costs (t-009): the lease holder also runs `pa-costs collect --space <id> --quiet` for each space,
// on its machine, every 120 s while some copy shows that space's Costs view (shared
// "project-agent.costsShown"), else every 30 min; and `pa-costs billed fetch --scheduled --quiet`
// here at most once an hour (the tool itself queries at most once a day, and only once the person
// turned it on). Neither is awaited: a slow or hung machine never delays a pass.

import { terminals, hc, readTextFile, exec, shared, setShared, notify, spaces } from "herdr";
import { paHome, replaceFile, localId, keeperJudging, missingTool } from "./store.js";

export const MIN_MINUTES = 5;
export const PRESETS = [5, 10, 15, 30, 60];
const ACTIVE = ["queued", "investigating", "in-progress", "needs-decision", "review"];
const iso = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
const parse = (text) => { try { return JSON.parse(text); } catch { return null; } };

export const cadence = (s) => (Number(s?.everyMinutes) > 0 ? Math.max(MIN_MINUTES, Number(s.everyMinutes)) : 0);

export function inQuietHours(q, date = new Date()) {
  const ok = (v) => typeof v === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(v);
  if (!q || !ok(q.from) || !ok(q.to) || q.from === q.to) return false;
  const now = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return q.from < q.to ? now >= q.from && now < q.to : now >= q.from || now < q.to;
}

// Why no scheduled pass is coming, or null when one is: shown in the panel, used by the timer.
export function holdReason(schedule, ledger) {
  if (!cadence(schedule)) return "off";
  if (schedule.paused) return "paused";
  if (schedule.onlyWhileActive !== false && !(ledger?.tasks ?? []).some((t) => ACTIVE.includes(t.state))) return "no active tasks";
  if (inQuietHours(schedule.quietHours)) return `quiet hours until ${schedule.quietHours.to}`;
  return null;
}

// Every Project Agent the keeper records in this window (shared "project-agent.tabs"), one per
// space: { address, machine, space, status, tab, dir, name, alive } (space: the panel's space id). alive: the record says its launcher
// runs in that terminal, and the terminal is live. Passes go only to an alive one; a labelled
// terminal without that (a kept shell, the old terminal during a restore) gets nothing, and so
// does every record while the marks are not the current keeper's (after a reload or takeover).
export function projectAgents() {
  const ts = terminals(), byDir = new Map(), judged = keeperJudging();
  for (const [tab, r] of Object.entries(shared("project-agent.tabs") ?? {})) {
    if (!r?.space) continue;
    const machine = String(r.space).includes("/") ? String(r.space).split("/")[0] : null;
    const dir = `${paHome(machine)}/spaces/${localId(r.space)}`;
    const term = ts.find((t) => t.address === r.terminal && t.state === "live");
    const alive = judged && r.alive === true && Boolean(term);
    const prev = byDir.get(dir);
    if (prev && (prev.alive || !alive)) continue;
    byDir.set(dir, { address: r.terminal, machine, space: String(r.space), status: term?.status ?? null, tab, dir, name: r.name ?? "", alive });
  }
  return [...byDir.values()];
}

async function send(agent, message) {
  const r = await hc(["send", agent.address, message]);
  if (r.code !== 0) throw new Error((r.stderr || r.stdout || "hc send failed").trim().slice(0, 200));
}

// Changes fields of a JSON file on the agent's machine, against what is on disk now.
export async function patchJson(path, machine, change, fallback) {
  const text = await readTextFile(path, { on: machine });
  const value = text === null ? fallback : parse(text);
  if (!value) throw new Error(`${path.split("/").pop()} is not valid JSON; fix it by hand.`);
  await replaceFile(path, `${JSON.stringify(change({ ...value }), null, 2)}\n`, text, machine);
}

// A notification for each decision card that is new and open, for every Project Agent (not only
// the space on screen). The cards seen per agent are kept in shared "project-agent.cards"; the
// first look at an agent only records what is there.
async function announceCards(agent) {
  const r = await exec("ls", ["-1", `${agent.dir}/decisions`], { on: agent.machine, timeout: 10000 });
  if (r.code !== 0) return;
  const ids = r.stdout.split("\n").filter((n) => /^[A-Za-z0-9_.-]+\.json$/.test(n)).map((n) => n.slice(0, -5));
  const all = { ...(shared("project-agent.cards") ?? {}) }, seen = all[agent.dir];
  const fresh = seen ? ids.filter((id) => !seen.includes(id)) : [];
  if (seen && !fresh.length && seen.length === ids.length) return;
  for (const id of fresh.slice(0, 5)) {
    const card = parse((await readTextFile(`${agent.dir}/decisions/${id}.json`, { on: agent.machine })) ?? "");
    if (card?.status !== "open") continue;
    const space = spaces().find((s) => s.tabs.some((t) => t.id === agent.tab));
    // Say which task, the question, and what is recommended.
    const about = card.task ? ` (${card.task})` : "", pick = card.recommendation ? ` Recommended: ${card.recommendation}.` : "";
    notify({ title: `Decision needed${about}: ${id}`, body: `${String(card.question ?? "")}${pick}`, level: "warning", ...(space ? { action: { name: "space.select", args: { id: space.id } } } : {}) });
  }
  all[agent.dir] = ids;
  setShared("project-agent.cards", all);
}

async function tickOne(agent, now) {
  await announceCards(agent).catch(() => {});
  if (!agent.alive) return; // no launcher known to run there: nothing is typed into that terminal
  const busy = agent.status === "working" || agent.status === "blocked";
  const [scheduleText, ledgerText, requestText] = await Promise.all(["schedule.json", "ledger.json", "pass-request.json"].map((f) => readTextFile(`${agent.dir}/${f}`, { on: agent.machine })));
  const schedule = parse(scheduleText ?? ""), ledger = parse(ledgerText ?? ""), request = parse(requestText ?? "");
  if (!schedule) return;
  const every = cadence(schedule) * 60000;
  const stamp = (s) => ({ ...s, lastPass: iso(now), nextPass: every ? iso(now + every) : null });
  // On demand: runs even when passes are paused, off or in quiet hours, and resets the timer.
  if (request?.pending) {
    if (busy) return;
    await send(agent, request.retro ? "manual pass with retrospective" : "manual pass");
    await patchJson(`${agent.dir}/pass-request.json`, agent.machine, (r) => ({ ...r, pending: false, sent: iso(now) }), {});
    await patchJson(`${agent.dir}/schedule.json`, agent.machine, stamp, null);
    return;
  }
  const hold = holdReason(schedule, ledger);
  if (hold) {
    if (schedule.nextPass) await patchJson(`${agent.dir}/schedule.json`, agent.machine, (s) => ({ ...s, nextPass: null }), null);
    return;
  }
  // A changed cadence applies from the next pass: one cadence after the last one.
  const last = Date.parse(schedule.lastPass ?? "") || null;
  const due = last ? last + every : Date.parse(schedule.nextPass ?? "") || now + every;
  if (now < due) {
    if (Date.parse(schedule.nextPass ?? "") !== Math.floor(due / 1000) * 1000) await patchJson(`${agent.dir}/schedule.json`, agent.machine, (s) => ({ ...s, nextPass: iso(due) }), null);
    return;
  }
  if (busy) return; // still due: goes out as soon as the turn ends
  await send(agent, "scheduled pass");
  await patchJson(`${agent.dir}/schedule.json`, agent.machine, stamp, null);
}

// ---- costs (t-009) ----
export const COLLECT_FAST = 120000; // a space whose Costs view is shown somewhere
export const COLLECT_SLOW = 1800000; // every other space
export const SHOWN_FRESH = 30000; // a costsShown stamp younger than this counts (the panel writes it every 20 s while shown)
export const COLLECT_STALE = 90000; // a collect still running after this counts as gone (the tool's lock refuses a second run anyway)
export const BILLING_EVERY = 3600000; // the scheduled billing query is asked at most this often (shared stamp)
const COLLECT_TIMEOUT = 30000, FETCH_TIMEOUT = 180000, LOG_EVERY = 3600000;
const logged = {}; // what -> when a failure was last written to the console
const logOnce = (what, text, now) => { if (now - (logged[what] ?? 0) < LOG_EVERY) return; logged[what] = now; console.log(`project-agent timer: ${text}`); };
const outcome = (r) => (r.code === 0 ? { ok: true } : r.code === 127 ? { ok: false, code: 127, missing: true }
  : { ok: false, code: r.code, error: String(r.stderr || r.stdout || "").trim().split("\n")[0].slice(0, 200) });
const failure = (e) => { const why = String(e?.message ?? e).slice(0, 200); return missingTool(why, "pa-costs") ? { ok: false, missing: true, error: why } : { ok: false, error: why }; };

// The space's collect, when due (shared "project-agent.costsRun": { [space]: { at, busy } }, kept across a takeover).
// The result goes to shared "project-agent.costs" ({ [space]: { at, ok, code?, error?, missing? } }), written on change only.
export function collectCosts(agent, now) {
  const id = agent.space;
  if (!id) return;
  const runs = shared("project-agent.costsRun") ?? {}, run = runs[id];
  if (run?.busy && now - run.at < COLLECT_STALE) return;
  const shownAt = Number((shared("project-agent.costsShown") ?? {})[id]) || 0;
  const every = now - shownAt < SHOWN_FRESH ? COLLECT_FAST : COLLECT_SLOW;
  if (run && now - run.at < every) return;
  setShared("project-agent.costsRun", { ...runs, [id]: { at: now, busy: true } });
  let p;
  try { p = exec("pa-costs", ["collect", "--space", localId(id), "--quiet"], { on: agent.machine, timeout: COLLECT_TIMEOUT }); } catch (e) { p = Promise.reject(e); }
  Promise.resolve(p).then(outcome, failure).then((res) => {
    const all = { ...(shared("project-agent.costsRun") ?? {}) };
    if (all[id]?.at === now) { all[id] = { at: now, busy: false }; setShared("project-agent.costsRun", all); }
    const kept = { ...(shared("project-agent.costs") ?? {}) }, was = kept[id];
    const same = was && was.ok === res.ok && was.code === res.code && was.error === res.error && was.missing === res.missing;
    if (!same) { kept[id] = { at: iso(now), ...res }; setShared("project-agent.costs", kept); }
    if (!res.ok) logOnce(`collect ${id}`, `pa-costs collect for ${id}: ${res.missing ? "pa-costs is not installed there" : res.error || `exit ${res.code}`}`, now);
  }, () => {});
}

// The scheduled billing query, here only: at most once per BILLING_EVERY (a shared stamp, so a takeover inside the hour runs none).
export function billingTick(now) {
  const last = Number(shared("project-agent.billingTick")?.at) || 0;
  if (now - last < BILLING_EVERY) return;
  setShared("project-agent.billingTick", { at: now });
  let p;
  try { p = exec("pa-costs", ["billed", "fetch", "--scheduled", "--quiet"], { timeout: FETCH_TIMEOUT }); } catch (e) { p = Promise.reject(e); }
  Promise.resolve(p).then(outcome, failure).then((res) => { if (!res.ok) logOnce("billing", `pa-costs billed fetch: ${res.missing ? "pa-costs is not installed here" : res.error || `exit ${res.code}`}`, now); }, () => {});
}

// One copy of the mod holds the lease and does the sending, so a pass is never sent twice.
export async function tick(me) {
  const now = Date.now(), lease = shared("project-agent.timer");
  // Renewed about every 30 s; taken over after 50 s of silence (the holder's copy was reloaded or closed).
  if (lease && lease.id !== me && now - lease.at < 50000) return;
  if (!lease || lease.id !== me || now - lease.at > 20000) setShared("project-agent.timer", { id: me, at: now });
  billingTick(now);
  for (const agent of projectAgents()) {
    collectCosts(agent, now); // not awaited: it runs beside the pass, also for an agent that is not alive
    try { await tickOne(agent, now); } catch (e) { console.log(`project-agent timer: ${agent.name}: ${e.message ?? e}`); }
  }
}

// The panel's side: settings only, never lastPass / nextPass.
export const setSchedule = (dir, machine, fields) => patchJson(`${dir}/schedule.json`, machine, (s) => ({ ...s, ...fields }), null);

// Queue an on-demand pass. Repeated requests collapse into one; a retrospective request sticks.
export const requestPass = (dir, machine, retro) => patchJson(`${dir}/pass-request.json`, machine,
  (r) => ({ pending: true, retro: Boolean(retro) || Boolean(r.pending && r.retro), at: iso(Date.now()), by: "panel" }), {});
