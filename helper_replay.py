"""Compare a changed pure helper on inputs observed in the unchanged test suite."""

import ast
import builtins
import copy
from functools import lru_cache
import json
import operator
import re

from pure_contract import allowed, signature
from semantic import parsed, source_difference
from operand_values import imported_seq, expression_value


class Unknown(Exception):
    pass


class Opaque:
    """Opaque original object. Any operation requiring its contents is unknown."""

    def unknown(self, *args, **kwargs):
        raise Unknown

    __bool__ = __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = unknown
    __len__ = __getitem__ = __iter__ = __int__ = __float__ = __index__ = unknown
    __str__ = __repr__ = __getattr__ = __hash__ = __contains__ = unknown
    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = unknown
    __mod__ = __rmod__ = __floordiv__ = __rfloordiv__ = __abs__ = unknown
    __neg__ = __pos__ = unknown


def checked_isinstance(value, cls):
    if type(value) is Opaque:
        raise Unknown
    return isinstance(value, cls)


@lru_cache(maxsize=16)
def baseline_spans(source, names):
    return [
        (n.lineno, n.end_lineno)
        for n in parsed(source).body
        if isinstance(n, ast.FunctionDef) and n.name in names
    ]


def overlaps(hunks, spans):
    line = 0
    for text in hunks:
        if text.startswith("@@ "):
            match = re.match(r"@@ -(\d+)", text)
            if match is None:
                return True
            line = int(match[1])
        elif text.startswith(("-", "+")):
            if any(start <= line <= end for start, end in spans):
                return True
            line += text.startswith("-")
        elif text.startswith(" "):
            line += 1
    return False


def fn_pair(source, updated, names):
    before, after = parsed(source), parsed(updated)
    if len(before.body) != len(after.body):
        return None
    diff = source_difference(source, updated) or []
    pair = next(
        (
            (a, b)
            for a, b in diff
            if isinstance(a, ast.FunctionDef) and a in before.body
        ),
        None,
    )
    if pair is None:
        return None
    a, b = pair
    if not (
        isinstance(b, ast.FunctionDef)
        and a.name == b.name
        and a.name in names
        and allowed(a)
        and allowed(b)
    ):
        return None
    if (
        any(ast.dump(a.__dict__[f]) != ast.dump(b.__dict__[f]) for f in ("args",))
        or a.returns is not None
        and (b.returns is None or ast.dump(a.returns) != ast.dump(b.returns))
    ):
        return None
    if bool(a.returns) != bool(b.returns) or ast.get_docstring(a) != ast.get_docstring(
        b
    ):
        return None
    if a.type_comment != b.type_comment or any(
        getattr(fn, "type_params", ()) for fn in (a, b)
    ):
        return None
    return a, b


def make_runner(fn):
    from pure_contract import BUILTINS

    clean = copy.deepcopy(fn)
    clean.args = ast.arguments(
        posonlyargs=[],
        args=[ast.arg(arg=n) for n in signature(clean)],
        vararg=None,
        kwonlyargs=[],
        kw_defaults=[],
        kwarg=None,
        defaults=[],
    )
    clean.returns = None
    clean.type_comment = None
    clean.decorator_list = []
    ops = {
        "Add": operator.add,
        "Sub": operator.sub,
        "FloorDiv": operator.floordiv,
        "Mod": operator.mod,
    }

    class CheckedArithmetic(ast.NodeTransformer):
        def visit_BinOp(self, node):
            self.generic_visit(node)
            return ast.copy_location(
                ast.Call(
                    ast.Name("_bounded_binary", ast.Load()),
                    [ast.Constant(type(node.op).__name__), node.left, node.right],
                    [],
                ),
                node,
            )

    def bounded_binary(op, left, right):
        if (
            type(left) is not int
            or type(right) is not int
            or left.bit_length() > 55
            or right.bit_length() > 55
        ):
            raise Unknown
        return ops[op](left, right)

    node = ast.fix_missing_locations(
        CheckedArithmetic().visit(ast.Module(body=[clean], type_ignores=[]))
    )
    safe = {n: getattr(builtins, n) for n in BUILTINS}
    safe["isinstance"] = checked_isinstance
    scope = {"__builtins__": safe, "_bounded_binary": bounded_binary}
    exec(compile(node, "<pure function>", "exec", dont_inherit=True), scope)
    return scope[fn.name]


def inputs(raw):
    aliases = {}

    def decode(v):
        k, x = v
        if k == "v":
            return x
        if k == "o":
            return aliases.setdefault(x, Opaque())
        if k in ("T", "L"):
            if x > 256:
                raise Unknown
            items = [Opaque() for _ in range(x)]
        else:
            items = [decode(item) for item in x]
        return items if k in ("l", "L") else tuple(items)

    return [decode(i) for i in json.loads(raw)]


def equal_output(old, new, args):
    if old is new:
        return True
    if type(old) != type(new) or type(old) not in (tuple, list):
        return False
    if any(old is arg or new is arg for arg in args):
        return False
    return len(old) == len(new) and all(
        equal_output(x, y, args) for x, y in zip(old, new)
    )


def caller_edit(source, updated, fn):
    if fn != "sqlglot/helper.py:seq_get" or not imported_seq(source):
        return None
    diff = source_difference(source, updated) or []
    for a, b in diff:
        if not (
            isinstance(a, ast.Call)
            and isinstance(b, ast.Call)
            and isinstance(a.func, ast.Name)
            and a.func.id == "seq_get"
            and ast.dump(a.func) == ast.dump(b.func)
            and not (a.keywords or b.keywords)
            and len(a.args) == len(b.args) == 2
            and ast.dump(a.args[0]) == ast.dump(b.args[0])
        ):
            continue
        old, new = expression_value(a.args[1]), expression_value(b.args[1])
        if type(old) is int and type(new) is int:
            return a.lineno, a.col_offset, old, new
    return None


class HelperProfile:
    def __init__(self, profile, numbers):
        self.rows = profile["functions"]
        self.cold = {
            numbers[name]: rows
            for name, rows in profile.get("cold", {}).items()
            if name in numbers
        }
        self.paths = {}
        for key in self.rows:
            path, name = key.split(":")
            self.paths.setdefault(path, set()).add(name)
        self.numbers = [numbers.get(t) for t in profile["tests"]]
        self.unseen = set(numbers.values()).difference(self.numbers)

    def selection(self, sections, selector, apply_hunks):
        if not (
            len(sections) in (1, 2)
            and len({p for p, h in sections}) == len(sections)
            and all(p in selector.sources for p, h in sections)
        ):
            return None
        paths = [p for p, _ in sections if p in self.paths]
        if not paths:
            return None
        source_pairs = {}
        pair = None
        for path, hunks in sections:
            source = selector.sources[path]
            if len(sections) == 1 and not overlaps(
                hunks, baseline_spans(source, tuple(sorted(self.paths[path])))
            ):
                return None
            updated = apply_hunks(source, hunks)
            source_pairs[path] = (source, updated)
            if path in self.paths and overlaps(
                hunks, baseline_spans(source, tuple(sorted(self.paths[path])))
            ):
                definition = fn_pair(source, updated, self.paths[path])
                if definition:
                    if pair is not None:
                        return None
                    pair = (path, *definition)
        if pair is None:
            return None
        path, old, new = pair
        fn = path + ":" + old.name
        target = None
        if len(sections) == 2:
            caller = next(p for p in source_pairs if p != path)
            edit = caller_edit(*source_pairs[caller], fn)
            if edit is None:
                return None
            line, col, original, patched = edit
            target = f"{caller}:{line}:{col}"
            if target not in self.rows[fn]:
                return None
            # Import-time and worker-only reads without site attribution keep
            # the general selection. A changed caller can enter via an alias.
            if line in selector.startup.get(caller, ()):
                return None
        left, right = make_runner(old), make_runner(new)

        @lru_cache(maxsize=8192)
        def differs(raw, adjusted):
            if raw == "?":
                return True
            try:
                args = inputs(raw)
                old_output = left(*args)
                next_args = args
                if adjusted:
                    if (
                        len(args) != 2
                        or type(args[1]) is not int
                        or args[1] != original
                    ):
                        return True
                    next_args = [args[0], patched]
                return not equal_output(old_output, right(*next_args), args + next_args)
            except (
                Unknown,
                TypeError,
                IndexError,
                ValueError,
                KeyError,
                ArithmeticError,
                AttributeError,
            ):
                return True

        measured = set()
        direct_risk = set()
        risk = self.unseen.copy()
        startup_changed = False
        for site, observations in self.rows[fn].items():
            for raw, slots in observations.items():
                possible = differs(raw, site == target)
                if -1 in slots and possible:
                    startup_changed = True
                ids = {self.numbers[i] for i in slots if i >= 0}
                measured.update(ids)
                if possible:
                    risk.update(ids)
                    direct_risk.update(ids)
        # Baseline coverage independently detects missing input attribution.
        start = old.body[int(ast.get_docstring(old) is not None)].lineno
        for line, slots in selector.lines.get(path, {}).items():
            if start <= line <= old.end_lineno:
                risk.update(selector.numbers_for(slots) - measured)
        for number, rec in selector.isolated.items():
            if number not in measured and any(
                old.lineno < l <= old.end_lineno for l in rec["lines"].get(path, ())
            ):
                risk.add(number)
        if startup_changed:
            # The whole-suite initialization may observe a changed helper call.
            # Only exclude an independently traced test if neither its own
            # fresh imports nor its complete body observed any changed output.
            protected = selector.all_numbers.copy()
            for number, rows in self.cold.items():
                if number in direct_risk or number in self.unseen:
                    continue
                if all(
                    not differs(raw, site == target)
                    for site, values in rows.get(fn, {}).items()
                    for raw in values
                ):
                    protected.discard(number)
                    risk.discard(number)
            risk.update(protected)
        risk.update(selector.untraced_reads)
        for p, _ in sections:
            risk.update(selector.source_readers.get(p, ()))
            risk.update(selector.class_fixtures.get(p, ()))
            risk.update(selector.by_file.get(p, ()))
        return risk
