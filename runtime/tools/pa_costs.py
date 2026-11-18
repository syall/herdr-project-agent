#!/usr/bin/env python3
"""Cost tracking per space: tokens from the session files, estimated money from the profile's price table.
Python standard library only (3.7+). Linked as pa-costs.

  pa-costs collect [--space ID] [--quiet] [--resummarize]   read new session bytes into the space's cost ledger
  pa-costs report [--space ID] [--by KEY] [--since D] [--until D] [--json] [--billed] [--account-wide] [--utc]
  pa-costs reconstruct [--space ID] [--dry-run]              find the space's sessions from before launch records
  pa-costs prices [--space ID]                               the price table in use, and models seen without a price
  pa-costs billed status|list|remove|disable|enable|fetch|import ...   billed amounts (the person's commands)
  pa-costs billed compare --json [--space ID] [--summary-json J] [--here-json J]   the Costs view's billed comparison
  pa-costs statusline --space ID [--then ARGV...]   Claude Code's status line (pa-claude/pa-session add it): records
                                                     the session's own cost figure, prints the inner line unchanged

Files, in the space's state folder (costs/): sessions.jsonl (launch and other session records, append-only),
turns.jsonl (the cost ledger: tokens per settled segment, append-only), state.json (read offsets), summary.json
(what the panel reads), cc/<session>.json (Claude Code's own per-session cost figure, list price, from its status
line input: ids and numbers only). Billed rows: <HERDR_PA_HOME>/billing/. Counters and ids only: no session text is kept,
printed or logged. Nothing here uses the network or reads any account; billed rows come only from a script the
profile declares, which only the person turns on (pa-costs billed enable, in a terminal).
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402

CLAUDE_HOME = pa.CLAUDE_HOME
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(pa.HOME, ".codex")
KINDS = ("in", "out", "cw5m", "cw1h", "cr")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def zero():
    return {k: 0 for k in KINDS}


def total_tokens(tokens):
    return sum(int((tokens or {}).get(k) or 0) for k in KINDS)


# ---- price table (profile data: profiles/<name>/prices.json) --------------------------------------

def _price_entry(entry, where):
    if not isinstance(entry, dict) or not isinstance(entry.get("validFrom"), str) or not DATE.match(entry["validFrom"]):
        raise ValueError("{}: validFrom must be a date YYYY-MM-DD".format(where))
    for k in KINDS:
        v = entry.get(k, 0)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            raise ValueError("{}: price {} must be a number >= 0 or null".format(where, k))
    long = entry.get("long")
    if long is not None:
        if not isinstance(long, dict):
            raise ValueError("{}: long must be an object".format(where))
        for k in KINDS:
            v = long.get(k, 0)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise ValueError("{}: long.{} must be a number >= 0 or null".format(where, k))
        if "above" in long:
            v = long["above"]
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError("{}: long.above must be a positive whole number of prompt tokens, or left out".format(where))
    return entry


SCOPE_FIELDS = ("provider", "endpoint", "region", "account")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # provider and account labels (t-032 D1)
ENDPOINT = re.compile(r"^[a-z][a-z0-9-]{0,23}$")  # endpoint kinds: global, geo, regional, or a profile's own
REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")  # a source region label, never a host or an account
FACT_RULES = {"provider": LABEL, "endpoint": ENDPOINT, "region": REGION, "account": LABEL}
TWELVE = re.compile(r"\d{12}")  # an account number: refused in every label these tools write or read


def fact_label(field, value):
    """value when it is a valid label for field (and holds no 12 digits), else None."""
    if isinstance(value, str) and FACT_RULES[field].match(value) and not TWELVE.search(value):
        return value
    return None


def _scope(obj, where):
    """(provider, endpoint, region, account) of a scope object; null or missing = any. ValueError when a field is not a label."""
    if not isinstance(obj, dict):
        raise ValueError("{} must be an object".format(where))
    out = []
    for f in SCOPE_FIELDS:
        v = obj.get(f)
        if v is not None and fact_label(f, v) is None:
            raise ValueError("{}: {} must be a label (or null)".format(where, f))
        out.append(v)
    return tuple(out)


def _models(raw_models, where):
    if not isinstance(raw_models, dict):
        raise ValueError("{}: no models object".format(where))
    models = {}
    for name, entries in raw_models.items():
        if not isinstance(entries, list):
            entries = [entries]
        models[name] = sorted((_price_entry(e, name) for e in entries), key=lambda e: e["validFrom"])
    return models


def scope_text(scope):
    """'provider · endpoint · region · account', null as '*'."""
    return " · ".join("*" if v is None else v for v in scope)


def facts_text(facts):
    """'provider · endpoint · region · account' of a row's facts, unknown as '?'."""
    return " · ".join("?" if v is None else v for v in facts)


def load_prices(profile):
    """(table, problem). (None, None) when the profile has no prices.json; (None, why) when it is unusable,
    and then everything is shown as tokens only. Scopes (t-032 A-P1): `defaultScope` (or the older `defaultProvider`,
    or the profile's pricing.defaultProvider) is the scope of the top-level models; `scoped` entries price rows whose
    facts match them (lookup); `scopedAliases` name keys of scoped models. Older tools read only `models`/`aliases`."""
    path = os.path.join(profile.get("_folder") or "", "prices.json")
    if not os.path.isfile(path):
        return None, None
    pricing = profile.get("pricing") if isinstance(profile.get("pricing"), dict) else {}
    prof_default = fact_label("provider", pricing.get("defaultProvider"))
    price_as = pricing.get("endpointPriceAs") if isinstance(pricing.get("endpointPriceAs"), dict) else {}
    price_as = {k: v for k, v in price_as.items() if fact_label("endpoint", k) and fact_label("endpoint", v)}
    warnings = []
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
            raise ValueError("no models object")
        if not isinstance(raw.get("currency"), str) or not raw["currency"]:
            raise ValueError("no currency")
        models = _models(raw["models"], "models")
        aliases = []
        for a in raw.get("aliases") or []:
            if not isinstance(a, dict) or not isinstance(a.get("match"), str) or a.get("model") not in models:
                raise ValueError("an alias needs match (a regex) and model (a key in models)")
            aliases.append((re.compile(a["match"]), a["model"]))
        has_default = "defaultScope" in raw
        if has_default:
            default = _scope(raw["defaultScope"], "defaultScope")
        elif raw.get("defaultProvider") is not None:
            if fact_label("provider", raw["defaultProvider"]) is None:
                raise ValueError("defaultProvider must be a label")
            default = (raw["defaultProvider"], None, None, None)
        else:
            default = (prof_default, None, None, None)
        if prof_default and default[0] != prof_default:
            warnings.append("prices.json of profile {}: default provider {} differs from the profile's pricing.defaultProvider {}: "
                            "the file's is used".format(profile.get("name"), default[0], prof_default))
        if not isinstance(raw.get("scoped") or [], list):
            raise ValueError("scoped must be a list")
        scoped, seen = [], {default}
        for i, e in enumerate(raw.get("scoped") or []):
            where = "scoped[{}]".format(i)
            scope = _scope(e, where)
            if all(v is None for v in scope):
                raise ValueError("{}: at least one of provider, endpoint, region, account".format(where))
            if scope in seen:
                raise ValueError("{}: scope {} appears twice (or is the default scope)".format(where, scope_text(scope)))
            seen.add(scope)
            scoped.append({"scope": scope, "models": _models(e.get("models"), where)})
        scoped_keys = {k for e in scoped for k in e["models"]}
        scoped_aliases = []
        for a in raw.get("scopedAliases") or []:
            if not isinstance(a, dict) or not isinstance(a.get("match"), str) or a.get("model") not in scoped_keys:
                raise ValueError("a scoped alias needs match (a regex) and model (a key in a scoped models)")
            scoped_aliases.append((re.compile(a["match"]), a["model"]))
    except (OSError, ValueError, re.error) as e:
        return None, "prices.json of profile {} is not usable ({}): tokens only".format(profile.get("name"), str(e)[:160])
    if has_default:  # rows' own endpoint/region (informational) should say their scope's; files without one are not checked
        for scope, table_models in [(default, models)] + [(e["scope"], e["models"]) for e in scoped]:
            for name, entries in sorted(table_models.items()):
                for field, want in (("endpoint", scope[1]), ("region", scope[2])):
                    if any(field in x and x[field] != want for x in entries):
                        warnings.append("prices.json of profile {}: {} in scope {} has a row whose {} is not the scope's".format(
                            profile.get("name"), name[:80], scope_text(scope), field))
    # The ledger keeps one long-context part, split at LONG prompt tokens. Another threshold is accepted but named.
    for name, entries in sorted(models.items()):
        other = sorted({e["long"]["above"] for e in entries if e.get("long") and e["long"].get("above", LONG) != LONG})
        if other:
            warnings.append("prices.json of profile {}: {} long.above {} differs from the {} prompt tokens the ledger splits at: "
                            "calls in between are priced at the base rate".format(profile.get("name"), name[:80],
                                                                                   ", ".join(str(x) for x in other), LONG))
    return {"currency": raw["currency"], "unit": raw.get("unit"), "source": raw.get("source"), "asOf": raw.get("asOf"),
            "note": raw.get("note"), "profile": profile.get("name"), "models": models, "aliases": aliases,
            "warnings": warnings, "defaultScope": default, "fileDefault": has_default, "scoped": scoped,
            "scopedAliases": scoped_aliases, "endpointPriceAs": price_as}, None


def model_key(table, model):
    """The table key of model: an exact key (top-level, then any scope's), then aliases, then scopedAliases."""
    if not table or not model:
        return None
    if model in table["models"]:
        return model
    for e in table.get("scoped") or []:
        if model in e["models"]:
            return model
    for pattern, key in list(table["aliases"]) + list(table.get("scopedAliases") or []):
        if pattern.search(model):
            return key
    return None


def facts_of(row):
    """(provider, endpoint, region, account) a ledger row carries; None where it has none."""
    return tuple(row.get(f) if isinstance(row.get(f), str) else None for f in SCOPE_FIELDS)


def _matches(scope, facts, default_provider):
    sp = scope[0]
    if sp is None:
        if facts[0] is not None and facts[0] != default_provider:  # no crossing of providers
            return False
    elif sp != facts[0]:
        return False
    return all(s is None or s == f for s, f in zip(scope[1:], facts[1:]))


def lookup(table, model, when, facts=None):
    """(price entry, pricedAs) for model at `when` (ISO time) under facts (provider, endpoint, region, account), t-032
    A-P2: the scopes that match (null = any; unknown provider and endpoint take the default scope's), most specific first
    (account, then region, then endpoint, then provider); in each the latest validFrom on or before the day; none: (None,
    None). pricedAs = {"scope": [p, n, g, a] with null as "*", "validFrom"}. No facts: the default scope alone."""
    key = model_key(table, model)
    if key is None:
        return None, None
    day = (when or "")[:10]
    default = table.get("defaultScope") or (None, None, None, None)
    scopes = [(default, table["models"])] + [(e["scope"], e["models"]) for e in table.get("scoped") or []]
    if facts is None:
        candidates = scopes[:1]
    else:
        p, n, g, a = (list(facts) + [None] * 4)[:4]
        p = default[0] if p is None else p
        n = default[1] if n is None else n
        n = (table.get("endpointPriceAs") or {}).get(n, n) if n is not None else None
        candidates = [x for x in scopes if _matches(x[0], (p, n, g, a), default[0])]
        candidates.sort(key=lambda x: (x[0][3] is not None, x[0][2] is not None, x[0][1] is not None, x[0][0] is not None), reverse=True)
    for scope, models in candidates:
        found = None
        for entry in models.get(key) or []:
            if entry["validFrom"] <= day:
                found = entry
        if found is not None:
            return found, {"scope": ["*" if v is None else v for v in scope], "validFrom": found["validFrom"]}
    return None, None


def price_for(table, model, when, facts=None):
    """The price entry in effect for model at `when` (ISO time): the latest validFrom on or before its day, in the
    scope lookup() picks for facts (none: the default scope)."""
    if not table:
        return None
    return lookup(table, model, when, facts)[0]


def money(tokens, long, price):
    """Σ tokens x price / 1e6, the long-context part priced from price.long when there is one. None when a kind
    that has tokens has no price."""
    if not price:
        return None
    long = long or {}
    high = price.get("long") or price
    total = 0.0
    for k in KINDS:
        n, n_long = int(tokens.get(k) or 0), int(long.get(k) or 0)
        n_long = min(n_long, n)
        for count, p in ((n - n_long, price.get(k, 0)), (n_long, high.get(k, price.get(k, 0)))):
            if not count:
                continue
            if p is None:
                return None
            total += count * p / 1e6
    return total


def row_money(row, table):
    """A ledger row's money, from its tokens, model and time only."""
    return money(row.get("tokens") or {}, row.get("long") or {}, price_for(table, row.get("model"), row.get("from"), facts_of(row)))


def price_rows(rows, table):
    """{"money", "unpricedTokens", "missing"} over rows. money is None when nothing is priced."""
    priced, unpriced, missing, any_priced = 0.0, 0, set(), False
    for row in rows:
        m = row_money(row, table)
        if m is None:
            unpriced += total_tokens(row.get("tokens"))
            missing.add(row.get("model") or "(unknown model)")
        else:
            priced += m
            any_priced = True
    return {"money": priced if any_priced else None, "unpricedTokens": unpriced, "missing": sorted(missing)}


# ---- the space's cost files --------------------------------------------------------------------

IDLE = 600  # a segment settles after this many seconds without a new line, and a Codex gap this long splits one
LONG = 200000  # a call whose prompt (in + cache write + cache read) is above this many tokens is long context
REBUILD_MIN = 20000  # a cache write after idle counts as a rebuild from this many tokens
TAIL = 256  # message ids remembered per session for the dedupe
PA_TURNS = 64  # turn starts remembered per Project Agent session, for its subagents' rows (REVIEW-4 RL4-1)
FORK_SLACK = 5  # seconds: a resumed or forked session skips calls older than its start record minus this
GONE_UNSEEN = 7 * 86400  # a recorded session whose file was never found is gone after this many seconds (a transcript
# is written at the first message, which can come hours after the start)
DONE_AFTER = 86400  # a closed or gone session, fully read, is not looked at again after this many seconds
MODEL = re.compile(r"^[\w.:@/\[\]+-]{1,80}$")


def costs_dir(directory):
    return os.path.join(directory, "costs")


def parse_ts(text):
    """Epoch seconds of an ISO UTC time (with or without fractions); None when it is not one."""
    try:
        base = datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None
    frac = re.match(r"\.(\d+)", text[19:])
    return base + (float("0." + frac.group(1)) if frac else 0.0)


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def clean_model(value):
    return value if isinstance(value, str) and MODEL.match(value) else ("(unknown model)" if value else None)


def read_lines(path):
    """JSON objects of a JSON-lines file, and the number of lines that did not parse."""
    out, bad = [], 0
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return out, bad
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            bad += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        else:
            bad += 1
    return out, bad


def append_line(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)


def sessions_of(records):
    """session id -> what the records say about it (start, bind, role, closed, gone)."""
    starts, out = {}, {}
    for r in records:
        if r.get("ev") == "start":
            starts["{} {}".format(r.get("at"), r.get("terminal"))] = r
    for r in records:
        ev, sid = r.get("ev"), r.get("session")
        if ev == "start" and sid:
            s = out.get(sid)
            if s is None:
                out[sid] = dict(r, starts=[r])
            else:
                s["starts"].append(r)
        elif ev == "bind" and sid:
            base = dict(starts.get(r.get("for")) or {})
            base.update({k: v for k, v in r.items() if k not in ("ev", "at", "for") and v is not None})
            base.setdefault("at", r.get("at"))
            base["session"] = sid
            if sid not in out:
                out[sid] = dict(base, starts=[base])
    for r in records:
        sid, ev = r.get("session"), r.get("ev")
        if ev == "start" and sid in out:  # a later start (a resume) opens a closed or gone session again
            out[sid]["gone"] = False
            out[sid].pop("closed", None)
        elif ev == "role" and sid in out and r.get("role"):
            out[sid]["role"], out[sid]["rolePinned"] = r["role"], True
        elif ev == "gone" and sid in out:
            out[sid]["gone"] = True
        elif ev == "closed":
            for s in ([out[sid]] if sid in out else [s for s in out.values() if r.get("terminal") and s.get("terminal") == r.get("terminal")
                                                     and (s.get("at") or "") <= (r.get("at") or "")]):
                s["closed"] = r.get("at")
    return out


def find_file(sess, cached):
    """The session file's path: cached, the expected place, else a search by id."""
    if cached and os.path.exists(cached):
        return cached
    sid = sess["session"]
    if not re.match(r"^[A-Za-z0-9-]{8,80}$", sid):
        return None
    if sess.get("program") == "codex":
        root = os.path.join(CODEX_HOME, "sessions")
        if os.path.isdir(root):
            for folder, _dirs, names in os.walk(root):
                for name in names:
                    if name.startswith("rollout-") and name.endswith(sid + ".jsonl"):
                        return os.path.join(folder, name)
        return None
    projects = os.path.join(CLAUDE_HOME, "projects")
    if sess.get("cwd"):
        expected = os.path.join(projects, pa.claude_slug(sess["cwd"]), sid + ".jsonl")
        if os.path.exists(expected):
            return expected
    if os.path.isdir(projects):
        for slug in sorted(os.listdir(projects)):
            candidate = os.path.join(projects, slug, sid + ".jsonl")
            if os.path.exists(candidate):
                return candidate
    return None


# ---- reading one session file -----------------------------------------------------------------

def reduce_claude(obj):
    """Only the allowlisted fields (spec section 4); everything else is dropped here."""
    t = obj.get("type")
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
    if t == "user":
        content = message.get("content")
        first = content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
        text = isinstance(content, str) or first.get("type") == "text"
        return {"type": "user", "ts": ts,
                "start": bool(text and not obj.get("isMeta") and not obj.get("isSidechain") and "toolUseResult" not in obj)}
    if t == "assistant" and isinstance(message.get("usage"), dict):
        u = message["usage"]
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
        if cc is not None and ("ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc):
            cw5m, cw1h = num(cc.get("ephemeral_5m_input_tokens")), num(cc.get("ephemeral_1h_input_tokens"))
        else:
            cw5m, cw1h = num(u.get("cache_creation_input_tokens")), 0
        ident = next((x for x in (message.get("id"), obj.get("requestId"), obj.get("uuid")) if isinstance(x, str) and x), None)
        return {"type": "call", "ts": ts, "id": hashlib.sha1(ident.encode()).hexdigest()[:16] if ident else None,
                "model": clean_model(message.get("model")),
                "tokens": {"in": num(u.get("input_tokens")), "out": num(u.get("output_tokens")), "cw5m": cw5m, "cw1h": cw1h,
                           "cr": num(u.get("cache_read_input_tokens"))}}
    return None


def reduce_codex(obj):
    t = obj.get("type")
    p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
    if t == "session_meta":
        return {"type": "meta", "ts": ts, "id": p.get("id") if isinstance(p.get("id"), str) else None,
                "start": p.get("timestamp") if isinstance(p.get("timestamp"), str) else ts,
                "cwd": p.get("cwd") if isinstance(p.get("cwd"), str) else None}
    if t == "turn_context":
        return {"type": "turn", "ts": ts, "model": clean_model(p.get("model"))}
    if t == "event_msg" and p.get("type") == "token_count":
        info = p.get("info") if isinstance(p.get("info"), dict) else {}
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else None
        if total is None:
            return None
        return {"type": "total", "ts": ts, "total": {k: num(total.get(k)) for k in (
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")}}
    return None


CLAUDE_MARKS = (b'"type":"assistant"', b'"type":"user"', b'"type": "assistant"', b'"type": "user"')
CODEX_MARKS = (b"token_count", b"session_meta", b"turn_context")


def new_segment(ts, turn, model):
    return {"from": ts, "to": ts, "day": ts[:10], "calls": 0, "tokens": zero(), "long": zero(),
            "rebuild": {"calls": 0, "cw5m": 0, "cw1h": 0, "gapS": 0}, "model": model, "turn": turn}


class Reader:
    """Turns new lines of one session file into settled segments. `f` is the file's state (state.json files[sid])."""

    def __init__(self, sess, f, settled, others=()):
        self.sess, self.f, self.settled = sess, f, settled
        self.others = others  # the dedupe tails of the session's other files (its subagent files, or the parent)
        first = min(sess.get("starts") or [sess], key=lambda r: r.get("at") or "")
        copies = first.get("bind") or first.get("fork")  # a new file that may hold copies of another session's calls
        at = parse_ts(first.get("at") or "")
        self.guard = (at - FORK_SLACK) if copies and at else None
        # A resume with the session's own id writes into its own file: the calls before the first record are this
        # session's past (from before launch records existed). They count, marked reconstructed (R7).
        self.before = (at - FORK_SLACK) if first.get("resumed") and not copies and at else None

    def settle(self, until=None):
        if self.f.get("open"):
            if self.f["open"]["calls"]:
                if until:
                    self.f["open"]["until"] = until
                self.settled.append(self.f["open"])
            self.f["open"] = None
            for item in self.f.get("tail", []):
                item[2] = True  # a later line of these calls is a late growth

    def new_start(self, seg, ts):
        """A later start record of the session took effect between the open segment's start and this call (t-032 J1d,
        d-117 limit 8): the segment settles there, so each row lies under one record and takes its facts."""
        if len(self.sess.get("starts") or ()) < 2:
            return False
        return start_in_effect(self.sess, seg.get("from")) is not start_in_effect(self.sess, ts)

    def add_call(self, ts, ident, model, tokens):
        f = self.f
        epoch = parse_ts(ts)
        if epoch is None:
            f["bad"] = f.get("bad", 0) + 1
            return
        if self.guard is not None and epoch < self.guard:
            return
        tail = f.setdefault("tail", [])
        if ident is not None:
            for item in tail:
                if item[0] == ident:
                    grown = tokens["out"] - item[1]
                    if grown > 0:
                        item[1] = tokens["out"]
                        if not f.get("open"):
                            f["open"] = new_segment(ts, f.get("turn") or ts, model or f.get("model"))
                        f["open"]["tokens"]["out"] += grown
                        if item[2]:  # its lines were already settled: added to the open segment, and said so
                            f["late"] = f.get("late", 0) + 1
                    return
            if any(item[0] == ident for other in self.others for item in other):
                return  # the same call, written in another file of this session: counted there
        last = parse_ts(f.get("lastCall") or "")
        seg = f.get("open")
        model = model or f.get("model")
        pre = self.before is not None and epoch < self.before
        if seg and (seg["day"] != ts[:10] or seg["model"] != model or bool(seg.get("pre")) != pre
                    or (self.sess.get("program") == "codex" and last is not None and epoch - last > IDLE)
                    or self.new_start(seg, ts)):
            self.settle()
            seg = None
        if not seg:
            seg = f["open"] = new_segment(ts, f.get("turn") or ts, model)
            if pre:
                seg["pre"] = True
        for k in KINDS:
            seg["tokens"][k] += tokens[k]
        if tokens["in"] + tokens["cw5m"] + tokens["cw1h"] + tokens["cr"] > LONG:
            for k in KINDS:
                seg["long"][k] += tokens[k]
        if last is not None:
            gap = epoch - last
            life = 3600 if tokens["cw1h"] else 300
            if gap > life and tokens["cw5m"] + tokens["cw1h"] >= REBUILD_MIN:
                rb = seg["rebuild"]
                rb["calls"] += 1
                rb["cw5m"] += tokens["cw5m"]
                rb["cw1h"] += tokens["cw1h"]
                rb["gapS"] = max(rb["gapS"], int(gap))
        seg["calls"] += 1
        seg["to"] = ts if ts > seg["to"] else seg["to"]
        f["lastCall"] = ts
        f["model"] = model
        if ident is not None:
            tail.append([ident, tokens["out"], False])
            del tail[:-TAIL]

    def line(self, raw_line):
        f = self.f
        codex = self.sess.get("program") == "codex"
        if not any(m in raw_line for m in (CODEX_MARKS if codex else CLAUDE_MARKS)):
            return
        try:
            obj = json.loads(raw_line.decode("utf-8"))
            e = (reduce_codex if codex else reduce_claude)(obj) if isinstance(obj, dict) else None
        except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
            f["bad"] = f.get("bad", 0) + 1
            return
        del obj
        if not e:
            return
        if e["ts"] and e["ts"] > (f.get("seen") or ""):
            f["seen"] = e["ts"]
        if e["type"] == "user":
            if e["start"]:
                self.settle(e["ts"])
                f["turn"] = e["ts"]
                if self.sess.get("kind") == "pa" and not self.sess.get("sub"):  # its subagents' rows take these turns
                    turns = f.setdefault("turns", [])
                    turns.append(e["ts"])
                    del turns[:-PA_TURNS]
        elif e["type"] == "call":
            self.add_call(e["ts"], e["id"], e["model"], e["tokens"])
        elif e["type"] == "turn":
            self.settle(e["ts"])
            f["turn"] = e["ts"]
            f["model"] = e["model"] or f.get("model")
        elif e["type"] == "total":
            cum = f.get("cum") or {}
            t = e["total"]
            d = {k: t[k] - cum.get(k, 0) for k in t}
            if any(v < 0 for v in d.values()):  # counters started again: this total is all new
                d = dict(t)
            f["cum"] = t
            if not any(d.values()):
                return
            cr, cw = max(0, d["cached_input_tokens"]), max(0, d["cache_write_input_tokens"])
            tokens = {"in": max(0, d["input_tokens"] - cr - cw), "out": max(0, d["output_tokens"]), "cw5m": cw, "cw1h": 0, "cr": cr}
            self.add_call(e["ts"], None, f.get("model"), tokens)


def read_new(sess, f, path, size, settled, others=()):
    """Read [offset, size) of the file, whole lines only; advance the offset past them."""
    reader = Reader(sess, f, settled, others)
    with open(path, "rb") as fh:
        fh.seek(f.get("offset", 0))
        data = fh.read(size - f.get("offset", 0))
    end = data.rfind(b"\n")
    if end < 0:
        return reader
    for raw_line in data[:end].split(b"\n"):
        if raw_line.strip():
            reader.line(raw_line)
    f["offset"] = f.get("offset", 0) + end + 1
    return reader


# ---- collect --------------------------------------------------------------------------------------

def agent_owner(path):
    """The parent session id an agent-*.jsonl file beside the sessions names in its lines (sessionId); "" when its
    first whole lines name none; None while it has no whole line yet. Only that field is looked at."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    end = head.rfind(b"\n")
    if end < 0:
        return None
    for raw_line in head[:end].split(b"\n"):
        if b'"sessionId"' not in raw_line:
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        sid = obj.get("sessionId") if isinstance(obj, dict) else None
        del obj
        if isinstance(sid, str) and re.match(r"^[A-Za-z0-9-]{8,80}$", sid):
            return sid
    return ""


def subagent_files(parent_path, sid, owners, listing):
    """(name, path) of the subagent transcripts of Claude session sid, in two layouts (P-c; DECISIONS, unverified):
    <folder>/<sid>/subagents/*.jsonl, and agent-*.jsonl in the session's own folder whose lines name sid. owners
    (kept in state.json) remembers which session each agent-*.jsonl belongs to, so its head is read once."""
    base = os.path.dirname(parent_path)
    nested = os.path.join(base, sid, "subagents")
    try:
        names = sorted(os.listdir(nested))
    except OSError:
        names = []
    out = [(n[:-6], os.path.join(nested, n)) for n in names if n.endswith(".jsonl") and re.match(r"^[\w.-]{1,120}$", n)]
    if base not in listing:
        try:
            listing[base] = sorted(n for n in os.listdir(base) if n.startswith("agent-") and n.endswith(".jsonl"))
        except OSError:
            listing[base] = []
    taken = {name for name, _p in out}
    for n in listing[base]:
        key = os.path.basename(base) + "/" + n
        owner = owners.get(key)
        if owner is None:
            owner = agent_owner(os.path.join(base, n))
            if owner is None:
                continue
            owners[key] = owner
        if owner == sid and n[:-6] not in taken:
            out.append((n[:-6], os.path.join(base, n)))
    return out


def unknown_subagent_files(parent_path, sid):
    """Relative paths of the .jsonl files under <folder>/<sid>/ that no P-c layout rule reads (VERIFY-2 M-1): they
    count nothing and are named in problems. Names only; no file is opened."""
    top = os.path.join(os.path.dirname(parent_path), sid)
    found = []
    for folder, dirs, names in os.walk(top):
        dirs.sort()
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(folder, n), top)
            if n.endswith(".jsonl") and not (os.path.dirname(rel) == "subagents" and re.match(r"^[\w.-]{1,120}$", n)):
                found.append(rel)
    return found


def short(sid):
    return (sid or "?")[:8]


def start_in_effect(sess, when):
    """The start record of a session in effect at `when` (t-032 fix round 3): its latest start at or before that time,
    the later line of two in one second; the first when `when` is earlier than all, or not a time. A session has more
    than one start record after t-033's fallback (the refused attempt's, then the one that started) or a resume."""
    starts = sess.get("starts") or [sess]
    at = parse_ts(when or "") if isinstance(when, str) else None
    best = starts[0]
    if at is None or len(starts) == 1:
        return best
    best_at = None
    for r in starts:
        t = parse_ts(r.get("at") or "") if isinstance(r.get("at"), str) else None
        if t is not None and t <= at and (best_at is None or t >= best_at):
            best, best_at = r, t
    return best


def make_row(sess, seg):
    """A ledger row from a settled segment: tokens only, attributed by attribute() (step 4 rules). Its facts are those
    of the start record in effect at the segment's start."""
    start = start_in_effect(sess, seg.get("from"))
    pre = seg.get("pre")  # calls of a resumed session from before its first record: its past, best effort (R7)
    return {"v": 1, "seq": None, "session": sess["session"], "program": sess.get("program") or "claude",
            "kind": sess.get("kind") or "unknown", "model": seg["model"], "from": seg["from"], "to": seg["to"],
            "day": seg["day"], "calls": seg["calls"], "tokens": seg["tokens"], "long": seg["long"], "rebuild": seg["rebuild"],
            "turn": seg["turn"], "task": sess.get("task"), "phase": None, "activity": None, "role": sess.get("role"),
            "pass": None, "trigger": None, "split": None, "reason": None,
            "source": "reconstructed" if pre else (sess.get("source") or "recorded"),
            "match": "resumed-history" if pre else (sess.get("match") or "launch-record"),
            "confidence": sess.get("confidence") or "high", "until": seg.get("until"),
            **({"sub": sess["sub"]} if sess.get("sub") else {}),
            **{k: start[k] for k in FACT_KEYS if k in start}}  # the launch record's facts (t-032), when it has them


PA_TRIGGERS = ("report", "message", "decision", "scheduled", "manual", "conversation")
ROLE_WORDS = {"investigate": "investigate", "design": "design", "plan": "plan", "build": "build", "verifier": "verifier",
              "verify": "verifier", "reviewer": "reviewer", "review": "reviewer"}
HANDOFF_SLACK = 5  # seconds: a handoff journaled just after the worker's prompt still counts for it


def space_facts(ctx):
    """The ledger and the journal, read once per collect and only when a row needs them. The journal's `why` is
    looked at only for address tokens; it is never kept."""
    if "journal" not in ctx:
        directory = ctx["directory"]
        try:
            ctx["ledger"] = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
        except (OSError, ValueError):
            ctx["ledger"] = {}
        entries, _bad = read_lines(os.path.join(directory, "journal.jsonl"))
        journal = []
        for e in entries:
            at = parse_ts(e.get("at") or "") if isinstance(e.get("at"), str) else None
            if at is None:
                continue
            journal.append({"at": at, "trigger": e.get("trigger") if isinstance(e.get("trigger"), str) else None,
                            "task": e.get("task") if isinstance(e.get("task"), str) else None,
                            "pass": e.get("pass") if isinstance(e.get("pass"), int) and not isinstance(e.get("pass"), bool) else None,
                            "to": e.get("to") if isinstance(e.get("to"), str) else None,
                            "why": e.get("why") if isinstance(e.get("why"), str) else ""})
        journal.sort(key=lambda e: e["at"])
        ctx["journal"] = journal
    return ctx["ledger"], ctx["journal"]


def attribute(row, sess, ctx):
    """Fills activity, task, phase, role, pass, trigger, split and reason (spec 3.5). None: a Project Agent turn whose
    journal entries may still come (held for up to IDLE seconds)."""
    kind = sess.get("kind")
    if kind == "pa":
        return attribute_pa(row, ctx)
    if kind == "worker":
        return attribute_worker(row, sess, ctx)
    row["activity"], row["reason"] = "unattributed", "unknown-session-kind"
    return row


def parent_turn(row, ctx):
    """A Project Agent subagent row's window from the parent turn open at its first call: (turn, start, end); "hold"
    while that turn is the parent's latest and still open; None when the parent's turns do not cover it."""
    pf = (ctx.get("files") or {}).get(row["session"]) or {}
    first = parse_ts(row.get("from") or "")
    turns = sorted((t, x) for t, x in ((parse_ts(x), x) for x in pf.get("turns") or []) if t is not None)
    if first is None or not turns or turns[0][0] > first:
        return None
    i = max(n for n, (t, _x) in enumerate(turns) if t <= first)
    if i + 1 < len(turns):
        return turns[i][1], turns[i][0], turns[i + 1][0]
    if pf.get("open"):
        return "hold"
    return turns[i][1], turns[i][0], max(parse_ts(row["to"]) or first, parse_ts(pf.get("lastCall") or "") or 0) + IDLE


def attribute_pa(row, ctx):
    _ledger, journal = space_facts(ctx)
    start = parse_ts(row.get("turn") or row["from"]) or 0
    end = parse_ts(row.get("until") or "") or ((parse_ts(row["to"]) or start) + IDLE)
    if row.get("sub"):
        window = parent_turn(row, ctx)
        if window == "hold":
            return None
        if window:
            row["turn"], start, end = window
    entries = [e for e in journal if e["at"] + 0.999 >= start and e["at"] < end]
    row["task"], row["role"] = None, None
    if not entries:
        if ctx["now"] - (parse_ts(row["to"]) or 0) < IDLE:
            return None
        row["activity"], row["reason"] = "unattributed", "pa-no-journal"
        return row
    counts = {}
    for e in entries:
        counts[e["trigger"]] = counts.get(e["trigger"], 0) + 1
    trigger = max(counts, key=lambda k: counts[k])  # ties: the first seen
    row["trigger"] = trigger
    row["activity"] = "pass:" + (trigger if trigger in PA_TRIGGERS else "other")
    passes = [e["pass"] for e in entries if e["pass"] is not None]
    row["pass"] = max(passes) if passes else None
    weights = {}
    for e in entries:
        if e["task"]:
            weights[e["task"]] = weights.get(e["task"], 0) + 1
    if len(weights) == 1:
        row["task"] = next(iter(weights))
    elif weights:
        n = float(sum(weights.values()))
        row["split"] = [{"task": t, "w": round(c / n, 4)} for t, c in sorted(weights.items(), key=lambda x: (-x[1], x[0]))]
    return row


def worker_entry(ledger, terminal, name):
    """(task, worker) ledger entries of this address. A named session skips entries of another named worker: the
    address may have been reused after an earlier worker closed (spec 2.1)."""
    found = [(t, w) for t in ledger.get("tasks", []) or [] for w in t.get("workers") or [] if w.get("address") == terminal
             and not (name and w.get("name") and w.get("name") != name)]
    found.sort(key=lambda tw: not (name and tw[1].get("name") == name))
    return found


def attribute_worker(row, sess, ctx):
    ledger, journal = space_facts(ctx)
    terminal, name = sess.get("terminal"), sess.get("name")
    start = parse_ts(row.get("turn") or row["from"]) or 0
    entries = worker_entry(ledger, terminal, name) if terminal else []
    task = sess.get("task")
    if terminal and (task is None or len({t.get("id") for t, _w in entries}) > 1):
        token = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(terminal) + r"(?![A-Za-z0-9_-])")
        handoff = None
        since = parse_ts(sess.get("startedAt") or sess.get("at") or "")  # entries from before this session started are not its handoffs
        for e in journal:
            if e["at"] > start + HANDOFF_SLACK:
                break
            if since is not None and e["at"] < since - HANDOFF_SLACK:
                continue
            if e["task"] and token.search(e["why"]):
                handoff = e
        if handoff:
            task = handoff["task"]
            row["match"], row["confidence"] = "journal-handoff", "medium"
    row["task"] = task
    role = sess.get("role")
    if not role and entries and entries[0][1].get("role"):
        role = entries[0][1]["role"]
        if not sess.get("rolePinned"):
            ctx["events"].append({"v": 1, "ev": "role", "at": iso(ctx["now"]), "session": sess["session"], "role": role, "from": "ledger"})
            sess["role"], sess["rolePinned"] = role, True
    if not role and name:
        role = ROLE_WORDS.get(name.rsplit("-", 1)[-1])
    row["role"] = role
    phase = None
    for e in journal:
        if e["at"] > start:
            break
        if e["task"] == task and e["to"]:
            phase = e["to"].split("/")[0]
    row["phase"] = phase
    if not task:
        row["activity"], row["reason"] = "unattributed", "worker-no-task"
        return row
    word = ROLE_WORDS.get(role or "", role)
    if word == "reviewer":
        activity = "review"
    elif word == "verifier":
        activity = "verify"
    elif word in ("build", "investigate", "plan"):
        activity = word
    elif word == "design":
        activity = {"intent": "investigate", "plan": "plan"}.get(phase, "design")
    elif name and name.endswith("-investigate"):
        activity = "investigate"
    else:
        activity = "other"
    row["activity"] = activity
    return row


def state_problems(state):
    """The collector's problems, worked out from the state alone (so a rerun with nothing new writes the same)."""
    out, never = [], 0
    for sid, f in sorted((state.get("files") or {}).items()):
        if f.get("bad"):
            out.append("{} lines not usable in {}".format(f["bad"], short(sid)))
        if f.get("late"):
            out.append("{} calls grew after their segment settled in {}".format(f["late"], short(sid)))
        if f.get("voided"):
            out.append("{} re-read from the start {} time(s) (file replaced or shrunk)".format(short(sid), f["voided"]))
        if f.get("neverRead"):
            never += 1
        if f.get("unknown"):
            out.append("{}: {} file(s) under its folder not read (unknown layout): {}".format(
                short(sid), len(f["unknown"]), ", ".join(f["unknown"][:5]) + (", ..." if len(f["unknown"]) > 5 else "")))
    unowned = sorted(k.split("/", 1)[-1] for k, v in (state.get("owners") or {}).items() if v == "")
    if unowned:
        out.append("{} agent-*.jsonl file(s) beside the sessions name no session, not read (unknown layout): {}".format(
            len(unowned), ", ".join(unowned[:5]) + (", ..." if len(unowned) > 5 else "")))
    if never:
        out.append("{} sessions' files were gone before they were read".format(never))
    out.extend(state.get("pending") or [])
    out.extend(state.get("notes") or [])
    return out


def collect(directory, now=None, quiet=True, resummarize=False, zone=None):
    """Read the new bytes of the space's sessions into costs/turns.jsonl. One collector per space at a time. zone: the
    panel's (t-036), kept in costs/zone.json for later collects; a changed one rewrites the summary."""
    now = time.time() if now is None else now
    folder = costs_dir(directory)
    if zone is not None and os.path.isdir(folder) and save_zone(directory, zone):
        resummarize = True
    if not os.path.isfile(os.path.join(folder, "sessions.jsonl")):
        if not quiet:
            print("No cost records yet: spend is recorded for sessions started from now on (pa-costs reconstruct finds older ones).")
        return {"rows": 0, "sessions": 0}
    fd = os.open(os.path.join(folder, ".collect.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not quiet:
                print("already running")
            return {"busy": True}
        return _collect(directory, folder, now, quiet, resummarize)
    finally:
        os.close(fd)


STATE_V = 1


def load_state(state_path, turns_path, now, quiet):
    """(state, the text it was read from). A missing or unreadable state.json never truncates turns.jsonl: the state
    is rebuilt from the ledger (AC5.3). A state of a version this code does not know is refused, and nothing changes."""
    why, text = None, ""
    try:
        with open(state_path) as fh:
            text = fh.read()
        state = json.loads(text)
    except FileNotFoundError:
        why, state = "missing", None
    except (OSError, ValueError, UnicodeDecodeError):
        why, state = "unreadable", None
    if why is None and isinstance(state, dict) and state.get("v") != STATE_V:
        raise pa.Problem("costs/state.json has version {!r}, which this pa-costs does not know: refused, nothing was "
                         "changed (update pa-costs, or move state.json aside to rebuild it from turns.jsonl)".format(state.get("v"))[:300])
    if why is None and not (isinstance(state, dict) and isinstance(state.get("turnsBytes"), int)
                            and not isinstance(state.get("turnsBytes"), bool) and isinstance(state.get("files"), dict)):
        why = "unreadable"
    if why is None:
        state.setdefault("lastSeq", 0)
        return state, text
    if why == "unreadable":  # kept for the person to look at
        os.replace(state_path, "{}.bad-{}".format(state_path, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))))
    state = {"v": STATE_V, "lastSeq": 0, "turnsBytes": 0, "files": {}}
    size = os.path.getsize(turns_path) if os.path.exists(turns_path) else 0
    if not size:
        return state, ""
    with open(turns_path, "rb") as fh:
        data = fh.read()
    end = data.rfind(b"\n") + 1
    if end < len(data):  # a half row from a crash: its session is read again below
        with open(turns_path, "r+b") as fh:
            fh.truncate(end)
    rows, _bad = read_lines(turns_path)
    seqs = [r["seq"] for r in rows if isinstance(r.get("seq"), int)]
    state["lastSeq"] = max(seqs) if seqs else 0
    state["turnsBytes"] = end
    for r in rows:
        if "seq" in r and isinstance(r.get("session"), str):
            key = r["session"] + "/" + r["sub"] if isinstance(r.get("sub"), str) else r["session"]
            state["files"][key] = {"offset": 0, "rebuilt": True}
    note = ("state.json was {} at {}: rebuilt from turns.jsonl (sessions with rows are voided and read again; rows of "
            "sessions whose files are gone are kept)").format(why, iso(now))
    state["notes"] = [note]
    if not quiet:
        print("pa-costs: " + note, file=sys.stderr)
    return state, ""


def _collect(directory, folder, now, quiet, resummarize):
    state_path, turns_path = os.path.join(folder, "state.json"), os.path.join(folder, "turns.jsonl")
    state, old_text = load_state(state_path, turns_path, now, quiet)
    old_problems = list(state.get("problems") or [])
    notes = state.setdefault("notes", [])
    size = os.path.getsize(turns_path) if os.path.exists(turns_path) else 0
    if size > state["turnsBytes"]:  # the last run died between its two writes: drop its rows, they are read again
        with open(turns_path, "r+b") as fh:
            fh.truncate(state["turnsBytes"])
    elif size < state["turnsBytes"]:
        state["turnsBytes"] = size
        if "turns.jsonl is shorter than recorded" not in notes:
            notes.append("turns.jsonl is shorter than recorded")
    records, _bad = read_lines(os.path.join(folder, "sessions.jsonl"))
    sessions = sessions_of(records)
    ctx = {"directory": directory, "records": records, "sessions": sessions, "now": now, "events": []}
    bind_pending(ctx)
    sessions = ctx["sessions"]
    files = ctx["files"] = state.setdefault("files", {})
    out, rows = [], []
    held, state["held"] = state.get("held") or [], []

    def settle_rows(sess, segments):
        for seg in segments:
            row = make_row(sess, seg)
            done = attribute(row, sess, ctx)
            if done is None:
                state["held"].append(row)
            else:
                done.pop("until", None)
                rows.append(done)
    for row in held:
        sess = sessions.get(row["session"]) or {"session": row["session"], "kind": row.get("kind")}
        done = attribute(row, sess, ctx)
        if done is None:
            state["held"].append(row)
        else:
            done.pop("until", None)
            rows.append(done)
    def read_one(sess, sid, sub, f, path, rebuilt, others):
        """Read one file of a session (its transcript, or one of its subagent files) and settle what is due."""
        nonlocal rows
        settled = []
        try:
            st = os.stat(path)
        except OSError:  # a subagent file seen before and now deleted: what was read stays
            Reader(sess, f, settled).settle()
            f["done"] = True
            settle_rows(sess, settled)
            return
        if rebuilt or (f.get("ino") is not None and (st.st_ino != f["ino"] or st.st_dev != f["dev"])) or st.st_size < f.get("offset", 0):
            # Its earlier rows are dropped by the void and read again. Rows of it not written yet (held Project Agent
            # turns, released in this run or still held) come back from the re-read too, so they are dropped here.
            def same(r):
                return r.get("session") == sid and r.get("sub") == sub
            out.extend(r for r in rows if not same(r))
            rows = []
            out[:] = [r for r in out if not ("seq" in r and same(r))]
            state["held"] = [r for r in state["held"] if not same(r)]
            void = {"v": 1, "ev": "void", "session": sid, "upTo": state.get("lastSeq", 0)}
            if sub:
                void["sub"] = sub
            out.append(void)
            for k in ("offset", "tail", "open", "turn", "turns", "cum", "lastCall", "seen", "model", "late", "bad"):
                f.pop(k, None)
            f["offset"] = 0
            f["tail"] = []
            if not rebuilt:
                f["voided"] = f.get("voided", 0) + 1
        f.update(path=path, dev=st.st_dev, ino=st.st_ino)
        if st.st_size > f.get("offset", 0):
            reader = read_new(sess, f, path, st.st_size, settled, others)
        else:
            reader = Reader(sess, f, settled, others)
        if f.get("open"):
            seen = parse_ts(f.get("seen") or f["open"]["to"]) or now
            if now - seen >= IDLE or iso(now)[:10] > f["open"]["day"]:
                reader.settle()
        settle_rows(sess, settled)

    for sid in sorted(sessions):
        sess = sessions[sid]
        f = files.setdefault(sid, {"offset": 0})
        latest = max((r.get("at") or "") for r in sess.get("starts") or [sess])
        if f.get("done"):
            f.setdefault("doneAt", iso(now))
            if not latest or latest <= f["doneAt"]:
                continue
            for k in ("done", "doneAt", "neverRead"):  # started again after it was done (a resume): read it again
                f.pop(k, None)
        settled = []
        path = find_file(sess, f.get("path"))
        rebuilt = f.pop("rebuilt", False)
        subs = {k: v for k, v in files.items() if k.startswith(sid + "/")}
        if not path:
            started = parse_ts(latest) or now
            if f.get("path") or rebuilt or sess.get("gone") or now - started >= GONE_UNSEEN:
                Reader(sess, f, settled).settle()
                for key, sf in sorted(subs.items()):
                    Reader(dict(sess, sub=key.split("/", 1)[1]), sf, settled).settle()
                    sf["done"] = True
                if not sess.get("gone"):
                    ctx["events"].append({"v": 1, "ev": "gone", "at": iso(now), "session": sid, "readTo": f.get("lastCall")})
                if not f.get("offset") and not rebuilt:
                    f["neverRead"] = True
                f["done"], f["doneAt"] = True, iso(now)
            settle_rows(sess, settled)
            continue
        read_one(sess, sid, None, f, path, rebuilt, [sf.setdefault("tail", []) for sf in subs.values()])
        if sess.get("program") != "codex":  # subagent transcripts in their own files count for this session (P-c)
            for sub, sub_path in subagent_files(path, sid, state.setdefault("owners", {}), ctx.setdefault("listing", {})):
                key = sid + "/" + sub
                sf = subs.get(key) or files.setdefault(key, {"offset": 0})
                subs[key] = sf
                if sf.get("done"):
                    continue
                others = [f.setdefault("tail", [])] + [o.setdefault("tail", []) for k, o in subs.items() if k != key]
                read_one(dict(sess, sub=sub), sid, sub, sf, sub_path, sf.pop("rebuilt", False), others)
            unknown = unknown_subagent_files(path, sid)
            if unknown:
                f["unknown"] = unknown
            else:
                f.pop("unknown", None)
        seen = parse_ts(f.get("seen") or "") or 0
        if ((sess.get("closed") or sess.get("gone")) and not f.get("open") and not any(sf.get("open") for sf in subs.values())
                and now - seen >= DONE_AFTER):
            f["done"], f["doneAt"] = True, iso(now)
    out.extend(rows)
    for row in out:
        if "seq" in row:
            state["lastSeq"] = state.get("lastSeq", 0) + 1
            row["seq"] = state["lastSeq"]
    if out:
        data = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in out).encode()
        wfd = os.open(turns_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(wfd, data)
            os.fsync(wfd)
        finally:
            os.close(wfd)
        state["turnsBytes"] = os.path.getsize(turns_path)
    for event in ctx["events"]:
        append_line(os.path.join(folder, "sessions.jsonl"), event)
    state["pending"] = ctx.get("pending") or []
    state["problems"] = state_problems(state)
    cc_sig = cc_signature(directory)  # Claude Code's figures change without new session bytes (t-029 R5)
    cc_changed, state["ccSig"] = cc_sig != state.get("ccSig"), cc_sig
    text = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    if text != old_text:
        pa.write_text(state_path, text)
    written = [r for r in out if "seq" in r]
    failed = os.path.join(folder, FAILED_MARK)
    if (written or resummarize or ctx["events"] or state["problems"] != old_problems or cc_changed
            or not os.path.exists(os.path.join(folder, "summary.json")) or os.path.exists(failed)):
        write_summary(directory, state, now, ctx["records"] + ctx["events"])
        if os.path.exists(failed):
            os.unlink(failed)
    if not quiet:
        print("collected {} new segment(s) from {} session(s)".format(len(written), len(sessions)))
    return {"rows": len(written), "sessions": len(sessions)}


def first_last(path, program):
    """(first time, last time, cwd) of a session file, from its first and last 64 KB; times and cwd only."""
    first = last = cwd = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(65536)
            fh.seek(max(0, size - 65536))
            tail = fh.read(65536)
    except OSError:
        return None, None, None
    for chunk, pick in ((head, "first"), (tail, "last")):
        parts = chunk.split(b"\n")
        if pick == "last":
            parts = parts[::-1]
        for raw_line in parts:
            if b'"timestamp"' not in raw_line:
                continue
            try:
                obj = json.loads(raw_line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            ts = payload.get("timestamp") if program == "codex" and obj.get("type") == "session_meta" else obj.get("timestamp")
            epoch = parse_ts(ts) if isinstance(ts, str) else None
            if epoch is None:
                continue
            if pick == "first":
                first = epoch
                c = payload.get("cwd") if program == "codex" else obj.get("cwd")
                cwd = c if isinstance(c, str) else None
            else:
                last = epoch
            del obj, payload
            break
    return first, last, cwd


def first_at_or_after(path, floor):
    """The first line time of a session file at or after `floor` (epoch), read line by line; times only. None when
    there is none. For a --continue / --fork-session file that may begin with copied old history (REVIEW L4)."""
    try:
        with open(path, "rb") as fh:
            for raw_line in fh:
                if b'"timestamp"' not in raw_line:
                    continue
                try:
                    obj = json.loads(raw_line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                ts = obj.get("timestamp") if isinstance(obj, dict) else None
                del obj
                epoch = parse_ts(ts) if isinstance(ts, str) else None
                if epoch is not None and epoch >= floor:
                    return epoch
    except OSError:
        return None
    return None


def copied_start(path, at):
    """The start of a file that a resumed or forked launch at `at` may have written with copied old history: its first
    line time at or after at - 5 s, when that is within the launch window and the file was written since then."""
    try:
        if os.path.getmtime(path) < at - FORK_SLACK:
            return None
    except OSError:
        return None
    t = first_at_or_after(path, at - FORK_SLACK)
    return t if t is not None and t <= at + 120 else None


def codex_rollouts(days=None):
    """(path, session id) of Codex rollout files; only the given UTC days' folders when days is set."""
    root = os.path.join(CODEX_HOME, "sessions")
    folders = [os.path.join(root, *d.split("-")) for d in sorted(days)] if days else None
    out = []
    if folders is None:
        folders = [folder for folder, _dirs, _names in os.walk(root)] if os.path.isdir(root) else []
    for folder in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            m = re.match(r"^rollout-.*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", name)
            if m:
                out.append((os.path.join(folder, name), m.group(1)))
    return out


def claude_files(folder):
    """(path, session id) of the Claude transcripts of one working folder (not subagent files)."""
    base = os.path.join(CLAUDE_HOME, "projects", pa.claude_slug(folder))
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [(os.path.join(base, n), n[:-6]) for n in names if n.endswith(".jsonl") and not n.startswith("agent-")]


def other_spaces_sessions(directory):
    """Session ids that the other spaces on this machine record (costs/sessions.jsonl) or name (session.log): several
    spaces can share one project folder, and those sessions are theirs (VERIFY R-d). Ids only."""
    root, me, out = os.path.join(pa.ROOT, "spaces"), os.path.basename(os.path.normpath(directory)), set()
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if name == me or not pa.ID.match(name):
            continue
        records, _bad = read_lines(os.path.join(root, name, "costs", "sessions.jsonl"))
        out.update(r["session"] for r in records if isinstance(r.get("session"), str))
        try:
            log = pa.read_text(os.path.join(root, name, "session.log"))
        except (OSError, UnicodeDecodeError):
            log = ""
        out.update(re.findall(r"\bsession=([0-9a-f-]{36})\b", log))
    return out


def record_key(r):
    return "{} {}".format(r.get("at"), r.get("terminal"))


def bind_pending(ctx):
    """Binds start records that have no session id yet (Codex; Claude --continue / --fork-session): the first unbound
    file in the record's folder whose start lies in [start - 5 s, start + 120 s]; the earlier start gets the earlier
    file. Unbound after an hour: a problem."""
    records, now = ctx["records"], ctx["now"]
    done = {r.get("for") for r in records if r.get("ev") == "bind" and r.get("for")}
    pending = [r for r in records if r.get("ev") == "start" and not r.get("session") and r.get("bind") == "launch-window"
               and record_key(r) not in done]
    ctx["pending"] = []
    if not pending:
        return
    known = {r.get("session") for r in records if r.get("session")} | other_spaces_sessions(ctx["directory"])
    events = []
    for program in ("codex", "claude"):
        recs = sorted((r for r in pending if (r.get("program") or "claude") == program), key=lambda r: r.get("at") or "")
        if not recs:
            continue
        cands = []
        if program == "codex":
            days = set()
            for r in recs:
                at = parse_ts(r.get("at") or "") or now
                days.update(iso(at + d)[:10] for d in (-86400, 0, 86400))
            files = codex_rollouts(days)
        else:
            files = [f for folder in sorted({r.get("cwd") for r in recs if r.get("cwd")}) for f in claude_files(folder)]
        for path, sid in files:
            if sid in known:
                continue
            first, _last, cwd = first_last(path, program)
            if first is not None:
                if program == "claude":
                    cwd = next((r.get("cwd") for r in recs if os.path.dirname(path).endswith(pa.claude_slug(r.get("cwd") or ""))), cwd)
                cands.append((first, sid, cwd, path))
        cands.sort()
        options, copied = {}, {}
        for r in recs:
            at = parse_ts(r.get("at") or "") or 0
            options[record_key(r)] = [c for c in cands if c[2] == r.get("cwd") and at - FORK_SLACK <= c[0] <= at + 120]
            # A resumed or forked Claude launch may write a new file that begins with copied old history (old times):
            # its start is its first line at or after the launch. Taken only when no file starts in the window.
            late = []
            if program == "claude" and (r.get("resumed") or r.get("fork")):
                for c in cands:
                    if c[2] == r.get("cwd") and c[0] < at - FORK_SLACK:
                        t = copied_start(c[3], at)
                        if t is not None:
                            late.append((t, c[1], c[2], c[3]))
            copied[record_key(r)] = sorted(late)
        used = set()
        for r in recs:
            key = record_key(r)
            free = [c for c in options[key] if c[1] not in used]
            late = not free
            if late:
                free = [c for c in copied[key] if c[1] not in used]
            if not free:
                if now - (parse_ts(r.get("at") or "") or now) > 3600:
                    ctx["pending"].append("{} session for {} not found".format(program, r.get("terminal") or "?"))
                continue
            pick = free[0]
            used.add(pick[1])
            shared = len(options[key]) > 1 or any(pick in v for k, v in options.items() if k != key)
            events.append({"v": 1, "ev": "bind", "at": iso(now), "session": pick[1], "program": program, "for": key,
                           "match": "launch-window-copied" if late else "launch-window",
                           "confidence": "medium" if shared or late else "high"})
    if events:
        ctx["events"].extend(events)
        ctx["records"] = records + events
        ctx["sessions"] = sessions_of(ctx["records"])


# ---- reconstruct ----------------------------------------------------------------------------------

OWNED_WINDOW = 600  # seconds: a journal start entry this close to a launch record of the same terminal is that record's
START = re.compile(r"(?<![A-Za-z0-9_-])(t[0-9a-z]{1,6})\s+\(([A-Za-z]+-\d+-[A-Za-z0-9_-]+)\)")


def scan_tokens(path, program):
    """Total tokens of a whole file, read like the collector reads it (counters only); nothing is kept."""
    f, settled = {"offset": 0}, []
    try:
        reader = read_new({"session": "scan", "program": program}, f, path, os.path.getsize(path), settled)
        reader.settle()
    except OSError:
        return 0
    return sum(total_tokens(seg["tokens"]) for seg in settled)


def reconstruct(directory, dry_run=False, now=None, quiet=False):
    """Bind the space's sessions from before launch records existed (spec 3.8), then collect them."""
    now = time.time() if now is None else now
    agent = pa.read_json(os.path.join(directory, "agent.json"), {}) or {}
    created = parse_ts(agent.get("created") or "") or 0
    folder = costs_dir(directory)
    records, _bad = read_lines(os.path.join(folder, "sessions.jsonl"))
    known = {r.get("session") for r in records if r.get("session")} | other_spaces_sessions(directory)
    ctx = {"directory": directory, "now": now}
    ledger, journal = space_facts(ctx)
    tasks = {t.get("id"): t for t in ledger.get("tasks", []) or [] if t.get("id")}
    worktrees = {}
    for t in list(tasks.values()) + [pa.read_json(p, {}) or {} for p in sorted(
            os.path.join(directory, "trash", n, "task.json") for n in (os.listdir(os.path.join(directory, "trash"))
                                                                       if os.path.isdir(os.path.join(directory, "trash")) else []))]:
        wt = t.get("worktree") if isinstance(t.get("worktree"), str) else None
        if wt:
            worktrees.setdefault(t.get("id"), set()).add(os.path.normpath(wt))
            src = os.path.join(wt, "src")
            for name in sorted(os.listdir(src)) if os.path.isdir(src) else []:
                worktrees[t.get("id")].add(os.path.normpath(os.path.join(src, name)))
    project = os.path.normpath(agent["project"]) if agent.get("project") else None
    folders = sorted({x for x in [project] + [w for ws in worktrees.values() for w in ws] if x})
    logged = set(re.findall(r"\bsession=([0-9a-f-]{36})\b", pa.read_text(os.path.join(directory, "session.log"))))
    cands = {}
    for f_ in folders:
        for path, sid in claude_files(f_):
            cands.setdefault(sid, {"sid": sid, "path": path, "program": "claude", "cwd": f_})
    for sid in sorted(logged - set(cands)):
        path = find_file({"session": sid, "program": "claude", "cwd": project}, None)
        if path:
            cands[sid] = {"sid": sid, "path": path, "program": "claude", "cwd": project}
    for path, sid in codex_rollouts():
        if sid not in cands:
            cands[sid] = {"sid": sid, "path": path, "program": "codex", "cwd": None}
    # Launch records still waiting for their file (Codex, --continue): the collect binds those files, not this.
    bound_for = {r.get("for") for r in records if r.get("ev") == "bind" and r.get("for")}
    waiting = [(r.get("program") or "claude", os.path.normpath(r["cwd"]), parse_ts(r.get("at") or "") or 0,
                bool(r.get("resumed") or r.get("fork"))) for r in records
               if r.get("ev") == "start" and not r.get("session") and r.get("bind") == "launch-window"
               and isinstance(r.get("cwd"), str) and record_key(r) not in bound_for]
    found = []
    for sid, c in sorted(cands.items()):
        if sid in known:
            continue
        first, last, cwd = first_last(c["path"], c["program"])
        if c["program"] == "codex":
            if not cwd or os.path.normpath(cwd) not in folders:
                continue
            c["cwd"] = os.path.normpath(cwd)
        if first is None or first < created - 60:
            continue
        if any(p == c["program"] and w == os.path.normpath(c["cwd"] or "") and (
                at - FORK_SLACK <= first <= at + 120
                or (p == "claude" and again and first < at - FORK_SLACK and copied_start(c["path"], at) is not None))
               for p, w, at, again in waiting):
            continue
        c.update(first=first, last=last or first)
        found.append(c)
    found.sort(key=lambda c: (c["first"], c["sid"]))
    matches, rest = [], []
    mine = set(agent.get("terminals") or []) | {agent.get("terminal")}
    recorded = {w.get("address") for t in tasks.values() for w in t.get("workers") or []}
    live = {}
    for address, info in sorted((pa.live_terminals() or {}).items()):
        pid = (info or {}).get("pid")
        if isinstance(pid, int) and (info.get("parent") in mine or address in recorded):
            pidfile = pa.read_json(os.path.join(CLAUDE_HOME, "sessions", "{}.json".format(pid)), {}) or {}
            if isinstance(pidfile.get("sessionId"), str):
                live[pidfile["sessionId"]] = (address, info.get("name") if isinstance(info.get("name"), str) else None)

    def match(c, how, confidence, **who):
        m = {"v": 1, "ev": "bind", "at": iso(now), "session": c["sid"], "program": c["program"], "kind": "worker", "task": None,
             "name": None, "role": None, "terminal": None, "cwd": c["cwd"], "startedAt": iso(c["first"]),
             "source": "reconstructed", "match": how, "confidence": confidence}
        m.update(who)
        matches.append(m)
    for c in found:
        if c["sid"] in logged:
            match(c, "session-log", "high", kind="pa", terminal=agent.get("terminal"))
        elif c["sid"] in live:
            address, name = live[c["sid"]]
            match(c, "live-pid", "high", name=name, terminal=address, task=task_for(directory, name))
        else:
            rest.append(c)
    starts = []
    for e in journal:
        if re.search(r"(?i)\bstart", e["why"]):
            for address, name in START.findall(e["why"]):
                starts.append({"at": e["at"], "address": address, "name": name, "task": task_for(directory, name) or e["task"]})
    used = {m["terminal"] for m in matches if m["terminal"]}
    # A start entry of a worker that has its own launch record (or an earlier bind) belongs to that session.
    owned = {}
    for r in records:
        if r.get("ev") in ("start", "bind") and isinstance(r.get("terminal"), str):
            at = parse_ts(r.get("startedAt") or r.get("at") or "")
            if at is not None:
                owned.setdefault(r["terminal"], []).append(at)

    def entries_for(c):
        out = []
        for s in starts:
            if s["address"] in used or not (c["first"] - 120 <= s["at"] <= c["first"]):
                continue
            if any(abs(at - s["at"]) <= OWNED_WINDOW for at in owned.get(s["address"], [])):
                continue
            if c["cwd"] == project or c["cwd"] in worktrees.get(s["task"], set()):
                out.append(s)
        return out

    def reported(address, after):
        token = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(address) + r"(?![A-Za-z0-9_-])")
        times = [e["at"] for e in journal if e["at"] > after and e["trigger"] == "report" and token.search(e["why"])]
        return max(times) if times else None
    unmatched, taken = [], set()
    for c in rest:
        if c["sid"] in taken:
            continue
        ents = entries_for(c)
        if len(ents) == 1:
            s = ents[0]
            match(c, "cwd+start", "medium", name=s["name"], terminal=s["address"], task=s["task"])
            used.add(s["address"])
            continue
        if len(ents) == 2:
            peers = [p for p in rest if p is not c and p["sid"] not in taken and abs(p["first"] - c["first"]) <= 2 and p["cwd"] == c["cwd"]]
            if len(peers) == 1 and {s["address"] for s in entries_for(peers[0])} == {s["address"] for s in ents}:
                pair = sorted([c, peers[0]], key=lambda x: x["last"])
                ends = sorted(ents, key=lambda s: reported(s["address"], s["at"]) or 0)
                if all(reported(s["address"], s["at"]) for s in ends):
                    for sess_, s in zip(pair, ends):
                        match(sess_, "end-time-pair", "low", name=s["name"], terminal=s["address"], task=s["task"])
                        taken.add(sess_["sid"])
                        used.add(s["address"])
                    continue
        unmatched.append(c)
    um = {"v": 1, "ev": "unmatched", "at": iso(now), "sessions": len(unmatched),
          "tokens": sum(scan_tokens(c["path"], c["program"]) for c in unmatched), "ids": sorted(short(c["sid"]) for c in unmatched)}
    if not quiet:
        for m in matches:
            print("{:<14} {} {:<6} {} {}".format(m["match"], short(m["session"]), m["kind"], m["terminal"] or "-", m["task"] or "(no task)"))
        print("unmatched: {} session(s), {} tokens (not added to totals)".format(um["sessions"], um["tokens"]))
        if dry_run:
            print("dry run: nothing written")
    if dry_run:
        return {"matches": matches, "unmatched": um}
    for m in matches:
        append_line(os.path.join(folder, "sessions.jsonl"), m)
    last = [r for r in records if r.get("ev") == "unmatched"]
    if (um["sessions"] or last) and (not last or (last[-1].get("sessions"), last[-1].get("tokens"), last[-1].get("ids")) !=
                                     (um["sessions"], um["tokens"], um["ids"])):
        append_line(os.path.join(folder, "sessions.jsonl"), um)
    if matches or os.path.exists(os.path.join(folder, "sessions.jsonl")):
        collect(directory, now=now, quiet=True)
    return {"matches": matches, "unmatched": um}


# ---- summary.json and the report ------------------------------------------------------------------

SUMMARY_MAX = 65536
DAYS, SESSIONS, PASSES, HOURS = 92, 200, 20, 48


# ---- days in a timezone (t-036): on disk everything stays UTC; only the day buckets are cut in a zone ----------------
# A zone is {"zone": name, "offsets": [[epoch, minutes east of UTC in force from it on], ...]}, the first from 0. The
# panel passes its own (its machine's getTimezoneOffset over the last 10 years .. now + 35 d, so daylight saving is in
# it and no zone database is needed here); costs/zone.json keeps it for the collects that pass none. The report uses
# the terminal's (time.localtime, which honours TZ) back to its oldest row, or UTC with --utc. "from" (absent: 0): where
# the table starts; a row before it is put under its UTC day, and that day is marked utc (never a guessed midnight).
UTC_ZONE = {"zone": "UTC", "offsets": [[0, 0]]}
ZONE_BACK, ZONE_AHEAD, ZONE_MAX = (DAYS + 8) * 86400, 35 * 86400, 64
ZONE_NAME = re.compile(r"^[A-Za-z0-9_+\-/:.]{1,64}$")


def offset_at(offsets, epoch):
    """Minutes east of UTC in force at `epoch` (before the first entry: the first entry's)."""
    minutes = offsets[0][1]
    for start, m in offsets:
        if epoch < start:
            break
        minutes = m
    return minutes


def before_table(zone, epoch):
    """Whether `epoch` is before where the zone's table starts (its day is then the UTC day)."""
    return epoch < zone.get("from", 0)


def day_in(zone, text):
    """The day (YYYY-MM-DD) of an ISO UTC time in `zone` (its UTC day before the table's start); None when not one."""
    epoch = parse_ts(text or "")
    if epoch is None:
        return None
    return iso(epoch + (0 if before_table(zone, epoch) else offset_at(zone["offsets"], epoch) * 60))[:10]


def offset_name(minutes):
    return "UTC{}{:02d}:{:02d}".format("-" if minutes < 0 else "+", abs(minutes) // 60, abs(minutes) % 60)


def machine_zone(now, oldest=None):
    """This machine's zone (the terminal's: TZ is honoured) as a table over now - ZONE_BACK (or a day before `oldest`,
    when earlier) .. now + ZONE_AHEAD. Over ZONE_MAX entries: the newest, with "from" where they start."""
    def off(t):
        return int(time.localtime(t).tm_gmtoff // 60)
    t = int(now) - ZONE_BACK if oldest is None else min(int(now) - ZONE_BACK, int(oldest) - 86400)
    end, step = int(now) + ZONE_AHEAD, 6 * 3600  # changes are months apart
    offsets = [[0, off(t)]]
    while t < end:
        nxt = t + step
        if off(nxt) != offsets[-1][1]:
            lo, hi = t, nxt  # off(lo) is the old offset, off(hi) the new one: the second it changes
            while hi - lo > 1:
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if off(mid) == offsets[-1][1] else (lo, mid)
            offsets.append([hi, off(hi)])
        t = nxt
    if offsets == [[0, 0]]:
        return dict(UTC_ZONE)
    name = time.strftime("%Z", time.localtime(now))
    zone = {"zone": name if ZONE_NAME.match(name or "") else offset_name(off(int(now))), "offsets": offsets}
    if len(offsets) > ZONE_MAX:
        kept = offsets[-ZONE_MAX:]
        zone.update(offsets=[[0, kept[0][1]]] + kept[1:], **{"from": kept[0][0]})
    return zone


def parse_zone(offsets_text, name=None, known_from=None):
    """The panel's --zone-offsets "0:<min>,<epoch>:<min>,...", --zone-name and --zone-from (where its table starts),
    checked; pa.Problem when not one."""
    offsets = []
    for part in (offsets_text or "").split(","):
        m = re.match(r"^(\d{1,12}):(-?\d{1,4})$", part.strip())
        if not m:
            raise pa.Problem("--zone-offsets is <epoch>:<minutes>[,...], the first from 0")
        offsets.append([int(m.group(1)), int(m.group(2))])
    if not offsets or offsets[0][0] != 0 or len(offsets) > ZONE_MAX:
        raise pa.Problem("--zone-offsets: from 0, at most {} entries".format(ZONE_MAX))
    if any(b[0] <= a[0] for a, b in zip(offsets, offsets[1:])) or any(abs(m) > 900 for _e, m in offsets):
        raise pa.Problem("--zone-offsets: epochs ascending, offsets within ±15 h")
    if name is not None and not ZONE_NAME.match(name):
        raise pa.Problem("--zone-name is letters, digits and _+-/:. only")
    if known_from is not None and not re.match(r"^\d{1,12}$", str(known_from)):
        raise pa.Problem("--zone-from is an epoch in seconds")
    zone = {"zone": name or offset_name(offsets[-1][1]), "offsets": offsets}
    if known_from is not None and int(known_from):
        zone["from"] = int(known_from)
    return zone


def load_zone(directory):
    """The zone the panel last passed (costs/zone.json), else UTC."""
    z = pa.read_json(os.path.join(costs_dir(directory), "zone.json"), None)
    try:
        return parse_zone(",".join("{}:{}".format(e, m) for e, m in z["offsets"]), z["zone"], z.get("from"))
    except (TypeError, KeyError, ValueError, pa.Problem):
        return dict(UTC_ZONE)


def save_zone(directory, zone):
    """Keeps the panel's zone for the collects that pass none; True when it changed. Its start ("from") moves on each
    day the panel runs: kept, but not a change (the summary is not rewritten for it alone)."""
    def table(z):
        return {k: v for k, v in z.items() if k != "from"}
    path = os.path.join(costs_dir(directory), "zone.json")
    old = load_zone(directory) if os.path.exists(path) else None
    if old == zone:
        return False
    pa.write_text(path, compact(zone))
    return old is None or table(old) != table(zone)


def zone_of(s):
    """A summary's zone (an older summary's: UTC)."""
    z = s.get("days") if isinstance(s.get("days"), dict) else None
    try:
        return parse_zone(",".join("{}:{}".format(e, m) for e, m in z["offsets"]), z["zone"], z.get("from"))
    except (TypeError, KeyError, ValueError, pa.Problem):
        return dict(UTC_ZONE)


def local_clock(text, zone):
    """An ISO UTC time as "YYYY-MM-DD HH:MM <zone>"."""
    epoch = parse_ts(text or "")
    if epoch is None:
        return str(text)
    return "{} {}".format(iso(epoch + offset_at(zone["offsets"], epoch) * 60)[:16].replace("T", " "), zone["zone"])


def ledger_rows(directory):
    """The ledger rows that count: a void line drops the earlier rows of its session."""
    rows, _bad = read_lines(os.path.join(costs_dir(directory), "turns.jsonl"))
    voids = {}
    for r in rows:
        if r.get("ev") == "void":  # a subagent file's void ("sub") drops the rows of that file only
            voids.setdefault((r.get("session"), r.get("sub")), []).append(r.get("upTo") or 0)
    out = []
    for r in rows:
        if "seq" not in r or not isinstance(r.get("tokens"), dict):
            continue
        if any(r["seq"] <= up for up in voids.get((r.get("session"), r.get("sub")), [])):
            continue
        out.append(r)
    return out


def space_prices(directory):
    """(table, problem, profile name) for the space's profile."""
    agent = pa.read_json(os.path.join(directory, "agent.json"), {}) or {}
    try:
        profile = pa.load_profile(agent.get("profile"))
    except pa.Problem as e:
        return None, str(e), agent.get("profile")
    table, problem = load_prices(profile)
    return table, problem, profile.get("name")


def account_chain(directory=None):
    """(label, by) of the billable account (t-032 R-A1): the space's agent.json costs.account, the machine's
    config costs.account, the profile's billing.account (the space's profile, else the default one), "default".
    A value that is not a label (or holds 12 digits) is skipped."""
    agent = {}
    if directory:
        try:
            agent = pa.read_json(os.path.join(directory, "agent.json"), {}) or {}
        except (OSError, ValueError):
            agent = {}
    steps = [("space", ((agent.get("costs") or {}) if isinstance(agent.get("costs"), dict) else {}).get("account"))]
    cfg = pa.config()
    steps.append(("machine", ((cfg.get("costs") or {}) if isinstance(cfg.get("costs"), dict) else {}).get("account")))
    try:
        prof = pa.load_profile(agent.get("profile") if agent.get("profile") else None)
        block = prof.get("billing") if isinstance(prof.get("billing"), dict) else {}
        steps.append(("profile", block.get("account")))
    except pa.Problem:
        pass
    for by, value in steps:
        if fact_label("account", value):
            return value, by
    return "default", "default"


def space_rules(directory):
    """(rules, problems) of the space's profile's pricing block (t-032); ({}, []) without a profile."""
    agent = pa.read_json(os.path.join(directory, "agent.json"), {}) or {}
    try:
        return pricing_rules(pa.load_profile(agent.get("profile")))
    except pa.Problem:
        return None, []


def costs_account(directory=None):
    return account_chain(directory)[0]


class Bucket:
    __slots__ = ("tokens", "money", "priced", "calls", "recon", "kinds", "unpriced")

    def __init__(self):
        self.tokens, self.money, self.priced, self.calls, self.recon, self.kinds = 0, 0.0, False, 0, 0.0, None
        self.unpriced = 0

    def partial(self):
        """The unpriced tokens of an entry that also has a priced part (its money is the priced part only), else 0."""
        return int(round(self.unpriced)) if self.priced else 0

    def add(self, tokens, money, calls, recon, kinds=None):
        self.tokens += tokens
        self.calls += calls
        if money is None:
            self.unpriced += tokens
        else:
            self.money += money
            self.priced = True
            if recon:
                self.recon += money
        if kinds is not None:
            self.kinds = self.kinds or zero()
            for k in KINDS:
                self.kinds[k] += kinds.get(k, 0)

    def out(self, key, **extra):
        d = {"key": key, "tokens": int(round(self.tokens)), "money": round(self.money, 6) if self.priced else None,
             "calls": int(round(self.calls))}
        if self.partial():
            d["unpricedTokens"] = self.partial()
        if self.recon:
            d["reconstructed"] = round(self.recon, 6)
        d.update(extra)
        return d


def group(buckets, key):
    b = buckets.get(key)
    if b is None:
        b = buckets[key] = Bucket()
    return b


def by_money(buckets, **extra_for):
    items = [b.out(k, **(extra_for.get(k) or {})) for k, b in buckets.items()]
    items.sort(key=lambda e: (-(e["money"] or 0), -e["tokens"], str(e["key"])))
    return items


def drill(buckets, keep=8):
    """The costliest `keep` entries of a breakdown, as {key, tokens, money} (and unpricedTokens when partial)."""
    return [{k: v for k, v in e.items() if k in ("key", "tokens", "money", "unpricedTokens")} for e in by_money(buckets)[:keep]]


def read_cc(directory):
    """(records, problems): costs/cc/*.json, newest first by mtime, at most CC_MAX_FILES, each checked again (t-029 R5)."""
    folder = os.path.join(costs_dir(directory), "cc")
    entries = []
    try:
        for e in os.scandir(folder):
            if e.name.startswith(".") or not e.name.endswith(".json"):
                continue
            try:
                if e.is_file():
                    entries.append((e.stat().st_mtime, e.name))
            except OSError:
                continue
    except OSError:
        return [], []
    entries.sort(reverse=True)
    problems = []
    if len(entries) > CC_MAX_FILES:
        problems.append("costs/cc: {} older file(s) beyond the {} newest were not read".format(len(entries) - CC_MAX_FILES, CC_MAX_FILES))
    out, bad = [], 0
    for _mtime, name in entries[:CC_MAX_FILES]:
        try:
            with open(os.path.join(folder, name), "rb") as f:
                rec = cc_clean(json.loads(f.read().decode("utf-8")), name[:-len(".json")])
        except (OSError, ValueError, UnicodeDecodeError):
            rec = None
        if rec is None:
            bad += 1
        else:
            out.append(rec)
    if bad:
        problems.append("costs/cc: {} file(s) that are not Claude Code figure records were skipped".format(bad))
    return out, problems


def cc_signature(directory):
    """[files, newest mtime in ns, bytes] of costs/cc/: a collect rewrites the summary when it changes."""
    count, newest, size = 0, 0, 0
    try:
        for e in os.scandir(os.path.join(costs_dir(directory), "cc")):
            if e.name.startswith(".") or not e.name.endswith(".json"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            count, newest, size = count + 1, max(newest, st.st_mtime_ns), size + st.st_size
    except OSError:
        pass
    return [count, newest, size]


def cc_join(cc, records, sessions, sess_b, since=None, until=None, zone=None):
    """Claude Code figure records joined to the summary's sessions (AC5.2): (parts: sid -> [(record, cleared)],
    unmatched count, unmatched money). By id; else, for an id no record names (a /clear), the latest start record of the
    same terminal at or before the record's first reading; else unmatched. A known session without rows is left out."""
    starts = {}
    for x in records:
        if x.get("ev") == "start" and isinstance(x.get("session"), str) and isinstance(x.get("terminal"), str):
            starts.setdefault(x["terminal"], []).append((x.get("at") or "", x["session"]))
    known = set(sessions) | set(sess_b)
    parts, um_n, um_money = {}, 0, 0.0
    for rec in cc:
        sid, cleared = rec["session"], False
        if sid not in sess_b:
            if sid in known:
                continue
            found = [c for c in starts.get(rec.get("terminal") or "", []) if c[0] <= rec["first"]]
            if not found:
                day = day_in(zone or UTC_ZONE, rec["at"]) or rec["at"][:10]
                if (not since or day >= since) and (not until or day <= until):
                    um_n += 1
                    um_money += cc_figure(rec) or 0.0
                continue
            sid, cleared = max(found)[1], True
            if sid not in sess_b:
                continue
        parts.setdefault(sid, []).append((rec, cleared))
    return parts, um_n, um_money


def summarize(directory, rows, records, table, price_problem, profile_name, now, problems=(), cc=None, since=None, until=None,
              zone=None, rules=None):
    """The summary.json object (spec 2.3) for the given ledger rows. cc: Claude Code's figure records (default: read
    costs/cc/); they add cc fields beside est., never to it (t-029 R5). zone: byDay's days (t-036; default UTC); a row
    counts on the day of its start. byDayModel keeps UTC days (the billed rows' days)."""
    zone = zone or UTC_ZONE
    problems = list(problems)
    chain = account_chain(directory)
    if rules is None:
        try:
            rules, rule_problems = pricing_rules(pa.load_profile(profile_name) if profile_name else {})
        except pa.Problem:
            rules, rule_problems = None, []
        problems.extend(p for p in rule_problems if p not in problems)
    if cc is None:
        cc, cc_problems = read_cc(directory)
        problems.extend(cc_problems)
    sessions = sessions_of(records)
    try:
        ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    except (OSError, ValueError):
        ledger = {}
    tasks = {t.get("id"): t for t in ledger.get("tasks", []) or [] if isinstance(t, dict) and t.get("id")}
    total, recon_money, unpriced, missing = Bucket(), 0.0, 0, set()
    kinds_total = zero()
    acts, task_b, phases, task_phase, task_sessions, task_split = {}, {}, {}, {}, {}, set()
    act_tasks, reasons = {}, {}  # activity -> task -> Bucket (the Costs view's drill); unattributed's reasons
    dims = {d: {} for d in ("kind", "role", "program", "model", "machine")}
    days, day_acts, day_model, hours, passes, sess_b, sess_info = {}, {}, {}, {}, {}, {}, {}
    utc_days = set()  # byDayModel's own days (UTC)
    cut_utc = set()  # byDay's days holding a row from before the zone table's start (cut in UTC, marked)
    day_act_b = {}  # day -> activity -> Bucket: the day drill's activities with their unpriced part (t-028)
    sess_tasks, sess_split = {}, set()  # the tasks of each session's rows; sessions with a split row
    fact_b = {f: {} for f in SCOPE_FIELDS}  # t-032: by provider, endpoint, region, account
    scope_b, scope_info, day_model_as = {}, {}, {}
    hour_floor = iso(now - HOURS * 3600)[:13]
    for r in rows:
        tokens = {k: num(r["tokens"].get(k)) for k in KINDS}
        t = total_tokens(tokens)
        sess = sessions.get(r["session"]) or {}
        rf = row_facts(r, sess, rules, chain)
        facts = tuple(rf[f] for f in SCOPE_FIELDS)
        entry, priced_as = (None, None)
        if table:
            # a provider or endpoint by default is left to the price file's default scope
            entry, priced_as = lookup(table, r.get("model"), r.get("from"),
                                      (None if rf["providerBy"] == "default" else facts[0],
                                       None if rf["endpointBy"] == "default" else facts[1], facts[2], facts[3]))
            if rf["providerBy"] == "default" and facts[0] is None:
                facts = ((table.get("defaultScope") or (None,))[0],) + facts[1:]
        m = money(r["tokens"] or {}, r.get("long") or {}, entry)
        rec = r.get("source") == "reconstructed"
        calls = num(r.get("calls"))
        if m is None and t:
            unpriced += t
            missing.add(r.get("model") or "(unknown model)")
        if m is not None and rec:
            recon_money += m
        total.add(t, m, calls, rec)
        for k in KINDS:
            kinds_total[k] += tokens[k]
        # by activity: a rebuild's cache write moves to cache-rebuild
        rb = r.get("rebuild") or {}
        rb_tokens = {"in": 0, "out": 0, "cw5m": num(rb.get("cw5m")), "cw1h": num(rb.get("cw1h")), "cr": 0}
        rb_t = rb_tokens["cw5m"] + rb_tokens["cw1h"]
        rb_m = None
        if rb_t:
            rb_m = money(rb_tokens, zero(), entry) if m is not None else None
            group(acts, "cache-rebuild").add(rb_t, rb_m, 0, rec)
        act = r.get("activity") or "unattributed"
        group(acts, act).add(t - rb_t, None if m is None else m - (rb_m or 0), calls, rec)
        if act == "unattributed":
            group(reasons, r.get("reason") or "(no reason)").add(t - rb_t, None if m is None else m - (rb_m or 0), calls, rec)
        # by task (split rows by their weights), phase, sessions per task
        split = r.get("split") if isinstance(r.get("split"), list) and r.get("split") else [{"task": r.get("task"), "w": 1.0}]
        wsum = sum(float(s.get("w") or 0) for s in split) or 1.0
        if len(split) > 1:
            sess_split.add(r["session"])
        sess_tasks.setdefault(r["session"], set()).update(s.get("task") or "(no task)" for s in split)
        for s in split:
            w, key = float(s.get("w") or 0) / wsum, s.get("task") or "(no task)"
            group(act_tasks.setdefault(act, {}), key).add((t - rb_t) * w, None if m is None else (m - (rb_m or 0)) * w, calls * w, rec)
            if rb_t:
                group(act_tasks.setdefault("cache-rebuild", {}), key).add(rb_t * w, None if rb_m is None else rb_m * w, 0, rec)
        for s in split:
            w = float(s.get("w") or 0) / wsum
            key = s.get("task") or "(no task)"
            group(task_b, key).add(t * w, None if m is None else m * w, calls * w, rec)
            if len(split) > 1:
                task_split.add(key)
            group(task_phase.setdefault(key, {}), r.get("phase") or "(none)").add(t * w, None if m is None else m * w, calls * w, rec)
            task_sessions.setdefault(key, [])
            if short(r["session"]) not in task_sessions[key]:
                task_sessions[key].append(short(r["session"]))
        group(phases, r.get("phase") or "(none)").add(t, m, calls, rec)
        for f, v in zip(SCOPE_FIELDS, facts):
            group(fact_b[f], "?" if v is None else v).add(t, m, calls, rec)
        skey = facts_text(facts)
        group(scope_b, skey).add(t, m, calls, rec)
        si = scope_info.setdefault(skey, {"scope": list(facts), "pricedAs": [], "by": {k: rf[k] for k in ("providerBy", "endpointBy", "regionBy", "accountBy")}})
        if priced_as is not None and priced_as not in si["pricedAs"] and len(si["pricedAs"]) < 4:
            si["pricedAs"].append(priced_as)
        for k in list(si["by"]):
            if si["by"][k] != rf[k]:
                si["by"][k] = "mixed"
        for dim, value in (("kind", r.get("kind")), ("role", r.get("role")), ("program", r.get("program")),
                           ("model", r.get("model")), ("machine", sess.get("host"))):
            group(dims[dim], value or ("unknown" if dim == "machine" else "(none)")).add(t, m, calls, rec)
        utc_day = r.get("day") or (r.get("from") or "")[:10]
        utc_days.add(utc_day)
        day = day_in(zone, r.get("from")) or utc_day
        start = parse_ts(r.get("from") or "")
        if start is not None and before_table(zone, start) and zone["offsets"] != [[0, 0]]:
            cut_utc.add(day)
        group(days, day).add(t, m, calls, rec)
        da = day_acts.setdefault(day, {})
        if m is not None:
            da[act] = da.get(act, 0.0) + m - (rb_m or 0)
            if rb_m:
                da["cache-rebuild"] = da.get("cache-rebuild", 0.0) + rb_m
        dab = day_act_b.setdefault(day, {})
        group(dab, act).add(t - rb_t, None if m is None else m - (rb_m or 0), calls, rec)
        if rb_t:
            group(dab, "cache-rebuild").add(rb_t, rb_m, 0, rec)
        dm_key = (utc_day, r.get("model") or "(unknown model)") + tuple("" if v is None else v for v in facts)
        group(day_model, dm_key).add(t, m, calls, rec, tokens)
        if dm_key not in day_model_as or day_model_as[dm_key][1] is None:
            day_model_as[dm_key] = (facts, priced_as, rf["accountBy"])
        hour = (r.get("from") or "")[:13]
        if hour >= hour_floor:
            group(hours, hour).add(t, m, calls, rec)
        if r.get("kind") == "pa" and r.get("pass") is not None:
            p = passes.setdefault(r["pass"], {"pass": r["pass"], "trigger": r.get("trigger"), "from": r.get("from"), "money": None,
                                               "tokens": 0, "rebuild": False, "unpricedTokens": 0})
            if m is not None:
                p["money"] = (p["money"] or 0.0) + m
            else:
                p["unpricedTokens"] += t
            p["tokens"] += t
            p["rebuild"] = p["rebuild"] or bool(rb_t)
            p["from"] = min(p["from"] or r.get("from"), r.get("from"))
        group(sess_b, r["session"]).add(t, m, calls, rec)
        info = sess_info.setdefault(r["session"], {"from": r.get("from"), "to": r.get("to"), "models": {}, "tasks": {}, "role": None})
        info["from"] = min(info["from"], r.get("from"))
        info["to"] = max(info["to"], r.get("to"))
        info["models"][r.get("model")] = info["models"].get(r.get("model"), 0) + t
        info["tasks"][r.get("task")] = info["tasks"].get(r.get("task"), 0) + t
        info["role"] = r.get("role") or info["role"]
        info["row"] = r
        sc = info.setdefault("scopes", {}).setdefault(skey, [0, facts, None])  # J1d (d-117 limit 9): tokens per scope
        sc[0] += t
        if sc[2] is None:
            sc[2] = priced_as
    cc_parts, um_n, um_money = cc_join(cc, records, sessions, sess_b, since, until, zone) if cc else ({}, 0, 0.0)
    cc_money, task_cc = {}, {}
    for sid, parts in cc_parts.items():
        figures = [cc_figure(rec) for rec, _cleared in parts if cc_figure(rec) is not None]
        if not figures:
            continue
        cc_money[sid] = sum(figures)
        tasks_of = sess_tasks.get(sid) or set()
        if sid not in sess_split and len(tasks_of) == 1:  # a session of one task alone: its figure is that task's
            t = task_cc.setdefault(next(iter(tasks_of)), [0.0, 0])
            t[0] += cc_money[sid]
            t[1] += 1
    task_extra = {}
    for key in task_b:
        t = tasks.get(key) or {}
        extra = {"state": t.get("state") if key in tasks else ("deleted" if key != "(no task)" else None),
                 "phases": [{k: v for k, v in ph.items() if k != "calls"} for ph in by_money(task_phase.get(key, {}))],
                 "sessions": task_sessions.get(key, [])[:20]}
        if isinstance(t.get("title"), str):
            extra["title"] = t["title"][:80]
        if key in task_split:
            extra["split"] = True
        if key in task_cc:
            extra.update(cc=round(task_cc[key][0], 6), ccSessions=task_cc[key][1], ccOf=len(task_sessions.get(key, [])))
        task_extra[key] = extra
    day_list = sorted(days, reverse=True)
    older = Bucket()
    for d in day_list[DAYS:]:
        b = days[d]
        older.tokens += b.tokens
        older.calls += b.calls
        older.unpriced += b.unpriced
        if b.priced:
            older.money += b.money
            older.priced = True
            older.recon += b.recon
    # byActivity: the priced money of each activity (as before); activities: every activity with its tokens, money
    # (None: no price) and unpricedTokens, for the day drill's marks (t-028).
    by_day = [days[d].out(d, byActivity={k: round(v, 6) for k, v in sorted(day_acts.get(d, {}).items())},
                          activities=drill(day_act_b.get(d, {}), keep=20), **({"utc": True} if d in cut_utc else {}))
              for d in day_list[:DAYS]]
    if day_list[DAYS:]:
        by_day.append(older.out("older", days=len(day_list) - DAYS))
    kept_days = set(sorted(utc_days, reverse=True)[:DAYS])
    by_day_model = [dict(day=k[0], model=k[1], tokens=b.kinds or zero(), money=round(b.money, 6) if b.priced else None,
                         **({"unpricedTokens": b.partial()} if b.partial() else {}),
                         **dict(zip(SCOPE_FIELDS, day_model_as[k][0])), accountBy=day_model_as[k][2], pricedAs=day_model_as[k][1])
                    for k, b in sorted(day_model.items(), reverse=True) if k[0] in kept_days]
    sess_list = []
    for sid, b in sess_b.items():
        info, sess = sess_info[sid], sessions.get(sid) or {}
        r = info["row"]
        top = max(info["scopes"].values(), key=lambda v: v[0])  # its largest scope by tokens; ties: the first seen
        state = "gone" if sess.get("gone") else ("closed" if sess.get("closed") else "open")
        entry = b.out(short(sid), kind=r.get("kind"), role=info["role"], program=r.get("program"),
                      task=max(info["tasks"], key=lambda k: info["tasks"][k]),
                      model=max(info["models"], key=lambda k: info["models"][k]), **{"from": info["from"]},
                      to=info["to"], state=state, source=r.get("source"), match=r.get("match"),
                      **dict(zip(SCOPE_FIELDS, top[1])), pricedAs=top[2],
                      **({"scopes": len(info["scopes"])} if len(info["scopes"]) > 1 else {}))
        if sid in cc_money:
            parts = [(rec, cleared) for rec, cleared in cc_parts[sid] if cc_figure(rec) is not None]
            entry["cc"], entry["ccAt"] = round(cc_money[sid], 6), max(rec["at"] for rec, _c in parts)
            segments = sum(rec["segments"] for rec, cleared in parts if not cleared)
            if segments > 1:
                entry["ccSegments"] = segments
            cleared = sum(1 for _rec, c in parts if c)
            if cleared:
                entry["ccCleared"] = cleared
            corrected = sum(1 for rec, _c in parts if rec.get("corrected"))
            if corrected:  # t-043: a written record that counted a resumed session's history again, read as counted once
                entry["ccCorrected"] = corrected
        sess_list.append(entry)
    sess_list.sort(key=lambda e: e["to"] or "", reverse=True)
    um = [x for x in records if x.get("ev") == "unmatched"]
    if price_problem:
        problems.append(price_problem)
    problems.extend(w for w in (table or {}).get("warnings") or [] if w not in problems)
    totals = {"tokens": {"in": kinds_total["in"], "out": kinds_total["out"], "cw": kinds_total["cw5m"] + kinds_total["cw1h"],
                         "cr": kinds_total["cr"], "all": total_tokens(kinds_total)},
              "money": round(total.money, 6) if total.priced else None, "unpricedTokens": unpriced,
              "reconstructedMoney": round(recon_money, 6) if total.priced else None, "calls": total.calls}
    if cc:  # only where the space has Claude Code figure records at all: older viewers and other spaces see no change
        totals["cc"] = {"money": round(sum(cc_money.values()), 6) if cc_money else None, "sessions": len(cc_money),
                        "claudeSessions": sum(1 for sid in sess_b if (sess_info[sid]["row"].get("program") or "claude") == "claude"),
                        "allSessions": len(sess_b),
                        "unmatched": {"sessions": um_n, "money": round(um_money, 6) if um_n else None}, "ccCurrency": "USD"}
    return {
        "v": 1, "space": os.path.basename(directory), "updated": iso(now), "account": chain[0], "accountBy": chain[1],
        "prices": {"profile": profile_name, "source": (table or {}).get("source"), "asOf": (table or {}).get("asOf"),
                   "currency": (table or {}).get("currency"), "missing": sorted(missing)},
        "totals": totals,
        "hours": [[h, dict({"tokens": int(b.tokens), "money": round(b.money, 6) if b.priced else None},
                           **({"unpricedTokens": b.partial()} if b.partial() else {}))] for h, b in sorted(hours.items())],
        "byActivity": by_money(acts, **{k: dict({"tasks": drill(act_tasks.get(k, {}))}, **({"reasons": drill(reasons)} if k == "unattributed" else {}))
                                         for k in acts}),
        "byTask": by_money(task_b, **task_extra),
        "byPhase": by_money(phases),
        "byKind": {d: by_money(v) for d, v in dims.items()},
        "byDay": by_day,
        "days": dict({"zone": zone["zone"], "offsets": zone["offsets"]}, **({"from": zone["from"]} if zone.get("from") else {})),
        "byDayModel": by_day_model,
        "byProvider": by_money(fact_b["provider"]), "byEndpoint": by_money(fact_b["endpoint"]),
        "byRegion": by_money(fact_b["region"]), "byAccount": by_money(fact_b["account"]),
        "byScope": by_money(scope_b, **scope_info), "scopes": len(scope_b),
        "passes": sorted(({**{k: v for k, v in p.items() if k != "unpricedTokens"},
                           "money": None if p["money"] is None else round(p["money"], 6),
                           **({"unpricedTokens": p["unpricedTokens"]} if p["money"] is not None and p["unpricedTokens"] else {})}
                          for p in passes.values()), key=lambda p: -(p["money"] or 0))[:PASSES],
        "sessions": sess_list[:SESSIONS],
        "unmatched": {"sessions": um[-1].get("sessions", 0), "tokens": um[-1].get("tokens", 0)} if um else {"sessions": 0, "tokens": 0},
        "problems": problems[:20],
    }


FAILED_MARK = ".collect-failed"  # in costs/: the next good collect rewrites summary.json, which clears the entry


def note_collect_failure(directory, why, now=None):
    """`pa-costs collect` refused or failed: summary.json says why (REVIEW-2 RL3), keeping the last good figures. With
    no summary yet, a short one without totals (so nothing reads as 0). Never raises."""
    try:
        now = time.time() if now is None else now
        folder = costs_dir(directory)
        if not os.path.isdir(folder):
            return
        path = os.path.join(folder, "summary.json")
        try:
            s = pa.read_json(path, None)
        except (OSError, ValueError):
            s = None
        if not isinstance(s, dict):
            s = {"v": 1, "space": os.path.basename(os.path.normpath(directory)), "updated": iso(now), "account": costs_account(directory)}
        s["collect"] = {"ok": False, "at": iso(now), "why": why}
        rest = [p for p in s.get("problems") or [] if isinstance(p, str) and not p.startswith("collect failed: ")]
        s["problems"] = (["collect failed: " + why] + rest)[:20]
        pa.write_text(path, compact(s))
        pa.write_text(os.path.join(folder, FAILED_MARK), iso(now) + "\n")
    except Exception:  # noqa: BLE001 - the failure itself is what is reported
        pass


def compact(value):
    return json.dumps(value, separators=(",", ":")) + "\n"


def fold_tasks(entries, keep):
    """The costliest `keep` tasks, and the rest folded into one "(other tasks)" entry (totals unchanged)."""
    if len(entries) <= keep:
        return entries
    rest = entries[keep:]
    priced = [e["money"] for e in rest if e["money"] is not None]
    other = {"key": "(other tasks)", "tokens": sum(e["tokens"] for e in rest), "money": round(sum(priced), 6) if priced else None,
             "calls": sum(e["calls"] for e in rest), "tasks": len(rest) + sum(e.get("tasks", 0) for e in rest if e["key"] == "(other tasks)"),
             "phases": [], "sessions": []}
    unpriced = sum(e.get("unpricedTokens", 0) if e["money"] is not None else e["tokens"] for e in rest)
    if priced and unpriced:
        other["unpricedTokens"] = unpriced
    recon = sum(e.get("reconstructed", 0) for e in rest)
    if recon:
        other["reconstructed"] = round(recon, 6)
    with_cc = [e for e in rest if e.get("cc") is not None]
    if with_cc:
        other.update(cc=round(sum(e["cc"] for e in with_cc), 6), ccSessions=sum(e.get("ccSessions", 0) for e in with_cc),
                     ccOf=sum(e.get("ccOf", 0) for e in with_cc))
    return entries[:keep] + [other]


def shrink(s):
    """One step towards SUMMARY_MAX: fewer sessions, then shorter session lists per task, then fewer tasks."""
    if s["sessions"]:
        s["sessions"] = s["sessions"][:len(s["sessions"]) * 3 // 4]
    elif any(t["sessions"] for t in s["byTask"]):
        for t in s["byTask"]:
            t["sessions"] = t["sessions"][:len(t["sessions"]) // 2]
    elif s["passes"]:
        s["passes"] = s["passes"][:len(s["passes"]) // 2]
    elif len(s["byTask"]) > 10:
        s["byTask"] = fold_tasks(s["byTask"], len(s["byTask"]) * 3 // 4)
    elif s["byDayModel"]:
        s["byDayModel"] = s["byDayModel"][:len(s["byDayModel"]) * 3 // 4]
    else:
        return False
    s["truncated"] = True
    return True


def write_summary(directory, state=None, now=None, records=None):
    """Rewrites costs/summary.json (≤ 64 KB: lists are shortened, never the totals, until it fits)."""
    now = time.time() if now is None else now
    folder = costs_dir(directory)
    if state is None:
        state = pa.read_json(os.path.join(folder, "state.json"), {}) or {}
    bad = 0
    if records is None:
        records, bad = read_lines(os.path.join(folder, "sessions.jsonl"))
    table, problem, name = space_prices(directory)
    problems = list(state.get("problems") or [])
    if bad:
        problems.append("{} lines of sessions.jsonl are not JSON".format(bad))
    s = summarize(directory, ledger_rows(directory), records, table, problem, name, now, problems, zone=load_zone(directory))
    text = compact(s)
    while len(text.encode()) > SUMMARY_MAX and shrink(s):
        text = compact(s)
    pa.write_text(os.path.join(folder, "summary.json"), text)
    return s


def fmt_money(value, currency=None):
    if value is None:
        return "—"
    return "<0.01" if 0 < value < 0.005 else "{:.2f}".format(value)


def fmt_tokens(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            return "{:.1f}{}".format(n / size, unit)
    return str(int(n))


REPORT_BY = ("activity", "task", "phase", "kind", "role", "program", "model", "machine", "day", "pass", "session",
             "provider", "endpoint", "region", "account", "scope")


def report_lines(s, by=None):
    out = []
    p, tot = s["prices"], s["totals"]
    if p.get("source") or p.get("currency"):
        head = "estimated from {} prices as of {} ({})".format(p.get("source") or "?", p.get("asOf") or "?", p.get("currency") or "?")
    else:
        head = "No price table in profile {}: tokens only".format(p.get("profile") or "?")
    zone = zone_of(s)
    out.append("Space {} · {} · updated {}".format(s["space"], head, local_clock(s["updated"], zone)))
    share = (100.0 * tot["tokens"]["cr"] / tot["tokens"]["all"]) if tot["tokens"]["all"] else 0
    cc = tot.get("cc") if isinstance(tot.get("cc"), dict) else None  # Claude Code's own figure: beside est., never in it
    line = "Total     {} est.{}   {} tokens (cache read {:.0f}%)".format(fmt_money(tot["money"]), " (no price)" if cc and tot["money"] is None else "",
                                                                     fmt_tokens(tot["tokens"]["all"]), share)
    if tot.get("reconstructedMoney"):
        line += "   includes {} est. reconstructed (best effort)".format(fmt_money(tot["reconstructedMoney"]))
    if tot.get("unpricedTokens"):
        line += "   + {} unpriced tokens".format(fmt_tokens(tot["unpricedTokens"]))
    if cc:
        line += "   Claude Code {} ({} of {} Claude sessions)".format(
            "{} USD list price".format(fmt_money(cc["money"])) if cc.get("money") is not None else "no figure",
            cc.get("sessions", 0), cc.get("claudeSessions", 0))
    if by is None:
        out.append(line)
        days = [e for e in s["byDay"] if e["key"] == day_in(zone, s["updated"])]
        today = days[0]["money"] if days else (0.0 if p.get("source") or p.get("currency") else None)
        out.append("Today     {}{} est. ({})".format(fmt_money(today), "+" if days and days[0].get("unpricedTokens") else "",
                                                     "UTC day" if zone["offsets"] == [[0, 0]] else "local day, " + zone["zone"]))

    def table(title, entries, label=lambda e: str(e["key"]), with_cc=False, ratio=False):
        total = sum(e["money"] or 0 for e in entries) or 0  # shares are of est. only, never of CC (AC6.4)
        cc_head = " {:>10}".format("CC") if with_cc else ""
        out.append("{:<28} {:>10}{} {:>6} {:>10}{}".format(title, "est.", cc_head, "share", "tokens", " {:>8}".format("CC/est.") if ratio else ""))
        for e in entries:
            pct = "{:.0f}%".format(100.0 * (e["money"] or 0) / total) if total and e["money"] is not None else ""
            mark = " ◌" if e.get("reconstructed") else ""
            plus = "+" if e["money"] is not None and e.get("unpricedTokens") else ""
            cc_cell = " {:>10}".format(fmt_money(e.get("cc"))) if with_cc else ""
            rate = ""
            if ratio:
                rate = " {:>8}".format("{:.2f}".format(e["cc"] / e["money"]) if e.get("cc") is not None and e["money"] else "")
            out.append("  {:<26} {:>10}{} {:>6} {:>10}{}{}".format(label(e)[:26], fmt_money(e["money"]) + plus, cc_cell, pct,
                                                               fmt_tokens(e["tokens"]), rate, mark))
        if any(e["money"] is not None and e.get("unpricedTokens") for e in entries):
            out.append("  + marks an estimate of the priced part only (some of its use has no price)")
        if with_cc:
            out.append("  CC = Claude Code's own list-price estimate (USD), per session; not the bill")
    sections = [("activity", "By activity", s["byActivity"], None), ("task", "By task", s["byTask"], None),
                ("phase", "By phase", s["byPhase"], None)]
    sections += [(d, "By " + d, s["byKind"].get(d, []), None) for d in ("kind", "role", "program", "model", "machine")]
    sections += [("day", "By day ({})".format(zone["zone"]), s["byDay"], lambda e: str(e["key"]) + (" (UTC)" if e.get("utc") else "")),
                 ("pass", "Costliest passes", [dict(p, key="pass {} ({})".format(p["pass"], p.get("trigger") or "?") + (" rebuild" if p.get("rebuild") else ""))
                                               for p in s["passes"]], None),
                 ("session", "Sessions", s["sessions"], lambda e: "{} {} {}".format(e["key"], e.get("role") or e.get("kind"), e.get("task") or ""))]
    sections += [(f, "By " + f, s.get("by" + f.capitalize()) or [], None) for f in SCOPE_FIELDS]
    sections += [("scope", "By scope (provider · endpoint · region · account)", s.get("byScope") or [], None)]
    for key, title, entries, label in sections:
        if by is None and key in ("phase", "session", "pass", "program", "machine") + SCOPE_FIELDS:
            continue
        if by is None and key == "scope" and len(entries) < 2:
            continue
        if by is not None and key != by:
            continue
        with_cc = bool(cc) and key in ("session", "task")
        table(title, entries, label or (lambda e: str(e["key"])), with_cc, with_cc and key == "session" and p.get("currency") == "USD")
        if key == "scope":
            for e in entries:
                said = ", ".join("{} (from {})".format(scope_text(tuple(None if x == "*" else x for x in a["scope"])), a["validFrom"])
                                 for a in e.get("pricedAs") or []) or "no price"
                out.append("  {}: priced as {}".format(e["key"], said))
    if by is None:
        out.append("No price: {}".format(", ".join(p.get("missing") or []) or "(none)"))
        if s.get("unmatched", {}).get("sessions"):
            out.append("Unmatched sessions (not in the totals): {}, {} tokens".format(s["unmatched"]["sessions"], fmt_tokens(s["unmatched"]["tokens"])))
        if cc and (cc.get("unmatched") or {}).get("sessions"):
            out.append("Claude Code figures matching no session (not in the totals): {}, {} USD list price".format(
                cc["unmatched"]["sessions"], fmt_money(cc["unmatched"].get("money"))))
        for prob in s.get("problems") or []:
            out.append("Problem: " + prob)
    return out


def entry_account(entry, otherwise):
    """The account of a byDayModel entry: its own when its launch recorded one (accountBy env or rule), else
    `otherwise` (the summary's label: an entry labelled by the space's chain follows the space)."""
    if entry.get("accountBy") in ("env", "rule") and fact_label("account", entry.get("account")):
        return entry["account"]
    return otherwise


def account_wide_summary(account, now):
    """byDayModel summed over every space on this machine with the same account label (read-only)."""
    root = os.path.join(pa.ROOT, "spaces")
    spaces, merged, unpriced = [], {}, {}
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        s = pa.read_json(os.path.join(root, name, "costs", "summary.json"), None) if pa.ID.match(name) else None
        if not isinstance(s, dict):
            continue
        # per entry (t-032 R-A3): the account a launch recorded for it, else the summary's
        mine = [e for e in s.get("byDayModel") or [] if isinstance(e, dict) and entry_account(e, s.get("account") or "default") == account]
        if not mine and (s.get("account") or "default") != account:
            continue
        spaces.append(name)
        for e in mine:
            m = merged.setdefault((e.get("day"), e.get("model")), {"day": e.get("day"), "model": e.get("model"), "tokens": zero(), "money": None})
            for k in KINDS:
                m["tokens"][k] += num((e.get("tokens") or {}).get(k))
            if e.get("money") is not None:
                m["money"] = (m["money"] or 0) + e["money"]
                u = num(e.get("unpricedTokens"))
            else:
                u = total_tokens({k: num((e.get("tokens") or {}).get(k)) for k in KINDS})
            unpriced[(e.get("day"), e.get("model"))] = unpriced.get((e.get("day"), e.get("model")), 0) + u
    for k, m in merged.items():  # a merged entry with a priced part and unpriced use is partial (VERIFY-5 RL5-2)
        if m["money"] is not None and unpriced.get(k):
            m["unpricedTokens"] = unpriced[k]
    return {"v": 1, "account": account, "spaces": spaces, "updated": iso(now),
            "byDayModel": [merged[k] for k in sorted(merged, key=lambda k: (k[0] or "", k[1] or ""), reverse=True)]}


def report(directory, by=None, since=None, until=None, as_json=False, account_wide=False, billed=False, now=None, utc=False,
           account=None):
    """The space's costs; days are the terminal's local days (t-036), or UTC days with utc."""
    now = time.time() if now is None else now
    if account_wide:
        if account is not None and fact_label("account", account) is None:
            raise pa.Problem("--account is a label (letters, digits, . _ -), never an account number")
        print(json.dumps(account_wide_summary(account or costs_account(), now), indent=2))
        return 0
    for d in (since, until):
        if d is not None and not DATE.match(d):
            raise pa.Problem("dates are YYYY-MM-DD (this terminal's days; --utc for UTC days)")
    if by is not None and by not in REPORT_BY:
        raise pa.Problem("--by is one of " + ", ".join(REPORT_BY))
    folder = costs_dir(directory)
    every = ledger_rows(directory)
    starts = [e for e in (parse_ts(r.get("from") or "") for r in every) if e is not None]
    zone = dict(UTC_ZONE) if utc else machine_zone(now, min(starts) if starts else None)  # its table reaches every row
    day = lambda r: day_in(zone, r.get("from")) or r.get("day", "")  # noqa: E731
    rows = [r for r in every if (not since or day(r) >= since) and (not until or day(r) <= until)]
    records, _bad = read_lines(os.path.join(folder, "sessions.jsonl"))
    table, problem, name = space_prices(directory)
    state = pa.read_json(os.path.join(folder, "state.json"), {}) or {}
    s = summarize(directory, rows, records, table, problem, name, now, state.get("problems") or [], since=since, until=until, zone=zone)
    if as_json:
        print(json.dumps(s, indent=2))
        return 0
    print("\n".join(report_lines(s, by)))
    if billed:
        print("\n".join(billed_lines(s, now)))
    return 0




# ---- billed amounts (R13-R15): neutral rows; a profile script fetches or imports them ------------------

SCRIPT_TIMEOUT = 120  # seconds a profile billing script may run
STDOUT_MAX = 5 * 1024 * 1024
MAX_PAGES = 3  # requests one query run may make
RUNS_PER_DAY = 3  # query runs (scheduled and --now) per UTC day
EVERY = 86400  # the scheduled query runs at most this often
RANGE_DAYS = 35
WIDE = re.compile(r"(?i)admin|poweruser|fullaccess|root|owner")
AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION")
CURRENCY = re.compile(r"^[A-Z]{3}$")


def billing_dir():
    return os.path.join(pa.ROOT, "billing")


def stdin_is_tty():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def refuse_agent(what):
    if any(os.environ.get(k) for k in AGENT_MARKERS):
        raise pa.Problem("{} is the person's command and is refused inside an agent's session; nothing was run".format(what))


def refuse_worker_terminal(what, scheduled):
    """--scheduled: only the app's timer, which has no terminal. Otherwise: not in a Project Agent's or a worker's
    terminal (a herdr.pa label, or a parent, on the terminal or above it)."""
    address = os.environ.get("HC_TERMINAL_ID")
    if not address:
        return
    if scheduled:
        raise pa.Problem("{} --scheduled runs only from the panel's timer, not in a terminal".format(what))
    seen = set()
    while address and address not in seen and len(seen) < 6:
        seen.add(address)
        info = pa.hc_get(address)
        if (info.get("labels") or {}).get("herdr.pa") or info.get("parent"):
            raise pa.Problem("{} is refused in a Project Agent's or a worker's terminal; run it in your own tab".format(what))
        address = info.get("parent")


def billing_profile():
    """The billing block of this machine's default profile (billing is per account, run where the panel is)."""
    p = pa.load_profile()
    block = p.get("billing") if isinstance(p.get("billing"), dict) else {}
    return p, block


def query_entry(block, name):
    q = (block.get("queries") or {}).get(name) if isinstance(block.get("queries"), dict) else None
    if not isinstance(q, dict):
        raise pa.Problem("the profile declares no billing query {!r}".format(name))
    if q.get("readOnly") is not True or not isinstance(q.get("calls"), list) or not q["calls"] \
            or not all(isinstance(c, str) and c for c in q["calls"]):
        raise pa.Problem("billing query {!r} must declare \"readOnly\": true and the read calls it makes (\"calls\"); refused".format(name))
    if not isinstance(q.get("run"), list) or not q["run"] or not all(isinstance(x, str) for x in q["run"]):
        raise pa.Problem("billing query {!r} has no run list".format(name))
    return q


def credential_problem(credential):
    if not isinstance(credential, str) or not credential.strip():
        return "no credential named: name a read-only credential profile with --credential"
    if credential.strip().lower() == "default":
        return "the credential 'default' is refused: name a read-only credential profile"
    if WIDE.search(credential):
        return "the credential {!r} looks like a wide one (admin, power user, full access, root, owner): refused".format(credential)
    if not re.match(r"^[\w.@+=,-]{1,128}$", credential):
        return "the credential name has characters that a profile name cannot have"
    return None


def scrub(text):
    return TWELVE.sub("<number>", text or "")


def run_script(profile, entry, values, timeout=None):
    """Runs a profile billing script: argv from its run list with {placeholders} filled, no shell, path inside the
    profile folder. (exit code or 'timeout', stdout text, last 5 stderr lines scrubbed)."""
    folder = os.path.realpath(profile["_folder"])
    argv = []
    for i, word in enumerate(entry["run"]):
        for k, v in values.items():
            word = word.replace("{" + k + "}", str(v))
        if i == 0:
            word = os.path.realpath(os.path.join(folder, word))
            if not word.startswith(folder + os.sep) or not os.path.isfile(word):
                raise pa.Problem("billing script {} is not a file inside the profile folder".format(entry["run"][0]))
        argv.append(word)
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                           timeout=SCRIPT_TIMEOUT if timeout is None else timeout, cwd=folder)
    except subprocess.TimeoutExpired:
        return "timeout", "", []
    except OSError as e:
        return "oserror", "", [scrub(str(e))[:200]]
    err = [scrub(x)[:200] for x in r.stderr.decode("utf-8", "replace").splitlines()[-5:]]
    if len(r.stdout) > STDOUT_MAX:
        return "toolarge", "", err
    return r.returncode, r.stdout.decode("utf-8", "replace"), err


def check_identity(profile, entry, credential, read_only_match, today):
    """The script's check mode must show a read-only identity: (identity, None) or (identity or None, why refused)."""
    code, out, err = run_script(profile, entry, {"mode": "check", "credential": credential, "from": today, "to": today, "maxPages": MAX_PAGES})
    first = next((x for x in out.splitlines() if x.strip()), "")
    try:
        said = json.loads(first)
    except ValueError:
        said = None
    identity = said.get("identity") if isinstance(said, dict) and isinstance(said.get("identity"), str) else None
    why = None
    if code != 0:
        why = "the check exited {}".format(code)
    elif not isinstance(said, dict) or said.get("readOnly") is not True:
        why = "the check did not say readOnly: true"
    elif not identity or TWELVE.search(identity):
        why = "the check gave no usable identity name"
    else:
        try:
            matched = re.search(read_only_match or "(?!)", identity)
        except re.error:
            matched = None
        if not matched:
            why = "identity {} does not match the read-only pattern".format(identity)
        elif WIDE.search(identity):
            why = "identity {} looks like a wide one".format(identity)
    return (scrub(identity)[:128] if identity else None), why, err


def query_state():
    return pa.read_json(os.path.join(billing_dir(), "query-state.json"), {}) or {}


def write_query_state(qs):
    qs["v"] = 1
    pa.write_json(os.path.join(billing_dir(), "query-state.json"), qs)


def billing_config():
    c = (pa.config().get("costs") or {}).get("billing")
    return c if isinstance(c, dict) else None


def set_billing_config(change):
    path = os.path.join(pa.ROOT, "config.json")
    conf = pa.read_json(path, {}) or {}
    costs = conf.get("costs") if isinstance(conf.get("costs"), dict) else {}
    billing = costs.get("billing") if isinstance(costs.get("billing"), dict) else {}
    billing.update(change)
    costs["billing"] = billing
    conf["costs"] = costs
    pa.write_json(path, conf)
    return billing


def billed_enable(query, credential, read_only_match, now=None, ask=input):
    """The person's one-time switch, in their own terminal: shows what will run, asks for the credential name again,
    runs one check, then turns the scheduled query on."""
    now = time.time() if now is None else now
    refuse_agent("pa-costs billed enable")
    refuse_worker_terminal("pa-costs billed enable", False)
    if not stdin_is_tty():
        raise pa.Problem("pa-costs billed enable needs a terminal: the person runs it and confirms it")
    problem = credential_problem(credential)
    if problem:
        raise pa.Problem(problem)
    try:
        re.compile(read_only_match or "")
    except re.error as e:
        raise pa.Problem("--read-only-match is not a regular expression ({})".format(e))
    if not read_only_match:
        raise pa.Problem("give --read-only-match: a pattern that the read-only identity's name matches")
    profile, block = billing_profile()
    entry = query_entry(block, query)
    print("Billing query {} ({}) of profile {}".format(query, entry.get("label") or "no label", profile["name"]))
    print("  read calls it makes: {}".format(", ".join(entry["calls"])))
    print("  credential: {}   identity must match: {}".format(credential, read_only_match))
    print("  runs: at most 1 per day from the panel's timer, at most {} per UTC day in all, at most {} requests each".format(RUNS_PER_DAY, MAX_PAGES))
    print("  stated fee: {} {} per request".format(entry.get("feePerRequest", "?"), entry.get("currency", "")))
    if (ask("Type the credential name again to turn it on: ") or "").strip() != credential:
        raise pa.Problem("not confirmed; nothing was turned on")
    today = iso(now)[:10]
    identity, why, err = check_identity(profile, entry, credential, read_only_match, today)
    if why:
        qs = query_state()
        qs.update(result="refused", lastError="refused: cannot show the configured identity is read-only ({})".format(why), stderr=err,
                  identity=identity, checkedAt=iso(now))
        write_query_state(qs)
        raise pa.Problem("refused: cannot show the configured identity is read-only ({}); nothing was turned on".format(why))
    conf = set_billing_config({"query": query, "credential": credential, "readOnlyMatch": read_only_match, "enabled": True,
                               "enabledAt": iso(now), "enabledBy": "terminal"})
    print("Turned on: identity {} is read-only. The panel's timer queries at most once a day; pa-costs billed disable turns it off.".format(identity))
    return conf


def billed_disable():
    conf = billing_config()
    if not conf:
        print("Billing query: off (it was never turned on).")
        return 0
    set_billing_config({"enabled": False, "disabledAt": pa.now()})
    print("Billing query: off. Billed rows are kept.")
    return 0


def billed_fetch(mode="scheduled", now=None, quiet=False, frm=None, to=None):
    """mode scheduled (the panel's timer, hourly: runs at most once a day) or now (the person, within the daily cap)."""
    now = time.time() if now is None else now
    scheduled = mode == "scheduled"
    refuse_agent("pa-costs billed fetch")
    refuse_worker_terminal("pa-costs billed fetch", scheduled)
    conf = billing_config()
    say = (lambda *a: None) if quiet else print
    if not conf or not conf.get("enabled"):
        if scheduled:
            return {"result": "skipped", "why": "off"}
        raise pa.Problem("the billing query is off on this machine (pa-costs billed enable, in your terminal)")
    qs = query_state()
    today = iso(now)[:10]
    runs = qs.get("runsToday") if isinstance(qs.get("runsToday"), dict) and qs["runsToday"].get("day") == today else {"day": today, "n": 0}
    last = parse_ts(qs.get("lastRun") or "")
    if scheduled and last is not None and now - last < EVERY:
        return {"result": "skipped", "why": "ran in the last 24 h"}
    if runs["n"] >= RUNS_PER_DAY:
        if scheduled:
            return {"result": "skipped", "why": "daily cap"}
        raise pa.Problem("the billing query already ran {} times today (UTC); the cap is {}".format(runs["n"], RUNS_PER_DAY))
    profile, block = billing_profile()

    def finish(result, error=None, requests=0, identity=None, stderr=(), ran=True):
        if ran:
            runs["n"] += 1
            qs.update(lastRun=iso(now), runsToday=runs, nextAfter=iso(now + EVERY))
        qs.update(result=result, requests=requests, identity=identity or qs.get("identity"), lastError=error, stderr=list(stderr))
        write_query_state(qs)
        say("Billing query {}: {}{}".format(conf.get("query"), result, " ({})".format(error) if error else ""))
        return {"result": result, "error": error, "requests": requests}
    try:
        entry = query_entry(block, conf.get("query"))
    except pa.Problem as e:
        return finish("refused", str(e), ran=False)
    problem = credential_problem(conf.get("credential"))
    if problem:
        return finish("refused", problem, ran=False)
    identity, why, err = check_identity(profile, entry, conf["credential"], conf.get("readOnlyMatch"), today)
    if why:
        return finish("refused", "refused: cannot show the configured identity is read-only ({})".format(why), identity=identity, stderr=err)
    frm = frm or iso(now - RANGE_DAYS * 86400)[:10]
    to = to or today
    code, out, err = run_script(profile, entry, {"mode": "query", "credential": conf["credential"], "from": frm, "to": to, "maxPages": MAX_PAGES})
    if code != 0:
        words = {4: "permission denied", 5: "not enabled for this account", "timeout": "timed out after {} s".format(SCRIPT_TIMEOUT),
                 "toolarge": "output over 5 MB"}.get(code, "failed (exit {})".format(code))
        return finish("failed", words, identity=identity, stderr=err)
    try:
        rows, done = parse_script_rows(out, "query:" + conf["query"], now)
        requests = done.get("requests")
        if not isinstance(requests, int) or isinstance(requests, bool) or requests < 0:
            raise ValueError("the done line has no request count")
        if requests > MAX_PAGES:
            raise ValueError("the script claims {} requests, more than the cap of {}".format(requests, MAX_PAGES))
    except ValueError as e:
        return finish("failed", "batch rejected: {}".format(e), identity=identity, stderr=err)
    append_billed(rows)
    result = finish("ok", None, requests=requests, identity=identity, stderr=err)
    say("{} billed row(s) from {} to {}".format(len(rows), frm, to))
    return dict(result, rows=len(rows))


def default_account():
    """The account label of billed rows: the machine's chain (R-A6: the same as the estimates' without a space)."""
    return account_chain()[0]


def validate_row(row, source, account, now):
    """A billed row in the neutral shape (spec 8.1), or ValueError naming the field."""
    if not isinstance(row, dict):
        raise ValueError("a row is not an object")
    out = {"v": 1}
    for k in ("from", "to"):
        if not isinstance(row.get(k), str) or not DATE.match(row[k]):
            raise ValueError("{} must be a date YYYY-MM-DD".format(k))
        out[k] = row[k]
    if out["from"] > out["to"]:
        raise ValueError("from is after to")
    amount = row.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount != amount or abs(amount) > 1e9:
        raise ValueError("amount must be a number")
    out["amount"] = round(float(amount), 6)
    if not isinstance(row.get("currency"), str) or not CURRENCY.match(row["currency"]):
        raise ValueError("currency must be a three-letter code")
    out["currency"] = row["currency"]
    out["account"] = row.get("account") if row.get("account") is not None else account
    if not isinstance(out["account"], str) or not out["account"] or len(out["account"]) > 64:
        raise ValueError("account must be a short label")
    out["label"] = row.get("label")
    if not isinstance(out["label"], str) or not out["label"] or len(out["label"]) > 200:
        raise ValueError("label must be a short text")
    for k, allowed in (("model", None), ("kind", KINDS + ("other",)), ("unit", ("tokens",))):
        v = row.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > 100 or (allowed and v not in allowed)):
            raise ValueError("{} is not valid".format(k))
        out[k] = v
    q = row.get("quantity")
    if q is not None and (isinstance(q, bool) or not isinstance(q, (int, float)) or q < 0):
        raise ValueError("quantity must be a number >= 0")
    out["quantity"] = q
    for k in ("final", "accountWide"):
        v = row.get(k, True)
        if not isinstance(v, bool):
            raise ValueError("{} must be true or false".format(k))
        out[k] = v
    note = row.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 200):
        raise ValueError("note must be a short text")
    out["note"] = note
    out["source"] = source
    # Scanned before the code adds its own fields: the id is a hash it computes and may hold 12 digits by chance.
    if TWELVE.search(json.dumps(out)) or TWELVE.search(json.dumps(dict(row, amount=out["amount"]), default=str)):
        raise ValueError("a row holds a 12-digit number (an account id?)")
    out["fetchedAt"] = iso(now)
    out["id"] = hashlib.sha1("|".join(str(out[k]) for k in ("source", "account", "from", "to", "label", "kind")).encode()).hexdigest()[:16]
    return out


def parse_script_rows(text, source, now):
    """Rows from a script's stdout, ending with {"done": true, ...}. Any bad row rejects the whole batch."""
    account = default_account()
    rows, done = [], None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise ValueError("line {} is not JSON".format(n))
        if done is not None:
            raise ValueError("output after the done line")
        if isinstance(obj, dict) and obj.get("done") is True:
            done = obj
            continue
        try:
            rows.append(validate_row(obj, source, account, now))
        except ValueError as e:
            raise ValueError("row {}: {}".format(n, e))
    if done is None:
        raise ValueError("no done line")
    return rows, done


def append_billed(rows):
    if not rows:
        return
    path = os.path.join(billing_dir(), "billed.jsonl")
    os.makedirs(billing_dir(), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows).encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def effective_billed():
    """Billed rows in effect: the latest fetchedAt per id, minus voided ids."""
    rows, _bad = read_lines(os.path.join(billing_dir(), "billed.jsonl"))
    out = {}
    for r in rows:
        if r.get("ev") == "void":
            out.pop(r.get("id"), None)
        elif r.get("id") and (r.get("id") not in out or (r.get("fetchedAt") or "") >= (out[r["id"]].get("fetchedAt") or "")):
            out[r["id"]] = r
    return sorted(out.values(), key=lambda r: (r.get("from") or "", r.get("label") or "", r.get("id")))


def billed_import(path, via, now=None, dry_run=False):
    now = time.time() if now is None else now
    refuse_agent("pa-costs billed import")
    profile, block = billing_profile()
    entry = (block.get("importers") or {}).get(via) if isinstance(block.get("importers"), dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("run"), list) or not entry["run"]:
        raise pa.Problem("the profile declares no billing importer {!r}".format(via))
    if not os.path.isfile(path):
        raise pa.Problem("no such file: " + path)
    code, out, err = run_script(profile, entry, {"file": os.path.abspath(path)})
    if code != 0:
        raise pa.Problem("importer {} failed (exit {}){}".format(via, code, ": " + err[-1] if err else ""))
    try:
        rows, _done = parse_script_rows(out, "file:" + via, now)
    except ValueError as e:
        raise pa.Problem("batch rejected, nothing added: {}".format(e))
    if dry_run:
        print("{} row(s) would be added (dry run)".format(len(rows)))
        return rows
    append_billed(rows)
    print("{} billed row(s) added from {} via {}".format(len(rows), os.path.basename(path), via))
    return rows


def billed_list(as_json=False):
    rows = effective_billed()
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No billed rows.")
    for r in rows:
        print("{}  {}..{}  {:<10} {:>12} {}  {} {}{}  ({}, {})".format(r["id"], r["from"], r["to"], r.get("account"), fmt_money(r.get("amount")),
                                                                  r.get("currency"), r.get("label"), r.get("kind") or "",
                                                                  "" if r.get("final", True) else " (not final)", r.get("source"), r.get("fetchedAt")))
    return 0


def billed_remove(row_id):
    if not any(r["id"] == row_id for r in effective_billed()):
        raise pa.Problem("no billed row {}".format(row_id))
    append_billed([{"v": 1, "ev": "void", "id": row_id, "at": pa.now()}])
    print("Removed billed row {} (a void line was appended).".format(row_id))
    return 0


def status_line(now=None):
    now = time.time() if now is None else now
    conf = billing_config()
    if not conf or not conf.get("enabled"):
        return "Billing query: off"
    qs = query_state()
    try:
        _p, block = billing_profile()
        entry = (block.get("queries") or {}).get(conf.get("query")) or {}
    except pa.Problem:
        entry = {}
    fee, cur = entry.get("feePerRequest"), entry.get("currency") or ""
    if qs.get("lastRun"):
        n = qs.get("requests") or 0
        cost = " ≈ {:.2f} {}".format(n * fee, cur) if isinstance(fee, (int, float)) else ""
        last = "last run {} ({}, {} requests{})".format(qs["lastRun"], qs.get("result"), n, cost)
        nxt = qs.get("nextAfter") or iso(now)
    else:
        last, nxt = "not run yet", iso(now)
    return "Billing query: {} · next after {} · cap 1/day (≤ {} requests) · [Turn off: pa-costs billed disable]".format(last, nxt, MAX_PAGES)


def billed_status(now=None, as_json=False):
    if as_json:
        print(json.dumps({"config": billing_config(), "state": query_state(), "line": status_line(now)}, indent=2))
        return 0
    print(status_line(now))
    qs = query_state()
    if qs.get("lastError"):
        print("  last error: " + qs["lastError"])
    return 0


def day_range(a, b):
    d0 = datetime.datetime.strptime(a, "%Y-%m-%d")
    d1 = datetime.datetime.strptime(b, "%Y-%m-%d")
    out = []
    while d0 <= d1 and len(out) < 400:
        out.append(d0.strftime("%Y-%m-%d"))
        d0 += datetime.timedelta(days=1)
    return out


def billing_comparison(billed, space, here, table):
    """Estimated (this space, all spaces here) beside billed (account-wide), per model, for the covered days (spec 8.2)."""
    account = space.get("account") or "default"
    currency = (table or {}).get("currency") or (space.get("prices") or {}).get("currency")
    mine = [r for r in billed if r.get("account") == account]
    rows = [r for r in mine if r.get("currency") == currency]
    separate = {}
    for r in mine:
        if r.get("currency") != currency:
            s = separate.setdefault(r.get("currency"), {"currency": r.get("currency"), "amount": 0.0, "rows": 0})
            s["amount"] += r.get("amount") or 0
            s["rows"] += 1
    if not rows:
        return {"account": account, "currency": currency, "accountWide": True, "models": [], "covered": None, "other": 0.0,
                "total": {"thisSpace": None, "allSpaces": None, "billed": 0.0}, "calibrated": None, "noPrice": [],
                "separate": sorted(separate.values(), key=lambda s: s["currency"] or ""), "sources": []}
    covered = set()
    for r in rows:
        covered.update(day_range(r["from"], r["to"]))
    first = min(covered)
    end = max(max(covered), (space.get("updated") or "")[:10] or max(covered))
    missing = [d for d in day_range(first, end) if d not in covered]

    def est(summary):
        """({key: money of the priced entries}, {keys with tokens and no price}) over the covered days."""
        out, unpriced = {}, set()
        for e in summary.get("byDayModel") or []:
            if e.get("day") not in covered:
                continue
            if entry_account(e, account) != account:  # R-A5: never another account's estimate
                continue
            key = model_key(table, e.get("model")) or e.get("model")
            if e.get("money") is not None:
                out[key] = out.get(key, 0.0) + e["money"]
                if e.get("unpricedTokens"):  # a mixed entry: priced part only
                    unpriced.add(key)
            elif total_tokens(e.get("tokens")):
                unpriced.add(key)
        return out, unpriced

    def est_of(priced, unpriced, key):
        # No use: a real 0.0. Use and no price: None ("no price"), never 0.
        return priced[key] if key in priced else (None if key in unpriced else 0.0)

    def est_total(priced, unpriced):
        return round(sum(priced.values()), 6) if priced else (None if unpriced else 0.0)
    (this, this_np), (alls, alls_np) = est(space), est(here)
    models, other = {}, 0.0
    for r in rows:
        key = model_key(table, r.get("model")) if r.get("model") else None
        if not key:
            other += r.get("amount") or 0
            continue
        m = models.setdefault(key, {"model": key, "billed": 0.0, "num": 0.0, "den": 0.0, "notFinal": False, "days": set()})
        m["billed"] += r.get("amount") or 0
        m["notFinal"] = m["notFinal"] or not r.get("final", True)
        m["days"].update(day_range(r["from"], r["to"]))
        if r.get("quantity") is not None and r.get("unit") == "tokens" and r.get("kind") in KINDS:
            price = price_for(table, key, r["from"])
            p = price.get(r["kind"]) if price else None
            if p:
                m["num"] += r.get("amount") or 0
                m["den"] += r["quantity"] * p / 1e6
    out_models, calibrated, cal_priced, cal_partial = [], 0.0, False, False
    for key in sorted(models):
        m = models[key]
        few = len(m["days"]) < 3 or m["notFinal"]
        t, a = est_of(this, this_np, key), est_of(alls, alls_np, key)
        if t is None or key in this_np:
            cal_partial = True
        if t is not None:
            cal_priced = True
        if m["den"] > 0:
            factor = {"kind": "price", "value": round(m["num"] / m["den"], 4), "applied": True, "few": few, "note": "from quantities"}
            calibrated += (t or 0.0) * factor["value"]
        elif a is not None and a > 0:
            value = round(m["billed"] / a, 4)
            factor = {"kind": "scope", "value": value, "applied": False, "few": few,
                      "note": "not applied" + ("; below 1.0: the table over-prices" if value < 1.0 else "")}
            if key in alls_np:  # the all-spaces figure is the priced part only (VERIFY-5 RL5-3)
                factor["partial"] = True
            calibrated += t or 0.0
        else:
            factor = {"kind": None, "value": None, "applied": False, "few": few, "note": "no estimate to compare"}
            calibrated += t or 0.0
        out_models.append({"model": key, "thisSpace": None if t is None else round(t, 6), "allSpaces": None if a is None else round(a, 6),
                           "thisSpacePartial": key in this and key in this_np, "allSpacesPartial": key in alls and key in alls_np,
                           "billed": round(m["billed"], 6), "factor": factor, "notFinal": m["notFinal"], "days": len(m["days"])})
    return {"account": account, "currency": currency, "accountWide": True,
            "covered": {"from": first, "to": max(covered), "days": len(covered), "of": len(day_range(first, end)), "missing": missing},
            "sources": sorted({r.get("source") for r in rows}), "fetchedAt": max(r.get("fetchedAt") or "" for r in rows),
            "models": out_models, "other": round(other, 6),
            "total": {"thisSpace": est_total(this, this_np), "allSpaces": est_total(alls, alls_np),
                      "thisSpacePartial": bool(this and this_np), "allSpacesPartial": bool(alls and alls_np),
                      "billed": round(sum(r.get("amount") or 0 for r in rows), 6)},
            # Calibrated only where there is a priced estimate to calibrate; partial when a billed model has unpriced use.
            # Also partial when this space has unpriced use of a model that was not billed (VERIFY-5 RL5-1).
            "calibrated": round(calibrated, 6) if cal_priced else None, "calibratedPartial": cal_priced and (cal_partial or bool(this_np)),
            "noPrice": sorted(this_np | alls_np), "separate": sorted(separate.values(), key=lambda s: s["currency"] or "")}


def billed_lines(s, now):
    table, _problem, _name = (None, None, None)
    try:
        profile = pa.load_profile(s["prices"].get("profile"))
        table, _problem = load_prices(profile)
    except pa.Problem:
        pass
    here = account_wide_summary(s.get("account") or "default", now)
    c = billing_comparison(effective_billed(), s, here, table)
    out = ["Billed (account-wide: includes use outside this space and outside Herdr)"]
    if not c["covered"]:
        if not c["separate"]:
            out.append("  no billed rows for account {}".format(c["account"]))
        elif c["currency"]:  # rows exist, in another currency only (REVIEW-4 RL4-2)
            out.append("  no billed rows in {} for account {}".format(c["currency"], c["account"]))
        else:
            out.append("  no price table, so no currency to compare: billed rows for account {} are not compared".format(c["account"]))
    else:
        cov = c["covered"]
        out.append("Covered {} – {} ({} of {} days{}) · source {}, fetched {}".format(
            cov["from"], cov["to"], cov["days"], cov["of"], "; not billed: " + ", ".join(cov["missing"]) if cov["missing"] else "",
            ", ".join(c["sources"]), c["fetchedAt"]))
        out.append("  {:<22} {:>12} {:>16} {:>14}".format("", "this space", "all spaces here", "billed"))
        out.append("  {:<22} {:>12} {:>16} {:>14}".format("", "est.", "est.", "account-wide"))
        def est(v, partial):
            return fmt_money(v) + ("+" if partial else "")
        for m in c["models"]:
            f = m["factor"]
            fac = "" if f["kind"] is None else "{} factor {}{}{}".format("price" if f["kind"] == "price" else "scope ratio", f["value"],
                                                                        " (not applied)" if not f["applied"] else "",
                                                                        " (from a partial estimate)" if f.get("partial") else "")
            out.append("  {:<22} {:>12} {:>16} {:>14}  {}{}{}".format(m["model"][:22], est(m["thisSpace"], m["thisSpacePartial"]), est(m["allSpaces"], m["allSpacesPartial"]),
                                                                    fmt_money(m["billed"]), fac, " (few days)" if f["few"] else "",
                                                                    " (not final)" if m["notFinal"] else ""))
        if c["other"]:
            out.append("  {:<22} {:>12} {:>16} {:>14}".format("other / unmapped labels", "", "", fmt_money(c["other"])))
        t = c["total"]
        out.append("  {:<22} {:>12} {:>16} {:>14}".format("total", est(t["thisSpace"], t["thisSpacePartial"]),
                                                           est(t["allSpaces"], t["allSpacesPartial"]), fmt_money(t["billed"])))
        if c["noPrice"]:
            out.append("  no price for {}{}".format(", ".join(c["noPrice"]), "; + marks an estimate of the priced part only"
                                                    if any(m["thisSpacePartial"] or m["allSpacesPartial"] for m in c["models"])
                                                    or t["thisSpacePartial"] or t["allSpacesPartial"] else ""))
        cal = c["calibrated"]
        out.append("  calibrated (price factor applied where available): this space {}".format(
            "— (no priced estimate)" if cal is None else fmt_money(cal) + (" + unpriced" if c["calibratedPartial"] else "")))
    for sep in c["separate"]:
        out.append("  in {} (not compared): {} billed, account-wide, {} row(s)".format(sep["currency"], fmt_money(sep["amount"]), sep["rows"]))
    out.append(status_line(now))
    return out


COMPARE_ARG_MAX = 100 * 1024  # a summary passed as an argument (the panel, for a remote space): Linux caps one at 128 KB


def json_arg(text, what):
    if len(text.encode()) > COMPARE_ARG_MAX:
        raise pa.Problem("{} is over 100 KB; pass a trimmed summary".format(what))
    try:
        value = json.loads(text)
    except ValueError:
        raise pa.Problem("{} is not JSON".format(what))
    if not isinstance(value, dict):
        raise pa.Problem("{} is not a JSON object".format(what))
    return value


def billing_status(now=None):
    """The query's status for the Costs view: config, state, the status line, and the stated fee and caps."""
    conf = billing_config()
    fee = cur = None
    try:
        _p, block = billing_profile()
        queries = block.get("queries") if isinstance(block.get("queries"), dict) else {}
        name = (conf or {}).get("query") or (next(iter(queries)) if len(queries) == 1 else None)
        entry = queries.get(name) if isinstance(queries.get(name), dict) else {}
        fee = entry.get("feePerRequest") if isinstance(entry.get("feePerRequest"), (int, float)) else None
        cur = entry.get("currency") if isinstance(entry.get("currency"), str) else None
    except pa.Problem:
        pass
    return {"config": conf, "state": query_state(), "line": status_line(now), "feePerRequest": fee, "feeCurrency": cur,
            "maxPages": MAX_PAGES, "runsPerDay": RUNS_PER_DAY}


def billed_compare(space=None, summary_json=None, here_json=None, now=None):
    """`pa-costs billed compare --json` (read-only, allowed anywhere): the billed comparison of the Costs view, as JSON.
    The space's summary from its folder (--space), or passed as an argument (a remote space's, trimmed); the all
    spaces here figure computed on this machine, or passed (a remote's `report --account-wide`). Billed rows are this
    machine's."""
    now = time.time() if now is None else now
    if summary_json is not None:
        s = json_arg(summary_json, "--summary-json")
    else:
        s = pa.read_json(os.path.join(costs_dir(space_directory(space)), "summary.json"), None)
        s = s if isinstance(s, dict) else {}
    account = s.get("account") or "default"
    here = json_arg(here_json, "--here-json") if here_json is not None else account_wide_summary(account, now)
    table = None
    try:
        table, _problem = load_prices(pa.load_profile((s.get("prices") or {}).get("profile")))
    except pa.Problem:
        pass
    billed = effective_billed()
    out = {"comparison": billing_comparison(billed, s, here, table), "status": billing_status(now),
           "rows": sum(1 for r in billed if r.get("account") == account)}
    print(json.dumps(out, indent=2))
    return 0


def add_billed_parser(sub):
    b = sub.add_parser("billed", help="billed amounts: status, list, remove, compare, disable; the person's enable, fetch, import")
    bs = b.add_subparsers(dest="action")
    s = bs.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = bs.add_parser("list")
    s.add_argument("--json", action="store_true")
    s = bs.add_parser("remove")
    s.add_argument("row_id")
    s = bs.add_parser("compare", help="the comparison with estimates, as JSON (read-only; the Costs view)")
    s.add_argument("--json", action="store_true", required=True)
    s.add_argument("--space")
    s.add_argument("--summary-json", help="a space's summary.json text (a remote space's); else --space's own")
    s.add_argument("--here-json", help="`pa-costs report --account-wide` output (a remote's); else computed here")
    bs.add_parser("disable")
    s = bs.add_parser("enable")
    s.add_argument("--query", required=True)
    s.add_argument("--credential", required=True, help="a read-only credential profile name; never default or an admin one")
    s.add_argument("--read-only-match", required=True, help="a pattern the read-only identity's name must match")
    s = bs.add_parser("fetch")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--scheduled", action="store_true", help="the panel's timer: at most once a day")
    g.add_argument("--now", action="store_true", help="the person: one run now, within the daily cap")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--from", dest="frm")
    s.add_argument("--to")
    s = bs.add_parser("import")
    s.add_argument("file")
    s.add_argument("--via", required=True)
    s.add_argument("--dry-run", action="store_true")
    return b


def cmd_billed(args):
    a = args.action
    if a == "status":
        return billed_status(as_json=args.json)
    if a == "list":
        return billed_list(as_json=args.json)
    if a == "remove":
        return billed_remove(args.row_id)
    if a == "compare":
        return billed_compare(args.space, args.summary_json, args.here_json)
    if a == "disable":
        return billed_disable()
    if a == "enable":
        billed_enable(args.query, args.credential, args.read_only_match)
        return 0
    if a == "fetch":
        for d in (args.frm, args.to):
            if d is not None and not DATE.match(d):
                raise pa.Problem("dates are YYYY-MM-DD")
        result = billed_fetch(mode="scheduled" if args.scheduled else "now", quiet=args.quiet, frm=args.frm, to=args.to)
        return 0 if result.get("result") in ("ok", "skipped") else 1
    if a == "import":
        billed_import(args.file, args.via, dry_run=args.dry_run)
        return 0
    raise pa.Problem("billed: give status, list, remove, compare, disable, enable, fetch or import")


# ---- launch records (called by pa.py: pa-claude, pa-codex, pa-session) ---------------------------

APPEND_WAIT = 1.0  # seconds a launcher waits for the append lock before it gives up (the launch goes on)
TASK_NAME = re.compile(r"^([A-Za-z]+-\d+)-")


def append_session(directory, record, wait=APPEND_WAIT):
    """Append one record to costs/sessions.jsonl under costs/.append.lock. True when written; never raises."""
    try:
        folder = costs_dir(directory)
        os.makedirs(folder, exist_ok=True)
        fd = os.open(os.path.join(folder, ".append.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.time() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        return False
                    time.sleep(0.05)
            append_line(os.path.join(folder, "sessions.jsonl"), record)
            return True
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - a launch never fails because of costs
        return False


def task_for(directory, name):
    """The longest ledger task id that prefixes name + '-' (as pa-cleanup matches), else t-NNN from the name."""
    if not name:
        return None
    try:
        tasks = [t.get("id") for t in (pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}).get("tasks", []) if t.get("id")]
    except (OSError, ValueError, AttributeError):
        tasks = []
    for tid in sorted(tasks, key=len, reverse=True):
        if name.startswith(tid + "-"):
            return tid
    m = TASK_NAME.match(name)
    return m.group(1) if m else None


def option_value(argv, *names):
    """(present, value) of an option given as `--x v`, `-x v` or `--x=v`."""
    for i, a in enumerate(argv):
        for n in names:
            if a == n:
                nxt = argv[i + 1] if i + 1 < len(argv) else None
                return True, (nxt if nxt is not None and not nxt.startswith("-") else None)
            if n.startswith("--") and a.startswith(n + "="):
                return True, a[len(n) + 1:] or None
    return False, None


def launch_plan(kind, argv):
    """What the session id is: {"session", "resumed", "bind", "fork", "add"} (add: pa-claude adds --session-id)."""
    if kind != "claude":
        return {"session": None, "resumed": False, "bind": "launch-window", "fork": False, "add": False}
    fork = "--fork-session" in argv
    has_id, given = option_value(argv, "--session-id")
    has_resume, resumed_id = option_value(argv, "--resume", "-r")
    cont = "--continue" in argv or "-c" in argv
    if has_id:
        return {"session": given, "resumed": has_resume or cont, "bind": None if given else "launch-window", "fork": fork, "add": False}
    if has_resume and resumed_id and not fork:
        return {"session": resumed_id, "resumed": True, "bind": None, "fork": False, "add": False}
    if has_resume or cont or fork:
        return {"session": None, "resumed": has_resume or cont, "bind": "launch-window", "fork": fork, "add": False}
    return {"session": None, "resumed": False, "bind": None, "fork": False, "add": True}


def host():
    try:
        return os.uname()[1].split(".")[0][:64]
    except (OSError, AttributeError):
        return None


# ---- facts of a session (t-032): provider, endpoint, region, account, recorded at launch -------------------------------
# Labels only. A variable is read by its declared name and only an accepted label is stored, never another value.

FACT_KEYS = ("provider", "providerBy", "endpoint", "endpointBy", "region", "regionBy", "account", "accountBy")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CREDENTIAL_NAME = re.compile(r"(?i)KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|SESSION")


def _env_name(name, where, problems):
    if not isinstance(name, str) or not ENV_NAME.match(name):
        problems.append("{}: env must be a variable name".format(where))
        return None
    if CREDENTIAL_NAME.search(name):
        problems.append("{}: variable {} looks like a credential: refused, not read".format(where, name))
        return None
    return name


def _regex(value, where, problems, optional=False):
    if value is None and optional:
        return None
    try:
        if not isinstance(value, str):
            raise re.error("not a string")
        return re.compile(value)
    except re.error:
        problems.append("{}: not a regex".format(where))
        return False


def pricing_rules(profile):
    """(rules, problems) of the profile's optional pricing block (R-P3, A-R1, A-R2). A bad rule is dropped and named;
    never raises, so a launch never stops on it. rules: defaultProvider, providers [(program, regex, provider)],
    endpoints [("model", regex, kind) | ("env", name, regex, kind)], region (names, regex) or None."""
    problems = []
    rules = {"defaultProvider": None, "providers": [], "endpoints": [], "region": None}
    block = (profile or {}).get("pricing")
    if block is None:
        return rules, problems
    if not isinstance(block, dict):
        return rules, ["pricing must be an object"]
    if block.get("defaultProvider") is not None:
        rules["defaultProvider"] = fact_label("provider", block["defaultProvider"])
        if rules["defaultProvider"] is None:
            problems.append("pricing.defaultProvider must be a label")
    providers = block.get("providers")
    if providers is not None and not isinstance(providers, list):
        problems.append("pricing.providers must be a list")
        providers = []
    for i, r in enumerate(providers or []):
        where = "pricing.providers[{}]".format(i)
        if not isinstance(r, dict) or fact_label("provider", r.get("provider")) is None or r.get("program") not in (None, "claude", "codex"):
            problems.append("{}: needs provider (a label) and program claude, codex or null".format(where))
            continue
        rx = _regex(r.get("model"), where, problems, optional=True)
        if rx is not False:
            rules["providers"].append((r.get("program"), rx, r["provider"]))
    endpoints = block.get("endpoints")
    if endpoints is not None and not isinstance(endpoints, list):
        problems.append("pricing.endpoints must be a list")
        endpoints = []
    for i, r in enumerate(endpoints or []):
        where = "pricing.endpoints[{}]".format(i)
        if not isinstance(r, dict) or fact_label("endpoint", r.get("endpoint")) is None:
            problems.append("{}: needs endpoint (a kind label)".format(where))
            continue
        if "env" in r:
            name = _env_name(r.get("env"), where, problems)
            rx = _regex(r.get("match"), where, problems)
            if name and rx is not False:
                rules["endpoints"].append(("env", name, rx, r["endpoint"]))
        else:
            rx = _regex(r.get("model"), where, problems)
            if rx is not False:
                rules["endpoints"].append(("model", rx, r["endpoint"]))
    region = block.get("region")
    if region is not None:
        names = region.get("env") if isinstance(region, dict) else None
        if not isinstance(names, list) or not names:
            problems.append("pricing.region needs env (a list of variable names) and match")
        else:
            ok = [n for n in (_env_name(n, "pricing.region", problems) for n in names) if n]
            rx = _regex(region.get("match"), "pricing.region", problems)
            if ok and rx is not False:
                rules["region"] = (ok, rx)
    if block.get("endpointPriceAs") is not None and not isinstance(block.get("endpointPriceAs"), dict):
        problems.append("pricing.endpointPriceAs must be an object")
    return rules, problems


def machine_rules():
    """(accountRules [(name, regex, label)], region label or None, problems) of the machine's config costs block."""
    problems, out = [], []
    costs = pa.config().get("costs")
    costs = costs if isinstance(costs, dict) else {}
    rules = costs.get("accountRules")
    if rules is not None and not isinstance(rules, list):
        problems.append("costs.accountRules must be a list")
        rules = []
    for i, r in enumerate(rules or []):
        where = "costs.accountRules[{}]".format(i)
        if not isinstance(r, dict) or fact_label("account", r.get("account")) is None:
            problems.append("{}: needs account (a label)".format(where))
            continue
        name = _env_name(r.get("env"), where, problems)
        rx = _regex(r.get("match"), where, problems)
        if name and rx is not False:
            out.append((name, rx, r["account"]))
    region = costs.get("region")
    if region is not None and fact_label("region", region) is None:
        problems.append("costs.region must be a region label")
        region = None
    return out, region, problems


def provider_rule(rules, program, model):
    for prog, rx, provider in (rules or {}).get("providers") or []:
        if prog is not None and prog != program:
            continue
        if rx is not None and not (isinstance(model, str) and rx.search(model)):
            continue
        return provider
    return None


def endpoint_rule(rules, model, env=None):
    """The endpoint kind of the first matching rule; env rules only when env is given (at launch)."""
    for r in (rules or {}).get("endpoints") or []:
        if r[0] == "model":
            if isinstance(model, str) and r[1].search(model):
                return r[2]
        elif env is not None:
            value = env.get(r[1])
            if value and r[2].search(value):
                return r[3]
    return None


def unknown_facts():
    return {"provider": None, "providerBy": "unknown", "endpoint": None, "endpointBy": "default", "region": None,
            "regionBy": "unknown", "account": "default", "accountBy": "default"}


def launch_facts(profile, program, model, directory=None, env=None):
    """The facts a start record gets (t-032 R-P3, R-A2, A-R1, A-R2), plus `refused` (names, never values) when a value
    or a rule was refused. Never raises: on any error, unknown facts."""
    env = os.environ if env is None else env
    try:
        rules, _problems = pricing_rules(profile)
        account_rules, machine_region, _mp = machine_rules()
        out, refused = {}, []
        provider = provider_rule(rules, program, model)
        out["provider"], out["providerBy"] = provider, "launch" if provider else "unknown"
        endpoint = endpoint_rule(rules, model, env)
        out["endpoint"], out["endpointBy"] = endpoint, "rule" if endpoint else "default"
        region, region_by = None, "unknown"
        if env.get("PA_COST_REGION"):
            if fact_label("region", env["PA_COST_REGION"]):
                region, region_by = env["PA_COST_REGION"], "env:PA_COST_REGION"
            else:
                refused.append("PA_COST_REGION")
        if region is None and rules["region"]:
            names, rx = rules["region"]
            for name in names:
                value = env.get(name)
                if not value:
                    continue
                if rx.search(value) and fact_label("region", value):
                    region, region_by = value, "env:" + name
                else:
                    region_by = "refused"
                    refused.append(name)
                break
        if region is None and region_by == "unknown" and machine_region:
            region, region_by = machine_region, "machine"
        if region is None and refused and region_by == "unknown":
            region_by = "refused"
        out["region"], out["regionBy"] = region, region_by
        account, account_by = None, None
        if env.get("PA_COST_ACCOUNT"):
            if fact_label("account", env["PA_COST_ACCOUNT"]):
                account, account_by = env["PA_COST_ACCOUNT"], "env"
            else:
                refused.append("PA_COST_ACCOUNT")
        if account is None:
            for name, rx, label in account_rules:
                value = env.get(name)
                if value and rx.search(value):
                    account, account_by = label, "rule"
                    break
        if account is None:
            account, account_by = account_chain(directory)
        out["account"], out["accountBy"] = account, account_by
        if refused:
            out["refused"] = refused
        return out
    except Exception:  # noqa: BLE001 - a launch never fails because of its facts
        return unknown_facts()


def row_facts(row, sess, rules, chain):
    """The facts a ledger row is priced and shown under (t-032): its own where it has them (from its launch record);
    else, at summarize time, provider and endpoint from the profile's model rules over its record's program and model
    (providerBy/endpointBy "rule"), else the default ("default"); region unknown; account from the chain (C7, C15).
    The rules read the program and model of the start record in effect at the row's time (t-032 J1d, d-117 limit 7).
    A stored account counts only when the launch recorded it (accountBy env or rule); one the launch took from the
    chain follows today's chain, as entry_account does (J1d, d-117 limit 2: one label per row)."""
    sess = sess or {}
    rec = start_in_effect(sess, row.get("from")) if sess else {}
    out = {}
    if row.get("provider"):
        out["provider"], out["providerBy"] = row["provider"], row.get("providerBy") or "launch"
    else:
        p = provider_rule(rules, row.get("program") or rec.get("program") or sess.get("program"), rec.get("model"))
        out["provider"], out["providerBy"] = (p, "rule") if p else ((rules or {}).get("defaultProvider"), "default")
    if row.get("endpoint"):
        out["endpoint"], out["endpointBy"] = row["endpoint"], row.get("endpointBy") or "rule"
    else:
        n = endpoint_rule(rules, rec.get("model"))
        out["endpoint"], out["endpointBy"] = (n, "rule") if n else (None, "default")
    if row.get("region"):
        out["region"], out["regionBy"] = row["region"], row.get("regionBy") or "unknown"
    else:
        out["region"], out["regionBy"] = None, row.get("regionBy") if row.get("regionBy") in ("refused",) else "unknown"
    if row.get("account") and row.get("accountBy") in ("env", "rule"):
        out["account"], out["accountBy"] = row["account"], row["accountBy"]
    else:
        out["account"], out["accountBy"] = chain
    return out


def record_launch(directory, space, kind, argv, command, walked, profile=None):
    """pa-claude / pa-codex under a Project Agent: the command with --session-id added when it is ours to choose,
    after appending one start record (counters' context only: never an argument other than --model's value)."""
    import uuid
    plan = launch_plan(kind, argv)
    session = plan["session"]
    if plan["add"]:
        session = str(uuid.uuid4())
        command = list(command) + ["--session-id", session]
    me = walked[0] if walked else {}
    _has, model = option_value(command, "--model")
    name = me.get("name") if isinstance(me.get("name"), str) else None
    append_session(directory, {
        "v": 1, "ev": "start", "at": pa.now(), "session": session, "program": kind, "kind": "worker", "space": space,
        "task": task_for(directory, name), "name": name, "role": None, "terminal": os.environ.get("HC_TERMINAL_ID"),
        "parent": me.get("parent"), "model": model, "host": host(), "cwd": os.getcwd(), "pid": os.getpid(),
        "resumed": plan["resumed"], "bind": plan["bind"], "fork": plan["fork"], "source": "recorded",
        **(launch_facts(profile, kind, model, directory) if profile is not None else unknown_facts())})
    return command


def record_closed(directory, terminal, by):
    """pa-worker close: the worker in `terminal` ended (the collector then stops looking at its file once read)."""
    return append_session(directory, {"v": 1, "ev": "closed", "at": pa.now(), "terminal": terminal, "by": by})


def record_agent_start(directory, agent, address, project, model, resume, profile=None):
    """pa-session: one start record per start and resume of the Project Agent's own session."""
    facts = launch_facts(profile, "claude", model, directory) if profile is not None else unknown_facts()
    return append_session(directory, {
        "v": 1, "ev": "start", "at": pa.now(), "session": agent.get("sessionId"), "program": "claude", "kind": "pa",
        "space": agent.get("space") or os.path.basename(directory), "task": None, "name": None, "role": None,
        "terminal": address, "parent": None, "model": model, "host": host(), "cwd": project, "pid": os.getpid(),
        "resumed": bool(resume), "bind": None, "fork": False, "source": "recorded", **facts})


# ---- Claude Code's own per-session cost (t-029): the recording status line -----------------------------
# Claude Code runs `pa-costs statusline --space ID --then <the profile's status line command>` (pa.py adds it with
# --settings) every update, with a JSON object on stdin. It keeps ids and numbers only, in costs/cc/<session>.json,
# and prints the inner command's output unchanged. It never fails, never writes to stderr and never waits long.

INNER_TIMEOUT = 2.0  # seconds the inner status line command may run (from its start); past that: killed, nothing printed
RECORD_BUDGET = 0.3  # seconds the record may take after it started; past that it is not waited for
STDIN_MAX = 1 << 20  # bytes of stdin read; more is cut (passed on cut) and nothing is recorded
CC_WRITE_EVERY = 300  # seconds: an unchanged record is rewritten at most this often (keeps `at` fresh)
CC_DROP = 0.01  # a cost lower than the stored one by more than this, with a lower duration, starts a new segment
CC_RESTORE_SLACK = 60  # seconds: a held drop is a resume when the duration comes back faster than the clock by more
CC_SPAN_SLACK = 300  # seconds: a written record whose duration passes its own span by more has restored totals (t-043)
CC_MAX_FILES = 5000  # costs/cc/ files a summary reads, newest first
CC_SESSION = re.compile(r"^[0-9A-Za-z-]{8,64}$")
CC_VERSION = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")
CC_KEYS = ("v", "session", "cost", "base", "segments", "durationMs", "apiDurationMs", "model", "version", "terminal",
           "first", "at")


def cc_number(value, top=None):
    """value when it is a finite number >= 0 (and < top), else None. Never a bool or a string."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0 or (top is not None and value >= top):
        return None
    return value


def cc_text(value, pattern):
    """value when the whole string is one the pattern takes (fullmatch: `$` alone would take a trailing newline)."""
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def cc_fields(obj, terminal, at):
    """The record's fields from a status line input: only the named ids and numbers, each checked (default deny: nothing
    else of the input is read). None when there is no valid session id."""
    if not isinstance(obj, dict):
        return None
    session = cc_text(obj.get("session_id"), CC_SESSION)
    if session is None:
        return None
    cost = obj.get("cost") if isinstance(obj.get("cost"), dict) else {}
    model = obj.get("model") if isinstance(obj.get("model"), dict) else {}
    return {"session": session, "cost": cc_number(cost.get("total_cost_usd"), 1e6),
            "durationMs": cc_number(cost.get("total_duration_ms")), "apiDurationMs": cc_number(cost.get("total_api_duration_ms")),
            "model": cc_text(model.get("id"), MODEL), "version": cc_text(obj.get("version"), CC_VERSION),
            "terminal": cc_text(terminal, pa.ID), "at": at}


def cc_clean(rec, session=None):
    """A stored record with every field checked again, or None when it is not one (or not the named session's)."""
    if not isinstance(rec, dict) or rec.get("v") != 1:
        return None
    sid = cc_text(rec.get("session"), CC_SESSION)
    if sid is None or (session is not None and sid != session):
        return None
    first, at = rec.get("first"), rec.get("at")
    if not isinstance(at, str) or parse_ts(at) is None:
        return None
    segments = rec.get("segments")
    out = {"v": 1, "session": sid, "cost": cc_number(rec.get("cost"), 1e6), "base": cc_number(rec.get("base"), 1e9),
           "segments": segments if isinstance(segments, int) and not isinstance(segments, bool) and segments >= 1 else None,
           "durationMs": cc_number(rec.get("durationMs")), "apiDurationMs": cc_number(rec.get("apiDurationMs")),
           "model": cc_text(rec.get("model"), MODEL), "version": cc_text(rec.get("version"), CC_VERSION),
           "terminal": cc_text(rec.get("terminal"), pa.ID),
           "first": first if isinstance(first, str) and parse_ts(first) is not None else at, "at": at}
    held_cost, held_ms = cc_number(rec.get("heldCost"), 1e6), cc_number(rec.get("heldMs"))
    if held_cost is not None and held_ms is not None:  # a drop record_cc holds until it knows it a resume or a restart
        out["heldCost"], out["heldMs"] = held_cost, held_ms
    elif (out["segments"] or 1) > 1 and out["durationMs"] is not None and out["base"] is not None:
        # t-043: totals holding more wall time than the record's own span were restored by a resume, so they hold
        # every earlier segment already (after a real restart the duration is at most the time since it).
        span = (parse_ts(out["at"]) or 0) - (parse_ts(out["first"]) or 0)
        if out["durationMs"] > (span + CC_SPAN_SLACK) * 1000:
            out.update(base=0, segments=1, corrected=True)
    return out


def cc_figure(rec):
    """The session's figure (base + cost), or None when the record has no usable one."""
    if rec is None or rec.get("cost") is None or rec.get("base") is None or rec.get("segments") is None:
        return None
    return rec["base"] + rec["cost"]


# The statusline process's cancel state (REVIEW M1): the inner command, whether Popen is still starting it, whether
# a cancel came, and the lock record_cc holds while it writes. Only statusline_main installs the signal handler.
CC_STOP = {"proc": None, "starting": False, "stopping": False, "write": threading.Lock()}


def statusline_kill(proc):
    """Kill the inner command's whole process group (it runs in its own session), or the process alone. Nothing once it
    has been reaped (returncode set by communicate or wait): its pid may since name another process (REVIEW-R2 R2-L1),
    and Popen.kill on Python 3.7/3.8 would signal that pid without checking."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, 9)  # its whole group: a shell's children hold its stdout open
    except OSError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def statusline_stop(signum=None, frame=None):
    """Claude Code cancelled this run (SIGTERM, SIGHUP, SIGINT): kill the inner group, let a record being written
    finish (at most 1 s), print nothing and exit 0. A signal while Popen is starting the inner command is held until
    Popen returns (statusline acts on it then)."""
    try:
        CC_STOP["stopping"] = True
        if CC_STOP["proc"] is None and CC_STOP["starting"]:
            return
        if CC_STOP["proc"] is not None:
            statusline_kill(CC_STOP["proc"])
        CC_STOP["write"].acquire(timeout=1.0)  # kept until exit: no record write starts now
    except BaseException:  # noqa: BLE001 - a status line never fails
        pass
    os._exit(0)


def record_cc(directory, fields, now):
    """Merge one status line reading into costs/cc/<session>.json (AC1.2-AC1.5). True when written. Only for an
    existing space folder; a busy lock records nothing this run."""
    if not os.path.isdir(directory):
        return False
    folder = os.path.join(costs_dir(directory), "cc")
    os.makedirs(folder, exist_ok=True)
    fd = os.open(os.path.join(folder, ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        path = os.path.join(folder, fields["session"] + ".json")
        try:
            with open(path, "rb") as f:
                old = cc_clean(json.loads(f.read().decode("utf-8")), fields["session"])
        except (OSError, ValueError, UnicodeDecodeError):
            old = None
        rec = dict(fields, v=1, base=0, segments=1, first=fields["at"])
        if old is not None:
            rec["base"] = old["base"] if old["base"] is not None else 0
            rec["segments"] = old["segments"] or 1
            rec["first"] = old["first"]
            if rec["cost"] is None:  # a reading without a valid cost never takes the figure away; other fields are the latest
                rec["cost"] = old["cost"]
            then = parse_ts(old["at"]) or 0
            if (fields["cost"] is not None and old["cost"] is not None and fields["cost"] < old["cost"] - CC_DROP
                    and fields["durationMs"] is not None and old["durationMs"] is not None and fields["durationMs"] < old["durationMs"]):
                # Lower totals: a restart from zero, or (t-043) a resume's first update before Claude Code restores the
                # session's saved totals. Counted as a new segment and held; undone below if the totals come back.
                rec["base"] += old["cost"]
                rec["segments"] += 1
                rec["heldCost"], rec["heldMs"] = old["cost"], old["durationMs"]
            elif "heldCost" in old:
                if (fields["cost"] is not None and fields["durationMs"] is not None and old["durationMs"] is not None
                        and fields["durationMs"] >= old["heldMs"] and fields["cost"] >= old["heldCost"] - CC_DROP
                        and fields["durationMs"] - old["durationMs"] > (now - then + CC_RESTORE_SLACK) * 1000):
                    # wall time grew faster than the clock: Claude Code restored the totals, which hold the old segment
                    rec["base"] = max(0, rec["base"] - old["heldCost"])
                    rec["segments"] = max(1, rec["segments"] - 1)
                else:
                    rec["heldCost"], rec["heldMs"] = old["heldCost"], old["heldMs"]
            if (rec["cost"] == old["cost"] and rec["model"] == old["model"] and rec["segments"] == old["segments"]
                    and rec.get("heldCost") == old.get("heldCost") and not old.get("corrected") and now - then < CC_WRITE_EVERY):
                return False
        text = json.dumps({k: rec[k] for k in CC_KEYS + ("heldCost", "heldMs") if k in rec}, separators=(",", ":")) + "\n"
        tmp = os.path.join(folder, ".{}.{}.tmp".format(fields["session"], os.getpid()))
        with CC_STOP["write"]:  # a cancel waits for a write in progress (statusline_stop), and none starts after it
            if CC_STOP["stopping"]:
                return False
            try:
                with open(tmp, "w") as f:
                    f.write(text)
                os.replace(tmp, path)  # atomic: a cancelled run leaves the old file whole
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return True
    finally:
        os.close(fd)


def statusline_args(argv):
    """(space, then) from `--space ID [--refresh N] [--then ARGV...]`. Unknown words before --then are ignored."""
    space, then, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--then":
            then = list(argv[i + 1:])
            break
        if a == "--space":
            space = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        i += 2 if a == "--refresh" else 1  # --refresh N: accepted, not used (Claude Code's refreshInterval is set by pa.py)
    return space, then


def statusline(argv, data, now=None):
    """One status line update: start the inner command on `data`, record while it runs, return its stdout bytes
    unchanged (b"" when there is none, it failed to start, or it ran past INNER_TIMEOUT). Never raises."""
    import subprocess as sp
    out, proc = b"", None
    try:
        space, then = statusline_args(argv)
        record = len(data) <= STDIN_MAX
        data = data[:STDIN_MAX]
        if then:
            CC_STOP["starting"] = True
            try:
                proc = sp.Popen(then, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL, close_fds=True, start_new_session=True)
            except Exception:  # noqa: BLE001 - missing or not executable: an empty line
                proc = None
            CC_STOP["proc"] = proc
            CC_STOP["starting"] = False
            if CC_STOP["stopping"]:
                statusline_stop()  # cancelled while Popen was starting it (only the handler sets stopping)
        started = time.monotonic()

        def work():
            try:
                stamp = time.time() if now is None else now
                if not record or not space or not pa.ID.fullmatch(space):
                    return
                fields = cc_fields(json.loads(data.decode("utf-8")), os.environ.get("HC_TERMINAL_ID"), iso(stamp))
                if fields is not None:
                    record_cc(os.path.join(pa.ROOT, "spaces", space), fields, stamp)
            except Exception:  # noqa: BLE001 - never breaks the status line; its text is never shown
                pass
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        if proc is not None:
            try:
                out = proc.communicate(data, timeout=max(0.0, INNER_TIMEOUT - (time.monotonic() - started)))[0] or b""
            except sp.TimeoutExpired:
                out = b""
                statusline_kill(proc)
                for pipe in (proc.stdin, proc.stdout):
                    try:
                        pipe.close()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    proc.wait(timeout=0.5)
                except Exception:  # noqa: BLE001
                    pass
        worker.join(max(0.0, started + RECORD_BUDGET - time.monotonic()) if proc is not None else RECORD_BUDGET)
    except Exception:  # noqa: BLE001 - whatever the inner command printed is still printed
        pass
    return out


def read_stdin(fd=0, limit=STDIN_MAX, wait=INNER_TIMEOUT):
    """At most limit + 1 bytes of fd, waiting at most `wait` s in all (Claude Code closes it after the JSON)."""
    import select
    chunks, size, deadline = [], 0, time.monotonic() + wait
    try:
        while size <= limit:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except (OSError, ValueError):
        pass
    return b"".join(chunks)[:limit + 1]


def statusline_main(argv):
    """`pa-costs statusline ...` as a process: nothing on stderr, the inner bytes on stdout, exit 0."""
    try:
        sys.stderr = open(os.devnull, "w")
        import signal
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):  # Claude Code cancels a run still in flight (M1)
            signal.signal(sig, statusline_stop)
        out = statusline(argv, read_stdin())
        view = memoryview(out)
        while view:
            n = os.write(1, view)
            view = view[n:]
    except BaseException:  # noqa: BLE001 - a status line never fails
        pass
    return 0


# ---- command line ------------------------------------------------------------------------------

def space_directory(explicit):
    directory, _agent = pa.space_for_command(explicit)
    return directory


def cmd_prices(directory):
    table, problem, name = space_prices(directory)
    rules, rule_problems = space_rules(directory)
    if problem:
        print(problem)
    for warning in ((table or {}).get("warnings") or []) + rule_problems:
        print(warning)

    def rows_of(models):
        for model, entries in sorted(models.items()):
            for e in entries:
                fetched = e.get("fetched") if isinstance(e.get("fetched"), dict) else {}
                prov = "  source {}".format(str(e["source"])[:120]) if isinstance(e.get("source"), str) else ""
                if fetched.get("publicationDate"):
                    prov += "  published {}".format(str(fetched["publicationDate"])[:40])
                print("  {:<24} from {}  ".format(model, e["validFrom"]) + "  ".join(
                    "{} {}".format(k, "—" if e.get(k) is None else e.get(k, 0)) for k in KINDS) + ("  (long above {})".format(
                        e["long"].get("above")) if e.get("long") else "") + prov)

    def heading(scope):
        return "provider {} · endpoint {} · region {} · account {}".format(*("*" if v is None else v for v in scope))
    if not table:
        print("No price table in profile {}: tokens only.".format(name))
    else:
        print("Profile {}: {} per 1M tokens, source {}, as of {}".format(name, table["currency"], table.get("source"), table.get("asOf")))
        d = table.get("defaultScope") or (None, None, None, None)
        if table.get("fileDefault"):
            print("default scope: " + heading(d))
        else:
            print("default scope: provider {} · any endpoint, any region, any account (the file has no defaultScope)".format(d[0] or "?"))
        rows_of(table["models"])
        for e in table.get("scoped") or []:
            print(heading(e["scope"]))
            rows_of(e["models"])
        if table.get("scoped"):
            print("Note: scoped prices are ignored by pa-costs from before t-032 (it prices everything from the default scope).")
        if rules and rules.get("providers") and not rules.get("defaultProvider") and not (table.get("defaultScope") or (None,))[0]:
            print("Note: the profile has provider rules but no pricing.defaultProvider: rows with a provider are priced only by scopes that name it.")
    records, _bad = read_lines(os.path.join(costs_dir(directory), "sessions.jsonl"))
    sessions, chain = sessions_of(records), account_chain(directory)
    unpriced, by_facts = set(), {}
    for r in ledger_rows(directory):
        if not r.get("model"):
            continue
        rf = row_facts(r, sessions.get(r.get("session")) or {}, rules, chain)
        facts = tuple(rf[f] for f in SCOPE_FIELDS)
        entry = None
        if table:
            entry = lookup(table, r["model"], r.get("from"), (None if rf["providerBy"] == "default" else facts[0],
                                                              None if rf["endpointBy"] == "default" else facts[1], facts[2], facts[3]))[0]
        if entry is None:
            unpriced.add(r["model"])
            by_facts.setdefault(facts_text(facts), set()).add(r["model"])
    print("Models seen without a price: {}".format(", ".join(sorted(unpriced)) or "(none)"))
    if table and (table.get("scoped") or len(by_facts) > 1):
        for key in sorted(by_facts):
            print("  under {}: {}".format(key, ", ".join(sorted(by_facts[key]))))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pa-costs", description="Cost tracking per space (tokens; money is an estimate).")
    sub = parser.add_subparsers(dest="cmd")
    c = sub.add_parser("collect", help="read new session bytes into the space's cost ledger")
    c.add_argument("--space")
    c.add_argument("--quiet", action="store_true")
    c.add_argument("--resummarize", action="store_true", help="only rewrite summary.json (after a price table change)")
    c.add_argument("--zone-offsets", help="the panel's timezone: <epoch>:<minutes east of UTC>[,...], the first from 0")
    c.add_argument("--zone-name", help="its name (shown beside the days)")
    c.add_argument("--zone-from", help="where its table starts (epoch seconds); a row before it is put under its UTC day")
    r = sub.add_parser("report", help="the space's costs as text, or --json")
    r.add_argument("--space")
    r.add_argument("--by", choices=REPORT_BY)
    r.add_argument("--since")
    r.add_argument("--until")
    r.add_argument("--json", action="store_true")
    r.add_argument("--billed", action="store_true", help="add the comparison with billed amounts")
    r.add_argument("--account-wide", action="store_true", help="byDayModel of every space here with the same account")
    r.add_argument("--utc", action="store_true", help="UTC days (default: this terminal's local days)")
    r.add_argument("--account", help="with --account-wide: this account label (default: this machine's)")
    x = sub.add_parser("reconstruct", help="find and bind the space's sessions from before launch records")
    x.add_argument("--space")
    x.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("prices", help="the price table in use, and models seen without a price")
    p.add_argument("--space")
    add_billed_parser(sub)
    args = parser.parse_args(argv)
    if args.cmd == "collect":
        zone = (parse_zone(args.zone_offsets, args.zone_name, args.zone_from)
                if args.zone_offsets is not None or args.zone_name is not None or args.zone_from is not None else None)
        directory = space_directory(args.space)
        try:
            collect(directory, quiet=args.quiet, resummarize=args.resummarize, zone=zone)
        except pa.Problem as e:  # our own text: shown to the panel, scrubbed and short
            note_collect_failure(directory, scrub(str(e))[:200])
            raise
        except Exception as e:  # noqa: BLE001 - any other error: its type only, never its text
            note_collect_failure(directory, type(e).__name__)
            raise
        return 0
    if args.cmd == "report":
        if args.account_wide:
            return report(None, account_wide=True, account=args.account)
        return report(space_directory(args.space), by=args.by, since=args.since, until=args.until, as_json=args.json, billed=args.billed,
                      utc=args.utc)
    if args.cmd == "reconstruct":
        reconstruct(space_directory(args.space), dry_run=args.dry_run)
        return 0
    if args.cmd == "prices":
        return cmd_prices(space_directory(args.space))
    if args.cmd == "billed":
        return cmd_billed(args)
    parser.print_help()
    return 2




if __name__ == "__main__":
    if sys.argv[1:2] == ["statusline"]:  # Claude Code's status line: before argparse, always exit 0 (t-029 R2)
        statusline_main(sys.argv[2:])
        os._exit(0)
    try:
        sys.exit(main())
    except pa.Problem as e:
        print("pa-costs: {}".format(e), file=sys.stderr)
        sys.exit(2)
