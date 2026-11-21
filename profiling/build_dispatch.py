"""Record class-option reads and callback invocations, preserving the original callback object."""
import ast
import builtins
import os
from pathlib import Path

import build_options as profiler


def invoked(key):
    profiler.observations[key].add(profiler.context)


class Instrument(profiler.Instrument):
    def __init__(self, path):
        self.path = path

    def visit_Lambda(self, node):
        self.generic_visit(node)
        key = f"lambda:{self.path}:{node.lineno}"
        event = ast.Call(ast.Name("_br_invoked", ast.Load()), [ast.Constant(key)], [])
        result = ast.Tuple([event, node.body], ast.Load())
        body = ast.Subscript(result, ast.Constant(1), ast.Load())
        node.body = ast.copy_location(body, node.body)
        return node


def install(root):
    files = (root / "sqlglot").rglob("*.py")

    def transform(path):
        return Instrument(path.relative_to(root).as_posix())

    profiler.install(root, files, transform, scalar_only=True)
    builtins._br_invoked = invoked


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profiler.main(install, ("sqlglot.", "lambda:"))
