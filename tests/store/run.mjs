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
  g.__remoteDelay = 0; g.__execHang = []; g.__batchFail = {}; g.__timers = []; g.__execOut = {}; g.__sleepHang = false; g.__execFail = []; g.__execFn = null;
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
  // 201 open cards: every one (t-061: the cap never drops an open card), as the per-card path takes them.
  world();
  const big = {}; for (let i = 0; i < 201; i++) big[`c-${String(i).padStart(3, "0")}.json`] = card(`c-${String(i).padStart(3, "0")}`);
  cards(big);
  const b2 = await store.loadDecisions(remote, { cx }), p2 = await store.loadDecisions(local, { cx });
  check(b2.length === 201 && b2.some((c) => c.id === "c-200") && !b2.omitted, `201 open cards, all kept (got ${b2.length}, omitted ${b2.omitted})`);
  check(norm(b2) === norm(p2), "the same 201 as the per-card path");
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

// ---- fix round 2 (REVIEW-2 I1, M2): unknown is not empty ----
cases["S-unknown"] = async () => { // I1: loadSpace names the parts whose read did not answer with content; a missing file is an answer
  const P = `${M}:.config/herdr-pa/profiles/test/profile.json`;
  const runs = [
    ["ledger hung", () => { g.__hang = [keyOf(remote, "/ledger.json")]; }, ["ledger"]],
    ["ledger invalid", () => { g.__files[keyOf(remote, "/ledger.json")] = "{bad"; }, ["ledger"]],
    ["ledger missing", () => { delete g.__files[keyOf(remote, "/ledger.json")]; }, []],
    ["profile hung", () => { g.__hang = [P]; }, ["profile"]],
    ["profile invalid", () => { g.__files[P] = "{bad"; }, ["profile"]],
    ["profile missing", () => { delete g.__files[P]; }, []],
    ["config.json hung", () => { agentFiles(remote, { profile: "" }); g.__hang = [`${M}:.config/herdr-pa/config.json`]; }, ["profile"]],
    ["schedule hung", () => { g.__hang = [keyOf(remote, "/schedule.json")]; }, ["schedule"]],
    ["pass-request hung", () => { g.__hang = [keyOf(remote, "/pass-request.json")]; }, ["request"]],
    ["all answered", () => {}, []],
  ];
  for (const [what, set, want] of runs) {
    world(); agentFiles(remote); set();
    const r = await within(store.loadSpace(remote, undefined, cx), 10000);
    check(r.done && r.value?.agent, `${what}: settles within 10 s`);
    check(JSON.stringify([...(r.value.unknown ?? [])].sort()) === JSON.stringify(want), `${what}: unknown ${JSON.stringify(r.value.unknown)} (want ${JSON.stringify(want)})`);
  }
};

cases["S-cards-unknown"] = async () => { // I1: a listing that never answered is null (unknown), not "no cards"; one that answered stays []
  const m = { "d-1.json": card("d-1") };
  const runs = [
    ["batch and ls hung", () => { cards(m); g.__execHang = [`${M}:batch`, `${M}:ls`]; }, remote, null],
    ["batch failed, ls threw", () => { cards(m); g.__batchFail = { [M]: { code: 1 } }; g.__execFail = [`${M}:ls`]; }, remote, null],
    ["batch and ls threw", () => { cards(m); g.__execFail = [`${M}:batch`, `${M}:ls`]; }, remote, null],
    ["local batch and ls threw", () => { cards(m); g.__execFail = [":batch", ":ls"]; }, local, null], // t-053: the batch runs here too
    ["batch failed, no folder (ls exit 1)", () => { g.__batchFail = { [M]: { code: 1 } }; }, remote, []],
    ["no folder (batch exit 3)", () => {}, remote, []],
  ];
  for (const [what, set, space, want] of runs) {
    world(); set();
    const r = await within(store.loadDecisions(space, { cx }), 20000, 2);
    check(r.done && JSON.stringify(r.value) === JSON.stringify(want), `${what}: ${JSON.stringify(want)} (got ${r.value instanceof Error ? r.value.message : JSON.stringify(r.value)})`);
  }
};

cases["S-card-known"] = async () => { // M2: a fallback card read that hangs takes the card's last known copy; one that throws is left out, never failing the rest
  world(); cards({ "d-1.json": card("d-1"), "d-2.json": card("d-2") }); g.__batchFail = { [M]: { code: 1 } };
  g.__hang = [keyOf(remote, "/decisions/d-2.json")];
  const known = [{ id: "d-2", path: `${rdir}/decisions/d-2.json`, text: card("d-2", { question: "known copy?" }) }];
  let r = await within(store.loadDecisions(remote, { cx, known }), 10000);
  check(r.done && Array.isArray(r.value) && r.value.map((c) => c.id).sort().join() === "d-1,d-2" && r.value.find((c) => c.id === "d-2")?.card?.question === "known copy?", `the known copy (got ${JSON.stringify(r.value?.map?.((c) => [c.id, c.card?.question]))})`);
  world(); cards({ "d-1.json": card("d-1"), "d-2.json": card("d-2") }); g.__batchFail = { [M]: { code: 1 } };
  g.__fail = [keyOf(remote, "/decisions/d-2.json")];
  r = await within(store.loadDecisions(remote, { cx }), 10000);
  check(r.done && Array.isArray(r.value) && r.value.map((c) => c.id).join() === "d-1", `a thrown read is left out (got ${r.value instanceof Error ? r.value.message : JSON.stringify(r.value?.map?.((c) => c.id))})`);
};

cases["S-push-refuse"] = async () => { // AC5.3 (t-007b): answerDecision refuses a push card, judged on the copy read from disk; the file is unchanged
  const push = { version: 1, space: S, task: "t-1", path: "/repo", url: "file:///tmp/o.git", branch: "main", commit: "c".repeat(40) };
  const opts = { options: [{ label: "Push" }, { label: "Not now" }] };
  const variants = { both: card("d-push-t-1-1", { kind: "push", push, ...opts }), object: card("d-push-t-1-2", { push, ...opts }), kind: card("d-push-t-1-3", { kind: "push", ...opts }) };
  for (const space of [local, remote]) {
    for (const [name, text] of Object.entries(variants)) {
      world();
      const id = JSON.parse(text).id, k = keyOf(space, `/decisions/${id}.json`);
      g.__files[k] = text;
      // The entry in memory looks like a plain card: the refusal comes from what is on disk.
      const entry = { id, path: `${space.machine ? rdir : ldir}/decisions/${id}.json`, card: { ...JSON.parse(card(id)), options: opts.options } };
      let err = null;
      try { await store.answerDecision(space, entry, "Push", "a note"); } catch (e) { err = e; }
      check(err && err.message === "A push card is answered with its own Submit; its answer runs pa-push-main.", `${space.machine ?? "local"} ${name}: refused (got ${err?.message ?? "no error"})`);
      check(g.__files[k] === text && !(g.__writes ?? []).length && !g.__execs.some(([c, a]) => c === "python3" && a[0] === "-c"), `${space.machine ?? "local"} ${name}: the file is unchanged, nothing written`);
    }
  }
  check(store.isPush({ push: {} }) && store.isPush({ kind: "push" }) && !store.isPush({ id: "d-1" }) && !store.isPush(null) && !store.isPush({ push: "x" }), "isPush by the push object or kind");
  world(); g.__files[keyOf(local, "/decisions/d-1.json")] = card("d-1");
  await store.answerDecision(local, { id: "d-1", path: `${ldir}/decisions/d-1.json` }, "A", "");
  check(JSON.parse(g.__files[keyOf(local, "/decisions/d-1.json")]).status === "answered", "a plain card is still answered");
};

// ---- t-009 Costs view, step 9b: loadCosts reads only costs/summary.json (and lists costs/ when it is missing) ----
const SUMMARY = JSON.stringify({ v: 1, space: S, updated: "2026-11-17T09:00:00Z", account: "default", totals: { tokens: { all: 10 }, money: 1.5, unpricedTokens: 0 } });
const costsCalls = () => g.__issued.filter((x) => (x.kind === "read" && /\/costs\//.test(x.path ?? "")) || x.kind === "ls");
cases["S-costs-paths"] = async () => { // R6, AC6.2: the summary on the space's machine, at its state folder, nothing else
  for (const space of [remote, local]) {
    world();
    g.__files[keyOf(space, "/costs/summary.json")] = SUMMARY;
    check(store.costsDir(space) === `${space.machine ? rdir : ldir}/costs`, `costsDir (got ${store.costsDir(space)})`);
    const r = await store.loadCosts(space, cx);
    check(r.summary?.totals?.money === 1.5 && !r.error && !r.timedOut && r.missing === null, `${space.machine ?? "local"}: the summary (got ${JSON.stringify(r)})`);
    const calls = g.__issued.map((x) => `${x.on ?? ""}:${x.kind}:${x.path ?? ""}`);
    check(JSON.stringify(calls) === JSON.stringify([`${space.machine ?? ""}:read:${space.machine ? rdir : ldir}/costs/summary.json`]), `one read, of summary.json (got ${JSON.stringify(calls)})`);
  }
};
cases["S-costs-missing"] = async () => { // §5.5 (D4): no summary: one ls of costs/ tells none from pending
  for (const space of [remote, local]) {
    world();
    let r = await store.loadCosts(space, cx);
    check(r.summary === null && r.missing === "none" && !r.error, `${space.machine ?? "local"}: no folder is none (got ${JSON.stringify(r)})`);
    const ls = g.__execs.filter(([c]) => c === "ls");
    check(ls.length === 1 && JSON.stringify(ls[0][1]) === JSON.stringify(["-1", `${space.machine ? rdir : ldir}/costs`]) && ls[0][2] === (space.machine ?? null), `one ls of costs/ on the machine (got ${JSON.stringify(ls)})`);
    world();
    g.__ls[keyOf(space, "/costs")] = ["sessions.jsonl"];
    r = await store.loadCosts(space, cx);
    check(r.summary === null && r.missing === "pending", `records but no summary: pending (got ${JSON.stringify(r)})`);
    world();
    g.__ls[keyOf(space, "/costs")] = [".collect.lock"];
    r = await store.loadCosts(space, cx);
    check(r.missing === "none", `a folder without records: none (got ${JSON.stringify(r)})`);
  }
};
cases["S-costs-invalid"] = async () => { // §5.5: not JSON is an error, never "none"
  world();
  g.__files[keyOf(remote, "/costs/summary.json")] = "{ not json";
  const r = await store.loadCosts(remote, cx);
  check(r.summary === null && typeof r.error === "string" && r.error.length > 0 && r.missing === null && !r.timedOut, `an error (got ${JSON.stringify(r)})`);
  check(!g.__execs.some(([c]) => c === "ls"), "no ls for an invalid file");
  world();
  g.__files[keyOf(remote, "/costs/summary.json")] = "[1,2]";
  const r2 = await store.loadCosts(remote, cx);
  check(r2.summary === null && /not a summary/.test(r2.error ?? ""), `not an object: an error (got ${JSON.stringify(r2)})`);
};
cases["S-costs-live"] = async () => { // MF-2 (b): the view left while the summary read was in flight: no ls of costs/
  for (const space of [remote, local]) {
    world();
    const r = await store.loadCosts(space, cx, () => false);
    check(r.summary === null && r.left === true && r.missing === null && !r.error && !r.timedOut, `${space.machine ?? "local"}: left, nothing known (got ${JSON.stringify(r)})`);
    check(!g.__execs.some(([c]) => c === "ls"), `${space.machine ?? "local"}: no ls when the check fails (got ${JSON.stringify(g.__execs)})`);
    world();
    let asked = 0;
    const r2 = await store.loadCosts(space, cx, () => { asked++; return true; });
    check(r2.missing === "none" && !r2.left && asked === 1 && g.__execs.filter(([c]) => c === "ls").length === 1, `${space.machine ?? "local"}: shown: asked once, then the ls (got ${JSON.stringify(r2)}, asked ${asked})`);
    world();
    g.__files[keyOf(space, "/costs/summary.json")] = SUMMARY;
    const r3 = await store.loadCosts(space, cx, () => false);
    check(r3.summary?.totals?.money === 1.5 && !r3.left, `${space.machine ?? "local"}: a summary read is returned as read (got ${JSON.stringify(r3)})`);
  }
};
cases["S-costs-timeout"] = async () => { // D3: a hung read is given up after READ_LIMIT, timedOut, and no ls
  world();
  g.__hang = [keyOf(remote, "/costs/summary.json")];
  let done = false, value;
  store.loadCosts(remote, cx).then((v) => { done = true; value = v; }, (e) => { done = true; value = e; });
  await moveOn(store.READ_LIMIT - 1000);
  check(!done, "not before READ_LIMIT");
  await moveOn(1000);
  check(done && value.timedOut === true && value.summary === null && value.missing === null, `timed out (got ${JSON.stringify(value)})`);
  check(!g.__execs.some(([c]) => c === "ls") && costsCalls().length === 1, `only the one read (got ${JSON.stringify(costsCalls())})`);
  // A hung ls (summary missing): timedOut too, never "none".
  world();
  g.__execHang = [`${M}:ls`];
  const r = await within(store.loadCosts(remote, cx), store.READ_LIMIT, 2);
  check(r.done && r.value.timedOut === true && r.value.missing === null, `a hung ls times out (got ${JSON.stringify(r.value)})`);
};

// t-028 B1: a push card's id is its file name; one whose own id differs, or whose name starts with "-", is flagged.
cases["S-push-id-file-name"] = async () => {
  const push = (id) => JSON.stringify({ id, kind: "push", question: "Push?", options: [{ label: "Push" }, { label: "Not now" }], status: "open", push: { version: 1 } });
  const cards = store.parseCards([
    { name: "d-push-t-1-1.json", path: "/d/d-push-t-1-1.json", text: push("d-push-t-9-9") },
    { name: "d-push-t-2-1.json", path: "/d/d-push-t-2-1.json", text: push("d-push-t-2-1") },
    { name: "-x.json", path: "/d/-x.json", text: push("-x") },
    { name: "d-a.json", path: "/d/d-a.json", text: JSON.stringify({ id: "d-other", question: "Q?", options: [{ label: "A" }], status: "open" }) }]);
  const by = Object.fromEntries(cards.map((c) => [c.path, c]));
  check(by["/d/d-push-t-1-1.json"].id === "d-push-t-1-1" && by["/d/d-push-t-1-1.json"].mismatch === "d-push-t-9-9", `file name, own id kept as mismatch (got ${JSON.stringify(by["/d/d-push-t-1-1.json"])})`);
  check(by["/d/d-push-t-2-1.json"].id === "d-push-t-2-1" && !by["/d/d-push-t-2-1.json"].mismatch, "a matching one is not flagged");
  check(by["/d/-x.json"].id === "-x" && by["/d/-x.json"].dash === true, `a leading dash is flagged (got ${JSON.stringify(by["/d/-x.json"])})`);
  check(by["/d/d-a.json"].id === "d-other" && !by["/d/d-a.json"].mismatch && !by["/d/d-a.json"].dash, "an ordinary card keeps its own id, as before");
};

// ---- t-045 W4: an answer already written is never changed; two remote writers are serialised ----
cases["S45-answered-refused"] = async () => {
  for (const space of [local, remote]) {
    world();
    const k = keyOf(space, "/decisions/d-1.json"), path = `${space.machine ? rdir : ldir}/decisions/d-1.json`;
    const done = card("d-1", { status: "answered", answer: "A", options: [{ label: "A" }, { label: "B" }] });
    g.__files[k] = done;
    let err = null;
    try { await store.answerDecision(space, { id: "d-1", path }, "B", "changed my mind"); } catch (e) { err = e; }
    check(err?.message === "This card was already answered.", `${space.machine ?? "local"}: refused (got ${err?.message ?? "no error"})`);
    check(g.__files[k] === done && !(g.__writes ?? []).length, `${space.machine ?? "local"}: the file is unchanged, nothing written`);
    const open = card("d-1", { options: [{ label: "A" }, { label: "B" }] });
    g.__files[k] = open;
    const r = await store.answerDecision(space, { id: "d-1", path }, "B", "");
    check(r && r.before === open && r.after === g.__files[k] && r.card?.status === "answered" && r.card.answer === "B",
      `${space.machine ?? "local"}: returns { card, before, after } (got ${JSON.stringify(r)?.slice(0, 300)})`);
  }
};

cases["S45-remote-write-lock"] = async () => { // the real REMOTE_WRITE under python3: compare-and-write, and a writer that holds the lock wins
  check(typeof store.REMOTE_WRITE === "string", "REMOTE_WRITE is exported");
  const { spawn, spawnSync } = await import("child_process");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "t045-write-")), file = path.join(dir, "d-1.json");
  try {
    const put = (s) => fs.writeFileSync(file, s), run = (expected, text) => spawnSync("python3", ["-c", store.REMOTE_WRITE, file, expected, text], { encoding: "utf8" });
    put("OPEN");
    let r = run("OPEN", "ANSWERED");
    check(r.status === 0 && fs.readFileSync(file, "utf8") === "ANSWERED", `a match is replaced (got ${r.status} ${r.stderr})`);
    r = run("OPEN", "OTHER");
    check(r.status === 3 && fs.readFileSync(file, "utf8") === "ANSWERED", `a mismatch exits 3, unchanged (got ${r.status})`);
    put("OPEN");
    const ready = path.join(dir, "ready");
    const holder = spawn("python3", ["-c", `
import fcntl, os, sys, time
f = open(sys.argv[1], "rb"); fcntl.flock(f, fcntl.LOCK_EX)
open(sys.argv[2], "w").close(); time.sleep(0.6)
tmp = sys.argv[1] + ".tmp"; open(tmp, "w").write("HOLDER"); os.replace(tmp, sys.argv[1])
`, file, ready]);
    const ended = new Promise((res) => holder.on("exit", res));
    for (let i = 0; i < 400 && !fs.existsSync(ready); i++) await new Promise((res) => setTimeout(res, 10));
    check(fs.existsSync(ready), "the holder has the lock");
    r = run("OPEN", "SECOND");
    await ended;
    check(r.status === 3 && fs.readFileSync(file, "utf8") === "HOLDER", `the waiting write sees the holder's answer and exits 3 (got ${r.status}, file ${fs.readFileSync(file, "utf8")})`);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
};

cases["S45-fallback-marked"] = async () => { // t-045 round 2: a card whose own read failed and came from its last known copy says so (fallback); one left out is named (unread)
  world(); cards({ "d-1.json": card("d-1"), "d-2.json": card("d-2"), "d-3.json": card("d-3") }); g.__batchFail = { [M]: { code: 1 } };
  g.__hang = [keyOf(remote, "/decisions/d-2.json")]; g.__fail = [keyOf(remote, "/decisions/d-3.json")];
  const known = [{ id: "d-2", path: `${rdir}/decisions/d-2.json`, text: card("d-2", { question: "known copy?" }) }];
  const r = await within(store.loadDecisions(remote, { cx, known }), 10000);
  const by = (id) => r.value?.find?.((c) => c.id === id);
  check(r.done && by("d-2")?.fallback === true && !by("d-1")?.fallback && !by("d-3"), `d-2 is its fallback copy, d-1 a read, d-3 left out (got ${JSON.stringify(r.value?.map?.((c) => [c.id, c.fallback ?? false]))})`);
  check(r.value.unread instanceof Set && r.value.unread.has(`${rdir}/decisions/d-3.json`) && r.value.unread.has(`${rdir}/decisions/d-2.json`) && !r.value.unread.has(`${rdir}/decisions/d-1.json`),
    `unread names the cards not read now (got ${JSON.stringify([...(r.value.unread ?? [])])})`);
  world(); cards({ "d-1.json": card("d-1") });
  const b = await store.loadDecisions(remote, { cx });
  check(b.length === 1 && !b[0].fallback && !(b.unread?.size), "a batch read: no fallback, nothing unread");
};

// ---- t-053 -----------------------------------------------------------------------------------------------------
cases["S53-batch-local"] = async () => { // R3: on this computer the cards are one batch exec too: the same cards and order as on another machine
  world();
  const m = {}; for (let i = 1; i <= 30; i++) m[`d-${String(i).padStart(3, "0")}.json`] = card(`d-${String(i).padStart(3, "0")}`, { status: i % 3 ? "answered" : "open" });
  m["bad name!.json"] = card("x"); m["d-broken.json"] = "{"; cards(m);
  const here = await store.loadDecisions(local, { cx }), there = await store.loadDecisions(remote, { cx });
  const localCalls = g.__issued.filter((x) => x.on === null);
  check(localCalls.length === 1 && localCalls[0].kind === "batch", `one local call, the batch (got ${JSON.stringify(localCalls.map((x) => x.kind))})`);
  check(norm(here) === norm(there), "the same list as the remote batch");
};

// ---- t-061 -----------------------------------------------------------------------------------------------------
// 200 answered cards named a-* (a-000 the oldest) and `open` open cards named z-* (sorting after every a-*), plus
// `extra` answered ones named b-*: the cap of 200 cards keeps every open card and drops the oldest answered.
const minute = (i) => `2026-11-${String(1 + Math.floor(i / 600)).padStart(2, "0")}T${String(Math.floor(i / 60) % 10).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}:00Z`;
function many61({ answered = 200, open = 30 } = {}) {
  const m = {};
  for (let i = 0; i < answered; i++) { const id = `a-${String(i).padStart(3, "0")}`; m[`${id}.json`] = card(id, { status: i % 10 === 9 ? "withdrawn" : "answered", answer: "A", created: minute(i) }); }
  for (let i = 0; i < open; i++) { const id = `z-${String(i).padStart(3, "0")}`; m[`${id}.json`] = card(id, { created: minute(i) }); }
  cards(m);
}
const check61 = (list, { total, open, omitted, gone }) => {
  const ids = list.map((c) => c.id), opens = list.filter((c) => c.card?.status === "open");
  check(opens.length === open, `every open card shown: ${open} (got ${opens.length}: ${opens.slice(-3).map((c) => c.id)})`);
  check(list.length === total, `${total} cards in all (got ${list.length})`);
  check(list.omitted === omitted, `omitted ${omitted} (got ${list.omitted})`);
  for (const id of gone) check(!ids.includes(id), `the oldest answered ${id} is the one left out`);
  if (omitted) check(ids.includes(`a-${String(omitted).padStart(3, "0")}`) && ids.includes("a-199"), "the newer answered cards are kept");
};

cases["S61-batch-open-last"] = async () => { // R1, R2: 230 cards in one batch, the 30 open ones sorting last: all 30 shown, 30 oldest answered left out
  world(); many61();
  check61(await store.loadDecisions(remote, { cx }), { total: 200, open: 30, omitted: 30, gone: ["a-000", "a-029"] });
  check(remoteCalls("batch").length === 1 && cardReads().length === 0, "one batch");
  world(); many61();
  check61(await store.loadDecisions(local, { cx }), { total: 200, open: 30, omitted: 30, gone: ["a-000", "a-029"] });
};

cases["S61-fallback-open-last"] = async () => { // R1, R2, R3: the same on the per-card fallback (the batch fails): every name is read
  world(); many61(); g.__batchFail = { [M]: { code: 1 } };
  const r = await within(store.loadDecisions(remote, { cx }), 10000);
  check(r.done && Array.isArray(r.value), "the fallback answered");
  check61(r.value, { total: 200, open: 30, omitted: 30, gone: ["a-000", "a-029"] });
  check(cardReads().length === 230, `every card read (got ${cardReads().length})`);
};

cases["S61-more-open-than-cap"] = async () => { // R1: 250 open cards and 10 answered: all 250 shown, the 10 answered left out and counted
  world(); many61({ answered: 10, open: 250 });
  const b = await store.loadDecisions(remote, { cx });
  check(b.length === 250 && b.every((c) => c.card?.status === "open") && b.omitted === 10, `250 open, omitted 10 (got ${b.length}, ${b.omitted})`);
  world(); many61({ answered: 10, open: 250 }); g.__batchFail = { [M]: { code: 1 } };
  const r = await within(store.loadDecisions(remote, { cx }), 10000);
  check(r.done && r.value.length === 250 && r.value.omitted === 10, `the fallback too (got ${r.value?.length}, ${r.value?.omitted})`);
};

cases["S61-fallback-known-closed-last"] = async () => { // R1: on the fallback, names whose last known copy is answered are read after the others
  world(); many61({ answered: 20, open: 5 }); g.__batchFail = { [M]: { code: 1 } };
  const known = Array.from({ length: 20 }, (_, i) => { const id = `a-${String(i).padStart(3, "0")}`; return { id, path: `${rdir}/decisions/${id}.json`, text: card(id, { status: "answered", answer: "A" }) }; });
  const r = await within(store.loadDecisions(remote, { cx, known }), 10000);
  check(r.done && r.value.length === 25, `all 25 (got ${r.value?.length})`);
  const order = cardReads().map((x) => x.path.split("/").pop());
  check(order.slice(0, 5).every((n) => n.startsWith("z-")), `the open (not known answered) ones read first (got ${order.slice(0, 7)})`);
};

// ---- t-054 -----------------------------------------------------------------------------------------------------
cases["S54-archive-folder"] = async () => { // R5: the archive is read with the same batch from decisions-archive/, only when asked
  world(); cards({ "d-1.json": card("d-1") });
  for (const space of [remote, local]) {
    g.__ls[keyOf(space, "/decisions-archive")] = ["d-0.json"];
    g.__files[keyOf(space, "/decisions-archive/d-0.json")] = card("d-0", { status: "answered", answer: "A" });
  }
  const list = await store.loadDecisions(remote, { cx });
  check(list.length === 1 && list[0].id === "d-1", `a load reads decisions/ only (got ${list.map((c) => c.id)})`);
  check(![...g.__issued, ...g.__execs].some((x) => JSON.stringify(x).includes("decisions-archive")), "no call names the archive");
  const arch = await store.loadDecisions(remote, { cx, batchOnly: true, folder: "decisions-archive" });
  check(arch?.length === 1 && arch[0].id === "d-0" && arch[0].path === `${rdir}/decisions-archive/d-0.json`, `the archive's cards (got ${JSON.stringify(arch?.map((c) => c.path))})`);
  check(remoteCalls("batch").length === 2, "one batch each");
};

const hostMod = fs.existsSync(path.join(dst, "host.js")) ? await import(path.join(dst, "host.js")) : null;
cases["S53-host-busy"] = async () => { // R2: a key still pending answers BUSY and starts nothing, given up or not; settling frees it
  world();
  check(hostMod, "host.js exists");
  const h = new hostMod.Host(); let started = 0, release;
  const start = () => { started += 1; return new Promise((r) => { release = r; }); };
  const first = h.call(cx, "k", start, { limit: 10000 });
  const r1 = await within(first, 11000, 11);
  check(r1.done && r1.value === hostMod.TIMED_OUT, `given up at its limit (got ${JSON.stringify(r1.value)})`);
  const second = await h.call(cx, "k", start, { limit: 10000 });
  check(second === hostMod.BUSY && hostMod.gaveUp(second) && started === 1, `BUSY, nothing started (started ${started})`);
  release("late"); await settle();
  const third = h.call(cx, "k", () => Promise.resolve("now"), { limit: 10000 });
  check((await third) === "now", "the key is free once the late call settled");
  const waited = h.call(cx, "w", () => new Promise((r) => { release = r; }), { limit: 10000 });
  const click = h.call(cx, "w", () => Promise.resolve("fresh"), { limit: 10000, wait: true });
  release("old"); await settle();
  check((await waited) === "old" && (await click) === "fresh", "a click waits for the call in flight, then reads again");
};

cases["S53-host-refused"] = async () => { // R6: the runtime's RangeError (thrown or a rejection) answers REFUSED, recorded once; quiet for a second
  world();
  check(hostMod, "host.js exists");
  const h = new hostMod.Host();
  const r1 = await h.call(cx, "a", () => { throw new RangeError("a HostModule's async function exceeded the per-runtime outstanding host task limit of 1024"); }, { what: "the ledger.json read" });
  check(r1 === hostMod.REFUSED && h.refusalSince(T0)?.what === "the ledger.json read", `refused, recorded (got ${JSON.stringify(r1)}, ${JSON.stringify(h.refusal)})`);
  let started = 0;
  const r2 = await h.call(cx, "b", () => { started += 1; return Promise.resolve(1); });
  check(r2 === hostMod.SKIPPED && started === 0, "a background call within the quiet second starts nothing");
  g.__now += 1001;
  const r3 = await h.call(cx, "c", () => Promise.reject(new RangeError("cx.sleep(ms) exceeded the per-runtime outstanding host task limit of 1024")));
  check(r3 === hostMod.REFUSED, "a rejection with the limit's text is REFUSED too");
  g.__now += 1001;
  check((await h.call(cx, "d", () => Promise.resolve(2), { background: false })) === 2, "a click runs");
  check(/^Too many calls waiting \(\d+\): the ledger\.json read was skipped; showing what was read at \d\d:\d\d$/.test(hostMod.refusalLine({ n: 3, what: "the ledger.json read" }, T0)), "the plain line");
};

// ---- t-046 J3: an answer in the person's own words (Other) ----
const s46 = (space, extra = {}) => {
  world();
  const k = keyOf(space, "/decisions/d-1.json"), path = `${space.machine ? rdir : ldir}/decisions/d-1.json`;
  g.__files[k] = card("d-1", { options: [{ label: "A" }, { label: "B" }], ...extra });
  return { k, path, entry: { id: "d-1", path } };
};
const refusedWith = async (fn) => { try { await fn(); return null; } catch (e) { return String(e?.message ?? e); } };

cases["S46-other-answer"] = async () => { // R2 AC1: answer Other, other: true, the words trimmed in note
  for (const space of [local, remote]) {
    const { k, entry } = s46(space);
    const before = g.__files[k];
    const r = await store.answerDecision(space, entry, "Other", "  do X first, then ask me \n");
    const c = JSON.parse(g.__files[k]);
    check(c.status === "answered" && c.answer === "Other" && c.other === true && c.note === "do X first, then ask me" && /Z$/.test(c.answered),
      `${space.machine ?? "local"}: written (got ${g.__files[k]})`);
    check(r.before === before && r.after === g.__files[k] && r.card.other === true, `${space.machine ?? "local"}: returns before/after`);
  }
  check(store.OTHER === "Other" && store.OTHER_MAX === 2000, "OTHER and OTHER_MAX exported");
};

cases["S46-other-empty"] = async () => { // R2 AC2: no words: refused, nothing written
  for (const words of ["", "   \n ", null]) {
    const { k, entry } = s46(local);
    const before = g.__files[k];
    const why = await refusedWith(() => store.answerDecision(local, entry, "Other", words));
    check(why === "An Other answer needs your own words.", `refused (got ${why})`);
    check(g.__files[k] === before && !(g.__writes ?? []).length, "the file is unchanged, nothing written");
  }
};

cases["S46-other-long"] = async () => { // R2 AC2: over 2000 characters refused; 2000 accepted
  const { k, entry } = s46(local);
  const before = g.__files[k];
  const why = await refusedWith(() => store.answerDecision(local, entry, "Other", "x".repeat(2001)));
  check(why === "Your answer is longer than 2000 characters; shorten it, or write it in a file and give the path.", `refused (got ${why})`);
  check(g.__files[k] === before, "unchanged");
  await store.answerDecision(local, entry, "Other", "y".repeat(2000));
  check(JSON.parse(g.__files[k]).note.length === 2000, "2000 characters accepted");
};

cases["S46-listed-no-other-key"] = async () => { // R2 AC3: a listed answer has no other field at all
  const { k, entry } = s46(local);
  await store.answerDecision(local, entry, "A", "a note");
  const c = JSON.parse(g.__files[k]);
  check(c.answer === "A" && !("other" in c) && c.note === "a note", `no other key (got ${g.__files[k]})`);
};

cases["S46-other-not-open"] = async () => { // R2 AC1: the existing refusals come first
  let { k, entry } = s46(local, { status: "answered", answer: "A" });
  let before = g.__files[k];
  check(await refusedWith(() => store.answerDecision(local, entry, "Other", "words")) === "This card was already answered.", "answered: refused");
  check(g.__files[k] === before, "unchanged");
  ({ k, entry } = s46(local, { kind: "push", push: { task: "t-1" } }));
  before = g.__files[k];
  check(/A push card is answered with its own Submit/.test(await refusedWith(() => store.answerDecision(local, entry, "Other", "words"))), "push card: refused");
  check(g.__files[k] === before, "unchanged");
};

cases["S46-isOther"] = async () => { // R2 AC6 as J1 decided (pa.is_other): the marker, or the label in any case
  const yes = [{ other: true }, { answer: "Other" }, { answer: " other " }, { answer: "OTHER", options: [{ label: "Other" }] }, { other: true, answer: "A" }];
  const no = [{ answer: "Others" }, { answer: "A" }, { answer: null }, { other: false, answer: "A" }, {}, null, "Other"];
  for (const c of yes) check(store.isOther(c) === true, `isOther(${JSON.stringify(c)}) is true`);
  for (const c of no) check(store.isOther(c) === false, `isOther(${JSON.stringify(c)}) is false`);
};

cases["S46-oneLine"] = async () => { // R4 AC3: one line, cut at 400 with the tail
  check(store.oneLine("a\nb\t c  \r\n d ") === "a b c d", `flattened (got ${store.oneLine("a\nb\t c  \r\n d ")})`);
  const s400 = "x".repeat(400);
  check(store.oneLine(s400) === s400, "400 kept whole");
  const cut = store.oneLine("z".repeat(600));
  check(cut === `${"z".repeat(399)}… (the rest is on the card)`, `600 cut to 399 + the tail (got ${cut.length})`);
  check(store.oneLine('say "hi"') === 'say "hi"', "quotes kept");
  check(store.oneLine("abcdef", 4) === "abc… (the rest is on the card)", "max is a parameter");
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
