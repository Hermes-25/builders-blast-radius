"""Capture primitive baseline operand values, argument sequence shape and boolean operands."""

import ast
import builtins
from collections import defaultdict
from functools import lru_cache
import math
import os
from pathlib import Path
import sys

import build_options as profiler

UNKNOWN = object()
known = defaultdict(set)
SCALAR = (int, float, str, bool)


@lru_cache(maxsize=100000)
def label(site, observation):
    seen = known[site]
    if observation not in seen:
        if len(seen) >= 72:
            observation = "?"
        else:
            seen.add(observation)
    return site + "|" + observation


def record(site, observation):
    profiler.observations[label(site, observation)].add(profiler.context)


def literal(value):
    typ = type(value)
    if value is None:
        return "N"
    if typ is bool:
        return "T" if value else "F"
    if typ is int and value.bit_length() <= 40:
        return "i" + str(value)
    if typ is str and len(value) <= 45:
        return "s" + value
    if typ is float and math.isfinite(value):
        return "f" + repr(value)
    if (
        typ is tuple
        and len(value) <= 5
        and all(type(i) is int and i.bit_length() <= 26 for i in value)
    ):
        return "t" + ",".join(map(str, value))
    return "?"


def operand(site, value):
    record(site, literal(value))
    return value


def sequence(site, seq):
    if type(seq) not in (list, tuple) or len(seq) > 12:
        record(site, "?")
        return seq
    ids = {}
    data = []
    for obj in seq:
        idx = 0 if obj is None else ids.setdefault(id(obj), len(ids) + 1)
        data.append(chr(65 + idx))
    record(site, "q" + "".join(data))
    return seq


def truth(obj):
    if obj is UNKNOWN:
        return "?"
    if (
        type(obj) in (str, int, float, bool, list, tuple, dict, set, frozenset, bytes)
        or obj is None
    ):
        return "1" if obj else "0"
    if (
        type(type(obj)) is type
        and not hasattr(type(obj), "__bool__")
        and not hasattr(type(obj), "__len__")
    ):
        return "1"
    return "?"


def read_atom(spec, env):
    if spec[0] == "const":
        return spec[1]
    value = env.get(spec[1], UNKNOWN)
    if spec[0] == "name":
        return value
    if value is UNKNOWN:
        return UNKNOWN
    if spec[0] == "isnone":
        return value is None
    if spec[0] == "notnone":
        return value is not None
    if spec[0] == "not":
        val = truth(value)
        return UNKNOWN if val == "?" else val == "0"
    if spec[0] == "len" and type(value) in (
        str,
        list,
        tuple,
        dict,
        set,
        frozenset,
        bytes,
    ):
        return len(value)
    return UNKNOWN


def boolean(site, first, tail):
    frame = sys._getframe(1)
    env = frame.f_locals
    if any(a[0] == "len" for a in tail) and (
        env.get("len", len) is not len or frame.f_globals.get("len", len) is not len
    ):
        record(site, "?")
        return first
    vector = truth(first) + "".join(truth(read_atom(a, env)) for a in tail)
    record(site, "?" if "?" in vector else "b" + vector)
    return first


def atom(expr):
    if isinstance(expr, ast.Name):
        return ("name", expr.id)
    if isinstance(expr, ast.Constant) and (
        type(expr.value) in (bool, int, str) or expr.value is None
    ):
        return ("const", expr.value)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "len"
        and len(expr.args) == 1
        and isinstance(expr.args[0], ast.Name)
        and not expr.keywords
    ):
        return ("len", expr.args[0].id)
    if (
        isinstance(expr, ast.UnaryOp)
        and isinstance(expr.op, ast.Not)
        and isinstance(expr.operand, ast.Name)
    ):
        return ("not", expr.operand.id)
    if (
        isinstance(expr, ast.Compare)
        and len(expr.ops) == 1
        and isinstance(expr.ops[0], (ast.Is, ast.IsNot))
        and isinstance(expr.left, ast.Name)
        and isinstance(expr.comparators[0], ast.Constant)
        and expr.comparators[0].value is None
    ):
        return (
            "isnone" if isinstance(expr.ops[0], ast.Is) else "notnone",
            expr.left.id,
        )
    return None


def supported_literal(node):
    try:
        return literal(ast.literal_eval(node)) != "?"
    except (ValueError, TypeError):
        return False


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def wrap(self, label, key, expr, extra=()):
        return ast.copy_location(
            ast.Call(
                ast.Name("_brv_" + label, ast.Load()),
                [ast.Constant(key), expr, *map(ast.Constant, extra)],
                [],
            ),
            expr,
        )

    def site(self, kind, node):
        return (
            "v:"
            + self.path
            + ":"
            + str(node.lineno)
            + ":"
            + str(node.col_offset)
            + ":"
            + kind
        )

    def visit_Compare(self, node):
        self.generic_visit(node)
        if (
            len(node.ops) == 1
            and isinstance(
                node.ops[0],
                (
                    ast.Lt,
                    ast.LtE,
                    ast.Gt,
                    ast.GtE,
                    ast.Eq,
                    ast.NotEq,
                    ast.Is,
                    ast.IsNot,
                ),
            )
            and supported_literal(node.comparators[0])
        ):
            node.left = self.wrap("operand", self.site("cmp", node), node.left)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "seq_get"
            and len(node.args) == 2
            and not node.keywords
            and supported_literal(node.args[1])
        ):
            node.args[0] = self.wrap("seq", self.site("seq", node), node.args[0])
        return node

    def visit_BoolOp(self, node):
        tail = tuple(map(atom, node.values[1:]))
        self.generic_visit(node)
        if all(tail):
            node.values[0] = self.wrap(
                "bool", self.site("bool", node), node.values[0], (tail,)
            )
        return node


def install(root):
    profiler.install(root, [], lambda p: Instrument(p.relative_to(root).as_posix()))
    builtins._brv_operand = operand
    builtins._brv_seq = sequence
    builtins._brv_bool = boolean


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profiler.main(install, ("v:",))
