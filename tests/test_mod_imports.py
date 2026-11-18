"""Every mods/project-agent/*.js named import from "gpui-kit" or "gpui-base" is a name that module really exports
(t-068). The panel harness maps both modules to one stub (tests/panel/stubs.mjs), so it cannot catch a name taken
from the wrong one: Herdr refuses the whole mod ("Could not find export 'h_flex' in module 'gpui-kit'").

The repo has no declaration file for either module, so the lists below are the names the mods import from each and
load with in Herdr, plus that error: h_flex and v_flex are gpui-base's, not gpui-kit's. A new name needs adding here
once it is known to load from that module."""
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODS = os.path.join(os.path.dirname(HERE), "mods", "project-agent")

EXPORTS = {
    "gpui-kit": {"View", "div", "svg", "image"},
    "gpui-base": {"v_flex", "h_flex", "InputState", "Input", "ScrollHandle", "TextView"},
}
IMPORT = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"(gpui-kit|gpui-base)"')


def named(spec):
    # "a, b as c" -> ["a", "b"]: the exported names, not local aliases.
    return [p.split(" as ")[0].strip() for p in spec.split(",") if p.strip()]


class ModImports(unittest.TestCase):
    def test_gpui_names_come_from_the_module_that_exports_them(self):
        files = sorted(glob.glob(os.path.join(MODS, "*.js")))
        self.assertTrue(files, "no mods/project-agent/*.js found")
        wrong = []
        seen = 0
        for f in files:
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
            for m in IMPORT.finditer(text):
                for name in named(m.group(1)):
                    seen += 1
                    if name not in EXPORTS[m.group(2)]:
                        wrong.append("%s: %s from %s" % (os.path.basename(f), name, m.group(2)))
        self.assertGreater(seen, 0, "no gpui-kit or gpui-base imports found: the pattern no longer matches")
        self.assertEqual(wrong, [], "names the module does not export (Herdr refuses the mod)")

    def test_lists_do_not_overlap(self):
        self.assertEqual(EXPORTS["gpui-kit"] & EXPORTS["gpui-base"], set())


if __name__ == "__main__":
    unittest.main()
