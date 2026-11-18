// The Rules view: the person's rules for one space (spec section 8). They can add, edit, turn
// off, reorder and delete rules, and approve or decline the orchestrator's proposals. The file
// stays theirs: every change is applied to what is on disk at that moment, and an invalid file
// is flagged, never written over. Below them, a read-only view of the person's push allowlist
// (t-007b), from `pa-push-allow show --json`: read when the view opens, on its Refresh, and on
// reloads while it is shown, at most once per ALLOW_EVERY; never written here.

import { div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { home } from "herdr";
import { loadRules, changeRules, touched, reorder, newRuleId, exportRules, importRules } from "./store.js";
import { readAllow } from "./push.js";
import { Copier, sel } from "./copy.js";

export const ALLOW_EVERY = 30000; // the push allowlist is read at most this often, but for Refresh

// A two-click Delete (a task's in main.js, a rule's here): the first click arms it, { id, space, at }.
// The arm holds while its space is shown, its item is expanded and it is under ARM_WAIT old; a
// press anywhere else in the panel, a Cancel beside it, or another view or space drops it (main.js).
export const ARM_WAIT = 10000;
export const arm = (id, spaceId) => ({ id, space: spaceId, at: Date.now() });
export const armHolds = (a, spaceId, shown) => Boolean(a && a.space === spaceId && shown && Date.now() - a.at < ARM_WAIT);
// A text field whose focus calls onFocus(event, cx) (t-028: focus in a panel field drops an armed Delete).
export function fieldState(options, onFocus) { const s = InputState.new(options); s.on?.("focus", onFocus); return s; }

const FIELDS = [["title", "Title"], ["text", "The rule, in plain language"], ["why", "Why (so later edits keep the intent)"], ["appliesTo", "Applies to: all, a phase, a task type or a worker role"]];

export class RulesPane {
  constructor(field = (options) => InputState.new(options)) {
    this.field = field; // makes the view's text fields (main.js: focus in one drops an armed Delete)
    this.data = null; // loadRules() result
    this.open = new Set(); // expanded rule ids
    this.form = null; // { id | null, approve, inputs: { title, text, why, appliesTo } }
    this.armed = null; // arm(): the rule whose Delete was clicked once (a second click deletes); see armedRule()
    this.pressed = null; // rule id being dragged
    this.declined = false; // declined rules shown
    this.copier = new Copier(); // each open rule's Copy (copy.js, t-026)
    this.message = null; this.error = null;
    this.importPath = null; // InputState, made on first use (never in render)
    this.allow = {}; // space id -> { data, asked, busy }: the last pa-push-allow show --json (data: its report, or { error })
  }

  // Reads the push allowlist for the view (main.js: the view opening, a reload while it is shown;
  // force: its Refresh link). At most once per ALLOW_EVERY unless forced; one read at a time.
  wantAllow(space, cx, force = false) {
    const a = (this.allow[space.id] ??= { data: null, asked: 0, busy: false });
    if (a.busy || (!force && a.asked && Date.now() - a.asked < ALLOW_EVERY)) return;
    a.busy = true; a.asked = Date.now();
    cx.spawn(async (cx) => {
      try { const got = await readAllow(space, cx, this.host, force); if (!got?.busy) a.data = got; } catch (e) { a.data = { error: String(e.message ?? e) }; } // busy (t-053): the last read still pending; keep what is shown
      a.busy = false;
      cx.notify();
    });
  }

  allowView(t, space) {
    const a = this.allow[space.id], d = a?.data;
    const line = (s, color) => div().px(4).text_size(11.5).text_color(color ?? t.muted).child(s);
    const str = (v) => (v === null || v === undefined ? "" : String(v));
    const box = v_flex().id("push-allow").flex_none().mt(8).p(10).gap(4).rounded(7).border(1).border_color(t.border).bg(t.surface)
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).text_size(12).font_semibold().text_color(t.foreground).child("Push allowlist"))
        .child(div().id("push-allow-refresh").flex_none().text_size(11.5).text_color(t.accent).cursor_pointer().on_click((_e, cx) => { this.wantAllow(space, cx, true); cx.notify(); }).child(a?.busy ? "Reading…" : "Refresh")));
    if (!d) box.child(line(a?.busy ? "Push allowlist: reading…" : "Push allowlist: not read yet."));
    else if (d.error) box.child(line(`Push allowlist: could not read it (${str(d.error)})`, t.blocked));
    else if (!d.exists) box.child(line("Push allowlist: off (the default): main-branch pushes are not possible.")).child(line(str(d.file).replace(home(), "~"), t.dim));
    else if (!d.valid) box.child(line(`Push allowlist: not valid, so nothing is allowed: ${str(d.problem)}. Fix it by hand.`, t.blocked)).child(line(str(d.file).replace(home(), "~"), t.dim));
    else {
      box.child(line(`Push allowlist: ${d.enabled ? "on" : "off (enabled: false)"}`, d.enabled ? t.foreground : t.muted))
        .child(line(`${str(d.file)} · changed ${str(d.mtime)}`, t.dim));
      if (!(d.repos ?? []).length) box.child(line("No entries."));
      for (const e of d.repos ?? []) {
        box.child(div().px(4).pt(2).text_size(12).text_color(e?.enabled ? t.foreground : t.muted).child(`${str(e?.path)} → ${str(e?.url)} ${str(e?.branch)}`))
          .child(div().px(4).pl(16).text_size(11).text_color(t.dim).child([e?.thisSpace ? "this space" : `space ${str(e?.space)}`, e?.enabled ? "on" : "off",
            Array.isArray(e?.prePush) && e.prePush.length ? `prePush: ${e.prePush.map(str).join(" ")}` : null, e?.note ? str(e.note) : null].filter(Boolean).join(" · ")));
      }
    }
    return box.child(line("Read only: change it in your own terminal with pa-push-allow, or by hand.", t.dim));
  }

  // The rule id whose Delete is armed, if the arm still holds (armHolds); one that does not is dropped.
  armedRule(spaceId) {
    if (armHolds(this.armed, spaceId, this.armed && this.open.has(this.armed.id))) return this.armed.id;
    this.armed = null;
    return null;
  }

  async load(space) { this.data = await loadRules(space); }

  // Applies a change, then reloads. `after(cx)` runs once the file is written (telling the orchestrator, say),
  // with this task's live cx: the click's is gone by then.
  save(space, cx, change, after) {
    this.error = null; this.message = null;
    cx.spawn(async (cx) => {
      try { await changeRules(space, change); if (after) await after(cx); }
      catch (e) { this.error = String(e.message ?? e); }
      try { await this.load(space); } catch {}
      cx.notify();
    });
  }

  edit(rule, approve) {
    for (const s of Object.values(this.form?.inputs ?? {})) s.release?.();
    const inputs = {};
    for (const [key, placeholder] of FIELDS) inputs[key] = this.field({ placeholder, value: String(rule?.[key] ?? (key === "appliesTo" ? "all" : "")) });
    this.form = { id: rule?.id ?? null, approve: Boolean(approve), inputs };
  }

  closeForm() { for (const s of Object.values(this.form?.inputs ?? {})) s.release?.(); this.form = null; }

  submit(space, cx, onDecided) {
    const f = this.form, v = Object.fromEntries(FIELDS.map(([k]) => [k, f.inputs[k].value().trim()]));
    if (!v.title || !v.text) { this.error = "A rule needs a title and its text."; cx.notify(); return; }
    v.appliesTo ||= "all";
    const id = f.id, approve = f.approve;
    this.closeForm();
    this.save(space, cx, (rules) => {
      if (!id) {
        const top = Math.max(0, ...rules.map((r) => r.order ?? 0));
        return [touched({ id: newRuleId(rules, v.title), ...v, status: "active", order: top + 10, source: "person" }, "created"), ...rules];
      }
      return rules.map((r) => (r.id === id ? touched({ ...r, ...v, ...(approve ? { status: "active" } : {}) }, approve ? "edited and approved" : "edited") : r));
    }, approve ? (cx) => onDecided(id, "approved", cx) : null);
  }

  setStatus(space, cx, id, status, what, onDecided) {
    this.save(space, cx, (rules) => rules.map((r) => (r.id === id ? touched({ ...r, status }, what) : r)), onDecided ? (cx) => onDecided(id, what, cx) : null);
  }

  // These buttons stop the press (so it never reaches the panel's root), so each one but the armed
  // Delete and its Cancel (keepArm) drops the arm itself.
  button(t, id, label, onClick, tone, keepArm) {
    return div().id(id).flex_none().px(8).h(24).flex().items_center().rounded(6).border(1).border_color(t.border).text_size(11.5)
      .text_color(tone ?? t.foreground).cursor_pointer().hover((s) => s.bg(t.hover))
      .on_mouse_down("left", (_e, cx) => { cx.stop_propagation(); if (!keepArm) this.drop(cx); })
      .on_click((e, cx) => { if (!keepArm) this.drop(cx); onClick(e, cx); }).child(label);
  }

  // Drops an armed Delete: on the press of a control that stops it (and on its click, for a click alone).
  drop(cx) {
    if (this.armed) { this.armed = null; cx.notify(); }
  }

  chip(t, text, color) {
    return div().flex_none().px(6).h(18).flex().items_center().rounded(5).bg(color + "26").text_size(10.5).font_semibold().text_color(color).child(text);
  }

  formView(t, space, onDecided) {
    const f = this.form;
    return v_flex().id("rule-form").flex_none().p(10).gap(6).rounded(7).border(1).border_color(t.accent).bg(t.surface)
      .child(div().text_size(12).font_semibold().child(f.id ? (f.approve ? `Edit, then approve ${f.id}` : `Edit ${f.id}`) : "New rule"))
      .children(FIELDS.map(([key]) => Input.new(f.inputs[key]).h(30).px(8).rounded(6).border_1().border_color(t.input).bg(t.background).text_size(12).text_color(t.foreground)))
      .child(h_flex().gap(6)
        .child(this.button(t, "rule-save", f.approve ? "Save and approve" : f.id ? "Save" : "Add rule", (_e, cx) => this.submit(space, cx, onDecided)))
        .child(this.button(t, "rule-cancel", "Cancel", (_e, cx) => { this.closeForm(); cx.notify(); }, t.muted)));
  }

  // A rule as plain text lines (its Copy, t-026): what its open row shows.
  ruleText(rule) {
    return [
      `${rule.title ?? rule.id} · ${rule.status} · ${rule.appliesTo ?? "all"}`,
      String(rule.text ?? ""),
      ...(rule.why ? [`Why: ${rule.why}`] : []),
      ...(rule.needsAttention ? [`The Project Agent can't follow this: ${rule.needsAttention}`] : []),
      `${rule.id} · from ${rule.source ?? "?"} · order ${rule.order ?? 0} · ${(rule.history ?? []).length} changes`,
    ].join("\n");
  }

  row(t, space, rule, onDecided, movable) {
    const armed = this.armedRule(space.id) === rule.id;
    const open = this.open.has(rule.id), proposed = rule.status === "proposed", on = rule.status === "active";
    const tone = { proposed: t.blocked, active: t.done, off: t.dim, declined: t.dim }[rule.status];
    return v_flex().id(`rule-${rule.id}`).flex_none().rounded(7).border(1).border_color(proposed ? t.input : t.border).bg(t.surface)
      .when(this.pressed === rule.id, (el) => el.opacity(0.5))
      .when(movable, (el) => el.on_mouse_up("left", (_e, cx) => {
        const from = this.pressed; this.pressed = null;
        if (from && from !== rule.id) this.save(space, cx, (rules) => reorder(rules, from, rule.id)); else cx.notify();
      }))
      .child(h_flex().id(`rule-head-${rule.id}`).h(32).px(8).gap(7).items_center().cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { open ? this.open.delete(rule.id) : this.open.add(rule.id); this.armed = null; cx.notify(); })
        .when(movable, (el) => el.child(div().id(`rule-grip-${rule.id}`).flex_none().w(14).text_size(11).text_color(t.dim).tooltip("Drag onto another rule to place this one above it")
          .on_mouse_down("left", (_e, cx) => { cx.stop_propagation(); this.pressed = rule.id; this.armed = null; cx.notify(); }).child("⋮⋮")))
        .child(div().flex_none().text_size(11).text_color(t.dim).child(open ? "▾" : "▸"))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(rule.status === "off" || rule.status === "declined" ? t.muted : t.foreground).child(String(rule.title ?? rule.id)))
        .when(Boolean(rule.needsAttention), (el) => el.child(this.chip(t, "needs attention", t.blocked)))
        .child(div().flex_none().text_size(10.5).text_color(t.dim).child(String(rule.appliesTo ?? "all")))
        .when(on || rule.status === "off", (el) => el.child(div().id(`rule-toggle-${rule.id}`).flex_none().px(7).h(20).flex().items_center().rounded(10).text_size(10.5).font_semibold().cursor_pointer()
          .bg(on ? t.done + "33" : t.raised).text_color(on ? t.done : t.dim).on_mouse_down("left", (_e, cx) => { cx.stop_propagation(); this.drop(cx); })
          .on_click((_e, cx) => { cx.stop_propagation(); this.armed = null; this.setStatus(space, cx, rule.id, on ? "off" : "active", on ? "turned off" : "turned on"); }).child(on ? "On" : "Off")))
        .when(!on && rule.status !== "off", (el) => el.child(this.chip(t, rule.status, tone))))
      .when(open, (el) => el.child(v_flex().px(10).pb(9).gap(6).text_size(12).text_color(t.muted)
        // Selectable (t-026): the open rule's text rows; never its head (it folds the rule).
        .child(div().text_color(t.foreground).child(sel(`sel-rule-${rule.id}`, String(rule.text ?? ""))))
        .when(Boolean(rule.why), (e) => e.child(div().child(sel(`sel-rule-why-${rule.id}`, `Why: ${rule.why}`))))
        .when(Boolean(rule.needsAttention), (e) => e.child(div().text_color(t.blocked).child(sel(`sel-rule-att-${rule.id}`, `The Project Agent can't follow this: ${rule.needsAttention}`))))
        .child(div().text_size(11).text_color(t.dim).child(sel(`sel-rule-meta-${rule.id}`, `${rule.id} · from ${rule.source ?? "?"} · order ${rule.order ?? 0} · ${(rule.history ?? []).length} changes`)))
        .child(h_flex().gap(6).flex_wrap()
          .child(this.copier.button(t, `rule-copy-${rule.id}`, () => this.ruleText(rule)))
          .when(proposed, (e) => e
            .child(this.button(t, `rule-approve-${rule.id}`, "Approve", (_e, cx) => this.setStatus(space, cx, rule.id, "active", "approved", onDecided)))
            .child(this.button(t, `rule-editapprove-${rule.id}`, "Edit, then approve", (_e, cx) => { this.edit(rule, true); cx.notify(); }))
            .child(this.button(t, `rule-decline-${rule.id}`, "Decline", (_e, cx) => this.setStatus(space, cx, rule.id, "declined", "declined", onDecided), t.muted)))
          .when(!proposed, (e) => e.child(this.button(t, `rule-edit-${rule.id}`, "Edit", (_e, cx) => { this.edit(rule, false); cx.notify(); })))
          // Two clicks on the same button (ARM_WAIT; Cancel, a press elsewhere, another view or space disarm).
          .child(this.button(t, `rule-delete-${rule.id}`, armed ? "Delete for good?" : "Delete", (_e, cx) => {
            // The arm is asked again at the click (t-028): one that ran out since this was drawn only arms again.
            if (this.armedRule(space.id) !== rule.id) { this.armed = arm(rule.id, space.id); cx.notify(); return; }
            this.armed = null;
            this.save(space, cx, (rules) => rules.filter((r) => r.id !== rule.id));
          }, armed ? t.blocked : t.muted, true))
          .when(armed, (e) => e.child(this.button(t, `rule-delete-cancel-${rule.id}`, "Cancel", (_e, cx) => { this.armed = null; cx.notify(); }, t.muted, true))))));
  }

  render(t, space, onDecided) {
    const d = this.data;
    const note = (text, color) => div().px(4).py(4).text_size(12).text_color(color ?? t.dim).child(text);
    if (!d) return v_flex().p(8).child(note("Loading…")).child(this.allowView(t, space));
    const proposed = d.rules.filter((r) => r.status === "proposed"), live = d.rules.filter((r) => r.status === "active" || r.status === "off"), declined = d.rules.filter((r) => r.status === "declined");
    return v_flex().id("pa-rules").flex_1().min_h(0).p(8).gap(6).overflow_y_scrollbar()
      .on_mouse_up("left", (_e, cx) => { if (this.pressed) { this.pressed = null; cx.notify(); } })
      .when(Boolean(d.error), (el) => el.child(v_flex().p(10).gap(4).rounded(7).border(1).border_color(t.blocked).bg(t.surface)
        .child(div().text_size(12).text_color(t.blocked).child(d.error))
        .child(div().text_size(11.5).text_color(t.muted).child(`Fix it by hand; this view won't write over it. ${String(d.path).replace(home(), "~")}`))))
      .when(Boolean(this.error), (el) => el.child(note(this.error, t.blocked)))
      .when(Boolean(this.message), (el) => el.child(note(this.message, t.muted)))
      .when(!d.error, (el) => el
        .child(h_flex().gap(6).flex_wrap()
          .child(this.button(t, "rule-add", "+ Add rule", (_e, cx) => { this.edit(null, false); cx.notify(); }))
          .child(this.button(t, "rule-export", "Export", (_e, cx) => cx.spawn(async (cx) => {
            try { this.message = `Exported to ${(await exportRules(space, d.rules)).replace(home(), "~")}`; } catch (e) { this.error = String(e.message ?? e); }
            cx.notify();
          }), t.muted))
          .child(this.button(t, "rule-import", this.importPath ? "Import this file" : "Import…", (_e, cx) => {
            if (!this.importPath) { this.importPath = this.field({ placeholder: "Path to an exported rules file" }); cx.notify(); return; }
            const path = this.importPath.value().trim();
            if (!path) { this.importPath.release?.(); this.importPath = null; cx.notify(); return; }
            cx.spawn(async (cx) => {
              try { this.message = `Imported ${await importRules(space, path)} new rules.`; this.importPath.release?.(); this.importPath = null; await this.load(space); }
              catch (e) { this.error = String(e.message ?? e); }
              cx.notify();
            });
          }, t.muted)))
        .when(Boolean(this.importPath), (e) => e.child(Input.new(this.importPath).h(30).px(8).rounded(6).border_1().border_color(t.input).bg(t.background).text_size(12).text_color(t.foreground)))
        .when(Boolean(this.form), (e) => e.child(this.formView(t, space, onDecided)))
        .when(proposed.length > 0, (e) => e.child(note(`Proposed by the Project Agent · ${proposed.length}`)).children(proposed.map((r) => this.row(t, space, r, onDecided, false))))
        .child(note(live.length ? "Your rules, highest precedence first" : "No rules yet."))
        .children(live.map((r) => this.row(t, space, r, onDecided, true)))
        .when(declined.length > 0, (e) => e.child(div().id("rules-declined").px(4).py(4).text_size(11.5).text_color(t.dim).cursor_pointer().hover((s) => s.text_color(t.foreground))
          .on_click((_e, cx) => { this.declined = !this.declined; cx.notify(); }).child(`${this.declined ? "▾" : "▸"} ${declined.length} declined (kept so they aren't proposed again)`)))
        .when(this.declined, (e) => e.children(declined.map((r) => this.row(t, space, r, onDecided, false)))))
      .child(this.allowView(t, space));
  }
}
