#!/usr/bin/env python3
"""Cost tracking per space: tokens from the session files, estimated money from the profile's price table.
Python standard library only (3.7+). Linked as pa-costs.

  pa-costs collect [--space ID] [--quiet] [--resummarize]   read new session bytes into the space's cost ledger
  pa-costs report [--space ID] [--by KEY] [--since D] [--until D] [--json] [--billed] [--account-wide]
  pa-costs reconstruct [--space ID] [--dry-run]              find the space's sessions from before launch records
  pa-costs prices [--space ID]                               the price table in use, and models seen without a price
  pa-costs billed status|list|remove|disable|enable|fetch|import ...   billed amounts (the person's commands)

Files, in the space's state folder (costs/): sessions.jsonl (launch and other session records, append-only),
turns.jsonl (the cost ledger: tokens per settled segment, append-only), state.json (read offsets), summary.json
(what the panel reads). Billed rows: <HERDR_PA_HOME>/billing/. Counters and ids only: no session text is kept,
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
import socket
import subprocess
import sys
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
        for k in KINDS + ("above",):
            v = long.get(k, 0)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise ValueError("{}: long.{} must be a number >= 0 or null".format(where, k))
    return entry


def load_prices(profile):
    """(table, problem). (None, None) when the profile has no prices.json; (None, why) when it is unusable,
    and then everything is shown as tokens only."""
    path = os.path.join(profile.get("_folder") or "", "prices.json")
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
            raise ValueError("no models object")
        if not isinstance(raw.get("currency"), str) or not raw["currency"]:
            raise ValueError("no currency")
        models = {}
        for name, entries in raw["models"].items():
            if not isinstance(entries, list):
                entries = [entries]
            models[name] = sorted((_price_entry(e, name) for e in entries), key=lambda e: e["validFrom"])
        aliases = []
        for a in raw.get("aliases") or []:
            if not isinstance(a, dict) or not isinstance(a.get("match"), str) or a.get("model") not in models:
                raise ValueError("an alias needs match (a regex) and model (a key in models)")
            aliases.append((re.compile(a["match"]), a["model"]))
    except (OSError, ValueError, re.error) as e:
        return None, "prices.json of profile {} is not usable ({}): tokens only".format(profile.get("name"), str(e)[:160])
    return {"currency": raw["currency"], "unit": raw.get("unit"), "source": raw.get("source"), "asOf": raw.get("asOf"),
            "note": raw.get("note"), "profile": profile.get("name"), "models": models, "aliases": aliases}, None


def model_key(table, model):
    if not table or not model:
        return None
    if model in table["models"]:
        return model
    for pattern, key in table["aliases"]:
        if pattern.search(model):
            return key
    return None


def price_for(table, model, when):
    """The price entry in effect for model at `when` (ISO time): the latest validFrom on or before its day."""
    key = model_key(table, model)
    if key is None:
        return None
    day, found = (when or "")[:10], None
    for entry in table["models"][key]:
        if entry["validFrom"] <= day:
            found = entry
    return found


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
    return money(row.get("tokens") or {}, row.get("long") or {}, price_for(table, row.get("model"), row.get("from")))


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
FORK_SLACK = 5  # seconds: a resumed or forked session skips calls older than its start record minus this
GONE_UNSEEN = 600  # a recorded session whose file was never found is gone after this many seconds
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
        if ev == "role" and sid in out and r.get("role"):
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

    def __init__(self, sess, f, settled):
        self.sess, self.f, self.settled = sess, f, settled
        first = min(sess.get("starts") or [sess], key=lambda r: r.get("at") or "")
        guarded = first.get("resumed") or first.get("bind") or first.get("fork")
        at = parse_ts(first.get("at") or "")
        self.guard = (at - FORK_SLACK) if guarded and at else None

    def settle(self):
        if self.f.get("open"):
            if self.f["open"]["calls"]:
                self.settled.append(self.f["open"])
            self.f["open"] = None
            for item in self.f.get("tail", []):
                item[2] = True  # a later line of these calls is a late growth

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
        last = parse_ts(f.get("lastCall") or "")
        seg = f.get("open")
        model = model or f.get("model")
        if seg and (seg["day"] != ts[:10] or seg["model"] != model
                    or (self.sess.get("program") == "codex" and last is not None and epoch - last > IDLE)):
            self.settle()
            seg = None
        if not seg:
            seg = f["open"] = new_segment(ts, f.get("turn") or ts, model)
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
                self.settle()
                f["turn"] = e["ts"]
        elif e["type"] == "call":
            self.add_call(e["ts"], e["id"], e["model"], e["tokens"])
        elif e["type"] == "turn":
            self.settle()
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


def read_new(sess, f, path, size, settled):
    """Read [offset, size) of the file, whole lines only; advance the offset past them."""
    reader = Reader(sess, f, settled)
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

def short(sid):
    return (sid or "?")[:8]


def make_row(sess, seg):
    """A ledger row from a settled segment: tokens only, attributed by attribute() (step 4 rules)."""
    return {"v": 1, "seq": None, "session": sess["session"], "program": sess.get("program") or "claude",
            "kind": sess.get("kind") or "unknown", "model": seg["model"], "from": seg["from"], "to": seg["to"],
            "day": seg["day"], "calls": seg["calls"], "tokens": seg["tokens"], "long": seg["long"], "rebuild": seg["rebuild"],
            "turn": seg["turn"], "task": sess.get("task"), "phase": None, "activity": None, "role": sess.get("role"),
            "pass": None, "trigger": None, "split": None, "reason": None, "source": sess.get("source") or "recorded",
            "match": sess.get("match") or "launch-record", "confidence": sess.get("confidence") or "high"}


def attribute(row, sess, ctx):
    """Fills activity, task, phase, role, pass, trigger, split and reason (spec 3.5). Step 2: nothing known yet."""
    row["activity"], row["reason"] = "unattributed", "unknown-session-kind"
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
    if never:
        out.append("{} sessions' files were gone before they were read".format(never))
    out.extend(state.get("notes") or [])
    return out


def collect(directory, now=None, quiet=True, resummarize=False):
    """Read the new bytes of the space's sessions into costs/turns.jsonl. One collector per space at a time."""
    now = time.time() if now is None else now
    folder = costs_dir(directory)
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


def _collect(directory, folder, now, quiet, resummarize):
    state_path, turns_path = os.path.join(folder, "state.json"), os.path.join(folder, "turns.jsonl")
    try:
        with open(state_path) as fh:
            old_text = fh.read()
        state = json.loads(old_text)
    except (OSError, ValueError):
        old_text, state = "", {}
    if not isinstance(state, dict) or state.get("v") != 1:
        state = {"v": 1, "lastSeq": 0, "turnsBytes": 0, "files": {}}
    notes = state.setdefault("notes", [])
    size = os.path.getsize(turns_path) if os.path.exists(turns_path) else 0
    if size > state.get("turnsBytes", 0):  # the last run died between its two writes: drop its rows, they are read again
        with open(turns_path, "r+b") as fh:
            fh.truncate(state.get("turnsBytes", 0))
    elif size < state.get("turnsBytes", 0):
        state["turnsBytes"] = size
        if "turns.jsonl is shorter than recorded" not in notes:
            notes.append("turns.jsonl is shorter than recorded")
    records, _bad = read_lines(os.path.join(folder, "sessions.jsonl"))
    sessions = sessions_of(records)
    ctx = {"directory": directory, "records": records, "sessions": sessions, "now": now, "events": []}
    bind_pending(ctx)
    sessions = ctx["sessions"]
    files = state.setdefault("files", {})
    out, rows = [], []
    for sid in sorted(sessions):
        sess = sessions[sid]
        f = files.setdefault(sid, {"offset": 0})
        if f.get("done"):
            continue
        settled = []
        path = find_file(sess, f.get("path"))
        if not path:
            started = parse_ts(sess.get("at") or "") or now
            if f.get("path") or sess.get("gone") or now - started >= GONE_UNSEEN:
                reader = Reader(sess, f, settled)
                reader.settle()
                if not sess.get("gone"):
                    ctx["events"].append({"v": 1, "ev": "gone", "at": iso(now), "session": sid, "readTo": f.get("lastCall")})
                if not f.get("offset"):
                    f["neverRead"] = True
                f["done"] = True
            rows.extend(attribute(make_row(sess, seg), sess, ctx) for seg in settled)
            continue
        st = os.stat(path)
        if (f.get("ino") is not None and (st.st_ino != f["ino"] or st.st_dev != f["dev"])) or st.st_size < f.get("offset", 0):
            out.extend(rows)
            rows = []
            out.append({"v": 1, "ev": "void", "session": sid, "upTo": state.get("lastSeq", 0)})
            for k in ("offset", "tail", "open", "turn", "cum", "lastCall", "seen", "model", "late", "bad"):
                f.pop(k, None)
            f["offset"] = 0
            f["voided"] = f.get("voided", 0) + 1
        f.update(path=path, dev=st.st_dev, ino=st.st_ino)
        reader = read_new(sess, f, path, st.st_size, settled) if st.st_size > f.get("offset", 0) else Reader(sess, f, settled)
        if f.get("open"):
            seen = parse_ts(f.get("seen") or f["open"]["to"]) or now
            if now - seen >= IDLE or iso(now)[:10] > f["open"]["day"]:
                reader.settle()
        rows.extend(attribute(make_row(sess, seg), sess, ctx) for seg in settled)
        seen = parse_ts(f.get("seen") or "") or 0
        if (sess.get("closed") or sess.get("gone")) and not f.get("open") and now - seen >= DONE_AFTER:
            f["done"] = True
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
    state["problems"] = state_problems(state)
    text = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    if text != old_text:
        pa.write_text(state_path, text)
    written = [r for r in out if "seq" in r]
    if written or resummarize or not os.path.exists(os.path.join(folder, "summary.json")):
        summarize = globals().get("write_summary")
        if summarize:
            summarize(directory, state)
    if not quiet:
        print("collected {} new segment(s) from {} session(s)".format(len(written), len(sessions)))
    return {"rows": len(written), "sessions": len(sessions)}


def bind_pending(ctx):
    """Binds start records that have no session id yet (Codex, Claude --continue): step 5."""
    return None


# ---- command line ------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="pa-costs")
    parser.parse_args(argv)
    raise pa.Problem("give a subcommand: collect, report, reconstruct, prices or billed")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except pa.Problem as e:
        print("pa-costs: {}".format(e), file=sys.stderr)
        sys.exit(2)
