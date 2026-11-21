"""Record entries into original compound-statement suites and lazy expression operands."""

import ast
import builtins
import os
from pathlib import Path

import build_options as profile


def observe(key):
    profile.observations[key].add(profile.context)


class Gates(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path
        self.inside = False

    def call(self, node, field):
        key = f"gate:{self.path}:{node.lineno}:{node.col_offset}:{field}"
        # A marker in the compiled original function with zero observations is
        # distinct from an uninstrumented module. Keep declarations explicitly.
        profile.observations["decl:" + key].add("")
        return ast.Call(ast.Name("_br_gate_entry", ast.Load()), [ast.Constant(key)], [])

    def visit_FunctionDef(self, node):
        prev = self.inside
        self.inside = True
        node.body = [self.visit(stmt) for stmt in node.body]
        self.inside = prev
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        prev = self.inside
        self.inside = False
        self.generic_visit(node)
        self.inside = prev
        return node

    def suite(self, node):
        self.generic_visit(node)
        if self.inside:
            for field in ("body", "orelse", "finalbody"):
                body = getattr(node, field, None)
                if body:
                    marker = ast.copy_location(
                        ast.Expr(self.call(node, field)), body[0]
                    )
                    body.insert(0, marker)
        return node

    visit_If = visit_While = visit_For = visit_AsyncFor = suite
    visit_With = visit_AsyncWith = visit_Try = visit_TryStar = visit_ExceptHandler = (
        suite
    )

    def entry_expr(self, parent, field, value):
        # observe returns None. In Boolean contexts this preserves the original
        # short-circuit jumps, including custom truth conversions.
        return ast.copy_location(
            ast.BoolOp(ast.Or(), [self.call(parent, field), value]), value
        )

    def visit_IfExp(self, node):
        self.generic_visit(node)
        if self.inside:
            for attr in ("body", "orelse"):
                setattr(node, attr, self.entry_expr(node, attr, getattr(node, attr)))
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self.inside:
            for i in range(1, len(node.values)):
                node.values[i] = self.entry_expr(node, f"arg{i}", node.values[i])
        return node


def install(root):
    def transform(path):
        return Gates(path.relative_to(root).as_posix())

    profile.install(root, (), transform)
    builtins._br_gate_entry = observe


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profile.main(install, ("gate:", "decl:"))
