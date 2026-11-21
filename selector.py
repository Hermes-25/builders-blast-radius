"""Choose regression tests using coverage from the unchanged SQLGlot revision."""

import argparse
import ast
from collections import defaultdict
import gzip
import json
from pathlib import Path
import re


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def changes(patch):
    """Read ordinary unified diffs. Ambiguous or unsupported changes fall back."""
    sections = re.split(r"(?m)^diff --git ", patch)
    if not sections[1:]:
        if not patch.strip():
            return []
        sections = ["", "a/unknown b/unknown\n" + patch]
    result = []
    for section in sections[1:]:
        lines = section.split("\n")
        hunk_start = next((i for i, line in enumerate(lines) if HUNK.match(line)), len(lines))
        old = next((line[4:] for line in lines[:hunk_start] if line.startswith("--- ")), "")
        new = next((line[4:] for line in lines[:hunk_start] if line.startswith("+++ ")), "")
        if not old.startswith("a/") or not new.startswith("b/") or old[2:] != new[2:]:
            raise ValueError("creation, deletion, rename, or unsupported path")
        if hunk_start == len(lines):
            raise ValueError("no text hunks")
        result.append((old[2:], lines[hunk_start:]))
    return result


def apply_hunks(source, hunks):
    original = source.split("\n")
    output = []
    cursor = 0
    used = 0
    written = 0
    old_count = new_count = 0
    for line in hunks:
        header = HUNK.match(line)
        if header:
            if (used, written) != (old_count, new_count):
                raise ValueError("hunk count mismatch")
            old_count = 1 if header[2] is None else int(header[2])
            new_count = 1 if header[4] is None else int(header[4])
            start = int(header[1]) - bool(old_count)
            if not cursor <= start <= len(original):
                raise ValueError("invalid hunk position")
            output.extend(original[cursor:start])
            cursor = start
            used = written = 0
        elif line.startswith((" ", "-")):
            if cursor >= len(original) or original[cursor] != line[1:]:
                raise ValueError("patch context does not match the pinned revision")
            if line[0] == " ":
                output.append(line[1:])
                written += 1
            cursor += 1
            used += 1
        elif line.startswith("+"):
            output.append(line[1:])
            written += 1
        elif line and not line.startswith("\\ No newline at end of file"):
            raise ValueError("unsupported hunk line")
    if (used, written) != (old_count, new_count):
        raise ValueError("incomplete hunk")
    output.extend(original[cursor:])
    return "\n".join(output)


class FunctionBodies(ast.NodeTransformer):
    """Separate runtime function bodies from declarations and import-time code."""

    def __init__(self):
        self.bodies = []

    def visit_FunctionDef(self, node):
        self.bodies.append((
            node.body[0].lineno,
            node.end_lineno,
            tuple(ast.dump(statement) for statement in node.body),
        ))
        node.body = [ast.Pass()]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.bodies.append((node.body.lineno, node.end_lineno, (ast.dump(node.body),)))
        node.body = ast.Constant(value=None)
        return node


def function_ranges(original, updated):
    old_tree = ast.parse(original, type_comments=True)
    new_tree = ast.parse(updated, type_comments=True)
    if ast.dump(old_tree) == ast.dump(new_tree):
        return []
    old_bodies = FunctionBodies()
    new_bodies = FunctionBodies()
    if ast.dump(old_bodies.visit(old_tree)) != ast.dump(new_bodies.visit(new_tree)):
        return None
    return [
        (start, end)
        for (start, end, old), (_, _, new) in zip(old_bodies.bodies, new_bodies.bodies, strict=True)
        if old != new
    ]


class ClassLiterals(ast.NodeTransformer):
    """Erase plain literal class attributes while keeping executable declarations."""

    def visit_ClassDef(self, node):
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                try:
                    ast.literal_eval(statement.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                statement.value = ast.Constant(value=None)
        return self.generic_visit(node)


def only_class_literals_changed(original, updated):
    return ast.dump(ClassLiterals().visit(ast.parse(original, type_comments=True))) == ast.dump(
        ClassLiterals().visit(ast.parse(updated, type_comments=True))
    )


class Selector:
    def __init__(self, index, tests):
        self.tests = tests
        self.numbers = {test["id"]: test["number"] for test in tests}
        self.by_file = defaultdict(set)
        for test in tests:
            self.by_file[test["file"]].add(test["number"])
        self.all_numbers = set(self.numbers.values())
        self.slots = [self.numbers.get(node) for node in index["tests"]]
        self.unprofiled = self.all_numbers.difference(self.slots)
        self.reads = index["reads"]
        self.sources = index["sources"]
        self.startup = index["startup"]
        self.lines = {
            path: {int(line): ids for line, ids in lines.items()}
            for path, lines in index["lines"].items()
        }
        self.file_tests = {
            path: self.numbers_for({slot for slots in lines.values() for slot in slots})
            for path, lines in self.lines.items()
        }

    def numbers_for(self, slots):
        return {self.slots[slot] for slot in slots} - {None}

    def affected(self, patch):
        selected = set()
        try:
            for path, hunks in changes(patch):
                readers = self.numbers_for(self.reads.get(path, ()))
                selected.update(readers)
                selected.update(self.by_file.get(path, ()))
                if path not in self.sources:
                    if not readers and path not in self.by_file:
                        return self.all_numbers
                    continue

                source = self.sources[path]
                updated = apply_hunks(source, hunks)
                ranges = function_ranges(source, updated)
                if ranges == []:
                    continue
                file_lines = self.lines.get(path, {})
                selected.update(self.unprofiled)
                if ranges is None:
                    # Globals can be consumed by other modules after import,
                    # without executing a line in their defining file again.
                    path_parts = path.split("/")
                    if len(path_parts) == 3 and path_parts[:2] == ["sqlglot", "dialects"]:
                        dialect = path_parts[-1]
                        if dialect not in {"__init__.py", "dialect.py"} and only_class_literals_changed(source, updated):
                            dialect_tests = set()
                            for component in ("dialects", "parsers", "generators", "typing"):
                                dialect_tests.update(self.file_tests.get(f"sqlglot/{component}/{dialect}", ()))
                            if dialect_tests:
                                selected.update(dialect_tests)
                                continue
                    return self.all_numbers

                for start, end in ranges:
                    if any(start <= line <= end for line in self.startup.get(path, ())):
                        return self.all_numbers
                    slots = set()
                    for line, ids in file_lines.items():
                        if start <= line <= end:
                            slots.update(ids)
                    selected.update(self.numbers_for(slots))
        except (ValueError, SyntaxError, RecursionError):
            return self.all_numbers
        return selected


def assign_workers(selected, durations):
    if not selected:
        return []
    ordered = sorted(selected, key=lambda number: (-durations[number], number))
    total = sum(durations[number] for number in ordered)
    best_cost = total + 2000
    best = [ordered]
    for count in range(2, min(8, len(ordered)) + 1):
        floor = max(durations[ordered[0]], (total + count - 1) // count) + 2000 * count
        if floor >= best_cost:
            if durations[ordered[0]] + 2000 * count >= best_cost:
                break
            continue
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            worker = loads.index(min(loads))
            workers[worker].append(number)
            loads[worker] += durations[number]
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best_cost, best = cost, workers
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--index", type=Path, default=Path(__file__).with_name("impact-index.json.gz"))
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))["tests"]
    with gzip.open(args.index, "rt", encoding="utf-8") as stream:
        selector = Selector(json.load(stream), tests)
    durations = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    for patch in sorted((args.data / "commits").glob("*.patch")):
        try:
            selected = selector.affected(patch.read_text(encoding="utf-8"))
        except UnicodeError:
            selected = selector.all_numbers
        workers = assign_workers(selected, durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
