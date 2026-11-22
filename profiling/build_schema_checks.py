"""Trace required-field validation and independent metadata readers on the pinned suite."""

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


REVISION = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
rows = defaultdict(set)
current = ''


@lru_cache(None)
def origin(cls):
    for base in cls.__mro__:
        if 'arg_types' in vars(base) and base.__module__.startswith('sqlglot.expressions.'):
            return base.__module__ + '.' + base.__qualname__
    return None


def reflect(obj):
    cls = obj if isinstance(obj, type) else type(obj)
    name = origin(cls)
    if name:
        rows[name + '|*'].add(current)


def read(obj, attr):
    reflect(obj)
    return getattr(obj, attr)


def lookup(obj, attr, *default):
    if attr in ('arg_types', 'required_args', '__dict__'):
        reflect(obj)
    return getattr(obj, attr, *default)


def dictionary(obj):
    reflect(obj)
    return vars(obj)


def checked(obj):
    owner = origin(type(obj))
    if owner:
        required = obj.required_args
        missing_required = 0
        for k in obj.arg_types:
            val = obj.args.get(k)
            if val is None or (isinstance(val, list) and not val):
                rows[owner + '|' + k].add(current)
                missing_required += k in required
        if missing_required >= 2:
            rows[owner + '|*'].add(current)
    return obj.required_args


class Instrument(ast.NodeTransformer):
    def __init__(self, path, root):
        self.path = path
        self.fn = ''
        self.parents = {x: n for n in ast.walk(root) for x in ast.iter_child_nodes(n)}

    def visit_FunctionDef(self, node):
        old, self.fn = self.fn, node.name
        self.generic_visit(node)
        self.fn = old
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load) or node.attr not in {'arg_types','required_args','__dict__'}:
            return node
        parent = self.parents.get(node)
        if self.path == 'sqlglot/expressions/core.py' and self.fn == '__init_subclass__' and node.attr == 'arg_types':
            return node
        if self.path == 'sqlglot/expressions/core.py' and self.fn == 'error_messages' and node.attr == 'required_args':
            return ast.copy_location(ast.Call(ast.Name('_br_schema_checked', ast.Load()), [node.value], []), node)
        if node.attr == 'arg_types':
            if isinstance(parent, ast.For) and parent.iter is node:
                return node
            if isinstance(parent, ast.Compare) and node in parent.comparators and all(isinstance(op,(ast.In,ast.NotIn)) for op in parent.ops):
                return node
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in ('len','tuple','list','zip','set'):
                return node
        return ast.copy_location(ast.Call(ast.Name('_br_schema_read', ast.Load()), [node.value, ast.Constant(node.attr)], []), node)

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name):
            if node.func.id == 'getattr' and len(node.args) >= 2:
                node.func.id = '_br_schema_lookup'
            elif node.func.id == 'vars' and len(node.args) == 1:
                node.func.id = '_br_schema_vars'
        return node


def install(root):
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    builtins._br_schema_checked = checked
    builtins._br_schema_read = read
    builtins._br_schema_vars = dictionary
    builtins._br_schema_lookup = lookup
    loader = importlib.machinery.SourceFileLoader
    prior = loader.get_code

    def instrument(self, name):
        path = Path(self.path)
        if path.is_relative_to(root) and name.partition('.')[0] in ('tests', 'sqlglot'):
            tree = ast.parse(path.read_bytes(), filename=str(path))
            ast.fix_missing_locations(Instrument(path.relative_to(root).as_posix(), tree).visit(tree))
            return compile(tree, str(path), 'exec', dont_inherit=True)
        return prior(self, name)

    loader.get_code = instrument


def flush():
    out = Path(os.environ['BR_SCHEMA_SCRATCH']) / f'{os.getpid()}.json'
    out.write_text(json.dumps({k: sorted(tests) for k, tests in rows.items()}))


def child(context, fn, args, kwargs):
    global current
    if not getattr(child, 'ready', False):
        Finalize(None, flush, exitpriority=9)
        child.ready = True
    current = context
    try:
        return fn(*args, **kwargs)
    finally:
        current = ''


class Plugin:
    def __init__(self):
        self.passed = set()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        global current
        current = item.nodeid
        try:
            return (yield)
        finally:
            current = ''

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' and report.passed:
            self.passed.add(report.nodeid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('checkout', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--workers', type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    output = args.output.resolve()
    if args.archive:
        with args.archive.open('rb') as fh:
            revision = subprocess.check_output(['git','get-tar-commit-id'], stdin=fh, text=True).strip()
    else:
        revision = subprocess.check_output(['git','-C',root,'rev-parse','HEAD'], text=True).strip()
        if subprocess.check_output(['git','-C',root,'diff','HEAD','--name-only']):
            raise ValueError('Tracked changes in source checkout')
    if revision != REVISION:
        raise ValueError('Unexpected source revision')
    chosen = json.load(gzip.open(args.workers))['tests'] if args.workers else ['tests']
    os.environ.update(BR_SCHEMA_ROOT=str(root),SKIP_INTEGRATION='0', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    install(root)
    submit = ProcessPoolExecutor.submit

    def propagate(pool, fn, /, *args, **kwargs):
        return submit(pool, child, current, fn, args, kwargs)

    ProcessPoolExecutor.submit = propagate
    with tempfile.TemporaryDirectory() as temp:
        os.environ['BR_SCHEMA_SCRATCH'] = temp
        from sqlglot.dialects.dialect import Dialect
        list(Dialect.classes.values())
        plugin = Plugin()
        code = pytest.main(['-q','-p','no:cacheprovider','--import-mode=prepend', *chosen], plugins=[plugin])
        if code != 0:
            raise ValueError('One or more baseline tests failed')
        flush()
        joined = defaultdict(set)
        for part in Path(temp).glob('*.json'):
            for k, tests in json.loads(part.read_text()).items():
                joined[k].update(tests)
        tests = sorted(plugin.passed)
        ids = {t:i for i,t in enumerate(tests)}
        result = {'tests':tests,'rows':{k:sorted(ids[t] for t in rr if t in ids) for k,rr in joined.items()},'startup':sorted(k for k,rr in joined.items() if '' in rr)}
        with gzip.GzipFile(str(output), 'wb', mtime=0) as stream:
            stream.write(json.dumps(result,sort_keys=True,separators=(',',':')).encode())


if __name__ == '__mp_main__' and 'BR_SCHEMA_ROOT' in os.environ:
    install(Path(os.environ['BR_SCHEMA_ROOT']))

if __name__ == '__main__':
    main()
