#!/usr/bin/env python3
"""Worker places, the Project Agent's waiting list and the person's start order (t-083 J1). Python standard library
only (3.7+).

  pa-places show [--json]                     places with their limits and holders, then the waiting list
  pa-places next [--json]                     the waiting jobs in effective order, each `start` or why not
  pa-places limits [--json]                   the limits per kind, the rule each comes from, ignored values
  pa-places add <task> --job J --kind K --role R [--source pa|queue] [--blocked-by LINE]   wait at the end
  pa-places block <id> --why LINE | unblock <id> | drop <id> --why LINE
  pa-places start <id> --worker ADDR          take it off the list; prints the place to record in the ledger

Kinds of place (their meaning is here, their numbers in the rules' `places`): working (a job that runs tests),
no-test (a job that runs none: it takes a working place, else the no-test extra place), suite (a job at the
full-suite step: a working place and one of the suite places). Seats are pooled: working and suite holders together
stay within `working`; all holders within `working` + `no-test`; suite holders within `suite`.

waiting.json, in the space's state folder, is written by this tool only, under <state>/.places.lock, whole (temp file
and rename); each change appends one journal line ("trigger": "places"). Workers' shells are refused the writes.
start-order.json is the person's (the panel writes it); rules.json is the person's: this tool reads both and writes
neither. The hold: the last journal line with a "hold" key ("on" or "off") decides; when that cannot be told,
`hold unknown` and nothing starts.
"""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402
import pa_hooks  # noqa: E402

WAITING_FILE = "waiting.json"
ORDER_FILE = "start-order.json"
RULES_FILE = "rules.json"
LOCK_FILE = ".places.lock"
LOCK_WAIT = 30  # seconds a write command waits for another one
KINDS = ("working", "no-test", "suite")
DEFAULTS = {"working": 4, "no-test": 0, "suite": None}  # suite None: no cap beyond working
SOURCES = ("pa", "queue")
ORDER_WHY = {"missing": "no start order yet", "empty": "start-order.json is empty",
             "not-json": "start-order.json is not JSON", "not-object": "start-order.json is not a JSON object",
             "version": "start-order.json is not version 1",
             "order": "start-order.json's order is not a list of job ids"}
HOLD_WHY = {"on": "hold on", "unknown": "hold unknown", "no-terminals": "terminal list unavailable"}
LABEL = {"working": "working", "no-test": "no-test extra", "suite": "suite"}


class Refused(Exception):
    pass


# ---- pure: limits, order, allocation, hold (the shared cases in tests/samples/places-cases.json) ----------------

def whole(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def limits_from(doc):
    """(limits, sources, notes) from a rules.json document: per kind, the active rule with the highest order that
    names it with a whole number >= 0; else the default. Bad values and unknown kinds are named, once each."""
    limits, sources, notes = dict(DEFAULTS), {k: "default" for k in KINDS}, []
    rules = doc.get("rules") if isinstance(doc, dict) else None
    rules = [r for r in rules if isinstance(r, dict) and r.get("status") == "active"] if isinstance(rules, list) else []
    order = lambda r: r.get("order") if isinstance(r.get("order"), (int, float)) and not isinstance(r.get("order"), bool) else 0
    taken = set()
    for r in sorted(rules, key=order, reverse=True):  # stable: equal orders keep the file's order
        if "places" not in r:
            continue
        rid, places = r.get("id"), r["places"]
        if not isinstance(places, dict):
            notes.append("{}: places is not an object; ignored".format(rid))
            continue
        for kind, value in places.items():
            if kind not in KINDS:
                notes.append("{}: places.{} is not a known kind; ignored".format(rid, kind))
            elif not whole(value):
                notes.append("{}: places.{} is not a whole number >= 0; ignored".format(rid, kind))
            elif kind not in taken:
                taken.add(kind)
                limits[kind], sources[kind] = value, rid
    return limits, sources, notes


def read_order(text):
    """(order ids, error code or None, why or None) from start-order.json's text (None: no file)."""
    if text is None:
        return [], "missing", ORDER_WHY["missing"]
    if not text.strip():
        return [], "empty", ORDER_WHY["empty"]
    try:
        doc = json.loads(text)
    except ValueError:
        return [], "not-json", ORDER_WHY["not-json"]
    if not isinstance(doc, dict):
        return [], "not-object", ORDER_WHY["not-object"]
    if doc.get("version") != 1 or isinstance(doc.get("version"), bool):
        return [], "version", ORDER_WHY["version"]
    order = doc.get("order")
    if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
        return [], "order", ORDER_WHY["order"]
    return order, None, None


def order_line(code):
    if code is None:
        return None
    if code == "missing":
        return "no start order yet; the Project Agent's order is used"
    return "start order unreadable: {}; the Project Agent's order is used".format(ORDER_WHY[code])


def effective_order(jobs, order):
    """The jobs in effective order: queue jobs keep their index; the movable slots take the movable jobs the
    person's order names, in that order, then the others in the Project Agent's order. Unknown ids are ignored."""
    movable = [j for j in jobs if j.get("source") != "queue"]
    by_id = {j["id"]: j for j in movable}
    ordered, seen = [], set()
    for i in order:
        if i in by_id and i not in seen:
            seen.add(i)
            ordered.append(by_id[i])
    ordered += [j for j in movable if j["id"] not in seen]
    it = iter(ordered)
    return [j if j.get("source") == "queue" else next(it) for j in jobs]


def counts(holders):
    """(b, n, s): working-type holders (working, suite, not recorded), no-test holders, suite holders."""
    n = sum(1 for p in holders if p == "no-test")
    s = sum(1 for p in holders if p == "suite")
    return len(holders) - n, n, s


def working_used(b, n, w):
    """The working places in use as drawn: working-type holders, then no-test holders while a working place is free."""
    return b + min(n, max(0, w - b))


def allocate(jobs, limits, holders, hold):
    """One reason per job, in the given (effective) order: {id, action: start|skip|blocked|hold, place, full}.
    holders: the recorded place of each live unfinished worker (None: not recorded). hold: off|on|unknown|no-terminals.
    A working or suite job needs b < working and b + n < working + no-test (the no-test place is only for no-test
    jobs; a no-test holder in a working place takes it). A job marked start counts as a holder for the jobs after it;
    a full kind is skipped, never waited on."""
    w, x, cap = limits["working"], limits["no-test"], limits["suite"]
    b, n, s = counts(holders)
    out = []
    for j in jobs:
        r = {"id": j["id"], "action": None, "place": None, "full": None}
        out.append(r)
        if hold != "off":
            r["action"] = "hold"
            continue
        if j.get("blockedBy"):
            r["action"] = "blocked"
            continue
        kind = j.get("kind")
        if kind == "no-test":
            if b + n < w + x:
                r["action"], r["place"] = "start", ("working" if b + n < w else "no-test")
                n += 1
            elif x:
                r["action"], r["full"] = "skip", {"kind": "no-test", "used": max(0, b + n - w), "limit": x}
            else:
                r["action"], r["full"] = "skip", {"kind": "working", "used": working_used(b, n, w), "limit": w}
            continue
        if kind == "suite" and cap is not None and s >= cap:
            r["action"], r["full"] = "skip", {"kind": "suite", "used": s, "limit": cap}
        elif b >= w or b + n >= w + x:  # working-type holders below working, and the pooled total below both
            r["action"], r["full"] = "skip", {"kind": "working", "used": working_used(b, n, w), "limit": w}
        else:
            r["action"], r["place"] = "start", kind
            b += 1
            s += kind == "suite"
    return out


def hold_state(text):
    """off | on | unknown from journal.jsonl's text (None: no journal). The last line with a "hold" key decides;
    a line that is not JSON after it (or with none at all) means it cannot be told."""
    state, doubt = "off", False
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            doubt = True
            continue
        if isinstance(value, dict) and "hold" in value:
            v = value["hold"]
            state = "on" if v in ("on", True) else "off" if v in ("off", False) else "unknown"
            doubt = False
    return "unknown" if doubt else state


def reason_text(r, job):
    if r["action"] == "start":
        return "start   record place: {}".format(r["place"])
    if r["action"] == "blocked":
        return "blocked: {}".format(job.get("blockedBy"))
    if r["action"] == "skip":
        f = r["full"]
        return "skip: kind full: {} {} of {}".format(LABEL[f["kind"]], f["used"], f["limit"])
    return r.get("why") or "hold"


# ---- the space's files ------------------------------------------------------------------------------------------

def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def load_waiting(directory):
    path = os.path.join(directory, WAITING_FILE)
    text = read_text(path)
    if text is None:
        return {"version": 1, "jobs": []}
    bad = "{} is not a valid waiting list ({}); nothing was changed (the person decides what to do with it)"
    try:
        data = json.loads(text)
    except ValueError:
        raise Refused(bad.format(path, "not JSON"))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise Refused(bad.format(path, 'it needs a "jobs" list'))
    for i, j in enumerate(data["jobs"]):
        if not isinstance(j, dict) or not isinstance(j.get("id"), str) or j.get("kind") not in KINDS \
                or j.get("source", "pa") not in SOURCES:
            raise Refused(bad.format(path, "job {} needs an id, a kind ({}) and a source (pa or queue)".format(
                i + 1, ", ".join(KINDS))))
    return data


def save_waiting(directory, data):
    data["version"] = 1
    data["updated"] = pa.now()
    pa.write_json(os.path.join(directory, WAITING_FILE), data)


def load_rules(directory):
    """(limits, sources, notes); a missing or broken rules.json gives the defaults and one note."""
    text = read_text(os.path.join(directory, RULES_FILE))
    if text is None:
        return limits_from({})[:2] + (["rules.json is missing; the default limits are used"],)
    try:
        doc = json.loads(text)
    except ValueError:
        return limits_from({})[:2] + (["rules.json is not valid JSON; the default limits are used"],)
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return limits_from({})[:2] + (['rules.json needs a "rules" list; the default limits are used'],)
    return limits_from(doc)


def holders_from(ledger, live, skip=None):
    """The live unfinished workers of the ledger: [{address, task, role, name, started, place, recorded}]."""
    out = []
    for t in ledger.get("tasks") or [] if isinstance(ledger, dict) else []:
        for w in t.get("workers") or [] if isinstance(t, dict) else []:
            if not isinstance(w, dict) or w.get("finished"):
                continue
            address = pa.local_id(str(w.get("address") or ""))
            if not address or address not in live or (skip and address == pa.local_id(skip)):
                continue
            place = w.get("place")
            out.append({"address": address, "task": t.get("id"), "role": w.get("role"), "name": w.get("name"),
                        "started": w.get("started"), "place": place if place in KINDS else None,
                        "recorded": place in KINDS})
    return out


class State:
    """Everything one read command needs, read once."""

    def __init__(self, directory, skip=None):
        self.directory = directory
        self.waiting = load_waiting(directory)
        self.limits, self.sources, self.notes = load_rules(directory)
        self.order, self.order_error, _ = read_order(read_text(os.path.join(directory, ORDER_FILE)))
        live = pa.live_terminals()
        self.terminals = live is not None
        self.holders = holders_from(pa_hooks.read_ledger(directory), live or {}, skip)
        self.hold = hold_state(read_text(os.path.join(directory, "journal.jsonl"))) if self.terminals else "no-terminals"
        self.jobs = effective_order(self.waiting["jobs"], self.order)
        self.reasons = allocate(self.jobs, self.limits, [h["place"] for h in self.holders], self.hold)
        for r in self.reasons:
            if r["action"] == "hold":
                r["why"] = HOLD_WHY[self.hold]

    def limit_text(self, kind):
        value = self.limits[kind]
        shown = "none" if value is None else value
        source = self.sources[kind]
        return "{} {} {}".format(kind, shown, "(default)" if source == "default" else "from " + source)


# ---- the lock and the worker refusal ----------------------------------------------------------------------------

@contextlib.contextmanager
def places_lock(directory):
    """<state>/.places.lock: one write command at a time in a space."""
    with open(os.path.join(directory, LOCK_FILE), "a") as f:
        end = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > end:
                    raise Refused("another pa-places is running in this space; try again in a minute")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def refuse_worker(directory):
    """Workers never run a write command: a task record above this folder, or this terminal is a ledger worker."""
    record = pa_hooks.find_task(os.getcwd())
    if record:
        raise Refused("workers do not run pa-places (this folder is the worktree of task {}); say in your report "
                      "what is ready".format(record.get("task")))
    me = pa.local_id(os.environ.get("HC_TERMINAL_ID") or "")
    if me:
        for t in pa_hooks.read_ledger(directory).get("tasks", []):
            for w in t.get("workers") or []:
                if isinstance(w, dict) and pa.local_id(str(w.get("address") or "")) == me:
                    raise Refused("workers do not run pa-places ({} is a worker of {} in the ledger)".format(me, t.get("id")))


def space(args):
    directory, _agent = pa.space_for_command(args.space)
    return directory


@contextlib.contextmanager
def writing(args):
    directory = space(args)
    refuse_worker(directory)
    with places_lock(directory):
        yield directory, load_waiting(directory)


def journal(directory, event, job, **kw):
    line = {"trigger": "places", "event": event, "task": job.get("task"), "job": job.get("job"), "id": job["id"]}
    line.update(kw)
    pa_hooks.append_journal(directory, line)


def find(data, job_id):
    for i, j in enumerate(data["jobs"]):
        if j.get("id") == job_id:
            return i, j
    raise Refused("{} is not waiting; nothing was changed".format(job_id))


# ---- commands ---------------------------------------------------------------------------------------------------

def cmd_add(args):
    pa.ident(args.task, "task id")
    pa.ident(args.job, "job")
    job_id = "{}/{}".format(args.task, args.job)
    with writing(args) as (directory, data):
        if any(j.get("id") == job_id for j in data["jobs"]):
            raise Refused("{} is already waiting; nothing was changed".format(job_id))
        job = {"id": job_id, "task": args.task, "job": args.job, "kind": args.kind, "role": args.role,
               "source": args.source, "added": pa.now(), "blockedBy": args.blocked_by or None}
        data["jobs"].append(job)
        save_waiting(directory, data)
        journal(directory, "add", job, kind=args.kind, source=args.source, **({"why": job["blockedBy"]} if job["blockedBy"] else {}))
    print("{} waits at {} ({})".format(job_id, len(data["jobs"]), args.kind))
    return 0


def cmd_block(args):
    with writing(args) as (directory, data):
        _i, job = find(data, args.id)
        job["blockedBy"] = args.why
        save_waiting(directory, data)
        journal(directory, "block", job, why=args.why)
    print("{} blocked: {}".format(args.id, args.why))
    return 0


def cmd_unblock(args):
    with writing(args) as (directory, data):
        _i, job = find(data, args.id)
        job["blockedBy"] = None
        save_waiting(directory, data)
        journal(directory, "unblock", job)
    print("{} unblocked".format(args.id))
    return 0


def cmd_drop(args):
    with writing(args) as (directory, data):
        i, job = find(data, args.id)
        del data["jobs"][i]
        save_waiting(directory, data)
        journal(directory, "drop", job, why=args.why)
    print("{} dropped".format(args.id))
    return 0


def cmd_start(args):
    with writing(args) as (directory, data):
        _i, job = find(data, args.id)
        st = State(directory, skip=args.worker)
        r = allocate([job], st.limits, [h["place"] for h in st.holders], st.hold)[0]
        if r["action"] == "hold":
            r["why"] = HOLD_WHY[st.hold]
        if r["action"] != "start":
            raise Refused("{} would not start now: {}; nothing was changed".format(args.id, reason_text(r, job)))
        i, job = find(data, args.id)
        del data["jobs"][i]
        save_waiting(directory, data)
        journal(directory, "start", job, worker=args.worker, place=r["place"], kind=job.get("kind"))
    print("{} started on {}".format(args.id, args.worker))
    print("record place: {}".format(r["place"]))
    return 0


def next_lines(st):
    lines = []
    note = order_line(st.order_error)
    lines.append("order: {}".format("the person's" if note is None else note))
    lines += st.notes
    if st.hold != "off":
        lines.append("{}: nothing starts".format(HOLD_WHY[st.hold]))
    if not st.jobs:
        lines.append("nothing waiting")
    width = max([len(j["id"]) for j in st.jobs] + [0])
    for k, (j, r) in enumerate(zip(st.jobs, st.reasons), 1):
        fixed = "   (merge queue, fixed)" if j.get("source") == "queue" else ""
        lines.append("{}. {:<{w}}  {:<8} {}{}".format(k, j["id"], j.get("kind"), reason_text(r, j), fixed, w=width))
    return lines


def json_jobs(st):
    return [dict(j, action=r["action"], place=r["place"], full=r["full"], reason=reason_text(r, j))
            for j, r in zip(st.jobs, st.reasons)]


def json_head(st):
    return {"limits": st.limits, "sources": st.sources, "notes": st.notes,
            "order": {"error": st.order_error, "line": order_line(st.order_error)},
            "hold": st.hold, "terminals": st.terminals}


def cmd_next(args):
    st = State(space(args))
    if args.json:
        print(json.dumps(dict(json_head(st), jobs=json_jobs(st)), indent=2))
    else:
        print("\n".join(next_lines(st)))
    return 0


def holder_line(h, suite=False):
    what = "{} · {} {}".format(h["address"], h["task"], h.get("name") or "")
    parts = [what.rstrip(), h.get("role") or "?"]
    if not h["recorded"]:
        parts.append("kind not recorded")
    elif h["place"] == "suite" and not suite:
        parts.append("suite step")
    parts.append("since {}".format(h.get("started") or "?"))
    return "    " + " · ".join(parts)


def cmd_show(args):
    st = State(space(args))
    if args.json:
        print(json.dumps(dict(json_head(st), holders=st.holders, jobs=json_jobs(st)), indent=2))
        return 0
    w, x, cap = st.limits["working"], st.limits["no-test"], st.limits["suite"]
    b, n, s = counts([h["place"] for h in st.holders])
    used = working_used(b, n, w)
    in_working = [h for h in st.holders if h["place"] != "no-test"]
    no_test = [h for h in st.holders if h["place"] == "no-test"]
    seat_w = in_working + no_test[:used - b]
    extra = no_test[used - b:]
    out = ["Places (limits: {})".format(", ".join(st.limit_text(k) for k in KINDS))]
    out += st.notes
    if not st.terminals:
        out.append("terminal list unavailable: holders not known")
    out.append("Working · {} of {}".format(used, w))
    out += [holder_line(h) for h in seat_w] or ["    free"]
    if x or extra:
        out.append("No-test extra · {} of {}".format(len(extra), x))
        out += [holder_line(h) for h in extra] or ["    free"]
    if cap is not None:
        out.append("Suite step · {} of {}  (held within Working)".format(s, cap))
        out += [holder_line(h, suite=True) for h in st.holders if h["place"] == "suite"]
    out.append("")
    out.append("Waiting · {}".format(len(st.jobs)))
    out += next_lines(st)
    print("\n".join(out))
    return 0


def cmd_limits(args):
    st_dir = space(args)
    limits, sources, notes = load_rules(st_dir)
    if args.json:
        print(json.dumps({"limits": limits, "sources": sources, "notes": notes}, indent=2))
        return 0
    for kind in KINDS:
        value = "none" if limits[kind] is None else limits[kind]
        print("{} {} {}".format(kind, value, "(default)" if sources[kind] == "default" else "from " + sources[kind]))
    for note in notes:
        print(note)
    return 0


COMMANDS = {"show": cmd_show, "next": cmd_next, "limits": cmd_limits, "add": cmd_add, "block": cmd_block,
            "unblock": cmd_unblock, "drop": cmd_drop, "start": cmd_start}


def parser():
    p = argparse.ArgumentParser(prog="pa-places", description="Worker places and the waiting list of a space (t-083).")
    sub = p.add_subparsers(dest="command")
    for name in ("show", "next", "limits"):
        s = sub.add_parser(name)
        s.add_argument("--json", action="store_true")
        s.add_argument("--space")
    s = sub.add_parser("add")
    s.add_argument("task")
    s.add_argument("--job", required=True)
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--role", required=True)
    s.add_argument("--source", default="pa", choices=SOURCES)
    s.add_argument("--blocked-by")
    s.add_argument("--space")
    for name in ("block", "unblock", "drop", "start"):
        s = sub.add_parser(name)
        s.add_argument("id", help="<task>/<job>")
        if name in ("block", "drop"):
            s.add_argument("--why", required=True)
        if name == "start":
            s.add_argument("--worker", required=True, help="the address of the worker started for it")
        s.add_argument("--space")
    return p


def main():
    args = parser().parse_args(sys.argv[1:])
    if not args.command:
        parser().print_usage(sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](args)
    except Refused as r:
        print("pa-places: refused: {}".format(r), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except pa.Problem as e:
        print("pa-places: {}".format(e), file=sys.stderr)
        sys.exit(2)
