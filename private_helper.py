"""Cold-check private helper additions and removals for a long isolated parser test."""

import ast
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from replay import mirror


def direct_functions(tree):
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def footprint(original, updated, bodies):
    old, new = (ast.parse(t, type_comments=True) for t in (original, updated))
    a, b = direct_functions(old), direct_functions(new)
    names = a.keys() ^ b.keys()
    if not 1 <= len(names) <= 3 or any(
        not n.startswith("_") or n.startswith("__") for n in names
    ):
        return None
    spans = []
    for tree, funcs in ((old, a), (new, b)):
        if not any(
            isinstance(n, ast.ImportFrom)
            and n.module == "__future__"
            and any(x.name == "annotations" for x in n.names)
            for n in tree.body
        ):
            return None
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in names:
                parent = parents.get(node)
                if not isinstance(parent, ast.Call) or parent.func is not node:
                    return None
                if tree is old:
                    spans.append((node.lineno, node.lineno))
            if (
                isinstance(node, ast.Attribute)
                and node.attr in names
                or isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in names
            ):
                return None
        for name in names & funcs.keys():
            fn = funcs[name]
            if (
                fn.decorator_list
                or fn.type_params
                or any(not isinstance(n, ast.Constant) for n in fn.args.defaults)
                or any(
                    n is not None and not isinstance(n, ast.Constant)
                    for n in fn.args.kw_defaults
                )
            ):
                return None
            if tree is old:
                spans.append((fn.body[0].lineno, fn.end_lineno))
            tree.body.remove(fn)
    old_bodies, new_bodies = bodies(), bodies()
    if ast.dump(old_bodies.visit(old)) != ast.dump(new_bodies.visit(new)):
        return None
    for (start, end, prior), (_, _, current) in zip(
        old_bodies.bodies, new_bodies.bodies, strict=True
    ):
        if prior != current:
            spans.append((start, end))
    return names, spans


def verify(text, selected, selector, parse, apply, bodies, root):
    target = selector.numbers.get("tests/test_parser.py::TestParser::test_max_nodes")
    if (
        target not in selected
        or target not in selector.isolated
        or selector.binding is None
    ):
        return None
    if not re.search(r"(?m)^[-+]def _[A-Za-z]", text):
        return None
    sections = parse(text)
    if len(sections) != 1:
        return None
    path, hunks = sections[0]
    if (
        not path.startswith("sqlglot/")
        or not path.endswith(".py")
        or path.endswith("/__init__.py")
    ):
        return None
    source = selector.sources[path]
    updated = apply(source, hunks)
    change = footprint(source, updated, bodies)
    if change is None:
        return None
    names, spans = change
    module = path[:-3].replace("/", ".")
    binding = selector.binding
    watched = names | {"__dict__", "__all__"}
    if (
        module not in binding.readers
        or any(module + ":" + n in binding.startup for n in watched)
        or names & set(binding.exports.get(module, ()))
        or target in selector.untraced_reads
        or target in selector.source_readers.get(path, ())
        or target in selector.class_fixtures.get(path, ())
    ):
        return None
    readings = binding.readers[module]
    if any(binding.ids[i] == target for n in watched for i in readings.get(n, ())):
        return None
    if any(
        any(name in other for name in names)
        for p, other in selector.sources.items()
        if p != path
    ):
        return None
    profile = selector.isolated[target]
    if any(a <= line <= b for line in profile["lines"].get(path, ()) for a, b in spans):
        return None
    with TemporaryDirectory(prefix="br-private-") as folder:
        project = Path(folder)
        mirror(root, project, {path: updated})
        outcome = project / "verified-import"
        code = (
            "import importlib,pathlib,sys;sys.path.insert(0,sys.argv[1]);importlib.import_module(sys.argv[2]);"
            "from tests.test_parser import TestParser;TestParser.setUpClass();TestParser.tearDownClass();pathlib.Path(sys.argv[3]).write_bytes(b'passed')"
        )
        run = subprocess.run(
            [sys.executable, "-B", "-c", code, str(project), module, str(outcome)],
            cwd=project,
            env=os.environ | {"PYTHONHASHSEED": "0", "PYTHONPATH": str(project)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        if (
            run.returncode == 0
            and outcome.is_file()
            and outcome.read_bytes() == b"passed"
        ):
            return target
    return None
