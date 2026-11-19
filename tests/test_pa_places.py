"""pa-places: worker places, the Project Agent's waiting list and the person's start order (t-083 J1).

No hc calls and no real homes: HOME, HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp folder made
with mkdtemp; a fake space sp1 holds rules.json, ledger.json, journal.jsonl, waiting.json and start-order.json.
pa.live_terminals is replaced per test (the live terminals of the fake machine). The shared cases in
tests/samples/places-cases.json are the same ones the store harness checks places.js against (AC8.3).
"""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(HERE, ".."))
TOOLS = os.path.join(REPO, "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t083-places-"))
CASES = os.path.join(HERE, "samples", "places-cases.json")
SPACE = "sp1"

_HOMES = {"HERDR_PA_HOME": os.path.join(TMP, "pa"), "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude"),
          "HERDR_APP_HOME": os.path.join(TMP, "app"), "HOME": os.path.join(TMP, "home")}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE")
_saved_env = {k: os.environ.get(k) for k in list(_HOMES) + list(DROP_ENV)}
_saved_mods = {k: sys.modules.get(k) for k in ("pa", "pa_hooks", "pa_places")}
os.environ.update(_HOMES)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pa = _load("pa")
pa_hooks = _load("pa_hooks")
try:
    pa_places = _load("pa_places")
except FileNotFoundError:  # before the tool exists (the failing-tests commit)
    pa_places = None
for _k, _v in _saved_mods.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
for _k in _HOMES:
    if _saved_env[_k] is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _saved_env[_k]
assert pa.ROOT.startswith(TMP), "pa.py must use the temp homes"


def setUpModule():
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ.update(_HOMES)
    os.makedirs(_HOMES["HOME"], exist_ok=True)


def tearDownModule():
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(TMP, ignore_errors=True)


def read(path):
    with open(path) as f:
        return f.read()


def dump(value, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


SHARED = json.loads(read(CASES))


def job(j):
    """A shared case's job: a string is a movable working job with no blocker."""
    if isinstance(j, str):
        j = {"id": j}
    out = {"id": j["id"], "kind": j.get("kind", "working"), "source": j.get("source", "pa"),
           "blockedBy": j.get("blockedBy")}
    task, _, name = j["id"].partition("/")
    out.update(task=task, job=name or task, role="build", added="2026-11-19T00:00:00Z")
    return out


RULES = {"version": 1, "rules": [{"id": "r-places", "title": "Worker places", "text": "places", "status": "active",
                                  "order": 50, "places": {"working": 4, "no-test": 1, "suite": 2}}]}


class Base(unittest.TestCase):
    def setUp(self):
        if pa_places is None:
            self.fail("runtime/tools/pa_places.py does not exist yet")
        self.dir = pa.space_dir(SPACE)
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        dump({"space": SPACE, "profile": None}, os.path.join(self.dir, "agent.json"))
        dump(RULES, os.path.join(self.dir, "rules.json"))
        dump({"version": 1, "tasks": []}, os.path.join(self.dir, "ledger.json"))
        write(os.path.join(self.dir, "journal.jsonl"), "")
        self.live = {}
        self._saved_live = pa.live_terminals
        pa.live_terminals = lambda: None if self.live is None else {a: {"address": a} for a in self.live}
        self._cwd = os.getcwd()
        os.chdir(TMP)  # no task record above the test's folder
        self._wait = pa_places.LOCK_WAIT
        for k in DROP_ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        pa.live_terminals = self._saved_live
        pa_places.LOCK_WAIT = self._wait
        os.chdir(self._cwd)
        os.environ.pop("HC_TERMINAL_ID", None)

    # ---- the space ----
    def path(self, name):
        return os.path.join(self.dir, name)

    def waiting(self):
        return json.loads(read(self.path("waiting.json")))

    def ids(self):
        return [j["id"] for j in self.waiting()["jobs"]]

    def journal(self, event=None):
        lines = [json.loads(l) for l in read(self.path("journal.jsonl")).splitlines() if l.strip()]
        lines = [l for l in lines if l.get("trigger") == "places"]
        return [l for l in lines if event is None or l.get("event") == event]

    def workers(self, *specs):
        """Ledger workers: (address, task, place or None, finished)."""
        tasks = {}
        for address, task, place, finished in specs:
            w = {"address": address, "role": "build", "name": "{}-w".format(task), "round": 1,
                 "started": "2026-11-19T10:02:00Z"}
            if place is not None:
                w["place"] = place
            if finished:
                w["finished"] = True
            tasks.setdefault(task, []).append(w)
        dump({"version": 1, "tasks": [{"id": t, "title": t, "workers": ws} for t, ws in tasks.items()]},
             self.path("ledger.json"))

    def run_main(self, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        saved = sys.argv
        sys.argv = ["pa-places"] + list(argv) + ["--space", SPACE]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = pa_places.main() or 0
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
                except pa.Problem as e:
                    print(str(e), file=sys.stderr)
                    code = 2
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_main(*argv)
        self.assertEqual(code, 0, out + err)
        return out

    def add(self, task, name, kind="working", role="build", *extra):
        return self.ok("add", task, "--job", name, "--kind", kind, "--role", role, *extra)


# ---- the shared cases (AC2.1-2.4, AC7.1-7.3, AC8.1-8.3) ----------------------------------------------------------

class SharedCases(Base):
    def test_cases_file_is_shared(self):  # AC8.3: one file, read by both suites
        self.assertEqual(SHARED["version"], 1)
        self.assertTrue(SHARED["orderCases"] and SHARED["limitCases"] and SHARED["allocCases"])

    def test_order_cases(self):  # AC8.1, AC8.2
        for case in SHARED["orderCases"]:
            with self.subTest(case=case["name"]):
                start, error, _why = pa_places.read_order(case["orderText"])
                self.assertEqual(error, case["error"])
                jobs = [job(j) for j in case["jobs"]]
                self.assertEqual([j["id"] for j in pa_places.effective_order(jobs, start)], case["effective"])

    def test_rank_cases(self):  # t-097 R9: the waiting list follows the task ranking
        self.assertTrue(SHARED["rankCases"])
        for case in SHARED["rankCases"]:
            with self.subTest(case=case["name"]):
                jobs = [job(j) for j in case["jobs"]]
                got = pa_places.effective_order(jobs, None, case["rank"])
                self.assertEqual([j["id"] for j in got], case["effective"])

    def test_pin_cases(self):  # t-097 R10: version 1 read as pins, version 2 pins, after the rank sort
        self.assertTrue(SHARED["pinCases"])
        for case in SHARED["pinCases"]:
            with self.subTest(case=case["name"]):
                start, error, _why = pa_places.read_order(case["orderText"])
                self.assertEqual(error, case["error"])
                jobs = [job(j) for j in case["jobs"]]
                got = pa_places.effective_order(jobs, start, case["rank"])
                self.assertEqual([j["id"] for j in got], case["effective"])

    def test_limit_cases(self):  # AC2.1-2.4
        for case in SHARED["limitCases"]:
            with self.subTest(case=case["name"]):
                limits, sources, notes = pa_places.limits_from(case["rules"])
                self.assertEqual(limits, case["limits"])
                self.assertEqual(sources, case["sources"])
                self.assertEqual(notes, case["notes"])

    def test_alloc_cases(self):  # AC7.1-7.4
        for case in SHARED["allocCases"]:
            with self.subTest(case=case["name"]):
                jobs = [job(j) for j in case["jobs"]]
                got = pa_places.allocate(jobs, case["limits"], case["holders"], case["hold"])
                self.assertEqual([{k: r[k] for k in ("id", "action", "place", "full")} for r in got], case["expect"])


# ---- t-097: the person's task order (the shared cases in tests/samples/task-order-cases.json) ------------------

TASK_CASES = json.loads(read(os.path.join(HERE, "samples", "task-order-cases.json")))


class TaskOrderCases(Base):
    def test_cases_file_is_shared(self):
        self.assertEqual(TASK_CASES["version"], 1)
        self.assertTrue(TASK_CASES["readCases"] and TASK_CASES["rankCases"] and TASK_CASES["moveCases"])

    def test_read_cases(self):  # R1, R2
        for case in TASK_CASES["readCases"]:
            with self.subTest(case=case["name"]):
                doc, error, _why = pa_places.read_task_order(case["text"])
                self.assertEqual(error, case["error"])
                self.assertEqual(pa_places.task_order_line(error), case["line"])
                if error is None:
                    self.assertEqual((doc["order"], doc["last"], doc["updated"]), (case["order"], case["last"], case["updated"]))
                else:
                    self.assertIsNone(doc)  # never the readable part of a bad file

    def test_rank_cases(self):  # R3, R5, R12, R27
        for case in TASK_CASES["rankCases"]:
            with self.subTest(case=case["name"]):
                doc, error, _why = pa_places.read_task_order(case["fileText"])
                self.assertEqual(error, case["error"])
                got = pa_places.effective_tasks(case["tasks"], case["taskOrder"], doc)
                self.assertEqual([[r["id"], r["source"]] for r in got], case["expect"])
                states = {t["id"]: t["state"] for t in case["tasks"]}
                self.assertEqual([r["state"] for r in got], [states[r["id"]] for r in got])

    def test_chat_quote_is_kept(self):  # R12, R13: the quote goes with a chat rank
        tasks = [{"id": "t-1", "state": "queued"}, {"id": "t-2", "state": "queued"}]
        got = pa_places.effective_tasks(tasks, {"order": [], "chat": [{"task": "t-2", "quote": "hotfix now", "over": None}]}, None)
        self.assertEqual([(r["id"], r["source"], r.get("quote")) for r in got], [("t-2", "chat", "hotfix now"), ("t-1", "agent", None)])

    def test_move_cases(self):  # R7 (the reference for the panel's moveTask), R12
        for case in TASK_CASES["moveCases"]:
            with self.subTest(case=case["name"]):
                ranking = [{"id": i, "state": s, "source": src} for i, s, src in case["ranking"]]
                before = json.dumps([ranking, case["order"], case["last"]])
                got = pa_places.move_task(ranking, case["section"], case["task"], case["move"], case["order"], case["last"])
                self.assertEqual(got, case["expect"])
                self.assertEqual(json.dumps([ranking, case["order"], case["last"]]), before)  # pure: inputs unchanged


class Tasks(Base):  # R24: pa-places tasks
    def ledger(self, tasks, task_order=None):
        doc = {"version": 1, "tasks": tasks}
        if task_order is not None:
            doc["taskOrder"] = task_order
        dump(doc, self.path("ledger.json"))

    def card(self, cid, task, status="open"):
        dump({"id": cid, "task": task, "question": "Q?", "options": [{"label": "A"}], "status": status,
              "answer": None, "created": "2026-11-19T00:00:00Z", "answered": None},
             os.path.join(self.dir, "decisions", cid + ".json"))

    def sample(self):
        self.ledger([{"id": "t-1", "state": "queued", "created": "2026-11-01T00:00:00Z",
                      "paused": {"at": "2026-11-19T00:00:00Z", "by": "person"}},
                     {"id": "t-2", "state": "in-progress", "created": "2026-11-02T00:00:00Z", "after": ["t-1", "t-5"]},
                     {"id": "t-3", "state": "needs-decision", "created": "2026-11-03T00:00:00Z"},
                     {"id": "t-4", "state": "parked", "created": "2026-11-04T00:00:00Z"},
                     {"id": "t-5", "state": "done", "created": "2026-11-05T00:00:00Z"},
                     {"id": "t-6", "state": "review", "created": "2026-11-06T00:00:00Z", "after": ["t-5"]}])
        self.card("d-1", "t-3")
        self.card("d-2", "t-6", status="answered")

    def test_text(self):
        self.sample()
        lines = self.ok("tasks").splitlines()
        self.assertEqual(lines[0], "order: the Project Agent's")
        self.assertEqual(lines[1:], ["1. t-1 queued agent · paused",
                                     "2. t-2 in-progress agent · waits for t-1 (after)",
                                     "3. t-3 needs-decision agent · card d-1 · hidden rank",
                                     "4. t-4 parked agent · hidden rank",
                                     "5. t-6 review agent"])

    def test_json(self):
        self.sample()
        data = json.loads(self.ok("tasks", "--json"))
        self.assertEqual(data["order"], {"error": "missing", "line": "order: the Project Agent's", "updated": None})
        self.assertEqual(data["notes"], [])
        t = {r["id"]: r for r in data["tasks"]}
        self.assertEqual([r["id"] for r in data["tasks"]], ["t-1", "t-2", "t-3", "t-4", "t-6"])
        self.assertEqual([r["rank"] for r in data["tasks"]], [1, 2, 3, 4, 5])
        self.assertEqual((t["t-1"]["paused"], t["t-1"]["after"], t["t-1"]["card"], t["t-1"]["hidden"]), (True, [], None, False))
        self.assertEqual(t["t-2"]["after"], ["t-1"])
        self.assertEqual((t["t-3"]["card"], t["t-3"]["hidden"]), ("d-1", True))
        self.assertTrue(t["t-4"]["hidden"])
        self.assertEqual((t["t-6"]["card"], t["t-6"]["after"]), (None, []))
        self.assertEqual({r["source"] for r in data["tasks"]}, {"agent"})

    def test_the_person_and_chat(self):
        self.ledger([{"id": "t-1", "state": "queued", "created": "2026-11-01T00:00:00Z"},
                     {"id": "t-2", "state": "queued", "created": "2026-11-02T00:00:00Z"},
                     {"id": "t-3", "state": "queued", "created": "2026-11-03T00:00:00Z", "after": ["t-gone"]}],
                    {"order": [], "chat": [{"task": "t-3", "quote": "do t-3 now", "over": "U1"}]})
        write(self.path("task-order.json"), json.dumps({"version": 1, "updated": "U1", "by": "person", "order": ["t-2"], "last": [], "moves": []}))
        lines = self.ok("tasks").splitlines()
        self.assertEqual(lines, ["order: the person's", '1. t-3 queued chat · waits for t-gone (after) · "do t-3 now"',
                                 "2. t-2 queued person", "3. t-1 queued agent"])
        self.assertEqual(json.loads(self.ok("tasks", "--json"))["order"]["updated"], "U1")

    def test_unreadable_file_said(self):  # R2: the same words as the panel, the Project Agent's order used
        self.ledger([{"id": "t-1", "state": "queued", "created": "2026-11-01T00:00:00Z"},
                     {"id": "t-2", "state": "queued", "created": "2026-11-02T00:00:00Z"}])
        write(self.path("task-order.json"), '{"version": 1, "order": ["t-2"], "last": 3}')
        lines = self.ok("tasks").splitlines()
        self.assertEqual(lines, ["task order unreadable: task-order.json's last is not a list of task ids; the Project Agent's order is used",
                                 "1. t-1 queued agent", "2. t-2 queued agent"])

    def test_bad_task_order_noted(self):
        self.ledger([{"id": "t-1", "state": "queued"}], "nope")
        out = self.ok("tasks")
        self.assertIn("ledger taskOrder is not {order, chat}; ignored", out)
        self.assertIn("1. t-1 queued agent", out)

    def test_nothing_ranked(self):
        self.assertIn("no ranked tasks", self.ok("tasks"))
        self.assertEqual(json.loads(self.ok("tasks", "--json"))["tasks"], [])

    def test_read_only_and_allowed_in_a_worker(self):  # R1: no tool writes task-order.json
        self.ledger([{"id": "t-1", "state": "queued", "workers": [{"address": "t77", "role": "build"}]}])
        os.environ["HC_TERMINAL_ID"] = "t77"
        self.ok("tasks")
        self.assertFalse(os.path.exists(self.path("task-order.json")))
        self.assertEqual(self.journal(), [])


class NextByTaskOrder(Base):  # R9, R10 through the tool
    def setUp(self):
        super().setUp()
        dump({"version": 1, "tasks": [{"id": "t-1", "state": "queued", "created": "2026-11-01T00:00:00Z"},
                                      {"id": "t-2", "state": "queued", "created": "2026-11-02T00:00:00Z"},
                                      {"id": "t-3", "state": "queued", "created": "2026-11-03T00:00:00Z"}]},
             self.path("ledger.json"))
        self.add("t-3", "a")
        self.add("t-2", "a")
        self.add("t-q", "check", "suite", "verifier", "--source", "queue")
        self.add("t-1", "a")

    def ids_next(self):
        return [j["id"] for j in json.loads(self.ok("next", "--json"))["jobs"]]

    def test_no_files_created_order(self):  # R27: created order with no file and no taskOrder
        self.assertEqual(self.ids_next(), ["t-1/a", "t-2/a", "t-q/check", "t-3/a"])
        self.assertIn("no start order yet; the Project Agent's order is used", self.ok("next"))  # J1-D3

    def test_the_person_s_task_order(self):
        write(self.path("task-order.json"), json.dumps({"version": 1, "updated": "U1", "order": ["t-3"], "last": ["t-1"]}))
        self.assertEqual(self.ids_next(), ["t-3/a", "t-2/a", "t-q/check", "t-1/a"])

    def test_unreadable_task_order_uses_the_agent_s(self):
        write(self.path("task-order.json"), "{nope")
        self.assertEqual(self.ids_next(), ["t-1/a", "t-2/a", "t-q/check", "t-3/a"])

    def test_a_version_2_pin(self):
        write(self.path("start-order.json"), json.dumps({"version": 2, "updated": "U1", "by": "person",
                                                         "pins": [{"id": "t-3/a", "slot": 0}], "moves": []}))
        self.assertEqual(self.ids_next(), ["t-3/a", "t-1/a", "t-q/check", "t-2/a"])
        self.assertIn("order: the person's", self.ok("next"))

    def test_a_version_1_file_still_read(self):
        write(self.path("start-order.json"), json.dumps({"version": 1, "order": ["t-2/a", "t-3/a"]}))
        self.assertEqual(self.ids_next(), ["t-2/a", "t-3/a", "t-q/check", "t-1/a"])

    def test_wrong_version_said(self):
        write(self.path("start-order.json"), json.dumps({"version": 3, "pins": []}))
        self.assertIn("start order unreadable: start-order.json is not version 1 or 2; the Project Agent's order is used",
                      self.ok("next"))
        self.assertEqual(self.ids_next(), ["t-1/a", "t-2/a", "t-q/check", "t-3/a"])

    def test_start_takes_the_job_and_never_writes_the_orders(self):  # R1, R10: a started job's pin goes with it
        task_order = json.dumps({"version": 1, "updated": "U1", "order": ["t-3"], "last": []}) + "\n"
        start_order = json.dumps({"version": 2, "pins": [{"id": "t-2/a", "slot": 0}]}) + "\n"
        write(self.path("task-order.json"), task_order)
        write(self.path("start-order.json"), start_order)
        self.assertEqual(self.ids_next(), ["t-2/a", "t-3/a", "t-q/check", "t-1/a"])
        self.ok("start", "t-2/a", "--worker", "t60")
        self.assertEqual(self.ids_next(), ["t-3/a", "t-q/check", "t-1/a"])
        self.ok("show")
        self.ok("tasks")
        self.ok("drop", "t-1/a", "--why", "w")
        self.assertEqual(read(self.path("task-order.json")), task_order)
        self.assertEqual(read(self.path("start-order.json")), start_order)


# ---- limits through the tool (AC2.1, AC2.4, AC2.5) ---------------------------------------------------------------

class Limits(Base):
    def test_limits_name_their_rule(self):  # AC2.1
        dump({"rules": [{"id": "r-a", "text": "x", "status": "active", "order": 30, "places": {"working": 3}},
                        {"id": "r-b", "text": "x", "status": "active", "order": 20, "places": {"working": 5}}]},
             self.path("rules.json"))
        out = self.ok("limits")
        self.assertIn("working 3 from r-a", out)
        self.assertIn("no-test 0 (default)", out)
        self.assertIn("suite none (default)", out)

    def test_bad_values_named_never_an_error(self):  # AC2.3
        dump({"rules": [{"id": "r-x", "text": "x", "status": "active", "order": 30, "places": {"gpu": 1}}]},
             self.path("rules.json"))
        out = self.ok("limits")
        self.assertEqual(out.count("r-x: places.gpu is not a known kind; ignored"), 1)
        self.assertIn("working 4 (default)", out)

    def test_rules_missing_or_broken_defaults(self):
        for text in (None, "{not json", '{"rules": 3}'):
            with self.subTest(text=text):
                if text is None:
                    os.unlink(self.path("rules.json")) if os.path.exists(self.path("rules.json")) else None
                else:
                    write(self.path("rules.json"), text)
                code, out, err = self.run_main("limits", "--json")
                self.assertEqual(code, 0, out + err)
                data = json.loads(out)
                self.assertEqual(data["limits"], {"working": 4, "no-test": 0, "suite": None})
                self.assertEqual(len(data["notes"]), 1)

    def test_next_uses_the_rules_file(self):  # AC2.5: the panel reads the same rules.json
        dump({"rules": [{"id": "r-one", "text": "x", "status": "active", "order": 1, "places": {"working": 1}}]},
             self.path("rules.json"))
        self.live = {"t51"}
        self.workers(("t51", "t-1", "working", False))
        self.add("t-2", "build")
        out = self.ok("next")
        self.assertIn("kind full: working 1 of 1", out)


# ---- the waiting list's commands (AC5.1-5.4, AC3.1) --------------------------------------------------------------

class Commands(Base):
    def test_add_appends_at_the_end(self):  # AC5.1
        self.add("t-1", "build")
        self.add("t-2", "verify", "suite", "verifier", "--source", "queue")
        self.add("t-3", "design", "no-test", "design", "--blocked-by", "waits for d-1")
        jobs = self.waiting()["jobs"]
        self.assertEqual([j["id"] for j in jobs], ["t-1/build", "t-2/verify", "t-3/design"])
        self.assertEqual(self.waiting()["version"], 1)
        j = jobs[1]
        self.assertEqual((j["task"], j["job"], j["kind"], j["role"], j["source"], j["blockedBy"]),
                         ("t-2", "verify", "suite", "verifier", "queue", None))
        self.assertTrue(j["added"].endswith("Z"))
        self.assertEqual(jobs[2]["blockedBy"], "waits for d-1")
        self.assertEqual(jobs[0]["source"], "pa")
        self.assertEqual([l["id"] for l in self.journal("add")], ["t-1/build", "t-2/verify", "t-3/design"])

    def test_add_twice_refused_unchanged(self):  # AC5.1
        self.add("t-1", "build")
        before = read(self.path("waiting.json"))
        code, out, err = self.run_main("add", "t-1", "--job", "build", "--kind", "no-test", "--role", "x")
        self.assertEqual(code, 1, out + err)
        self.assertIn("already waiting", err)
        self.assertEqual(read(self.path("waiting.json")), before)
        self.assertEqual(len(self.journal("add")), 1)

    def test_add_bad_values_refused(self):
        for argv in (["add", "t-1", "--job", "b", "--kind", "gpu", "--role", "r"],
                     ["add", "t-1", "--job", "b", "--kind", "working", "--role", "r", "--source", "other"],
                     ["add", "t 1", "--job", "b", "--kind", "working", "--role", "r"],
                     ["add", "t-1", "--job", "b/c", "--kind", "working", "--role", "r"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(*argv)
                self.assertNotEqual(code, 0, out + err)
                self.assertFalse(os.path.exists(self.path("waiting.json")))

    def test_block_unblock_drop(self):  # AC5.2
        self.add("t-1", "build")
        self.add("t-2", "build")
        self.ok("block", "t-1/build", "--why", "waits for d-7")
        self.assertEqual(self.waiting()["jobs"][0]["blockedBy"], "waits for d-7")
        self.assertEqual(self.journal("block")[0]["why"], "waits for d-7")
        self.ok("unblock", "t-1/build")
        self.assertIsNone(self.waiting()["jobs"][0]["blockedBy"])
        self.assertEqual(len(self.journal("unblock")), 1)
        self.ok("drop", "t-2/build", "--why", "the person parked t-2")
        self.assertEqual(self.ids(), ["t-1/build"])
        line = self.journal("drop")[0]
        self.assertEqual((line["task"], line["job"], line["why"]), ("t-2", "build", "the person parked t-2"))

    def test_unknown_id_refused(self):
        self.add("t-1", "build")
        before = read(self.path("waiting.json"))
        for argv in (["block", "t-9/x", "--why", "w"], ["unblock", "t-9/x"], ["drop", "t-9/x", "--why", "w"],
                     ["start", "t-9/x", "--worker", "t60"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(*argv)
                self.assertEqual(code, 1, out + err)
                self.assertIn("not waiting", err)
                self.assertEqual(read(self.path("waiting.json")), before)

    def test_start_removes_and_prints_the_place(self):  # AC5.2, AC4.1
        self.add("t-1", "design", "no-test", "design")
        self.add("t-2", "build")
        out = self.ok("start", "t-1/design", "--worker", "t60")
        self.assertIn("record place: working", out)
        self.assertEqual(self.ids(), ["t-2/build"])
        line = self.journal("start")[0]
        self.assertEqual((line["id"], line["worker"], line["place"]), ("t-1/design", "t60", "working"))

    def test_start_takes_the_extra_place(self):
        self.live = {"a", "b", "c", "d"}
        self.workers(("a", "t-a", "working", False), ("b", "t-b", "working", False), ("c", "t-c", "suite", False),
                     ("d", "t-d", None, False))
        self.add("t-1", "review", "no-test", "reviewer")
        self.assertIn("record place: no-test", self.ok("start", "t-1/review", "--worker", "t60"))

    def test_start_does_not_count_its_own_worker_twice(self):  # the PA may record the worker first
        dump({"rules": [{"id": "r-one", "text": "x", "status": "active", "order": 1, "places": {"working": 1}}]},
             self.path("rules.json"))
        self.live = {"t60"}
        self.workers(("t60", "t-1", None, False))
        self.add("t-1", "build")
        self.assertIn("record place: working", self.ok("start", "t-1/build", "--worker", "t60"))

    def test_start_refused_when_it_would_not_start(self):  # D4
        self.add("t-1", "build", "working", "build", "--blocked-by", "waits for d-1")
        code, out, err = self.run_main("start", "t-1/build", "--worker", "t60")
        self.assertEqual(code, 1, out + err)
        self.assertIn("blocked: waits for d-1", err)
        self.assertEqual(self.ids(), ["t-1/build"])
        self.ok("unblock", "t-1/build")
        dump({"rules": [{"id": "r-one", "text": "x", "status": "active", "order": 1, "places": {"working": 1}}]},
             self.path("rules.json"))
        self.live = {"t51"}
        self.workers(("t51", "t-0", "working", False))
        code, out, err = self.run_main("start", "t-1/build", "--worker", "t60")
        self.assertEqual(code, 1, out + err)
        self.assertIn("kind full: working 1 of 1", err)
        self.live = None
        code, out, err = self.run_main("start", "t-1/build", "--worker", "t60")
        self.assertEqual(code, 1, out + err)
        self.assertIn("terminal list unavailable", err)
        self.assertEqual(self.ids(), ["t-1/build"])
        self.assertEqual(self.journal("start"), [])

    def test_start_refused_when_the_pool_is_full(self):  # D3, D4: a no-test holder in a working place counts
        dump({"rules": [{"id": "r-four", "text": "x", "status": "active", "order": 1, "places": {"working": 4}}]},
             self.path("rules.json"))
        self.live = {"a", "b", "c", "d"}
        self.workers(("a", "t-a", "working", False), ("b", "t-b", "working", False), ("c", "t-c", "working", False),
                     ("d", "t-d", "no-test", False))
        self.add("t-1", "build")
        code, out, err = self.run_main("start", "t-1/build", "--worker", "t60")
        self.assertEqual(code, 1, out + err)
        self.assertIn("kind full: working 4 of 4", err)
        self.assertEqual(self.ids(), ["t-1/build"])
        self.assertEqual(self.journal("start"), [])

    def test_start_refused_under_a_hold(self):  # D4, AC7.4
        self.add("t-1", "build")
        with open(self.path("journal.jsonl"), "a") as f:
            f.write(json.dumps({"at": "2026-11-19T00:00:00Z", "trigger": "message", "hold": "on", "why": "hold all"}) + "\n")
        code, out, err = self.run_main("start", "t-1/build", "--worker", "t60")
        self.assertEqual(code, 1, out + err)
        self.assertIn("hold on", err)
        self.assertEqual(self.ids(), ["t-1/build"])

    def test_whole_writes_no_temp_left(self):  # AC5.3
        self.add("t-1", "build")
        self.add("t-2", "build")
        self.ok("drop", "t-1/build", "--why", "w")
        left = [n for n in os.listdir(self.dir) if n.startswith(".waiting.json")]
        self.assertEqual(left, [])
        self.assertTrue(read(self.path("waiting.json")).endswith("\n"))

    def test_lock_held_refused(self):  # AC5.3
        pa_places.LOCK_WAIT = 0.3
        with open(self.path(".places.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out, err = self.run_main("add", "t-1", "--job", "build", "--kind", "working", "--role", "build")
        self.assertEqual(code, 1, out + err)
        self.assertIn("another pa-places", err)
        self.assertFalse(os.path.exists(self.path("waiting.json")))

    def test_worker_refused_by_task_record(self):  # AC5.3
        tree = os.path.join(TMP, "wt-refused")
        dump({"task": "t-1", "space": SPACE}, os.path.join(tree, ".claude", "pa-task.json"))
        os.makedirs(os.path.join(tree, "src"), exist_ok=True)
        os.chdir(os.path.join(tree, "src"))
        code, out, err = self.run_main("add", "t-1", "--job", "build", "--kind", "working", "--role", "build")
        self.assertEqual(code, 1, out + err)
        self.assertIn("workers do not run pa-places", err)
        self.assertFalse(os.path.exists(self.path("waiting.json")))

    def test_worker_refused_by_ledger_address(self):  # AC5.3
        self.workers(("t77", "t-1", "working", False))
        os.environ["HC_TERMINAL_ID"] = "t77"
        code, out, err = self.run_main("add", "t-1", "--job", "build", "--kind", "working", "--role", "build")
        self.assertEqual(code, 1, out + err)
        self.assertIn("workers do not run pa-places", err)
        self.assertFalse(os.path.exists(self.path("waiting.json")))

    def test_invalid_waiting_list_refused(self):
        for text in ("{nope", '{"version": 1, "jobs": 3}', '{"version": 1, "jobs": [{"id": 5}]}', "[]"):
            with self.subTest(text=text):
                write(self.path("waiting.json"), text)
                code, out, err = self.run_main("add", "t-1", "--job", "b", "--kind", "working", "--role", "r")
                self.assertEqual(code, 1, out + err)
                self.assertIn("not a valid waiting list", err)
                self.assertEqual(read(self.path("waiting.json")), text)
                code, out, err = self.run_main("next")
                self.assertEqual(code, 1, out + err)
                self.assertIn("not a valid waiting list", err)

    def test_never_writes_the_order_or_the_rules(self):  # AC5.4, AC3.1
        order = '{"version": 1, "order": ["t-2/build"]}\n'
        write(self.path("start-order.json"), order)
        rules = read(self.path("rules.json"))
        stamps = {n: os.stat(self.path(n)).st_mtime_ns for n in ("start-order.json", "rules.json")}
        self.add("t-1", "build")
        self.add("t-2", "build")
        self.ok("block", "t-1/build", "--why", "w")
        self.ok("unblock", "t-1/build")
        self.ok("next")
        self.ok("show")
        self.ok("limits")
        self.ok("start", "t-2/build", "--worker", "t60")
        self.ok("drop", "t-1/build", "--why", "w")
        self.assertEqual(read(self.path("start-order.json")), order)
        self.assertEqual(read(self.path("rules.json")), rules)
        self.assertEqual({n: os.stat(self.path(n)).st_mtime_ns for n in stamps}, stamps)
        source = read(os.path.join(TOOLS, "pa_places.py"))
        self.assertNotIn("write_json(order_path", source)
        for name in pa_places.parser()._subparsers._group_actions[0].choices:
            self.assertNotIn(name, ("order", "move", "reorder", "set-limit", "limit"))


# ---- next and show (AC7.1-7.4, AC8.1, AC8.2) ---------------------------------------------------------------------

class Next(Base):
    def mockup(self):
        self.live = {"t51", "t53", "t54", "t56"}
        self.workers(("t51", "t-020", "working", False), ("t53", "t-041", "suite", False),
                     ("t54", "t-055", "suite", False), ("t56", "t-077", "working", False),
                     ("t40", "t-001", "working", True), ("t41", "t-002", "working", False))  # finished; not live
        self.add("t-020", "verify-2", "suite", "verifier")
        self.add("t-060", "merge-check", "suite", "verifier", "--source", "queue")
        self.add("t-083", "design", "no-test", "design")

    def test_mockup_text(self):
        self.mockup()
        lines = self.ok("next").splitlines()
        body = [l for l in lines if l[:2] in ("1.", "2.", "3.")]
        self.assertEqual(len(body), 3, lines)
        self.assertIn("t-020/verify-2", body[0])
        self.assertIn("skip: kind full: suite 2 of 2", body[0])
        self.assertIn("(merge queue, fixed)", body[1])
        self.assertIn("skip: kind full: suite 2 of 2", body[1])
        self.assertIn("t-083/design", body[2])
        self.assertIn("start", body[2])
        self.assertIn("record place: no-test", body[2])

    def test_mockup_json(self):
        self.mockup()
        data = json.loads(self.ok("next", "--json"))
        self.assertEqual([j["id"] for j in data["jobs"]], ["t-020/verify-2", "t-060/merge-check", "t-083/design"])
        self.assertEqual([j["action"] for j in data["jobs"]], ["skip", "skip", "start"])
        self.assertEqual(data["jobs"][2]["place"], "no-test")
        self.assertEqual(data["limits"], {"working": 4, "no-test": 1, "suite": 2})
        self.assertEqual(data["hold"], "off")
        self.assertEqual(data["order"]["error"], "missing")

    def test_ac71_text(self):
        self.live = {"a", "b"}
        self.workers(("a", "t-a", "suite", False), ("b", "t-b", "suite", False))
        self.add("t-1", "verify", "suite", "verifier")
        self.add("t-2", "verify", "suite", "verifier")
        self.add("t-3", "build")
        data = json.loads(self.ok("next", "--json"))
        self.assertEqual([j["action"] for j in data["jobs"]], ["skip", "skip", "start"])

    def test_ac73_blocked_text(self):
        self.add("t-1", "build", "working", "build", "--blocked-by", "waits for d-192 (plan approval)")
        self.assertIn("blocked: waits for d-192 (plan approval)", self.ok("next"))

    def hold_line(self, value, extra=None):
        line = {"at": "2026-11-19T00:00:00Z", "trigger": "message", "why": "the person said so"}
        if value is not None:
            line["hold"] = value
        line.update(extra or {})
        with open(self.path("journal.jsonl"), "a") as f:
            f.write(json.dumps(line) + "\n")

    def test_ac74_hold_on_then_off(self):
        self.add("t-1", "build")
        self.hold_line("on")
        data = json.loads(self.ok("next", "--json"))
        self.assertEqual(data["hold"], "on")
        self.assertEqual([j["action"] for j in data["jobs"]], ["hold"])
        self.assertIn("hold on", self.ok("next"))
        self.hold_line(None)  # a line with no hold key changes nothing
        self.assertEqual(json.loads(self.ok("next", "--json"))["hold"], "on")
        self.hold_line("off")
        data = json.loads(self.ok("next", "--json"))
        self.assertEqual(data["hold"], "off")
        self.assertEqual([j["action"] for j in data["jobs"]], ["start"])

    def test_hold_state_reading(self):  # D1
        H = pa_places.hold_state
        self.assertEqual(H(None), "off")  # no journal
        self.assertEqual(H(""), "off")
        self.assertEqual(H('{"trigger": "pass"}\n'), "off")
        self.assertEqual(H('{"hold": "on"}\n{"trigger": "pass"}\n'), "on")
        self.assertEqual(H('{"hold": true}\n'), "on")
        self.assertEqual(H('{"hold": "on"}\n{"hold": false}\n'), "off")
        self.assertEqual(H('{"hold": "on"}\n{"hold": "off"}\n'), "off")
        self.assertEqual(H('{"hold": "maybe"}\n'), "unknown")
        self.assertEqual(H('{"hold": "off"}\nnot json\n'), "unknown")  # cannot tell what that line said
        self.assertEqual(H('not json\n{"hold": "off"}\n'), "off")
        self.assertEqual(H('not json\n'), "unknown")
        self.assertEqual(H('[1]\n{"trigger": "x"}\n'), "off")  # JSON, not a hold line

    def test_ac74_hold_unknown_starts_none(self):
        self.add("t-1", "build")
        self.hold_line("on")
        with open(self.path("journal.jsonl"), "a") as f:
            f.write("{broken\n")
        out = self.ok("next")
        self.assertIn("hold unknown", out)
        self.assertNotIn("record place", out)

    def test_terminal_list_unavailable(self):
        self.add("t-1", "build")
        self.live = None
        out = self.ok("next")
        self.assertIn("terminal list unavailable", out)
        self.assertEqual([j["action"] for j in json.loads(self.ok("next", "--json"))["jobs"]], ["hold"])

    def test_dead_or_finished_workers_hold_nothing(self):
        dump({"rules": [{"id": "r-one", "text": "x", "status": "active", "order": 1, "places": {"working": 1}}]},
             self.path("rules.json"))
        self.live = {"t40"}
        self.workers(("t40", "t-0", "working", True), ("t41", "t-0", "working", False))
        self.add("t-1", "build")
        self.assertEqual(json.loads(self.ok("next", "--json"))["jobs"][0]["action"], "start")

    def test_machine_prefixed_address_counts(self):
        dump({"rules": [{"id": "r-one", "text": "x", "status": "active", "order": 1, "places": {"working": 1}}]},
             self.path("rules.json"))
        self.live = {"t40"}
        self.workers(("m1/t40", "t-0", "working", False))
        self.add("t-1", "build")
        self.assertEqual(json.loads(self.ok("next", "--json"))["jobs"][0]["action"], "skip")

    def test_ac81_person_order_used(self):
        self.add("t-1", "a")
        self.add("t-q", "check", "suite", "verifier", "--source", "queue")
        self.add("t-2", "b")
        self.add("t-3", "c")
        write(self.path("start-order.json"), json.dumps({"version": 1, "order": ["t-3/c", "t-1/a"]}))
        data = json.loads(self.ok("next", "--json"))
        self.assertEqual([j["id"] for j in data["jobs"]], ["t-3/c", "t-q/check", "t-1/a", "t-2/b"])
        self.assertIsNone(data["order"]["error"])
        self.assertIn("order: the person's", self.ok("next"))

    def test_ac82_unreadable_order_said(self):
        self.add("t-1", "a")
        self.add("t-2", "b")
        write(self.path("start-order.json"), "{nope")
        out = self.ok("next")
        self.assertIn("start order unreadable: start-order.json is not JSON; the Project Agent's order is used", out)
        self.assertLess(out.index("t-1/a"), out.index("t-2/b"))
        os.unlink(self.path("start-order.json"))
        self.assertIn("no start order yet; the Project Agent's order is used", self.ok("next"))

    def test_empty_waiting_list(self):
        self.assertIn("nothing waiting", self.ok("next"))
        self.assertEqual(json.loads(self.ok("next", "--json"))["jobs"], [])

    def test_show_places_and_waiting(self):
        self.mockup()
        out = self.ok("show")
        self.assertIn("Working · 4 of 4", out)
        self.assertIn("No-test extra · 0 of 1", out)
        self.assertIn("Suite step · 2 of 2", out)
        self.assertIn("t53 · t-041", out)
        self.assertNotIn("t40", out)
        self.assertNotIn("t41", out)
        self.assertIn("Waiting · 3", out)
        data = json.loads(self.ok("show", "--json"))
        self.assertEqual(len(data["holders"]), 4)
        self.assertEqual(sorted(h["address"] for h in data["holders"]), ["t51", "t53", "t54", "t56"])

    def test_show_kind_not_recorded(self):  # AC1.3 on the tool's side
        self.live = {"t51"}
        self.workers(("t51", "t-1", None, False))
        self.assertIn("kind not recorded", self.ok("show"))

    def test_show_over_limit(self):  # AC1.4 on the tool's side: shown, nothing stopped
        dump({"rules": [{"id": "r-one", "text": "x", "status": "active", "order": 1, "places": {"working": 1}}]},
             self.path("rules.json"))
        self.live = {"a", "b"}
        self.workers(("a", "t-a", "working", False), ("b", "t-b", "working", False))
        self.assertIn("Working · 2 of 1", self.ok("show"))

    def test_read_commands_allowed_in_a_worker(self):  # only writes are refused
        self.workers(("t77", "t-1", "working", False))
        os.environ["HC_TERMINAL_ID"] = "t77"
        self.ok("next")
        self.ok("show")
        self.ok("limits")


# ---- wiring: install, parse check, guard (AC5.3, AC5.4) ----------------------------------------------------------

class Wiring(unittest.TestCase):
    def test_install_and_parse(self):
        lines = [l for l in read(os.path.join(REPO, "install.sh")).splitlines() if "pa_places.py" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("for n in pa-places; do", lines[0])
        line = next(l for l in read(os.path.join(REPO, "tests", "run-all.sh")).splitlines() if "parses as Python 3.7" in l)
        self.assertIn("runtime/tools/pa_places.py", line)


if __name__ == "__main__":
    unittest.main()
