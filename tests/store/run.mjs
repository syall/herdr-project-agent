// Store harness (t-012): runs mods/project-agent/store.js against the panel stubs (a slow remote,
// hung calls, the batched card read) and checks how it reads a space. Usage:
//   node tests/store/run.mjs [case ...]
// Prints one line per case; exits non-zero when any case fails.
import fs from "fs";
import os from "os";
import path from "path";
import { execFileSync } from "child_process";

const here = path.dirname(new URL(import.meta.url).pathname);
const src = path.join(here, "..", "..", "mods", "project-agent");
const dst = fs.mkdtempSync(path.join(os.tmpdir(), "t012-store-"));
fs.copyFileSync(path.join(here, "..", "keeper", "stubs.mjs"), path.join(dst, "keeper-stubs.mjs"));
fs.copyFileSync(path.join(here, "..", "panel", "stubs.mjs"), path.join(dst, "stubs.mjs"));
for (const f of fs.readdirSync(src).filter((n) => n.endsWith(".js"))) {
  fs.writeFileSync(path.join(dst, f), fs.readFileSync(path.join(src, f), "utf8").replace(/from "(herdr|gpui-kit|gpui-base)"/g, 'from "./stubs.mjs"'));
}
const stubs = await import(path.join(dst, "stubs.mjs"));
const store = await import(path.join(dst, "store.js"));
const { cx } = stubs;
const g = globalThis;

class Failed extends Error {}
const check = (ok, what) => { if (!ok) throw new Failed(what); };

const T0 = 1_800_000_000_000;
const M = "m1", S = "sp1";
const remote = { id: `${M}/${S}`, name: "proj", machine: M, folder: "/proj", active: true, tabs: [] };
const local = { id: S, name: "proj", machine: null, folder: "/proj", active: true, tabs: [] };
const rdir = `.config/herdr-pa/spaces/${S}`, ldir = `/home/u/.config/herdr-pa/spaces/${S}`;
const keyOf = (space, p) => `${space.machine ?? ""}:${space.machine ? rdir : ldir}${p}`;

function world() {
  g.__now = T0; g.__gen = (g.__gen ?? 0) + 1;
  g.__files = {}; g.__ls = {}; g.__fail = []; g.__hang = []; g.__delay = {}; g.__offline = [];
  g.__reads = []; g.__execs = []; g.__writes = []; g.__issued = [];
  g.__remoteDelay = 0; g.__execHang = []; g.__batchFail = {}; g.__timers = []; g.__execOut = {}; g.__sleepHang = false;
  g.__shared = {};
}
// Cards for both a remote and a local copy of the same space: { name: text | null (listed, file gone) }.
function cards(map) {
  for (const space of [remote, local]) {
    g.__ls[keyOf(space, "/decisions")] = Object.keys(map);
    for (const [n, text] of Object.entries(map)) if (text !== null) g.__files[keyOf(space, `/decisions/${n}`)] = text;
  }
}
const card = (id, extra = {}) => JSON.stringify({ id, status: "open", question: `Q ${id}?`, options: [{ label: "A" }], created: `2026-11-16T10:${String(id.length % 60).padStart(2, "0")}:00Z`, ...extra });
const remoteCalls = (kind) => g.__issued.filter((x) => x.on === M && (!kind || x.kind === kind));
const cardReads = () => g.__issued.filter((x) => x.on === M && x.kind === "read" && x.path.includes("/decisions/"));
// Strip what differs by machine (the path prefix) so remote and local results compare.
const norm = (list) => JSON.stringify(list.map((c) => ({ ...c, path: c.path.replace(/^.*\/decisions\//, "") })));
// Lets pending polls (the stubs' delays) see the clock.
async function settle() { for (let i = 0; i < 25; i++) await new Promise((r) => setTimeout(r, 2)); await stubs.flush(); }
async function moveOn(ms) { g.__now += ms; await settle(); }
async function within(promise, ms, steps = 1) {
  let done = false, value;
  promise.then((v) => { done = true; value = v; }, (e) => { done = true; value = e; });
  await settle(); // the calls go out at the current time
  for (let i = 0; i < steps && !done; i++) await moveOn(ms / steps);
  await settle();
  return { done, value };
}

const cases = {};

cases["S-batch-one-call"] = async () => { // AC2.1: 27 cards on another machine come from one exec, no per-card reads
  world();
  const m = {}; for (let i = 1; i <= 27; i++) m[`d-${String(i).padStart(3, "0")}.json`] = card(`d-${String(i).padStart(3, "0")}`);
  cards(m);
  const list = await store.loadDecisions(remote, { cx });
  check(list.length === 27, `27 cards (got ${list.length})`);
  check(remoteCalls().length === 1 && remoteCalls("batch").length === 1, `exactly 1 remote call, the batch (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
  check(cardReads().length === 0, `no readTextFile under decisions/ (got ${cardReads().length})`);
};

cases["S-parity"] = async () => { // AC2.2: the same cards as the per-card path: validity, errors, id fallback, filter, sort, the 200 cap
  world();
  const m = {
    "d-open.json": card("d-open"),
    "d-answered.json": card("d-answered", { status: "answered", answer: "A", created: "2026-11-16T11:00:00Z" }),
    "noid.json": JSON.stringify({ status: "open", question: "no id", options: [], created: "2026-11-16T09:00:00Z" }),
    "broken.json": "{ not json",
    "notacard.json": JSON.stringify({ id: "x1", question: "no options" }),
    "a b.json": card("spaced"), "x.txt": "nope", "-dash.json": card("-dash"),
  };
  cards(m);
  const batched = await store.loadDecisions(remote, { cx });
  check(remoteCalls("batch").length === 1 && cardReads().length === 0, `the remote path used the batch (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
  const per = await store.loadDecisions(local, { cx });
  check(norm(batched) === norm(per), `batch equals the per-card path\n  batch ${norm(batched)}\n  per   ${norm(per)}`);
  const ids = batched.map((c) => c.id);
  check(!ids.includes("spaced") && batched.length === 6, `the filter drops "a b.json" and "x.txt" (got ${ids})`);
  check(ids.includes("noid") && batched.find((c) => c.id === "broken").card === null && /JSON|Unexpected|Expected/i.test(batched.find((c) => c.id === "broken").error ?? ""), "id fallback and the JSON error");
  check(batched.find((c) => c.id === "x1").error === "not a decision card (needs question and options)", "the not-a-card text");
  check(ids[0] === "d-open" || batched[0].card?.status === "open", `open first (got ${ids})`);
  // 201 cards: the first 200 names, as the per-card path takes them.
  world();
  const big = {}; for (let i = 0; i < 201; i++) big[`c-${String(i).padStart(3, "0")}.json`] = card(`c-${String(i).padStart(3, "0")}`);
  cards(big);
  const b2 = await store.loadDecisions(remote, { cx }), p2 = await store.loadDecisions(local, { cx });
  check(b2.length === 200 && !b2.some((c) => c.id === "c-200"), `200 cap (got ${b2.length})`);
  check(norm(b2) === norm(p2), "the same 200 as the per-card path");
  check(remoteCalls("batch").length === 1, "one batch");
};

cases["S-content"] = async () => { // AC2.3: awkward contents survive; a vanished file is skipped
  world();
  const odd = { id: "odd", status: "open", question: "line1\nline2\ttab \"q\" 'q' $HOME `x` {b} é ü 日本", options: [{ label: "$(rm -rf ~)" }], context: "\\0 not a nul\r\n" };
  cards({ "odd.json": JSON.stringify(odd, null, 2), "gone.json": null, "ok.json": card("ok") });
  const list = await store.loadDecisions(remote, { cx });
  check(remoteCalls().length === 1, `one remote call (got ${remoteCalls().length})`);
  const got = list.find((c) => c.id === "odd");
  check(got && JSON.stringify(got.card) === JSON.stringify(odd), `contents intact (got ${JSON.stringify(got?.card)})`);
  check(!list.some((c) => c.id === "gone") && list.length === 2, `vanished file skipped (got ${list.map((c) => c.id)})`);
};

cases["S-real-sh"] = async () => { // AC2.3: the real script under sh: quoting, NULs, a leading dash, the name filter, a missing folder
  world();
  cards({ "x.json": card("x") });
  await store.loadDecisions(remote, { cx });
  const call = g.__execs.find(([c, a]) => c === "sh" && stubs.execKind(c, a) === "batch");
  check(Boolean(call), "the batch was an sh -c call");
  const [, args] = call;
  check(args[0] === "-c" && args[2] === "sh" && args[3] === `${rdir}/decisions` && !args[1].includes(rdir), `path passed as $1, not in the script (got ${JSON.stringify(args)})`);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "t012-sh-")), d = path.join(tmp, "dir with 'quote' $x");
  fs.mkdirSync(d);
  const texts = { "a.json": card("a", { question: "multi\nline\t\"q\" `b` $y é" }), "-lead.json": card("-lead"), "b c.json": card("bc"), "sub.json": null, "z.txt": "zz" };
  for (const [n, t] of Object.entries(texts)) { if (t === null) fs.mkdirSync(path.join(d, n)); else fs.writeFileSync(path.join(d, n), t); }
  const out = execFileSync("sh", [args[0], args[1], args[2], d]).toString("utf8");
  g.__batchFail = { [M]: { code: 0, stdout: out } };
  g.__issued = [];
  const list = await store.loadDecisions(remote, { cx });
  check(remoteCalls().length === 1, `one call (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
  check(JSON.stringify(list.map((c) => c.id).sort()) === JSON.stringify(["-lead", "a"]), `a and -lead only: "b c.json" is never read, a folder is skipped (got ${list.map((c) => c.id)})`);
  check(!out.includes("bc"), "the filtered name's file was not printed");
  check(list.find((c) => c.id === "a").card.question === "multi\nline\t\"q\" `b` $y é", "text intact through the real shell");
  let code = 0;
  try { execFileSync("sh", [args[0], args[1], args[2], path.join(tmp, "missing")]); } catch (e) { code = e.status; }
  check(code === 3, `a missing folder exits 3 (got ${code})`);
  fs.rmSync(tmp, { recursive: true, force: true });
};

cases["S-fallback"] = async () => { // AC2.4: a failed, hung or unreadable batch falls back to per-card reads, in parallel, losing nothing
  const m = {}; for (const id of ["d-1", "d-2", "d-3"]) m[`${id}.json`] = card(id);
  for (const how of ["exit", "garbage", "hang"]) {
    world(); cards(m);
    if (how === "exit") g.__batchFail = { [M]: { code: 1, stdout: "" } };
    if (how === "garbage") g.__batchFail = { [M]: { code: 0, stdout: "d-1.json\0{}" } };
    if (how === "hang") g.__execHang = [`${M}:batch`];
    const { done, value } = await within(store.loadDecisions(remote, { cx }), 10000);
    check(done && Array.isArray(value), `${how}: settles within 10 s (done ${done}, ${value})`);
    check(value.length === 3, `${how}: all 3 cards (got ${value.map((c) => c.id)})`);
    check(remoteCalls("batch").length === 1 && remoteCalls("ls").length === 1 && cardReads().length === 3, `${how}: batch, then ls and 3 reads (got ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
    const at = cardReads().map((x) => x.at);
    check(new Set(at).size === 1, `${how}: the 3 reads are issued together (at ${at})`);
  }
  world(); cards(m); g.__batchFail = { [M]: { code: 1, stdout: "" } };
  check(await store.loadDecisions(remote, { cx, batchOnly: true }) === null, "batchOnly: a failed batch gives null, no fallback");
  check(remoteCalls().length === 1, "batchOnly: one call");
  world(); // no decisions folder: no cards, no fallback (as today's failing ls)
  const none = await store.loadDecisions(remote, { cx });
  check(Array.isArray(none) && none.length === 0 && remoteCalls().length === 1, `missing folder: [] after 1 call (got ${JSON.stringify(none)}, ${remoteCalls().length} calls)`);
};

cases["S-hung-card"] = async () => { // AC5.2: fallback path, one card read hung: the others come back within 10 s
  world();
  cards({ "d-1.json": card("d-1"), "d-2.json": card("d-2") });
  g.__batchFail = { [M]: { code: 1, stdout: "" } };
  g.__hang = [keyOf(remote, "/decisions/d-2.json")];
  const { done, value } = await within(store.loadDecisions(remote, { cx }), 10000);
  check(done && Array.isArray(value) && value.map((c) => c.id).join() === "d-1", `d-1 only, within 10 s (done ${done}, ${JSON.stringify(value?.map?.((c) => c.id))})`);
};

function agentFiles(space, { profile = "test", ledger = '{"tasks":[]}' } = {}) {
  const home = space.machine ? `${M}:.config/herdr-pa` : ":/home/u/.config/herdr-pa";
  g.__files[keyOf(space, "/agent.json")] = JSON.stringify({ space: S, project: "/proj", profile, name: "proj" });
  g.__files[keyOf(space, "/ledger.json")] = ledger;
  g.__files[keyOf(space, "/schedule.json")] = JSON.stringify({ everyMinutes: 15 });
  g.__files[`${home}/profiles/test/profile.json`] = JSON.stringify({ name: "test" });
  g.__files[`${home}/config.json`] = JSON.stringify({ defaultProfile: "test" });
}

cases["S-parallel-loadspace"] = async () => { // AC3.1: after agent.json, ledger, profile, schedule and pass-request go out together
  world(); agentFiles(remote); g.__remoteDelay = 50;
  const { done, value } = await within(store.loadSpace(remote, undefined, cx), 100, 2);
  check(done && value.agent && value.ledger && value.profile && value.schedule, `settled by 2 round trips (done ${done}, ${JSON.stringify(value)?.slice(0, 200)})`);
  const second = g.__issued.filter((x) => x.on === M && x.at === T0 + 50).map((x) => x.path.split("/").pop());
  check(["ledger.json", "profile.json", "schedule.json", "pass-request.json"].every((n) => second.includes(n)), `4 reads issued at once after agent.json (got ${second})`);
  // No profile in agent.json: config.json first, then the profile: 3 round trips.
  world(); agentFiles(remote, { profile: "" }); g.__remoteDelay = 50;
  const r3 = await within(store.loadSpace(remote, undefined, cx), 150, 3);
  check(r3.done && r3.value.profile?.name === "test", `config.json then the profile, by 3 round trips (done ${r3.done})`);
};

cases["S-errors-same"] = async () => { // AC3.2: the same error lines, in today's order; a thrown read still fails the load as today
  world(); agentFiles(remote, { ledger: "{bad" });
  g.__files[keyOf(remote, "/schedule.json")] = "{bad";
  g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`] = "{bad";
  const r = await store.loadSpace(remote, undefined, cx);
  check(r.errors.length === 3 && /^ledger\.json: /.test(r.errors[0]) && /^schedule\.json: /.test(r.errors[1]) && /^profile test: /.test(r.errors[2]), `errors in today's order (got ${JSON.stringify(r.errors)})`);
  check(r.agent && r.ledger === null && r.schedule === null && r.request === null, "the other reads unaffected");
  world(); agentFiles(remote); g.__fail = [keyOf(remote, "/ledger.json")];
  let threw = null;
  try { await store.loadSpace(remote, undefined, cx); } catch (e) { threw = String(e.message ?? e); }
  check(threw === "read failed", `a thrown read rejects the load, as today (got ${threw})`);
};

cases["S-timeout"] = async () => { // AC5.1: a hung agent.json ends after 10 s with "agent.json: timed out"
  world(); agentFiles(remote); g.__hang = [keyOf(remote, "/agent.json")];
  let done = false, value;
  store.loadSpace(remote, undefined, cx).then((v) => { done = true; value = v; }, (e) => { done = true; value = e; });
  await moveOn(9000);
  check(!done, "not before 10 s");
  await moveOn(1000);
  check(done && value.agent === null && value.timedOut === true && JSON.stringify(value.errors) === '["agent.json: timed out"]', `timed out (done ${done}, ${JSON.stringify(value)})`);
  // A hung ledger: the agent is there, the ledger times out, the rest is read.
  world(); agentFiles(remote); g.__hang = [keyOf(remote, "/ledger.json")];
  const r2 = await within(store.loadSpace(remote, undefined, cx), 10000);
  check(r2.done && r2.value.agent && r2.value.errors.includes("ledger.json: timed out") && r2.value.schedule, `ledger timed out, others read (got ${JSON.stringify(r2.value?.errors)})`);
};

// ---- t-012 Part P fix round ----
const batchOut = (ids) => ids.map((id) => `${id}.json\0${card(id)}\0`).join("");
cases["S-batch-nonzero"] = async () => { // AC2.4 (review P-M1): a batch that exits non-zero is not taken, even with whole-looking output
  const m = {}; for (const id of ["d-1", "d-2", "d-3"]) m[`${id}.json`] = card(id);
  world(); cards(m); g.__batchFail = { [M]: { code: 143, stdout: batchOut(["d-1"]) } }; // killed after one card
  const list = await store.loadDecisions(remote, { cx });
  check(list.length === 3 && remoteCalls("ls").length === 1 && cardReads().length === 3, `fell back: 3 cards from ls and reads (got ${list.map((c) => c.id)}; ${JSON.stringify(remoteCalls().map((x) => x.kind))})`);
  world(); cards(m); g.__batchFail = { [M]: { code: 1, stdout: batchOut(["d-1"]) } };
  check(await store.loadDecisions(remote, { cx, batchOnly: true }) === null, "batchOnly: a non-zero exit gives null");
  world(); cards(m); g.__batchFail = { [M]: { code: 3, stdout: "" } };
  const none = await store.loadDecisions(remote, { cx });
  check(Array.isArray(none) && none.length === 0 && remoteCalls().length === 1, "exit 3 with no output: still no folder, no fallback");
};

cases["S-not-live-null"] = async () => { // AC5.3a: a load no longer wanted gets null (no result), not an empty list it could commit
  world(); cards({ "d-1.json": card("d-1") }); g.__batchFail = { [M]: { code: 1, stdout: "" } };
  const r = await store.loadDecisions(remote, { cx, live: () => false });
  check(r === null && remoteCalls().length === 1, `null after the batch, no fallback (got ${JSON.stringify(r)}, ${remoteCalls().length} calls)`);
};

cases["S-progress"] = async () => { // AC5.3: a read settling or reaching its limit counts as progress for the load that made it
  world(); cards({ "d-1.json": card("d-1") });
  let n = 0; const rcx = { sleep: (ms) => cx.sleep(ms), progress: () => { n++; } };
  await store.loadDecisions(remote, { cx: rcx });
  check(n === 1, `one progress for the batch (got ${n})`);
  world(); g.__hang = [keyOf(remote, "/agent.json")]; n = 0;
  const r = await within(store.loadSpace(remote, undefined, rcx), 10000);
  check(r.done && n === 1, `a read that times out is progress too (got ${n})`);
};

cases["S-load-ledger"] = async () => { // AC8.5: the hidden refresh's ledger read: one read, capped at 10 s
  world(); g.__files[keyOf(remote, "/ledger.json")] = '{"tasks":[{"id":"t-1"}]}';
  const r = await store.loadLedger(remote, cx);
  check(r.value?.tasks?.[0]?.id === "t-1" && !r.error && remoteCalls().length === 1, `the ledger (got ${JSON.stringify(r)})`);
  world(); g.__files[keyOf(remote, "/ledger.json")] = "{bad";
  check((await store.loadLedger(remote, cx)).error && true, "invalid JSON: an error");
  world(); g.__hang = [keyOf(remote, "/ledger.json")];
  const h = await within(store.loadLedger(remote, cx), 10000);
  check(h.done && h.value.timedOut === true, `hung: timed out within 10 s (got ${JSON.stringify(h.value)})`);
};

cases["S-load-profile"] = async () => { // AC8.5: the hidden panel's profile read: answered or not, and the profile
  world(); agentFiles(remote); g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`] = JSON.stringify({ name: "test", mainBranches: ["trunk"] });
  let r = await store.loadProfile(remote, undefined, cx);
  check(r.answered === true && JSON.stringify(r.profile?.mainBranches) === '["trunk"]' && remoteCalls().length === 2, `agent.json then the profile (got ${JSON.stringify(r)}, ${remoteCalls().length} calls)`);
  world(); agentFiles(remote, { profile: "" });
  r = await store.loadProfile(remote, undefined, cx);
  check(r.answered && r.profile?.name === "test" && remoteCalls().length === 3, `through config.json (got ${JSON.stringify(r)})`);
  world(); agentFiles(remote); delete g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`];
  r = await store.loadProfile(remote, undefined, cx);
  check(r.answered && r.profile === null, `a missing profile is an answer (got ${JSON.stringify(r)})`);
  world();
  r = await store.loadProfile(remote, undefined, cx);
  check(r.answered && r.profile === undefined, `no agent.json: answered, nothing to write (got ${JSON.stringify(r)})`);
  world(); agentFiles(remote); g.__hang = [keyOf(remote, "/agent.json")];
  const h = await within(store.loadProfile(remote, undefined, cx), 10000);
  check(h.done && h.value.answered === false, `hung agent.json: not answered within 10 s (got ${JSON.stringify(h.value)})`);
  world(); agentFiles(remote); g.__files[`${M}:.config/herdr-pa/profiles/test/profile.json`] = "{bad";
  check((await store.loadProfile(remote, undefined, cx)).answered === false, "an invalid profile is not an answer");
};

const pick = process.argv.slice(2);
let failed = 0;
for (const [name, fn] of Object.entries(cases)) {
  if (pick.length && !pick.includes(name)) continue;
  try { await fn(); console.log(`PASS ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}: ${e instanceof Failed ? e.message : e.stack}`); }
}
fs.rmSync(dst, { recursive: true, force: true });
console.log(failed ? `${failed} case(s) failed` : "all store cases passed");
process.exit(failed ? 1 : 0);
