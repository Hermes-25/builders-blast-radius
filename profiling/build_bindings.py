"""Record external attribute access to pristine SQLGlot modules, including missing attributes."""

import ast
import importlib.machinery
import os
from pathlib import Path
import sys
from types import ModuleType

import build_options as profiler


class WatchedModule(ModuleType):
    def __getattribute__(self, name):
        module = ModuleType.__getattribute__(self, "__name__")
        # The loader obtains this dict exclusively to execute the module code.
        # In-module identifier reads are independently indexed from source.
        if name == "__dict__":
            frame = sys._getframe(1)
            if (
                frame.f_globals.get("__name__", "")
                in ("_frozen_importlib_external", "importlib._bootstrap_external")
                and frame.f_code.co_name == "exec_module"
            ):
                return ModuleType.__getattribute__(self, name)
        if name in ("__dict__", "__all__") or not name.startswith("__"):
            prefix = "r:" if profiler.context else "s:"
            key = prefix + module + ":" + name
            profiler.observations[key].add(profiler.context)
        return ModuleType.__getattribute__(self, name)

    def __dir__(self):
        name = ModuleType.__getattribute__(self, "__name__")
        profiler.observations[
            ("r:" if profiler.context else "s:") + name + ":__dict__"
        ].add(profiler.context)
        return ModuleType.__dir__(self)


def install(root):
    profiler.install(root, [], lambda path: ast.NodeTransformer())
    loader = importlib.machinery.SourceFileLoader
    original = loader.exec_module

    def exec_module(self, module):
        name = module.__name__
        if (name == "sqlglot" or name.startswith("sqlglot.")) and type(
            module
        ) is ModuleType:
            module.__class__ = WatchedModule
        return original(self, module)

    loader.exec_module = exec_module


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profiler.main(install, ("r:", "s:"))
