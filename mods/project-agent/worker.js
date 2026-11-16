// The "delete this worker" dialog: the person's way to close any worker, with what belongs to
// it: its terminal, its worktree (when nobody else uses it) and, if you say so, its task. The
// Project Agent closes finished workers itself (pa-worker close); this dialog is for any other.
// Opened with props { deleteWorker: { space, machine, address, name } }.
// Runs `pa-worker info` to show what would go and `pa-worker delete` when you confirm.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { props, closeOverlay, theme, exec } from "herdr";
import { localId } from "./store.js";

export class WorkerPane {
  constructor(cx) {
    this.handle = cx.focus_handle();
    this.handle.focus();
    this.info = null; this.error = null; this.busy = false;
    this.withTree = true; this.withTask = false; // what to delete besides the worker itself
    const w = props().deleteWorker;
    cx.spawn(async (cx) => {
      try {
        const r = await exec("pa-worker", ["info", localId(w.address), "--space", localId(w.space)], { on: w.machine ?? null, timeout: 30000 });
        if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim());
        this.info = JSON.parse(r.stdout);
        // Work that exists nowhere else is kept unless you tick the box yourself.
        const tree = this.info.worktree;
        this.withTree = Boolean(tree) && !tree.keep && !tree.sharedWith.length && !this.atRisk(tree);
      } catch (e) { this.error = String(e.message ?? e); }
      cx.notify();
    });
  }

  atRisk(tree) { return tree.uncommitted > 0 || (tree.removesBranches && tree.unpushed > 0); }

  confirm(cx) {
    if (this.busy || !this.info) return;
    this.busy = true; this.error = null;
    const w = props().deleteWorker, tree = this.info.worktree;
    const args = ["delete", localId(w.address), "--space", localId(w.space)];
    if (!tree || !this.withTree) args.push("--keep-worktree");
    else if (this.atRisk(tree)) args.push("--discard");
    if (this.withTask) args.push("--with-task");
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec("pa-worker", args, { on: w.machine ?? null, timeout: 120000 });
        if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim().slice(0, 400));
        closeOverlay(props().overlay);
      } catch (e) { this.error = String(e.message ?? e); this.busy = false; cx.notify(); }
    });
  }

  render() {
    const t = theme(), w = props().deleteWorker ?? {}, info = this.info, tree = info?.worktree;
    const box = (id, on, enabled, label, detail, warn, click) => h_flex().id(id).gap(9).px(10).py(8).items_start().rounded(8).border(1).border_color(t.border).bg(t.surface)
      .when(enabled, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)).on_click(click))
      .child(div().flex_none().mt(2).size(14).rounded(4).border(1).border_color(on ? t.accent : t.input).bg(on ? t.accent : t.background))
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(div().text_size(12.5).text_color(enabled ? t.foreground : t.dim).child(label))
        .when(Boolean(detail), (el) => el.child(div().text_size(11.5).text_color(t.muted).child(detail)))
        .when(Boolean(warn), (el) => el.child(div().text_size(11.5).text_color(t.blocked).child(warn))));
    const button = (id, text, primary, click) => h_flex().id(id).h(32).px(14).flex_none().items_center().rounded(8).border(1)
      .border_color(primary ? t.blocked : t.input).bg(primary ? t.blocked + "33" : t.raised).text_color(t.foreground).text_size(12.5).cursor_pointer()
      .hover((s) => s.opacity(0.88)).on_click(click).child(text);
    const shared = tree?.sharedWith?.length ? `Also used by ${tree.sharedWith.join(", ")}, so it stays.` : tree?.keep ? "This task's worktree is marked to keep." : null;
    return v_flex().id("pa-delete-worker").track_focus(this.handle).w_full().min_h(0).p(20).gap(12).bg(t.popover).text_color(t.foreground)
      .on_key_down((e, cx) => { if (e.key === "escape") { closeOverlay(props().overlay); cx.stop_propagation(); } })
      .child(v_flex().gap(3)
        .child(div().text_size(16).font_semibold().child(`Delete worker ${w.name ?? localId(w.address ?? "")}?`))
        .child(div().text_size(12).text_color(t.muted).child(info?.task ? `${info.role ?? "worker"} on ${info.task}: ${info.title ?? ""}` : info ? "Not recorded against a task." : "Looking at what belongs to it…")))
      .when(Boolean(info), (el) => el
        .child(box("dw-term", true, false, "End its session and remove its terminal", info.state === "live" ? "It is still running; whatever it has not written to a file is lost." : `Its terminal is ${info.state}.`, null, null))
        .when(Boolean(tree), (e) => e.child(box("dw-tree", this.withTree && !shared, !shared, `Delete its ${tree.kind}`, tree.path,
          shared ?? (this.atRisk(tree) ? `${tree.uncommitted} uncommitted file(s)${tree.removesBranches ? ` and ${tree.unpushed} unpushed commit(s)` : ""} would be lost. Not ticked for that reason.`
            : tree.removesBranches ? "Its branches go with it." : `Branch ${tree.branch} is kept.`),
          (_e, cx) => { this.withTree = !this.withTree; cx.notify(); })))
        .when(Boolean(info.task) && info.onlyWorker, (e) => e.child(box("dw-task", this.withTask, true, `Also delete task ${info.task}`,
          "With its evidence and decision cards. They go to the trash and can be restored with pa-task restore.", null, (_e, cx) => { this.withTask = !this.withTask; cx.notify(); }))))
      .when(Boolean(this.error), (el) => el.child(div().text_size(11.5).text_color(t.blocked).child(this.error)))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().text_size(11).text_color(t.dim).child("Untick what you want to keep."))
        .child(button("dw-cancel", "Cancel", false, () => closeOverlay(props().overlay)))
        .when(Boolean(info), (el) => el.child(button("dw-go", this.busy ? "Deleting…" : "Delete", true, (_e, cx) => this.confirm(cx)))));
  }
}
