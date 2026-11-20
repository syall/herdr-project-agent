#!/usr/bin/env python3
"""Claude Code's status line (t-111): `pa_statusline.py statusline --space ID [--then ARGV...]` does what
`pa-costs statusline ...` does, but loads pa.py and pa_costs.py from compiled code kept outside tools/.
Python standard library only (3.7+).

Why: run as a script, pa_costs.py is __main__, which Python never caches, and it turns bytecode writing off before
`import pa` (to keep tools/ free of caches), so every status line run compiled both files from source: most of its
CPU. Here the compiled code is kept in <HERDR_PA_HOME>/cache/pycache, one file per module, install path and Python:
  - never stale: a file is used only when its header holds the sha256 of the source bytes it was compiled from (with
    the interpreter's magic number, optimize level and the path), not a modification time;
  - never partial: written to a temp file in the same folder and renamed into place (atomic); read back only when the
    sha256 of its payload matches, so a damaged file is compiled again and replaced;
  - two writers at once each write their own temp file of the same bytes; the last rename wins;
  - the folder is used only when it is this user's own: a real directory (no symlink), not writable by group or
    others (code in it is run). Otherwise, or when it is read-only or full, the code is compiled in memory as before.
    pa-doctor says when it is not used, and why (refused()).
Not checked (t-135): HERDR_PA_HOME itself (whoever can write it can already replace tools/ in it), and ACLs (macOS
`chmod +a`, POSIX ACLs) on either folder: only the mode bits are read, and only the owner can add an ACL.
Nothing here fails a status line: any error leaves it printing nothing, exit 0.
"""
import hashlib
import importlib.util
import marshal
import os
import stat
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.realpath(__file__))
STRAY = 60  # seconds after which a temp file left by a killed writer is removed (by a later write)


def cache_folder():
    """<HERDR_PA_HOME>/cache/pycache, HERDR_PA_HOME found as pa.ROOT finds it."""
    root = os.environ.get("HERDR_PA_HOME") or os.path.join(os.path.expanduser("~"), ".config", "herdr-pa")
    return os.path.join(root, "cache", "pycache")


def refused(folder, missing_ok=False):
    """(path, why) when folder or its parent is not fit for the cache, else None: each must be a real directory of this
    user that neither group nor others can write. With missing_ok a folder that is not there is passed over. Makes
    nothing."""
    for f in (folder, os.path.dirname(folder)):
        try:
            st = os.lstat(f)
        except FileNotFoundError:
            if missing_ok:
                continue
            return f, "is missing"
        except OSError as e:
            return f, "cannot be read ({})".format(e.strerror or e)
        if not stat.S_ISDIR(st.st_mode):
            return f, "is not a real directory"
        if st.st_uid != os.getuid():
            return f, "belongs to another user"
        if st.st_mode & 0o022:
            return f, "is writable by group or others"
    return None


def private(folder):
    """folder (it and its parent made 0700 when missing, whatever the umask) when refused() finds nothing; else None."""
    try:
        os.makedirs(os.path.dirname(folder), mode=0o700, exist_ok=True)  # makedirs applies mode to the leaf only
        os.makedirs(folder, mode=0o700, exist_ok=True)
    except OSError:
        return None
    return None if refused(folder) else folder


def code_of(name, folder=TOOLS, cache=None):
    """(path, code) of folder/<name>.py: from cache when its file was compiled from these exact source bytes, else
    compiled (and, with a cache, written there for the next run)."""
    path = os.path.realpath(os.path.join(folder, name + ".py"))
    with open(path, "rb") as f:
        source = f.read()
    key = b"\0".join([importlib.util.MAGIC_NUMBER, str(sys.flags.optimize).encode(), os.fsencode(path), source])
    digest = hashlib.sha256(key).digest()
    target = None
    if cache:
        tag = sys.implementation.cache_tag or "py"
        if sys.flags.optimize:  # python3 -O: a file of its own, as importlib names it, not one rewritten in turn
            tag += ".opt-{}".format(sys.flags.optimize)
        target = os.path.join(cache, "{}-{}.{}.pyc".format(name, hashlib.sha256(os.fsencode(path)).hexdigest()[:16], tag))
        try:
            with open(target, "rb") as f:
                data = f.read()
            if data[:32] == digest and hashlib.sha256(data[64:]).digest() == data[32:64]:
                return path, marshal.loads(data[64:])
        except (OSError, ValueError, EOFError, TypeError):
            pass
    code = compile(source, path, "exec", dont_inherit=True)
    if target:
        write(target, digest, marshal.dumps(code))
    return path, code


def write(target, digest, payload):
    """Puts digest + sha256(payload) + payload at target through a temp file and a rename. Never raises."""
    folder, tmp = os.path.dirname(target), None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".", suffix=".tmp", dir=folder)  # mode 0600
        with os.fdopen(fd, "wb") as f:
            f.write(digest + hashlib.sha256(payload).digest() + payload)
        os.replace(tmp, target)
        tmp = None
        old = time.time() - STRAY
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if n.endswith(".tmp") and os.lstat(p).st_mtime < old:
                os.unlink(p)
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load(name, folder=TOOLS, cache=None):
    """Imports folder/<name>.py as module `name` (in sys.modules, __file__ its real path) from code_of's code."""
    path, code = code_of(name, folder, cache)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def main(argv):
    if argv[:1] != ["statusline"]:
        sys.stderr.write("usage: pa_statusline.py statusline --space ID [--then ARGV...]\n")
        return 2
    try:
        cache = private(cache_folder())
        load("pa", cache=cache)  # first: pa_costs' `import pa` finds it
        costs = load("pa_costs", cache=cache)
    except BaseException:  # noqa: BLE001 - a status line never fails
        os._exit(0)
    costs.statusline_main(argv[1:])
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
