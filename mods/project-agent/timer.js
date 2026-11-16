// The pass timer (spec section 10). Herdr has no scheduler, so one copy of this mod sends
// "scheduled pass" to each Project Agent when its schedule says so, and delivers on-demand
// passes queued by the panel or by `pa-pass`. It never interrupts: while the agent is
// working or waiting on the person, the pass stays due and goes out when the turn ends.
//
// schedule.json ownership: the person (this panel) and the orchestrator write the settings;
// only this timer writes lastPass and nextPass. pass-request.json is the on-demand queue.

import { terminals, hc, readTextFile, exec, shared, setShared, notify, spaces } from "herdr";
import { paHome, replaceFile, localId, keeperJudging } from "./store.js";

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
// space: { address, machine, status, tab, dir, name, alive }. alive: the record says its launcher
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
    byDir.set(dir, { address: r.terminal, machine, status: term?.status ?? null, tab, dir, name: r.name ?? "", alive });
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

// One copy of the mod holds the lease and does the sending, so a pass is never sent twice.
export async function tick(me) {
  const now = Date.now(), lease = shared("project-agent.timer");
  // Renewed about every 30 s; taken over after 50 s of silence (the holder's copy was reloaded or closed).
  if (lease && lease.id !== me && now - lease.at < 50000) return;
  if (!lease || lease.id !== me || now - lease.at > 20000) setShared("project-agent.timer", { id: me, at: now });
  for (const agent of projectAgents()) {
    try { await tickOne(agent, now); } catch (e) { console.log(`project-agent timer: ${agent.name}: ${e.message ?? e}`); }
  }
}

// The panel's side: settings only, never lastPass / nextPass.
export const setSchedule = (dir, machine, fields) => patchJson(`${dir}/schedule.json`, machine, (s) => ({ ...s, ...fields }), null);

// Queue an on-demand pass. Repeated requests collapse into one; a retrospective request sticks.
export const requestPass = (dir, machine, retro) => patchJson(`${dir}/pass-request.json`, machine,
  (r) => ({ pending: true, retro: Boolean(retro) || Boolean(r.pending && r.retro), at: iso(Date.now()), by: "panel" }), {});
