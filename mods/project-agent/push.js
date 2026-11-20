// Push cards (t-007b): a decision card with a `push` object, opened by `pa-push-main plan` for a task
// that passed its full gate, for a repository on the person's push allowlist. The panel never reads
// the facts from the card file (an agent can write it): it shows only what `pa-push-main check`
// reads now, on the space's machine. Submit runs `pa-push-main run`, which records the answer and
// pushes, under its own lock and with every check made again. Nothing else in the panel reaches it.
//   check: on the first show of an open push card in a shown copy (main.js commit(), at most one
//          per card per CHECK_EVERY), on picking Push, and on "Check again". Never from render, a
//          hidden copy, the timer copy, resend or the keeper.
//   run:   only from the Submit button of a rendered push card (main.js answer()), once per click;
//          Push needs a check that passed, with a seen token, under FRESH old, whose caller line
//          says this panel may push.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { isPush, isQueuePush, isOther, oneLine, OTHER, localId, capped, gaveUp, TIMED_OUT } from "./store.js";
import { run as hostRun } from "./host.js";
import { sel } from "./copy.js";

export { isPush, isQueuePush };
export const PUSH = "Push", NOT_NOW = "Not now";
const OTHER_SAID = "Answered Other: nothing was pushed."; // t-046: a push card answered in the person's own words
export const CHECK_EVERY = 30000; // checks not asked for by a click: at most one per card this often
export const FRESH = 120000; // Push needs a check started less than this long ago
const CHECK_TIMEOUT = 60000, ALLOW_TIMEOUT = 30000;
export const RUN_TIMEOUT = 600000;
export const GRACE = 5000; // an exec still unsettled this long after its own timeout is given up here
const COMMITS_SHOWN = 20;
// The options a push card shows, whatever its file says: pa-push-main run takes only these.
export const PUSH_OPTIONS = [
  { label: PUSH, detail: "Push exactly what is listed above, as a fast-forward. No force." },
  { label: NOT_NOW, detail: "Nothing is pushed. The card closes." },
];

// t-140 R4: a queue card that pushes shows its own options (pa-push-main run checks the label against the card, and the
// option's count is what approves); a push card shows PUSH_OPTIONS whatever its file says.
export const pushOptions = (card) => (isQueuePush(card) ? (Array.isArray(card.options) ? card.options : []).filter((o) => o && typeof o.label === "string") : PUSH_OPTIONS);
// Whether answering `label` merges or pushes (so it needs a passed, fresh check and its seen token): Push on a push card,
// an option with a count above 0 on a queue card that pushes. Not now and Other never do.
export const runsLabel = (card, label) => {
  if (!label || label === NOT_NOW || label === OTHER) return false;
  if (isQueuePush(card)) return pushOptions(card).some((o) => o.label === label && Number.isInteger(o.count) && o.count > 0);
  return label === PUSH;
};

const short = (sha) => (typeof sha === "string" && sha ? sha.slice(0, 7) : "none");
const clock = (iso, seconds = true) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "?" : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) }); };
const stamp = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? String(iso ?? "?") : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
const text = (v) => (v === null || v === undefined ? "" : String(v));
const failed = (message) => ({ ok: false, refusal: { check: "panel", message } });
// Why a push card is shown only (store.js parseCards, t-028): its own id is not its file name, or the name reads as
// an option. Such a card is never checked or run, whatever is clicked. null: none.
export function shownOnly(entry) {
  if (entry?.dash) return `This card's file name (${entry.id}) starts with a dash: shown only, never checked or run.`;
  if (entry?.mismatch) return `This card's id (${entry.mismatch}) is not its file name (${entry.id}): shown only, never checked or run.`;
  return null;
}

// The JSON object a pa-push-main/pa-push-allow command printed, or null.
function printed(r) {
  try { const v = JSON.parse(String(r?.stdout ?? "")); return v && typeof v === "object" && !Array.isArray(v) ? v : null; } catch { return null; }
}
const said = (r) => String(r?.stderr || r?.stdout || "").trim().slice(0, 600);

// One exec of the tool on the space's machine, given up GRACE after its own timeout (an exec that never
// settles). { r } or { error }, or { busy: true } (t-053: one of its kind still pending; nothing started, nothing to say).
// hcx: the task's context (a Host's ctx: keyed `key`; a plain cx: capped only).
async function call(hcx, space, command, args, timeout, key = null) {
  try {
    const r = await capped(hcx, () => hostRun(command, args, { on: space.machine ?? null, timeout }), { key, limit: timeout + GRACE, what: `${command} ${args[0]}` });
    if (gaveUp(r) && r !== TIMED_OUT) return { busy: true };
    return r === TIMED_OUT ? { error: `${command} ${args[0]} did not answer in ${Math.round((timeout + GRACE) / 1000)} s` } : { r };
  } catch (e) { return { error: String(e.message ?? e) }; }
}
const ctxOf = (host, cx, click) => (host ? host.ctx(cx, { origin: "push", background: !click, wait: click }) : cx);

// `pa-push-allow show --json` (read-only), for the Rules view: its report, or { error }.
export async function readAllow(space, cx, host = null, click = false) {
  const { r, error, busy } = await call(ctxOf(host, cx, click), space, "pa-push-allow", ["show", "--json", "--space", localId(space.id)], ALLOW_TIMEOUT, `allow:${space.id}`);
  if (busy) return { busy: true }; // t-053: the last one is still pending: the caller keeps what it has
  if (error) return { error };
  return printed(r) ?? { error: said(r) || `pa-push-allow exited with ${r?.code}` };
}

// A check that read the card open now (t-045 round 2): its status is open and it is not done, whether it passed or
// refused for another reason (a moved or unreachable remote, the gate); never a check that could not run, or C4.
export const readOpen = (c) => Boolean(c && c.status === "open" && c.done !== true && c.refusal?.check !== "C4");

export class PushCards {
  // share(spaceId, id, label | null, running, unknown, note): main.js's shared mark (note: an Other answer's words, t-131) for the other panel windows (t-045 round 2):
  // set when a run starts (running) and when it ends answered or unknown, removed when the card is known open again.
  // opened(spaceId, id, asked): a check from this copy, asked at `asked`, read the card open (t-045 round 3): an unknown
  // result's shared mark from any window, written before then, goes; nothing else unlocks it, not its age.
  constructor(share = () => {}, opened = () => {}, host = null) {
    this.share = share; this.opened = opened;
    this.host = host; // t-053: the copy's Host (host.js); null: each call as is
    // `${space id}\0${card id}` -> { check, at, asked, busy, running, result, kept, error, errorFor, unknown, stale }
    //   check: the last check's JSON; at: when that check was asked for; running: the label being answered;
    //   kept: a push from here whose result stays in the Decisions list until the person leaves it.
    this.cards = {};
  }

  of(spaceId, id) { return (this.cards[`${spaceId}\0${id}`] ??= { check: null, at: 0, asked: 0, busy: false, running: null, result: null, error: null, unknown: null, stale: false }); }
  running(spaceId, id) { return this.cards[`${spaceId}\0${id}`]?.running ?? null; }
  // The answer a run from this copy ended with (t-045), or null.
  answeredHere(spaceId, id) { return this.cards[`${spaceId}\0${id}`]?.result?.label ?? null; }
  // The words of an Other run from this copy, in flight or answered (t-131), or null.
  noteHere(spaceId, id) { const st = this.cards[`${spaceId}\0${id}`]; return (st?.running ?? st?.result?.label) === OTHER ? st.note ?? null : null; }
  // Whether this copy may not answer the card again (t-045): a run is in flight, a run from here ended answered, or its
  // result is unknown and no check since read the card open (readOpen: a refusal with the card open counts as open).
  // The card then has no option or Submit to click.
  locked(spaceId, id) {
    const st = this.cards[`${spaceId}\0${id}`];
    return Boolean(st && (st.running || st.result || (st.unknown && !(readOpen(st.check) && !st.busy))));
  }
  // A card pushed from this copy stays in the Decisions list, answered, with its result (spec §8.2),
  // until the person leaves it: another view (main.js) or its Done link.
  kept(spaceId, id) { return Boolean(this.cards[`${spaceId}\0${id}`]?.kept); }
  leave(spaceId) { for (const [k, st] of Object.entries(this.cards)) if (k.startsWith(`${spaceId}\0`)) st.kept = false; }

  // From a shown copy's commit (main.js): the first check of an open push card, at most one per CHECK_EVERY.
  wantCheck(space, entry, cx) {
    if (!isPush(entry?.card) || entry.card.status !== "open" || shownOnly(entry)) return;
    const st = this.of(space.id, entry.id);
    if (st.check || st.busy || st.running || (st.asked && Date.now() - st.asked < CHECK_EVERY)) return;
    this.check(space, entry, cx, { auto: true });
  }

  // `pa-push-main check <id> --space <id> --json`: read-only; its facts are what the block shows.
  // auto (t-053): the panel's own first check (wantCheck), which skips quietly while an older check is still pending.
  check(space, entry, cx, { auto = false } = {}) {
    const st = this.of(space.id, entry.id);
    if (st.busy || st.running || shownOnly(entry)) return;
    const asked = Date.now();
    st.busy = true; st.asked = asked; st.stale = false;
    cx.spawn(async (cx) => {
      const { r, error, busy } = await call(ctxOf(this.host, cx, !auto), space, "pa-push-main", ["check", entry.id, "--space", localId(space.id), "--json"], CHECK_TIMEOUT, `check:${space.id}:${entry.id}`);
      if (busy) { st.busy = false; cx.notify(); return; } // the last check still pending: nothing read, nothing said (never "open")
      st.check = error ? failed(`pa-push-main check could not run: ${error}`) : printed(r) ?? failed(`pa-push-main check printed no result${said(r) ? `: ${said(r)}` : ` (exit ${r?.code})`}`);
      st.at = asked; st.busy = false;
      if (st.unknown && !st.running && !st.result && readOpen(st.check)) this.share(space.id, entry.id, null); // known open again
      if (readOpen(st.check)) this.opened(space.id, entry.id, asked); // another window's unknown result, before this check
      cx.notify();
    });
  }

  // Whether Submit may run Push now: a check that passed, not done, with a seen token, under FRESH
  // old, whose caller line says this panel may push; nothing running, and not pushed from here already.
  canSubmit(spaceId, id, now = Date.now()) {
    const st = this.cards[`${spaceId}\0${id}`], c = st?.check;
    return Boolean(c && c.ok === true && !c.done && typeof c.seen === "string" && c.seen && c.caller?.ok === true && !st.busy && !st.running && !st.result && now - st.at < FRESH);
  }

  // From Submit only (main.js answer()): `pa-push-main run`, once. `told(message, cx)` gets the notice for
  // the orchestrator, with this task's live cx (the click's is gone by then). Exit 0: done (or Not now saved); exit 1 or 2 with the tool's refusal JSON:
  // refused or failed, the card stays open; anything else (exit 1 or 2 without that JSON, as when the
  // tool crashed, maybe after the push; another code, a throw or no answer; a Push's exit 0 whose JSON
  // does not say ok, done, with a result): the result is unknown, and the card stays shown (kept) with
  // that line and the follow-up check's verdict until the person leaves it, as after a push.
  // After a refusal or an unknown result the card is checked again. Returns whether it started.
  run(space, entry, label, note, cx, told) {
    const st = this.of(space.id, entry.id);
    if (shownOnly(entry)) return false;
    // Not now (or Other, t-046) while a check runs: not run, and said (t-028); not queued: a run starts only from a Submit click.
    // Other pushes nothing: it is run like Not now, with the person's words as --note (one argv element), never --seen.
    const closes = label === NOT_NOW || label === OTHER;
    if (closes && st.busy && !st.running) { st.notNowWait = label; cx.notify(); return false; }
    const card = entry.card, queue = isQueuePush(card), runs = runsLabel(card, label);
    if (this.locked(space.id, entry.id) || st.busy || (!runs && !closes)) return false; // never a second run from here
    if (label === OTHER && !String(note ?? "").trim()) return false; // an Other answer is its words
    if (runs && !this.canSubmit(space.id, entry.id)) { st.stale = true; cx.notify(); return false; }
    const args = ["run", entry.id, "--answer", label, "--space", localId(space.id), ...(runs ? ["--seen", st.check.seen] : []), "--json", ...(note ? ["--note", note] : [])];
    st.running = label; st.result = null; st.error = null; st.errorFor = label; st.unknown = null; st.stale = false; st.notNowWait = false;
    st.note = label === OTHER ? note : null;
    this.share(space.id, entry.id, label, true, false, st.note);
    cx.spawn(async (cx) => {
      const { r, error } = await call(ctxOf(this.host, cx, true), space, "pa-push-main", args, RUN_TIMEOUT, `run:${space.id}:${entry.id}`);
      st.running = null;
      let notice;
      const out = error ? null : printed(r);
      // The answer the card has, as the tool says (t-045): a card answered before this run (Push, say, when Not now
      // was asked) is shown and told as that answer, never as the label asked.
      const known = queue ? [...pushOptions(card).map((o) => o.label), OTHER] : [PUSH, NOT_NOW, OTHER];
      const answer = r?.code === 0 && known.includes(out?.answer) ? out.answer : label;
      // Exit 0 of a Push is done only when the tool's JSON says so (ok, done, and its result). A queue card's merge answer
      // (t-140) is answered when the tool says ok with its result; done says whether it pushed.
      const pushedSaid = out?.ok === true && (queue || out.done === true) && out.result !== null && typeof out.result === "object";
      if (error || ![0, 1, 2].includes(r?.code) || (r.code !== 0 && out?.ok !== false) || (r.code === 0 && runsLabel(card, answer) && !pushedSaid)) {
        st.unknown = error ?? `pa-push-main run exited with ${r?.code}${r?.code === 0 ? " without a result that says it pushed" : ""}${said(r) ? `: ${said(r)}` : ""}`;
        st.check = null;
        st.kept = true; // the tool may have saved the card answered before it failed: keep it shown with the check's verdict
        this.share(space.id, entry.id, label, false, true, st.note); // every window stays locked until a check of its own reads it open
        notice = `decision ${entry.id} answered ${label}; result unknown (${st.unknown}); the panel checks the card again`;
      } else {
        if (r.code === 0) {
          st.result = { ...(out ?? {}), label: answer }; st.kept = runsLabel(card, answer);
          this.share(space.id, entry.id, answer, false, false, answer === OTHER ? st.note : null);
          let what = String(out?.message || resultLine(out?.result) || "pushed");
          if (what.startsWith(`${entry.id}: `)) what = what.slice(entry.id.length + 2); // the tool's message starts with the card id
          notice = answer !== label ? `decision ${entry.id} was already answered ${answer}; ${what}`
            : queue && runsLabel(card, label) ? `decision ${String(out?.message || `${entry.id} answered ${label}`)}`
            : label === PUSH ? `decision ${entry.id} answered Push; result: ${what}`
            : label === OTHER ? `decision ${entry.id} answered Other (nothing was pushed), in the person's own words: "${oneLine(note)}"` : `decision ${entry.id} answered Not now`;
        } else {
          st.error = text(out?.refusal?.message || out?.message) || said(r) || `pa-push-main run exited with ${r.code}`;
          st.check = null;
          this.share(space.id, entry.id, null); // refused: the card stays open
          notice = `decision ${entry.id} answered ${label}; refused: ${st.error} (the card stays open)`;
        }
      }
      told(notice, cx);
      if (st.check === null && !st.result) this.check(space, entry, cx);
      cx.notify();
    });
    return true;
  }

  // The block drawn inside an open push card, above its options (spec §8.1, §8.2); for an answered one,
  // what this copy saw or what the card records.
  // Every text row it draws is also pushed to `lines` (the card's Copy, t-026; an indented row with two spaces);
  // the Check again and Done links are not.
  block(t, space, entry, onCheck, onDone, lines = []) {
    // Each row's text is selectable (t-026): ids by row order, the same while the rows are.
    const c = entry.card, open = c.status === "open", st = this.cards[`${space.id}\0${entry.id}`];
    const id = (n) => `sel-push-${entry.id}-${n}`;
    const line = (s, color) => { lines.push(String(s)); return div().text_size(12).text_color(color ?? t.muted).child(sel(id(lines.length), s)); };
    const small = (s, color) => { if (s) lines.push(String(s)); return div().text_size(11).text_color(color ?? t.dim).child(sel(id(lines.length), s)); };
    const sub = (s, color, size = 11.5) => { lines.push(`  ${s}`); return div().pl(12).text_size(size).text_color(color).child(sel(id(lines.length), s)); };
    const box = v_flex().id(`push-${entry.id}`).gap(3).p(8).rounded(6).border(1).border_color(t.border).bg(t.background);
    const only = shownOnly(entry);
    if (only) return box.child(line(only, t.blocked)); // never checked or run: nothing else to show
    if (!open) {
      const r = isQueuePush(c) ? null : c.push?.result, qr = isQueuePush(c) ? c.queue.push.result : null;
      if (st?.result) this.shown(t, box, st, {}, lines, entry.id);
      // The last check found the card open, and it has closed since (another route, t-028): that verdict is old.
      if (st?.unknown && !st.busy && st.check?.ok === true) box.child(line(`The result was unknown (${st.unknown}); the card was open when checked and has closed since.`, t.blocked));
      else if (st?.unknown) {
        // A run whose result is unknown, after which the card was read answered: the one check's verdict.
        box.child(line(`The result is unknown (${st.unknown}); checking the card again.`, t.blocked));
        if (st.busy) box.child(line("Checking…", t.dim));
        else if (st.check) box.child(line(st.check.ok === true ? "The check found the card open." : `The check found (${text(st.check.refusal?.check)}): ${text(st.check.refusal?.message)}`));
      }
      if (qr && typeof qr === "object" && !st?.result) box.child(small(`Recorded on the card: ${text(qr.message) || text(qr.state)}`));
      else if (!(isOther(c) && st?.result)) box.child(small(r ? `Recorded on the card: pushed ${text(r.url)} ${text(r.branch)} ${short(r.from)}..${short(r.to)} at ${stamp(r.at)}` : isOther(c) ? OTHER_SAID : "Nothing was pushed from this card."));
      if (st?.kept) box.child(div().id(`push-done-${entry.id}`).flex_none().text_size(11.5).text_color(t.accent).cursor_pointer().on_click(onDone).child("Done"));
      return box;
    }
    const chk = st?.check, f = chk?.facts ?? {}, busy = Boolean(st?.busy), now = Date.now();
    if (chk) box.child(small(`Read now by pa-push-main check (not from the card file), at ${clock(st.at)}`));
    if (isQueuePush(c)) return this.queueBlock(t, box, entry, st, chk, f, busy, now, line, small, sub, onCheck);
    if (f.path) box.child(line(`Repository: ${text(f.path)}`));
    const entryOn = f.allow?.entry;
    if (f.url) box.child(line(`Pushes to: ${text(f.url)}${entryOn ? `, on your allowlist (entry ${entryOn.enabled ? "on" : "off"}, file changed ${stamp(f.allow.mtime)})` : ""}`));
    if (f.allow?.changedSinceCard) box.child(line(`The allowlist changed at ${stamp(f.allow.mtime)}, after this card was made.`, t.blocked));
    if (f.originUrl && f.url && f.originUrl !== f.url) box.child(line(`origin's URL is ${text(f.originUrl)}, not the allowlist's URL.`, t.blocked));
    if (f.branch && f.remoteNow !== undefined && f.remoteNow !== null) box.child(line(`Remote ${text(f.branch)} now: ${short(f.remoteNow)} (checked ${clock(st.at)})`));
    if (f.branch && f.localNow) box.child(line(`Local ${text(f.branch)}: ${short(f.localNow)}${f.merge ? `; fast-forwarded to ${short(f.commit)} first` : ""}`));
    if (Array.isArray(f.commits)) {
      const count = Number.isFinite(f.count) ? f.count : f.commits.length;
      box.child(line(`Commits (${count}):`));
      for (const x of f.commits.slice(0, COMMITS_SHOWN)) box.child(sub(`${short(x?.sha)} ${text(x?.subject)} — ${text(x?.author)}${x?.fromTask === false ? " · already on local main, not from this task" : ""}`,
        x?.fromTask === false ? t.working : t.muted));
      if (count > COMMITS_SHOWN) box.child(sub(`… and ${count - COMMITS_SHOWN} more`, t.dim));
    }
    if (f.commit) box.child(line(`Gated commit: ${short(f.commit)}, full gate passed at ${stamp(f.gate?.at)}, task ${text(c.push?.task ?? c.task)}, branch ${text(f.taskBranch)}`));
    if (Array.isArray(f.commands) && f.commands.length) {
      box.child(line("What will run:"));
      for (const argv of f.commands) box.child(sub(Array.isArray(argv) ? argv.map(text).join(" ") : text(argv), t.muted, 11));
      box.child(small("No force, no tags, no other branches."));
    }
    if (chk?.caller) box.child(line(chk.caller.ok ? "This panel may push: yes" : `This panel may push: no (${text(chk.caller.why)})`, chk.caller.ok ? t.muted : t.blocked));
    // State: what this copy did last, then the check's verdict.
    if (st?.result) this.shown(t, box, st, f, lines, entry.id);
    if (st?.error) box.child(line(`${st.errorFor !== PUSH ? "Not saved" : "Not pushed"}: ${st.error}`, t.blocked));
    if (st?.unknown) box.child(line(`The result is unknown (${st.unknown}); checking the card again.`, t.blocked));
    if (st?.notNowWait) box.child(line(`${st.notNowWait === OTHER ? OTHER : NOT_NOW} was not saved: a check was running. Submit it again.`, t.blocked));
    if (busy) box.child(line("Checking…", t.dim));
    else if (!chk) box.child(small(st?.running ? "" : "Not checked yet."));
    else if (chk.ok !== true) box.child(line(`Push is not possible now (${text(chk.refusal?.check)}): ${text(chk.refusal?.message)}`, t.blocked));
    else if (now - st.at >= FRESH) box.child(line("This check is over 2 minutes old: Check again before pushing.", t.blocked));
    else if (!chk.seen) box.child(line("The check gave no seen token: Check again.", t.blocked));
    else if (chk.caller?.ok === true && !st?.result) box.child(line("Ready: Push pushes exactly this.", t.done));
    if (st?.stale && !busy) box.child(line("Push needs a check that passed, under 2 minutes old: Check again.", t.blocked));
    if (!st?.running && !busy) box.child(div().id(`push-check-${entry.id}`).flex_none().text_size(11.5).text_color(t.accent).cursor_pointer().on_click(onCheck).child("Check again"));
    return box;
  }

  // t-140 R2, R4: the open queue card that pushes: what its options merge now, what waits, the commits each would push,
  // and why a push would not run now, all from the check (never the card file's context).
  queueBlock(t, box, entry, st, chk, f, busy, now, line, small, sub, onCheck) {
    if (f.path) box.child(line(`Repository: ${text(f.path)}`));
    if (f.url) box.child(line(`Pushes to: ${text(f.url)} ${text(f.branch)}${f.allow?.entry ? ", on your allowlist" : ""}`));
    if (f.branch && f.remoteNow) box.child(line(`Remote ${text(f.branch)} now: ${short(f.remoteNow)}; local ${text(f.branch)}: ${short(f.localNow)} (checked ${clock(st.at)})`));
    for (const o of Array.isArray(chk?.options) ? chk.options : []) {
      box.child(line(`${text(o.label)}:`));
      box.child(sub(`Merges now: ${(o.mergeNow ?? []).map(text).join(", ") || "nothing"}`, t.muted));
      if (o.waits) box.child(sub(`Waits: ${text(o.waits.task)}: ${text(o.waits.why)}`, t.working));
      const p = o.push ?? {}, commits = Array.isArray(p.commits) ? p.commits : [];
      if (p.ok) {
        box.child(sub(`Commits to push (${Number.isFinite(p.count) ? p.count : commits.length}):`, t.muted));
        for (const x of commits.slice(0, COMMITS_SHOWN)) box.child(sub(`  ${short(x?.sha)} ${text(x?.subject)} — ${text(x?.author)}`, t.muted, 11));
        if (commits.length > COMMITS_SHOWN) box.child(sub(`  … and ${commits.length - COMMITS_SHOWN} more`, t.dim, 11));
      } else if ((o.mergeNow ?? []).length) box.child(sub(`Not pushed now: ${text(p.refusal?.message)}; the merges would still run and a push card would follow`, t.blocked));
    }
    if (chk?.caller) box.child(line(chk.caller.ok ? "This panel may push: yes" : `This panel may push: no (${text(chk.caller.why)})`, chk.caller.ok ? t.muted : t.blocked));
    if (st?.result) this.shown(t, box, st, f, [], entry.id);
    if (st?.error) box.child(line(`Not saved: ${st.error}`, t.blocked));
    if (st?.unknown) box.child(line(`The result is unknown (${st.unknown}); checking the card again.`, t.blocked));
    if (st?.notNowWait) box.child(line(`${st.notNowWait === OTHER ? OTHER : NOT_NOW} was not saved: a check was running. Submit it again.`, t.blocked));
    if (busy) box.child(line("Checking…", t.dim));
    else if (!chk) box.child(small(st?.running ? "" : "Not checked yet."));
    else if (chk.ok !== true) box.child(line(`Merging and pushing are not possible now (${text(chk.refusal?.check)}): ${text(chk.refusal?.message)}`, t.blocked));
    else if (now - st.at >= FRESH) box.child(line("This check is over 2 minutes old: Check again before answering.", t.blocked));
    else if (!chk.seen) box.child(line("The check gave no seen token: Check again.", t.blocked));
    else if (chk.caller?.ok === true && !st?.result) box.child(line("Ready: a merge option merges and pushes exactly what is listed above.", t.done));
    if (st?.stale && !busy) box.child(line("A merge option needs a check that passed, under 2 minutes old: Check again.", t.blocked));
    if (!st?.running && !busy) box.child(div().id(`push-check-${entry.id}`).flex_none().text_size(11.5).text_color(t.accent).cursor_pointer().on_click(onCheck).child("Check again"));
    return box;
  }

  // What this copy's run printed: the result line and the tail of git's output.
  shown(t, box, st, f, lines = [], cardId = "") {
    const r = st.result.result;
    if (r && typeof r === "object" && typeof r.state === "string") { // t-140: a queue card's run
      const said = st.result.label === OTHER ? "Answered Other: nothing was merged or pushed." : st.result.label === NOT_NOW ? "Answered Not now: nothing was merged or pushed."
        : text(st.result.message) || text(r.message) || text(r.state);
      lines.push(said);
      box.child(div().text_size(12).text_color(r.state === "pushed" || r.state === "already" ? t.done : t.blocked).child(sel(`sel-push-${cardId}-${lines.length}`, said)));
      return;
    }
    const where = `${text(r?.url ?? f.url)} ${text(r?.branch ?? f.branch)}`;
    const said = st.result.label === OTHER ? OTHER_SAID : st.result.label !== PUSH ? "Answered Not now: nothing was pushed."
      : r?.how && r.how !== "pushed" ? `Already on the remote: ${where} is at ${short(r?.to)} (recorded at ${clock(r?.at, false)})`
      : `Pushed ${where} ${short(r?.from)}..${short(r?.to)} at ${clock(r?.at, false)}`;
    const id = () => `sel-push-${cardId}-${lines.length}`; // the block's scheme: one id per row
    lines.push(said);
    box.child(div().text_size(12).text_color(t.done).child(sel(id(), said)));
    const out = Array.isArray(r?.output) ? r.output : text(r?.output).split("\n").filter(Boolean);
    for (const o of out.slice(-15)) { lines.push(`  ${text(o)}`); box.child(div().pl(12).text_size(11).text_color(t.dim).child(sel(id(), text(o)))); }
  }
}

// "pushed <url> <branch> <from7>..<to7>" from a run result, or "".
function resultLine(r) {
  return r && typeof r === "object" ? `pushed ${text(r.url)} ${text(r.branch)} ${short(r.from)}..${short(r.to)}` : "";
}
