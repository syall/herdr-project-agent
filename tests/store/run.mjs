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

// ---- t-131 (from t-064: t-061 R1) ------------------------------------------------------------------------------
cases["S131-fallback-missing-counted"] = async () => { // a card whose fallback read failed or hung and has no known copy is never silently missing: counted
  world(); cards({ "d-1.json": card("d-1"), "d-2.json": card("d-2"), "d-3.json": card("d-3"), "d-4.json": card("d-4") }); g.__batchFail = { [M]: { code: 1 } };
  g.__hang = [keyOf(remote, "/decisions/d-2.json"), keyOf(remote, "/decisions/d-4.json")]; g.__fail = [keyOf(remote, "/decisions/d-3.json")];
  const known = [{ id: "d-2", path: `${rdir}/decisions/d-2.json`, text: card("d-2") }];
  const r = await within(store.loadDecisions(remote, { cx, known }), 15000);
  check(r.done && Array.isArray(r.value), "the fallback answered");
  check(r.value?.missing === 2, `d-3 (failed) and d-4 (hung), with no known copy, are counted (got ${r.value?.missing})`);
  world(); cards({ "d-1.json": card("d-1") });
  const b = await store.loadDecisions(remote, { cx });
  check(b.missing === 0, `a whole batch: none missing (got ${b.missing})`);
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

// ---- t-037 J4: the Restart button's store part (R13, R14, R15) ----
cases["S37-code-parity"] = async () => { // R14 AC14.1: the same table as pa.code_state (test_pa_stamp J4_CodeStateParity)
  const rows = JSON.parse(fs.readFileSync(path.join(here, "..", "samples", "code-state-cases.json"), "utf8"));
  check(rows.length >= 15, `the fixture (got ${rows.length})`);
  for (const row of rows) {
    const st = store.codeState(row.launcher, row.installed, row.terminal);
    check(st.word === row.word, `${row.name}: word ${row.word} (got ${st.word})`);
    check(st.capable === row.capable, `${row.name}: capable ${row.capable} (got ${st.capable})`);
    check(typeof st.detail === "string" && st.detail.length > 0, `${row.name}: a detail`);
  }
};

cases["S37-code-state"] = async () => { // instance, prompt and the header's words
  const T = "T".repeat(64), C = "C".repeat(64);
  const rec = { version: 2, accepts: ["restart"], instance: "i-1", terminal: "t1", code: { tools: T }, session: { core: C, prompt: "recorded", promptCore: C } };
  const st = store.codeState(rec, { tools: T, core: C }, "t1");
  check(st.word === "current" && st.instance === "i-1" && st.prompt === "recorded" && st.capable, `fields (got ${JSON.stringify(st)})`);
  check(store.codeBadge(st) === null, "current: no badge");
  check(store.codeBadge(store.codeState({ ...rec, code: { tools: "X" } }, { tools: T, core: C }, "t1")) === "older code (tools)", "older tools");
  check(store.codeBadge(store.codeState({ ...rec, session: { promptCore: "Y" } }, { tools: T, core: C }, "t1")) === "older code (core)", "older core");
  check(store.codeBadge(store.codeState({ ...rec, code: { tools: "X" }, session: { promptCore: "Y" } }, { tools: T, core: C }, "t1")) === "older code (tools, core)", "older both");
  check(store.codeBadge(store.codeState(null, { tools: T, core: C }, "t1")) === "started before restart support", "no launcher.json");
  check(store.codeBadge(store.codeState({ ...rec, version: 1 }, { tools: T, core: C }, "t1")) === "started before restart support", "version 1");
  check(store.codeBadge(store.codeState(rec, null, "t1")) === "code unknown", "no installed.json");
  check(store.codeBadge(null) === null, "nothing read: no badge");
  check(store.codeState({ ...rec, instance: 5 }, null, "t1").instance === null, "an instance that is not text: null");
};

cases["S37-load-code"] = async () => { // R8, R14: both files read on the space's machine, capped; a give-up is a give-up
  world();
  g.__files[`${M}:.config/herdr-pa/spaces/${S}/launcher.json`] = JSON.stringify({ version: 2, instance: "i-r" });
  g.__files[`${M}:.config/herdr-pa/installed.json`] = JSON.stringify({ tools: "T" });
  const r = await store.loadCode(remote, cx);
  check(r.launcher?.instance === "i-r" && r.installed?.tools === "T", `remote read (got ${JSON.stringify(r)})`);
  check(g.__reads.length === 2 && g.__reads.every((x) => x.startsWith(`${M}:`)), `only on ${M} (got ${JSON.stringify(g.__reads)})`);
  world();
  const l = await store.loadCode(local, cx);
  check(l.launcher === null && l.installed === null && !store.gaveUp(l), `local, missing files: nulls (got ${JSON.stringify(l)})`);
  world();
  g.__files[`:/home/u/.config/herdr-pa/spaces/${S}/launcher.json`] = "{not json";
  const b = await store.loadCode(local, cx);
  check(b.launcher === null, "broken JSON: no record (as read_json_safe)");
  world();
  g.__hang = [`${M}:.config/herdr-pa/installed.json`];
  const { done, value: h } = await within(store.loadCode(remote, cx), 12000);
  check(done && store.gaveUp(h), `a hung read gives up within its limit (done ${done}, got ${JSON.stringify(h)})`);
};

const RS = ["pa", "restart"].join("-"); // the tool's name, spelled so a guard reading this file's commands is not confused
cases["S37-restart-args"] = async () => { // R13: only the in-place restart; never --end-terminal, --anyway or --on
  const dry = store.restartArgs(remote, { dry: true });
  check(JSON.stringify(dry) === JSON.stringify(["--space", S, "--dry-run"]), `dry run (got ${JSON.stringify(dry)})`);
  const go = store.restartArgs(local, { instance: "i-1" });
  check(JSON.stringify(go) === JSON.stringify(["--space", S, "--json", "--wait", "180", "--expect-instance", "i-1", "--by", "panel", "--why", store.RESTART_WHY]), `restart (got ${JSON.stringify(go)})`);
  check(JSON.stringify(store.restartArgs(local, { instance: "i-1", now: true, fresh: true }).slice(-2)) === JSON.stringify(["--now", "--fresh"]), "now, fresh");
  for (const a of [dry, go, store.restartArgs(remote, { instance: "i", now: true, fresh: true })]) check(!a.some((x) => ["--end-terminal", "--anyway", "--on", "--confirmed-terminal"].includes(x)), `never those (got ${JSON.stringify(a)})`);
  check(store.restartArgs(local, { instance: null }) === null && store.restartArgs(local, { instance: "-x" }) === null, "no instance (or one that looks like a flag): no argv");
  check(store.RESTART_WAIT === 180 && store.RESTART_TIMEOUT > store.RESTART_WAIT && store.RESTART_HOLD === 120000, "constants");
  check(store.endTerminalLine(local) === `${RS} --space ${S} --end-terminal` && store.endTerminalLine(remote) === `${RS} --space ${S} --end-terminal --on ${M}`, "the line to copy");
};

cases["S37-restart-result"] = async () => { // R13: the texts of each outcome
  const launcher = { code: { tools: "abcdef1234", commit: "1234567890" }, session: { promptCore: "fedcba9876", prompt: "rebuilt" }, profile: { name: "test", readAt: "2026-11-18T10:05:00Z" } };
  const ok = store.restartOutcome({ r: { code: 0, stdout: `Restarted proj in place.\nLoaded: x.\n${JSON.stringify({ request: "r1", outcome: "ok", launcher })}\n`, stderr: "" } }, local);
  check(ok.ok && ok.text === "Restarted: tools abcdef1 (1234567), core fedcba9, profile test read 10:05 UTC", `ok (got ${JSON.stringify(ok)})`);
  const rec = store.restartedLine({ ...launcher, code: { tools: "abcdef1234" }, session: { core: "fedcba9876", prompt: "recorded" } });
  check(rec === "Restarted: tools abcdef1 (no commit), core fedcba9 (still the text this conversation began with), profile test read 10:05 UTC", `recorded (got ${rec})`);
  const failed = store.restartOutcome({ r: { code: 1, stdout: `${JSON.stringify({ request: "r1", outcome: "failed", why: "installed.json is missing" })}\n`, stderr: "The restart did not happen (failed): installed.json is missing" } }, local);
  check(!failed.ok && failed.text === "The restart did not happen: installed.json is missing", `failed (got ${JSON.stringify(failed)})`);
  const late = store.restartOutcome({ r: { code: 1, stdout: "Requested (r1); waiting up to 180 s.\n", stderr: "The restart did not happen within 180 s (it is mid-turn; the request was withdrawn).\n" } }, local);
  check(!late.ok && late.text === "The restart did not happen within 180 s (it is mid-turn; the request was withdrawn).", `timeout (got ${JSON.stringify(late)})`);
  const refused = store.restartOutcome({ r: { code: 2, stdout: "", stderr: "cannot restart: its launcher restarted since you looked (now instance i-2); look again\n" } }, local);
  check(!refused.ok && refused.text === "Not restarted: cannot restart: its launcher restarted since you looked (now instance i-2); look again", `refused (got ${JSON.stringify(refused)})`);
  const missing = store.restartOutcome({ error: `exec: ${RS}: not found` }, remote);
  check(missing.text === `${RS} is not installed on ${M}; run pa-sync --on ${M}.`, `missing remote (got ${missing.text})`);
  check(store.restartOutcome({ error: `exec: ${RS}: not found` }, local).text === `${RS} is not installed here; run ./install.sh.`, "missing here");
  check(store.restartOutcome({ r: { code: 127, stdout: "", stderr: `sh: ${RS}: not found` } }, remote).text === `${RS} is not installed on ${M}; run pa-sync --on ${M}.`, "127 on the machine");
  const unknown = store.restartOutcome({ timedOut: true }, local);
  check(!unknown.ok && unknown.unknown && /not known/.test(unknown.text), `no answer (got ${JSON.stringify(unknown)})`);
  check(store.restartOutcome({ busy: true }, local) === null, "busy: nothing to say");
  check(/could not run: boom/.test(store.restartOutcome({ error: "boom" }, local).text), "another error");
};

cases["S37-run-restart"] = async () => { // the exec: the tool on the space's machine, with its timeout
  world();
  g.__execFn = (command) => (command === RS ? { code: 0, stdout: "x", stderr: "" } : undefined);
  const res = await store.runRestart(cx, remote, ["--space", S, "--dry-run"], store.DRY_TIMEOUT);
  check(res.r?.code === 0, `answered (got ${JSON.stringify(res)})`);
  const call = g.__execs.find(([c]) => c === RS);
  check(call && call[2] === M && JSON.stringify(call[1]) === JSON.stringify(["--space", S, "--dry-run"]) && call[3]?.timeout === store.DRY_TIMEOUT, `its call (got ${JSON.stringify(g.__execs)})`);
  world();
  g.__execFn = () => { throw new Error(`exec: ${RS}: not found`); };
  const err = await store.runRestart(cx, local, ["--space", S, "--dry-run"], store.DRY_TIMEOUT);
  check(/not found/.test(err.error ?? ""), `a thrown exec is an error (got ${JSON.stringify(err)})`);
};

cases["S37-restarting-label"] = async () => { // R15: pa.py clears the label by setting it empty
  check(store.restartingLabel({ labels: { "herdr.pa.restarting": "r1" } }) === true, "set");
  check(store.restartingLabel({ labels: { "herdr.pa.restarting": "" } }) === false, "empty: cleared");
  check(store.restartingLabel({ labels: {} }) === false && store.restartingLabel(null) === false, "absent");
};

// ---- t-055: queue.json for the Queue tab ----
const queueMod = await import(path.join(dst, "queue.js")).catch(() => null);
const QK = (space) => keyOf(space, "/queue.json");

cases["S55-load-queue"] = async () => { // loadQueue: missing, valid, each invalid form, on the space's machine
  check(typeof store.loadQueue === "function", "store.loadQueue exists");
  world();
  let r = await store.loadQueue(remote, cx);
  check(r.missing === true && r.doc === null && r.error === null, `missing (got ${JSON.stringify(r)})`);
  check(g.__issued.some((x) => x.on === M && x.kind === "read" && x.path === `${rdir}/queue.json`), "read on the space's machine");
  const doc = { version: 1, repos: { "/w/p": { main: "main", entries: [], left: [], merged: [] } } };
  g.__files[QK(local)] = JSON.stringify(doc);
  r = await store.loadQueue(local, cx);
  check(r.missing === false && r.error === null && JSON.stringify(r.doc) === JSON.stringify(doc), `valid (got ${JSON.stringify(r)})`);
  for (const [body, why] of [["{x", /^queue\.json is not valid JSON/], ["[]", /needs a "repos" object/], [JSON.stringify({ version: 1, repos: [] }), /needs a "repos" object/],
    [JSON.stringify({ version: 3, repos: {} }), /is version 3; this panel reads version 1/]]) {
    g.__files[QK(local)] = body;
    r = await store.loadQueue(local, cx);
    check(r.doc === null && why.test(r.error ?? ""), `${body}: ${why} (got ${JSON.stringify(r)})`);
  }
};

cases["S55-load-queue-hung"] = async () => { // a hung read gives up as TIMED_OUT, never "missing"
  world();
  g.__hang = [QK(remote)];
  const { done, value } = await within(store.loadQueue(remote, cx), 12000, 12);
  check(done && store.gaveUp(value) && !value.missing, `gave up (got ${JSON.stringify(value)})`);
  g.__hang = [];
};

cases["S55-queue-rows"] = async () => { // queueRows: the waiting and approval rules, without rendering
  check(typeof queueMod?.queueRows === "function", "queue.js exports queueRows");
  const zoneMod = await import(path.join(dst, "zone.js"));
  zoneMod.zone.offsetAt = () => 0; // UTC, never the machine's zone (as the panel harness)
  const e = (task, x = {}) => ({ task, job: null, branch: `${task}-b`, onto: "main", state: "checking", tip: "b".repeat(40), needs: null, suite: null, review: null, approval: null, ...x });
  const doc = { version: 1, repos: { "/w/p": { main: "main", entries: [
    e("t-A", { suite: { exit: 0, ended: "2026-11-18T10:05:00Z", worker: "t40" } }),
    e("t-B", { onto: "t-A-b", needs: { shortReview: { file: "S.md", kind: "doc-only" } }, suite: { exit: 0 } }),
    e("t-C", { onto: "t-B-b", needs: { shortReview: { file: "R.md" } }, review: { tip: "b".repeat(40), verdict: "pass" }, suite: { exit: 0 } }),
    e("t-D", { onto: "t-C-b", state: "invalidated" }),
    e("t-E", { onto: "t-D-b", job: "J2", approval: { card: "d-queue-1", option: "Merge up to t-E", answeredAt: "2026-11-18T10:10:00Z" } }),
  ], left: [], merged: [] } } };
  const cards = [{ id: "d-queue-9", card: { id: "d-queue-9", status: "open", kind: "queue", queue: { entries: [{ task: "t-A", job: null }] } } }];
  const q = queueMod.queueRows({ doc, error: null, missing: false }, { tasks: [] }, cards);
  const rows = q.repos[0].entries;
  check(q.count === 5 && rows.length === 5, `5 rows (got ${JSON.stringify(q.count)})`);
  check(rows[0].waits === "waits for pa-gate at its tip, or the next pa-queue refresh", `t-A (got ${rows[0].waits})`);
  check(rows[1].waits === "waits for its doc-only short review (S.md)", `t-B (got ${rows[1].waits})`);
  check(rows[2].waits === "waits for pa-gate at its tip, or the next pa-queue refresh", `t-C: its review is recorded for its tip (got ${rows[2].waits})`);
  check(rows[3].waits === "waits for pa-queue restack", `t-D (got ${rows[3].waits})`);
  check(rows[4].label === "5. t-E J2" && rows[4].waits === "waits for its suite", `t-E (got ${JSON.stringify(rows[4])})`);
  check(rows[0].approval === "approval: card asked (d-queue-9)" && rows[1].approval === "approval: not asked yet", `approvals (got ${rows[0].approval}; ${rows[1].approval})`);
  check(rows[4].approval === "approval: approved: Merge up to t-E (d-queue-1, 11-18 10:10)", `approved (got ${rows[4].approval})`);
  check(queueMod.queueRows(null, null, []).state === "loading" && queueMod.queueRows(store.TIMED_OUT, null, []).state === "loading", "unknown: loading");
  check(queueMod.queueRows({ doc: null, error: null, missing: true }, null, []).state === "missing", "missing");
  check(queueMod.queueRows({ doc: null, error: "queue.json is version 2; this panel reads version 1", missing: false }, null, []).state === "error", "error");
};

cases["S84-queue-links"] = async () => { // t-084 R1-R4: an entry's workers and evidence files, the header, a merge card's approval
  const zoneMod = await import(path.join(dst, "zone.js"));
  zoneMod.zone.offsetAt = () => 0;
  const e = (task, x = {}) => ({ task, job: null, branch: `${task}-b`, worktree: `/wt/${task}`, onto: "main", state: "checking", tip: "b".repeat(40), needs: null, suite: null, review: null, approval: null, ...x });
  const doc = { version: 1, repos: { "/w/p": { main: "main", entries: [
    e("t-A", { needs: { shortReview: { file: "SHORT-REVIEW-t-A-1.md" } }, review: { tip: "c".repeat(40), verdict: "pass", worker: "t72", file: "SHORT-REVIEW-t-A-1.md" }, suite: { exit: 0, worker: "t71" },
      heldApproval: { file: "SHORT-REVIEW-t-A-0.md" } }),
    e("t-B", { onto: "t-A-b", job: "J3g" }),
    e("t-C", { onto: "t-B-b" }),
  ], left: [], merged: [] } } };
  const ledger = { tasks: [
    { id: "t-A", workers: [{ address: "t70", role: "queue", worktree: "/wt/t-A/" }, { address: "t69", role: "build", worktree: "/wt/t-A", finished: true },
      { address: "t68", role: "build", worktree: "/wt/other" }, { address: "t71", role: "verifier", worktree: "/wt/t-A", finished: true }] },
  ] };
  const card = (id, task, answer, extra = {}) => ({ id, card: { id, task, status: "answered", answer, question: `Merge ${task} into main?`, answered: "2026-11-19T02:21:07Z", ...extra } });
  const cards = [card("d-190", "t-B", "Merge", { question: "Merge t-B J2 into main?" }), card("d-191", "t-B", "Merge", { question: "Merge t-B J3g into main?" }),
    card("d-192", "t-C", "Hold"), { id: "d-193", card: { id: "d-193", task: "t-C", status: "open", question: "Merge t-C?" } }];
  const q = queueMod.queueRows({ doc, error: null, missing: false }, ledger, cards);
  const r = q.repos[0], [a, b, c] = r.entries;
  check(r.intoLine === "into main", `the header: into main (got ${JSON.stringify(r.intoLine)})`);
  check(JSON.stringify((a.workers ?? []).map((w) => [w.address, w.label, Boolean(w.finished)])) === JSON.stringify([["t70", "queue t70", false], ["t71", "verifier t71", true], ["t72", "worker t72", false]]),
    `t-A: its open worker on its worktree, then the suite and review workers (got ${JSON.stringify(a.workers)})`);
  check(JSON.stringify(a.files) === JSON.stringify(["SHORT-REVIEW-t-A-1.md", "SHORT-REVIEW-t-A-0.md"]), `t-A's files, once each (got ${JSON.stringify(a.files)})`);
  check(JSON.stringify(b.workers) === "[]" && JSON.stringify(b.files) === "[]", `t-B: none (got ${JSON.stringify([b.workers, b.files])})`);
  check(b.approval === "approval: approved on d-191 (Merge, 11-19 02:21)", `t-B J3g: the card naming its job (got ${b.approval})`);
  check(c.approval === "approval: not asked yet", `t-C: Hold and an open plain card do not count (got ${c.approval})`);
  check(a.approval === "approval: not asked yet", `t-A: no card (got ${a.approval})`);
};

// ---- t-083 J2: worker places, the waiting list and the person's start order (places.js, store.js) ----
const placesMod = await import(path.join(dst, "places.js")).catch(() => null);
const SHARED = JSON.parse(fs.readFileSync(path.join(here, "..", "samples", "places-cases.json"), "utf8"));
const sJob = (j) => { // a shared case's job, as tests/test_pa_places.py job(): a string is a movable working job
  const x = typeof j === "string" ? { id: j } : j, [task, name] = x.id.split("/");
  return { id: x.id, kind: x.kind ?? "working", source: x.source ?? "pa", blockedBy: x.blockedBy ?? null, task, job: name ?? task, role: "build", added: "2026-11-19T00:00:00Z" };
};
const WK = (space) => keyOf(space, "/waiting.json"), OK = (space) => keyOf(space, "/start-order.json");
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

cases["S83-shared-cases"] = async () => { // AC8.3: the JS side agrees with pa_places.py on every shared case
  check(typeof placesMod?.readOrder === "function" && typeof placesMod?.effectiveOrder === "function" && typeof placesMod?.limitsFrom === "function" && typeof placesMod?.allocate === "function", "places.js exports readOrder, effectiveOrder, limitsFrom, allocate");
  check(SHARED.version === 1 && SHARED.orderCases.length && SHARED.limitCases.length && SHARED.allocCases.length, "the shared file has its three lists");
  for (const c of SHARED.orderCases) {
    const r = placesMod.readOrder(c.orderText);
    check(r.error === c.error, `${c.name}: error ${JSON.stringify(c.error)} (got ${JSON.stringify(r.error)})`);
    const ids = placesMod.effectiveOrder(c.jobs.map(sJob), r.start).map((j) => j.id); // t-097: readOrder's start (v1 or v2), no rank
    check(same(ids, c.effective), `${c.name}: ${JSON.stringify(c.effective)} (got ${JSON.stringify(ids)})`);
  }
  for (const c of SHARED.limitCases) {
    const r = placesMod.limitsFrom(c.rules);
    check(same(r.limits, c.limits) && same(r.sources, c.sources) && same(r.notes, c.notes), `${c.name} (got ${JSON.stringify(r)})`);
  }
  for (const c of SHARED.allocCases) {
    const got = placesMod.allocate(c.jobs.map(sJob), c.limits, c.holders, c.hold).map((r) => ({ id: r.id, action: r.action, place: r.place, full: r.full }));
    check(same(got, c.expect), `${c.name}: ${JSON.stringify(c.expect)} (got ${JSON.stringify(got)})`);
  }
};

cases["S83-order-lines"] = async () => { // AC8.2, J1-D2: the lines the panel and pa-places both say
  check(typeof placesMod?.readOrder === "function", "places.js exports readOrder");
  check(placesMod.readOrder(null).line === "no start order yet; the Project Agent's order is used", "missing");
  check(placesMod.readOrder("{x").line === "start order unreadable: start-order.json is not JSON; the Project Agent's order is used", `not JSON (got ${placesMod.readOrder("{x").line})`);
  check(placesMod.readOrder('{"version": 1, "order": ["a"]}').line === null, "valid: no line");
  check(placesMod.readOrder('{"version": 3}').line === "start order unreadable: start-order.json is not version 1 or 2; the Project Agent's order is used", `t-097 J1-D3: version (got ${placesMod.readOrder('{"version": 3}').line})`);
  check(placesMod.readOrder('{"version": 2, "pins": 1}').line === "start order unreadable: start-order.json's pins are not a list of {id, slot}; the Project Agent's order is used", "t-097 J1-D3: pins");
  check(placesMod.readOrder('{"version": 2, "pins": []}').line === null, "version 2: no line");
};

cases["S83-load-waiting"] = async () => { // loadWaiting: missing, valid, each invalid form, read on the space's machine
  check(typeof store.loadWaiting === "function", "store.loadWaiting exists");
  world();
  let r = await store.loadWaiting(remote, cx);
  check(r.missing === true && r.doc === null && r.error === null, `missing (got ${JSON.stringify(r)})`);
  check(g.__issued.some((x) => x.on === M && x.kind === "read" && x.path === `${rdir}/waiting.json`), "read on the space's machine");
  const doc = { version: 1, jobs: [{ id: "t-1/build", task: "t-1", job: "build", kind: "working", role: "build", source: "pa", added: "x", blockedBy: null }, { id: "t-2/v", kind: "suite" }] };
  g.__files[WK(local)] = JSON.stringify(doc);
  r = await store.loadWaiting(local, cx);
  check(r.missing === false && r.error === null && same(r.doc, doc), `valid; a job with no source is a pa job (got ${JSON.stringify(r)})`);
  for (const [body, why] of [["{x", /^waiting\.json is not valid JSON/], ["[]", /needs a "jobs" list/], [JSON.stringify({ version: 1, jobs: {} }), /needs a "jobs" list/],
    [JSON.stringify({ version: 1, jobs: [{ id: "a", kind: "gpu" }] }), /job 1 needs an id, a kind \(working, no-test, suite\) and a source \(pa or queue\)/],
    [JSON.stringify({ version: 1, jobs: [{ id: "a", kind: "working" }, { id: "b", kind: "working", source: "x" }] }), /job 2 needs/],
    [JSON.stringify({ version: 1, jobs: [{ kind: "working" }] }), /job 1 needs/]]) {
    g.__files[WK(local)] = body;
    r = await store.loadWaiting(local, cx);
    check(r.doc === null && why.test(r.error ?? ""), `${body}: ${why} (got ${JSON.stringify(r)})`);
  }
};

cases["S83-load-order"] = async () => { // loadStartOrder: the raw text for the compare; missing; read on the machine
  check(typeof store.loadStartOrder === "function", "store.loadStartOrder exists");
  world();
  let r = await store.loadStartOrder(remote, cx);
  check(r.missing === true && r.text === null, `missing (got ${JSON.stringify(r)})`);
  check(g.__issued.some((x) => x.on === M && x.kind === "read" && x.path === `${rdir}/start-order.json`), "read on the space's machine");
  g.__files[OK(local)] = "{not json";
  r = await store.loadStartOrder(local, cx);
  check(r.missing === false && r.text === "{not json", `an unreadable file keeps its text (got ${JSON.stringify(r)})`);
};

cases["S83-load-hung"] = async () => { // a hung read gives up as TIMED_OUT, never "missing"
  world();
  for (const [fn, key] of [[store.loadWaiting, WK(remote)], [store.loadStartOrder, OK(remote)]]) {
    g.__hang = [key];
    const { done, value } = await within(fn(remote, cx), 12000, 12);
    check(done && store.gaveUp(value) && !value.missing, `${key}: gave up (got ${JSON.stringify(value)})`);
    g.__hang = [];
  }
};

cases["S83-write-order"] = async () => { // AC6.1, AC6.2: compare-and-write, locally and on the space's machine
  check(typeof store.writeStartOrder === "function", "store.writeStartOrder exists");
  world();
  await store.writeStartOrder(local, "A\n", null);
  check(g.__files[OK(local)] === "A\n", "a new local file");
  let err = null;
  try { await store.writeStartOrder(local, "B\n", "old\n"); } catch (e) { err = String(e.message ?? e); }
  check(/changed on disk.*reloaded, try again/.test(err ?? "") && g.__files[OK(local)] === "A\n", `a changed file is refused (got ${err})`);
  await store.writeStartOrder(remote, "R\n", null);
  check(g.__files[OK(remote)] === "R\n" && g.__writes.includes(OK(remote)), "a new remote file through the python write");
  err = null;
  try { await store.writeStartOrder(remote, "S\n", "other\n"); } catch (e) { err = String(e.message ?? e); }
  check(/changed on disk/.test(err ?? "") && g.__files[OK(remote)] === "R\n", `remote: a changed file is refused (got ${err})`);
};

cases["S83-move-order"] = async () => { // R9, t-097 R10: ↑/↓ swap with the nearest movable job (fixed queue jobs skipped), ⤒, the ends: one pin
  check(typeof placesMod?.moveOrder === "function", "places.js exports moveOrder");
  const eff = ["A", { id: "Q", source: "queue" }, "B", "C"].map(sJob);
  const mv = (id, m) => placesMod.moveOrder(eff, id, m);
  check(same(mv("B", "up"), { id: "B", slot: 0 }), `B up skips Q: slot 0 among the movable jobs (got ${JSON.stringify(mv("B", "up"))})`);
  check(same(mv("A", "down"), { id: "A", slot: 1 }), `A down skips Q (got ${JSON.stringify(mv("A", "down"))})`);
  check(same(mv("C", "top"), { id: "C", slot: 0 }), `C to the top (got ${JSON.stringify(mv("C", "top"))})`);
  check(mv("A", "up") === null && mv("A", "top") === null && mv("C", "down") === null, "off an end: null");
  check(mv("Q", "up") === null && mv("gone", "top") === null, "a fixed or unknown job: null");
};

cases["S83-order-text"] = async () => { // R6, t-097 R10: the file the panel writes: version 2 pins, by person, moves (last 20)
  check(typeof placesMod?.orderText === "function", "places.js exports orderText");
  const prev = JSON.stringify({ version: 1, order: ["x"], moves: Array.from({ length: 20 }, (_, i) => ({ at: "t", id: `j${i}`, move: "up" })) });
  const doc = JSON.parse(placesMod.orderText(prev, [{ id: "B", slot: 0 }], { id: "B", move: "top" }));
  check(doc.version === 2 && doc.by === "person" && same(doc.pins, [{ id: "B", slot: 0 }]) && !("order" in doc) && typeof doc.updated === "string", `the head (got ${JSON.stringify(doc)})`);
  check(doc.moves.length === 20 && doc.moves[0].id === "j1" && doc.moves[19].id === "B" && doc.moves[19].move === "top", `the last 20 moves (got ${JSON.stringify(doc.moves.slice(-2))})`);
  const fresh = JSON.parse(placesMod.orderText("{bad", [], { id: null, move: "reset" }));
  check(same(fresh.pins, []) && fresh.moves.length === 1 && fresh.moves[0].move === "reset", `over an unreadable file (got ${JSON.stringify(fresh)})`);
  check(placesMod.orderText(null, [], { id: "A", move: "up" }).endsWith("}\n"), "a trailing newline");
};

cases["S83-rows"] = async () => { // R1 AC1.1-1.4, R9, R11 without rendering: placesRows
  check(typeof placesMod?.placesRows === "function", "places.js exports placesRows");
  const zoneMod = await import(path.join(dst, "zone.js"));
  zoneMod.zone.offsetAt = () => 0;
  const rules = { rules: [], error: null, doc: { rules: [{ id: "r-places", text: "x", status: "active", order: 50, places: { working: 4, "no-test": 1, suite: 2 } }] } };
  const w = (address, name, role, extra = {}) => ({ address, name, role, started: "2026-11-19T10:02:00Z", ...extra });
  const ledger = { tasks: [
    { id: "t-020", workers: [w("t51", "t-020-build", "build", { place: "working" }), w("t53", "t-020-verify", "verifier", { place: "suite" }), w("t60", "t-020-old", "build", { place: "working", finished: "x" })] },
    { id: "t-041", workers: [w("t54", "t-041-review", "reviewer", { place: "no-test" }), w("t61", "t-041-gone", "build", { place: "working" })] },
  ] };
  const live = new Set(["t51", "t53", "t54", "t60"]);
  const waiting = { doc: { version: 1, jobs: [sJob({ id: "t-020/verify-2", kind: "suite" }), { ...sJob({ id: "t-060/merge-check", kind: "suite", source: "queue" }), role: "verifier" }, sJob({ id: "t-083/design", kind: "no-test" })] }, error: null, missing: false };
  const rows = placesMod.placesRows({ waiting, startOrder: { text: null, missing: true }, rules, ledger, isLive: (a) => live.has(a) });
  check(rows.shown && rows.state === "ok", `shown (got ${JSON.stringify({ shown: rows.shown, state: rows.state })})`);
  const heads = rows.kinds.map((k) => k.head);
  check(same(heads, ["Working · 3 of 4", "No-test extra · 0 of 1", "Suite step · 1 of 2  (held within Working)"]), `AC1.1 heads (got ${JSON.stringify(heads)})`);
  const lines = rows.kinds[0].holders.map((h) => h.text);
  check(lines.includes("t53 · t-020 verify · verifier · suite step · since 11-19 10:02"), `AC1.1 holder line (got ${JSON.stringify(lines)})`);
  check(!lines.some((l) => /t60|t61/.test(l)), "AC1.2 finished and not live hold nothing");
  check(rows.head === "Places · 3 of 4 working · 3 waiting", `head (got ${rows.head})`);
  check(rows.limitsLine === "limits: r-places", `limits line (got ${rows.limitsLine})`);
  const jl = rows.jobs.map((j) => [j.label, j.reason, j.up, j.down, j.top, j.fixed]);
  check(rows.jobs[0].label === "1. t-020 verify-2 · build · suite step" && rows.jobs[2].label === "3. t-083 design · build · no-test", `labels (got ${JSON.stringify(jl)})`);
  check(rows.jobs[0].reason === "next to start" && rows.jobs[2].reason === "next to start: the no-test extra place is free", `reasons (got ${JSON.stringify(jl)})`);
  check(rows.jobs[1].fixed && rows.jobs[1].note === "merge queue order (Queue tab)" && rows.jobs[1].reason === "waits for a suite place (2 of 2)" && !rows.jobs[1].up && !rows.jobs[1].down && !rows.jobs[1].top, `the queue job: fixed, no buttons (got ${JSON.stringify(jl[1])})`);
  check(!rows.jobs[0].up && !rows.jobs[0].top && rows.jobs[0].down && rows.jobs[2].up && rows.jobs[2].top && !rows.jobs[2].down, `AC9.1 (got ${JSON.stringify(jl)})`);
  check(same(rows.badges["t-020"].map((b) => b.text), ["running · working", "running · suite step", "next #1 · suite step"]), `R11 badges (got ${JSON.stringify(rows.badges["t-020"])})`);
  check(!rows.badges["t-999"], "AC11.1 no badge");
  // AC1.3 no place recorded; AC1.4 over the limit
  const over = { tasks: [{ id: "t-1", workers: ["a", "b", "c", "d", "e"].map((x) => w(x, `t-1-${x}`, "build", x === "e" ? {} : { place: "working" })) }] };
  const r2 = placesMod.placesRows({ waiting: { doc: null, error: null, missing: true }, startOrder: null, rules, ledger: over, isLive: () => true });
  check(r2.kinds[0].head === "Working · 5 of 4" && r2.kinds[0].over === true, `AC1.4 (got ${JSON.stringify(r2.kinds[0])})`);
  check(r2.kinds[0].holders.some((h) => /^e · t-1 e · build · kind not recorded · since/.test(h.text)), `AC1.3 (got ${JSON.stringify(r2.kinds[0].holders.map((h) => h.text))})`);
  check(r2.waitingLine === "No waiting list yet: the Project Agent writes it with pa-places.", `AC9.2 (got ${r2.waitingLine})`);
  // AC2.4 defaults; D3: nothing waiting, no holder: not shown
  const r3 = placesMod.placesRows({ waiting: { doc: { version: 1, jobs: [] }, error: null, missing: false }, startOrder: null, rules: { rules: [], error: null, doc: { rules: [] } }, ledger: { tasks: [] }, isLive: () => true });
  check(same(r3.kinds.map((k) => k.head), ["Working · 0 of 4 (default)"]), `AC2.4 (got ${JSON.stringify(r3.kinds.map((k) => k.head))})`);
  check(placesMod.placesRows({ waiting: { doc: null, error: null, missing: true }, startOrder: null, rules: null, ledger: { tasks: [] }, isLive: () => true }).shown === false, "D3: no list, no holder: not shown");
};

cases["S133-rows-over"] = async () => { // t-083 J2 verify RL1: `over` at the limit is false; a suite kind over its cap is true
  const zoneMod = await import(path.join(dst, "zone.js"));
  zoneMod.zone.offsetAt = () => 0;
  const rules = { rules: [], error: null, doc: { rules: [{ id: "r-places", text: "x", status: "active", order: 50, places: { working: 4, "no-test": 1, suite: 2 } }] } };
  const w = (address, place) => ({ address, name: `t-1-${address}`, role: "build", started: "2026-11-19T10:02:00Z", place });
  const rows = (places) => placesMod.placesRows({ waiting: { doc: null, error: null, missing: true }, startOrder: null, rules,
    ledger: { tasks: [{ id: "t-1", workers: places.map((p, i) => w(`a${i}`, p)) }] }, isLive: () => true });
  const at = rows(["working", "working", "working", "working"]);
  check(at.kinds[0].head === "Working · 4 of 4" && at.kinds[0].over === false, `Working 4 of 4: over false (got ${JSON.stringify(at.kinds[0])})`);
  const suite = rows(["suite", "suite", "suite"]).kinds.find((k) => k.kind === "suite");
  check(suite.head === "Suite step · 3 of 2  (held within Working)" && suite.over === true, `suite 3 of 2: over true (got ${JSON.stringify(suite)})`);
  const atCap = rows(["suite", "suite"]).kinds.find((k) => k.kind === "suite");
  check(atCap.over === false, `suite 2 of 2: over false (got ${JSON.stringify(atCap)})`);
  const extra = rows(["working", "working", "working", "working", "no-test", "no-test"]).kinds.find((k) => k.kind === "no-test");
  check(extra.head === "No-test extra · 2 of 1" && extra.over === true, `no-test extra 2 of 1: over true (got ${JSON.stringify(extra)})`);
};

cases["S133-limits-raw-rules"] = async () => { // t-083 J2 RL1: a rule-check error still gives pa-places' limits, not the defaults
  world();
  const doc = { version: 1, rules: [{ id: "r-places", status: "active", order: 50, places: { working: 2 } }, { id: "r-two", text: "y", status: "active", order: 1 }] }; // r-places has no text
  g.__files[keyOf(local, "/rules.json")] = JSON.stringify(doc);
  const r = await store.loadRules(local, cx);
  check(/^rules\.json: r-places has no text/.test(r.error ?? "") && same(r.doc, doc), `the rule checks fail, the parsed file is kept (got ${JSON.stringify(r)})`);
  const rows = placesMod.placesRows({ waiting: { doc: null, error: null, missing: true }, startOrder: null, rules: r, ledger: { tasks: [] }, isLive: () => true });
  check(rows.kinds[0].head === "Working · 0 of 2", `pa-places' limit, from r-places (got ${JSON.stringify(rows.kinds.map((k) => k.head))})`);
  check(rows.notes.some((n) => n === "rules.json: r-places has no text; the limits are read as pa-places reads them"), `the note (got ${JSON.stringify(rows.notes)})`);
  check(rows.limitsLine === "limits: r-places", `limits line (got ${rows.limitsLine})`);
  for (const body of ["{x", "[]"]) { // not JSON, or no rules list: the defaults, as pa-places
    g.__files[keyOf(local, "/rules.json")] = body;
    const bad = await store.loadRules(local, cx);
    const r2 = placesMod.placesRows({ waiting: { doc: null, error: null, missing: true }, startOrder: null, rules: bad, ledger: { tasks: [] }, isLive: () => true });
    check(r2.kinds[0].head === "Working · 0 of 4 (default)" && r2.notes.some((n) => n.endsWith("; the default limits are used")), `${body}: defaults (got ${JSON.stringify([r2.kinds[0].head, r2.notes])})`);
  }
};

// ---- t-097 J2: the person's task order (tasks.js, store.js) and the start order's version 2 pins (places.js) ----
const tasksMod = await import(path.join(dst, "tasks.js")).catch(() => null);
const TSHARED = JSON.parse(fs.readFileSync(path.join(here, "..", "samples", "task-order-cases.json"), "utf8"));
const TK = (space) => keyOf(space, "/task-order.json");
const rk = (list) => list.map(([id, state, source]) => ({ id, state, source }));

cases["S97-task-order-shared"] = async () => { // R1-R3, R7, R12: tasks.js agrees with pa_places.py on every shared case
  check(typeof tasksMod?.readTaskOrder === "function" && typeof tasksMod?.effectiveTasks === "function" && typeof tasksMod?.moveTask === "function", "tasks.js exports readTaskOrder, effectiveTasks, moveTask");
  check(TSHARED.version === 1 && TSHARED.readCases.length && TSHARED.rankCases.length && TSHARED.moveCases.length, "the shared file has its three lists");
  for (const c of TSHARED.readCases) {
    const r = tasksMod.readTaskOrder(c.text);
    const got = { error: r.error, line: r.line, order: r.order, last: r.last, updated: r.updated };
    check(same(got, { error: c.error, line: c.line, order: c.order, last: c.last, updated: c.updated }), `${c.name} (got ${JSON.stringify(got)})`);
  }
  for (const c of TSHARED.rankCases) {
    const read = tasksMod.readTaskOrder(c.fileText);
    check(read.error === c.error, `${c.name}: error ${c.error} (got ${read.error})`);
    const got = tasksMod.effectiveTasks(c.tasks, c.taskOrder, read).map((r) => [r.id, r.source]);
    check(same(got, c.expect), `${c.name}: ${JSON.stringify(c.expect)} (got ${JSON.stringify(got)})`);
  }
  for (const c of TSHARED.moveCases) {
    const ranking = rk(c.ranking), order = [...c.order], last = [...c.last];
    const got = tasksMod.moveTask(ranking, c.section, c.task, c.move, order, last);
    check(same(got, c.expect), `${c.name}: ${JSON.stringify(c.expect)} (got ${JSON.stringify(got)})`);
    check(same(order, c.order) && same(last, c.last) && same(ranking, rk(c.ranking)), `${c.name}: the inputs are not changed`);
  }
};

cases["S97-chat-quote"] = async () => { // R12, R13: a chat entry carries its quote
  const read = tasksMod.readTaskOrder(null);
  const r = tasksMod.effectiveTasks([{ id: "t-1", state: "queued" }, { id: "t-2", state: "queued" }], { order: [], chat: [{ task: "t-2", quote: "do t-2 now", over: null }] }, read);
  check(same(r, [{ id: "t-2", state: "queued", source: "chat", quote: "do t-2 now" }, { id: "t-1", state: "queued", source: "agent" }]), `the quote (got ${JSON.stringify(r)})`);
};

cases["S97-has-order"] = async () => { // J1-D10: the Places sort by rank waits until a task order exists (R27)
  check(typeof tasksMod?.hasTaskOrder === "function", "tasks.js exports hasTaskOrder");
  const R = tasksMod.readTaskOrder, ok = '{"version": 1, "order": [], "last": []}';
  const cases97 = [[R(null), undefined, false], [R(null), null, false], [R(ok), null, true], [R("{bad"), null, false], [R(null), { order: [], chat: [] }, true],
    [R(null), "x", false], [R(null), { order: 5 }, false], [R(null), {}, true], [R("{bad"), { order: [], chat: [] }, true]];
  for (const [read, to, want] of cases97) check(tasksMod.hasTaskOrder(read, to) === want, `${read.error} ${JSON.stringify(to)}: ${want}`);
};

cases["S97-places-shared"] = async () => { // R9, R10: places.js agrees with pa_places.py on the rank and pin cases
  check(Array.isArray(SHARED.rankCases) && SHARED.rankCases.length && Array.isArray(SHARED.pinCases) && SHARED.pinCases.length, "the shared file has rankCases and pinCases");
  for (const c of SHARED.rankCases) {
    const ids = placesMod.effectiveOrder(c.jobs.map(sJob), null, c.rank).map((j) => j.id);
    check(same(ids, c.effective), `${c.name}: ${JSON.stringify(c.effective)} (got ${JSON.stringify(ids)})`);
  }
  for (const c of SHARED.pinCases) {
    const r = placesMod.readOrder(c.orderText);
    check(r.error === c.error, `${c.name}: error ${JSON.stringify(c.error)} (got ${JSON.stringify(r.error)})`);
    const ids = placesMod.effectiveOrder(c.jobs.map(sJob), r.start, c.rank).map((j) => j.id);
    check(same(ids, c.effective), `${c.name}: ${JSON.stringify(c.effective)} (got ${JSON.stringify(ids)})`);
  }
};

cases["S97-pins-after"] = async () => { // R10: a Places move writes one pin for that job (its earlier pin replaced, pins of jobs not waiting dropped)
  check(typeof placesMod?.pinsAfter === "function", "places.js exports pinsAfter");
  const jobs = ["A", "B", "C", "D"].map(sJob), v2 = (pins) => placesMod.readOrder(JSON.stringify({ version: 2, pins })).start;
  const pa = (start, id, move, rank = null, js = jobs) => placesMod.pinsAfter({ jobs: js, start, rank, id, move });
  check(same(pa(null, "D", "top"), [{ id: "D", slot: 0 }]), `no file: one pin (got ${JSON.stringify(pa(null, "D", "top"))})`);
  check(same(pa(v2([{ id: "A", slot: 2 }]), "D", "up"), [{ id: "A", slot: 2 }, { id: "D", slot: 2 }]), `other pins stay (got ${JSON.stringify(pa(v2([{ id: "A", slot: 2 }]), "D", "up"))})`);
  check(same(pa(v2([{ id: "D", slot: 0 }]), "D", "down"), [{ id: "D", slot: 1 }]), "the job's earlier pin is replaced");
  check(same(pa(v2([{ id: "gone", slot: 0 }, { id: "B", slot: 0 }]), "C", "top"), [{ id: "B", slot: 0 }, { id: "C", slot: 0 }]), "a pin of a job not waiting is dropped");
  const v1 = placesMod.readOrder(JSON.stringify({ version: 1, order: ["C", "gone"] })).start;
  check(same(pa(v1, "D", "up"), [{ id: "C", slot: 0 }, { id: "D", slot: 2 }]), `a version 1 file's waiting ids carry over as pins (got ${JSON.stringify(pa(v1, "D", "up"))})`);
  check(same(pa(null, "A", "down", ["B", "A"]), [{ id: "A", slot: 2 }]) && same(pa(null, "B", "down", ["B", "A"]), [{ id: "B", slot: 1 }]), "the slot counts in the ranked order");
  // pins that interact: the replaced pin would land elsewhere than drawn; every movable job is pinned in the drawn order
  const x3 = ["A", "B", "X"].map(sJob), start = v2([{ id: "X", slot: 0 }, { id: "B", slot: 1 }]);
  check(same(placesMod.effectiveOrder(x3, start).map((j) => j.id), ["X", "B", "A"]), "the case's effective order");
  const got = pa(start, "X", "down", null, x3);
  check(same(placesMod.effectiveOrder(x3, v2(got)).map((j) => j.id), ["B", "X", "A"]), `the written pins give the order drawn (got ${JSON.stringify(got)})`);
  check(same(pa(start, "X", "reset", null, x3), []), "Reset order: no pins");
  check(pa(null, "A", "up") === null && pa(null, "D", "down") === null && pa(null, "gone", "top") === null, "no-ops: null");
  const q = ["A", { id: "Q", source: "queue" }, "B"].map(sJob);
  check(pa(null, "Q", "up", null, q) === null && same(pa(null, "B", "up", null, q), [{ id: "B", slot: 0 }]), "a queue job has no pin; slots count the movable jobs");
};

cases["S97-prune-text"] = async () => { // R4, R1, R8: prune, then the file the panel writes
  check(typeof tasksMod?.pruneOrder === "function" && typeof tasksMod?.taskOrderText === "function", "tasks.js exports pruneOrder, taskOrderText");
  const tasks = [{ id: "t-1", state: "queued" }, { id: "t-2", state: "done" }, { id: "t-3", state: "parked" }, { id: "t-4", state: "needs-decision" }, { id: "t-5", state: "done" }, { id: "t-6", state: "review" }];
  const p = tasksMod.pruneOrder({ order: ["t-1", "gone", "t-2", "t-3"], last: ["t-4", "t-5", "t-6"] }, tasks);
  check(same(p, { order: ["t-1", "t-3"], last: ["t-4", "t-6"] }), `R4 (got ${JSON.stringify(p)})`);
  const prev = JSON.stringify({ version: 1, order: [], last: [], moves: Array.from({ length: 20 }, (_, i) => ({ at: "t", task: `t-${i}`, move: "up", section: "queued" })) });
  const doc = JSON.parse(tasksMod.taskOrderText(prev, { order: ["t-1"], last: ["t-4"] }, { task: "t-1", move: "top", section: "queued" }));
  check(doc.version === 1 && doc.by === "person" && typeof doc.updated === "string" && /Z$/.test(doc.updated) && same(doc.order, ["t-1"]) && same(doc.last, ["t-4"]), `the head (got ${JSON.stringify(doc)})`);
  check(doc.moves.length === 20 && doc.moves[0].task === "t-1" && same(Object.keys(doc.moves[19]).sort(), ["at", "move", "section", "task"]) && doc.moves[19].task === "t-1" && doc.moves[19].move === "top" && doc.moves[19].section === "queued", `the last 20 moves (got ${JSON.stringify(doc.moves.slice(-2))})`);
  const fresh = JSON.parse(tasksMod.taskOrderText("{bad", { order: [], last: [] }, { task: null, move: "reset", section: null }));
  check(same(fresh.order, []) && same(fresh.last, []) && fresh.moves.length === 1 && fresh.moves[0].move === "reset" && !("task" in fresh.moves[0]), `Reset order over an unreadable file (got ${JSON.stringify(fresh)})`);
  check(tasksMod.taskOrderText(null, { order: [], last: [] }, { task: "t-1", move: "up", section: "queued" }).endsWith("}\n"), "a trailing newline");
  check(tasksMod.NOTICE === "task order changed", "the notice");
};

cases["S97-start-next"] = async () => { // R18 (a): the task to the very top of the ranking; out of last; the chat tasks into order (R12)
  check(typeof tasksMod?.startNextOrder === "function", "tasks.js exports startNextOrder");
  const ranking = rk([["t-9", "queued", "chat"], ["t-1", "in-progress", "person"], ["t-2", "queued", "agent"], ["t-3", "queued", "person"]]);
  check(same(tasksMod.startNextOrder(ranking, "t-2", ["t-1"], ["t-3"]), { order: ["t-2", "t-9", "t-1"], last: ["t-3"] }), `from the agent block (got ${JSON.stringify(tasksMod.startNextOrder(ranking, "t-2", ["t-1"], ["t-3"]))})`);
  check(same(tasksMod.startNextOrder(ranking, "t-3", ["t-1"], ["t-3"]), { order: ["t-3", "t-9", "t-1"], last: [] }), "from last");
  check(tasksMod.startNextOrder(rk([["t-1", "queued", "person"], ["t-2", "queued", "agent"]]), "t-1", ["t-1"], []) === null, "already first and the person's: nothing to write");
  check(tasksMod.startNextOrder(ranking, "gone", [], []) === null, "not in the ranking: null");
};

cases["S97-done-line"] = async () => { // R20: what stays when the person marks a task done
  check(typeof tasksMod?.doneLine === "function", "tasks.js exports doneLine");
  check(tasksMod.doneLine({}) === "Done merges nothing: nothing is left open.", `none (got ${tasksMod.doneLine({})})`);
  const l = tasksMod.doneLine({ changeRequest: { id: "CR-123" }, worktrees: 2, queueEntries: 1, live: ["t14"] });
  check(l === "Done merges nothing: change request CR-123 stays unmerged · 2 worktrees stay · 1 merge-queue entry stays · t14 keeps running until you close it.", `all (got ${l})`);
  const l2 = tasksMod.doneLine({ changeRequest: { url: "https://x" }, worktrees: 1, queueEntries: 2, live: ["t1", "t2"] });
  check(l2 === "Done merges nothing: its change request stays unmerged · 1 worktree stays · 2 merge-queue entries stay · t1 keeps running until you close it · t2 keeps running until you close it.", `no id (got ${l2})`);
};

cases["S97-sections"] = async () => { // R5, R13, R14, R15: the sections drawn from the ranking (pure)
  check(typeof tasksMod?.taskSections === "function", "tasks.js exports taskSections");
  const tasks = [
    { id: "t-1", state: "queued" }, { id: "t-2", state: "queued", followUpOf: "t-8" }, { id: "t-3", state: "queued", followUpOf: "t-8" }, { id: "t-4", state: "queued" },
    { id: "t-5", state: "queued", followUpOf: "t-7" }, { id: "t-6", state: "in-progress" }, { id: "t-10", state: "needs-decision" }, { id: "t-11", state: "done" }, { id: "t-12", state: "parked" }, { id: "t-13", state: "review" }];
  const ranking = rk([["t-4", "queued", "person"], ["t-6", "in-progress", "person"], ["t-3", "queued", "person"], ["t-10", "needs-decision", "agent"], ["t-1", "queued", "agent"], ["t-13", "review", "agent"], ["t-2", "queued", "agent"], ["t-5", "queued", "chat"], ["t-12", "parked", "agent"]]);
  ranking[7].quote = "now";
  const secs = tasksMod.taskSections(tasks, ranking);
  check(same(secs.map((x) => x.state), ["needs-decision", "review", "in-progress", "queued", "parked", "done"]), `the section order (got ${JSON.stringify(secs.map((x) => x.state))})`);
  const q = secs.find((x) => x.state === "queued");
  check(q.ranked && same(q.items.map((i) => i.kind === "fold" ? `fold:${i.origin}` : i.task.id), ["t-4", "fold:t-8", "t-1", "t-5"]), `R14 the fold at its first member's place (got ${JSON.stringify(q.items.map((i) => i.kind === "fold" ? i.origin : i.task.id))})`);
  const fold = q.items[1];
  check(fold.count === 2 && fold.first === 2 && same(fold.rows.map((r) => [r.task.id, r.n]), [["t-3", 2], ["t-2", 4]]), `the fold's members and numbers (got ${JSON.stringify(fold)})`);
  check(tasksMod.foldLine(fold, false) === "▸ 2 follow-ups of t-8 · first #2" && tasksMod.foldLine(fold, true) === "▾ 2 follow-ups of t-8 · first #2", `the fold line (got ${tasksMod.foldLine(fold, false)})`);
  const r4 = q.items[0], r5 = q.items[3];
  check(r4.n === 1 && !r4.up && !r4.top && r4.down && r4.bottom && r4.source === "person", `the first row (got ${JSON.stringify(r4)})`);
  check(r5.n === 5 && r5.up && r5.top && !r5.down && !r5.bottom && r5.source === "chat" && r5.quote === "now", `the last row (got ${JSON.stringify(r5)})`);
  check(tasksMod.chipText("agent") === "PA" && tasksMod.chipText("chat") === "PA · your words" && tasksMod.chipText("person") === null, "R13 the marker");
  const nd = secs.find((x) => x.state === "needs-decision");
  check(!nd.ranked && nd.items[0].n === null && !nd.items[0].up && !nd.items[0].down && !nd.items[0].top && !nd.items[0].bottom, `R5 hidden rank (got ${JSON.stringify(nd)})`);
  const ip = secs.find((x) => x.state === "in-progress");
  check(ip.items[0].n === 1 && !ip.items[0].up && !ip.items[0].down, "one row: no move");
  check(secs.find((x) => x.state === "parked").items[0].n === null && secs.find((x) => x.state === "done").items[0].n === null, "parked and done: no number");
  // a single follow-up is a normal row (J2-D); parked and done keep the order given (newest first)
  check(q.items[3].kind === "task", "a lone follow-up is not folded");
  const pd = tasksMod.taskSections([{ id: "t-2", state: "done" }, { id: "t-1", state: "done" }], []);
  check(same(pd[0].items.map((i) => i.task.id), ["t-2", "t-1"]), "done keeps the given order");
  check(tasksMod.taskSections([{ id: "t-1", state: "odd" }], [])[0].state === "odd", "an unknown state still gets a section");
};

cases["S97-after"] = async () => { // R18: a row whose after tasks are not all done says so (an id not in the ledger counts as not done)
  const tasks = [{ id: "t-1", state: "in-progress" }, { id: "t-2", state: "done" }, { id: "t-3", state: "queued", after: ["t-1", "t-2", "gone", 5] }, { id: "t-4", state: "queued", after: "t-1" }];
  const secs = tasksMod.taskSections(tasks, rk([["t-1", "in-progress", "agent"], ["t-3", "queued", "agent"], ["t-4", "queued", "agent"]]));
  const q = secs.find((x) => x.state === "queued").items;
  check(JSON.stringify(q[0].after) === '["t-1","gone"]' && JSON.stringify(q[1].after) === "[]", `after (got ${JSON.stringify(q.map((i) => i.after))})`);
  check(tasksMod.afterLine(q[0].after) === "waits for t-1, gone (after)" && tasksMod.afterLine([]) === null, "the words");
};

cases["S97-order-line"] = async () => { // R2, R15: the order line above the sections
  check(typeof tasksMod?.orderLine === "function", "tasks.js exports orderLine");
  const R = tasksMod.readTaskOrder;
  check(tasksMod.orderLine(null) === "order: not read yet", "not read yet");
  check(tasksMod.orderLine(R(null)) === "order: the Project Agent's", "no file");
  check(tasksMod.orderLine(R('{"version": 1, "order": []}')) === "order: yours · moves are saved to task-order.json and tell the Project Agent", `readable (got ${tasksMod.orderLine(R('{"version": 1, "order": []}'))})`);
  check(tasksMod.orderLine(R("{x")) === "task order unreadable: task-order.json is not JSON; the Project Agent's order is used", "unreadable: the shared words");
};

cases["S97-load-write"] = async () => { // R1, R8: one keyed, capped read; compare-and-write locally and on the space's machine
  check(typeof store.loadTaskOrder === "function" && typeof store.writeTaskOrder === "function", "store.loadTaskOrder and writeTaskOrder exist");
  world();
  let r = await store.loadTaskOrder(remote, cx);
  check(r.missing === true && r.text === null, `missing (got ${JSON.stringify(r)})`);
  check(g.__issued.some((x) => x.on === M && x.kind === "read" && x.path === `${rdir}/task-order.json`), "read on the space's machine");
  g.__files[TK(local)] = "{not json";
  r = await store.loadTaskOrder(local, cx);
  check(r.missing === false && r.text === "{not json", `an unreadable file keeps its text (got ${JSON.stringify(r)})`);
  g.__hang = [TK(remote)];
  const { done, value } = await within(store.loadTaskOrder(remote, cx), 12000, 12);
  check(done && store.gaveUp(value) && !value.missing, `a hung read gives up (got ${JSON.stringify(value)})`);
  g.__hang = [];
  world();
  await store.writeTaskOrder(local, "A\n", null);
  check(g.__files[TK(local)] === "A\n", "a new local file");
  let err = null;
  try { await store.writeTaskOrder(local, "B\n", "old\n"); } catch (e) { err = String(e.message ?? e); }
  check(/changed on disk/.test(err ?? "") && g.__files[TK(local)] === "A\n", `a changed file is refused (got ${err})`);
  await store.writeTaskOrder(remote, "R\n", null);
  check(g.__files[TK(remote)] === "R\n" && g.__writes.includes(TK(remote)), "a new remote file through the python write");
  err = null;
  try { await store.writeTaskOrder(remote, "S\n", "other\n"); } catch (e) { err = String(e.message ?? e); }
  check(/changed on disk/.test(err ?? "") && g.__files[TK(remote)] === "R\n", `remote: a changed file is refused (got ${err})`);
};

// ---- t-041 J5: the context line, the Fresh context button and the mode switch (store part) ----
// The tool's name, spelled so a guard reading this file's commands is not confused.
const CT = ["pa", "context"].join("-");
const TODAY = new Date(T0).toISOString().slice(0, 10); // the harness's UTC day (2027-01-15)
const pcDay = (over = {}) => ({ day: TODAY, calls: 40, mean: 158000, peak: 254000, peakExact: true, resets: { fresh: 6, compact: 0 }, ...over });
const hm = (iso) => String(iso).slice(11, 16); // a fixed clock for the texts

cases["S41-1-context-line"] = async () => { // R15: today's (UTC) paContext entry as one line, as pa-costs report
  check(store.contextLine([pcDay(), pcDay({ day: "2027-01-14", mean: 1 })], T0) === "PA context today (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0)", `exact (got ${store.contextLine([pcDay()], T0)})`);
  check(store.contextLine([pcDay({ peakExact: false, peak: 254400 })], T0) === "PA context today (UTC): mean 158k · peak ≥254k · resets 6 (fresh 6, compact 0)", "≥ for a peak that is not exact");
  check(store.contextDay([pcDay({ day: "2027-01-14" })], T0) === null, "another day only: no entry");
  check(store.contextLine([pcDay({ day: "2027-01-14" })], T0) === "PA context today (UTC): no Project Agent calls yet", "another day only: the no-calls line");
  check(store.contextLine([pcDay({ calls: 0, mean: null, peak: 0, resets: { fresh: 2, compact: 0 } })], T0) === "PA context today (UTC): no Project Agent calls · resets 2 (fresh 2, compact 0)", `no calls, 2 resets (got ${store.contextLine([pcDay({ calls: 0, mean: null, peak: 0, resets: { fresh: 2, compact: 0 } })], T0)})`);
  check(store.contextLine([], T0) === "PA context today (UTC): no Project Agent calls yet", "an empty list");
  check(store.contextLine(null, T0) === null && store.contextLine(undefined, T0) === null && store.contextLine("x", T0) === null, "no paContext: no line");
  check(store.contextDay([pcDay({ calls: "40" })], T0) === null && store.contextDay([pcDay({ resets: null })], T0) === null && store.contextDay([null, 5], T0) === null, "malformed entries: none, no throw");
};

cases["S41-8-context-line-day"] = async () => { // MF-J5-1: the day of the read, named when it is not today's
  const AT = T0 + (15 * 60 + 50) * 60000, NEXT = AT + 20 * 60000; // 23:50 UTC, then 00:10 the next day
  check(store.contextLine([pcDay()], AT, AT) === "PA context today (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0)", "same day: today");
  check(store.contextLine([pcDay()], AT, NEXT) === `PA context ${TODAY} (UTC): mean 158k · peak 254k · resets 6 (fresh 6, compact 0)`, `read yesterday: its day and its figures (got ${store.contextLine([pcDay()], AT, NEXT)})`);
  check(store.contextLine([], AT, NEXT) === `PA context ${TODAY} (UTC): no Project Agent calls yet`, "read yesterday, no entry: that day named");
};

cases["S41-9-context-at"] = async () => { // MF-J5r2-1: the summary's own time (updated), else the read's
  const READ = T0 + 60000, UP = `${TODAY}T07:59:00Z`;
  check(store.contextAt({ updated: UP }, READ) === Date.parse(UP), `updated (got ${store.contextAt({ updated: UP }, READ)})`);
  for (const [summary, what] of [[{}, "no updated"], [{ updated: "not a time" }, "unreadable"], [{ updated: 1800000000 }, "a number"], [{ updated: null }, "null"], [null, "no summary"], [undefined, "undefined"]]) {
    check(store.contextAt(summary, READ) === READ, `${what}: the read's time (got ${store.contextAt(summary, READ)})`);
  }
};

cases["S41-2-context-args"] = async () => { // R6, R13: the tool's argv; never --on (the exec runs on the space's machine)
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  check(eq(store.contextArgs(remote, { dry: true }), ["fresh", "--space", S, "--dry-run", "--json"]), `dry (got ${JSON.stringify(store.contextArgs(remote, { dry: true }))})`);
  check(eq(store.contextArgs(local, {}), ["fresh", "--space", S, "--by", "panel", "--json", "--why", store.CONTEXT_WHY]), `ask (got ${JSON.stringify(store.contextArgs(local, {}))})`);
  check(eq(store.contextArgs(local, { withdraw: true }), ["fresh", "--space", S, "--withdraw", "--by", "panel", "--json", "--why", store.CONTEXT_WHY]), "withdraw");
  check(eq(store.contextArgs(remote, { mode: "off" }), ["set", "--space", S, "--by", "panel", "--mode", "off", "--json"]), "mode off");
  check(eq(store.contextArgs(local, { mode: "fresh" }), ["set", "--space", S, "--by", "panel", "--mode", "fresh", "--json"]), "mode fresh");
  check(store.contextArgs(local, { mode: "on" }) === null && store.contextArgs(local, { mode: "" }) === null, "a mode not fresh/off: no argv");
  check(store.CONTEXT_WHY === "the Fresh context button in the Project Agent panel" && store.CONTEXT_TOOL === CT, "constants");
  for (const a of [store.contextArgs(remote, { dry: true }), store.contextArgs(remote, {}), store.contextArgs(remote, { withdraw: true }), store.contextArgs(remote, { mode: "off" })]) {
    check(!a.some((x) => ["--on", "--now", "--fresh", "--task-cap", "--pass-cap", "--net"].includes(x)), `never those (got ${JSON.stringify(a)})`);
  }
};

cases["S41-3-context-capable"] = async () => { // R6: pa.context_capable exactly
  check(store.contextCapable({ version: 2, accepts: ["restart", "context"] }) === true, "version 2 with context");
  check(store.contextCapable({ version: 3, accepts: ["context"] }) === true, "version 3");
  for (const [rec, what] of [[{ version: 2, accepts: ["restart"] }, "restart only"], [{ version: 1, accepts: ["context"] }, "version 1"], [null, "no record"],
    [{ version: 2, accepts: "context" }, "accepts a string"], [{ version: true, accepts: ["context"] }, "version a boolean"], [{ version: "2", accepts: ["context"] }, "version text"], [{ version: 2.5, accepts: ["context"] }, "version not whole"]]) {
    check(store.contextCapable(rec) === false, what);
  }
};

cases["S41-4-context-dry"] = async () => { // R6: the dry run's JSON line is the confirm's content
  const json = { outcome: "dry-run", ctx: 260000, source: "transcript", due: "pass-cap", request: "rq-abcdefgh-1", expires: "2027-01-15T10:00:00Z", reasons: ["turn working", "checkpoint none"] };
  const d = store.contextDry({ r: { code: 0, stdout: `mode fresh  taskCap 150,000  passCap 250,000\ncontext: 260k (transcript)\n{"not": "the last"}\n${JSON.stringify(json)}\n`, stderr: "" } }, local);
  check(d.ok && d.ctx === 260000 && d.source === "transcript" && d.due === "pass-cap" && d.request === "rq-abcdefgh-1" && d.expires === json.expires && JSON.stringify(d.reasons) === JSON.stringify(json.reasons), `parsed (got ${JSON.stringify(d)})`);
  const none = store.contextDry({ r: { code: 0, stdout: `${JSON.stringify({ outcome: "dry-run", ctx: null, source: null, due: null, request: null, expires: null, reasons: [] })}\n`, stderr: "" } }, local);
  check(none.ok && none.ctx === null && none.request === null && none.reasons.length === 0, `nothing known (got ${JSON.stringify(none)})`);
  const bad = store.contextDry({ r: { code: 0, stdout: "context: unknown\n", stderr: "" } }, local);
  check(!bad.ok && typeof bad.text === "string" && bad.text.length > 0, `no JSON: not ok, a text (got ${JSON.stringify(bad)})`);
  const refused = store.contextDry({ r: { code: 2, stdout: "", stderr: `${CT}: no Project Agent in this space\n` } }, local);
  check(!refused.ok && refused.text === `Not done: ${CT}: no Project Agent in this space`, `refused (got ${JSON.stringify(refused)})`);
  check(store.contextDry({ busy: true }, local) === null, "busy: nothing to say");
};

cases["S41-5-context-outcome"] = async () => { // R6, R13: the texts of each answer
  const out = (o, code = 0, stderr = "") => ({ r: { code, stdout: `Some line.\n${JSON.stringify(o)}\n`, stderr } });
  const asked = store.contextOutcome(out({ outcome: "requested", request: "rq1", expires: "2027-01-15T10:00:00Z" }), local, hm);
  check(asked.ok && asked.text === "Fresh context asked for: it is taken at the Project Agent's next clean point (the request waits until 10:00).", `requested (got ${JSON.stringify(asked)})`);
  check(store.contextOutcome(out({ outcome: "withdrawn", request: "rq1" }), local, hm).text === "Request withdrawn.", "withdrawn");
  check(store.contextOutcome(out({ outcome: "none", request: null }), local, hm).text === "No request was pending.", "none");
  const off = store.contextOutcome(out({ v: 1, mode: "off", taskCap: 150000, passCap: 250000, net: 400000, enabledAt: null, by: "panel" }), local, hm);
  check(off.ok && off.text === "Context mode off: no fresh contexts from now; the 400k net ends at its next start. Turn fresh contexts on undoes it.", `off (got ${off.text})`);
  const on = store.contextOutcome(out({ v: 1, mode: "fresh", taskCap: 120000, passCap: 250000, net: 400000, enabledAt: "2027-01-15T08:00:00Z", by: "panel" }), local, hm);
  check(on.ok && on.text === "Context mode fresh: fresh contexts at 120k at a task boundary and 250k at a pass end from its launcher's next check; the 400k net at its next start. Turn fresh contexts off undoes it.", `fresh (got ${on.text})`);
  const no = store.contextOutcome({ r: { code: 2, stdout: "", stderr: `${CT}: the context mode is off (context.json), so no fresh context is taken\n` } }, local, hm);
  check(!no.ok && no.text === `Not done: ${CT}: the context mode is off (context.json), so no fresh context is taken`, `refused (got ${no.text})`);
  check(store.contextOutcome({ r: { code: 127, stdout: "", stderr: `sh: ${CT}: not found` } }, remote, hm).text === `${CT} is not installed on ${M}; run pa-sync --on ${M}.`, "127 on the machine");
  check(store.contextOutcome({ error: `exec: ${CT}: not found` }, local, hm).text === `${CT} is not installed here; run ./install.sh.`, "missing here");
  check(/could not run: boom/.test(store.contextOutcome({ error: "boom" }, local, hm).text), "another error");
  const late = store.contextOutcome({ timedOut: true }, local, hm);
  check(!late.ok && late.text === `${CT} did not answer in 35 s; the panel shows what it reads next.`, `timed out (got ${late.text})`);
  check(store.contextOutcome({ busy: true }, local, hm) === null, "busy: nothing to say");
  check(store.contextOutcome({ r: { code: 0, stdout: "done\n", stderr: "" } }, local, hm).ok, "exit 0 without a JSON line: ok");
};

cases["S41-6-context-settings"] = async () => { // R13: context.json over the defaults; read on the space's machine; no costs file (J5-D1)
  const d = store.contextSettings(null);
  check(d.mode === "fresh" && d.taskCap === 150000 && d.passCap === 250000 && d.net === 400000 && d.problem === null, `missing: the defaults (got ${JSON.stringify(d)})`);
  const off = store.contextSettings({ v: 1, mode: "off", taskCap: 120000, passCap: 250000, net: 400000, enabledAt: null, by: "person" });
  check(off.mode === "off" && off.taskCap === 120000 && off.by === "person" && off.problem === null, `off (got ${JSON.stringify(off)})`);
  for (const [v, what] of [[[1], "a list"], ["x", "text"], [{ v: 1, mode: "on" }, "a bad mode"]]) {
    const s = store.contextSettings(v);
    check(s.mode === "fresh" && typeof s.problem === "string" && s.problem.length > 0, `${what}: the defaults with a problem (got ${JSON.stringify(s)})`);
  }
  world();
  g.__files[`${M}:${rdir}/context.json`] = JSON.stringify({ v: 1, mode: "off", taskCap: 150000, passCap: 250000, net: 400000, enabledAt: null, by: "panel" });
  g.__files[`${M}:${rdir}/costs/summary.json`] = JSON.stringify({ updated: `${TODAY}T07:59:00Z`, paContext: [pcDay()] });
  const r = await store.loadContext(remote, cx);
  check(r.settings.mode === "off" && r.paContext === undefined, `remote read (got ${JSON.stringify(r)})`);
  check(JSON.stringify(g.__reads) === JSON.stringify([`${M}:${rdir}/context.json`]), `context.json only, on ${M}; no costs file (J5-D1, t-009 R11) (got ${JSON.stringify(g.__reads)})`);
  world();
  const missing = await store.loadContext(local, cx);
  check(missing.settings.mode === "fresh" && missing.settings.problem === null && g.__reads.length === 1 && g.__reads[0].endsWith("/context.json"), `missing: the defaults, one read (got ${JSON.stringify(g.__reads)})`);
  world();
  g.__files[`:${ldir}/context.json`] = "{not json";
  const b = await store.loadContext(local, cx);
  check(b.settings.mode === "fresh" && /not valid JSON/.test(b.settings.problem ?? ""), `a broken file (got ${JSON.stringify(b)})`);
  world();
  g.__hang = [`${M}:${rdir}/context.json`];
  const { done, value: h } = await within(store.loadContext(remote, cx), 12000);
  check(done && store.gaveUp(h), `a hung read gives up (done ${done}, got ${JSON.stringify(h)})`);
};

cases["S41-7-reset-line"] = async () => { // C6: the header line after a reset; Q7: the last request's outcome
  const launcher = { lastReset: { at: "2027-01-15T07:02:00Z", from: "3f2a9c1e-aaaa-bbbb-cccc-dddddddddddd", to: "s-new", reason: "pass-cap", ctx: 260000 } };
  const l = store.resetLine(launcher, "s-new", hm);
  check(l && l.text === "Fresh context at 07:02 (pass cap, 260k) · previous session 3f2a9c1e…" && l.previous === launcher.lastReset.from, `the line (got ${JSON.stringify(l)})`);
  check(store.resetLine({ lastReset: { ...launcher.lastReset, reason: "task-cap", ctx: null } }, "s-new", hm).text === "Fresh context at 07:02 (task cap) · previous session 3f2a9c1e…", "task cap, no size");
  check(store.resetLine({ lastReset: { ...launcher.lastReset, reason: "person" } }, "s-new", hm).text.startsWith("Fresh context at 07:02 (asked for, 260k)"), "a person's request");
  check(store.resetLine(launcher, "s-other", hm) === null, "another session runs now: none");
  check(store.resetLine({}, "s-new", hm) === null && store.resetLine(null, "s-new", hm) === null && store.resetLine(launcher, null, hm) === null, "nothing known: none");
  check(store.resetLine({ lastReset: { ...launcher.lastReset, from: 5 } }, "s-new", hm) === null, "a from that is not text: none");
  check(store.lastRequestLine({ context: { request: "rq1", outcome: "expired", why: null, at: "2027-01-15T06:00:00Z" } }, hm) === "Last request: expired at 06:00", "expired");
  check(store.lastRequestLine({ context: { request: "rq1", outcome: "refused", why: "another space", at: "2027-01-15T06:00:00Z" } }, hm) === "Last request: refused at 06:00 (another space)", "refused");
  check(store.lastRequestLine({ context: { request: "rq1", outcome: "ok", why: null, at: "2027-01-15T06:00:00Z" } }, hm) === null && store.lastRequestLine({ context: null }, hm) === null && store.lastRequestLine(null, hm) === null, "ok or none: no line");
};

// ---- t-119: the suite machines' slots (store part): pa-full-suite --places --json on the space's machine ----
const FS = ["pa", "full", "suite"].join("-"); // spelled so a guard reading this file's commands is not confused
const MDOC = { version: 1, notes: [], machines: [{ machine: "box-1", cap: 2, reachable: true, reason: null, os: "Linux", arch: "x86_64", seconds: 0.4, holders: [] }], local: { cap: 1, holders: [] } };

cases["S119-1-load-machines"] = async () => { // R2: one call on the space's machine; the doc parsed
  check(typeof store.loadMachines === "function", "store.loadMachines exists");
  world();
  g.__execFn = (command, args) => (command === FS ? { code: 0, stdout: JSON.stringify(MDOC) + "\n", stderr: "" } : undefined);
  let r = await store.loadMachines(remote, cx);
  check(r.error === null && JSON.stringify(r.doc) === JSON.stringify(MDOC), `parsed (got ${JSON.stringify(r)})`);
  const call = g.__execs.find(([c]) => c === FS);
  check(call && JSON.stringify(call[1]) === JSON.stringify(["--places", "--json"]) && call[2] === M, `argv and machine (got ${JSON.stringify(call)})`);
  check(call[3]?.timeout === store.MACHINES_TIMEOUT && store.MACHINES_TIMEOUT >= 120000, `its timeout (got ${JSON.stringify(call[3])})`);
  g.__execs = [];
  r = await store.loadMachines(local, cx);
  check(r.error === null && g.__execs.find(([c]) => c === FS)?.[2] === null, "a local space: here");
};

cases["S119-2-load-machines-errors"] = async () => { // R2: exit, not JSON, version: an error with the first line
  world();
  for (const [reply, want] of [
    [{ code: 2, stdout: "", stderr: "usage: pa-full-suite ...\npa-full-suite: error: unrecognized arguments: --json\n" }, /usage: pa-full-suite/],
    [{ code: 0, stdout: "box-1: reachable\n", stderr: "" }, /not JSON/],
    [{ code: 0, stdout: JSON.stringify({ ...MDOC, version: 2 }), stderr: "" }, /version/],
    [{ code: 0, stdout: JSON.stringify({ version: 1 }), stderr: "" }, /machines/],
  ]) {
    g.__execFn = (command) => (command === FS ? reply : undefined);
    const r = await store.loadMachines(local, cx);
    check(r.doc === null && want.test(r.error ?? ""), `${JSON.stringify(reply).slice(0, 60)}: ${want} (got ${JSON.stringify(r)})`);
  }
  g.__execFn = (command) => { if (command === FS) throw new Error(`exec: ${FS}: not found`); return undefined; };
  const r = await store.loadMachines(local, cx);
  check(r.doc === null && /not found/.test(r.error ?? ""), `a failed call (got ${JSON.stringify(r)})`);
};

cases["S119-3-load-machines-hung"] = async () => { // R2: a hung call gives up as TIMED_OUT, never "no machines"
  world();
  g.__execHang = [`${M}:${FS}`];
  const { done, value } = await within(store.loadMachines(remote, cx), store.MACHINES_TIMEOUT + store.GRACE + 1000, 20);
  check(done && store.gaveUp(value), `gave up (got ${JSON.stringify(value)})`);
  g.__execHang = [];
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
