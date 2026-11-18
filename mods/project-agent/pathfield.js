// A folder field with completion: type a path and pick from the folders that match, here or
// on another machine. Lists folders with `ls` on that machine; runs nothing else.
// Use: this.field = new PathField({ machine, value }) in init or a click handler (never in
// render), draw this.field.render(t), read this.field.value(), and await this.field.check().

import { div, svg } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { home } from "herdr";
import { run } from "./host.js";

const MAX_ROWS = 200;

export class PathField {
  constructor({ machine, value, placeholder, onSubmit }) {
    this.machine = machine ?? null;
    this.input = InputState.new({ placeholder: placeholder ?? "Type a folder path", value: value ?? "" });
    this.cache = {}; // folder -> its subfolder names (null: can't list)
    this.listed = null; // the folder the suggestions are for
    this.at = 0; // highlighted suggestion
    this.onSubmit = onSubmit;
    this.homeDir = this.machine ? null : home(); // that machine's home folder, asked once
    this.input.on("change", (_e, cx) => { this.at = 0; this.refresh(cx); });
    this.input.on("submit", (_e, cx) => {
      const pick = this.matches()[this.at];
      // Enter takes the highlighted folder first; Enter again (nothing left to complete) confirms.
      if (pick && this.split().prefix && `${this.split().dir}${pick}/` !== this.input.value()) this.take(pick, cx);
      else this.onSubmit?.(cx);
    });
    this.input.focus();
  }

  value() { return this.input.value().trim().replace(/(.)\/+$/, "$1"); }

  // The typed text as a folder to list and the start of a name inside it.
  split() {
    const text = this.input.value(), cut = text.lastIndexOf("/");
    return cut < 0 ? { dir: "", prefix: text } : { dir: text.slice(0, cut + 1), prefix: text.slice(cut + 1) };
  }

  // Where `ls` should look: "~" is this computer's home here, and the login folder elsewhere.
  real(dir) {
    if (dir === "~/" || dir.startsWith("~/")) return this.machine ? `./${dir.slice(2)}` : `${home()}/${dir.slice(2)}`;
    if (!dir) return this.machine ? "." : home();
    return dir;
  }

  refresh(cx) {
    const { dir } = this.split();
    this.listed = dir;
    if (dir in this.cache) { cx.notify(); return; }
    this.cache[dir] = undefined;
    cx.spawn(async (cx) => {
      let names = null;
      try {
        // -p marks folders with "/", -L follows links, -A shows dot-folders (hidden unless typed).
        const r = await run("ls", ["-1pAL", this.real(dir)], { on: this.machine, timeout: 8000 });
        if (r.stdout || r.code === 0) names = r.stdout.split("\n").filter((n) => n.endsWith("/")).map((n) => n.slice(0, -1)).sort((a, b) => a.localeCompare(b));
      } catch {}
      this.cache[dir] = names;
      cx.notify();
    });
  }

  matches() {
    const { dir, prefix } = this.split(), names = this.cache[dir];
    if (!names) return [];
    const p = prefix.toLowerCase();
    const shown = names.filter((n) => (p.startsWith(".") ? true : !n.startsWith(".")));
    const starts = shown.filter((n) => n.toLowerCase().startsWith(p));
    return [...starts, ...shown.filter((n) => p && !n.toLowerCase().startsWith(p) && n.toLowerCase().includes(p))].slice(0, MAX_ROWS);
  }

  // The typed path with "~" spelled out, for commands that don't expand it. Null until the
  // other machine has said where its home is.
  async absolute() {
    const path = this.value();
    if (path !== "~" && !path.startsWith("~/")) return path;
    if (!this.homeDir) {
      try { this.homeDir = (await run("sh", ["-c", "printf %s \"$HOME\""], { on: this.machine, timeout: 8000 })).stdout.trim() || null; } catch {}
    }
    return this.homeDir ? `${this.homeDir}${path.slice(1)}` : null;
  }

  take(name, cx) {
    // With nothing typed the list is the home folder's: say so in the path.
    this.input.set_value(`${this.split().dir || "~/"}${name}/`);
    this.input.focus();
    this.at = 0;
    this.refresh(cx);
  }

  // Tab completes, the arrows move through the list. Returns true when it used the key.
  key(e, cx) {
    const list = this.matches();
    if (e.key === "tab") { if (list[this.at]) this.take(list[this.at], cx); return true; }
    if (e.key === "down") { this.at = Math.min(this.at + 1, Math.max(0, list.length - 1)); cx.notify(); return true; }
    if (e.key === "up") { this.at = Math.max(this.at - 1, 0); cx.notify(); return true; }
    return false;
  }

  // null when the typed path is a folder on that machine, else why not.
  async check() {
    const path = this.value();
    if (!path) return "Type or pick a folder.";
    if (!(path.startsWith("/") || path === "~" || path.startsWith("~/"))) return "Give a full path (it starts with / or ~/).";
    try {
      const r = await run("test", ["-d", this.real(path.endsWith("/") ? path : `${path}/`)], { on: this.machine, timeout: 8000 });
      return r.code === 0 ? null : "That folder doesn't exist.";
    } catch (e) { return String(e.message ?? e); }
  }

  render(t, rows = 7) {
    const { dir, prefix } = this.split(), names = this.cache[dir], list = this.matches();
    const row = (name, i) => h_flex().id(`pf-${i}`).h(26).flex_none().px(8).gap(7).items_center().rounded(6).cursor_pointer()
      .bg(i === this.at ? t.selected : t.background).hover((s) => s.bg(t.hover))
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).on_click((_e, cx) => this.take(name, cx))
      .child(svg("mods/files/folder.svg").size(13).flex_none().text_color(t.muted))
      .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(t.foreground).child(name))
      .when(i === this.at, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child("Tab")));
    const note = (text) => div().px(8).py(6).text_size(11.5).text_color(t.dim).child(text);
    return v_flex().gap(6)
      .child(Input.new(this.input).h(32).px(10).rounded(8).border_1().border_color(t.input).bg(t.background).text_size(12.5).text_color(t.foreground))
      .child(v_flex().id("pf-list").h(rows * 26 + 8).p(4).gap(0).rounded(8).border(1).border_color(t.border).bg(t.background).overflow_y_scrollbar()
        .when(names === undefined, (el) => el.child(note("Listing folders…")))
        .when(names === null, (el) => el.child(note(dir ? `Can't list ${dir}` : "Start with / or ~/")))
        .when(Array.isArray(names) && !list.length, (el) => el.child(note(prefix ? `No folder here matches "${prefix}".` : "No folders inside. Press Enter to use this one.")))
        .children(list.map(row)));
  }
}
