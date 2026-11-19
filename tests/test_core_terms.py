"""t-087: the generic layer (runtime/core, runtime/tools, mods/project-agent) names no profile's terms.

Runs the real pa-core-check (pa.py cmd_core_check) against temp homes: the core and tools folders and the app's
mods/project-agent are links to this repo's, and the profiles are the repo's sample profiles plus a fixture profile
whose coreCheck lists terms a real profile keeps out of the generic layer. Never reads the real ~/.config.

Run: python3 -m unittest -v tests/test_core_terms.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PA = os.path.join(REPO, "runtime", "tools", "pa.py")

# Terms a fixture profile lists, like an installed work profile does (generic words only, nothing of a real one).
FIXTURE_TERMS = ["usage type"]


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class CoreTerms(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pa-core-terms-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "pa")
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.root, "profiles"))
        os.makedirs(os.path.join(self.app, "mods"))
        os.symlink(os.path.join(REPO, "runtime", "core"), os.path.join(self.root, "core"))
        os.symlink(os.path.join(REPO, "runtime", "tools"), os.path.join(self.root, "tools"))
        os.symlink(os.path.join(REPO, "mods", "project-agent"), os.path.join(self.app, "mods", "project-agent"))
        samples = os.path.join(REPO, "runtime", "profiles")
        for name in sorted(os.listdir(samples)):
            src = os.path.join(samples, name, "profile.json")
            if os.path.isfile(src):
                with open(src, encoding="utf-8") as f:
                    write_json(os.path.join(self.root, "profiles", name, "profile.json"), json.load(f))
        write_json(os.path.join(self.root, "profiles", "fixture-work", "profile.json"),
                   {"name": "fixture-work", "coreCheck": FIXTURE_TERMS})

    def core_check(self, root=None):
        env = dict(os.environ, HERDR_PA_HOME=root or self.root, HERDR_APP_HOME=self.app,
                   CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))
        return subprocess.run([sys.executable, PA, "core-check"], env=env, capture_output=True, text=True, timeout=120)

    def test_generic_layer_has_no_profile_terms(self):
        r = self.core_check()
        self.assertEqual(r.returncode, 0, "pa-core-check found profile terms:\n" + r.stderr + r.stdout)
        self.assertIn("0 profile-specific terms", r.stdout)
        self.assertIn("in 3 folders", r.stdout)  # core, tools and the mod were all scanned

    def test_check_finds_a_planted_term(self):  # guard: the scan is real, so the test above cannot pass by scanning nothing
        root = os.path.join(self.tmp, "planted")
        os.makedirs(os.path.join(root, "core"))
        os.makedirs(os.path.join(root, "tools"))
        with open(os.path.join(root, "tools", "x.py"), "w", encoding="utf-8") as f:
            f.write("# rows with a Usage Type\n")
        write_json(os.path.join(root, "profiles", "fixture-work", "profile.json"), {"coreCheck": FIXTURE_TERMS})
        r = self.core_check(root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("x.py:1: 'Usage Type'", r.stderr)


if __name__ == "__main__":
    unittest.main()
