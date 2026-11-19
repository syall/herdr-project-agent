"""pa-full-suite's remote mode (t-089 J2): the full suite on a configured machine, cloud first, Mac fallback.
Nothing leaves this Mac: hc is tests/stub-hc (a machine is a temp folder), the machine names are invented
(box-1, box-2), and the temp HERDR_PA_HOME, space and worktree come from test_pa_suite's fixture.

  x0  no suiteMachines: no hc call, today's lines and record          x10 --status on the machine, --wait's exit
  x1  bad entries named, cap 1..32 (more is used as 32)              x11 combined exit, places line, record keys
  x2  probe: unreachable reasons, first reachable, none -> Mac        x12 machine lost -> 124
  x3  busy machine -> waits, then the Mac after PREFER_WAIT          x13 TERM -> remote ended and cleaned, 143
  x4  cap per machine                                                x14 leftovers of earlier runs swept
  x5  --on M (no fallback, exit 2), --local                          x15 local part under the machine slot
  x6  dirty tree, another command, --module stay local               x16 part medians per place
  x7  only the pinned commit arrives                                 x17 --places
  x8  setup failures -> the Mac in the same call                     x18 docs and help
  x9  every remote argv a fixed form under /tmp/pa-suite-<run>      x19 the profile's hooks/tests.sh full (d-222)
                                                                     x20 each probed tool is the one that runs
                                                                     x21 the suite itself run on a suite machine
t-106 J1, a profile's step run (tests.remoteFull): x22 when it applies (R1), x23 the entry's H-cases (R2), x24 the
facts hook's H-cases (R3), x25 what is sent (R5), x26 probe and prepare, prepare's H-cases (R6, R7), x27 exit,
record and output (R13-R15), x28 cleanup (R16), x29 generic words and docs (R17), x30 no step: today's run.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pa_suite as tps  # noqa: E402 (its fixture: only names bound here are collected)
from test_pa_suite import pa, pa_hooks, pa_suite, dump, write, sh  # noqa: E402

REPO = os.path.realpath(os.path.join(HERE, ".."))
STUB = os.path.join(HERE, "stub-hc")
BIN = os.path.join(tps.TMP, "rbin")
PA_FULL_SUITE = os.path.join(BIN, "pa-full-suite")
RUN = re.compile(r"pa-full-suite: run ([0-9a-f]{12})")

FAKE_RUN_ALL = r"""#!/bin/sh
# A fake tests/run-all.sh for the remote cases: each place's part prints what it saw.
case "${1:-}" in
  --except-integration)
    echo "fake remote part in $(pwd)"
    echo "env HERDR_PA_HOME=$HERDR_PA_HOME TMPDIR=$TMPDIR HOME=$HOME XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-} PCF=[${PA_CHANGED_FILES-unset}] PYC=${PYTHONDONTWRITEBYTECODE:-}"
    hc ls > ../hc-said 2>&1; echo "hc-exit $?: $(cat ../hc-said)"
    echo "heads: $(git bundle list-heads ../b.bundle)"
    echo "refs: $(git for-each-ref --format='%(refname)' | tr '\n' ' ')"
    echo "files: $(ls -A | tr '\n' ' ')"
    echo "status: [$(git status --porcelain | tr '\n' ' ')]"
    echo "python3 ran: $(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>&1)"
    mkdir -p "$HERDR_PA_HOME/suite-runs"
    printf '{"run": "rr", "parts": {"total": 3, "done": 2, "running": [], "failed": [], "hung": []}}' > "$HERDR_PA_HOME/suite-runs/rr.json"
    if [ -n "${FAKE_GO:-}" ]; then while [ ! -f "$FAKE_GO" ]; do sleep 0.1; done; fi
    i=0; while [ "$i" -lt "${FAKE_LINES:-0}" ]; do echo "line $i"; i=$((i + 1)); done
    echo "=== summary (each part's whole output: /tmp/x)"
    echo "  unit tests: test_a: ok (3s)"
    if [ "${FAKE_REMOTE_EXIT:-0}" = 0 ]; then echo "  unit tests: test_b: ok (4s)"; else echo "  unit tests: test_b: FAILED (4s)"; fi
    exit "${FAKE_REMOTE_EXIT:-0}" ;;
  --only-integration)
    echo "fake local part in $(pwd) slot=${PA_SUITE_SLOT:-}"
    if [ -n "${FAKE_SLOT_PROBE:-}" ]; then "$FAKE_PY" "$FAKE_SLOT_PROBE" slot --for "tests/run-all.sh --only-integration" -- sh -c 'echo "hold: $PA_SUITE_HOLD"'; fi
    if [ -n "${FAKE_MAC_SLOT:-}" ]; then "$FAKE_PY" -c "import fcntl,sys
f=open(sys.argv[1],'a+')
try:
    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print('mac slot free')
except OSError:
    print('mac slot locked')" "$FAKE_MAC_SLOT"; fi
    if [ -n "${FAKE_LOCAL_HOLD:-}" ]; then while [ ! -f "$FAKE_LOCAL_HOLD" ]; do sleep 0.1; done; fi
    if [ "${FAKE_LOCAL_EXIT:-0}" = 0 ]; then echo "  pa-session integration (private hc core): ok (5s)"; else echo "  pa-session integration (private hc core): FAILED (5s)"; fi
    exit "${FAKE_LOCAL_EXIT:-0}" ;;
  *)
    echo "fake whole run on this Mac"
    echo "  unit tests: test_a: ok (9s)"
    echo "  pa-session integration (private hc core): ok (6s)"
    exit "${FAKE_EXIT:-0}" ;;
esac
"""

HOLDER = tps.HOLDER


def setUpModule():
    tps.setUpModule()
    os.makedirs(BIN, exist_ok=True)
    for name, target in (("hc", STUB), ("pa-full-suite", os.path.join(REPO, "runtime", "tools", "pa_hooks.py"))):
        if not os.path.lexists(os.path.join(BIN, name)):
            os.symlink(target, os.path.join(BIN, name))
    write(os.path.join(BIN, "node"), "#!/bin/sh\necho v24.0.0\n")
    os.chmod(os.path.join(BIN, "node"), 0o755)


tearDownModule = tps.tearDownModule


class Remote(tps.Base):
    machines = [{"machine": "box-1", "cap": 1}]

    def setUp(self):
        super().setUp()
        write(os.path.join(self.wt, "tests", "run-all.sh"), FAKE_RUN_ALL)
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "fake run-all")
        self.set_command("sh tests/run-all.sh")
        self.stub = os.path.join(self.tmp, "stub")
        os.makedirs(self.stub)
        self.log = os.path.join(self.tmp, "hc.log")
        self.saved = {}
        self.setenv(PATH=BIN + os.pathsep + os.environ.get("PATH", ""), STUB_HC_ROOT=self.stub, STUB_HC_LOG=self.log,
                    STUB_HC_MACHINES="box-1 box-2", PA_SUITE_REMOTE_POLL="0.2", PA_SUITE_PREFER_WAIT="1",
                    PA_SUITE_LOST_GRACE="1.5", PA_SUITE_HEARTBEAT="60", FAKE_PY=sys.executable)
        for k in ("STUB_HC_FAIL", "STUB_HC_SUB", "FAKE_GO", "FAKE_LINES", "FAKE_REMOTE_EXIT", "FAKE_LOCAL_EXIT",
                  "FAKE_LOCAL_HOLD", "FAKE_EXIT", "FAKE_SLOT_PROBE", "FAKE_MAC_SLOT", "PA_SUITE_SLOT", "PA_SUITE_CAP"):
            self.setenv(**{k: None})
        if self.machines is not None:
            self.configure(self.machines)
        self.addCleanup(self.end_terminals)

    def setenv(self, **values):
        for k, v in values.items():
            self.saved.setdefault(k, os.environ.get(k))
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if not getattr(self, "_restoring", False):
            self._restoring = True
            self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.saved, self._restoring = {}, False

    def configure(self, machines):
        cfg = pa.read_json(os.path.join(pa.ROOT, "config.json"), {}) or {}
        cfg["suiteMachines"] = machines
        dump(cfg, os.path.join(pa.ROOT, "config.json"))

    def end_terminals(self):
        for k in ("FAKE_GO", "FAKE_LOCAL_HOLD"):
            if os.environ.get(k):
                write(os.environ[k], "")
        for root, _, files in os.walk(self.stub):
            for name in files:
                if root.endswith("terminals") and name.endswith(".json"):
                    try:
                        with open(os.path.join(root, name)) as f:
                            os.killpg(json.load(f)["pid"], signal.SIGKILL)
                    except (OSError, ValueError, KeyError):
                        pass

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def run_id(self, out):
        m = RUN.search(out)
        self.assertTrue(m, out)
        return m.group(1)

    def output(self, run):
        return pa.read_text(os.path.join(self.dir, "evidence", "t-1", "full-suite-{}.out".format(run)))

    def build_log(self):
        return pa.read_text(os.path.join(self.dir, "evidence", "t-1", "build.log"))

    def remote_folder(self, run, machine="box-1"):
        return os.path.join(self.stub, machine, "tmp", "pa-suite-" + run)

    def progress(self, run):
        return pa_suite.read_progress(os.path.join(pa_suite.runs_dir(), run + ".json"))

    def hold(self, path):
        """Another process holding the slot file at path (an flock), until the test ends."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ready = os.path.join(self.tmp, "ready-" + os.path.basename(os.path.dirname(path)) + os.path.basename(path))
        script = os.path.join(self.tmp, "holder.py")
        write(script, HOLDER)
        p = subprocess.Popen([sys.executable, script, path, ready])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        end = time.time() + 20
        while not os.path.exists(ready) and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(ready))
        return p

    def machine_slot(self, machine, i=0):
        return os.path.join(pa_suite.slot_dir(), machine, "slot-{}".format(i))

    def background(self, *args):
        log = os.path.join(self.tmp, "bg.out")
        with open(log, "w") as f:
            p = subprocess.Popen([sys.executable, PA_FULL_SUITE] + list(args), cwd=self.wt, stdin=subprocess.DEVNULL,
                                 stdout=f, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p, log

    def until(self, test, limit=30):
        end = time.time() + limit
        while time.time() < end:
            got = test()
            if got:
                return got
            time.sleep(0.1)
        return None


class X0NoConfig(Remote):
    machines = None

    def test_x0a_no_key_no_hc_call_and_todays_lines(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])
        first = out.splitlines()[0]
        self.assertRegex(first, r"^pa-full-suite: run [0-9a-f]{12}: output in ")
        self.assertIn("fake whole run on this Mac", self.output(self.run_id(out)))
        line = self.lines()[-1]
        for key in ("machine", "os", "tools", "places", "note"):
            self.assertNotIn(key, line)
        self.assertNotIn(" on ", self.build_log().splitlines()[0])

    def test_x0b_an_empty_list_is_no_remote_mode(self):
        self.configure([])
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertRegex(out.splitlines()[0], r"^pa-full-suite: run [0-9a-f]{12}: output in ")


class X1Config(Remote):
    def test_x1a_bad_entries_are_named_once_and_good_ones_used(self):
        self.configure([{"machine": "bad/name"}, {"machine": "box-2", "cap": 0}, {"machine": "box-2", "cap": "2"},
                        {"machine": "box-2", "cap": True}, "box-2", {"machine": "box-1"}])
        code, out = self.main()
        self.assertEqual(code, 0, out)
        for bad in ("bad/name", "cap 0", "cap '2'", "cap True", "'box-2'"):
            self.assertEqual(out.count(bad), 1, (bad, out))
        self.assertIn("ignored", out)
        self.assertEqual(self.lines()[-1].get("machine"), "box-1")

    def test_x1b_cap_whole_number_1_to_32_more_used_as_32(self):
        good, bad = pa_suite.machines({"suiteMachines": [{"machine": "box-1", "cap": 12}, {"machine": "box-2", "cap": 40}]})
        self.assertEqual(good, [{"machine": "box-1", "cap": 12}, {"machine": "box-2", "cap": 32}])
        self.assertEqual(len(bad), 1)
        self.assertIn("used as 32", bad[0])
        good, bad = pa_suite.machines({"suiteMachines": [{"machine": "box-1"}]})
        self.assertEqual(good, [{"machine": "box-1", "cap": 1}])
        self.assertEqual(bad, [])
        self.assertEqual(pa_suite.machines({}), ([], []))


class X2Choice(Remote):
    machines = [{"machine": "box-1"}, {"machine": "box-2"}]

    def test_x2a_the_first_reachable_machine_is_chosen(self):
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("needs_login", out)
        self.assertIn(" on box-2 (", out.splitlines()[[i for i, l in enumerate(out.splitlines()) if RUN.search(l)][0]])
        self.assertEqual(self.lines()[-1]["machine"], "box-2")

    def test_x2b_a_missing_tool_is_not_reachable(self):
        self.setenv(STUB_HC_FAIL="notool:box-1")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("node", out)
        self.assertEqual(self.lines()[-1]["machine"], "box-2")

    def test_x2c_none_reachable_runs_on_the_mac_at_once_and_says_why(self):
        self.setenv(STUB_HC_FAIL="down:box-1,down:box-2")
        began = time.time()
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertLess(time.time() - began, 20)
        run_line = [l for l in out.splitlines() if RUN.search(l)][0]
        self.assertIn("on this Mac (", run_line)
        self.assertIn("fake whole run on this Mac", self.output(self.run_id(out)))
        line = self.lines()[-1]
        self.assertNotIn("machine", line)
        self.assertIn("needs_login", line.get("note", ""))
        self.assertIn("needs_login", self.build_log())
        self.assertFalse([c for c in self.calls() if c[0] == "run"])


class X3Busy(Remote):
    def test_x3_a_busy_machine_is_waited_for_then_the_mac_is_taken(self):
        self.hold(self.machine_slot("box-1"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("waiting", out)
        self.assertIn("now on this Mac", out)
        self.assertIn("fake whole run on this Mac", self.output(self.run_id(out)))
        self.assertIn("busy", self.lines()[-1].get("note", ""))
        self.assertFalse([c for c in self.calls() if c[0] == "run"])


class X4Cap(Remote):
    machines = [{"machine": "box-1", "cap": 2}]

    def test_x4_a_cap_of_two_lets_a_second_run_in_and_the_mac_slot_stays_free(self):
        self.hold(self.machine_slot("box-1", 0))
        self.setenv(FAKE_MAC_SLOT=os.path.join(pa_suite.slot_dir(), "slot-0"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1]["machine"], "box-1")
        self.assertNotIn("now on this Mac", out)
        self.assertIn("mac slot free", self.output(self.run_id(out)))


class X5Forced(Remote):
    def test_x5a_on_an_unreachable_machine_is_exit_2_no_fallback(self):
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out = self.main("--on", "box-1")
        self.assertEqual(code, 2, out)
        self.assertIn("needs_login", out)
        self.assertEqual(self.lines(), [])

    def test_x5b_on_an_unconfigured_machine_is_exit_2(self):
        code, out = self.main("--on", "box-2")
        self.assertEqual(code, 2, out)
        self.assertIn("box-2", out)
        self.assertEqual(self.calls(), [])

    def test_x5c_on_with_a_setup_failure_is_exit_2(self):
        self.setenv(STUB_HC_FAIL="match:clone")
        code, out = self.main("--on", "box-1")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.lines(), [])
        self.assertEqual(os.listdir(os.path.join(self.stub, "box-1", "tmp")), [])

    def test_x5d_local_makes_no_hc_call(self):
        code, out = self.main("--local")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertIn("fake whole run on this Mac", self.output(self.run_id(out)))

    def test_x5e_on_runs_there(self):
        code, out = self.main("--on", "box-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1]["machine"], "box-1")


class X6StaysLocal(Remote):
    def check_local(self, word, *args):
        code, out = self.main(*args)
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in self.calls() if c[0] in ("run", "file")], self.calls())
        reasons = [l for l in out.splitlines() if "on this Mac" in l]
        self.assertTrue(reasons and word in " ".join(reasons), out)
        return out

    def test_x6a_uncommitted_file(self):
        write(os.path.join(self.wt, "a.txt"), "changed\n")
        self.check_local("uncommitted")

    def test_x6b_untracked_file(self):
        write(os.path.join(self.wt, "new.txt"), "n\n")
        self.check_local("untracked")

    def test_x6c_another_command(self):
        self.set_command("sh tests/run-all.sh; true")
        self.check_local("command")

    def test_x6d_module(self):
        write(os.path.join(self.wt, "tests", "test_x.py"), "")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "x")
        self.check_local("module", "--module", "test_x")


class X7Ship(Remote):
    def test_x7_only_the_pinned_commit_arrives(self):
        sh(self.wt, "git", "branch", "other")
        sh(self.wt, "git", "tag", "v1")
        head = sh(self.wt, "git", "rev-parse", "HEAD")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        text = self.output(self.run_id(out))
        self.assertIn("heads: {} HEAD\n".format(head), text)
        # A clone of a bundle holding HEAD alone has no ref at all (a detached HEAD): no branch, no tag travels.
        self.assertEqual(text.split("refs:", 1)[1].splitlines()[0].strip(), "")
        files = text.split("files: ", 1)[1].splitlines()[0].split()
        self.assertEqual(sorted(files), [".git", ".gitignore", "a.txt", "tests"])
        self.assertIn("status: []", text)
        self.assertNotIn("pa-task.json", text)

    def test_x7b_untracked_and_uncommitted_files_never_travel(self):
        # The run is refused remote (R6), so nothing is written there at all.
        write(os.path.join(self.wt, "secret.txt"), "s\n")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in self.calls() if c[0] == "file"])


class X8Setup(Remote):
    def test_x8a_a_tree_mismatch_falls_back_in_the_same_call(self):
        tree = sh(self.wt, "git", "rev-parse", "HEAD^{tree}")
        self.setenv(STUB_HC_SUB="{}={}".format(tree, "0" * 40))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        self.assertIn("now on this Mac", out)
        self.assertIn("fake whole run on this Mac", self.output(run))
        self.assertIn("tree", self.lines()[-1].get("note", ""))
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        self.assertFalse([c for c in self.calls() if c[0] == "run"])

    def test_x8b_a_write_failure_falls_back(self):
        self.setenv(STUB_HC_FAIL="write")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        self.assertIn("now on this Mac", out)
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        self.assertIn("now on this Mac", self.build_log())


class X9Argv(Remote):
    def test_x9_every_remote_argv_is_a_fixed_form_under_the_run_folder(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        folder = "/tmp/pa-suite-" + run
        calls = self.calls()
        self.assertTrue(calls)
        tools = set()
        for tool in self.lines()[-1]["tools"].values():
            tools.update([tool["path"], os.path.dirname(tool["path"])])
        tools = sorted(tools, key=len, reverse=True)
        kinds = set()
        for c in calls:
            text = " ".join(c)
            if c[0] == "exec":
                self.assertEqual(c[1:3], ["--on", "box-1"], c)
                if "uname" in text and "/tmp/" not in text:
                    kinds.add("probe")
                    continue
                kinds.add("exec")
            elif c[0] == "file":
                self.assertIn(c[1], ("read", "write"))
                kinds.add("file " + c[1])
            elif c[0] == "run":
                kinds.add("run")
                for flag in ("--bg", "--rm", "--no-notify"):
                    self.assertIn(flag, c)
                self.assertEqual(c[c.index("--on") + 1], "box-1")
                self.assertEqual(c[c.index("--name") + 1], "pa-suite-" + run)
                self.assertEqual(c[c.index("--cwd") + 1], folder + "/w")
                script = c[-1]
                self.assertIn("sh tests/run-all.sh --except-integration", script)
                for name in ("HERDR_PA_HOME=" + folder + "/home", "TMPDIR=" + folder + "/tmp", "XDG_CACHE_HOME=",
                             "XDG_CONFIG_HOME=", "XDG_DATA_HOME=", "XDG_STATE_HOME=", "PYTHONDONTWRITEBYTECODE=1",
                             "npm_config_cache=", "PA_CHANGED_FILES=", "NODE=", "HOME=" + folder + "/u"):
                    self.assertIn(name, script)
            elif c[0] in ("kill", "rm", "ls"):
                kinds.add(c[0])
            else:
                self.fail("unexpected hc call {}".format(c))
            for tool in tools:  # the probe's own answers (read there, never written)
                text = text.replace(tool, "")
            for path in re.findall(r"(?<![\w.$-])/[A-Za-z][^\s'\":;]*", text):
                self.assertTrue(path == folder or path.startswith(folder + "/") or path.startswith("/dev/null"),
                                "a remote argv names a path outside the run folder: {} in {}".format(path, c))
        self.assertTrue({"probe", "exec", "file write", "run", "file read"} <= kinds, kinds)
        rm = [c for c in calls if c[0] == "exec" and "rm" in c and "-rf" in c]
        self.assertTrue(rm and rm[-1][-1] == folder, calls)
        text = self.output(run)
        self.assertIn("hc-exit 2: hc is not available in a remote suite run", text)
        self.assertIn("PCF=[]", text)
        self.assertIn("PYC=1", text)
        self.assertFalse(os.path.exists(self.remote_folder(run)))


class X10Status(Remote):
    def test_x10_status_says_the_machine_and_the_remote_parts_and_wait_gives_the_exit(self):
        go = os.path.join(self.tmp, "go")
        self.setenv(FAKE_GO=go, FAKE_REMOTE_EXIT="3")
        p, log = self.background()
        got = self.until(lambda: "on box-1" in self.main("--status")[1] and "parts 2 of" in self.main("--status")[1])
        self.assertTrue(got, (self.main("--status"), pa.read_text(log)))
        run = self.run_id(pa.read_text(log))
        self.assertTrue(self.until(lambda: "fake remote part in" in (pa.read_text(
            os.path.join(self.dir, "evidence", "t-1", "full-suite-{}.out".format(run))) or "")))
        row = self.progress(run)
        self.assertEqual(row.get("machine"), "box-1")
        self.assertEqual(row.get("remote", {}).get("folder"), "/tmp/pa-suite-" + run)
        write(go, "")
        code, out = self.main("--wait", "--timeout", "60")
        self.assertEqual(code, 3, out)
        self.assertEqual(p.wait(timeout=60), 3, pa.read_text(log))


class X11Combined(Remote):
    def combined(self, remote, local):
        self.setenv(FAKE_REMOTE_EXIT=str(remote), FAKE_LOCAL_EXIT=str(local), FAKE_LINES="300")
        code, out = self.main("--again")
        return code, out, self.run_id(out)

    def test_x11a_remote_pass_local_fail_is_1(self):
        code, out, run = self.combined(0, 1)
        self.assertEqual(code, 1, out)
        text = self.output(run)
        self.assertIn("=== local part on this Mac", text)
        self.assertIn("=== places: box-1 (", text)
        self.assertIn("exit 0; local exit 1 -> exit 1", text)

    def test_x11b_remote_fail_local_pass_is_1(self):
        code, out, run = self.combined(1, 0)
        self.assertEqual(code, 1, out)
        self.assertIn("exit 1; local exit 0 -> exit 1", self.output(run))

    def test_x11c_both_pass_record_and_reuse(self):
        code, out, run = self.combined(0, 0)
        self.assertEqual(code, 0, out)
        text = self.output(run)
        self.assertIn("line 0\n", text)
        self.assertIn("line 299\n", text)
        self.assertEqual(text.count("line 299\n"), 1)
        line = self.lines()[-1]
        self.assertEqual(line["exit"], 0)
        self.assertEqual(line["machine"], "box-1")
        self.assertEqual(line["command"], "sh tests/run-all.sh")
        self.assertTrue(line["os"])
        self.assertIn("node", line["tools"])
        self.assertEqual([p["place"] for p in line["places"]], ["box-1", "local"])
        places = {p["name"]: p["place"] for p in line["parts"]}
        self.assertEqual(places, {"unit tests: test_a": "box-1", "unit tests: test_b": "box-1",
                                  "pa-session integration (private hc core)": "local"})
        self.assertIn(" on box-1 (", self.build_log())
        runs = pa_hooks.suite_runs(self.dir)
        self.assertEqual(runs[-1]["machine"], "box-1")
        self.assertTrue(runs[-1]["reusable"])
        before = len(self.calls())
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("already passed", out)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(self.progress(run).get("cleaned"), True)


class X12Lost(Remote):
    def test_x12_a_lost_machine_ends_with_124(self):
        self.setenv(STUB_HC_FAIL="lost:box-1", FAKE_GO=os.path.join(self.tmp, "go"),
                    FAKE_LOCAL_HOLD=os.path.join(self.tmp, "hold"))
        began = time.time()
        code, out = self.main()
        self.assertEqual(code, 124, out)
        self.assertLess(time.time() - began, 60)
        run = self.run_id(out)
        self.assertIn("machine box-1 lost after", self.output(run))
        self.assertIn("machine box-1 lost after", self.progress(run).get("note", ""))
        self.assertEqual(self.progress(run).get("exit"), 124)
        self.assertEqual(self.lines()[-1]["exit"], 124)
        rm = [c for c in self.calls() if c[0] == "exec" and "-rf" in c]
        self.assertTrue(rm, "the cleanup was not tried")


class X13Interrupt(Remote):
    def test_x13_term_ends_the_remote_terminal_and_cleans(self):
        self.setenv(FAKE_GO=os.path.join(self.tmp, "go"))
        p, log = self.background()
        self.assertTrue(self.until(lambda: RUN.search(pa.read_text(log) or "")), pa.read_text(log))
        run = self.run_id(pa.read_text(log))
        self.assertTrue(self.until(lambda: "fake remote part" in (self.output(run) or "")), pa.read_text(log))
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=60), 143, pa.read_text(log))
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        terms = os.path.join(self.stub, "box-1", "terminals")
        self.assertEqual([n for n in os.listdir(terms) if n.endswith(".json")], [])
        self.assertTrue([c for c in self.calls() if c[0] == "kill"])
        self.assertEqual(self.progress(run).get("exit"), 143)
        self.assertEqual(self.lines(), [])


class X14Sweep(Remote):
    def test_x14_leftovers_of_this_macs_earlier_runs_are_swept(self):
        old = "aaaaaaaaaaaa"
        os.makedirs(self.remote_folder(old))
        other = os.path.join(self.stub, "box-1", "tmp", "other")
        os.makedirs(other)
        dump({"run": old, "kind": "pa-full-suite", "state": "done", "machine": "box-1", "place": "box-1",
              "remote": {"folder": "/tmp/pa-suite-" + old, "terminal": "box-1/t99"}, "pid": 1},
             os.path.join(pa_suite.runs_dir(), old + ".json"))
        dump({"run": "../x", "kind": "pa-full-suite", "state": "done", "machine": "box-1", "place": "box-1",
              "remote": {"folder": "/tmp/pa-suite-../x", "terminal": "box-1/t98"}, "pid": 1},
             os.path.join(pa_suite.runs_dir(), "bad.json"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.remote_folder(old)))
        self.assertTrue(os.path.isdir(other))
        self.assertTrue(self.progress(old).get("cleaned"))
        for c in self.calls():
            self.assertNotIn("../x", " ".join(c))


class X15LocalPart(Remote):
    def test_x15_the_local_part_runs_under_the_machine_slot(self):
        self.setenv(FAKE_SLOT_PROBE=os.path.join(REPO, "runtime", "tools", "pa_suite.py"),
                    FAKE_MAC_SLOT=os.path.join(pa_suite.slot_dir(), "slot-0"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        text = self.output(self.run_id(out))
        self.assertIn("slot=" + self.machine_slot("box-1"), text)
        self.assertIn("hold: held by the caller", text)
        self.assertIn("mac slot free", text)


class X16Medians(Remote):
    machines = None

    def test_x16_part_medians_per_place(self):
        rows = [{"run": "a", "parts": [{"name": "p", "ok": True, "seconds": 100}], "seconds": 500},
                {"run": "b", "machine": "box-1", "parts": [{"name": "p", "ok": True, "seconds": 20, "place": "box-1"},
                                                            {"name": "i", "ok": True, "seconds": 50, "place": "local"}],
                 "seconds": 90}]
        local = {p["name"]: p["median"] for p in pa_suite.report(rows, place="local")}
        self.assertEqual(local, {"p": 100, "i": 50})
        remote = {p["name"]: p["median"] for p in pa_suite.report(rows, place="box-1")}
        self.assertEqual(remote, {"p": 20})
        self.assertEqual(pa_suite.median_total(rows, full=True, place="local"), (500, 1))
        self.assertEqual(pa_suite.median_total(rows, full=True, place="box-1"), (90, 1))
        self.assertEqual(len(pa_suite.report(rows)), 2)  # no place: every record, as before


class X17Places(Remote):
    machines = [{"machine": "box-1", "cap": 2}, {"machine": "box-2"}]

    def test_x17_places_lists_each_machine_and_the_mac(self):
        self.setenv(STUB_HC_FAIL="down:box-2")
        self.hold(self.machine_slot("box-1", 1))
        code, out = self.main("--places")
        self.assertEqual(code, 0, out)
        box1 = [l for l in out.splitlines() if l.startswith("box-1")][0]
        self.assertIn("reachable", box1)
        self.assertIn("cap 2", box1)
        self.assertIn("t-holder", box1)
        box2 = [l for l in out.splitlines() if l.startswith("box-2")][0]
        self.assertIn("not reachable", box2)
        self.assertIn("needs_login", box2)
        self.assertTrue([l for l in out.splitlines() if l.startswith("this Mac")], out)


class X18Docs(unittest.TestCase):
    def read(self, *path):
        with open(os.path.join(REPO, *path)) as f:
            return " ".join(f.read().split())

    def test_x18a_worker_and_sdlc_say_a_linux_pass_counts_and_local(self):
        for name in ("worker.md", "sdlc.md"):
            text = self.read("runtime", "core", name)
            self.assertIn("another machine", text, name)
            self.assertIn("pa-full-suite --local", text, name)

    def test_x18b_readme_names_the_key_with_an_invented_machine(self):
        text = self.read("README.md")
        self.assertIn('"suiteMachines"', text)
        self.assertIn("box-1", text)

    def test_x18c_help_and_docstring(self):
        r = subprocess.run([sys.executable, PA_FULL_SUITE, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        for flag in ("--local", "--on", "--places"):
            self.assertIn(flag, r.stdout)
        for flag in ("--local", "--on", "--places", "suiteMachines"):
            self.assertIn(flag, pa_suite.__doc__)

    def test_x18d_no_machine_name_outside_tests_and_readme(self):
        r = subprocess.run(["git", "grep", "-l", "box-1"], cwd=REPO, stdout=subprocess.PIPE, universal_newlines=True)
        files = [f for f in r.stdout.split() if not f.startswith("tests/")]
        self.assertEqual(files, ["README.md"])


PROFILE_RUNNER = r"""#!/bin/sh
# A fake profile test runner: in a plain git repo it runs tests/run-all.sh from the top level (as the real one does).
cd "$(git rev-parse --show-toplevel)" || exit 1
exec tests/run-all.sh
"""


class X19ProfileRunner(Remote):
    """d-222 A: a profile whose tests.fullIsRunAll declares that its own full command runs tests/run-all.sh in a plain
    git repo (unless a named file is in the worktree root or above it) goes remote there; elsewhere it stays local."""
    declared = {"command": "hooks/tests.sh full", "unlessFound": ["ws.marker"]}

    def setUp(self):
        super().setUp()
        os.chmod(os.path.join(self.wt, "tests", "run-all.sh"), 0o755)  # the runner runs only an executable one
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "run-all executable")
        self.use_profile("hooks/tests.sh full")

    def use_profile(self, full, declared="default"):
        folder = os.path.join(pa.ROOT, "profiles", "test")
        write(os.path.join(folder, "hooks", "tests.sh"), PROFILE_RUNNER)
        os.chmod(os.path.join(folder, "hooks", "tests.sh"), 0o755)
        tests = {"full": full}
        declared = self.declared if declared == "default" else declared
        if declared is not None:
            tests["fullIsRunAll"] = declared
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "tests": tests},
             os.path.join(folder, "profile.json"))
        agent = pa.read_json(os.path.join(self.dir, "agent.json"))
        agent.pop("commands", None)
        dump(agent, os.path.join(self.dir, "agent.json"))
        return os.path.join(folder, "hooks", "tests.sh")

    def check_local(self, word, *args):
        code, out = self.main(*args)
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in self.calls() if c[0] in ("run", "file")], self.calls())
        reasons = [l for l in out.splitlines() if "on this Mac" in l]
        self.assertTrue(reasons and word in " ".join(reasons), out)
        self.assertIn("fake whole run on this Mac", self.output(self.run_id(out)))
        return out

    def test_x19a_plain_git_repo_goes_to_the_machine(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        line = self.lines()[-1]
        self.assertEqual(line.get("machine"), "box-1", out)
        self.assertIn("fake remote part", self.output(self.run_id(out)))
        self.assertTrue([c for c in self.calls() if c[0] == "run"], self.calls())
        self.assertEqual(line.get("command"), os.path.join(pa.ROOT, "profiles", "test", "hooks", "tests.sh") + " full")

    def test_x19b_package_workspace_layout_stays_local(self):
        ws = os.path.join(self.tmp, "ws")
        write(os.path.join(ws, "ws.marker"), "a workspace\n")
        pkg = os.path.join(ws, "src", "Pkg")
        os.makedirs(os.path.dirname(pkg))
        shutil.move(self.wt, pkg)
        write(os.path.join(pkg, "Config"), "package.Pkg = {}\n")
        sh(pkg, "git", "add", "-A")
        sh(pkg, "git", "commit", "-q", "-m", "config")
        self.wt = pkg
        os.chdir(pkg)
        self.check_local("plain git repo")

    def test_x19c_another_profile_command_stays_local(self):
        runner = self.use_profile("hooks/tests.sh full")
        shutil.copy(runner, os.path.join(os.path.dirname(runner), "other.sh"))
        self.use_profile("hooks/other.sh full")
        self.check_local("command")

    def test_x19d_run_all_not_executable_stays_local(self):
        os.chmod(os.path.join(self.wt, "tests", "run-all.sh"), 0o644)
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "not executable")
        code, out = self.main()
        self.assertFalse([c for c in self.calls() if c[0] in ("run", "file")], self.calls())
        reasons = [l for l in out.splitlines() if "on this Mac" in l]
        self.assertTrue(reasons and "plain git repo" in " ".join(reasons), out)

    def test_x19e_affected_profile_command_stays_local(self):
        self.use_profile("hooks/tests.sh affected")
        self.check_local("command")

    def test_x19f_no_declaration_stays_local(self):
        self.use_profile("hooks/tests.sh full", declared=None)
        self.check_local("command")

    def test_x19g_a_bad_declaration_is_named_and_stays_local(self):
        c = "hooks/tests.sh full"
        for bad in ({"command": c, "unlessFound": [".."]}, {"command": c, "unlessFound": "ws.marker"}, ["ws.marker"],
                    {"unlessFound": ["ws.marker"]}, {"command": c, "unlessFound": ["a/b"]}):
            self.use_profile("hooks/tests.sh full", declared=bad)
            self.check_local("fullIsRunAll", "--again")  # the same tree each time

    def test_x19h_a_space_command_of_its_own_stays_local(self):
        self.set_command("sh tests/run-all.sh; true")
        self.check_local("command")

    def test_x19i_an_empty_list_means_any_plain_git_repo(self):
        self.use_profile("hooks/tests.sh full", declared={"command": "hooks/tests.sh full", "unlessFound": []})
        write(os.path.join(self.tmp, "ws.marker"), "")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1].get("machine"), "box-1", out)


class X20ProbedTools(Remote):
    """J2 round 3: a folder that holds git (or another probed tool) and also an older python3 must not shadow the
    python3 the probe found; the remote run uses each probed path and the record names it."""

    def test_x20_an_older_python3_beside_git_does_not_shadow_the_probed_one(self):
        good, gitdir = os.path.join(self.tmp, "goodpy"), os.path.join(self.tmp, "gitdir")
        os.makedirs(good)
        os.makedirs(gitdir)
        os.symlink(sys.executable, os.path.join(good, "python3"))
        os.symlink(shutil.which("git"), os.path.join(gitdir, "git"))
        write(os.path.join(gitdir, "python3"), "#!/bin/sh\necho old-python3-ran\n")
        os.chmod(os.path.join(gitdir, "python3"), 0o755)
        self.setenv(PATH=os.pathsep.join([good, gitdir, os.environ["PATH"]]))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        text = self.output(run)
        self.assertNotIn("old-python3-ran", text)
        ran = re.search(r"python3 ran: (\S+)", text)
        self.assertTrue(ran, text)
        self.assertEqual(os.path.realpath(ran.group(1)), os.path.realpath(sys.executable), text)
        line = self.lines()[-1]
        self.assertEqual(line.get("machine"), "box-1", out)
        self.assertEqual(line["tools"]["python3"]["path"], os.path.join(good, "python3"), line)
        self.assertEqual(line["tools"]["git"]["path"], os.path.join(gitdir, "git"), line)


class X21InsideARemoteRun(Remote):
    """J2 round 4: the suite itself running on a suite machine. There the PATH starts with the outer run's
    /tmp/pa-suite-<outer>/bin links, so the probe finds every tool under that real folder; stub-hc must not move those
    paths into its fake machine, or every fake setup fails and the run falls back to the Mac."""

    def test_x21_an_outer_runs_tool_links_are_not_mapped_into_the_fake_machine(self):
        outer = "/tmp/pa-suite-" + os.urandom(6).hex()
        self.assertFalse(os.path.lexists(outer), outer)
        os.makedirs(os.path.join(outer, "bin"))
        self.addCleanup(shutil.rmtree, outer, True)
        tools = {"python3": sys.executable}
        for tool in ("git", "sh", "node"):
            if shutil.which(tool):
                tools[tool] = shutil.which(tool)
        for tool, where in tools.items():
            os.symlink(where, os.path.join(outer, "bin", tool))
        self.setenv(PATH=os.pathsep.join([os.path.join(outer, "bin"), os.environ["PATH"]]))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        line = self.lines()[-1]
        self.assertEqual(line.get("machine"), "box-1", out)
        self.assertEqual(line["tools"]["python3"]["path"], os.path.join(outer, "bin", "python3"), line)
        text = self.output(self.run_id(out))
        ran = re.search(r"python3 ran: (\S+)", text)
        self.assertTrue(ran, text)
        self.assertEqual(os.path.realpath(ran.group(1)), os.path.realpath(sys.executable), text)
        self.assertTrue(os.path.isdir(os.path.join(outer, "bin")), "the outer run's folder is left alone")


# ---- t-106 J1: a profile's remote full command with a facts hook and a step (R1-R7, R13-R17) ----

STEP_TESTS_SH = r"""#!/bin/sh
# The stub profile's own full command (its local run): prints like a whole run on this Mac.
echo "fake whole run on this Mac ($1)"
echo "  unit tests: test_a: ok (9s)"
exit "${FAKE_EXIT:-0}"
"""

STEP_FACTS = r"""#!/bin/sh
# An invented facts hook: answers for the root it is given (or prints FAKE_FACTS_FILE as it is).
[ -n "${FAKE_FACTS_LOG:-}" ] && printf '%s|%s|%s|[%s]\n' "$#" "$1" "$(pwd)" "${PA_CHANGED_FILES-unset}" >> "$FAKE_FACTS_LOG"
if [ -n "${FAKE_FACTS_SLEEP:-}" ]; then sleep "$FAKE_FACTS_SLEEP"; echo late > "$FAKE_FACTS_LATE"; fi
if [ -n "${FAKE_FACTS_FILE:-}" ]; then cat "$FAKE_FACTS_FILE"; exit "${FAKE_FACTS_EXIT:-0}"; fi
echo '{"ok": true, "repoSubdir": "src/pkg", "key": {"flavour": "x1"}, "show": "Flavour x1", "extra": "never sent"}'
exit "${FAKE_FACTS_EXIT:-0}"
"""

STEP_SCRIPT = r"""#!/bin/sh
# An invented step: probe names its tools; prepare lays the clone out as box/src/pkg and keeps a folder under the
# real HOME; run runs a stub suite there.
action=$1 run=$2
[ -n "${FAKE_STEP_LOG:-}" ] && printf '%s|%s|%s|%s|%s|%s|%s\n' "$action" "$run" "$(pwd)" "$HOME" "${PA_STEP_FACTS:-}" "${PA_STEP_REAL_HOME:-}" "${PA_STEP_RUN:-}" >> "$FAKE_STEP_LOG"
layout() {
  keep="$PA_STEP_REAL_HOME/.cache/stub-keep"
  mkdir -p "$keep"; state=reused; [ -f "$keep/made" ] || { : > "$keep/made"; state=created; }
  echo "keep $keep"; echo "kept $state"
  mkdir -p box/src && mv w box/src/pkg || exit 5
  echo "laid out"
  echo "home stubtool"
}
case $action in
  probe)
    if [ -n "${FAKE_STEP_PROBE:-}" ]; then printf '%s\n' "$FAKE_STEP_PROBE"; else echo "tool stubtool /bin/sh 1.0"; echo "tool stub-hash - abc123"; fi
    exit "${FAKE_STEP_PROBE_EXIT:-0}" ;;
  prepare)
    if [ -n "${FAKE_PREPARE:-}" ]; then . "$FAKE_PREPARE"; exit "${FAKE_PREPARE_EXIT:-0}"; fi
    layout
    echo "cwd box/src/pkg"
    exit "${FAKE_PREPARE_EXIT:-0}" ;;
  run)
    echo "stub step run in $(pwd)"
    echo "facts: $(cat "$PA_STEP_FACTS")"
    echo "env HOME=$HOME HERDR_PA_HOME=$HERDR_PA_HOME PCF=[${PA_CHANGED_FILES-unset}]"
    hc ls > /dev/null 2>&1; echo "hc-exit $?"
    if [ -n "${FAKE_GO:-}" ]; then while [ ! -f "$FAKE_GO" ]; do sleep 0.1; done; fi
    echo "=== summary (each part's whole output: /tmp/x)"
    echo "  unit tests: test_s: ok (2s)"
    exit "${FAKE_STEP_EXIT:-0}" ;;
esac
exit 9
"""

STEP_ENTRY = {"command": "hooks/tests.sh full", "marker": "box.marker", "facts": "hooks/facts.sh", "step": "hooks/step.sh"}


class StepBase(Remote):
    """A stub profile whose tests.remoteFull names a facts hook and a step; the worktree has the marker above it."""

    def setUp(self):
        super().setUp()
        self.folder = os.path.join(pa.ROOT, "profiles", "test")
        for name, text in (("tests.sh", STEP_TESTS_SH), ("facts.sh", STEP_FACTS), ("step.sh", STEP_SCRIPT)):
            write(os.path.join(self.folder, "hooks", name), text)
            os.chmod(os.path.join(self.folder, "hooks", name), 0o755)
        self.use_entry(dict(STEP_ENTRY))
        agent = pa.read_json(os.path.join(self.dir, "agent.json"))
        agent.pop("commands", None)
        dump(agent, os.path.join(self.dir, "agent.json"))
        write(os.path.join(self.tmp, "box.marker"), "")
        self.step_log = os.path.join(self.tmp, "step.log")
        self.facts_log = os.path.join(self.tmp, "facts.log")
        self.keep_dir = os.path.join(os.environ["HOME"], ".cache", "stub-keep")
        shutil.rmtree(self.keep_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.keep_dir, True)
        for k in ("FAKE_FACTS_FILE", "FAKE_FACTS_EXIT", "FAKE_FACTS_SLEEP", "FAKE_FACTS_LATE", "FAKE_STEP_PROBE",
                  "FAKE_STEP_PROBE_EXIT", "FAKE_PREPARE", "FAKE_PREPARE_EXIT", "FAKE_STEP_EXIT", "STUB_HC_KEEP",
                  "PA_SUITE_FACTS_TIMEOUT"):
            self.setenv(**{k: None})
        self.setenv(FAKE_STEP_LOG=self.step_log, FAKE_FACTS_LOG=self.facts_log)

    def use_entry(self, entry, full="hooks/tests.sh full", extra=None):
        tests = {"full": full}
        if entry is not None:
            tests["remoteFull"] = entry
        tests.update(extra or {})
        profile = {"name": "test", "claude": ["true"], "codex": ["true"], "tests": tests}
        dump(profile, os.path.join(self.folder, "profile.json"))

    def command(self):
        return os.path.join(self.folder, "hooks", "tests.sh") + " full"

    def step_calls(self):
        return [l.split("|") for l in (pa.read_text(self.step_log) or "").splitlines()]

    def facts_calls(self):
        return (pa.read_text(self.facts_log) or "").splitlines()

    def check_local(self, word, *args):
        """The run stays on this Mac (no terminal, no file sent) and the output says word."""
        code, out = self.main("--again", *args)
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in self.calls() if c[0] in ("run", "file")], self.calls())
        self.assertIn(word, out)
        self.assertIn("fake whole run on this Mac", self.output(self.run_id(out)) or "", out)
        self.assertNotIn("step", self.lines()[-1])
        return out

    def check_refused_on(self, word):
        """With --on: exit 2, the word in the output, no record."""
        before = len(self.lines())
        code, out = self.main("--again", "--on", "box-1")
        self.assertEqual(code, 2, out)
        self.assertIn(word, out)
        self.assertEqual(len(self.lines()), before, out)
        return out

    def fresh_log(self):
        for p in (self.log, self.step_log, self.facts_log):
            if os.path.exists(p):
                os.remove(p)


class X22StepRun(StepBase):
    """R1: when a run is a step run."""

    def test_x22a_a_step_run_goes_to_the_machine_through_probe_prepare_run(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        line = self.lines()[-1]
        self.assertEqual(line.get("machine"), "box-1", out)
        self.assertEqual(line.get("command"), self.command())
        self.assertEqual([c[0] for c in self.step_calls()], ["probe", "prepare", "run"], self.step_calls())
        text = self.output(run)
        self.assertIn("stub step run in ", text)
        self.assertIn("/box/src/pkg", text)
        self.assertEqual(len(self.facts_calls()), 1)
        n, root, cwd, pcf = self.facts_calls()[0].split("|")
        self.assertEqual((n, os.path.realpath(root), os.path.realpath(cwd), pcf),
                         ("1", os.path.realpath(self.wt), os.path.realpath(self.wt), "[unset]"))

    def test_x22b_no_marker_above_the_root_stays_local(self):
        os.remove(os.path.join(self.tmp, "box.marker"))
        self.check_local("box.marker")
        self.assertEqual(self.facts_calls(), [])

    def test_x22c_the_marker_in_the_root_itself_does_not_count(self):
        os.remove(os.path.join(self.tmp, "box.marker"))
        write(os.path.join(self.wt, "box.marker"), "")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "marker in root")
        self.check_local("box.marker")
        self.assertEqual(self.facts_calls(), [])

    def test_x22d_a_dirty_tree_stays_local_and_the_facts_hook_does_not_run(self):
        write(os.path.join(self.wt, "loose.txt"), "x\n")
        self.check_local("untracked")
        self.assertEqual(self.facts_calls(), [])

    def test_x22e_module_stays_local(self):
        self.main("--module", "test_x")
        self.assertFalse([c for c in self.calls() if c[0] in ("run", "file")], self.calls())
        self.assertEqual(self.facts_calls(), [])

    def test_x22f_a_space_command_of_its_own_stays_local(self):
        self.set_command(self.command())
        self.check_local("command")
        self.assertEqual(self.facts_calls(), [])

    def test_x22g_another_profile_full_command_ignores_the_entry(self):
        self.use_entry(dict(STEP_ENTRY), full="hooks/tests.sh full --x")
        self.check_local("remoteFull")
        self.assertEqual(self.facts_calls(), [])

    def test_x22h_full_is_run_all_is_checked_first(self):
        os.chmod(os.path.join(self.wt, "tests", "run-all.sh"), 0o755)
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "run-all executable")
        write(os.path.join(self.folder, "hooks", "tests.sh"), PROFILE_RUNNER)
        self.use_entry(dict(STEP_ENTRY), extra={"fullIsRunAll": {"command": "hooks/tests.sh full", "unlessFound": []}})
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("fake remote part", self.output(self.run_id(out)))
        self.assertEqual(self.step_calls(), [])
        self.assertEqual(self.facts_calls(), [])
        self.assertNotIn("step", self.lines()[-1])

    def test_x22i_no_machines_no_facts_hook_and_no_hc_call(self):
        self.configure([])
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.facts_calls(), [])
        self.assertRegex(out.splitlines()[0], r"^pa-full-suite: run [0-9a-f]{12}: output in ")

    def test_x22j_local_flag_no_facts_hook(self):
        code, out = self.main("--local")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.facts_calls(), [])


class X23Entry(StepBase):
    """R2: each bad remoteFull is ignored (named once) and the run is today's (local here)."""

    def bad(self, entry, word="remoteFull", files=None):
        for name, kind in (files or {}).items():
            path = os.path.join(self.folder, "hooks", name)
            if os.path.lexists(path):
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            kind(path)
        self.use_entry(entry)
        self.fresh_log()
        out = self.check_local(word)
        self.assertEqual(out.count("remoteFull"), 1, out)
        self.assertEqual(self.facts_calls(), [])
        return out

    def test_x23a_command_spellings(self):
        for c in (" hooks/tests.sh full", "hooks/tests.sh  full", "sh hooks/tests.sh full",
                  "sh -c 'hooks/tests.sh full'", "env X=1 hooks/tests.sh full", self.command()):
            with self.subTest(command=c):
                self.bad(dict(STEP_ENTRY, command=c))

    def test_x23b_markers(self):
        for m in ("", ".", "..", "a/box.marker", "x\u0000y", "box.marker\n", 3, None, ["box.marker"]):
            with self.subTest(marker=m):
                self.bad(dict(STEP_ENTRY, marker=m))

    def test_x23c_a_dash_marker_is_accepted(self):
        os.remove(os.path.join(self.tmp, "box.marker"))
        write(os.path.join(self.tmp, "-x"), "")
        self.use_entry(dict(STEP_ENTRY, marker="-x"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1].get("machine"), "box-1", out)

    def test_x23d_hook_paths(self):
        for key in ("facts", "step"):
            for p in ("", "/bin/sh", "../x", "hooks/../../x", "./hooks/x", "hooks//x", "hooks/sub/x", "-hooks/x",
                      "hooks/x\n", "hooks/..", "hooks/.", "hooks/missing.sh", ["hooks/step.sh"], 7):
                with self.subTest(key=key, path=p):
                    self.bad(dict(STEP_ENTRY, **{key: p}))

    def test_x23e_hook_files(self):
        def link_to(target):
            return lambda path: os.symlink(target, path)

        def fifo(path):
            os.mkfifo(path)

        def plain(path):
            write(path, "#!/bin/sh\n")

        def big(path):
            write(path, "#!/bin/sh\n" + "#" * (64 * 1024 + 1 - len("#!/bin/sh\n")))
            os.chmod(path, 0o755)

        def folder(path):
            os.makedirs(path)
        outside = os.path.join(self.tmp, "outside.sh")
        write(outside, STEP_SCRIPT)
        os.chmod(outside, 0o755)
        cases = (("passwd", link_to("/etc/passwd")), ("out", link_to(outside)), ("rel", link_to("../../outside")),
                 ("fifo", fifo), ("plain", plain), ("big", big), ("dir", folder))
        for key in ("facts", "step"):
            for name, kind in cases:
                with self.subTest(key=key, case=name):
                    self.bad(dict(STEP_ENTRY, **{key: "hooks/x-" + name}), files={"x-" + name: kind})

    def test_x23f_exactly_64_kb_is_accepted(self):
        path = os.path.join(self.folder, "hooks", "step.sh")
        write(path, STEP_SCRIPT + "#" * (64 * 1024 - len(STEP_SCRIPT.encode()) - 1) + "\n")
        self.assertEqual(os.path.getsize(path), 64 * 1024)
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1].get("machine"), "box-1", out)

    def test_x23g_keys(self):
        entry = dict(STEP_ENTRY)
        entry["Step"] = entry.pop("step")
        for e in (entry, dict(STEP_ENTRY, other="x"), {k: v for k, v in STEP_ENTRY.items() if k != "marker"},
                  ["hooks/step.sh"], "hooks/step.sh"):
            with self.subTest(entry=e):
                self.bad(e)


class X24Facts(StepBase):
    """R3: each bad facts answer: local with the reason (output, build.log, the record's note); --on: exit 2."""

    def answer(self, text, code=0):
        path = os.path.join(self.tmp, "facts-answer")
        with open(path, "w") as f:
            f.write(text)
        self.setenv(FAKE_FACTS_FILE=path, FAKE_FACTS_EXIT=str(code))

    def refused(self, text, code=0):
        self.answer(text, code)
        self.fresh_log()
        out = self.check_local("facts hook")
        self.assertIn("facts hook", self.lines()[-1].get("note") or "", self.lines()[-1])
        self.assertIn("facts hook", self.build_log().splitlines()[-1] if False else self.build_log())
        self.assertEqual(self.step_calls(), [])
        return out

    def good(self, **change):
        value = {"ok": True, "repoSubdir": "src/pkg", "key": {"flavour": "x1"}, "show": "Flavour x1"}
        value.update(change)
        return json.dumps(value)

    def test_x24a_exit_and_shape(self):
        for text, code in ((self.good(), 1), ("not json", 0), (self.good() + "\n" + self.good(), 0),
                           (json.dumps({"ok": True, "repoSubdir": "src/pkg", "key": {"flavour": "x1"},
                                        "show": "s", "pad": "x" * (16 * 1024)}), 0),
                           (json.dumps({"repoSubdir": "src/pkg", "key": {}, "show": "s"}), 0),
                           (self.good(ok="true"), 0), (self.good(ok=1), 0), (self.good(key=["flavour", "x1"]), 0),
                           ("[]", 0), ("", 0), (self.good(show="a\nb"), 0), (self.good(show=3), 0)):
            with self.subTest(text=text[:60], code=code):
                self.refused(text, code)

    def test_x24b_key_names(self):
        for name in ("Event-Id", "", "a" * 33, "1a", "a b"):
            with self.subTest(name=name):
                self.refused(self.good(key={name: "x1"}))

    def test_x24c_key_values(self):
        for value in ("", "-rf", "--root", "../x", "a/../b", "a//b", "/abs", "a b", "a;b", "$(x)", "`x`", "a\nb",
                      "x" * 129, 5, None, ["x"], "a/..", "a/b/"):
            with self.subTest(value=value):
                self.refused(self.good(key={"flavour": value}))

    def test_x24d_too_many_keys(self):
        self.refused(self.good(key={"k{}".format(i): "v" for i in range(9)}))

    def test_x24e_repo_subdir(self):
        for value in ("", ".", "/src/P", "src/../P", "src/P/", "src//P", "-P", "a/b/c/d/e", "src/-P", "src/./P",
                      None, 3, "src/P\n"):
            with self.subTest(value=value):
                self.refused(self.good(repoSubdir=value))

    def test_x24f_ok_false_names_its_reason(self):
        out = self.refused(json.dumps({"ok": False, "reason": "not a stub workspace"}))
        self.assertIn("not a stub workspace", out)

    def test_x24g_on_gives_exit_2(self):
        self.answer(json.dumps({"ok": False, "reason": "not a stub workspace"}))
        self.check_refused_on("not a stub workspace")
        self.assertFalse([c for c in self.calls() if c[0] in ("run", "file")], self.calls())

    def test_x24h_a_slow_hook_is_killed_and_the_run_is_local(self):
        late = os.path.join(self.tmp, "late")
        self.setenv(PA_SUITE_FACTS_TIMEOUT="1", FAKE_FACTS_SLEEP="4", FAKE_FACTS_LATE=late)
        began = time.time()
        self.check_local("facts hook")
        self.assertLess(time.time() - began, 20)
        time.sleep(4.5)
        self.assertFalse(os.path.exists(late), "the facts hook was not killed")

    def test_x24i_eight_keys_long_values_and_four_segments_are_accepted(self):
        key = {"k{}".format(i): "v" + "x" * 127 for i in range(8)}
        key["k0"] = "a.b_c@d/e+f-g"
        self.answer(self.good(key=key, repoSubdir="a/b/c/d"))
        path = os.path.join(self.tmp, "prepare.sh")
        write(path, "mkdir -p a/b/c && mv w a/b/c/d && echo 'cwd a/b/c/d'\n")
        self.setenv(FAKE_PREPARE=path)
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1]["step"]["facts"], key)


class X25Sent(StepBase):
    """R5: the bundle, the step and the facts file are sent; nothing else."""

    def test_x25a_only_the_bundle_the_step_and_the_facts_arrive(self):
        import base64
        import hashlib
        keep = os.path.join(self.tmp, "kept")
        self.setenv(STUB_HC_KEEP=keep)
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(os.listdir(keep)), ["b.b64", "facts.json", "step.b64"])
        with open(os.path.join(keep, "facts.json")) as f:
            self.assertEqual(json.load(f), {"key": {"flavour": "x1"}, "repoSubdir": "src/pkg"})
        with open(os.path.join(keep, "step.b64")) as f:
            sent = base64.b64decode(f.read())
        with open(os.path.join(self.folder, "hooks", "step.sh"), "rb") as f:
            self.assertEqual(sent, f.read())
        self.assertEqual(self.lines()[-1]["step"]["sha256"], hashlib.sha256(sent).hexdigest())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["file", "write"]]), 3, self.calls())

    def test_x25b_the_step_argv_is_fixed_words(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        folder = "/tmp/pa-suite-" + run
        actions = []
        for c in self.calls():
            text = c[-1] if c and c[0] in ("exec", "run") else ""
            for m in re.finditer(r"(\S+/step) (\w+) (\S+)", text):
                self.assertEqual((m.group(1), m.group(3)), (folder + "/step", folder), text)
                actions.append(m.group(2))
            self.assertNotIn("x1", " ".join(c))
            self.assertNotIn("Flavour", " ".join(c))
        self.assertEqual(actions, ["probe", "prepare", "run"])
        run_call = [c for c in self.calls() if c[0] == "run"][0]
        self.assertEqual(run_call[run_call.index("--cwd") + 1], folder)
        setup = [c for c in self.calls() if c[0] == "exec" and "clone" in c[-1]][0]
        self.assertIn("chmod 700 step", setup[-1])

    def test_x25c_a_committed_secret_is_never_sent(self):
        dot = "."
        for name in (dot + "env", "conf/" + dot + "env.local", dot + "ssh/id", "credent" + "ials",
                     dot + "claude/x.json"):
            with self.subTest(path=name):
                path = os.path.join(self.wt, name)
                write(path, "x\n")
                sh(self.wt, "git", "add", "-f", name)
                sh(self.wt, "git", "commit", "-q", "-m", "a secret")
                self.fresh_log()
                self.check_refused_on("never sent")
                self.assertFalse([c for c in self.calls() if c[0] == "file"], self.calls())
                sh(self.wt, "git", "rm", "-q", "--cached", name)
                os.remove(path)
                sh(self.wt, "git", "commit", "-q", "-m", "dropped")

    def test_x25d_a_profile_secret_path_is_never_sent(self):
        profile = pa.read_json(os.path.join(self.folder, "profile.json"))
        profile["secretPaths"] = [r"(^|/)token\.txt$"]
        dump(profile, os.path.join(self.folder, "profile.json"))
        write(os.path.join(self.wt, "token.txt"), "x\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "token")
        self.check_refused_on("never sent")
        self.assertFalse([c for c in self.calls() if c[0] == "file"], self.calls())

    def test_x25e_the_step_counts_in_the_size_bound(self):
        """ship() itself (pa-full-suite loads its own copy of pa_suite, so BUNDLE_MAX is set on this one). The
        positive control measures what one ship sends (b.b64, step.b64, facts.json); the limit is then set between
        "the bundle and facts.json" and "all three", so only counting step.b64 refuses (VERIFY J1 Must fix 1)."""
        pa_suite.pa = pa
        entry, note = pa_suite.remote_full(pa.load_profile("test"))
        self.assertIsNone(note)
        step = {"entry": entry, "key": {"flavour": "x1"}, "repoSubdir": "src/pkg"}
        tools = pa_suite.probe("box-1")["tools"]
        pin = pa_suite.pins(pa_hooks, self.wt)[0]
        sent = {}
        real_hc = pa_suite.hc

        def hc(argv, timeout=120, stdin=None):
            if argv[:2] == ["file", "write"] and stdin is not None:
                sent[argv[-1].rsplit("/", 1)[-1]] = len(stdin)
            return real_hc(argv, timeout=timeout, stdin=stdin)
        self.addCleanup(setattr, pa_suite, "hc", real_hc)
        pa_suite.hc = hc
        saved = pa_suite.BUNDLE_MAX
        self.addCleanup(setattr, pa_suite, "BUNDLE_MAX", saved)
        pa_suite.ship(pa_hooks, "box-1", "a" * 12, pin, tools, self.wt, step=step)
        self.assertTrue(os.path.isfile(os.path.join(self.remote_folder("a" * 12), "step")))
        pa_suite.cleanup("box-1", "a" * 12)
        self.assertEqual(sorted(sent), ["b.b64", "facts.json", "step.b64"], sent)
        self.assertGreater(sent["step.b64"], 100, sent)  # room for the margin below
        pa_suite.BUNDLE_MAX = sent["b.b64"] + sent["facts.json"] + sent["step.b64"] // 2
        with self.assertRaises(pa_suite.SetupFailed) as e:
            pa_suite.ship(pa_hooks, "box-1", "b" * 12, pin, tools, self.wt, step=step)
        self.assertIn("too large", str(e.exception))
        self.assertFalse([c for c in self.calls() if c[:2] == ["file", "write"] and "b" * 12 in c[-1]])
        pa_suite.cleanup("box-1", "b" * 12)


class X26Prepare(StepBase):
    """R6: the step's probe and prepare; each bad prepare output is a setup failure."""

    def snippet(self, text):
        path = os.path.join(self.tmp, "prepare.sh")
        write(path, text)
        self.setenv(FAKE_PREPARE=path)

    def test_x26a_prepare_output_h_cases(self):
        lay = "mkdir -p box/src && mv w box/src/pkg\n"
        cwd = "echo 'cwd box/src/pkg'\n"
        cases = {
            "cwd /abs": lay + "echo 'cwd /abs'\n",
            "cwd ../x": lay + "echo 'cwd ../x'\n",
            "cwd w/../..": "echo 'cwd w/../..'\n",
            "cwd empty": lay + "echo 'cwd '\n",
            "no cwd": lay,
            "two cwd": lay + cwd + cwd,
            "missing folder": lay + "echo 'cwd nothere'\n",
            "symlink out": "ln -s / out\necho 'cwd out'\n",
            "symlink up": "ln -s .. up\necho 'cwd up'\n",
            "keep relative": "echo 'keep relative/path'\n" + lay + cwd,
            "keep /": "echo 'keep /'\n" + lay + cwd,
            "keep /tmp": "echo 'keep /tmp'\n" + lay + cwd,
            "keep HOME": "echo \"keep $PA_STEP_REAL_HOME\"\n" + lay + cwd,
            "keep above HOME": "echo \"keep $(dirname \"$PA_STEP_REAL_HOME\")\"\n" + lay + cwd,
            "keep ..": "echo \"keep $PA_STEP_REAL_HOME/a/../b\"\n" + lay + cwd,
            "keep space": "echo \"keep $PA_STEP_REAL_HOME/a b\"\n" + lay + cwd,
            "keep tab": "printf 'keep %s/a\\tb\\n' \"$PA_STEP_REAL_HOME\"\n" + lay + cwd,
            "keep under run": "echo \"keep $PA_STEP_RUN/k\"\n" + lay + cwd,
            "keep the run": "echo \"keep $PA_STEP_RUN\"\n" + lay + cwd,
            "two keep": "echo \"keep $PA_STEP_REAL_HOME/k\"; echo \"keep $PA_STEP_REAL_HOME/k2\"\n" + lay + cwd,
            "keep not first": "echo progress\n" + lay + "echo \"keep $PA_STEP_REAL_HOME/k\"\n" + cwd,
            "bad kept": "echo \"keep $PA_STEP_REAL_HOME/k\"; echo 'kept maybe'\n" + lay + cwd,
            "bad home": lay + "echo 'home a b'\n" + cwd,
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.snippet(text)
                self.fresh_log()
                out = self.check_refused_on("prepare")
                self.assertEqual(os.listdir(os.path.join(self.stub, "box-1", "tmp")), [], out)

    def test_x26b_prepare_exit_is_a_setup_failure_and_falls_back(self):
        self.setenv(FAKE_PREPARE_EXIT="3")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("setup on box-1 failed", out)
        self.assertIn("prepare", out)
        line = self.lines()[-1]
        self.assertIsNone(line.get("machine"))
        self.assertIn("prepare", line.get("note"))
        self.assertIn("fake whole run on this Mac", self.output(self.run_id(out)))

    def test_x26c_probe_missing_tool_or_exit_is_a_setup_failure(self):
        for env in ({"FAKE_STEP_PROBE": "missing stubtool"}, {"FAKE_STEP_PROBE_EXIT": "1"},
                    {"FAKE_STEP_PROBE": "tool stubtool relative/path 1"}):
            with self.subTest(env=env):
                self.setenv(FAKE_STEP_PROBE=None, FAKE_STEP_PROBE_EXIT=None)
                self.setenv(**env)
                self.fresh_log()
                self.check_refused_on("probe")
                self.assertNotIn("prepare", [c[0] for c in self.step_calls()])

    def test_x26d_each_action_has_the_scratch_home_and_the_step_variables(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        folder = os.path.realpath(self.remote_folder(run)) if os.path.exists(self.remote_folder(run)) else None
        rows = self.step_calls()
        self.assertEqual([r[0] for r in rows], ["probe", "prepare", "run"])
        want_folder = os.path.join(os.path.realpath(os.path.join(self.stub, "box-1", "tmp")), "pa-suite-" + run)
        self.assertIsNone(folder)  # cleaned
        for action, arg, cwd, home_, facts, real, step_run in rows:
            self.assertEqual(arg, want_folder, action)
            self.assertEqual(home_, want_folder + "/u", action)
            self.assertEqual(facts, want_folder + "/facts.json", action)
            self.assertEqual(real, os.environ["HOME"], action)
            self.assertEqual(step_run, want_folder, action)
            want = want_folder if action != "run" else want_folder + "/box/src/pkg"
            self.assertEqual(os.path.realpath(cwd), os.path.realpath(want) if os.path.exists(want) else want, action)
        text = self.output(run)
        self.assertIn("hc-exit 2", text)
        self.assertIn("PCF=[]", text)

    def test_x26e_a_cwd_symlink_inside_the_run_folder_is_accepted(self):
        self.snippet("mkdir -p box/src && mv w box/src/pkg && ln -s box/src/pkg here\necho 'cwd here'\n")
        code, out = self.main("--on", "box-1")
        self.assertEqual(code, 0, out)

    def test_x26f_a_relative_home_from_the_probe_is_a_setup_failure(self):
        self.setenv(HOME="relative/home")  # the stub machine's user HOME is this process's
        self.check_refused_on("HOME")
        self.assertEqual(self.step_calls(), [])


class X27Record(StepBase):
    """R13-R15: exit, record, output and build.log of a step run."""

    def test_x27a_record_and_output(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        self.assertRegex(out, r"pa-full-suite: run {} on box-1 \(\w+; Flavour x1; reachable in [0-9.]+s\)".format(run))
        line = self.lines()[-1]
        self.assertEqual(line["command"], self.command())
        self.assertEqual([p["place"] for p in line["places"]], ["box-1"])
        self.assertEqual(line["places"][0]["exit"], 0)
        self.assertIn("step:stubtool", line["tools"])
        self.assertEqual(line["tools"]["step:stub-hash"], {"path": "-", "version": "abc123"})
        self.assertEqual([(p.get("name"), p.get("place")) for p in line["parts"]], [("unit tests: test_s", "box-1")])
        step = line["step"]
        self.assertEqual(step["facts"], {"flavour": "x1"})
        self.assertEqual(step["keep"], "created")
        self.assertEqual(step["homeExceptions"], ["stubtool"])
        self.assertRegex(step["sha256"], r"^[0-9a-f]{64}$")
        text = self.output(run)
        self.assertNotIn("=== local part", text)
        self.assertIn("=== places: box-1 ({}) exit 0 -> exit 0".format(line["os"]), text)
        self.assertRegex(text, r"=== step: Flavour x1; workspace created in [0-9.]+s")
        self.assertIn("on box-1 (", self.build_log())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evidence", "t-1", "full-suite-{}-local.out".format(run))))
        code, out = self.main("--again")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1]["step"]["keep"], "reused")
        self.assertIn("workspace reused", self.output(self.run_id(out)))

    def test_x27b_the_exit_is_the_step_runs(self):
        self.setenv(FAKE_STEP_EXIT="3")
        code, out = self.main()
        self.assertEqual(code, 3, out)
        self.assertEqual(self.lines()[-1]["exit"], 3)
        self.assertIn("exit 3 -> exit 3", self.output(self.run_id(out)))

    def test_x27c_no_local_part_runs(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertNotIn("fake local part", self.output(self.run_id(out)))
        self.assertNotIn("this Mac", out.splitlines()[-1])

    def test_x27d_lost_machine_is_124(self):
        self.setenv(STUB_HC_FAIL="lost:box-1", FAKE_GO=os.path.join(self.tmp, "go"))
        code, out = self.main()
        self.assertEqual(code, 124, out)
        self.assertIn("machine box-1 lost after", self.output(self.run_id(out)))
        self.assertEqual(self.lines()[-1]["exit"], 124)

    def test_x27e_term_cleans_and_exits_143(self):
        self.setenv(FAKE_GO=os.path.join(self.tmp, "go"))
        p, log = self.background()
        self.assertTrue(self.until(lambda: RUN.search(pa.read_text(log) or "")), pa.read_text(log))
        run = self.run_id(pa.read_text(log))
        self.assertTrue(self.until(lambda: "stub step run" in (self.output(run) or "")), pa.read_text(log))
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=60), 143, pa.read_text(log))
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        self.assertEqual(self.lines(), [])


class X28Cleanup(StepBase):
    """R16: the run folder (the step's layout inside it) is removed; the kept folder never is."""

    def test_x28_run_folder_removed_keep_left(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        self.assertTrue(os.path.isfile(os.path.join(self.keep_dir, "made")))
        rm = [c for c in self.calls() if c[0] == "exec" and "-rf" in c]
        self.assertEqual(rm[-1][-1], "/tmp/pa-suite-" + run)
        for c in self.calls():
            self.assertNotIn(self.keep_dir, " ".join(c))


class X29Generic(unittest.TestCase):
    """R17: no workspace tool's words in the public tool, its tests or docs; the docs name tests.remoteFull."""
    WORDS = ["bra" + "zil", "package" + "info", "version" + "set", "event" + "id", "tool" + "box"]

    def test_x29a_no_profile_terms(self):
        for rel in ("runtime/tools/pa_suite.py", "tests/test_pa_suite_remote.py", "tests/stub-hc", "README.md"):
            with open(os.path.join(REPO, rel)) as f:
                text = f.read().lower()
            for w in self.WORDS:
                self.assertNotIn(w, text, rel)

    def test_x29b_docs(self):
        with open(os.path.join(REPO, "README.md")) as f:
            readme = " ".join(f.read().split())
        self.assertIn("remoteFull", readme)
        for word in ("probe", "prepare", "PA_STEP_FACTS", "PA_STEP_REAL_HOME", "kept created"):
            self.assertIn(word, readme)
        doc = " ".join(pa_suite.__doc__.split())
        for word in ("remoteFull", "facts", "prepare", "kept"):
            self.assertIn(word, doc)


class X30NoStepUnchanged(Remote):
    """Today's remote runs, with no remoteFull: the probe argv byte-equal to t-089's, no step key."""

    def test_x30_probe_and_record_unchanged(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        probes = [c for c in self.calls() if c[0] == "exec" and "uname" in c[-1]]
        self.assertEqual(probes[0], ["exec", "--on", "box-1", "--timeout", "30", "--", "sh", "-c", pa_suite.PROBE])
        line = self.lines()[-1]
        self.assertNotIn("step", line)
        self.assertEqual([p["place"] for p in line["places"]], ["box-1", "local"])
        run_call = [c for c in self.calls() if c[0] == "run"][0]
        self.assertEqual(run_call[run_call.index("--cwd") + 1], "/tmp/pa-suite-{}/w".format(run))
        self.assertEqual(run_call[-1], pa_suite.run_script("/tmp/pa-suite-" + run, line["tools"]))


if __name__ == "__main__":
    unittest.main()
