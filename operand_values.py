"""Resolve a small change against primitive values observed in the pristine program."""

import ast
from functools import lru_cache
import operator

from semantic import parsed, source_difference

COMPARISONS = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
UNKNOWN = object()


@lru_cache(maxsize=8192)
def decode(raw):
    if raw == "N":
        return None
    if raw == "T":
        return True
    if raw == "F":
        return False
    if raw.startswith("i"):
        return int(raw[1:])
    if raw.startswith("f"):
        return float(raw[1:])
    if raw.startswith("s"):
        return raw[1:]
    if raw.startswith("t"):
        return tuple(int(x) for x in raw[1:].split(",") if x)
    return UNKNOWN


def expression_value(node):
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError):
        return UNKNOWN
    if (
        value is None
        or type(value) in (bool, str, int, float)
        or (type(value) is tuple and all(type(i) is int for i in value))
    ):
        return value
    return UNKNOWN


def comparison(old, new):
    if (
        len(old.ops) != 1
        or len(new.ops) != 1
        or ast.dump(old.left) != ast.dump(new.left)
    ):
        return None
    lhs, rhs = type(old.ops[0]), type(new.ops[0])
    a, b = expression_value(old.comparators[0]), expression_value(new.comparators[0])
    if UNKNOWN in (a, b) or lhs not in COMPARISONS or rhs not in COMPARISONS:
        return None
    if any(
        op in (ast.Is, ast.IsNot) and val is not None and type(val) is not bool
        for op, val in ((lhs, a), (rhs, b))
    ):
        return None

    def differs(raw):
        value = decode(raw)
        if value is UNKNOWN:
            return True
        try:
            return COMPARISONS[lhs](value, a) != COMPARISONS[rhs](value, b)
        except (TypeError, ValueError, OverflowError):
            return True

    return differs


@lru_cache(maxsize=100)
def imported_seq(source):
    tree = parsed(source)
    if not any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "sqlglot.helper"
        and not stmt.level
        and any(
            i.name == "seq_get" and i.asname in (None, "seq_get") for i in stmt.names
        )
        for stmt in tree.body
    ):
        return False
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == "seq_get"
            or isinstance(node, ast.arg)
            and node.arg == "seq_get"
            or isinstance(node, ast.Name)
            and node.id == "seq_get"
            and not isinstance(node.ctx, ast.Load)
        ):
            return False
    return True


def relation(old, new):
    if (
        len(old.ops) != 1
        or len(new.ops) != 1
        or type(old.ops[0]) not in (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
        or type(new.ops[0]) not in (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
        or ast.dump(old.left) != ast.dump(new.left)
        or ast.dump(old.comparators[0]) != ast.dump(new.comparators[0])
    ):
        return None
    outcomes = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
    a, b = outcomes[type(old.ops[0])], outcomes[type(new.ops[0])]
    return lambda raw: len(raw) != 2 or raw[0] != "r" or (raw[1] in a) != (raw[1] in b)


def sequence(old, new, source):
    if (
        not imported_seq(source)
        or not isinstance(old.func, ast.Name)
        or old.func.id != "seq_get"
        or ast.dump(old.func) != ast.dump(new.func)
    ):
        return None
    if (
        len(old.args) != 2
        or len(new.args) != 2
        or old.keywords
        or new.keywords
        or ast.dump(old.args[0]) != ast.dump(new.args[0])
    ):
        return None
    a, b = expression_value(old.args[1]), expression_value(new.args[1])
    if type(a) is not int or type(b) is not int:
        return None

    def differs(raw):
        if not raw.startswith("q"):
            return True
        seq = raw[1:]

        def get(k):
            return seq[k] if -len(seq) <= k < len(seq) else "A"

        return get(a) != get(b)

    return differs


def boolean(old, new, parents):
    if (
        not isinstance(old.op, (ast.And, ast.Or))
        or type(old.op) is type(new.op)
        or len(old.values) != len(new.values)
    ):
        return None
    if not all(ast.dump(a) == ast.dump(b) for a, b in zip(old.values, new.values)):
        return None
    child = old
    for n, _ in parents:
        if isinstance(n, ast.BoolOp):
            child = n
            continue
        if (
            isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert))
            and n.test is child
            or isinstance(n, ast.UnaryOp)
            and isinstance(n.op, ast.Not)
            or isinstance(n, ast.comprehension)
            and child in n.ifs
        ):
            return lambda raw: not raw.startswith("b") or ("1" in raw and "0" in raw)
        return None
    return None


class ValueProfile:
    def __init__(self, profile, numbers):
        self.sites = profile["sites"]
        self.slots = [numbers.get(t) for t in profile["tests"]]
        self.unseen = set(numbers.values()) - set(self.slots)

    def selection(self, path, source, replacement, selector):
        diff = source_difference(source, replacement)
        if not diff:
            return None
        child = None
        for n, _ in diff:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if (
                    child not in n.body
                    or any(
                        not isinstance(d, ast.Name)
                        or d.id not in ("classmethod", "staticmethod", "property")
                        for d in n.decorator_list
                    )
                    or n.name in ("__init_subclass__", "__set_name__")
                ):
                    return None
                break
            if isinstance(n, ast.Lambda) and child is n.body:
                break
            child = n
        else:
            return None
        for i, (old, new) in enumerate(diff):
            if type(old) is not type(new):
                continue
            if isinstance(old, ast.Compare):
                kind = "cmp"
                evaluate = comparison(old, new)
                if evaluate is None:
                    kind = "rel"
                    evaluate = relation(old, new)
            elif (
                isinstance(old, ast.Call)
                and isinstance(old.func, ast.Name)
                and old.func.id == "seq_get"
            ):
                kind = "seq"
                evaluate = sequence(old, new, source)
            elif isinstance(old, ast.BoolOp):
                kind = "bool"
                evaluate = boolean(old, new, diff[i + 1 :])
            else:
                continue
            if evaluate is None:
                continue
            key = f"v:{path}:{old.lineno}:{old.col_offset}:{kind}"
            entries = self.sites.get(key)
            if not entries and kind == "bool":
                entries = self.sites.get(key.removesuffix(":bool") + ":extended")
            if not entries:
                return None
            risk = self.unseen.copy()
            measured = set()
            for raw, slots in entries.items():
                tests = {self.slots[j] for j in slots}
                measured.update(tests)
                if raw == "?" or evaluate(raw):
                    risk.update(tests)
            # An isolated worker trace reaching this physical line without a
            # corresponding operand observation can indicate startup attribution.
            risk.update(
                n
                for n, rec in selector.isolated.items()
                if n not in measured and old.lineno in rec["lines"].get(path, ())
            )
            risk.discard(None)
            return risk
        return None
