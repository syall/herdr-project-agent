// t-060 keep awake: the panel's cases for mods/project-agent/awake.js, registered by run.mjs (register(cases, h)).
// No case runs the real caffeinate or holds a real assertion: exec is stubbed (__execFn), and A7 runs awake.js's own
// script with a fake caffeinate (a sh script in a temp folder that only waits for its -w pid).
import fs from "fs";
import os from "os";
import path from "path";
import { execFileSync } from "child_process";

// A Map-backed stand-in for the runtime's bare `localStorage` (a file that survives a restart).
export function fakeStorage(init = {}) {
  const m = new Map(Object.entries(init));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, removeItem: (k) => { m.delete(k); }, clear: () => m.clear(), get length() { return m.size; }, key: (i) => [...m.keys()][i] ?? null, flush: async () => {} };
}

export async function register(cases, h) {
  const { world, mount, look, click, check, settle, fire, advance, dst } = h;
  const g = globalThis;
  // Before awake.js exists (the failing-first commit) every case fails with that, and the rest of the harness runs.
  const A = await import(path.join(dst, "awake.js")).catch(() => null);
  if (!A) { for (const n of ["A1-one-hold", "A1-two-copies", "A2-toggle", "A2-toggle-other-copy", "A3-default-and-restart", "A4-says-limits", "A5-not-macos", "A6-not-holder", "A8-failure", "A7-script-fake-caffeinate"]) cases[n] = async () => check(false, "mods/project-agent/awake.js is missing"); return; }
  const calls = () => (g.__execs ?? []).filter(([c, a]) => c === "sh" && a?.[1] === A.SCRIPT).map(([, a]) => a.slice(3));
  // A fresh world, the given storage and platform, exec answering the keep-awake script as a working Mac would.
  const setup = ({ storage = fakeStorage(), platform = "macos", reply = null } = {}) => {
    world();
    g.localStorage = storage; g.__platform = platform;
    g.__execFn = (command, args) => {
      if (command !== "sh" || args?.[1] !== A.SCRIPT) return undefined;
      if (reply) return reply(args[3]);
      return { code: 0, stdout: args[3] === "start" ? "held 5001 4001\n" : "off\n", stderr: "" };
    };
  };
  const hold = (v) => { g.__shared["project-agent.timer"] = { id: v.me, at: g.__now }; }; // v holds the timer lease
  const done = () => { g.__platform = undefined; };
  const wrap = (fn) => async () => { try { await fn(); } finally { done(); } };

  cases["A1-one-hold"] = wrap(async () => { // VP1, VP2: the holder starts one caffeinate -d -i -u -w <Herdr>; ticks add none
    setup();
    const v = await mount(); hold(v);
    await fire(1000);
    let c = calls();
    check(c.length === 1 && c[0][0] === "start" && c[0][1] === "Herdr" && c[0][2] === "/usr/bin/caffeinate", `one start for Herdr (got ${JSON.stringify(c)})`);
    check(/-d -i -u -w "\$pid"/.test(A.SCRIPT), "the script holds -d -i -u -w <Herdr's pid>");
    await fire(1000); await fire(1000);
    check(calls().length === 1, `no second start while it holds (got ${calls().length})`);
    check(/Keep this Mac awake while Herdr runs: on/.test(look(v).text) && /holding/.test(look(v).text), `bar says on, holding (got ${look(v).text.slice(0, 600)})`);
    advance(31000); await fire(1000);
    c = calls();
    check(c.length === 2 && c[1][0] === "start", `after 30 s it checks again with an idempotent start (got ${JSON.stringify(c)})`);
  });

  cases["A1-two-copies"] = wrap(async () => { // VP2: two panel copies, one hold
    setup();
    const v = await mount(), w = await mount(); hold(v);
    await fire(1000); await fire(1000);
    check(calls().length === 1, `one start for two copies (got ${JSON.stringify(calls())})`);
    check(/holding/.test(look(w).text), `the other copy shows the hold (got ${look(w).text.slice(0, 600)})`);
  });

  cases["A2-toggle"] = wrap(async () => { // VP2, VP3: off ends it at once, on starts it again
    setup();
    const v = await mount(); hold(v);
    await fire(1000);
    await click(v, "awake-toggle"); await settle();
    let c = calls();
    check(c.length === 2 && c[1][0] === "stop", `Turn off stops at once (got ${JSON.stringify(c)})`);
    check(g.localStorage.getItem("project-agent.awake") === "off", `kept off (got ${g.localStorage.getItem("project-agent.awake")})`);
    check(/Keep this Mac awake while Herdr runs: off/.test(look(v).text) && Boolean(look(v).clicks["awake-toggle"]) && /Turn on/.test(look(v).text), `bar says off with Turn on (got ${look(v).text.slice(0, 600)})`);
    await fire(1000);
    check(calls().length === 2, "no call while off");
    await click(v, "awake-toggle"); await settle();
    c = calls();
    check(c.length === 3 && c[2][0] === "start", `Turn on starts it again (got ${JSON.stringify(c)})`);
    check(g.localStorage.getItem("project-agent.awake") === "on", "kept on");
  });

  cases["A2-toggle-other-copy"] = wrap(async () => { // VP2: a click in a copy that does not hold the lease: the holder acts
    setup();
    const v = await mount(), w = await mount(); hold(v);
    await fire(1000);
    await click(w, "awake-toggle");
    check(calls().length === 1, "the clicked copy runs nothing itself");
    check(/runs: off/.test(look(w).text), `the clicked copy shows off at once (got ${look(w).text.slice(0, 400)})`);
    await fire(1000);
    const c = calls();
    check(c.length === 2 && c[1][0] === "stop", `the holder stops it within its 1 s tick (got ${JSON.stringify(c)})`);
  });

  cases["A3-default-and-restart"] = wrap(async () => { // VP3: on by default; the choice survives a restart
    const storage = fakeStorage();
    setup({ storage });
    let v = await mount(); hold(v);
    check(/runs: on/.test(look(v).text), `on by default (got ${look(v).text.slice(0, 400)})`);
    await click(v, "awake-toggle"); await settle();
    setup({ storage }); // Herdr restarted: new shared state, the same storage file
    v = await mount(); hold(v);
    check(/runs: off/.test(look(v).text), `still off after a restart (got ${look(v).text.slice(0, 400)})`);
    await fire(1000);
    check(calls().every((x) => x[0] === "stop"), `nothing started while off (got ${JSON.stringify(calls())})`);
  });

  cases["A4-says-limits"] = wrap(async () => { // VP1, VP4: the bar says the manual lock, the lid, and that remote machines are not affected
    setup();
    const v = await mount();
    const { text } = look(v);
    check(/still lock the screen by hand/.test(text), `manual lock (got ${text.slice(0, 600)})`);
    check(/Closing the lid still sleeps the Mac/.test(text), "the lid");
    check(/Remote machines you work on are not affected/.test(text), "remote machines");
  });

  cases["A5-not-macos"] = wrap(async () => { // VP5: does nothing and says so
    setup({ platform: "linux" });
    const v = await mount(); hold(v);
    await fire(1000); advance(31000); await fire(1000);
    check(calls().length === 0, `no call off macOS (got ${JSON.stringify(calls())})`);
    check(/macOS only/.test(look(v).text) && !look(v).clicks["awake-toggle"], `says macOS only, no toggle (got ${look(v).text.slice(0, 400)})`);
  });

  cases["A6-not-holder"] = wrap(async () => { // VP2: a copy without the timer lease never holds
    setup();
    const v = await mount();
    await fire(1000); advance(31000); await fire(1000);
    check(calls().length === 0, `no call without the lease (got ${JSON.stringify(calls())})`);
    check(Boolean(v), "mounted");
  });

  cases["A8-failure"] = wrap(async () => { // a hold that could not start says so, and is tried again after 30 s, not every second
    setup({ reply: () => ({ code: 3, stdout: "no Herdr process above this one\n", stderr: "" }) });
    const v = await mount(); hold(v);
    await fire(1000);
    check(/could not keep it awake/.test(look(v).text), `says it could not (got ${look(v).text.slice(0, 600)})`);
    await fire(1000); advance(5000); await fire(1000);
    check(calls().length === 1, `no retry within 30 s (got ${calls().length})`);
    advance(31000); await fire(1000);
    check(calls().length === 2, `retried after 30 s (got ${calls().length})`);
  });

  cases["A7-script-fake-caffeinate"] = async () => { // VP1, VP2: the real script, a fake caffeinate: one hold, idempotent, stop ends it
    const T = fs.mkdtempSync(path.join(os.tmpdir(), "t060-"));
    const fake = path.join(T, "caffeinate");
    fs.writeFileSync(fake, '#!/bin/sh\n# a fake caffeinate: holds nothing, waits for its -w pid\nwhile kill -0 "$5" 2>/dev/null; do sleep 0.2; done\n');
    fs.chmodSync(fake, 0o755);
    const owner = path.basename(process.execPath); // the script's ancestor here is this node process, standing for Herdr
    const pat = `caffeinate -d -i -u -w ${process.pid}$`;
    const run = (act) => { try { return { code: 0, out: execFileSync("sh", ["-c", A.SCRIPT, "sh", act, owner, fake], { encoding: "utf8", timeout: 15000 }) }; } catch (e) { return { code: e.status, out: String(e.stdout ?? "") }; } };
    const running = () => { try { return execFileSync("pgrep", ["-f", pat], { encoding: "utf8" }).trim().split(/\s+/).filter(Boolean); } catch { return []; } };
    try {
      check(running().length === 0, "no fake hold before");
      const a = run("start");
      check(a.code === 0 && new RegExp(`^held \\d+ ${process.pid}$`).test(a.out.trim()), `start holds for this pid (got ${JSON.stringify(a)})`);
      check(running().length === 1, `one fake running (got ${running()})`);
      const b = run("start");
      check(b.code === 0 && b.out.trim() === a.out.trim() && running().length === 1, `start again keeps the one (got ${JSON.stringify(b)}, ${running()})`);
      const c = run("stop");
      check(c.code === 0 && c.out.trim() === "off", `stop (got ${JSON.stringify(c)})`);
      for (let i = 0; i < 50 && running().length; i++) await new Promise((r) => setTimeout(r, 100));
      check(running().length === 0, `nothing left running (got ${running()})`);
      const d = (() => { try { execFileSync("sh", ["-c", A.SCRIPT, "sh", "start", "no-such-owner-t060", fake], { encoding: "utf8", timeout: 15000 }); return 0; } catch (e) { return e.status; } })();
      check(d === 3 && running().length === 0, `no owner found: exit 3, nothing started (got ${d})`);
    } finally {
      try { execFileSync("pkill", ["-f", pat]); } catch { /* none left */ }
      fs.rmSync(T, { recursive: true, force: true });
    }
  };
}
