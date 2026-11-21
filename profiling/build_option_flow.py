"""Trace configuration fields after they are forwarded through the tokenizer constructor."""

import ast
import builtins
import os
from pathlib import Path
import build_options as profiler

keys = {}
transfers = set()


def scan(root):
    factory = ast.parse((root / "sqlglot/tokens.py").read_bytes())
    core = ast.parse((root / "sqlglot/tokenizer_core.py").read_bytes())
    cls = next(
        n
        for n in core.body
        if isinstance(n, ast.ClassDef) and n.name == "TokenizerCore"
    )
    ctor = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    for call in ast.walk(factory):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == cls.name
        ):
            continue
        for kw in call.keywords:
            val = kw.value
            if not (kw.arg and isinstance(val, ast.Attribute) and val.attr.isupper()):
                continue
            param = kw.arg
            refs = [
                node
                for node in ast.walk(ctor)
                if isinstance(node, ast.Name)
                and node.id == param
                and isinstance(node.ctx, ast.Load)
            ]
            if len(refs) != 1:
                continue
            stores = [
                n
                for n in ctor.body
                if isinstance(n, ast.Assign)
                and n.value is refs[0]
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
            ]
            if len(stores) != 1:
                continue
            field = stores[0].targets[0]
            if not (isinstance(field.value, ast.Name) and field.value.id == "self"):
                continue
            keys[val.attr] = val.attr
            keys[field.attr] = val.attr
            transfers.add((val.lineno, val.col_offset))


def record(name, value):
    profiler.observations["flow:" + name].add(profiler.context)
    return value


def lookup(obj, name, *defaults):
    if type(name) is str and name in keys:
        record(keys[name], None)
    return getattr(obj, name, *defaults)


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def wrap(self, name, node):
        return ast.copy_location(
            ast.Call(
                ast.Name("_br_flow_read", ast.Load()),
                [ast.Constant(keys[name]), node],
                [],
            ),
            node,
        )

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in keys:
            if (
                self.path == "sqlglot/tokens.py"
                and (node.lineno, node.col_offset) in transfers
            ):
                return node
            return self.wrap(node.attr, node)
        return node

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id in keys and node.id.isupper():
            return self.wrap(node.id, node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            node.func.id = "_br_flow_lookup"
        return node


def install(root):
    scan(root)
    profiler.install(root, [], lambda p: Instrument(p.relative_to(root).as_posix()))
    builtins._br_flow_read = record
    builtins._br_flow_lookup = lookup


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profiler.main(install, ("flow:",))
