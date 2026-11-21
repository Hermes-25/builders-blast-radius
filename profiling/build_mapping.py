"""Observe keys whose values are read from literal class configuration dictionaries."""

import ast, builtins, importlib.machinery, json, os, sys
from pathlib import Path
import build_options as profiler


class ObservedDict(dict):
    def __init__(self, site, source):
        super().__init__(source)
        self.site = site
        for key in source:
            profiler.observations[site + "|" + json.dumps(key, ensure_ascii=True)]

    def record(self, suffix):
        try:
            site = self.site
        except AttributeError:
            return
        profiler.observations[site + "|" + suffix].add(profiler.context)

    def __getitem__(self, key):
        if isinstance(key, str):
            self.record(json.dumps(key, ensure_ascii=True))
        else:
            self.record("*")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if isinstance(key, str):
            self.record(json.dumps(key, ensure_ascii=True))
        else:
            self.record("*")
        return super().get(key, default)


def tracked(method, event):
    def called(self, *args, **kwargs):
        self.record(event)
        return method(self, *args, **kwargs)

    return called


for name in (
    "keys",
    "items",
    "values",
    "copy",
    "__iter__",
    "__reversed__",
    "__or__",
    "__ror__",
    "__eq__",
    "__ne__",
    "__repr__",
    "__reduce_ex__",
):
    setattr(ObservedDict, name, tracked(getattr(dict, name), "*"))
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
    setattr(ObservedDict, name, tracked(getattr(dict, name), "!"))


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        for item in node.body:
            if not isinstance(item, ast.Assign) or len(item.targets) != 1:
                continue
            target = item.targets[0]
            if (
                not isinstance(target, ast.Name)
                or not target.id.isupper()
                or not isinstance(item.value, ast.Dict)
            ):
                continue
            try:
                value = ast.literal_eval(item.value)
            except (ValueError, TypeError):
                continue
            if not value or len(value) > 1024 or not all(type(k) is str for k in value):
                continue
            site = f"mapping:{self.path}:{item.lineno}:{target.id}"
            call = ast.Call(
                ast.Name("_br_observed_dict", ast.Load()),
                [ast.Constant(site), item.value],
                [],
            )
            item.value = ast.copy_location(call, item.value)
        return node


def install(root):
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader
    previous = loader.get_code

    def get_code(self, fullname):
        filename = Path(self.path)
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            tree = Instrument(filename.relative_to(root).as_posix()).visit(
                ast.parse(filename.read_bytes(), str(filename))
            )
            return compile(
                ast.fix_missing_locations(tree),
                str(filename),
                "exec",
                dont_inherit=True,
            )
        return previous(self, fullname)

    loader.get_code = get_code
    builtins._br_observed_dict = ObservedDict


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    profiler.main(install, ("mapping:",))
