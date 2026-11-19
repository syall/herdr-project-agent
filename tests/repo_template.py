"""Fixture repositories built once and copied per test (t-095, t-062 F3). Not a test module.

A fixture that made the same repository with git in every setUp (init, config, add, commit, ...) builds it once per
process as a template, under its own temp folder, and copies it into each test's folder with shutil.copytree: no
process start. Each test still owns its copy alone (objects, refs, index, config); the template is never handed to a
test and never written after it is built. A layout whose files name absolute paths (a remote url, linked worktrees)
is fixed by its fixture after the copy; references() lists what still names the template, and the fixtures assert
it is empty.
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


def references(dest, old):
    """The git files under dest whose text still names the folder old (a copy must name only itself)."""
    found = []
    for top, dirs, names in os.walk(dest):
        dirs[:] = [d for d in dirs if d != "objects"]
        for n in names:
            p = os.path.join(top, n)
            if n in _PATH_FILES or (n == ".git" and os.path.isfile(p)):
                with open(p, errors="replace") as f:
                    if old in f.read():
                        found.append(p)
    return found
