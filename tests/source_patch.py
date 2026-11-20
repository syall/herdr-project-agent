"""Inject code at the start of a function in a Python source file, for tests that plant a bug in a copy of pa.py
(t-195 round 2, VERIFY M1). Not a test module.

The function is found by name in the parsed source (any signature, any line layout), not by an exact text match, so
a later change to its def line does not make the patch silently miss. Anything that keeps the patch from landing
exactly once (no such top-level function, two of them, a source that does not parse, a result whose function does
not start with the code) exits 3 with `patch did not apply: <why>` on stderr and leaves the file unchanged.

Usage: python3 tests/source_patch.py <file> <function> <code>
  <code> is one or more Python statements; `{arg}` in it is replaced by the function's first parameter's name.
"""
import ast
import sys


class NotApplied(Exception):
    pass


def first_param(fn):
    params = list(getattr(fn.args, "posonlyargs", [])) + list(fn.args.args)
    if not params:
        raise NotApplied("{} has no parameter for {{arg}}".format(fn.name))
    return params[0].arg


def find(tree, name):
    found = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(found) != 1:
        raise NotApplied("{} top-level function(s) named {!r}".format(len(found), name))
    return found[0]


def body_start(fn):
    """(the 0-based line before which the code goes, its indent, the statement index it gets): after the
    docstring, else before the first statement."""
    first = fn.body[0]
    if (isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant)
            and isinstance(first.value.value, str) and len(fn.body) > 1):
        return first.end_lineno, fn.body[1].col_offset, 1
    return first.lineno - 1, first.col_offset, 0


def patch(text, name, code):
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise NotApplied("the source does not parse: {}".format(e))
    fn = find(tree, name)
    if "{arg}" in code:
        code = code.replace("{arg}", first_param(fn))
    try:
        planted = ast.parse(code).body
    except SyntaxError as e:
        raise NotApplied("the code does not parse: {}".format(e))
    if not planted:
        raise NotApplied("no code")
    at, indent, offset = body_start(fn)
    lines = text.splitlines(True)
    if at > 0 and not lines[at - 1].endswith("\n"):
        raise NotApplied("unexpected line ending at line {}".format(at))
    block = "".join(" " * indent + line + "\n" for line in code.strip("\n").splitlines())
    out = "".join(lines[:at]) + block + "".join(lines[at:])
    try:
        again = find(ast.parse(out), name)
    except SyntaxError as e:
        raise NotApplied("the result does not parse: {}".format(e))
    stmts = again.body[offset:offset + len(planted)]
    if [ast.dump(s) for s in stmts] != [ast.dump(s) for s in planted]:
        raise NotApplied("the function does not start with the code after the patch")
    return out


def main(argv):
    if len(argv) != 3:
        print("usage: source_patch.py <file> <function> <code>", file=sys.stderr)
        return 2
    path, name, code = argv
    try:
        with open(path) as f:
            text = f.read()
        out = patch(text, name, code)
    except (NotApplied, OSError, UnicodeDecodeError) as e:
        print("patch did not apply: {}: {}".format(path, e), file=sys.stderr)
        return 3
    with open(path, "w") as f:
        f.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
