"""Fixture repositories built once and copied per test (t-095, t-062 F3). Not a test module.

A fixture that made the same repository with git in every setUp (init, config, add, commit, ...) builds it once per
process as a template, under its own temp folder, and copies it into each test's folder with shutil.copytree: no
process start. Each test still owns its copy alone (objects, refs, index, config); the template is never handed to a
test and never written after it is built. A layout whose files name absolute paths (a remote url, linked worktrees)
is pointed at the copy by relink(); references() lists the git files that still name a path outside the copy (the
template or any other folder), and push_fixtures asserts it is empty.
"""
import os
import shutil

# The git files that can hold an absolute path to another folder (gitrepository-layout(5)).
_PATH_FILES = ("config", "gitdir", "commondir")


def copy(base, key, dest, build):
    """Copy the template <base>/templates/<key> to dest (missing, or an empty folder) and return dest. The template is
    built in place by build(path) the first time (and again if its folder is gone, e.g. after a tearDownModule); a
    failed build is removed, so no half template is ever copied. Built in place, not renamed: the absolute paths git
    writes in it (a remote url, worktree links) name the template's own folder."""
    src = template(base, key)
    if not os.path.isdir(src):
        os.makedirs(src)
        try:
            build(src)
        except BaseException:
            shutil.rmtree(src, ignore_errors=True)
            raise
    if os.path.isdir(dest) and os.listdir(dest):
        raise AssertionError("{} is not empty: a template is copied only into a new folder".format(dest))
    shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    return dest


def template(base, key):
    """The template's folder (to find its paths in a copy)."""
    return os.path.join(base, "templates", key)


def _path_files(dest):
    for top, dirs, names in os.walk(dest):
        dirs[:] = [d for d in dirs if d != "objects"]
        for n in names:
            p = os.path.join(top, n)
            if n in _PATH_FILES or (n == ".git" and os.path.isfile(p)):
                yield p


def relink(dest, old):
    """Point the git files under dest that name a path inside old (the template: a remote url, a worktree's .git file
    and its gitdir) at the same path inside dest. A plain text rewrite, the same on every git version (git 2.47's
    `worktree repair` keeps links that still name a valid worktree, the template's own)."""
    for p in _path_files(dest):
        with open(p, errors="surrogateescape") as f:
            text = f.read()
        if old + os.sep in text:
            with open(p, "w", errors="surrogateescape") as f:
                f.write(text.replace(old + os.sep, dest + os.sep))


def _paths_named(text):
    """The absolute paths a git path file names: a config value (`key = value`, a `file://` url read as its path),
    a `.git` file's `gitdir: <path>`, or a gitdir file's bare line."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if line.startswith("gitdir:"):
            value = line[len("gitdir:"):]
        elif "=" in line:
            value = line.split("=", 1)[1]
        else:
            value = line
        value = value.strip().strip('"')
        if value.startswith("file://"):
            value = value[len("file://"):]
        if os.path.isabs(value):
            yield value


def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def references(dest, old=None):
    """The git files under dest that name an absolute path outside dest (a copy must name only itself, t-114 REVIEW
    R1): the template (old), another test's copy, or any other folder. old is kept for the callers; it is outside
    dest, so it is flagged like any other outside path."""
    found = []
    for p in _path_files(dest):
        with open(p, errors="replace") as f:
            text = f.read()
        if any(not _inside(v, dest) for v in _paths_named(text)) or (old and old in text):
            found.append(p)
    return found
