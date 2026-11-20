#!/usr/bin/env python3
"""The pa-* commands' launcher (t-147, t-166; t-135 D option 1): install.sh links each command of pa.py, pa_hooks.py,
pa_costs.py, pa_places.py and pa_handoff.py here (`pa_launch.py --names` lists them). Python standard library only
(3.7+).

Why: run as a script, a tool is __main__, which Python never compiles to a cache, and the tools turn bytecode
writing off (to keep tools/ free of caches), so each run compiled its tool and every tool it imports from source.
pa-hook runs on every tool call of every session. Here a command runs its tool's code as __main__ (the same globals as
a script run: __file__ its real path, its own `if __name__ == "__main__":` block, its exit codes and error lines),
the code taken from the compiled code t-111 keeps outside tools/ (pa_statusline.code_of: <HERDR_PA_HOME>/cache/pycache,
checked by the source's sha256, written atomically, used only in a folder of this user that group and others cannot
write). The tools' own imports of each other (`import pa`) come from it too (Finder); pa_hooks' own loaders of
pa_guard, pa_suite, pa_run and pa_push ask the Finder on sys.meta_path (pa_tool_spec) when this launcher started the
process.
When the cache cannot be used, or pa_statusline.py cannot be loaded, the tool is compiled from source as before: the
launcher never stops a command (the guard must still refuse what it refuses).
Not here: pa-queue, pa-push-main and pa-push-allow (pa_queue.py, pa_push.py) are linked to their tools as before.
"""
import importlib.util
import os
import sys
import types

sys.dont_write_bytecode = True  # keep tools/ free of caches
TOOLS = os.path.dirname(os.path.realpath(__file__))

# command -> the tool module whose code runs for it (what install.sh linked it to before t-147)
COMMANDS = {}
for _tool, _names in (
        ("pa", "pa-start pa-session pa-claude pa-codex pa-profile pa-peers pa-pass pa-cleanup pa-doctor pa-task "
               "pa-decide pa-sync pa-core-check pa-stamp pa-restart pa-context"),
        ("pa_hooks", "pa-gate pa-hooks pa-hook pa-full-suite pa-run pa-worktree pa-worktree-allow pa-worker pa-trust"),
        ("pa_costs", "pa-costs"),
        ("pa_places", "pa-places"),
        ("pa_handoff", "pa-handoff")):
    COMMANDS.update((_n, _tool) for _n in _names.split())
# the same table written out for the wiring tests' grep: "pa-costs": "pa_costs", "pa-places": "pa_places",
# "pa-handoff": "pa_handoff", "pa-trust": "pa_hooks"
del _tool, _names

_STATE = {"cache": None, "code_of": None}


def _plain_code_of(name, folder=TOOLS, cache=None):
    """(path, code) of folder/<name>.py compiled from source: pa_statusline.code_of without a cache."""
    path = os.path.realpath(os.path.join(folder, name + ".py"))
    with open(path, "rb") as f:
        return path, compile(f.read(), path, "exec", dont_inherit=True)


def _setup():
    """The cache folder (None when refused) and code_of, from pa_statusline; compiling from source if it fails."""
    try:
        import pa_statusline
        _STATE["cache"] = pa_statusline.private(pa_statusline.cache_folder())
        _STATE["code_of"] = pa_statusline.code_of
    except Exception:  # noqa: BLE001 - no cache: compile from source, as a script run does
        _STATE["cache"], _STATE["code_of"] = None, _plain_code_of


def code_of(path):
    """(real path, code) of the tool file at path, from the cache when it can be used."""
    folder, name = os.path.split(path)
    return _STATE["code_of"](name[:-3], folder, _STATE["cache"])  # raises only what compiling the source raises


class Loader:
    """Runs a tools/ module's code from code_of (the spec's origin is its file)."""

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        _path, code = code_of(module.__spec__.origin)
        exec(code, module.__dict__)


class Finder:
    """`import pa` (any top-level pa*.py of tools/) from code_of; everything else as usual."""

    def find_spec(self, name, path=None, target=None):
        if path is not None or "." in name or not name.startswith("pa"):
            return None
        file = os.path.join(TOOLS, name + ".py")
        if not os.path.isfile(file):
            return None
        return self.pa_tool_spec(name, os.path.realpath(file))

    def pa_tool_spec(self, name, path):
        """For pa_hooks' own loaders of pa_guard, pa_suite, pa_run and pa_push (pa_hooks.tool_spec): the module at
        path as name, its code from code_of."""
        return importlib.util.spec_from_file_location(name, path, loader=Loader())


def run(tool):
    """Runs tools/<tool>.py as __main__ from code_of, as `python3 <tool>.py` would (its exit is the process's)."""
    path, code = code_of(os.path.join(TOOLS, tool + ".py"))
    main = types.ModuleType("__main__")
    main.__file__ = path
    main.__builtins__ = __builtins__
    main.__spec__ = None
    main.__package__ = None
    main.__cached__ = None
    sys.modules["__main__"] = main
    exec(code, main.__dict__)


def main():
    kind = os.path.basename(sys.argv[0])
    if kind == "pa_launch.py":
        if sys.argv[1:] == ["--names"]:
            print(" ".join(COMMANDS))
            return 0
        sys.stderr.write("usage: pa_launch.py --names; run as one of its commands: {}\n".format(" ".join(COMMANDS)))
        return 2
    tool = COMMANDS.get(kind)
    if tool is None:
        sys.stderr.write("pa_launch.py: {} is not a command of this launcher (one of: {})\n".format(
            kind, " ".join(COMMANDS)))
        return 2
    _setup()
    sys.meta_path.insert(0, Finder())
    run(tool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
