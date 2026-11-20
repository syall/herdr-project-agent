// "gpui-base" for the panel harness (t-049, t-069): only the names Herdr's gpui-base exports, from the stand-ins in
// stubs.mjs. run.mjs maps `from "gpui-base"` here (see gpui-kit.mjs); tests/test_mod_imports.py reads this list.
// h_flex and v_flex are gpui-base's, not gpui-kit's.
export { v_flex, h_flex, InputState, Input, ScrollHandle, TextView } from "./stubs.mjs";
