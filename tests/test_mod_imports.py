"""Every mods/project-agent/*.js named import from "gpui-kit" or "gpui-base" is a name that module really exports
(t-068). Herdr refuses the whole mod for a name taken from the wrong module ("Could not find export 'h_flex' in
module 'gpui-kit'").

The repo has no declaration file for either module, so the lists are the panel harness's own stand-ins for them
(t-049, t-069): tests/panel/gpui-kit.mjs and tests/panel/gpui-base.mjs each re-export only the names that module has
in Herdr, and the panel harness loads the mods against them (a wrong module fails there too). This test reads the
lists from those two files; a new name is added there once it is known to load from that module."""
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODS = os.path.join(os.path.dirname(HERE), "mods", "project-agent")
MODULES = ("gpui-kit", "gpui-base")
IMPORT = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"(gpui-kit|gpui-base)"')
REEXPORT = re.compile(r'^export\s*\{([^}]*)\}\s*from\s*"\./stubs\.mjs";', re.M)


def named(spec):
    # "a, b as c" -> ["a", "b"]: the exported names, not local aliases.
    return [p.split(" as ")[0].strip() for p in spec.split(",") if p.strip()]


def exported(module):
    """The names tests/panel/<module>.mjs re-exports from stubs.mjs (`export { a, b as c } from ...` -> the names
    the module exports: a and c)."""
    with open(os.path.join(HERE, "panel", module + ".mjs"), encoding="utf-8") as fh:
        text = fh.read()
    names = set()
    for m in REEXPORT.finditer(text):
        names.update(p.split(" as ")[-1].strip() for p in m.group(1).split(",") if p.strip())
    return names


EXPORTS = {m: exported(m) for m in MODULES}


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

    def test_lists_are_read_from_the_panel_stand_ins(self):
        for m in MODULES:
            self.assertTrue(EXPORTS[m], "no names read from tests/panel/{}.mjs: the pattern no longer matches".format(m))
        # the module Herdr's error names (t-068): h_flex and v_flex are gpui-base's
        self.assertTrue({"h_flex", "v_flex"} <= EXPORTS["gpui-base"])
        self.assertFalse({"h_flex", "v_flex"} & EXPORTS["gpui-kit"])


if __name__ == "__main__":
    unittest.main()
