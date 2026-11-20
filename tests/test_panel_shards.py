"""tests/panel/run.mjs's case selection (t-031): --list prints the cases that would run and runs none;
--shard=<k>/<n> runs every n-th case by definition order, so the n shards together run each case exactly once and a
case added later lands in exactly one shard without an edit; case names still pick those cases; an unknown name or
a bad --shard is refused (exit 2). Runs the real harness with --list only: no case runs here."""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RUN = os.path.join(HERE, "panel", "run.mjs")


def node():
    # As tests/run-all.sh finds it: $NODE, else node on PATH, else the newest mise install.
    found = os.environ.get("NODE") or shutil.which("node")
    if not found:
        installs = sorted(glob.glob(os.path.expanduser("~/.local/share/mise/installs/node/*/bin/node")),
                          key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"[./]", p)])
        found = installs[-1] if installs else None
    if not found:
        raise AssertionError("no node found (set NODE)")
    return found


def harness(*args, run=RUN):
    r = subprocess.run([node(), run] + list(args), cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def defined(text):
    return re.findall(r'^cases\["([^"]+)"\] = ', text, re.M)


class Shards(unittest.TestCase):
    def listed(self, *args, run=RUN):
        code, out, err = harness("--list", *args, run=run)
        self.assertEqual(code, 0, out + err)
        self.assertNotRegex(out, r"(?m)^(PASS|FAIL) ", "--list runs no case")
        return out.splitlines()

    def test_list_is_every_case_in_order(self):
        with open(RUN) as f:
            want = defined(f.read())
        self.assertGreaterEqual(len(want), 210)
        self.assertEqual(len(set(want)), len(want), "case names are unique")
        self.assertEqual(self.listed(), want)

    def test_shards_partition_the_cases(self):
        full = self.listed()
        for n in (1, 2, 3, 4, 7):
            with self.subTest(n=n):
                parts = [self.listed("--shard={}/{}".format(k, n)) for k in range(1, n + 1)]
                merged = [name for part in parts for name in part]
                self.assertEqual(sorted(merged), sorted(full), "each case in exactly one shard")
                self.assertEqual(len(merged), len(set(merged)))
                for k, part in enumerate(parts):
                    self.assertEqual(part, full[k::n], "shard {}/{}: every n-th case in order".format(k + 1, n))

    def test_names_pick_those_cases(self):
        self.assertEqual(self.listed("P-noagent", "P-baseline"), ["P-baseline", "P-noagent"])
        full = self.listed()
        one, two = full[0], full[1]  # with --shard=1/2: the first is in it, the second is not
        self.assertEqual(self.listed("--shard=1/2", one, two), [one])

    def test_a_case_added_later_lands_in_one_shard(self):
        tmp = tempfile.mkdtemp(prefix="t031-shards-")
        try:
            os.makedirs(os.path.join(tmp, "tests", "panel"))
            os.symlink(os.path.join(REPO, "mods"), os.path.join(tmp, "mods"))
            os.symlink(os.path.join(HERE, "keeper"), os.path.join(tmp, "tests", "keeper"))
            for name in os.listdir(os.path.join(HERE, "panel")):  # stubs.mjs, gpui-kit.mjs, gpui-base.mjs (t-049)
                if name.endswith(".mjs") and name != "run.mjs":
                    os.symlink(os.path.join(HERE, "panel", name), os.path.join(tmp, "tests", "panel", name))
            with open(RUN) as f:
                text = f.read()
            at = text.index("\nconst pick = ")
            extra = '\ncases["Z-added-later"] = async () => { world(); await mount(); };\n'
            run = os.path.join(tmp, "tests", "panel", "run.mjs")
            with open(run, "w") as f:
                f.write(text[:at] + extra + text[at:])
            full = self.listed(run=run)
            self.assertEqual(full[-1], "Z-added-later")
            hits = [k for k in range(1, 5) if "Z-added-later" in self.listed("--shard={}/4".format(k), run=run)]
            self.assertEqual(len(hits), 1, hits)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refused(self):
        for args in (["--shard=0/4"], ["--shard=5/4"], ["--shard=x"], ["--shard=1/0"], ["--nope"], ["No-such-case"]):
            with self.subTest(args=args):
                code, out, err = harness("--list", *args)
                self.assertEqual(code, 2, out + err)
                self.assertNotRegex(out, r"(?m)^(PASS|FAIL) ")
                self.assertTrue(err.strip(), "says why")


if __name__ == "__main__":
    unittest.main()
