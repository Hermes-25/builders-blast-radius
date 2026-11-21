"""Attach tests reading scalar dialect options to the existing semantic profile."""

import argparse
import ast
from collections import defaultdict
import gzip
import json
from pathlib import Path


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("options", type=Path)
    parser.add_argument("coverage", type=Path)
    parser.add_argument("semantic", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--semantic-workers", type=Path)
    parser.add_argument("--option-workers", type=Path)
    args = parser.parse_args()
    profile = read(args.semantic)
    options = read(args.options)
    coverage = read(args.coverage)
    slots = {test: idx for idx, test in enumerate(profile["tests"])}
    if args.semantic_workers:
        workers = read(args.semantic_workers)
        remap = [slots[name] for name in workers["tests"]]
        for key, indices in workers["owners"].items():
            parent = set(profile["owners"].get(key, ()))
            profile["owners"][key] = sorted(parent | {remap[i] for i in indices})
        for key, types in workers["types"].items():
            profile["types"][key] = sorted(set(profile["types"].get(key, ())) | set(types))
        for key in ("declared", "startup"):
            profile[key] = sorted(set(profile[key]) | set(workers[key]))
    if args.option_workers:
        workers = read(args.option_workers)
        option_slots = {name: index for index, name in enumerate(options["tests"])}
        remap = [option_slots[name] for name in workers["tests"]]
        startup = set(workers.get("startup", ()))
        for key in list(options["owners"]):
            if key in startup:
                del options["owners"][key]
            else:
                extra = {remap[i] for i in workers["owners"].get(key, ())}
                options["owners"][key] = sorted(set(options["owners"][key]) | extra)
    if set(options["tests"]) != slots.keys():
        raise ValueError(
            "Option profile did not exercise the full original test population"
        )
    dialect_options = {key.rsplit(".", 1)[-1] for key in options["owners"]}
    test_readers = defaultdict(set)
    for path, src in coverage["sources"].items():
        if not path.startswith("tests/"):
            continue
        for node in ast.walk(ast.parse(src)):
            key = None
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                key = node.attr
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
            ):
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    key = node.args[1].value
            if isinstance(key, str) and key in dialect_options:
                active = coverage["lines"].get(path, {}).get(str(node.lineno), ())
                test_readers[key].update(coverage["tests"][i] for i in active)
    for key, ids in options["owners"].items():
        attr = key.rsplit(".", 1)[-1]
        names = {options["tests"][i] for i in ids} | test_readers.get(attr, set())
        profile["owners"]["option:" + key] = sorted(
            slots[name] for name in names if name in slots
        )
    with gzip.GzipFile(str(args.output), "wb", mtime=0) as stream:
        stream.write(
            json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf8")
        )
    print(f"Indexed {len(options['owners'])} measured dialect option dependencies.")


if __name__ == "__main__":
    main()
