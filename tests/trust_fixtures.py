"""Fake screens for tests/test_pa_trust.py (t-035).

The primary fixture is a real screen: tests/trust_screens/claude-real.txt is `hc read <t> --lines 0` of a worker
blocked on Claude Code's folder trust prompt (saved by the Project Agent as evidence/t-035/trust-screen.txt, spec
Q1), byte for byte except that the folder path line is replaced by `{path}` (the real path named the person's
home; this repo is public). It shows: 'Accessing workspace:', the folder alone on its line, the safety question,
'Claude Code'll be able to read, edit, and execute files here.', 'Security guide', the two choices with the cursor
on '❯ No, exit' above 'Yes, I trust this folder' (no numbers), and 'Enter to confirm · Esc to cancel'. The screen
is 200 columns wide.

`old_screen` is NOT a real screen: the older wording ("Do you trust the files in this folder?", "1. Yes, proceed")
as the spec remembers it (spec.md 1.1). The matcher fails closed on any other wording.
"""
import os

REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trust_screens", "claude-real.txt")
WIDTH = 200


def real_lines():
    with open(REAL, encoding="utf-8") as f:
        return f.read().split("\n")


def new_screen(path, cursor="no", extra=None, after_path=None):
    """The real screen with this folder. cursor: "no" (as it opens), "yes" (after one down) or "none".
    extra: lines put in after the "able to read" paragraph (a listing, unknown text).
    after_path: a line right under the path line (a wrapped path's continuation)."""
    out = []
    for line in real_lines():
        if line.strip() == "{path}":
            out.append(line.replace("{path}", path))
            if after_path is not None:
                out.append(" " + after_path)
            continue
        if line.strip() == "❯ No, exit":
            line = (" ❯ No, exit" if cursor == "no" else "   No, exit")
        elif line.strip() == "Yes, I trust this folder":
            line = (" ❯ Yes, I trust this folder" if cursor == "yes" else "   Yes, I trust this folder")
        out.append(line)
        if extra and line.startswith(" Claude Code'll be able"):
            out += [""] + [" " + e for e in extra]
    return "\n".join(out)


INNER = 160


def box(rows):
    out = ["╭" + "─" * (INNER + 2) + "╮"]
    out += ["│ " + r.ljust(INNER) + " │" for r in rows]
    out += ["╰" + "─" * (INNER + 2) + "╯"]
    return out


def old_screen(path, selected=1):
    """The older wording, in a rounded box."""
    yes = ("❯ " if selected == 1 else "  ") + "1. Yes, proceed"
    no = ("❯ " if selected == 2 else "  ") + "2. No, exit"
    rows = ["", "Do you trust the files in this folder?", "", path, "",
            "Claude Code may read files in this folder. Reading untrusted files may lead Claude Code to behave in",
            "unexpected ways.", "",
            "With your permission Claude Code may execute files in this folder. Executing untrusted code is unsafe.", "",
            "Learn more", "", yes, no, ""]
    return "\n".join(box(rows) + ["   Enter to confirm · Esc to exit", ""]) + "\n"


def tool_permission():
    rows = ["Bash command", "", "  git status", "  Show working tree status", "",
            "Do you want to proceed?", "❯ 1. Yes", "  2. Yes, and don't ask again for git status commands in /x/proj",
            "  3. No, and tell Claude what to do differently (esc)"]
    return "\n".join(box(rows)) + "\n"


def mcp_approval():
    rows = ["New MCP server found in .mcp.json: files", "",
            "MCP servers may execute code or access system resources. All tool calls require approval.", "",
            "❯ 1. Use this and all future MCP servers in this project", "  2. Use this MCP server",
            "  3. Continue without using this MCP server"]
    return "\n".join(box(rows)) + "\n"


def bypass_prompt():
    rows = ["WARNING: Claude Code running in Bypass Permissions mode", "",
            "In Bypass Permissions mode, Claude Code will not ask for your approval before running potentially",
            "dangerous commands.", "", "❯ 1. No, exit", "  2. Yes, I accept"]
    return "\n".join(box(rows)) + "\n"


def auto_mode_notice():
    rows = ["Auto mode", "", "Claude decides when to ask you before running a tool.", "",
            "❯ 1. Yes, enable auto mode", "  2. No, exit"]
    return "\n".join(box(rows)) + "\n"


def shell_screen():
    return "$ ls\nREADME.md  src\n$ \n"


def codex_screen(path):
    return ("> You are in " + path + "\n\n"
            "  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of\n"
            "  prompt injection.\n\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n\n"
            "  Press enter to continue\n")


def moved_on():
    """What the worker shows once the prompt is accepted: its launch prompt being worked on."""
    return "> Read the handoff and do the job it describes.\n\n✻ Thinking…\n"
