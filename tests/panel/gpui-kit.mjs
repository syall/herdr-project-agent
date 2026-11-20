// "gpui-kit" for the panel harness (t-049, t-069): only the names Herdr's gpui-kit exports, from the stand-ins in
// stubs.mjs. run.mjs maps `from "gpui-kit"` here, so a mod importing a name from the wrong module fails to load here
// as it does in Herdr ("Could not find export 'h_flex' in module 'gpui-kit'"); tests/test_mod_imports.py reads this
// list. A new name goes here once it is known to load from gpui-kit in Herdr.
export { View, div, svg, image } from "./stubs.mjs";
