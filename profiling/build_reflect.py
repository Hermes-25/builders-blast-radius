"""Attribute explicit-object namespace inspection to its actual target type."""

import ast
import builtins
from pathlib import Path
import os
import build_options as profiler


def reflect(site, fn, obj):
    # For a class whose metaclass is exactly the built-in type, both
    # operations inspect the class namespace; they cannot enumerate globals.
    mode = "class" if fn in (dir, vars) and type(obj) is type else "unknown"
    prefix = "r:" if profiler.context else "s:"
    profiler.observations[prefix + site + "|" + mode].add(profiler.context)
    return fn(obj)


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Call(self, node):
        self.generic_visit(node)
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id not in ("dir", "vars")
            or len(node.args) != 1
            or node.keywords
            or isinstance(node.args[0], ast.Starred)
        ):
            return node
        key = f"{self.path}:{node.lineno}:{node.col_offset}"
        return ast.copy_location(
            ast.Call(
                ast.Name("_br_reflect", ast.Load()),
                [ast.Constant(key), node.func, node.args[0]],
                [],
            ),
            node,
        )


def install(root):
    profiler.install(
        root, [], lambda path: Instrument(path.relative_to(root).as_posix())
    )
    builtins._br_reflect = reflect


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profiler.main(install, ("r:", "s:"))
