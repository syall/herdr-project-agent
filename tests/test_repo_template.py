"""repo_template.references() (t-114 REVIEW R1): a copy must name only itself. It flags a git path file that names
any absolute path outside the copy: the template, another test's copy, or any other folder; not only the template."""
import os
import shutil
import subprocess
import tempfile
import unittest

import repo_template

ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t",
           GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True)


def build(path):
    """origin.git (bare), main (its clone: an absolute remote url) and wt (main's linked worktree)."""
    git("init", "-q", "--bare", os.path.join(path, "origin.git"))
    git("clone", "-q", os.path.join(path, "origin.git"), os.path.join(path, "main"))
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=os.path.join(path, "main"))
    git("worktree", "add", "-q", "-b", "w", os.path.join(path, "wt"), cwd=os.path.join(path, "main"))


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


class References(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.old = repo_template.template(self.base, "repos")

    def copy(self, name):
        dest = os.path.join(self.base, name)
        os.makedirs(dest)
        repo_template.copy(self.base, "repos", dest, build)
        repo_template.relink(dest, self.old)
        return dest

    def test_a_relinked_copy_names_only_itself(self):
        dest = self.copy("a")
        self.assertEqual([], repo_template.references(dest, self.old))
        self.assertEqual([], repo_template.references(dest))

    def test_the_template_is_flagged(self):
        dest = os.path.join(self.base, "raw")
        os.makedirs(dest)
        repo_template.copy(self.base, "repos", dest, build)  # not relinked: it names the template
        found = repo_template.references(dest)
        self.assertIn(os.path.join(dest, "wt", ".git"), found)
        self.assertIn(os.path.join(dest, "main", ".git", "config"), found)

    def test_another_copy_is_flagged(self):
        a, b = self.copy("a"), self.copy("b")
        link = os.path.join(b, "wt", ".git")
        write(link, read(link).replace(b + os.sep, a + os.sep))  # b's worktree points at a's repo (t-114 VERIFY §1)
        self.assertEqual([link], repo_template.references(b, self.old))
        gitdir = os.path.join(b, "main", ".git", "worktrees", "wt", "gitdir")
        write(gitdir, os.path.join(a, "wt", ".git") + "\n")
        self.assertEqual(sorted([link, gitdir]), sorted(repo_template.references(b, self.old)))

    def test_an_outside_url_is_flagged(self):
        dest = self.copy("a")
        config = os.path.join(dest, "main", ".git", "config")
        elsewhere = os.path.join(self.base, "elsewhere.git")
        for url in ("file://" + elsewhere, elsewhere, '"{}"'.format(elsewhere)):
            text = read(config)
            git("config", "remote.origin.url", "x", cwd=os.path.join(dest, "main"))
            write(config, read(config).replace("url = x", "url = " + url))
            self.assertEqual([config], repo_template.references(dest, self.old), url)
            write(config, text)
        self.assertEqual([], repo_template.references(dest, self.old))

    def test_a_sibling_folder_name_is_outside(self):
        dest = self.copy("a")
        link = os.path.join(dest, "wt", ".git")
        write(link, read(link).replace(dest + os.sep, dest + "-x" + os.sep))  # "<dest>-x/..." is not inside dest
        self.assertEqual([link], repo_template.references(dest))


if __name__ == "__main__":
    unittest.main()
