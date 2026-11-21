"""Build a source-line index from pristine control-flow markers and ordinary coverage."""

import ast
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from line_gate import SIMPLE, shape


def read(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


class Lines:
    def __init__(self, path, source, gates, cov, isolated):
        self.path = path
        self.source = source.splitlines()
        self.gates = gates
        self.cov = cov
        self.isolated = isolated
        self.rows = {}
        self.duplicates = set()

    def walk(self, node, guards=(), inside=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # An edit to a nested function can promote an enclosing local to a
            # closure cell before execution reaches the edited statement.
            if (
                inside
                or isinstance(node, ast.AsyncFunctionDef)
                or any(
                    isinstance(x, (ast.Await, ast.Yield, ast.YieldFrom))
                    for x in ast.walk(node)
                )
            ):
                return
            for stmt in node.body:
                self.walk(stmt, (), True)
            return
        if isinstance(node, ast.ClassDef):
            if inside:
                return
            for stmt in node.body:
                self.walk(stmt, (), False)
            return
        if inside and isinstance(node, SIMPLE):
            end = (
                node.test.end_lineno
                if isinstance(node, (ast.If, ast.While))
                else node.end_lineno
            )
            if node.lineno == end:
                self.add(node, guards)
        for name, field in ast.iter_fields(node):
            if isinstance(field, list):
                keys = guards
                if (
                    inside
                    and hasattr(node, "lineno")
                    and name in ("body", "orelse", "finalbody")
                ):
                    key = f"{self.path}:{node.lineno}:{node.col_offset}:{name}"
                    idx = self.gates["owners"].get(key)
                    if idx is not None:
                        keys = (*guards, idx)
                for child in field:
                    if isinstance(child, ast.AST):
                        self.walk(child, keys, inside)
            elif isinstance(field, ast.AST):
                self.walk(field, guards, inside)

    def add(self, node, guards):
        line = node.lineno
        if line in self.rows:
            self.duplicates.add(line)
            return
        if line in self.cov["startup"].get(self.path, ()):
            return
        try:
            if not shape(self.source[line - 1]):
                return
        except (SyntaxError, ValueError):
            return
        observed = set(self.cov["lines"].get(self.path, {}).get(str(line), ()))
        for number, rec in self.isolated.items():
            if line in rec["lines"].get(self.path, ()):
                observed.add(number)
        if not observed and not guards:
            return
        result = observed if observed else None
        for index in guards:
            values = self.gates["groups"][index]
            result = set(values) if result is None else result.intersection(values)
        if result is not None:
            self.rows[line] = tuple(sorted(result))


def main(gates_path, coverage_path, isolated_path, output):
    gates, cov, raw = map(read, (gates_path, coverage_path, isolated_path))
    if gates["tests"] != cov["tests"]:
        raise ValueError("Unaligned baseline test IDs")
    ids = {x: i for i, x in enumerate(gates["tests"])}
    isolated = {ids[node]: rec for node, rec in raw.items() if node in ids}
    groups = {}
    rows = {}
    for path, source in cov["sources"].items():
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            continue
        index = Lines(path, source, gates, cov, isolated)
        index.walk(ast.parse(source))
        sites = {
            line: groups.setdefault(group, len(groups))
            for line, group in index.rows.items()
            if line not in index.duplicates
        }
        if sites:
            rows[path] = sites
    result = {"tests": cov["tests"], "groups": list(groups), "lines": rows}
    content = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
    with open(output, "wb") as f:
        with gzip.GzipFile(filename="", fileobj=f, mode="wb", mtime=0) as z:
            z.write(content)
    print(
        sum(map(len, rows.values())),
        "proved source lines,",
        len(groups),
        "distinct dependencies",
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
