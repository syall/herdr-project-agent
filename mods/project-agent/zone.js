// The person's timezone (t-036): the one of the machine this panel runs on, whichever machine wrote the data.
// Data stays UTC everywhere; pa-costs cuts the Costs view's days by the table the panel passes to its collect:
//   --zone-offsets "0:<min>,<epoch>:<min>,..."  minutes east of UTC in force from each epoch on (the first from 0),
//                                               one entry per change in now - 100 d .. now + 35 d (daylight saving in it)
//   --zone-name <name>                          the IANA name when the runtime has one, else UTC±hh:mm
// The summary says which zone its days are in ("days"); the view draws them as local days only when that is this
// panel's zone, else it names the zone beside them.

const NAME = /^[A-Za-z0-9_+\-/:.]{1,64}$/;
const BACK = 100 * 86400000, AHEAD = 35 * 86400000, STEP = 6 * 3600000; // changes are months apart

// offsetAt(ms): minutes east of UTC here at that time; name(): the zone's name or null. Tests replace both.
export const zone = {
  offsetAt: (ms) => -new Date(ms).getTimezoneOffset(),
  name: () => { try { const n = Intl.DateTimeFormat().resolvedOptions().timeZone; return typeof n === "string" && NAME.test(n) ? n : null; } catch { return null; } },
};

const pad = (n) => String(n).padStart(2, "0");
export const offsetName = (m) => `UTC${m < 0 ? "-" : "+"}${pad(Math.floor(Math.abs(m) / 60))}:${pad(Math.abs(m) % 60)}`;

// This panel's zone as [[epoch seconds, minutes], ...] over now - BACK .. now + AHEAD, the first from 0.
export function zoneTable(now) {
  const off = (ms) => zone.offsetAt(ms);
  let t = Math.floor(now / 1000) * 1000 - BACK;
  const end = now + AHEAD, out = [[0, off(t)]];
  for (; t < end; t += STEP) {
    const next = t + STEP;
    if (off(next) === out[out.length - 1][1]) continue;
    let lo = t, hi = next; // the old offset at lo, the new one at hi: the second it changes
    while (hi - lo > 1000) { const mid = lo + Math.floor((hi - lo) / 2000) * 1000; if (off(mid) === out[out.length - 1][1]) lo = mid; else hi = mid; }
    out.push([hi / 1000, off(hi)]);
  }
  return out;
}

// The collect's extra args.
export function zoneArgs(now) {
  return ["--zone-offsets", zoneTable(now).map(([e, m]) => `${e}:${m}`).join(","), "--zone-name", zone.name() ?? offsetName(zone.offsetAt(now))];
}

// An old pa-costs (before t-036) refuses the zone args: the collect is run again without them (UTC days, labelled).
export const oldTool = (r) => Boolean(r) && r.code === 2 && /unrecognized arguments:.*--zone-/.test(String(r.stderr ?? ""));

export const offsetIn = (table, sec) => { let m = table[0][1]; for (const [e, o] of table) { if (sec < e) break; m = o; } return m; };
// The local date (YYYY-MM-DD) of a time here.
export const localDate = (ms) => new Date(ms + zone.offsetAt(ms) * 60000).toISOString().slice(0, 10);
// The date n days before a YYYY-MM-DD date.
export const daysBefore = (day, n) => { const [y, m, d] = day.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d - n)).toISOString().slice(0, 10); };

const valid = (t) => Array.isArray(t) && t.length > 0 && t.every((x) => Array.isArray(x) && x.length === 2 && x.every((v) => typeof v === "number" && Number.isFinite(v)));
const seen = new WeakMap(); // summary -> { at, r }: the comparison is worked out again at most once a minute

// Whether a summary's days are this panel's local days: { local, label } (label: the summary's zone, "UTC" for an older
// summary without "days"). The same: the same offset at every change of either table in the last 100 days.
export function summaryZone(s, now) {
  const days = s && typeof s.days === "object" && s.days ? s.days : null;
  const label = typeof days?.zone === "string" && NAME.test(days.zone) ? days.zone : "UTC";
  if (!days || !valid(days.offsets)) return { local: false, label };
  const kept = seen.get(s);
  if (kept && Math.abs(now - kept.at) < 60000) return kept.r;
  const mine = zoneTable(now), from = (now - BACK) / 1000, to = now / 1000;
  const points = [from, to, ...[...mine, ...days.offsets].flatMap(([e]) => [e, e - 1])].filter((x) => x >= from && x <= to);
  const r = { local: points.every((p) => offsetIn(mine, p) === offsetIn(days.offsets, p)), label };
  seen.set(s, { at: now, r });
  return r;
}
