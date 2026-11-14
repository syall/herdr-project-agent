// The setup page a new space opens with: set up a Project Agent now, or open another kind
// of session. It is a dialog over the space's first tab, whose launcher is waiting for the
// answer; a click sends that answer to the launcher's terminal (`hc send`), nothing else.
// props: { setup: { terminal, space, folder, home, claude, codex } }.

import { div, svg, image } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { props, closeOverlay, theme, hc, pickFolder, home } from "herdr";
import { PathField } from "./pathfield.js";

export class SetupPane {
  constructor(cx) {
    this.handle = cx.focus_handle();
    this.handle.focus();
    this.path = null; // PathField for "another folder" (made on click)
    this.error = null; this.busy = false;
  }

  choose(word, cx) {
    if (this.busy) return;
    this.busy = true; this.error = null;
    const s = props().setup;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await hc(["send", s.terminal, word]);
        if (r.code !== 0) throw new Error((r.stderr || r.stdout || "hc send failed").trim().slice(0, 200));
        closeOverlay(props().overlay);
      } catch (e) { this.error = `Couldn't reach the tab: ${e.message ?? e}. You can type the choice there.`; this.busy = false; cx.notify(); }
    });
  }

  // "Another folder…": a path field with completion (on the space's machine), starting beside
  // the current folder. A second call confirms what is typed.
  other(cx) {
    const s = props().setup;
    if (!this.path) {
      const folder = String(s.folder ?? ""), mine = s.machine ? null : home();
      const parent = s.home || !folder.includes("/") ? "~/" : `${folder.slice(0, folder.lastIndexOf("/"))}/`;
      this.path = new PathField({ machine: s.machine, value: mine && parent.startsWith(`${mine}/`) ? `~/${parent.slice(mine.length + 1)}` : parent,
        placeholder: "Project folder, e.g. ~/code/project", onSubmit: (cx) => this.other(cx) });
      this.path.refresh(cx);
      this.error = null;
      cx.notify();
      return;
    }
    cx.spawn(async (cx) => {
      const problem = await this.path.check();
      if (problem) { this.error = problem; cx.notify(); return; }
      this.choose(`other ${this.path.value()}`, cx);
    });
  }

  browse(cx) {
    cx.spawn(async (cx) => {
      const folder = await pickFolder({ prompt: "Set up here" });
      if (folder) this.choose(`other ${folder}`, cx);
    });
  }

  // The folder chooser replaces the rest of the page while it is open.
  chooser(t, s) {
    const button = (id, label, primary, click) => h_flex().id(id).h(32).px(13).flex_none().items_center().rounded(8).border(1)
      .border_color(primary ? t.solid : t.input).bg(primary ? t.solid : t.raised).text_color(primary ? t.solid_foreground : t.foreground)
      .text_size(12.5).cursor_pointer().hover((x) => x.opacity(0.88)).on_click(click).child(label);
    return v_flex().gap(10)
      .child(h_flex().gap(8).items_center()
        .child(div().id("setup-back").text_size(12).text_color(t.muted).cursor_pointer().hover((x) => x.text_color(t.foreground))
          .on_click((_e, cx) => { this.path = null; this.error = null; this.handle.focus(); cx.notify(); }).child("← Back"))
        .child(div().flex_1().text_size(14).font_semibold().child("Project folder"))
        .child(div().text_size(11).text_color(t.dim).child(s.machine ? `on ${s.machine}` : "on this computer")))
      .child(this.path.render(t, 7))
      .when(Boolean(this.error), (el) => el.child(div().text_size(11.5).text_color(t.blocked).child(this.error)))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().text_size(11).text_color(t.dim).child("Type to filter · Tab or click completes · Enter confirms"))
        .when(!s.machine, (el) => el.child(button("setup-browse", "Browse…", false, (_e, cx) => this.browse(cx))))
        .child(button("setup-there", this.busy ? "Starting…" : "Set up the Project Agent here", true, (_e, cx) => this.other(cx))));
  }

  render() {
    const t = theme(), s = props().setup ?? {};
    const shown = String(s.folder ?? "").replace(home(), "~");
    const name = shown.split("/").pop() || shown;
    const key = (k) => div().flex_none().w(18).h(18).flex().items_center().justify_center().rounded(4).border(1).border_color(t.border).text_size(10.5).text_color(t.dim).child(k);
    const tile = (id, k, icon, title, detail, word) => v_flex().id(id).flex_1().min_w(0).p(11).gap(5).rounded(9).border(1).border_color(t.border).bg(t.surface)
      .cursor_pointer().hover((x) => x.bg(t.hover).border_color(t.input)).on_click((_e, cx) => this.choose(word, cx))
      .child(h_flex().gap(7).items_center().child(icon).child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().child(title)).child(key(k)))
      .child(div().text_size(11).text_color(t.dim).truncate().child(detail));
    return v_flex().id("pa-setup").track_focus(this.handle).w_full().min_h(0).p(22).gap(14).bg(t.popover).text_color(t.foreground)
      .on_key_down((e, cx) => {
        if (this.path) { // choosing a folder: Tab and the arrows belong to the field
          if (e.key === "escape") { this.path = null; this.error = null; this.handle.focus(); cx.notify(); cx.stop_propagation(); }
          else if (this.path.key(e, cx)) cx.stop_propagation();
          return;
        }
        const k = e.key;
        if (k === "escape") { closeOverlay(props().overlay); cx.stop_propagation(); }
        else if (k === "enter") this.choose(s.home ? "claude" : "here", cx);
        else if (k === "1" && !s.home) this.choose("here", cx);
        else if (k === "2") this.other(cx);
        else if (k === "3") this.choose("claude", cx);
        else if (k === "4") this.choose("codex", cx);
        else if (k === "5") this.choose("shell", cx);
      })
      .when(Boolean(this.path), (el) => el.child(this.chooser(t, s)))
      .when(!this.path, (el) => el.child(this.page(t, s, shown, name, key, tile)));
  }

  page(t, s, shown, name, key, tile) {
    return v_flex().gap(14)
      .child(v_flex().gap(3)
        .child(div().text_size(17).font_semibold().child("New space"))
        .child(div().text_size(12).text_color(t.muted).truncate().child(s.home ? "This space is in your home folder, which isn't a project." : shown)))
      // The main choice: a Project Agent.
      .child(v_flex().p(14).gap(10).rounded(10).border(1).border_color(t.input).bg(t.surface)
        .child(h_flex().gap(9).items_center()
          .child(svg("mods/project-agent/project.svg").size(18).text_color(t.accent))
          .child(div().text_size(14).font_semibold().child("Project Agent")))
        .child(div().text_size(12).text_color(t.muted).child("It plans the work in this space, hands it to worker agents, and asks you only for decisions. It gets the first tab and stays open until you close the space."))
        .child(h_flex().gap(8).items_center().flex_wrap()
          .when(!s.home, (el) => el.child(h_flex().id("setup-here").h(32).px(14).gap(8).flex_none().items_center().rounded(8).bg(t.solid).text_color(t.solid_foreground).text_size(12.8).font_semibold().cursor_pointer()
            .hover((x) => x.opacity(0.88)).on_click((_e, cx) => this.choose("here", cx)).child(this.busy ? "Starting…" : `Set up for ${name}`).child(div().text_size(10.5).opacity(0.7).child("Enter"))))
          .child(h_flex().id("setup-other").h(32).px(12).gap(8).flex_none().items_center().rounded(8).border(1).border_color(t.input).bg(t.raised).text_size(12.5).cursor_pointer()
            .hover((x) => x.bg(t.hover)).on_click((_e, cx) => this.other(cx)).child(s.home ? "Choose a project folder…" : "Another folder…").child(key("2")))))
      // Or no agent: a plain session.
      .child(div().text_size(11.5).text_color(t.dim).child(s.home ? "Or open a session:" : "Or skip it for now and open a session:"))
      .child(h_flex().gap(10)
        .child(tile("setup-claude", s.home ? "↵" : "3", image("icons/claude.svg").size(16), "Claude", s.claude ?? "claude", "claude"))
        .child(tile("setup-codex", "4", image("icons/codex.svg").size(16), "Codex", s.codex ?? "codex", "codex"))
        .child(tile("setup-shell", "5", svg("icons/shell.svg").size(16).text_color(t.muted), "Terminal", "A plain shell", "shell")))
      .when(Boolean(this.error), (el) => el.child(div().text_size(11.5).text_color(t.blocked).child(this.error)))
      .child(div().text_size(11).text_color(t.dim).child("You can start a Project Agent later from the Project panel. Esc closes this; the tab then waits for a typed choice."));
  }
}
