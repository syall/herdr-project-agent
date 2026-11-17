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
import { exec } from "herdr";
import { isPush, localId, withTimeout, TIMED_OUT } from "./store.js";

export { isPush };
export const PUSH = "Push", NOT_NOW = "Not now";
export const CHECK_EVERY = 30000; // checks not asked for by a click: at most one per card this often
export const FRESH = 120000; // Push needs a check started less than this long ago
const CHECK_TIMEOUT = 60000, RUN_TIMEOUT = 600000, ALLOW_TIMEOUT = 30000;
const GRACE = 5000; // an exec still unsettled this long after its own timeout is given up here
const COMMITS_SHOWN = 20;
// The options a push card shows, whatever its file says: pa-push-main run takes only these.
export const PUSH_OPTIONS = [
  { label: PUSH, detail: "Push exactly what is listed above, as a fast-forward. No force." },
  { label: NOT_NOW, detail: "Nothing is pushed. The card closes." },
];

const short = (sha) => (typeof sha === "string" && sha ? sha.slice(0, 7) : "none");
const clock = (iso, seconds = true) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "?" : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) }); };
const stamp = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? String(iso ?? "?") : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
const text = (v) => (v === null || v === undefined ? "" : String(v));
const sleeper = (cx) => ({ sleep: (ms) => cx.sleep(ms) });
const failed = (message) => ({ ok: false, refusal: { check: "panel", message } });

// The JSON object a pa-push-main/pa-push-allow command printed, or null.
function printed(r) {
  try { const v = JSON.parse(String(r?.stdout ?? "")); return v && typeof v === "object" && !Array.isArray(v) ? v : null; } catch { return null; }
}
const said = (r) => String(r?.stderr || r?.stdout || "").trim().slice(0, 600);

// One exec of the tool on the space's machine, given up GRACE after its own timeout (an exec that never
// settles). { r } or { error }.
async function call(cx, space, command, args, timeout) {
  try {
    const r = await withTimeout(sleeper(cx), exec(command, args, { on: space.machine ?? null, timeout }), timeout + GRACE);
    return r === TIMED_OUT ? { error: `${command} ${args[0]} did not answer in ${Math.round((timeout + GRACE) / 1000)} s` } : { r };
  } catch (e) { return { error: String(e.message ?? e) }; }
}

// `pa-push-allow show --json` (read-only), for the Rules view: its report, or { error }.
export async function readAllow(space, cx) {
  const { r, error } = await call(cx, space, "pa-push-allow", ["show", "--json", "--space", localId(space.id)], ALLOW_TIMEOUT);
  if (error) return { error };
  return printed(r) ?? { error: said(r) || `pa-push-allow exited with ${r?.code}` };
}

export class PushCards {
  constructor() {
    // `${space id}\0${card id}` -> { check, at, asked, busy, running, result, error, errorFor, unknown, stale }
    //   check: the last check's JSON; at: when that check was asked for; running: the label being answered.
    this.cards = {};
  }

  of(spaceId, id) { return (this.cards[`${spaceId}\0${id}`] ??= { check: null, at: 0, asked: 0, busy: false, running: null, result: null, error: null, unknown: null, stale: false }); }
  running(spaceId, id) { return this.cards[`${spaceId}\0${id}`]?.running ?? null; }

  // From a shown copy's commit (main.js): the first check of an open push card, at most one per CHECK_EVERY.
  wantCheck(space, entry, cx) {
    if (!isPush(entry?.card) || entry.card.status !== "open") return;
    const st = this.of(space.id, entry.id);
    if (st.check || st.busy || st.running || (st.asked && Date.now() - st.asked < CHECK_EVERY)) return;
    this.check(space, entry, cx);
  }

  // `pa-push-main check <id> --space <id> --json`: read-only; its facts are what the block shows.
  check(space, entry, cx) {
    const st = this.of(space.id, entry.id);
    if (st.busy || st.running) return;
    const asked = Date.now();
    st.busy = true; st.asked = asked; st.stale = false;
    cx.spawn(async (cx) => {
      const { r, error } = await call(cx, space, "pa-push-main", ["check", entry.id, "--space", localId(space.id), "--json"], CHECK_TIMEOUT);
      st.check = error ? failed(`pa-push-main check could not run: ${error}`) : printed(r) ?? failed(`pa-push-main check printed no result${said(r) ? `: ${said(r)}` : ` (exit ${r?.code})`}`);
      st.at = asked; st.busy = false;
      cx.notify();
    });
  }

  // Whether Submit may run Push now: a check that passed, not done, with a seen token, under FRESH
  // old, whose caller line says this panel may push; nothing running, and not pushed from here already.
  canSubmit(spaceId, id, now = Date.now()) {
    const st = this.cards[`${spaceId}\0${id}`], c = st?.check;
    return Boolean(c && c.ok === true && !c.done && typeof c.seen === "string" && c.seen && c.caller?.ok === true && !st.busy && !st.running && !st.result && now - st.at < FRESH);
  }

  // From Submit only (main.js answer()): `pa-push-main run`, once. `told(message)` gets the notice for
  // the orchestrator. Exit 0: done (or Not now saved); exit 1 or 2: refused or failed, the card stays
  // open; anything else, a throw or no answer: the result is unknown. After a refusal or an unknown
  // result the card is checked again. Returns whether it started.
  run(space, entry, label, note, cx, told) {
    const st = this.of(space.id, entry.id);
    if (st.running || st.busy || (label !== PUSH && label !== NOT_NOW)) return false;
    if (label === PUSH && !this.canSubmit(space.id, entry.id)) { st.stale = true; cx.notify(); return false; }
    const args = ["run", entry.id, "--answer", label, "--space", localId(space.id), ...(label === PUSH ? ["--seen", st.check.seen] : []), "--json", ...(note ? ["--note", note] : [])];
    st.running = label; st.result = null; st.error = null; st.errorFor = label; st.unknown = null; st.stale = false;
    cx.spawn(async (cx) => {
      const { r, error } = await call(cx, space, "pa-push-main", args, RUN_TIMEOUT);
      st.running = null;
      let notice;
      if (error || ![0, 1, 2].includes(r?.code)) {
        st.unknown = error ?? `pa-push-main run exited with ${r?.code}${said(r) ? `: ${said(r)}` : ""}`;
        st.check = null;
        notice = `decision ${entry.id} answered ${label}; result unknown (${st.unknown}); the panel checks the card again`;
      } else {
        const out = printed(r);
        if (r.code === 0) {
          st.result = { label, ...(out ?? {}) };
          let what = String(out?.message || resultLine(out?.result) || "pushed");
          if (what.startsWith(`${entry.id}: `)) what = what.slice(entry.id.length + 2); // the tool's message starts with the card id
          notice = label === PUSH ? `decision ${entry.id} answered Push; result: ${what}` : `decision ${entry.id} answered Not now`;
        } else {
          st.error = text(out?.refusal?.message || out?.message) || said(r) || `pa-push-main run exited with ${r.code}`;
          st.check = null;
          notice = `decision ${entry.id} answered ${label}; refused: ${st.error} (the card stays open)`;
        }
      }
      told(notice);
      if (st.check === null && !st.result) this.check(space, entry, cx);
      cx.notify();
    });
    return true;
  }

  // The block drawn inside an open push card, above its options (spec §8.1, §8.2); for an answered one,
  // what this copy saw or what the card records.
  block(t, space, entry, onCheck) {
    const c = entry.card, open = c.status === "open", st = this.cards[`${space.id}\0${entry.id}`];
    const line = (s, color) => div().text_size(12).text_color(color ?? t.muted).child(s);
    const small = (s, color) => div().text_size(11).text_color(color ?? t.dim).child(s);
    const box = v_flex().id(`push-${entry.id}`).gap(3).p(8).rounded(6).border(1).border_color(t.border).bg(t.background);
    if (!open) {
      const r = c.push?.result;
      return box.child(small(r ? `Recorded on the card: pushed ${text(r.url)} ${text(r.branch)} ${short(r.from)}..${short(r.to)} at ${stamp(r.at)}` : "Nothing was pushed from this card."));
    }
    const chk = st?.check, f = chk?.facts ?? {}, busy = Boolean(st?.busy), now = Date.now();
    if (chk) box.child(small(`Read now by pa-push-main check (not from the card file), at ${clock(st.at)}`));
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
      for (const x of f.commits.slice(0, COMMITS_SHOWN)) box.child(div().pl(12).text_size(11.5).text_color(x?.fromTask === false ? t.working : t.muted)
        .child(`${short(x?.sha)} ${text(x?.subject)} — ${text(x?.author)}${x?.fromTask === false ? " · already on local main, not from this task" : ""}`));
      if (count > COMMITS_SHOWN) box.child(div().pl(12).text_size(11.5).text_color(t.dim).child(`… and ${count - COMMITS_SHOWN} more`));
    }
    if (f.commit) box.child(line(`Gated commit: ${short(f.commit)}, full gate passed at ${stamp(f.gate?.at)}, task ${text(c.push?.task ?? c.task)}, branch ${text(f.taskBranch)}`));
    if (Array.isArray(f.commands) && f.commands.length) {
      box.child(line("What will run:"));
      for (const argv of f.commands) box.child(div().pl(12).text_size(11).text_color(t.muted).child(Array.isArray(argv) ? argv.map(text).join(" ") : text(argv)));
      box.child(small("No force, no tags, no other branches."));
    }
    if (chk?.caller) box.child(line(chk.caller.ok ? "This panel may push: yes" : `This panel may push: no (${text(chk.caller.why)})`, chk.caller.ok ? t.muted : t.blocked));
    // State: what this copy did last, then the check's verdict.
    if (st?.result) {
      const r = st.result.result;
      const where = `${text(r?.url ?? f.url)} ${text(r?.branch ?? f.branch)}`;
      box.child(line(st.result.label !== PUSH ? "Answered Not now: nothing was pushed."
        : r?.how && r.how !== "pushed" ? `Already on the remote: ${where} is at ${short(r?.to)} (recorded at ${clock(r?.at, false)})`
        : `Pushed ${where} ${short(r?.from)}..${short(r?.to)} at ${clock(r?.at, false)}`, t.done));
      const out = Array.isArray(r?.output) ? r.output : text(r?.output).split("\n").filter(Boolean);
      for (const o of out.slice(-15)) box.child(div().pl(12).text_size(11).text_color(t.dim).child(text(o)));
    }
    if (st?.error) box.child(line(`${st.errorFor === NOT_NOW ? "Not saved" : "Not pushed"}: ${st.error}`, t.blocked));
    if (st?.unknown) box.child(line(`The result is unknown (${st.unknown}); checking the card again.`, t.blocked));
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
}

// "pushed <url> <branch> <from7>..<to7>" from a run result, or "".
function resultLine(r) {
  return r && typeof r === "object" ? `pushed ${text(r.url)} ${text(r.branch)} ${short(r.from)}..${short(r.to)}` : "";
}
