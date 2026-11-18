// The Costs view (t-009): a space's spend, from the costs/summary.json that `pa-costs` writes on the
// space's machine. Tokens are measured; every money figure is an estimate from the profile's price
// table and says "est."; billed figures say "billed · account-wide". Generic: no currency, price or
// provider here; the currency code comes from the summary. Where the summary has totals.cc (t-029), Claude Code's own
// list-price figure is shown beside est. (est. first; alone only where there is no est.), labelled "Claude Code" or
// "CC", always USD, never added to est.; it is read from the same summary.json (no other read or exec).
//   reads:  costs/summary.json only (never a session file), and one ls of costs/ when it is missing;
//           only while this view is shown in a shown copy (main.js reload(), after its hidden return),
//           at most once per COSTS_EVERY per space, one in flight per space. The billed comparison:
//           the local `pa-costs billed compare --json`, at most once per BILLING_EVERY while shown.
//           `shown(spaceId)` (main.js) is asked live before each read and exec, also between the steps of one
//           (before the ls, the remote report and the second compare): a run, read or compare that ends after
//           the view was left starts nothing; the space is marked stale and read at once when shown.
//   days:   Today, Last 7 days and By day are this machine's local days when the summary's days are in its zone (t-036:
//           the collect passes it, zone.js); else they are the summary's days, named ("(UTC)" for an older summary).
//   scopes: (t-032 J1d) where the summary has them (pa-costs of t-032 on): the account beside Total, or an Accounts section
//           with two or more; with two or more scopes a By scope section, each scope's "priced from" where it was priced from
//           another (wider) scope, the scopes of a model in its drill and the scope of each session line. Scopes and prices
//           are said in words (t-050: "provider p, account a, no region recorded"; "the price file's default prices valid
//           from <date>"), never as "?" or "*". An older summary has none of these keys and draws as before; nothing more is read.
//   runs:   on a click only: Refresh (`pa-costs collect`, with this panel's zone), Reconstruct (`pa-costs reconstruct`) on the
//           space's machine, Turn off (`pa-costs billed disable`, here). Nothing else writes (R12).

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { loadCosts, localId, capped, gaveUp, TIMED_OUT, READ_LIMIT, missingTool } from "./store.js";
import { run } from "./host.js";
import { Copier, sel } from "./copy.js";
import { zoneArgs, oldTool, localDate, daysBefore, summaryZone } from "./zone.js";

export const COSTS_EVERY = 10000; // the summary of a shown space: at most one read this often
export const BILLING_EVERY = 60000; // the billed comparison: at most one this often
const CACHE_MAX = 8; // spaces whose last summary is kept, the least recently shown dropped first
const READ_STALE = 2 * READ_LIMIT + 5000; // a read still busy this long is given up (its two calls are capped)
const GRACE = 5000; // an exec still unsettled this long after its own timeout is given up here
const COLLECT_TIMEOUT = 60000, RECONSTRUCT_TIMEOUT = 600000, TOOL_TIMEOUT = 30000;
const ARG_MAX = 100 * 1024; // the summary passed to the local compare for a remote space (pa-costs refuses more)
const DAYS_SHOWN = 14, DAYS_MORE = 92, PROBLEMS_SHOWN = 20;
const DIMS = ["kind", "role", "program", "model", "machine"];
const ACTIVITY = { "cache-rebuild": "cache rebuild after idle", unattributed: "unattributed" };

const str = (v) => (v === null || v === undefined ? "" : String(v));
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const firstLine = (s) => str(s).trim().split("\n").find((l) => l.trim()) ?? "";
const NOT_FOUND = /not found|no such file|ENOENT|cannot (start|find)/i;
export const notInstalled = (machine) => (machine ? `pa-costs is not installed on ${machine}; run pa-sync --on ${machine}.` : "pa-costs is not installed here; run ./install.sh.");

// One pa-costs exec, on `machine` (null: here), given up GRACE after its own timeout. { r } or { error, missing }, or
// { busy: true } when one of its kind is still pending (t-053: nothing started; the caller says nothing).
// hcx: the task's context (CostsPane.ctx: the copy's Host, keyed per machine and command).
async function call(hcx, machine, args, timeout) {
  try {
    const r = await capped(hcx, () => run("pa-costs", args, { on: machine ?? null, timeout }), { key: `pa-costs:${machine ?? ""}:${args.slice(0, 2).join(" ")}`, limit: timeout + GRACE, what: `pa-costs ${args[0]}` });
    if (gaveUp(r) && r !== TIMED_OUT) return { busy: true, error: "skipped" };
    if (r === TIMED_OUT) return { error: `pa-costs ${args[0]} did not answer in ${Math.round((timeout + GRACE) / 1000)} s` };
    if (r.code === 127) return { error: notInstalled(machine), missing: true };
    return { r };
  } catch (e) {
    const why = String(e?.message ?? e);
    return missingTool(why, "pa-costs") ? { error: notInstalled(machine), missing: true } : { error: why }; // not an unreachable machine (t-028)
  }
}
const said = (r) => firstLine(r?.stderr || r?.stdout).slice(0, 300) || `pa-costs exited with ${r?.code}`;
const printed = (r) => { try { const v = JSON.parse(str(r?.stdout)); return v && typeof v === "object" && !Array.isArray(v) ? v : null; } catch { return null; } };

// Money in the summary's currency: Intl when it can, else "12.34 CUR"; null (nothing priced) is "—", never 0.
export function money(v, cur) {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  if (v > 0 && v < 0.005) return cur ? `<0.01 ${cur}` : "<0.01";
  if (cur) { try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur }).format(v); } catch {} }
  return cur ? `${v.toFixed(2)} ${cur}` : v.toFixed(2);
}
export function tokens(n) {
  const v = num(n), f = (x, s) => `${x >= 100 ? Math.round(x) : Math.round(x * 10) / 10}${s}`;
  return v >= 1e9 ? f(v / 1e9, "B") : v >= 1e6 ? f(v / 1e6, "M") : v >= 1e3 ? f(v / 1e3, "k") : String(Math.round(v));
}
export function ago(iso, now = Date.now()) {
  const t = Date.parse(str(iso));
  if (Number.isNaN(t)) return "at an unknown time";
  const m = Math.max(0, Math.round((now - t) / 60000));
  return m < 1 ? "just now" : m < 120 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
}
const clock = (iso) => { const d = new Date(str(iso)); return Number.isNaN(d.getTime()) ? str(iso) || "?" : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
// An entry's estimate: "12.34 est.", "12.34 est. + unpriced" (partial: a priced part and unpriced use), "— est." (no price).
const est = (e, cur) => (e?.money === null || e?.money === undefined ? "— est." : `${money(e.money, cur)} est.${num(e.unpricedTokens) > 0 ? " + unpriced" : ""}`);
const priceTag = (e) => (e?.money === null || e?.money === undefined ? (num(e?.tokens) > 0 ? "no price" : "") : num(e.unpricedTokens) > 0 ? "partly no price" : "");
// Claude Code's own figure (t-029 R7): its list-price estimate, always USD (never the summary's currency), shown beside
// est. and never added to it. Only a summary with totals.cc carries it; without that, nothing of it is drawn (AC7.5).
const ccOf = (s) => (s?.totals?.cc && typeof s.totals.cc === "object" && !Array.isArray(s.totals.cc) ? s.totals.cc : null);
const ccNum = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const ccMoney = (v) => `${money(v, null)} USD`;
// A summary's own entry list (a key that is not one is an empty list).
const list = (v) => (Array.isArray(v) ? v.filter((e) => e && typeof e === "object") : []);

// t-032 J1d: the facts a cost was priced under (provider · endpoint · region · account). factsText is the key that groups
// and compares them (a fact that is not a label is "?"); it is never shown. t-050: the view shows them in words.
const BY = ["providerBy", "endpointBy", "regionBy", "accountBy"];
const FACT = ["provider", "endpoint", "region", "account"];
const label = (v) => (typeof v === "string" && v ? v : null);
const factsOf = (e) => (Array.isArray(e?.scope) && e.scope.length === 4 ? e.scope : [e?.provider, e?.endpoint, e?.region, e?.account]);
const factsText = (facts) => facts.map((v) => label(v) ?? "?").join(" · ");
const either = (names, last = "or") => (names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} ${last} ${names[names.length - 1]}`);
// A scope in words (t-050): known facts named ("provider p", "region g"); unknown ones grouped by how they were found,
// after them: "no provider, endpoint or region recorded" (unknown, default, or not said), "region value refused",
// "region not recorded or refused" (mixed).
function scopeWords(facts, by = null) {
  const known = [], why = { none: [], refused: [], mixed: [] };
  facts.forEach((v, i) => {
    if (label(v)) { known.push(`${FACT[i]} ${v}`); return; }
    const b = by && typeof by === "object" ? by[BY[i]] : null;
    why[b === "refused" ? "refused" : b === "mixed" ? "mixed" : "none"].push(FACT[i]);
  });
  const unknown = [why.none.length ? `no ${either(why.none)} recorded` : "", why.refused.length ? `${either(why.refused, "and")} value refused` : "",
    why.mixed.length ? `${either(why.mixed, "and")} not recorded or refused` : ""].filter(Boolean);
  return { known, unknown };
}
const scopeText = (facts, by = null) => { const w = scopeWords(facts, by); return [...w.known, ...w.unknown].join(", "); };
// How a cost was priced, for each pricedAs (one, or a list) whose scope is not the facts themselves (a fact "?" priced
// from "*" counts as another scope): "the price file's default prices" (every fact "*"), else "the price file's prices
// for <its named facts>"; "" when every one is the facts. from: with the dates its rows hold from ("valid from d1 and d2").
function priceWords(facts, pricedAs, from = false) {
  const plain = factsText(facts);
  const as = (Array.isArray(pricedAs) ? pricedAs : [pricedAs]).filter((x) => x && typeof x === "object" && Array.isArray(x.scope) && x.scope.length === 4);
  const groups = new Map();
  for (const x of as) {
    if (x.scope.map((v) => label(v) ?? "*").join(" · ") === plain) continue;
    const named = x.scope.map((v, i) => (label(v) && v !== "*" ? `${FACT[i]} ${v}` : null)).filter(Boolean);
    const text = named.length ? `the price file's prices for ${named.join(", ")}` : "the price file's default prices";
    const dates = groups.get(text) ?? [];
    if (from && label(x.validFrom) && !dates.includes(x.validFrom)) dates.push(x.validFrom);
    groups.set(text, dates);
  }
  return [...groups].map(([text, dates]) => `${text}${dates.length ? ` valid from ${either(dates, "and")}` : ""}`).join("; ");
}
const pricedNote = (facts, pricedAs, from = false) => { const w = priceWords(facts, pricedAs, from); return w ? `priced from ${w}` : ""; };
// The summary has two or more scopes (then lines say theirs); its one account label (null: none, or two or more).
const tagged = (s) => num(s?.scopes) > 1 || list(s?.byScope).length > 1;
const accountsOf = (s) => list(s?.byAccount).filter((e) => label(e.key));
const oneAccount = (s) => { const a = accountsOf(s); return a.length === 1 ? a[0].key : null; };

// The figure on the tab, from the last summary read (none: no figure).
function tabFigure(s) {
  const tot = s?.totals;
  if (!tot || typeof tot !== "object") return null;
  const cur = s.prices?.currency ?? null;
  if (tot.money === null || tot.money === undefined) return num(tot.tokens?.all) > 0 ? `${tokens(tot.tokens.all)} tokens · no price` : null;
  return `${money(tot.money, cur)} est.${num(tot.unpricedTokens) > 0 ? " + unpriced" : ""}`;
}

export class CostsPane {
  constructor(shown = () => true, host = null) {
    this.host = host; // t-053: the copy's Host (host.js); null: each call as is
    this.copier = new Copier(); // the Copy of the totals, each section and the billed table (copy.js, t-026)
    this.shown = shown; // (space id) -> this view is on screen for that space now (main.js)
    this.spaces = new Map(); // space id -> its state (most recently shown last; at most CACHE_MAX)
    this.seq = 0;
  }

  // A task's context for its calls: background (a read or compare the view asked for itself) or a click's (waits for a
  // call of its kind still pending, never skipped).
  ctx(cx, click = false) { return this.host ? this.host.ctx(cx, { origin: "costs", background: !click, wait: click }) : cx; }
  task(cx, fn, what) { if (this.host) return this.host.spawn(cx, fn, { what }); cx.spawn(fn); return true; }

  of(id) {
    let st = this.spaces.get(id);
    if (st) { this.spaces.delete(id); this.spaces.set(id, st); return st; }
    st = { summary: null, readAt: 0, error: null, timedOut: false, missing: null, readFailed: null, busy: false, at: 0, seq: 0, stale: false,
      run: null, billing: { data: null, at: 0, busy: false, error: null }, open: {}, dim: "kind", moreDays: false, problemsOpen: false };
    this.spaces.set(id, st);
    while (this.spaces.size > CACHE_MAX) this.spaces.delete(this.spaces.keys().next().value);
    return st;
  }
  peek(id) { return this.spaces.get(id) ?? null; }

  tabLabel(spaceId) { const f = tabFigure(this.peek(spaceId)?.summary); return f ? `Costs · ${f}` : "Costs"; }

  // A read of the space's summary, unless one is in flight or one started under COSTS_EVERY ago (force: a click,
  // or the space was marked stale). Not shown now: nothing, the space is marked stale (read at once when shown).
  want(space, cx, force = false) {
    const st = this.of(space.id), now = Date.now();
    if (!this.shown(space.id)) { st.stale = true; return; }
    if (st.busy && now - st.at < READ_STALE) { if (force) st.stale = true; return; }
    // Inside COSTS_EVERY: no read; a billed compare refused while the view was left runs now (t-028).
    if (!force && !st.stale && st.at && now - st.at < COSTS_EVERY) { if (st.billing.due) this.wantBilling(space, cx, true); return; }
    const mine = ++this.seq;
    st.busy = true; st.at = now; st.seq = mine; st.stale = false;
    const started = this.task(cx, async (cx) => {
      let r;
      try { r = await loadCosts(space, this.ctx(cx, force), () => this.shown(space.id)); } catch (e) { r = { failed: String(e?.message ?? e) }; }
      if (st.seq !== mine) return; // a newer read of this space started (this one was given up)
      st.busy = false;
      if (r.busy) { cx.notify(); return; } // t-053: the last read is still pending: skipped, the kept copy stays as it is; the next 10 s read tries again
      if (r.left) { st.stale = true; cx.notify(); return; } // the view was left before the ls: the kept copy stays; read at once when shown
      if (r.timedOut || r.failed) { st.timedOut = true; st.readFailed = r.failed ?? null; } // the kept copy stays
      else if (r.error) { st.error = r.error; st.timedOut = false; st.readFailed = null; }
      else { st.summary = r.summary; st.missing = r.missing; st.error = null; st.timedOut = false; st.readFailed = null; st.readAt = Date.now(); }
      if (st.summary?.totals) this.wantBilling(space, cx);
      if (st.stale) this.want(space, cx); // asked for while this read ran (a Refresh ended): reread now (t-028), if shown
      cx.notify();
    }, "the costs read");
    if (!started) st.busy = false;
  }

  // From main.js's 1 s tick while this view is chosen (t-028): shown again with a reread or a billed compare that was
  // asked for while it was left: start it now, not at the next 10 s reload. Shown only; nothing else is read or run.
  due(space, cx) {
    const st = this.peek(space.id);
    if (!st || !this.shown(space.id)) return;
    if (st.stale && !st.busy) this.want(space, cx);
    else if (st.billing.due && !st.billing.busy && st.summary?.totals) this.wantBilling(space, cx, true);
  }

  // Refresh: `pa-costs collect` on the space's machine, one at a time, then one read.
  refresh(space, cx) { this.runTool(space, cx, "collect", ["collect", "--space", localId(space.id)], COLLECT_TIMEOUT, true); }
  // Reconstruct from session files: `pa-costs reconstruct` (it collects itself), then one read.
  reconstruct(space, cx) { this.runTool(space, cx, "reconstruct", ["reconstruct", "--space", localId(space.id)], RECONSTRUCT_TIMEOUT); }
  runTool(space, cx, what, args, timeout, withZone = false) {
    const st = this.of(space.id);
    if (st.run?.busy) return;
    st.run = { what, busy: true, error: null };
    cx.spawn(async (cx) => {
      const hcx = this.ctx(cx, true);
      let { r, error } = await call(hcx, space.machine, withZone ? [...args, ...zoneArgs(Date.now())] : args, timeout);
      if (withZone && !error && oldTool(r)) ({ r, error } = await call(hcx, space.machine, args, timeout)); // a pa-costs from before t-036
      st.run = { what, busy: false, error: error ?? (r.code !== 0 ? said(r) : null) };
      this.want(space, cx, true);
      cx.notify();
    });
  }

  // The billed comparison (R14). Local space: `billed compare --json --space`. Remote space: the local compare with
  // its summary trimmed to what the comparison reads; only when this machine has billed rows for its account, the
  // remote's `report --account-wide` and the compare again with it ("all spaces here" are that machine's spaces).
  wantBilling(space, cx, force = false) {
    const st = this.of(space.id), b = st.billing, now = Date.now(), s = st.summary;
    // Not shown: none; it is due, and runs as soon as the view is shown again (want(), due(); t-028).
    if (!this.shown(space.id)) { b.due = true; if (force) b.at = 0; return; }
    if (b.busy || (!force && b.at && now - b.at < BILLING_EVERY) || !s) return;
    const mine = (b.seq ?? 0) + 1; // the newest compare: an older one that ends later commits nothing (Turn off, t-028)
    b.busy = true; b.at = now; b.seq = mine; b.due = false;
    const started = this.task(cx, async (cx) => {
      const hcx = this.ctx(cx, force);
      let skipped = false; // t-053: a call of its kind still pending: this compare ends, says nothing, and the next one asks again
      // Asked before each step after the first: a newer compare started -> stop; the view was left -> stop, and the
      // next shown read runs the whole chain.
      const left = () => { if (b.seq !== mine) return true; if (this.shown(space.id)) return false; b.busy = false; b.at = 0; st.stale = true; cx.notify(); return true; };
      const compare = async (extra) => {
        const { r, error, busy } = await call(hcx, null, ["billed", "compare", "--json", ...extra], TOOL_TIMEOUT);
        if (busy) { skipped = true; return { error: "skipped" }; }
        if (error) return { error };
        return printed(r) ?? { error: said(r) };
      };
      let got;
      if (!space.machine) got = await compare(["--space", localId(space.id)]);
      else {
        const trimmed = JSON.stringify({ v: s.v, space: s.space, updated: s.updated, account: s.account, prices: s.prices, byDayModel: s.byDayModel });
        if (trimmed.length > ARG_MAX) got = { error: "the space's summary is over 100 KB, too large to compare here" };
        else {
          got = await compare(["--summary-json", trimmed]);
          if (!got.error && num(got.rows) > 0) {
            if (left()) return;
            const { r, error, busy } = await call(hcx, space.machine, ["report", "--account-wide"], TOOL_TIMEOUT);
            if (busy) skipped = true;
            const here = error ? null : r.code === 0 ? str(r.stdout).trim() : null;
            if (!here || here.length > ARG_MAX) got = { ...got, comparison: null, hereError: error ?? (here ? "over 100 KB" : said(r)) };
            else { if (left()) return; got = await compare(["--summary-json", trimmed, "--here-json", here]); }
          }
        }
      }
      if (b.seq !== mine) return; // a newer compare started (Turn off): this result is older than its
      b.busy = false;
      if (skipped) { b.at = 0; cx.notify(); return; }
      if (got.error) b.error = got.error; else { b.data = got; b.error = null; }
      cx.notify();
    }, "the billed compare");
    if (!started) b.busy = false;
  }

  // Turn off: `pa-costs billed disable`, here (the billing query runs only on this machine), then the status again.
  turnOff(space, cx) {
    const b = this.of(space.id).billing;
    if (b.off) return;
    b.off = true;
    cx.spawn(async (cx) => {
      const { r, error } = await call(this.ctx(cx, true), null, ["billed", "disable"], TOOL_TIMEOUT);
      b.off = false;
      b.offError = error ?? (r.code !== 0 ? said(r) : null);
      b.busy = false;
      this.wantBilling(space, cx, true);
      cx.notify();
    });
  }

  render(t, space, collector) {
    const st = this.of(space.id), s = st.summary, cur = s?.prices?.currency ?? null;
    // Selectable (t-026): every text row with no click handler, by a per-render counter (the same while the rows are).
    let n = 0;
    const line = (text, color, id) => { const el = div().px(4).text_size(11.5).text_color(color ?? t.muted); return id ? el.id(id).child(text) : el.child(sel(`costs-sel-${n++}`, text)); };
    const button = (id, label, click) => div().id(id).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(t.border).text_size(11.5)
      .text_color(t.foreground).cursor_pointer().hover((x) => x.bg(t.hover)).on_click(click).child(label);
    const refresh = () => button("costs-refresh", st.run?.busy && st.run.what === "collect" ? "Refreshing…" : "Refresh", (_e, cx) => { this.refresh(space, cx); cx.notify(); });
    const reconstruct = () => button("costs-reconstruct", st.run?.busy && st.run.what === "reconstruct" ? "Reconstructing…" : "Reconstruct from session files", (_e, cx) => { this.reconstruct(space, cx); cx.notify(); });
    const box = v_flex().id("pa-costs").flex_1().min_h(0).p(8).gap(6).overflow_y_scrollbar();
    const runLine = st.run?.error ? line(`${st.run.what === "reconstruct" ? "Reconstruct" : "Refresh"} failed: ${st.run.error}`, t.blocked) : null;
    // The timer's collector (shared "project-agent.costs"): its last run failed.
    const collectorLine = collector && collector.ok === false
      ? line(collector.missing ? notInstalled(space.machine ?? null) : `Collector: last run ${clock(collector.at)} failed: ${firstLine(collector.error) || `exit ${collector.code}`}`, t.blocked) : null;
    const extras = (el) => el.when(Boolean(runLine), (e) => e.child(runLine)).when(Boolean(collectorLine), (e) => e.child(collectorLine));
    if (!s) {
      if (st.timedOut) return extras(box.child(line(st.readFailed && NOT_FOUND.test(st.readFailed) ? st.readFailed : "Costs did not answer. Trying again shortly.", t.blocked)).child(h_flex().child(refresh())));
      if (st.error) return extras(box.child(line(`costs/summary.json: ${st.error}`, t.blocked)).child(h_flex().child(refresh())));
      if (st.missing === "none") return extras(box.child(line("No cost records yet. Spend is recorded for sessions started from now on.")).child(h_flex().child(reconstruct())));
      if (st.missing === "pending") return extras(box.child(line("Collecting… (first run)")).child(h_flex().child(refresh())));
      return extras(box.child(line("Loading costs…", t.dim)));
    }
    const tot = s.totals && typeof s.totals === "object" ? s.totals : null;
    const problems = Array.isArray(s.problems) ? s.problems.filter((p) => typeof p === "string") : [];
    const priced = Boolean(s.prices?.source || s.prices?.currency);
    const said = `${priced ? `Estimated from ${str(s.prices?.source) || "the profile's"} prices as of ${str(s.prices?.asOf) || "?"} (${str(cur) || "?"})`
      : `No price table in profile ${str(s.prices?.profile) || "?"}: tokens only.`} · updated ${ago(s.updated)}`;
    const totals = [said]; // the header and the tiles (filled by tiles() below), for Copy
    const header = h_flex().gap(8).items_center()
      .child(div().flex_1().min_w(0).text_size(11.5).text_color(t.muted).child(sel("costs-sel-head", said)))
      .child(refresh())
      .when(Boolean(tot), (e) => e.child(this.copier.button(t, "costs-copy-totals", () => totals.join("\n"))));
    box.child(header);
    if (st.timedOut) box.child(line(`summary.json did not answer; showing the copy read at ${clock(new Date(st.readAt).toISOString())}.`, t.blocked));
    if (st.error) box.child(line(`costs/summary.json: ${st.error}; showing the copy read at ${clock(new Date(st.readAt).toISOString())}.`, t.blocked));
    extras(box);
    if (problems.length) {
      box.child(line(`⚠ ${problems.length} ${problems.length === 1 ? "problem" : "problems"} reading the records ${st.problemsOpen ? "▾" : "▸"}`, t.blocked, "costs-problems").cursor_pointer()
        .on_click((_e, cx) => { st.problemsOpen = !st.problemsOpen; cx.notify(); }));
      if (st.problemsOpen) for (const p of problems.slice(0, PROBLEMS_SHOWN)) box.child(line(p.slice(0, 300), t.dim).pl(16));
    }
    if (s.truncated) box.child(line("Lists shortened to fit; pa-costs report shows all.", t.dim));
    if (!tot) { // summary.json without totals: a collect refused or failed before anything was collected
      const why = str(s.collect?.why) || problems[0] || "";
      return box.child(line(`Nothing collected yet.${why ? ` ${why}` : ""}`)).child(h_flex().gap(6).child(reconstruct()));
    }
    const missingModels = Array.isArray(s.prices?.missing) ? s.prices.missing.map(str) : [];
    if (tot.money === null && num(tot.tokens?.all) > 0 && priced) box.child(line(`No price for ${missingModels.join(", ") || "these models"}: shown as tokens only.`, t.blocked));
    else if (tot.money !== null && num(tot.unpricedTokens) > 0) box.child(line(`${tokens(tot.unpricedTokens)} tokens of ${missingModels.join(", ") || "some models"} have no price: shown as tokens only.`, t.blocked));
    box.child(this.tiles(t, s, tot, cur, priced, totals));
    if (num(tot.reconstructedMoney) > 0) box.child(line(`◌ Includes ${money(tot.reconstructedMoney, cur)} est. reconstructed (best effort) from session files still on disk.`, t.dim));
    for (const text of this.scopeNote(s)) { totals.push(text); box.child(line(text, t.dim)); }
    for (const text of this.ccNotes(s)) { totals.push(text); box.child(line(text, t.dim)); }
    box.child(this.sections(t, space, st, s, cur));
    box.child(this.billed(t, space, st, s, cur));
    return box;
  }

  tiles(t, s, tot, cur, priced, lines = []) {
    const hours = Array.isArray(s.hours) ? s.hours.filter((h) => Array.isArray(h) && h[1] && typeof h[1] === "object") : [];
    const now = Date.now(), today = new Date(now), localToday = localDate(now), z = summaryZone(s, now);
    const dayList = list(s.byDay).filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(str(d.key)));
    // Today: the summary's local today when its days are this machine's (exact); else the local day of its UTC hours.
    const mine = z.local ? dayList.filter((d) => d.key === localToday)
      : hours.filter(([k]) => { const ms = Date.parse(`${k}:00:00Z`); return !Number.isNaN(ms) && localDate(ms) === localToday; }).map(([, v]) => v);
    const sum = (entries) => {
      const pr = entries.filter((e) => e.money !== null && e.money !== undefined);
      const unpriced = entries.reduce((a, e) => a + (e.money === null || e.money === undefined ? num(e.tokens) : num(e.unpricedTokens)), 0);
      return { money: pr.length ? pr.reduce((a, e) => a + num(e.money), 0) : entries.length || !priced ? null : 0, unpricedTokens: pr.length ? unpriced : 0, tokens: entries.reduce((a, e) => a + num(e.tokens), 0) };
    };
    const day0 = z.local ? daysBefore(localToday, 6) : new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - 6)).toISOString().slice(0, 10);
    const week = sum(dayList.filter((d) => d.key >= day0));
    // alt: the figure instead of est. (Claude Code's, where there is no est.); cc: Claude Code's line under it.
    const tile = (title, e, small, alt = null, cc = null) => {
      const figure = alt ?? (priced ? est(e, cur) : `${tokens(e.tokens)} tokens`);
      lines.push(`${title}: ${figure} · ${small}`);
      const i = lines.length; // its row in the copy text: a stable id
      if (cc) lines.push(cc);
      return v_flex().flex_1().min_w(0).p(8).gap(2).rounded(7).border(1).border_color(t.border).bg(t.surface)
        .child(div().text_size(11).text_color(t.dim).child(sel(`costs-sel-tile-${i}-title`, title)))
        .child(div().text_size(15).font_semibold().text_color(t.foreground).child(sel(`costs-sel-tile-${i}-figure`, figure)))
        .child(div().text_size(10.5).text_color(t.dim).child(sel(`costs-sel-tile-${i}-small`, small)))
        .when(Boolean(cc), (x) => x.child(div().text_size(10.5).text_color(t.muted).child(sel(`costs-sel-tile-${i}-cc`, cc))));
    };
    const all = num(tot.tokens?.all), cr = num(tot.tokens?.cr);
    // Claude Code's figure in the Total tile (AC7.1): under est.; where there is no est., the tile's money, never "est.".
    const cc = ccOf(s), ccM = ccNum(cc?.money), count = cc ? `${num(cc.sessions)} of ${num(cc.claudeSessions)} Claude sessions` : "";
    const noEst = tot.money === null || tot.money === undefined;
    const acct = oneAccount(s); // R-A3 (t-032 J1d): one account: its label beside Total; two or more: the Accounts section
    const ccAlt = cc && ccM !== null && noEst ? `Claude Code: ${ccMoney(ccM)} (list price)` : null;
    const ccLine = !cc ? null : ccM === null ? `Claude Code: no figure · ${count}` : noEst ? `Claude Code's figure: ${count}` : `Claude Code: ${ccMoney(ccM)} (list price) · ${count}`;
    return h_flex().gap(6)
      .child(tile("Total", { money: tot.money, unpricedTokens: tot.unpricedTokens, tokens: all }, `${tokens(all)} tokens${all ? ` · ${Math.round((cr / all) * 100)}% cache read` : ""}${acct ? ` · account ${acct}` : ""}`, ccAlt, ccLine))
      .child(tile("Today", sum(mine), `${tokens(sum(mine).tokens)} tokens`))
      .child(tile(z.local ? "Last 7 days" : `Last 7 days (${z.label})`, week, `${tokens(week.tokens)} tokens`));
  }

  // One scope priced from another (A-V3 "always a priced as note", t-032 J1d): said once here, since no line names a scope.
  scopeNote(s) {
    const sc = list(s.byScope);
    if (tagged(s) || sc.length !== 1) return [];
    const f = factsOf(sc[0]), how = priceWords(f, sc[0].pricedAs, true), w = scopeWords(f, sc[0].by);
    // t-050: "All costs priced from <prices> (<what was not recorded>) · <the facts known>".
    return how ? [`All costs priced from ${how}${w.unknown.length ? ` (${w.unknown.join("; ")})` : ""}${w.known.length ? ` · ${w.known.join(", ")}` : ""}`] : [];
  }

  // The notes under the tiles where the summary has Claude Code's figure: the legend of "CC", how many sessions have it
  // (AC7.3) and the figures that match no session (AC7.4).
  ccNotes(s) {
    const cc = ccOf(s);
    if (!cc) return [];
    const out = [];
    if (ccNum(cc.money) !== null) out.push("CC = Claude Code's own list-price estimate (USD), per session; not the bill");
    if (num(cc.sessions) < num(cc.claudeSessions)) {
      const days = list(s.sessions).filter((x) => ccNum(x.cc) !== null && /^\d{4}-\d{2}-\d{2}/.test(str(x.ccAt))).map((x) => (Number.isNaN(Date.parse(x.ccAt)) ? x.ccAt.slice(0, 10) : localDate(Date.parse(x.ccAt)))).sort();
      const covers = days.length ? `covers sessions started from ${days[0]}` : num(cc.sessions) ? `covers ${num(cc.sessions)} of ${num(cc.claudeSessions)} Claude sessions` : "covers none of these sessions yet";
      out.push(`Claude Code's figure ${covers}; older, Codex and killed-at-the-end sessions have none or a partial one.`);
    }
    const um = cc.unmatched && typeof cc.unmatched === "object" ? cc.unmatched : {};
    if (num(um.sessions) > 0) out.push(`Claude Code figures matching no session (not in the totals): ${num(um.sessions)}, ${ccNum(um.money) === null ? "no figure" : `${ccMoney(um.money)} list price`}`);
    return out;
  }

  sections(t, space, st, s, cur) {
    const out = v_flex().gap(6);
    const totalMoney = s.totals?.money;
    const ccOn = Boolean(ccOf(s)); // Claude Code's figure on session lines and task rows (AC7.2), only with totals.cc
    // Each section's text as drawn, for its Copy: the head, its rows, and the open drill rows (indented).
    const blocks = {}; let lines = [];
    const begin = (key, text) => { lines = blocks[key] = [text]; };
    const copy = (key) => this.copier.button(t, `costs-copy-${key}`, () => blocks[key].join("\n"));
    const head = (text) => div().pt(4).px(4).text_size(11.5).font_semibold().text_color(t.foreground).child(text);
    const titled = (key, text) => { begin(key, text); return h_flex().gap(6).items_center().child(head(text)).child(copy(key)); };
    let n = 0; // the drill rows (children of a row's v_flex, not of its clickable h_flex) are selectable
    const sub = (text, color) => { lines.push(`  ${text}`); return div().pl(20).text_size(11).text_color(color ?? t.muted).child(sel(`costs-sel-sub-${n++}`, text)); };
    const row = (section, i, entry, label, extra, drill) => {
      const key = str(entry.key), open = st.open[section] === key;
      const share = totalMoney && entry.money !== null && entry.money !== undefined && !num(entry.unpricedTokens) ? ` · ${Math.round((num(entry.money) / totalMoney) * 100)}%` : "";
      const tag = priceTag(entry), mark = num(entry.reconstructed) > 0 ? " ◌" : "";
      const ccCell = ccOn && section === "task" && ccNum(entry.cc) !== null ? `CC ${money(entry.cc, null)} (${num(entry.ccSessions)} of ${num(entry.ccOf)} ${num(entry.ccOf) === 1 ? "session" : "sessions"})` : "";
      lines.push([`${label}${mark}`, extra, tag, `${tokens(entry.tokens)} tokens`, `${est(entry, cur)}${share}`, ccCell].filter(Boolean).join(" · "));
      const el = v_flex().child(h_flex().id(`costs-${section}-${i}`).gap(8).px(4).items_center().cursor_pointer().hover((x) => x.bg(t.hover))
        .on_click((_e, cx) => { st.open[section] = open ? null : key; cx.notify(); })
        .child(div().flex_none().text_size(11).text_color(t.dim).child(drill ? (open ? "▾" : "▸") : " "))
        .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.foreground).child(`${label}${mark}`))
        .when(Boolean(extra), (x) => x.child(div().flex_none().text_size(10.5).text_color(t.dim).child(extra)))
        .when(Boolean(tag), (x) => x.child(div().flex_none().text_size(10.5).text_color(t.blocked).child(tag)))
        .child(div().flex_none().text_size(11).text_color(t.dim).child(`${tokens(entry.tokens)} tokens`))
        .child(div().flex_none().text_size(12).text_color(t.foreground).child(`${est(entry, cur)}${share}`))
        .when(Boolean(ccCell), (x) => x.child(div().flex_none().text_size(11).text_color(t.muted).child(ccCell))));
      return open && drill ? el.children(drill()) : el;
    };
    // With two or more scopes (t-032 J1d) a session line says its scope after the model, and how it was priced where that
    // is another scope (or not at all); a session whose rows span scopes shows its largest and says so.
    const tags = tagged(s);
    const scopeOf = (x) => (tags ? ` (${scopeText(factsOf(x))})` : "");
    const noteOf = (x) => {
      if (!tags) return "";
      const n = [pricedNote(factsOf(x), x.pricedAs) || priceTag(x), num(x.scopes) > 1 ? `rows in ${num(x.scopes)} scopes (the largest shown)` : ""].filter(Boolean).join(" · ");
      return n ? ` · ${n}` : "";
    };
    const sessionsOf = (pred) => list(s.sessions).filter(pred).map((x) => sub(`${x.source === "reconstructed" ? "◌ " : ""}${str(x.key)} · ${[x.kind, x.role, x.program, x.model].filter(Boolean).map(str).join(" · ")}${scopeOf(x)} · ${clock(x.from)} – ${clock(x.to)} · ${est(x, cur)}${ccOn && ccNum(x.cc) !== null ? ` · CC ${money(x.cc, null)}` : ""}${noteOf(x)} · ${str(x.state)}${x.source ? ` · ${str(x.source)}` : ""}`));
    // The model lines of a scope or of a model (t-032 J1d): byDayModel (UTC days, from its oldest) summed per model and scope.
    const dayModel = list(s.byDayModel);
    const dmDays = dayModel.map((e) => str(e.day)).filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d)).sort();
    const dmFrom = dmDays.length ? ` (from ${dmDays[0]})` : "";
    const modelLines = (pred, withScope) => {
      const groups = new Map();
      for (const e of dayModel.filter(pred)) {
        const f = factsOf(e), key = `${str(e.model)}\0${factsText(f)}`;
        const g = groups.get(key) ?? { model: str(e.model), facts: f, tokens: 0, money: null, unpricedTokens: 0, pricedAs: [] };
        const tk = e.tokens && typeof e.tokens === "object" ? Object.values(e.tokens).reduce((a, x) => a + num(x), 0) : 0;
        g.tokens += tk;
        if (e.money === null || e.money === undefined) g.unpricedTokens += tk; else { g.money = (g.money ?? 0) + num(e.money); g.unpricedTokens += num(e.unpricedTokens); }
        if (e.pricedAs) g.pricedAs.push(e.pricedAs);
        groups.set(key, g);
      }
      return [...groups.values()].map((g) => {
        const e = { money: g.money, unpricedTokens: g.money === null ? 0 : g.unpricedTokens, tokens: g.tokens };
        return sub([`${g.model}${withScope ? ` (${scopeText(g.facts)})` : ""}`, `${tokens(g.tokens)} tokens`, est(e, cur), pricedNote(g.facts, g.pricedAs), priceTag(e)].filter(Boolean).join(" · "));
      });
    };
    const entries = (items) => items.map((e) => sub(`${str(e.key)} · ${tokens(e.tokens)} tokens · ${est(e, cur)}${priceTag(e) ? ` · ${priceTag(e)}` : ""}`));
    // By activity: tasks of an activity; unattributed: its reasons; cache rebuild: the passes with a rebuild and its tasks.
    const acts = list(s.byActivity);
    out.child(titled("activity", "By activity"));
    acts.forEach((a, i) => out.child(row("activity", i, a, ACTIVITY[a.key] ?? str(a.key).replace(/^pass:/, "pass: "), null, () => [
      ...(a.key === "unattributed" ? [sub("Reasons:", t.dim), ...entries(list(a.reasons))] : []),
      ...(a.key === "cache-rebuild" ? [sub("Passes with a cache rebuild:", t.dim), ...list(s.passes).filter((p) => p.rebuild).map((p) => sub(`pass ${str(p.pass)} · ${str(p.trigger)} · ${clock(p.from)} · ${est(p, cur)} · cache rebuild`))] : []),
      sub("Tasks:", t.dim), ...entries(list(a.tasks)),
    ])));
    // By task: its phases and its sessions.
    out.child(titled("task", "By task"));
    list(s.byTask).forEach((x, i) => out.child(row("task", i, x, `${str(x.key)}${x.key === "(other tasks)" && x.tasks ? ` (${x.tasks} tasks)` : ""}${x.state === "deleted" ? " (deleted)" : ""}${x.title ? ` ${str(x.title)}` : ""}${x.split ? " ≈" : ""}`,
      x.state && x.state !== "deleted" ? str(x.state) : null, () => [
        ...(x.split ? [sub("≈ shared passes split by journal entries", t.dim)] : []),
        sub("Phases:", t.dim), ...entries(list(x.phases)),
        sub("Sessions:", t.dim), ...sessionsOf((ss) => (Array.isArray(x.sessions) ? x.sessions : []).includes(ss.key)),
      ])));
    // By session type: a selector; the machine is shown as Herdr's machine name, never a host name.
    begin("kind", `By session type: ${st.dim}`);
    out.child(h_flex().gap(4).items_center().child(head("By session type")).child(copy("kind")).children(DIMS.map((d) => div().id(`costs-dim-${d}`).px(6).h(20).flex().items_center().rounded(5).text_size(11)
      .bg(st.dim === d ? t.selected : t.chrome).text_color(st.dim === d ? t.foreground : t.muted).cursor_pointer().on_click((_e, cx) => { st.dim = d; st.open.kind = null; cx.notify(); }).child(d))));
    const dimRows = st.dim === "machine" ? [this.machineRow(s, space)] : list(s.byKind?.[st.dim]);
    dimRows.forEach((x, i) => out.child(row("kind", i, x, str(x.key), null, () => [
      ...(tags && st.dim === "model" ? [sub(`Scopes${dmFrom}:`, t.dim), ...modelLines((e) => str(e.model) === str(x.key), true)] : []),
      ...sessionsOf((ss) => st.dim === "machine" || str(ss[st.dim] ?? "(none)") === str(x.key))])));
    // Accounts (R-A3, t-032 J1d): two or more account labels; each with its sessions.
    const accts = accountsOf(s);
    if (accts.length > 1) {
      out.child(titled("account", "Accounts"));
      accts.forEach((x, i) => out.child(row("account", i, x, str(x.key), null, () => [sub("Sessions:", t.dim), ...sessionsOf((ss) => str(ss.account) === str(x.key))])));
    }
    // By scope (A-V3): two or more scopes; each says how it was priced where that is another scope; its models and sessions.
    if (tags) {
      out.child(titled("scope", "By scope (provider · endpoint · region · account)"));
      list(s.byScope).forEach((x, i) => {
        const f = factsOf(x), same = (e) => factsText(factsOf(e)) === factsText(f);
        out.child(row("scope", i, x, scopeText(f, x.by), pricedNote(f, x.pricedAs, true) || null, () => [
          sub(`Models${dmFrom}:`, t.dim), ...modelLines(same, false), sub("Sessions:", t.dim), ...sessionsOf(same)]));
      });
    }
    // By day: newest first; Show more reveals up to 92. A day pa-costs cut in UTC (older than the zone table) says (UTC).
    const days = list(s.byDay);
    const z = summaryZone(s, Date.now());
    out.child(titled("day", z.local ? "By day" : `By day (${z.label})`));
    days.slice(0, st.moreDays ? DAYS_MORE : DAYS_SHOWN).forEach((d, i) => out.child(row("day", i, d, d.key === "older" ? `older (${num(d.days)} days)` : `${str(d.key)}${d.utc ? " (UTC)" : ""}`, null,
      // Each activity with its tokens and est., marked as the rows are (t-028; activities from pa-costs of t-028 on);
      // an older summary's byActivity (priced money only) as before.
      () => (Array.isArray(d.activities) ? list(d.activities).map((a) => sub(`${ACTIVITY[a.key] ?? str(a.key)} · ${tokens(a.tokens)} tokens · ${est(a, cur)}${priceTag(a) ? ` · ${priceTag(a)}` : ""}`))
        : Object.entries(d.byActivity && typeof d.byActivity === "object" ? d.byActivity : {}).map(([k, v]) => sub(`${ACTIVITY[k] ?? k} · ${money(v, cur)} est.`))))));
    if (days.length > DAYS_SHOWN && !st.moreDays) out.child(div().id("costs-days-more").px(4).text_size(11.5).text_color(t.accent).cursor_pointer().on_click((_e, cx) => { st.moreDays = true; cx.notify(); }).child("Show more"));
    return out;
  }

  // All of the space's machine in one row (the summary's keys are host names, never shown).
  machineRow(s, space) {
    const rows = list(s.byKind?.machine);
    const pr = rows.filter((e) => e.money !== null && e.money !== undefined);
    return { key: space.machine ?? "this computer", tokens: rows.reduce((a, e) => a + num(e.tokens), 0), money: pr.length ? pr.reduce((a, e) => a + num(e.money), 0) : null,
      unpricedTokens: pr.length ? rows.reduce((a, e) => a + (e.money === null || e.money === undefined ? num(e.tokens) : num(e.unpricedTokens)), 0) : 0,
      reconstructed: rows.reduce((a, e) => a + num(e.reconstructed), 0) };
  }

  // Billed amounts beside the estimates (R14). Every billed figure says account-wide; the query's status and Turn off.
  billed(t, space, st, s, cur) {
    const b = st.billing, d = b.data, box = v_flex().id("costs-billed").gap(3).pt(6);
    const lines = []; // what the block draws, for its Copy (beside the head; there is none without billed rows)
    let n = 0; // selectable rows (t-026), by a per-render counter
    const line = (text, color) => { lines.push(String(text)); return div().px(4).text_size(11.5).text_color(color ?? t.muted).child(sel(`costs-sel-billed-${n++}`, text)); };
    const status = d?.status, conf = status?.config, on = Boolean(conf?.enabled), qs = status?.state && typeof status.state === "object" ? status.state : {};
    if (!d) return b.error ? box.child(line(`Billed amounts could not be read (${b.error})`, t.blocked)) : box.child(line(b.busy ? "Billed amounts: reading…" : "Billed amounts: not read yet.", t.dim));
    if (b.error) box.child(line(`Billed amounts could not be read (${b.error}); showing the copy read earlier.`, t.blocked));
    if (b.offError) box.child(line(`Turn off failed: ${b.offError}`, t.blocked));
    const fee = typeof status?.feePerRequest === "number" ? `, stated fee ≈ ${status.feePerRequest} ${str(status.feeCurrency)} each` : "";
    const statusRow = () => {
      const last = qs.lastRun ? `last run ${clock(qs.lastRun)} (${str(qs.result)}, ${num(qs.requests)} requests)` : "not run yet";
      const said = `Billing query: on · ${last} · next after ${clock(qs.nextAfter ?? new Date(Date.now()).toISOString())} · cap 1/day (≤ ${num(status?.maxPages) || 3} requests${fee})`;
      lines.push(said);
      return h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).px(4).text_size(11).text_color(t.dim).child(sel("costs-sel-billing-status", said)))
        .child(div().id("costs-billing-off").flex_none().px(8).h(22).flex().items_center().rounded(6).border(1).border_color(t.border).text_size(11).text_color(t.foreground)
          .cursor_pointer().hover((x) => x.bg(t.hover)).on_click((_e, cx) => { this.turnOff(space, cx); cx.notify(); }).child(b.off ? "Turning off…" : "Turn off"));
    };
    const err = str(qs.lastError);
    if (on && qs.result === "failed" && err.startsWith("batch rejected")) box.child(line(`The last query's rows were rejected; nothing was added: ${err.replace(/^batch rejected:?\s*/, "")}.`, t.blocked));
    else if (on && qs.result === "refused") box.child(line(`Billing query refused: ${err}`, t.blocked));
    else if (on && qs.result === "failed" && err) box.child(line(`Billing query failed: ${err}; next try after ${clock(qs.nextAfter)}`, t.blocked));
    const c = d.comparison;
    if (!num(d.rows)) {
      if (!on) return box.child(line("Billed amounts: none. The billing query is off; the person turns it on in their own terminal (pa-costs billed enable, see README)."));
      return box.child(statusRow()).child(line(`No billed rows for account ${str(c?.account ?? s.account)} yet.`));
    }
    const billedHead = "Billed (account-wide: includes use outside this space and outside Herdr)";
    lines.length = 0; lines.push(billedHead); // the Copy starts at the head (a refused or failed query's line above it is dropped)
    box.child(h_flex().gap(6).items_center().child(div().flex_1().min_w(0).px(4).text_size(11.5).font_semibold().text_color(t.foreground).child(sel("costs-sel-billed-head", billedHead)))
      .child(this.copier.button(t, "costs-copy-billed", () => lines.join("\n"))));
    if (d.hereError) box.child(line(`All spaces on ${str(space.machine)} could not be read (${str(d.hereError)}); the comparison is not shown.`, t.blocked));
    if (c && c.covered) {
      const cov = c.covered, ccur = c.currency ?? cur;
      box.child(line(`Covered ${str(cov.from)} – ${str(cov.to)} (${num(cov.days)} of ${num(cov.of)} days${Array.isArray(cov.missing) && cov.missing.length ? `; not billed: ${cov.missing.map(str).join(", ")}` : ""}) · source ${(Array.isArray(c.sources) ? c.sources : []).map(str).join(", ")}, fetched ${clock(c.fetchedAt)} (billing days are UTC days)`, t.dim));
      const missing = Array.isArray(s.prices?.missing) ? s.prices.missing.map(str) : [];
      const cell = (v, partial) => (v === null || v === undefined ? "— est. (no price)" : `${money(v, ccur)} est.${partial ? " + unpriced" : ""}`);
      const fig = (cols) => { lines.push(cols.map(String).join(" | ").replace(/^ \| /, "")); return figRow(cols); };
      const figRow = (cols) => { const r = n++; return h_flex().gap(8).px(4).children(cols.map((x, i) => div().when(i === 0, (e) => e.flex_1().min_w(0).truncate()).when(i > 0, (e) => e.flex_none()).text_size(11.5).text_color(t.foreground).child(sel(`costs-sel-fig-${r}-${i}`, x)))); };
      box.child(fig(["", "this space", "all spaces here", "billed"]));
      for (const m of list(c.models)) {
        const f = m.factor ?? {};
        const fac = f.kind === "price" ? `price factor ${f.value} (from quantities)` : f.kind === "scope" ? `scope ratio ${f.value} (not applied)${num(f.value) < 1 ? " below 1.0: the table over-prices" : ""}` : "";
        box.child(fig([str(m.model), cell(m.thisSpace, m.thisSpacePartial), cell(m.allSpaces, m.allSpacesPartial), `${money(m.billed, ccur)} billed · account-wide`]))
          .when(Boolean(fac) || m.notFinal || missing.includes(str(m.model)), (e) => {
            const note = [fac, f.partial ? "(from a partial estimate)" : "", f.few ? "(few days)" : "", m.notFinal ? "(not final)" : "", missing.includes(str(m.model)) ? "(no price for part of it)" : ""].filter(Boolean).join(" ");
            lines.push(`  ${note}`);
            return e.child(div().pl(20).text_size(10.5).text_color(f.few ? t.dim : t.muted).child(sel(`costs-sel-billed-${n++}`, note)));
          });
      }
      if (num(c.other)) box.child(fig(["other / unmapped labels", "", "", `${money(c.other, ccur)} billed · account-wide`]));
      const tt = c.total ?? {};
      box.child(fig(["total", cell(tt.thisSpace, tt.thisSpacePartial), cell(tt.allSpaces, tt.allSpacesPartial), `${money(tt.billed, ccur)} billed · account-wide`]));
      box.child(line(`calibrated (est. × price factor, where one applies): this space ${c.calibrated === null || c.calibrated === undefined ? "— (no priced estimate)" : `${money(c.calibrated, ccur)}${c.calibratedPartial ? " + unpriced" : ""}`}`));
      if (Array.isArray(c.noPrice) && c.noPrice.length) box.child(line(`No price for ${c.noPrice.map(str).join(", ")}: its estimate is the priced part only.`, t.dim));
    } else if (c && !d.hereError) box.child(line(c.currency ? `No billed rows in ${str(c.currency)} for account ${str(c.account)}.` : `No price table, so no currency to compare: billed rows for account ${str(c.account)} are not compared.`));
    for (const x of list(c?.separate)) box.child(line(`In ${str(x.currency)} (not compared): ${money(x.amount, x.currency)} billed · account-wide, ${num(x.rows)} row(s)`));
    if (on) box.child(statusRow());
    return box;
  }
}
