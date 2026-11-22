"""Choose regression tests using coverage from the unchanged SQLGlot revision."""

import argparse
import ast
from collections import defaultdict
from functools import lru_cache
import gzip
import hashlib
import json
import marshal
from pathlib import Path
import re
import subprocess
import sys
import time

from semantic import SemanticProfile, statement_span, parsed


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
        hunk_start = next(
            (i for i, line in enumerate(lines) if HUNK.match(line)), len(lines)
        )
        old = next(
            (line[4:] for line in lines[:hunk_start] if line.startswith("--- ")), ""
        )
        new = next(
            (line[4:] for line in lines[:hunk_start] if line.startswith("+++ ")), ""
        )
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
        self.bodies.append(
            (
                node.body[0].lineno,
                node.end_lineno,
                tuple(ast.dump(statement) for statement in node.body),
            )
        )
        node.body = [ast.Pass()]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.bodies.append((node.body.lineno, node.end_lineno, (ast.dump(node.body),)))
        node.body = ast.Constant(value=None)
        return node


REFERENCE_SHAPES = {}
REFERENCE_CODE = {}


@lru_cache(maxsize=28)
def function_shape(source):
    known=REFERENCE_SHAPES.get(source)
    if known is not None:
        return known
    funcs=FunctionBodies()
    tree=funcs.visit(ast.parse(source,type_comments=True))
    declaration=hashlib.sha256(ast.dump(tree).encode()).hexdigest()
    rows=tuple((start,end,hashlib.sha256('\0'.join(body).encode()).hexdigest())
               for start,end,body in funcs.bodies)
    return declaration,rows


def code_hash(source,path):
    return hashlib.sha256(marshal.dumps(compile(source,path,'exec',dont_inherit=True))).hexdigest()


def function_ranges(original, updated):
    old_shape,old_bodies=function_shape(original)
    new_shape,new_bodies=function_shape(updated)
    if old_shape!=new_shape:
        return None
    return [
        (start, end)
        for (start, end, old), (_, _, new) in zip(
            old_bodies, new_bodies, strict=True
        )
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
    return ast.dump(
        ClassLiterals().visit(ast.parse(original, type_comments=True))
    ) == ast.dump(ClassLiterals().visit(ast.parse(updated, type_comments=True)))


class Selector:
    def __init__(self, index, tests, semantic=None, source_reads=None):
        self.tests = tests
        self.numbers = {test["id"]: test["number"] for test in tests}
        self.by_file = defaultdict(set)
        for test in tests:
            self.by_file[test["file"]].add(test["number"])
        self.all_numbers = set(self.numbers.values())
        self.slots = [self.numbers.get(node) for node in index["tests"]]
        self.unprofiled = self.all_numbers.difference(self.slots)
        self.semantic = SemanticProfile(semantic, self.numbers) if semantic else None
        self.reads = index["reads"]
        self.class_fixtures = {
            path: {self.numbers[name] for name in names if name in self.numbers}
            for path, names in (source_reads or {}).get("class_fixtures", {}).items()
        }
        self.isolated = {}
        self.values = None
        self.helper = None
        self.binding = None
        self.parser_registry = None
        self.import_graph = None
        self.flow = None
        self.module_lines = {}
        self.module_groups = []
        self.module_unknown = set()
        self.source_readers = None
        if source_reads is not None:
            seen = set(source_reads["tests"])
            self.untraced_reads = {
                n for name, n in self.numbers.items() if name not in seen
            }
            self.source_readers = {
                path: {self.numbers[name] for name in names if name in self.numbers}
                for path, names in source_reads["reads"].items()
            }
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

    def isolated_risk(self, path, spans):
        """Baseline execution includes each expensive test's own import-time and worker code."""
        result = self.all_numbers.copy()
        for number, profile in self.isolated.items():
            seen = profile["lines"].get(path, ())
            if spans is None:
                if seen or path in profile["opened"]:
                    continue
            elif any(a <= line <= b for a, b in spans for line in seen):
                continue
            if number not in self.source_readers.get(path, ()):
                result.discard(number)
        return result

    def set_module_profile(self, profile):
        """Each group contains tests run together in a separate baseline interpreter."""
        self.module_groups = [self.by_file[path] for path in profile["modules"]]
        self.module_unknown = self.all_numbers.difference(
            *(self.by_file[path] for path in profile["modules"] if path not in profile["unknown"])
        )
        self.module_lines = {
            path: {int(line): mask for line, mask in lines.items()}
            for path, lines in profile["lines"].items()
        }

    def structural_risk(self, path, spans):
        """Intersect broad fallback with startup, case and worker events for each test file."""
        broad = self.isolated_risk(path, spans)
        module_lines = self.module_lines.get(path)
        if not module_lines:
            return broad
        mask = 0
        if spans is None:
            for owners in module_lines.values():
                mask |= owners
            risk = self.file_tests.get(path, set()).copy()
        else:
            def within(line):
                return any(start <= line <= end for start, end in spans)

            for line, owners in module_lines.items():
                if within(line):
                    mask |= owners
            if not mask:
                return broad
            slots = {
                slot for line, values in self.lines.get(path, {}).items() if within(line)
                for slot in values
            }
            risk = self.numbers_for(slots)
        for group, numbers in enumerate(self.module_groups):
            if mask & (1 << group):
                risk.update(numbers)
        risk.update(self.module_unknown)
        risk.update(self.source_readers.get(path, ()))
        risk.update(self.unprofiled)
        risk.update(number for number in self.isolated if number in broad)
        return broad.intersection(risk)

    def numbers_for(self, slots):
        return {self.slots[slot] for slot in slots} - {None}

    def affected(self, patch):
        selected = set()
        try:
            sections = changes(patch)
            if 1 < len(sections) <= 8 and len({p for p, _ in sections}) == len(sections):
                parts = re.split(r"(?m)^diff --git ", patch)[1:]
                kept = []
                removed = set()
                if len(parts) == len(sections):
                    for part, (path, hunks) in zip(parts, sections):
                        source = self.sources.get(path)
                        if (source is not None and path.startswith("sqlglot/") and path.endswith(".py")
                            and all(not line[1:].strip() or line[1:].lstrip().startswith("#")
                                    for line in hunks if line.startswith(("+","-")))):
                            updated = apply_hunks(source, hunks)
                            if ast.dump(parsed(source)) == ast.dump(parsed(updated)):
                                removed.add(path)
                                continue
                        kept.append("diff --git " + part)
                    if removed:
                        selected.update(self.untraced_reads)
                        for path in removed:
                            selected.update(self.source_readers.get(path, ()))
                            selected.update(self.by_file.get(path, ()))
                            selected.update(self.class_fixtures.get(path, ()))
                        if kept:
                            selected.update(self.affected("".join(kept)))
                        return selected
            if (
                1 < len(sections) <= 6
                and len({p for p, _ in sections}) == len(sections)
                and sum(p in self.sources for p, _ in sections) == 1
                and all(
                    p in self.sources or p.startswith("tests/fixtures/") and p.endswith(".sql")
                    for p, _ in sections
                )
            ):
                # A changed callee can invalidate a caller-specific proof. Keep
                # broad selection across multiple production files. Fixture reads
                # can be combined with one production file, followed by replay of
                # the complete patched tree.
                parts = re.split(r"(?m)^diff --git ", patch)[1:]
                if len(parts) == len(sections):
                    return set().union(*(self.affected("diff --git " + entry) for entry in parts))
            for path, hunks in sections:
                readers = self.numbers_for(self.reads.get(path, ()))
                if self.source_readers is not None and path in self.sources:
                    readers = self.source_readers.get(path, set()) | self.untraced_reads
                readers.update(self.class_fixtures.get(path, ()))
                selected.update(readers)
                selected.update(self.by_file.get(path, ()))
                if path not in self.sources:
                    if not readers and path not in self.by_file:
                        return self.all_numbers
                    continue

                source = self.sources[path]
                updated = apply_hunks(source, hunks)
                # Compilation also catches illegal scope/control-flow edits that
                # pass the AST parser. Identical bytecode and metadata cover
                # harmless changes folded by Python's compiler.
                baseline = REFERENCE_CODE.get((path, source)) or code_hash(source, path)
                if baseline == code_hash(updated, path):
                    continue
                if (self.parser_registry is not None and len(sections)==1 and path.startswith("sqlglot/parsers/")
                    and any(re.match(r"""[-+]\s+(?:"[^"]+"|'[^']+'|TokenType\.\w+)\s*:""",line)
                            for line in hunks)):
                    precise=self.parser_registry.selection(path,source,updated,self)
                    if precise is not None:
                        selected.update(precise)
                        continue
                if (self.flow is not None and len(sections)==1 and
                        any(re.match(r"[-+]\s+[A-Z][A-Z_]+\s*[:=]", line) for line in hunks)):
                    precise=self.flow.selection(path,source,updated)
                    if precise is not None:
                        selected.update(precise)
                        continue
                if (self.binding is not None and len(sections) == 1 and path.startswith("sqlglot/") and
                    any(re.match(r"[-+](?:from \w|[A-Za-z_]\w*\s*[:=])", line) for line in hunks)):
                    precise = self.binding.selection(path, source, updated, self)
                    if precise is not None:
                        selected.update(precise)
                        continue
                if self.semantic is not None and len(sections) == 1:
                    precise = self.semantic.selection(
                        path, source, updated, self.sources
                    )
                    if precise is not None:
                        selected.update(precise)
                        continue
                ranges = function_ranges(source, updated)
                if ranges == []:
                    continue
                file_lines = self.lines.get(path, {})
                selected.update(self.unprofiled)
                if ranges is None:
                    # Globals can be consumed by other modules after import,
                    # without executing a line in their defining file again.
                    path_parts = path.split("/")
                    if len(path_parts) == 3 and path_parts[:2] == [
                        "sqlglot",
                        "dialects",
                    ]:
                        dialect = path_parts[-1]
                        if dialect not in {
                            "__init__.py",
                            "dialect.py",
                        } and only_class_literals_changed(source, updated):
                            dialect_tests = set()
                            for component in (
                                "dialects",
                                "parsers",
                                "generators",
                                "typing",
                            ):
                                dialect_tests.update(
                                    self.file_tests.get(
                                        f"sqlglot/{component}/{dialect}", ()
                                    )
                                )
                            # Shared parser methods may never execute a line in the
                            # concrete dialect, including throughout its own tests.
                            dialect_tests.update(
                                self.by_file.get("tests/dialects/test_" + dialect, ())
                            )
                            if dialect_tests:
                                selected.update(dialect_tests)
                                continue
                    selected.update(self.structural_risk(path, None))
                    continue

                span = statement_span(source, updated) if len(ranges) == 1 else None
                for start, end in ranges:
                    if any(start <= line <= end for line in self.startup.get(path, ())):
                        selected.update(self.structural_risk(path, [(start, end)]))
                        continue
                    if span and start <= span[0] <= span[1] <= end:
                        if any(span[0] <= line <= span[1] for line in file_lines):
                            start, end = span
                    slots = set()
                    for line, ids in file_lines.items():
                        if start <= line <= end:
                            slots.update(ids)
                    selected.update(self.numbers_for(slots))
                    for number, profile in self.isolated.items():
                        if any(
                            start <= line <= end
                            for line in profile["lines"].get(path, ())
                        ):
                            selected.add(number)
        except (ValueError, SyntaxError, RecursionError):
            self.invalid_patch = True
            return self.all_numbers
        if self.values is not None and len(sections) == 1 and selected:
            path, hunks = sections[0]
            if (
                path in self.sources
                and path.startswith("sqlglot/")
                and path.endswith(".py")
            ):
                source = self.sources[path]
                try:
                    observed = self.values.selection(path, source, updated, self)
                    if observed is not None:
                        preserved = (
                            set(self.source_readers.get(path, ())) | self.untraced_reads
                        )
                        preserved.update(self.by_file.get(path, ()))
                        preserved.update(self.class_fixtures.get(path, ()))
                        selected.intersection_update(observed | preserved)
                except (ValueError, SyntaxError, RecursionError):
                    pass
        if self.helper is not None and selected:
            try:
                proof = self.helper.selection(sections,self,apply_hunks)
            except (ValueError,TypeError,KeyError,SyntaxError,RecursionError,OverflowError):
                proof = None
            if proof is not None:
                selected.intersection_update(proof)
        if self.import_graph is not None and len(sections) == 1 and selected:
            path, hunks = sections[0]
            if path in self.sources and any(line.startswith(("-from ", "+from ")) for line in hunks):
                try:
                    source = self.sources[path]
                    proof = self.import_graph.selection(path, source, apply_hunks(source, hunks), self)
                    if proof is not None:
                        selected.intersection_update(proof)
                except (ValueError, TypeError, KeyError, SyntaxError, RecursionError):
                    pass
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


def guard_schedule(
    workers, patch, selector, durations, verified_passes=(), allowance=0
):
    """Use already-paid worker time for extra coverage, preserving the original assignment."""
    if not workers:
        return workers
    scheduled = {n for worker in workers for n in worker}
    excluded = scheduled.union(verified_passes)
    if len(scheduled) == len(durations):
        return workers
    try:
        sections = changes(patch)
    except ValueError:
        return workers
    primary = set()
    secondary = set()
    for path, hunks in sections:
        file_lines = selector.lines.get(path, {})
        near = set()
        old_line = 0
        for line in hunks:
            hunk = HUNK.match(line)
            if hunk:
                old_line = int(hunk[1])
            elif line.startswith("-"):
                near.update((old_line - 1, old_line, old_line + 1))
                old_line += 1
            elif line.startswith("+"):
                near.update((old_line - 1, old_line))
            elif line.startswith(" "):
                old_line += 1
        for line in near:
            primary.update(selector.numbers_for(file_lines.get(line, ())))
        secondary.update(selector.file_tests.get(path, ()))
        secondary.update(selector.by_file.get(path, ()))
        if path.startswith("sqlglot/") and path.endswith(".py"):
            stem = path.split("/")[-1]
            secondary.update(selector.by_file.get("tests/dialects/test_" + stem, ()))
            secondary.update(selector.by_file.get("tests/test_" + stem, ()))
    relevant = bool(primary or secondary)
    loads = [sum(durations[n] for n in worker) for worker in workers]
    busiest = max(loads)

    def pack(group, cap):
        for number in sorted(
            group.difference(excluded), key=lambda n: (durations[n], n)
        ):
            idx = min(range(len(loads)), key=loads.__getitem__)
            if loads[idx] + durations[number] > cap:
                break
            workers[idx].append(number)
            loads[idx] += durations[number]
            excluded.add(number)

    for group in (primary, secondary, selector.all_numbers):
        pack(group, busiest)
    if relevant and allowance:
        for group in (primary, secondary):
            pack(group, busiest + allowance)
    return workers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument(
        "--index", type=Path, default=Path(__file__).with_name("impact-index.json.gz")
    )
    parser.add_argument(
        "--semantics",
        type=Path,
        default=Path(__file__).with_name("semantic-index.json.gz"),
    )
    parser.add_argument("--project", type=Path, default=Path("/project"))
    parser.add_argument("--static", action="store_true")
    parser.add_argument(
        "--guard",
        type=int,
        default=120,
        help="Extra milliseconds allowed per patch; negative disables packing",
    )
    args = parser.parse_args()
    with gzip.open(args.semantics, "rt", encoding="utf-8") as stream:
        semantic = json.load(stream)
    tests = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))["tests"]
    with gzip.open(args.index, "rt", encoding="utf-8") as stream:
        with gzip.open(
            Path(__file__).with_name("source-reads.json.gz"), "rt", encoding="utf-8"
        ) as reader:
            selector = Selector(json.load(stream), tests, semantic, json.load(reader))
    isolated_path = Path(__file__).with_name("isolated.json.gz")
    if isolated_path.exists():
        with gzip.open(isolated_path, "rt", encoding="utf-8") as handle:
            selector.isolated = {
                selector.numbers[name]: profile
                for name, profile in json.load(handle).items()
                if name in selector.numbers
            }
    module_path = Path(__file__).with_name("module-profile.json.gz")
    if module_path.exists():
        with gzip.open(module_path, "rt", encoding="utf-8") as handle:
            selector.set_module_profile(json.load(handle))
    value_path = Path(__file__).with_name("operand-values.json.gz")
    if value_path.exists():
        from operand_values import ValueProfile
        with gzip.open(value_path, "rt") as handle:
            selector.values = ValueProfile(json.load(handle), selector.numbers)
    durations = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    flow_path=Path(__file__).with_name("forward-reads.json.gz")
    if flow_path.exists():
        from option_flow import FlowProfile
        with gzip.open(flow_path,"rt") as handle:
            selector.flow=FlowProfile(json.load(handle),selector.numbers)
    helper_path = Path(__file__).with_name("helper-inputs.json.gz")
    if helper_path.exists():
        from helper_replay import HelperProfile
        with gzip.open(helper_path,"rt") as handle:
            selector.helper = HelperProfile(json.load(handle),selector.numbers)
    binding_path = Path(__file__).with_name("binding-reads.json.gz")
    if binding_path.exists():
        from binding_reach import BindingProfile
        with gzip.open(binding_path,"rt") as handle:
            selector.binding = BindingProfile(json.load(handle),selector.numbers,args.project)
    verifier = None
    line_gate = None
    line_path = Path(__file__).with_name("line-gates.json.gz")
    if line_path.exists():
        from line_gate import LineGate
        from replay import schedule_cost
        with gzip.open(line_path,"rt") as handle:
            line_gate=LineGate(json.load(handle),selector.numbers)
    parser_map_path = Path(__file__).with_name("parser-registry.json.gz")
    if parser_map_path.exists() and not args.static:
        from parser_registry import ParserRegistry
        with gzip.open(parser_map_path,"rt") as handle:
            selector.parser_registry=ParserRegistry(json.load(handle),selector.numbers)
    graph_path = Path(__file__).with_name("import-graph.json.gz")
    if graph_path.exists() and not args.static:
        from import_graph import ImportGraph
        with gzip.open(graph_path, "rt") as handle:
            selector.import_graph = ImportGraph(json.load(handle), tests, args.project)
    reference_path=Path(__file__).with_name('syntax-reference.json.gz')
    if reference_path.is_file():
        with gzip.open(reference_path,'rt') as handle:
            reference=json.load(handle)
        if reference['runtime']==[sys.implementation.cache_tag,sys.hexversion,sys.flags.optimize]:
            for path,(digest,code,shape,functions) in reference['files'].items():
                source=selector.sources.get(path)
                if source is not None and hashlib.sha256(source.encode()).hexdigest()==digest:
                    REFERENCE_CODE[path,source]=code
                    REFERENCE_SHAPES[source]=shape,functions
    slices = None
    optimizer_checkpoint = None
    if args.project.is_dir() and not args.static:
        from replay import Verifier
        from frontier import FrontierVerifier
        from optimizer_checkpoint import OptimizerCheckpoint
        from private_helper import verify as verify_private_helper

        verifier = Verifier(args.project, tests, assign_workers)
        slices = FrontierVerifier(args.project, tests, assign_workers)
        optimizer_checkpoint = OptimizerCheckpoint(args.project, tests, durations, assign_workers, slices.records['execute_tpcds'])
    emergency_deadline = time.monotonic() + 135
    full_schedule = None
    for patch in sorted((args.data / "commits").glob("*.patch")):
        if time.monotonic() >= emergency_deadline:
            if full_schedule is None:
                full_schedule = assign_workers(selector.all_numbers, durations)
            print(
                json.dumps(
                    {"commit": patch.stem, "workers": full_schedule},
                    separators=(",", ":"),
                )
            )
            continue
        try:
            text = patch.read_text(encoding="utf-8")
            if selector.import_graph is not None:
                selector.import_graph.confirmed_path = None
            selector.invalid_patch = False
            selected = selector.affected(text)
            if (
                line_gate is not None
                and not selector.invalid_patch
                and (len(selected) > 35 or any(durations[n] >= 1000 for n in selected))
            ):
                try:
                    proof = line_gate.selection(changes(text), selector)
                    if proof is not None:
                        narrower = selected.intersection(proof)
                        if narrower != selected:
                            before = schedule_cost(selected, durations, assign_workers)
                            after = schedule_cost(narrower, durations, assign_workers)
                            if before - after >= 500:
                                selected = narrower
                except (KeyError, ValueError, TypeError, SyntaxError, OverflowError, RecursionError):
                    pass
            # A fresh interpreter already confirmed that the target module
            # cannot be initialized. Keep every identified dependent test and
            # reserve the limited real-test budget for other patch types.
            confirmed = (selector.import_graph and selector.import_graph.confirmed_path
                         and text.count("diff --git ") == 1)
            if slices and not confirmed:
                selected = slices.check(selected, text, selector, changes, apply_hunks, function_ranges)
                if 'def _' in text:
                    try:
                        private_case=verify_private_helper(text,selected,selector,changes,apply_hunks,FunctionBodies,args.project)
                        if private_case is not None:
                            selected=selected-{private_case}
                            slices.passed.add(private_case)
                    except (OSError,ValueError,KeyError,TypeError,SyntaxError,RecursionError,subprocess.TimeoutExpired):
                        pass
                if optimizer_checkpoint.target not in slices.attempted and 'sqlglot/optimizer/' in text:
                    try:
                        if optimizer_checkpoint.check(selected, text, selector, changes, apply_hunks, function_ranges):
                            selected = selected - {optimizer_checkpoint.target}
                            slices.passed.add(optimizer_checkpoint.target)
                    except (ValueError, KeyError, TypeError, SyntaxError, OSError, RecursionError):
                        pass
            if verifier and not confirmed:
                selected = verifier.check(
                    selected, text, selector, changes, apply_hunks
                )
            if confirmed:
                if slices:
                    slices.passed = set()
                if verifier:
                    verifier.passed = set()
        except UnicodeError:
            text = ""
            selected = selector.all_numbers
        workers = assign_workers(selected, durations)
        if args.guard >= 0:
            workers = guard_schedule(
                workers,
                text,
                selector,
                durations,
                (verifier.passed | slices.passed) if verifier else (),
                args.guard,
            )
        print(
            json.dumps(
                {"commit": patch.stem, "workers": workers}, separators=(",", ":")
            )
        )


if __name__ == "__main__":
    main()
