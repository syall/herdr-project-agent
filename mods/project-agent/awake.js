// Keep awake (t-060): while Herdr runs on a Mac and the toggle is on, one `caffeinate -d -i -u -w <Herdr's pid>` holds
// the Mac awake: no idle sleep (-i), no display sleep or dimming and so no automatic lock (-d), the user counted active
// (-u). A lock by hand still works (an assertion only stops idle timers). It ends when Herdr quits or crashes (-w).
// One hold per machine: only the copy holding the timer lease runs the script, and the script itself keeps at most one
// caffeinate for Herdr's pid (pgrep on its exact arguments). The toggle is on by default, kept in localStorage (it
// survives a restart) and mirrored to shared AWAKE so every copy sees a click at once. Off macOS it does nothing.
// Every call goes through host.js (key "awake", a limit, the budget).

import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { shared, setShared, window as herdrWindow } from "herdr";
import { run as hostRun, gaveUp } from "./host.js";

export const AWAKE = "project-agent.awake"; // shared { on, at }, and the localStorage key ("on" | "off")
export const AWAKE_STATE = "project-agent.awakeState"; // shared { state: held | off | failed, why?, at }: the holder's last answer
export const RECHECK = 30000; // while on, the holder runs its (idempotent) start again this often; a failure is retried after it
export const OWNER = "Herdr"; // the command name of Herdr's own process (/Applications/Herdr.app/Contents/MacOS/Herdr)
export const CAFFEINATE = "/usr/bin/caffeinate";
const LIMIT = 12000;

// sh -c SCRIPT sh <start|stop> <owner name> <caffeinate path>. Finds the nearest ancestor whose command's last part is
// the owner name (none: exit 3, nothing starts). start: keeps the oldest caffeinate already holding for that pid and
// stops any other, or starts one detached (exit 4 if it is not seen running within 2 s); prints "held <its pid> <owner
// pid>". stop: stops every one for that pid; prints "off".
export const SCRIPT = [
  'act=$1 owner=$2 caf=$3 p=$PPID pid=',
  'while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null; do',
  '  set -- $(ps -o ppid= -o comm= -p "$p" 2>/dev/null)',
  '  [ $# -ge 2 ] || break',
  '  pp=$1; shift; c="$*"',
  '  if [ "${c##*/}" = "$owner" ]; then pid=$p; break; fi',
  '  p=$pp',
  'done',
  '[ -n "$pid" ] || { echo "no $owner process above this one"; exit 3; }',
  'pat="caffeinate -d -i -u -w $pid\\$"',
  'if [ "$act" = stop ]; then pkill -f "$pat"; echo off; exit 0; fi',
  'old=$(pgrep -o -f "$pat")',
  'if [ -z "$old" ]; then',
  '  nohup "$caf" -d -i -u -w "$pid" </dev/null >/dev/null 2>&1 &',
  '  i=0',
  '  while [ $i -lt 20 ]; do old=$(pgrep -o -f "$pat") && break; sleep 0.1; i=$((i + 1)); done',
  '  [ -n "$old" ] || { echo "caffeinate did not start"; exit 4; }',
  'fi',
  'for x in $(pgrep -f "$pat"); do [ "$x" = "$old" ] || kill "$x" 2>/dev/null; done',
  'echo "held $old $pid"',
].join("\n");

export const isMac = () => { try { return herdrWindow()?.platform === "macos"; } catch { return false; } };
const storage = () => globalThis.localStorage ?? null;
// The toggle: a click in any copy (shared), else what was kept across restarts, else on.
export function wanted() {
  const s = shared(AWAKE);
  if (typeof s?.on === "boolean") return s.on;
  try { return storage()?.getItem(AWAKE) !== "off"; } catch { return true; }
}

export class Awake {
  // mine(): whether this copy holds the timer lease (the one copy that runs the script).
  constructor(mine, host) {
    this.mine = mine; this.host = host;
    this.applied = null; // what the last answered call made true (true: held, false: off), or null
    this.at = 0; // when the last call started
    this.failed = null; // { at, on }: the last call failed; tried again after RECHECK
  }

  // From the 1 s timer (and a click in the holder): run start or stop when the toggle and the last answer differ,
  // and while on, start again every RECHECK (a hold that ended comes back).
  tick(cx) {
    if (!this.mine() || !isMac() || this.host.busy("awake")) return;
    const on = wanted(), now = Date.now();
    if (this.failed && this.failed.on === on && now - this.failed.at < RECHECK) return;
    if (!this.failed && on === this.applied && (!on || now - this.at < RECHECK)) return;
    this.at = now;
    this.host.spawn(cx, async (cx) => {
      let r;
      try {
        r = await this.host.call(cx, "awake", () => hostRun("sh", ["-c", SCRIPT, "sh", on ? "start" : "stop", OWNER, CAFFEINATE], { timeout: 10000 }),
          { limit: LIMIT, what: "the keep-awake call", background: false });
      } catch (e) { r = { code: -1, stdout: "", stderr: String(e?.message ?? e) }; }
      const ok = !gaveUp(r) && r?.code === 0;
      const why = gaveUp(r) ? "the call did not answer" : String(r?.stdout || r?.stderr || `exit ${r?.code}`).trim().slice(0, 200);
      if (ok) { this.applied = on; this.failed = null; } else { this.applied = null; this.failed = { at: Date.now(), on }; }
      setShared(AWAKE_STATE, ok ? { state: on ? "held" : "off", at: Date.now() } : { state: "failed", on, why, at: Date.now() });
      cx.notify?.();
    }, { what: "the keep-awake call", background: false });
  }

  // The panel's toggle: kept for restarts, shown to every copy at once; the holder acts in its next tick (this one now).
  toggle(cx) {
    const on = !wanted();
    try { storage()?.setItem(AWAKE, on ? "on" : "off"); } catch { /* not kept: shared still carries it until a restart */ }
    setShared(AWAKE, { on, at: Date.now() });
    this.failed = null;
    this.tick(cx);
    cx.notify();
  }

  // One row under the schedule bar: the state, the toggle, and what it does not cover.
  bar(t) {
    const row = v_flex().px(12).py(6).gap(2).flex_none().border_b(1).border_color(t.border).text_size(11.5);
    if (!isMac()) return row.child(div().text_color(t.dim).child("Keep awake works on macOS only: it does nothing on this system."));
    const on = wanted(), s = shared(AWAKE_STATE);
    const status = !on ? (s?.state === "held" ? " · stopping…" : "")
      : s?.state === "held" ? " · holding" : s?.state === "failed" && s.on !== false ? ` · could not keep it awake: ${s.why}` : " · starting…";
    return row
      .child(h_flex().gap(6).items_center()
        .child(div().flex_1().min_w(0).text_color(s?.state === "failed" && on ? t.blocked : t.muted).child(`Keep this Mac awake while Herdr runs: ${on ? "on" : "off"}${status}`))
        .child(div().id("awake-toggle").flex_none().px(7).h(22).flex().items_center().rounded(6).text_size(11.5).cursor_pointer()
          .bg(on ? t.selected : t.chrome).border(1).border_color(on ? t.accent : t.border).text_color(on ? t.foreground : t.muted).hover((x) => x.bg(t.hover))
          .on_click((_e, cx) => this.toggle(cx)).child(on ? "Turn off" : "Turn on")))
      .child(div().text_size(10.5).text_color(t.dim).child("While on: no idle sleep, no dimming, no automatic lock; you can still lock the screen by hand. Closing the lid still sleeps the Mac. Remote machines you work on are not affected."));
  }
}
