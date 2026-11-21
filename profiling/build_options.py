"""Record consumers of literal SQLGlot dialect options on the pinned base revision."""

import argparse
import ast
import builtins
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
import gzip
import importlib.machinery
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

observations = defaultdict(set)
context = ""
root_pid = os.getpid()
options = set()


@lru_cache(None)
def origin(cls, attr):
    return next(
        (
            base.__module__ + "." + base.__qualname__ + "." + attr
            for base in cls.__mro__
            if attr in vars(base)
        ),
        None,
    )


def observe(obj, attr):
    cls = obj if isinstance(obj, type) else type(obj)
    if not cls.__module__.startswith("sqlglot."):
        return
    key = origin(cls, attr)
    if key:
        observations[key].add(context)


def read(obj, attr):
    observe(obj, attr)
    return getattr(obj, attr)


def lookup(obj, attr, *defaults):
    if isinstance(attr, str) and attr in options:
        observe(obj, attr)
    return getattr(obj, attr, *defaults)


class Instrument(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in options:
            return ast.copy_location(
                ast.Call(
                    ast.Name("_br_option", ast.Load()),
                    [node.value, ast.Constant(node.attr)],
                    [],
                ),
                node,
            )
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            node.func.id = "_br_lookup"
        return node


def flush():
    path = Path(os.environ["BR_OPTIONS_DIR"]) / f"{os.getpid()}.json"
    path.write_text(json.dumps({k: sorted(v) for k, v in observations.items()}))


def child(ctx, fn, args, kwargs):
    global context
    if os.getpid() != int(os.environ["BR_PARENT_PID"]) and not getattr(
        child, "started", False
    ):
        Finalize(None, flush, exitpriority=9)
        child.started = True
    context = ctx
    try:
        return fn(*args, **kwargs)
    finally:
        context = ""


class Plugin:
    def __init__(self):
        self.passed = set()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        global context
        context = item.nodeid
        try:
            return (yield)
        finally:
            context = ""

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)


def install(root):
    for f in (root / "sqlglot/dialects").glob("*.py"):
        path = f.relative_to(root).as_posix()
        for cls in ast.walk(ast.parse(f.read_bytes())):
            if not isinstance(cls, ast.ClassDef):
                continue
            for s in cls.body:
                if not (
                    isinstance(s, ast.Assign)
                    and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name)
                    and s.targets[0].id.isupper()
                ):
                    continue
                try:
                    ast.literal_eval(s.value)
                except (ValueError, TypeError):
                    continue
                options.add(s.targets[0].id)
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader
    original = loader.get_code

    def get_code(self, name):
        path = Path(self.path)
        if name == "sqlglot" or name.startswith("sqlglot."):
            tree = Instrument().visit(ast.parse(path.read_bytes(), filename=str(path)))
            return compile(
                ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True
            )
        return original(self, name)

    loader.get_code = get_code
    builtins._br_option = read
    builtins._br_lookup = lookup


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkout", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--tests", nargs="+", default=["tests"])
    args = p.parse_args()
    root = args.checkout.resolve()
    out = args.out.resolve()
    rev = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if rev != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(
        ["git", "-C", str(root), "diff", "HEAD", "--name-only"]
    ):
        raise ValueError("Expected unmodified base revision")
    os.environ["BR_ROOT"] = str(root)
    os.environ["BR_PARENT_PID"] = str(os.getpid())
    install(root)
    submit = ProcessPoolExecutor.submit

    def tracked(pool, fn, /, *args, **kwargs):
        return submit(pool, child, context, fn, args, kwargs)

    ProcessPoolExecutor.submit = tracked
    plugin = Plugin()
    os.environ.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", SKIP_INTEGRATION="0")
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["BR_OPTIONS_DIR"] = scratch
        from sqlglot.dialects.dialect import Dialect

        list(Dialect.classes.values())
        result = pytest.main(
            ["-q", "-p", "no:cacheprovider", "--import-mode=prepend", *args.tests],
            plugins=[plugin],
        )
        flush()
        merged = defaultdict(set)
        for part in Path(scratch).glob("*.json"):
            for key, ids in json.loads(part.read_text()).items():
                merged[key].update(ids)
        tests = sorted(plugin.passed)
        slots = {node: i for i, node in enumerate(tests)}
        vals = {
            key: sorted(slots[node] for node in names if node in slots)
            for key, names in merged.items()
            if "" not in names and key.startswith("sqlglot.dialects.")
        }
        with gzip.GzipFile(str(out), "wb", mtime=0) as out_file:
            out_file.write(
                json.dumps(
                    {
                        "tests": tests,
                        "owners": vals,
                        "startup": sorted(
                            k
                            for k, names in merged.items()
                            if "" in names and k.startswith("sqlglot.dialects.")
                        ),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
        print(
            f"Passed {len(tests)} baseline tests and found {len(vals)} observed options without startup reads."
        )
        sys.exit(result)


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))
    from sqlglot.dialects.dialect import Dialect

    list(Dialect.classes.values())

if __name__ == "__main__":
    main()
