"""Profile operand types and class declaration consumers on the unchanged checkout."""

import argparse
import ast
import builtins
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from enum import Enum
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
import types

import pytest


def qualified(cls):
    return cls.__module__ + "." + cls.__qualname__


@lru_cache(None)
def category(cls):
    if cls is type(None):
        return "None"
    if cls in (bool, int, str, float, bytes, tuple, list, dict, set, frozenset):
        return "builtin"
    if cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
        return "identity"
    if issubclass(cls, Enum) and (
        cls.__eq__, cls.__ne__
    ) in {(str.__eq__, str.__ne__), (int.__eq__, int.__ne__)}:
        return "enum:" + qualified(cls)
    return "other"


types_seen = defaultdict(set)
owners = defaultdict(set)
sites = set()
node_id = ""
parent_pid = os.getpid()


def operand(key, value):
    types_seen[key].add(category(type(value)))
    return value


def declare(key, callback):
    # A callback produced in a table retains the same dependencies when
    # copied to an inherited table during module initialization.
    if not isinstance(callback, types.FunctionType):
        return callback
    sites.add(key)
    def called(*args, **kwargs):
        owners[key].add(node_id)
        return callback(*args, **kwargs)
    return called


@lru_cache(None)
def arg_owner(cls):
    if not isinstance(cls, type):
        return ""
    return next((qualified(base) for base in cls.__mro__ if "arg_types" in vars(base)), "")


def class_value(obj, name):
    cls = obj if isinstance(obj, type) else type(obj)
    key = arg_owner(cls)
    if key:
        owners["schema:" + key].add(node_id)
    return getattr(obj, name)


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.Is, ast.NotEq, ast.IsNot)):
            return node
        right = node.comparators[0]
        if not (isinstance(right, ast.Constant) and right.value is None
                or isinstance(right, ast.Attribute) and right.attr.isupper()):
            return node
        key = f"{self.path}:{node.lineno}:{node.col_offset}"
        for suffix, expr in (("L", node.left), ("R", right)):
            call = ast.copy_location(ast.Call(ast.Name("_br_operand", ast.Load()), [ast.Constant(key + suffix), expr], []), expr)
            if suffix == "L":
                node.left = call
            else:
                node.comparators[0] = call
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in {"arg_types", "required_args"}:
            return ast.copy_location(ast.Call(ast.Name("_br_class_value", ast.Load()), [node.value, ast.Constant(node.attr)], []), node)
        return node

    def visit_Dict(self, node):
        self.generic_visit(node)
        for i, (key, val) in enumerate(zip(node.keys, node.values)):
            if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                and isinstance(val, ast.Call) and isinstance(val.func, ast.Name)):
                ident = f"{self.path}:{val.lineno}:{val.col_offset}"
                node.values[i] = ast.copy_location(ast.Call(ast.Name("_br_declare", ast.Load()), [ast.Constant(ident), val], []), val)
        return node


def flush():
    output = Path(os.environ["BR_SCRATCH"]) / f"{os.getpid()}.json"
    output.write_text(json.dumps([{k: list(v) for k, v in types_seen.items()},
                                  {k: list(v) for k, v in owners.items()}, list(sites)]))


def run_child(context, fn, args, kwargs):
    global node_id
    if os.getpid() != int(os.environ["BR_PARENT_PID"]) and not getattr(run_child, "started", False):
        Finalize(None, flush, exitpriority=9)
        run_child.started = True
    node_id = context
    try:
        return fn(*args, **kwargs)
    finally:
        node_id = ""


class Plugin:
    def __init__(self):
        self.passed = set()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        global node_id
        node_id = item.nodeid
        try:
            return (yield)
        finally:
            node_id = ""

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)


def install(root):
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader
    original = loader.get_code
    def get_code(self, name):
        path = Path(self.path)
        if name == "sqlglot" or name.startswith("sqlglot."):
            src = path.read_bytes()
            tree = Instrument(path.relative_to(root).as_posix()).visit(ast.parse(src, filename=str(path)))
            return compile(ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True)
        return original(self, name)
    loader.get_code = get_code
    builtins._br_operand = operand
    builtins._br_declare = declare
    builtins._br_class_value = class_value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tests", nargs="+", default=["tests"])
    args = parser.parse_args()
    root = args.checkout.resolve()
    output = args.output.resolve()
    rev = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if rev != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(["git","-C",str(root),"diff","HEAD","--name-only"]):
        raise ValueError("The checkout must be clean and pinned")
    os.environ["BR_ROOT"] = str(root)
    os.environ["BR_PARENT_PID"] = str(os.getpid())
    install(root)
    submit = ProcessPoolExecutor.submit
    def tracked(pool, fn, /, *args, **kwargs):
        return submit(pool, run_child, node_id, fn, args, kwargs)
    ProcessPoolExecutor.submit = tracked
    plugin = Plugin()
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["BR_SCRATCH"] = scratch
        result = pytest.main(["-q", "-p", "no:cacheprovider", "--import-mode=prepend", *args.tests], plugins=[plugin])
        flush()
        joined_types, joined_owners, declared = defaultdict(set), defaultdict(set), set()
        for part in Path(scratch).glob("*.json"):
            ts, own, keys = json.loads(part.read_text())
            for k, vs in ts.items(): joined_types[k].update(vs)
            for k, vs in own.items(): joined_owners[k].update(vs)
            declared.update(keys)
        ordered = sorted(plugin.passed)
        slots = {test:i for i,test in enumerate(ordered)}
        labels = {key: sorted(slots[t] for t in tests if t in slots) for key,tests in joined_owners.items()}
        with gzip.GzipFile(str(output), "wb", mtime=0) as stream:
            stream.write(json.dumps({"tests":ordered, "types":{k:sorted(v) for k,v in joined_types.items()},
                "owners":labels, "declared":sorted(declared),"startup":[k for k,vs in joined_owners.items() if "" in vs and not k.startswith("schema:")]},
                separators=(",",":"), sort_keys=True).encode())
        print(f"Profiled {len(ordered)} passing tests, {len(joined_types)} operand sites and {len(joined_owners)} class/table consumers; pytest exit {result}")
        sys.exit(result)


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    install(Path(os.environ["BR_ROOT"]))


if __name__ == "__main__":
    main()
