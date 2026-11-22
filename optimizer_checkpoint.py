"""Re-run the native optimizer for expensive executor cases after an eligible edit."""

import ast
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from replay import cpu_used, mirror, safe_fixture, schedule_cost


def initial_imports(body):
    result = []
    for stmt in body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        if isinstance(stmt, ast.ImportFrom) and (stmt.module or "").startswith(
            "sqlglot."
        ):
            result.append(stmt)
        else:
            break
    return result


class Declarations(ast.NodeTransformer):
    """Keep all eager declarations, optionally ignoring ordinary helper argument names."""

    def __init__(self):
        self.functions = []

    def visit_FunctionDef(self, node):
        arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        names = tuple(arg.arg for arg in arguments)
        body = tuple(ast.dump(stmt) for stmt in node.body)
        self.functions.append((node, names, body))
        for arg in arguments:
            arg.arg = "_"
        node.body = [ast.copy_location(ast.Pass(), node.body[0])]
        return node


def runtime_ranges(source, updated):
    """Accept edits to existing function bodies and simple helper parameter renames."""
    a, b = Declarations(), Declarations()
    old = ast.parse(source, type_comments=True)
    original_functions = [
        node for node in ast.walk(old) if isinstance(node, ast.FunctionDef)
    ]
    original_bodies = {id(node): tuple(node.body) for node in original_functions}
    new = ast.parse(updated, type_comments=True)
    new_bodies = {
        id(n): tuple(n.body) for n in ast.walk(new) if isinstance(n, ast.FunctionDef)
    }
    if ast.dump(a.visit(old)) != ast.dump(b.visit(new)):
        return None
    ranges = []
    for (before, old_names, old_body), (after, new_names, new_body) in zip(
        a.functions, b.functions
    ):
        if (old_names, old_body) == (new_names, new_body):
            continue
        if (
            before.decorator_list != []
            or before.returns
            and isinstance(before.returns, ast.Call)
        ):
            return None
        if old_names != new_names and (
            before.name.startswith("__") or "self" in old_names or "cls" in old_names
        ):
            return None
        # Ambient process state can leak beyond the compared expression graph.
        # Reject changed functions that access it, even along unvisited paths.
        forbidden_names = {
            "open",
            "eval",
            "exec",
            "setattr",
            "delattr",
            "globals",
            "__import__",
            "builtins",
            "os",
            "sys",
            "subprocess",
            "threading",
            "random",
            "time",
            "ctypes",
            "socket",
        }
        old_imports = initial_imports(original_bodies[id(before)])
        new_imports = initial_imports(new_bodies[id(after)])
        if [ast.dump(n) for n in old_imports] != [ast.dump(n) for n in new_imports]:
            return None
        for stmt in (*original_bodies[id(before)], *new_bodies[id(after)]):
            if stmt in old_imports or stmt in new_imports:
                continue
            for n in ast.walk(stmt):
                if (
                    isinstance(
                        n,
                        (
                            ast.Global,
                            ast.Nonlocal,
                            ast.Import,
                            ast.ImportFrom,
                            ast.Await,
                            ast.Yield,
                            ast.YieldFrom,
                        ),
                    )
                    or isinstance(n, ast.Name)
                    and n.id in forbidden_names
                    or isinstance(n, ast.Attribute)
                    and n.attr in {"__dict__", "__globals__", "__class__"}
                ):
                    return None
        ranges.append((original_bodies[id(before)][0].lineno, before.end_lineno))
    return ranges


class OptimizerCheckpoint:
    def __init__(self, root, tests, durations, schedule, record):
        self.root, self.durations, self.schedule = root, durations, schedule
        self.target = next(
            (
                t["number"]
                for t in tests
                if t["id"] == "tests/test_executor.py::TestExecutor::test_execute_tpcds"
            ),
            None,
        )
        self.startup, phases = record
        self.phases = phases["lines"]
        self.spent = 0.0
        self.elapsed = 0.0

    def check(self, selected, text, selector, parse, apply, ordinary_ranges):
        n = self.target
        if (
            n not in selected
            or n not in selector.isolated
            or self.spent >= 18
            or self.elapsed >= 28
        ):
            return False
        sections = parse(text)
        if not 1 <= len(sections) <= 4 or not any(
            p.startswith("sqlglot/optimizer/") for p, _ in sections
        ):
            return False
        saved = (
            schedule_cost(selected, self.durations, self.schedule)
            - schedule_cost(selected - {n}, self.durations, self.schedule)
        ) / 1000
        if saved < 5:
            return False
        replacements = {}
        affected = 0
        new_boundary = False
        production = sum(path.startswith('sqlglot/') for path, _ in sections)
        for path, hunks in sections:
            if path in replacements or n in selector.source_readers.get(path, ()):
                return False
            if safe_fixture(path):
                if (
                    path in selector.isolated[n]["opened"]
                    or n in selector.class_fixtures.get(path, ())
                    or n in selector.numbers_for(selector.reads.get(path, ()))
                ):
                    return False
                continue
            if (
                not path.startswith("sqlglot/optimizer/")
                or path.count("/") != 2
                or not path.endswith(".py")
                or path.endswith("/__init__.py")
            ):
                return False
            original = selector.sources[path]
            updated = apply(original, hunks)
            if ordinary_ranges(original, updated) is None:
                new_boundary = True
            elif production == 1:
                return False
            ranges = runtime_ranges(original, updated)
            if ranges is None:
                return False
            changed = {line for start, end in ranges for line in range(start, end + 1)}
            if changed.intersection(self.startup.get(path, ())):
                return False
            inside = {
                int(l): mask
                for l, mask in self.phases.get("opt", {}).get(path, {}).items()
            }
            other = {
                int(l)
                for phase, paths in self.phases.items()
                if phase != "opt"
                for l in paths.get(path, {})
            }
            isolated = set(selector.isolated[n]["lines"].get(path, ()))
            if changed & other or (changed & isolated) - inside.keys():
                return False
            for line in changed & inside.keys():
                affected |= inside[line]
            replacements[path] = updated
        if not replacements or not new_boundary:
            return False
        start = time.monotonic()
        cpu = cpu_used()
        timeout = min(10.0, max(1.5, 0.6 * saved), 28 - self.elapsed, 18 - self.spent)
        if timeout < 0.5:
            return False
        try:
            with TemporaryDirectory(prefix="br-optimizer-") as tmp:
                project = Path(tmp)
                mirror(self.root, project, replacements)
                outcome = project / "optimizer-verdict"
                run = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(Path(__file__).with_name("optimizer_result.py")),
                        str(project),
                        str(outcome),
                        str(affected),
                        *replacements,
                    ],
                    cwd=project,
                    env=os.environ
                    | {
                        "PYTHONHASHSEED": "0",
                        "PYTHONPATH": str(project),
                        "SKIP_INTEGRATION": "0",
                    },
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
                return (
                    run.returncode == 0
                    and outcome.is_file()
                    and outcome.read_bytes() == b"complete"
                )
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            self.elapsed += time.monotonic() - start
            self.spent += cpu_used() - cpu
