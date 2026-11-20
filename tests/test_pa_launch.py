"""t-147 item 2 (t-166, t-135 D option 1): pa_launch.py, the one launcher every pa-* command of pa.py, pa_hooks.py,
pa_costs.py, pa_places.py and pa_handoff.py is linked to. It runs the tool's code as __main__ from the compiled code
t-111 keeps outside tools/ (<HERDR_PA_HOME>/cache/pycache), and serves the tools' own imports from it, so a warm run
compiles none of them. Temp homes only; nothing is installed for real.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t147-launch-"))

# What install.sh linked each command to before t-147 (main 6541313): the launcher must run the same tool.
BEFORE = {
    "pa.py": "pa-start pa-session pa-claude pa-codex pa-profile pa-peers pa-pass pa-cleanup pa-doctor pa-task "
             "pa-decide pa-sync pa-core-check pa-stamp pa-restart pa-context",
    "pa_hooks.py": "pa-gate pa-hooks pa-hook pa-full-suite pa-run pa-worktree pa-worktree-allow pa-worker pa-trust",
    "pa_costs.py": "pa-costs",
    "pa_places.py": "pa-places",
    "pa_handoff.py": "pa-handoff",
}
NOT_LAUNCHED = {"pa-queue": "pa_queue.py", "pa-push-main": "pa_push.py", "pa-push-allow": "pa_push.py"}


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def launch_module():
    spec = importlib.util.spec_from_file_location("pa_launch_under_test", os.path.join(TOOLS, "pa_launch.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tools_copy(name):
    """runtime/tools/*.py copied to TMP/<name>/tools, as an installed tools/ folder."""
    folder = os.path.join(TMP, name, "tools")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    for n in os.listdir(TOOLS):
        if n.endswith(".py"):
            shutil.copy2(os.path.join(TOOLS, n), os.path.join(folder, n))
    return folder


def files_under(folder):
    return sorted(os.path.relpath(os.path.join(d, f), folder) for d, _, fs in os.walk(folder) for f in fs)


# Runs the launcher as a script would run through a link (argv[0] the link, sys.path[0] the tools folder), counting
# every compile of a tools/ file, and writes {"compiled": [...], "code": exit code} to out.
PROBE = r'''
import builtins, json, os, sys
link, tools, out = sys.argv[1:4]
sys.argv = [link] + sys.argv[4:]
sys.path[0] = tools
seen, real = [], builtins.compile
def counting(source, filename, *a, **k):
    seen.append(str(filename))
    return real(source, filename, *a, **k)
launcher = os.path.join(tools, "pa_launch.py")
with open(launcher) as f:
    code = real(f.read(), launcher, "exec", dont_inherit=True)
builtins.compile = counting
status = 0
try:
    exec(code, {"__name__": "__main__", "__file__": launcher, "__builtins__": builtins})
except SystemExit as e:
    status = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
mine = sorted(os.path.basename(f) for f in seen if os.path.dirname(os.path.realpath(f)) == os.path.realpath(tools))
with open(out, "w") as f:
    json.dump({"compiled": [n for n in mine if n not in ("pa_launch.py", "pa_statusline.py")], "code": status}, f)
'''


class Launch(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(TMP, self.id().rsplit(".", 1)[-1])
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)
        os.chmod(self.root, 0o700)
        self.home = os.path.join(self.root, "pa")  # HERDR_PA_HOME
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(self.bin)

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HC_", "PA_", "CLAUDE")) and k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
        env.update(HOME=os.path.join(self.root, "home"), HERDR_PA_HOME=self.home)
        return env

    def link(self, name, target):
        path = os.path.join(self.bin, name)
        if os.path.lexists(path):
            os.unlink(path)
        os.symlink(target, path)
        return path

    def run_link(self, path, *args, data=b""):
        r = subprocess.run([sys.executable, path] + list(args), input=data, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=self.env(), cwd=self.root, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def probe(self, link, tools, *args, data=b""):
        out = os.path.join(self.root, "probe.json")
        r = subprocess.run([sys.executable, "-c", PROBE, link, tools, out] + list(args), input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(), cwd=self.root, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out) as f:
            return json.load(f)

    def cache(self):
        return os.path.join(self.home, "cache", "pycache")

    def test_l1_the_table_runs_what_install_sh_linked_before(self):
        table = launch_module().COMMANDS
        before = {n: tool[:-3] for tool, names in BEFORE.items() for n in names.split()}
        self.assertEqual(table, before)
        for name in NOT_LAUNCHED:
            self.assertNotIn(name, table)

    def test_l1b_install_sh_links_the_launchers_names_from_the_launcher(self):
        with open(os.path.join(REPO, "install.sh")) as f:
            lines = f.read().splitlines()
        loops = [ln for ln in lines if ln.startswith("for n in") and "pa_launch.py" in ln]
        self.assertEqual(loops, ['for n in $("$PA/tools/pa_launch.py" --names); do ln -sfn "$PA/tools/pa_launch.py" '
                                 '"$HOME/.local/bin/$n"; done'])
        for tool in BEFORE:  # no command is linked to its tool any more
            self.assertFalse([ln for ln in lines if '"$PA/tools/{}"'.format(tool) in ln], tool)
        tools = tools_copy("names")
        r = subprocess.run([sys.executable, os.path.join(tools, "pa_launch.py"), "--names"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=self.env(), timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(sorted(r.stdout.split()), sorted(launch_module().COMMANDS))

    def test_l1c_an_install_in_a_temp_home_links_them_all(self):
        home = os.path.join(self.root, "home")
        env = self.env()
        env.update(HOME=home, HERDR_PA_HOME=os.path.join(home, ".config", "herdr-pa"),
                   HERDR_APP_HOME=os.path.join(home, ".config", "herdr-app"))
        r = subprocess.run(["sh", os.path.join(REPO, "install.sh")], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        tools = os.path.join(env["HERDR_PA_HOME"], "tools")
        for name in launch_module().COMMANDS:
            self.assertEqual(os.path.realpath(os.path.join(home, ".local", "bin", name)),
                             os.path.realpath(os.path.join(tools, "pa_launch.py")), name)
        for name, tool in NOT_LAUNCHED.items():
            self.assertEqual(os.path.realpath(os.path.join(home, ".local", "bin", name)),
                             os.path.realpath(os.path.join(tools, tool)), name)
        self.assertFalse(os.path.exists(os.path.join(tools, "__pycache__")))

    def test_l2_a_warm_run_compiles_no_tool_and_writes_nothing_in_tools(self):
        tools = tools_copy("warm")
        before = files_under(tools)
        link = self.link("pa-hook", os.path.join(tools, "pa_launch.py"))
        call = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push origin main"},
                           "cwd": self.root}).encode()
        cold = self.probe(link, tools, "guard", data=call)
        self.assertEqual(cold["code"], 2)  # the guard refuses a push
        for name in ("pa.py", "pa_hooks.py", "pa_guard.py"):
            self.assertIn(name, cold["compiled"])
        self.assertEqual(self.probe(link, tools, "guard", data=call), {"compiled": [], "code": 2})
        self.assertEqual(files_under(tools), before)
        self.assertTrue(os.listdir(self.cache()))

    def test_l2b_pa_costs_and_a_pa_command_warm(self):
        tools = tools_copy("warm-b")
        for name in ("pa-costs", "pa-doctor"):
            with self.subTest(name=name):
                link = self.link(name, os.path.join(tools, "pa_launch.py"))
                self.assertEqual(self.probe(link, tools, "--help")["code"], 0)
                self.assertEqual(self.probe(link, tools, "--help"), {"compiled": [], "code": 0})

    def test_l3_the_same_output_and_code_as_the_tool_itself(self):
        tools = tools_copy("same")
        push = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": self.root}).encode()
        cases = [("pa-hook", "pa_hooks.py", ["guard"], push), ("pa-hook", "pa_hooks.py", ["no-such-hook"], b""),
                 ("pa-gate", "pa_hooks.py", [], b""), ("pa-costs", "pa_costs.py", ["--help"], b""),
                 ("pa-task", "pa.py", ["--help"], b""), ("pa-places", "pa_places.py", ["--help"], b""),
                 ("pa-handoff", "pa_handoff.py", ["--help"], b"")]
        for name, tool, args, data in cases:
            with self.subTest(name=name, args=args):
                direct = self.run_link(self.link(name, os.path.join(tools, tool)), *args, data=data)
                launched = self.run_link(self.link(name, os.path.join(tools, "pa_launch.py")), *args, data=data)
                self.assertEqual(launched, direct)
                self.assertEqual(self.run_link(self.link(name, os.path.join(tools, "pa_launch.py")), *args, data=data),
                                 direct)  # warm, from the cache

    def test_l4_a_refused_cache_still_runs_compiled_in_memory(self):
        tools = tools_copy("refused")
        os.makedirs(self.cache(), mode=0o700)
        os.chmod(os.path.dirname(self.cache()), 0o777)  # group/world-writable: never used
        link = self.link("pa-costs", os.path.join(tools, "pa_launch.py"))
        code, out, err = self.run_link(link, "--help")
        self.assertEqual((code, err), (0, b""))
        self.assertIn(b"usage: pa-costs", out)
        self.assertEqual(os.listdir(self.cache()), [])
        os.chmod(os.path.dirname(self.cache()), 0o700)

    def test_l4b_a_broken_statusline_loader_still_runs_the_guard(self):
        # the guard must still refuse (fail closed) when the cache code cannot load: the tool is compiled from source
        tools = tools_copy("broken-loader")
        with open(os.path.join(tools, "pa_statusline.py"), "w") as f:
            f.write("raise RuntimeError('broken on purpose')\n")
        link = self.link("pa-hook", os.path.join(tools, "pa_launch.py"))
        push = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": self.root}).encode()
        code, _out, err = self.run_link(link, "guard", data=push)
        self.assertEqual(code, 2, err)
        self.assertIn(b"Refused by the Project Agent's guard", err)

    def test_l5_an_unknown_name_is_a_usage_error(self):
        tools = tools_copy("unknown")
        code, out, err = self.run_link(self.link("pa-nothing", os.path.join(tools, "pa_launch.py")))
        self.assertEqual((code, out), (2, b""))
        self.assertIn(b"pa_launch.py: pa-nothing is not a command of this launcher", err)

    def test_l6_the_launcher_parses_as_3_7_and_is_stdlib_only(self):
        import ast
        with open(os.path.join(TOOLS, "pa_launch.py")) as f:
            tree = ast.parse(f.read(), feature_version=(3, 7))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported - {"pa_statusline"}, set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names")
                             else imported)


if __name__ == "__main__":
    unittest.main()
