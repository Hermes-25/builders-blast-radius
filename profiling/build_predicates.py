"""Capture baseline branch inputs whose values can be observed without invoking user callbacks."""

import ast
import builtins
from functools import lru_cache
import inspect
import operator
import os
from pathlib import Path
import sys
import textwrap
import types

import build_options as profiler
import build_values as base

UNKNOWN = base.UNKNOWN
OPS = {"Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
CMP = {
    "Lt": operator.lt,
    "LtE": operator.le,
    "Gt": operator.gt,
    "GtE": operator.ge,
    "Eq": operator.eq,
    "NotEq": operator.ne,
    "Is": operator.is_,
    "IsNot": operator.is_not,
}


def primitive(x):
    return (
        type(x) in (bool, int, float, str, bytes)
        or x is None
        or type(x) is tuple
        and len(x) <= 7
        and all(type(i) in (bool, int, float, str) for i in x)
    )


def relation(site, op, a, b):
    condition = "?"
    if primitive(a) and primitive(b):
        try:
            condition = "r" + (
                "<" if a < b else ">" if a > b else "=" if a == b else "!"
            )
        except TypeError:
            pass
    base.record(site, condition)
    return OPS[op](a, b)


def spec(n):
    if isinstance(n, ast.Name):
        return ("name", n.id)
    if isinstance(n, ast.Constant) and type(n.value) in (
        type(None),
        str,
        int,
        bool,
        float,
    ):
        return ("const", n.value)
    if isinstance(n, (ast.List, ast.Tuple)) and len(n.elts) < 8:
        values = tuple(map(spec, n.elts))
        if all(values):
            return ("list" if isinstance(n, ast.List) else "tuple", *values)
    if isinstance(n, ast.Attribute):
        a = spec(n.value)
        if a:
            return ("attr", a, n.attr)
    if isinstance(n, ast.Subscript):
        a, b = spec(n.value), spec(n.slice)
        if a and b:
            return ("item", a, b)
    if isinstance(n, ast.Call) and not n.keywords:
        args = tuple(map(spec, n.args))
        if all(args):
            if isinstance(n.func, ast.Name) and (
                n.func.id in ("len", "bool")
                and len(args) == 1
                or n.func.id == "isinstance"
                and len(args) == 2
            ):
                return ("call", n.func.id, *args)
            if (
                isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and len(args) in (1, 2)
            ):
                self = spec(n.func.value)
                if self:
                    return ("get", self, *args)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        v = spec(n.operand)
        if v:
            return ("not", v)
    if isinstance(n, ast.Compare) and len(n.ops) == 1:
        a, b = spec(n.left), spec(n.comparators[0])
        op = type(n.ops[0]).__name__
        if a and b and op in CMP:
            return ("cmp", op, a, b)
    if isinstance(n, ast.BoolOp):
        values = tuple(map(spec, n.values))
        if all(values):
            return ("and" if isinstance(n.op, ast.And) else "or", *values)
    return None


@lru_cache(maxsize=1024)
def property_return(prop):
    fn = prop.fget
    if not fn or not fn.__module__.startswith("sqlglot."):
        return None
    try:
        node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        body = [
            s
            for s in node.body
            if not (
                isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Constant)
                and isinstance(s.value.value, str)
            )
        ]
        if (
            len(body) == 1
            and isinstance(body[0], ast.Return)
            and len(node.args.args) == 1
        ):
            return node.args.args[0].arg, spec(body[0].value)
    except (TypeError, ValueError, OSError, SyntaxError, AttributeError):
        pass
    return None


def look(name, local, glob):
    if name in local:
        return local[name]
    if name in glob:
        return glob[name]
    return vars(builtins).get(name, UNKNOWN)


def field(obj, name, depth):
    if obj is UNKNOWN:
        return UNKNOWN
    if type(obj) is types.ModuleType:
        return vars(obj).get(name, UNKNOWN)
    cls = type(obj)
    if cls.__getattribute__ is not object.__getattribute__:
        return UNKNOWN
    entry = next((vars(c)[name] for c in cls.__mro__ if name in vars(c)), UNKNOWN)
    if type(entry) is types.MemberDescriptorType:
        try:
            return entry.__get__(obj, cls)
        except AttributeError:
            return UNKNOWN
    if type(entry) is property:
        result = property_return(entry)
        if result and result[1] and depth < 5:
            return resolve(
                result[1], {result[0]: obj}, entry.fget.__globals__, depth + 1
            )
        return UNKNOWN
    if entry is not UNKNOWN and hasattr(type(entry), "__get__"):
        return UNKNOWN
    # Plain instance dictionary retrieval is safe for ordinary project classes.
    if any(
        "__dict__" in vars(c)
        and type(vars(c)["__dict__"]) is types.GetSetDescriptorType
        for c in cls.__mro__
    ):
        vals = object.__getattribute__(obj, "__dict__")
        if type(vals) is dict and name in vals:
            return vals[name]
    return entry


def resolve(k, local, glob, depth=0):
    kind = k[0]
    if kind == "name":
        return look(k[1], local, glob)
    if kind == "const":
        return k[1]
    if kind == "attr":
        return field(resolve(k[1], local, glob, depth), k[2], depth)
    if kind in ("tuple", "list"):
        out = [resolve(v, local, glob, depth) for v in k[1:]]
        if any(v is UNKNOWN for v in out):
            return UNKNOWN
        return tuple(out) if kind == "tuple" else out
    if kind in ("and", "or"):
        for v in k[1:]:
            x = resolve(v, local, glob, depth)
            t = base.truth(x)
            if t == "?":
                return UNKNOWN
            if t == ("0" if kind == "and" else "1"):
                return x
        return x
    if kind == "not":
        val = base.truth(resolve(k[1], local, glob, depth))
        return UNKNOWN if val == "?" else val == "0"
    if kind == "call":
        name = k[1]
        if look(name, local, glob) is not getattr(builtins, name):
            return UNKNOWN
        v = resolve(k[2], local, glob, depth)
        if v is UNKNOWN:
            return UNKNOWN
        if name == "len":
            return (
                len(v)
                if type(v) in (str, bytes, list, tuple, dict, set, frozenset)
                else UNKNOWN
            )
        if name == "bool":
            t = base.truth(v)
            return UNKNOWN if t == "?" else t == "1"
        typ = resolve(k[3], local, glob, depth)
        classes = typ if type(typ) is tuple else (typ,)
        if all(
            isinstance(c, type) and type(c).__instancecheck__ is type.__instancecheck__
            for c in classes
        ):
            return isinstance(v, typ)
        return UNKNOWN
    if kind == "cmp":
        a, b = resolve(k[2], local, glob, depth), resolve(k[3], local, glob, depth)
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        if k[1] in ("Is", "IsNot") or primitive(a) and primitive(b):
            try:
                return CMP[k[1]](a, b)
            except (TypeError, ValueError, OverflowError):
                return UNKNOWN
        return UNKNOWN
    if kind in ("get", "item"):
        obj = resolve(k[1], local, glob, depth)
        key = resolve(k[2], local, glob, depth)
        if obj is UNKNOWN or key is UNKNOWN:
            return UNKNOWN
        default = None if len(k) == 3 else resolve(k[3], local, glob, depth)
        if default is UNKNOWN:
            return UNKNOWN
        if (
            type(obj) is dict
            and type(key) in (str, int)
            and all(type(i) in (str, int) for i in obj)
        ):
            if kind == "get":
                return obj.get(key, default)
            return obj.get(key, UNKNOWN)
        if (
            kind == "item"
            and type(obj) in (tuple, list, str, bytes)
            and type(key) is int
            and -len(obj) <= key < len(obj)
        ):
            return obj[key]
    return UNKNOWN


def boolean(site, first, tail):
    frame = sys._getframe(1)
    env = frame.f_locals
    try:
        # A local name missing in this invocation may raise at its original
        # LOAD_FAST instruction even when the module exports a namesake.
        unbound = {n: UNKNOWN for n in local_names(frame.f_code) if n not in env}
        if unbound:
            env = env | unbound
        vector = base.truth(first) + "".join(
            base.truth(resolve(k, env, frame.f_globals)) for k in tail
        )
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        vector = "?"
    base.record(site, "?" if "?" in vector else "b" + vector)
    return first


@lru_cache(maxsize=None)
def local_names(code):
    return set(code.co_varnames + code.co_cellvars + code.co_freevars)


class Instrument(ast.NodeTransformer):
    def __init__(self, p):
        self.path = p

    def site(self, k, n):
        return (
            "v:" + self.path + ":" + str(n.lineno) + ":" + str(n.col_offset) + ":" + k
        )

    def visit_Compare(self, n):
        self.generic_visit(n)
        if (
            len(n.ops) == 1
            and type(n.ops[0]).__name__ in OPS
            and not base.supported_literal(n.comparators[0])
        ):
            return ast.copy_location(
                ast.Call(
                    ast.Name("_brp_rel", ast.Load()),
                    [
                        ast.Constant(self.site("rel", n)),
                        ast.Constant(type(n.ops[0]).__name__),
                        n.left,
                        n.comparators[0],
                    ],
                    [],
                ),
                n,
            )
        return n

    def visit_BoolOp(self, n):
        tail = tuple(map(spec, n.values[1:]))
        simple = all(base.atom(v) for v in n.values[1:])
        self.generic_visit(n)
        if all(tail) and not simple:
            expr = n.values[0]
            n.values[0] = ast.copy_location(
                ast.Call(
                    ast.Name("_brp_bool", ast.Load()),
                    [ast.Constant(self.site("extended", n)), expr, ast.Constant(tail)],
                    [],
                ),
                expr,
            )
        return n


def install(root):
    profiler.install(root, [], lambda p: Instrument(p.relative_to(root).as_posix()))
    builtins._brp_rel = relation
    builtins._brp_bool = boolean


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())
if __name__ == "__main__":
    profiler.main(install, ("v:",))
