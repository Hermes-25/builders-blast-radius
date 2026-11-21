"""Merge test-attributed option and callback reads, including worker and test-source observations."""
import argparse
import ast
from collections import defaultdict
import gzip
import json
from pathlib import Path

def read(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("full", "workers", "coverage", "semantic", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    full = read(args.full)
    workers = read(args.workers)
    coverage = read(args.coverage)
    semantic = read(args.semantic)
    numbers = {node: i for i, node in enumerate(semantic["tests"])}
    if set(full["tests"]) != numbers.keys():
        raise ValueError("Incomplete whole-suite profile")
    blocked = set(workers["startup"]) | set(full["startup"])
    attributes = {key.rsplit(".", 1)[-1] for key in full["owners"] if not key.startswith("lambda:")}
    test_reads = defaultdict(set)
    for path, source in coverage["sources"].items():
        if not path.startswith("tests/"):
            continue
        startup = set(coverage["startup"].get(path, ()))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"getattr", "hasattr"} and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    name = node.args[1].value
            if not isinstance(name, str) or name not in attributes:
                continue
            for slot in coverage["lines"].get(path, {}).get(str(node.lineno), ()):
                test_reads[name].add(coverage["tests"][slot])
            if node.lineno in startup:
                test_reads[name].update(test for test in numbers if test.startswith(path + "::"))
    total = 0
    for key, slots in full["owners"].items():
        if key in blocked:
            continue
        tests = {full["tests"][i] for i in slots}
        tests.update(workers["tests"][i] for i in workers["owners"].get(key, ()))
        name = key
        if not key.startswith("lambda:"):
            name = "option:" + key
            tests.update(test_reads[key.rsplit(".", 1)[-1]])
        ids = {numbers[node] for node in tests if node in numbers}
        ids.update(semantic["owners"].get(name, ()))
        semantic["owners"][name] = sorted(ids)
        total += 1
    with gzip.GzipFile(str(args.output), "wb", mtime=0) as stream:
        stream.write(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode())
    print(f"Integrated {total} observed, non-startup callback and option keys.")

if __name__ == "__main__":
    main()
