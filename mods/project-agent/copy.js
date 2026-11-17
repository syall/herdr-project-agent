// Copying text out of the panel (t-026). A Copy control per card, task, rule and Costs block writes that block
// as plain text lines to the clipboard (cx.write_to_clipboard, as Herdr's own machine-add and toast mods do)
// and says "Copied" for COPIED_FOR. It is never inside an element with a click handler, it stops its own click,
// and it does nothing else: no answer, pick, arm, check, run or write.
import { div } from "gpui-kit";
import { TextView } from "gpui-base";

// Selectable text (Job 2): the text as a TextView the mouse can select (Cmd+C copies the selection), escaped into
// one <span>, as Herdr's machine-add mod draws its messages. Put it as the only child of the row's own div, which
// keeps the size, colour and padding. Never inside an element with a click handler (a drag there could click it).
// `id` must be stable across renders and unique in the panel.
const escape = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
export const sel = (id, text) => (text === null || text === undefined || text === "" ? "" : TextView.html(id, `<span>${escape(text)}</span>`).selectable(true));

export const COPIED_FOR = 2000; // ms the control says "Copied" (or "Copy failed")

export class Copier {
  constructor() { this.at = {}; this.failed = {}; }

  // `text` is a function: the block's text is read at the click, from what that render drew.
  button(t, id, text) {
    const shown = Date.now() - (this.at[id] ?? -Infinity) < COPIED_FOR, failed = shown && this.failed[id];
    return div().id(id).flex_none().px(6).h(20).flex().items_center().rounded(5).border(1).border_color(t.border).text_size(10.5)
      .text_color(failed ? t.blocked : shown ? t.done : t.muted).cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => {
        cx.stop_propagation();
        try { cx.write_to_clipboard(String(text())); this.failed[id] = false; } catch { this.failed[id] = true; }
        this.at[id] = Date.now();
        cx.notify();
        // The click's cx ends with the click: the redraw that takes the mark away gets the timer's own cx.
        cx.timer.after(COPIED_FOR + 50, (later) => later.notify());
      })
      .child(failed ? "Copy failed" : shown ? "Copied" : "Copy");
  }
}
