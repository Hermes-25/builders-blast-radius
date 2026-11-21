"""Trace concrete reads of materialized SQLGlot parser registries on the pristine suite."""

import os
from pathlib import Path
import sys

import build_options as profile


def encode(key):
    if type(key) is str and len(key) <= 128:
        return "s:" + key
    if type(key).__name__ == "TokenType" and type(key).__module__.startswith(
        "sqlglot."
    ):
        return "t:" + key.name
    return "*"


class ParserMap(dict):
    def __init__(self, names, original):
        self.labels = tuple("pk:" + name + "|" for name in sorted(names))
        super().__init__(original)

    def event(self, kind, key="*"):
        for label in self.labels:
            profile.observations[label + kind + "|" + key].add(profile.context)

    def __getitem__(self, key):
        self.event("read", encode(key))
        return super().__getitem__(key)

    def get(self, key, *default):
        self.event("read", encode(key))
        return super().get(key, *default)

    def __contains__(self, key):
        self.event("has", encode(key))
        return super().__contains__(key)


def logged(method, event):
    def call(self, *args, **kwargs):
        self.event(event)
        return method(self, *args, **kwargs)

    return call


for name in ("keys", "__iter__", "__reversed__", "__len__"):
    setattr(ParserMap, name, logged(getattr(dict, name), "has"))
for name in (
    "items",
    "values",
    "copy",
    "__or__",
    "__ror__",
    "__eq__",
    "__ne__",
    "__repr__",
    "__reduce_ex__",
):
    setattr(ParserMap, name, logged(getattr(dict, name), "read"))
for name in (
    "update",
    "clear",
    "pop",
    "popitem",
    "setdefault",
    "__setitem__",
    "__delitem__",
    "__ior__",
):
    setattr(ParserMap, name, logged(getattr(dict, name), "mutate"))


def install(root):
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    from sqlglot import Parser
    from sqlglot.dialects.dialect import Dialect

    bases = {
        base
        for dialect in Dialect.classes.values()
        for base in dialect.parser_class.__mro__
        if issubclass(base, Parser)
    }
    owners = {}
    originals = {}
    for base in bases:
        for name, value in vars(base).items():
            if not name.isupper() or type(value) is not dict or not value:
                continue
            if any(encode(key) == "*" for key in value):
                continue
            originals[id(value)] = value
            owners.setdefault(id(value), []).append((base, name))
    for ident, definitions in owners.items():
        names = {name for _, name in definitions}
        wrapped = ParserMap(names, originals[ident])
        for base, name in definitions:
            profile.observations["decl:pk:" + name].add("")
            setattr(base, name, wrapped)


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))

if __name__ == "__main__":
    profile.main(install, ("pk:", "decl:"))
