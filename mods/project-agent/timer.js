// The pass timer (spec section 10). Herdr has no scheduler, so one copy of this mod sends
// "scheduled pass" to each Project Agent when its schedule says so, and delivers on-demand
// passes queued by the panel or by `pa-pass`. It never interrupts: while the agent is
// working or waiting on the person, the pass stays due and goes out when the turn ends.
//
// schedule.json ownership: the person (this panel) and the orchestrator write the settings;
// only this timer writes lastPass and nextPass. pass-request.json is the on-demand queue.
//
// Costs (t-009): the lease holder also runs `pa-costs collect --space <id> --quiet` (+ its zone, t-036) for each space,
// on its machine, every 120 s while some copy shows that space's Costs view (shared
// "project-agent.costsShown"), else every 30 min; and `pa-costs billed fetch --scheduled --quiet`
// here at most once an hour (the tool itself queries at most once a day, and only once the person
// turned it on). Neither is awaited: a slow or hung machine never delays a pass.
//
// t-053: with the copy's Host (tick's host and cx) every read, listing, send and pa-costs run is keyed and capped: a call
// still pending (a hung machine) starts no second one of its kind, and a pass that waits on one gives up at its limit,
// so the other spaces' passes still go out.

import { terminals, shared, setShared, notify, spaces } from "herdr";
import { paHome, replaceFile, localId, keeperJudging, missingTool, capped, gaveUp, SEND_LIMIT, GRACE } from "./store.js";
import { read, run as hostRun, send as hcSend } from "./host.js";
import { zoneArgs, oldTool } from "./zone.js";

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

// A send given up whose call answered 0 later (the pass did go out): address -> message. The next tick stamps it and
// never sends it again.
const lateSent = {};
async function send(agent, message, timerCx) {
  if (lateSent[agent.address] === message) { delete lateSent[agent.address]; return; }
  const r = await capped(timerCx, () => hcSend(["send", agent.address, message]), { key: `hc:${agent.address}`, limit: SEND_LIMIT, what: "the hc send",
    onLate: (v) => { if (v?.code === 0) lateSent[agent.address] = message; } });
  if (gaveUp(r)) throw new Error(r.busy ? "an earlier hc send there is still waiting" : "hc send did not answer");
  if (r.code !== 0) throw new Error((r.stderr || r.stdout || "hc send failed").trim().slice(0, 200));
}

// Changes fields of a JSON file on the agent's machine, against what is on disk now.
export async function patchJson(path, machine, change, fallback, timerCx = null) {
  const text = await capped(timerCx, () => read(path, { on: machine }), { key: `read:${machine ?? ""}:${path}`, what: `the ${path.split("/").pop()} read` });
  if (gaveUp(text)) throw new Error(`${path.split("/").pop()} did not answer`);
  const value = text === null ? fallback : parse(text);
  if (!value) throw new Error(`${path.split("/").pop()} is not valid JSON; fix it by hand.`);
  await replaceFile(path, `${JSON.stringify(change({ ...value }), null, 2)}\n`, text, machine, timerCx); // keyed and capped with the timer's cx (round 2)
}

// A notification for each decision card that is new and open, for every Project Agent (not only
// the space on screen). The cards seen per agent are kept in shared "project-agent.cards"; the
// first look at an agent only records what is there.
async function announceCards(agent, timerCx) {
  const r = await capped(timerCx, () => hostRun("ls", ["-1", `${agent.dir}/decisions`], { on: agent.machine, timeout: 10000 }), { key: `ls:${agent.machine ?? ""}:${agent.dir}/decisions`, limit: 10000 + GRACE, what: "the decision cards listing" });
  if (gaveUp(r) || r.code !== 0) return;
  const ids = r.stdout.split("\n").filter((n) => /^[A-Za-z0-9_.-]+\.json$/.test(n)).map((n) => n.slice(0, -5));
  const all = { ...(shared("project-agent.cards") ?? {}) }, seen = all[agent.dir];
  const fresh = seen ? ids.filter((id) => !seen.includes(id)) : [];
  if (seen && !fresh.length && seen.length === ids.length) return;
  for (const id of fresh.slice(0, 5)) {
    const path = `${agent.dir}/decisions/${id}.json`, text = await capped(timerCx, () => read(path, { on: agent.machine }), { key: `read:${agent.machine ?? ""}:${path}`, what: `the ${id}.json read` });
    if (gaveUp(text)) return; // not recorded: the next tick looks again
    const card = parse(text ?? "");
    if (card?.status !== "open") continue;
    const space = spaces().find((s) => s.tabs.some((t) => t.id === agent.tab));
    // Say which task, the question, and what is recommended.
    const about = card.task ? ` (${card.task})` : "", pick = card.recommendation ? ` Recommended: ${card.recommendation}.` : "";
    notify({ title: `Decision needed${about}: ${id}`, body: `${String(card.question ?? "")}${pick}`, level: "warning", ...(space ? { action: { name: "space.select", args: { id: space.id } } } : {}) });
  }
  all[agent.dir] = ids;
  setShared("project-agent.cards", all);
}

const STAMPED = ["pass-request.json", "schedule.json"];
async function tickOne(agent, now, timerCx) {
  await announceCards(agent, timerCx).catch(() => {});
  if (!agent.alive) return; // no launcher known to run there: nothing is typed into that terminal
  // A stamp write of this agent given up and still pending (round 2): it may still land, so no pass until it settles.
  if (timerCx?.host && STAMPED.some((f) => timerCx.host.busy(`${timerCx.origin}:write:${agent.machine ?? ""}:${agent.dir}/${f}`))) return;
  const busy = agent.status === "working" || agent.status === "blocked";
  const texts = await Promise.all(["schedule.json", "ledger.json", "pass-request.json"].map((f) => capped(timerCx, () => read(`${agent.dir}/${f}`, { on: agent.machine }), { key: `read:${agent.machine ?? ""}:${agent.dir}/${f}`, what: `the ${f} read` })));
  if (texts.some(gaveUp)) return; // did not answer: the next tick tries again
  const [scheduleText, ledgerText, requestText] = texts;
  const schedule = parse(scheduleText ?? ""), ledger = parse(ledgerText ?? ""), request = parse(requestText ?? "");
  if (!schedule) return;
  const every = cadence(schedule) * 60000;
  const stamp = (s) => ({ ...s, lastPass: iso(now), nextPass: every ? iso(now + every) : null });
  // On demand: runs even when passes are paused, off or in quiet hours, and resets the timer.
  if (request?.pending) {
    if (busy) return;
    await send(agent, request.retro ? "manual pass with retrospective" : "manual pass", timerCx);
    await patchJson(`${agent.dir}/pass-request.json`, agent.machine, (r) => ({ ...r, pending: false, sent: iso(now) }), {}, timerCx);
    await patchJson(`${agent.dir}/schedule.json`, agent.machine, stamp, null, timerCx);
    return;
  }
  const hold = holdReason(schedule, ledger);
  if (hold) {
    if (schedule.nextPass) await patchJson(`${agent.dir}/schedule.json`, agent.machine, (s) => ({ ...s, nextPass: null }), null, timerCx);
    return;
  }
  // A changed cadence applies from the next pass: one cadence after the last one.
  const last = Date.parse(schedule.lastPass ?? "") || null;
  const due = last ? last + every : Date.parse(schedule.nextPass ?? "") || now + every;
  if (now < due) {
    if (Date.parse(schedule.nextPass ?? "") !== Math.floor(due / 1000) * 1000) await patchJson(`${agent.dir}/schedule.json`, agent.machine, (s) => ({ ...s, nextPass: iso(due) }), null, timerCx);
    return;
  }
  if (busy) return; // still due: goes out as soon as the turn ends
  await send(agent, "scheduled pass", timerCx);
  await patchJson(`${agent.dir}/schedule.json`, agent.machine, stamp, null, timerCx);
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
export function collectCosts(agent, now, timerCx = null) {
  const id = agent.space;
  if (!id) return;
  const runs = shared("project-agent.costsRun") ?? {}, run = runs[id];
  if (run?.busy && now - run.at < COLLECT_STALE) return;
  const shownAt = Number((shared("project-agent.costsShown") ?? {})[id]) || 0;
  const every = now - shownAt < SHOWN_FRESH ? COLLECT_FAST : COLLECT_SLOW;
  if (run && now - run.at < every) return;
  setShared("project-agent.costsRun", { ...runs, [id]: { at: now, busy: true } });
  let p;
  // With this panel's zone (t-036, zone.js); a pa-costs from before it refuses that: run again without it.
  const args = ["collect", "--space", localId(id), "--quiet"];
  const collectWith = (a) => capped(timerCx, () => hostRun("pa-costs", a, { on: agent.machine, timeout: COLLECT_TIMEOUT }), { key: `collect:${id}`, limit: COLLECT_TIMEOUT + GRACE, what: "pa-costs collect" });
  try { p = Promise.resolve(collectWith([...args, ...zoneArgs(now)])).then((r) => (!gaveUp(r) && oldTool(r) ? collectWith(args) : r)); } catch (e) { p = Promise.reject(e); }
  // A collect that did not answer is a failure; one skipped (the last still pending, or no room) records nothing.
  Promise.resolve(p).then((r) => (gaveUp(r) ? (r.busy || r.skipped || r.refused ? null : { ok: false, error: "pa-costs collect did not answer" }) : outcome(r)), failure).then((res) => {
    const all = { ...(shared("project-agent.costsRun") ?? {}) };
    if (all[id]?.at === now) { all[id] = { at: now, busy: false }; setShared("project-agent.costsRun", all); }
    if (!res) return;
    const kept = { ...(shared("project-agent.costs") ?? {}) }, was = kept[id];
    const same = was && was.ok === res.ok && was.code === res.code && was.error === res.error && was.missing === res.missing;
    if (!same) { kept[id] = { at: iso(now), ...res }; setShared("project-agent.costs", kept); }
    if (!res.ok) logOnce(`collect ${id}`, `pa-costs collect for ${id}: ${res.missing ? "pa-costs is not installed there" : res.error || `exit ${res.code}`}`, now);
  }, () => {});
}

// The scheduled billing query, here only: at most once per BILLING_EVERY (a shared stamp, so a takeover inside the hour runs none).
export function billingTick(now, timerCx = null) {
  const last = Number(shared("project-agent.billingTick")?.at) || 0;
  if (now - last < BILLING_EVERY) return;
  setShared("project-agent.billingTick", { at: now });
  let p;
  try { p = capped(timerCx, () => hostRun("pa-costs", ["billed", "fetch", "--scheduled", "--quiet"], { timeout: FETCH_TIMEOUT }), { key: "billing", limit: FETCH_TIMEOUT + GRACE, what: "the scheduled billing query" }); } catch (e) { p = Promise.reject(e); }
  // Did not answer: a failure (logged); skipped (the last one still pending, or no room): nothing to say.
  Promise.resolve(p).then((r) => (gaveUp(r) ? (r.busy || r.skipped || r.refused ? { ok: true } : { ok: false, error: "did not answer" }) : outcome(r)), failure).then((res) => { if (!res.ok) logOnce("billing", `pa-costs billed fetch: ${res.missing ? "pa-costs is not installed here" : res.error || `exit ${res.code}`}`, now); }, () => {});
}

// One copy of the mod holds the lease and does the sending, so a pass is never sent twice.
// host, cx (t-053): the copy's Host and the tick task's cx; without them each call is made as is (tests).
export async function tick(me, host = null, cx = null) {
  const timerCx = host ? host.ctx(cx, { origin: "timer" }) : null;
  const now = Date.now(), lease = shared("project-agent.timer");
  // Renewed about every 30 s; taken over after 50 s of silence (the holder's copy was reloaded or closed).
  if (lease && lease.id !== me && now - lease.at < 50000) return;
  if (!lease || lease.id !== me || now - lease.at > 20000) setShared("project-agent.timer", { id: me, at: now });
  billingTick(now, timerCx);
  for (const agent of projectAgents()) {
    collectCosts(agent, now, timerCx); // not awaited: it runs beside the pass, also for an agent that is not alive
    try { await tickOne(agent, now, timerCx); } catch (e) { console.log(`project-agent timer: ${agent.name}: ${e.message ?? e}`); }
  }
}

// The panel's side: settings only, never lastPass / nextPass.
export const setSchedule = (dir, machine, fields) => patchJson(`${dir}/schedule.json`, machine, (s) => ({ ...s, ...fields }), null);

// Queue an on-demand pass. Repeated requests collapse into one; a retrospective request sticks.
export const requestPass = (dir, machine, retro) => patchJson(`${dir}/pass-request.json`, machine,
  (r) => ({ pending: true, retro: Boolean(retro) || Boolean(r.pending && r.retro), at: iso(Date.now()), by: "panel" }), {});
