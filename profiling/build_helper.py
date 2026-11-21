"""Record original arguments and physical caller positions of small pure functions."""

import ast
import builtins
from collections import defaultdict
from functools import lru_cache
import json
import os
from pathlib import Path
import sys

import build_options as profiler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pure_contract import calls_in_source, signature

ROOT = ""
eligible = {}
distinct = defaultdict(set)


@lru_cache(maxsize=32000)
def caller_position(code, offset):
    filename = code.co_filename
    if not filename.startswith(ROOT):
        return "-"
    positions = list(code.co_positions())
    if not 0 <= offset // 2 < len(positions):
        return "-"
    line, _, col, _ = positions[offset // 2]
    return f"{filename[len(ROOT) :]}:{line}:{col}"


def encode(values):
    aliases = {}

    def opaque(v):
        return ("o", aliases.setdefault(id(v), len(aliases) + 1))

    def atom(v, nested=False):
        typ = type(v)
        if (
            v is None
            or typ is bool
            or typ is int
            and v.bit_length() <= 45
            or typ is str
            and len(v) <= 40
        ):
            return ("v", v)
        if not nested and typ in (tuple, list) and len(v) <= 12:
            return ("t" if typ is tuple else "l", tuple(atom(x, True) for x in v))
        if not nested and typ in (tuple, list):
            return ("T" if typ is tuple else "L", len(v))
        return opaque(v)

    return tuple(atom(v) for v in values)


@lru_cache(maxsize=100000)
def label(key, values):
    seen = distinct[key]
    if len(seen) >= 96 and values not in seen:
        return key + "|?"
    seen.add(values)
    return key + "|" + json.dumps(values, separators=(",", ":"))


def observe(fn, *args):
    caller = sys._getframe(2)
    site = caller_position(caller.f_code, caller.f_lasti)
    key = f"h:{fn}@{site}"
    profiler.observations[label(key, encode(args))].add(profiler.context)


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Module(self, node):
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            candidates = eligible.get((self.path, fn.name))
            if candidates is None or fn.lineno != candidates.lineno:
                continue
            key = self.path + ":" + fn.name
            expr = ast.Expr(
                ast.Call(
                    ast.Name("_br_helper_input", ast.Load()),
                    [ast.Constant(key)]
                    + [ast.Name(n, ast.Load()) for n in signature(fn)],
                    [],
                )
            )
            doc = ast.get_docstring(fn) is not None
            fn.body.insert(int(doc), ast.copy_location(expr, fn.body[int(doc)]))
        return node


def install(root):
    global ROOT, eligible
    ROOT = root.as_posix() + "/"
    eligible = calls_in_source(root)
    profiler.install(root, [], lambda p: Instrument(p.relative_to(root).as_posix()))
    builtins._br_helper_input = observe


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profiler.main(install, ("h:",))
