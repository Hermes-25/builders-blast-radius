"""Audit static aliases then merge original parser map lookups, including absent keys."""

import ast
import gzip
import json
from pathlib import Path
import sys


def read(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


def target(stmt):
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    return (
        targets[0].id
        if len(targets) == 1 and isinstance(targets[0], ast.Name)
        else None
    )


def passthrough(table, node, ancestors):
    """Only accept eager inheritance copies that preserve the map's key identity."""
    assignment = next(
        (x for x in reversed(ancestors) if isinstance(x, (ast.Assign, ast.AnnAssign))),
        None,
    )
    if not assignment or target(assignment) != table:
        return False
    value = assignment.value
    if node is value:
        return True
    if isinstance(value, ast.Dict):
        return any(k is None and v is node for k, v in zip(value.keys, value.values))
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "copy"
    ):
        return value.func.value is node and not value.args and not value.keywords
    if isinstance(value, ast.DictComp) and len(value.generators) == 1:
        comp = value.generators[0]
        if (
            isinstance(comp.target, ast.Tuple)
            and len(comp.target.elts) == 2
            and all(isinstance(x, ast.Name) for x in comp.target.elts)
            and isinstance(value.key, ast.Name)
            and isinstance(value.value, ast.Name)
            and [x.id for x in comp.target.elts] == [value.key.id, value.value.id]
            and isinstance(comp.iter, ast.Call)
            and isinstance(comp.iter.func, ast.Attribute)
            and comp.iter.func.value is node
            and comp.iter.func.attr == "items"
        ):
            return all(
                not any(isinstance(x, (ast.Call, ast.NamedExpr)) for x in ast.walk(c))
                for c in comp.ifs
            )
    return False


def safe_argument(node, parents):
    parent = parents.get(node)
    if isinstance(parent, ast.Subscript) and parent.value is node:
        return True
    if isinstance(parent, ast.Compare):
        if (
            len(parent.ops) == 1
            and isinstance(parent.ops[0], (ast.Is, ast.IsNot))
            and (
                isinstance(parent.left, ast.Constant)
                and parent.left.value is None
                or isinstance(parent.comparators[0], ast.Constant)
                and parent.comparators[0].value is None
            )
        ):
            return True
        return any(
            isinstance(op, (ast.In, ast.NotIn)) and v is node
            for op, v in zip(parent.ops, parent.comparators)
        )
    if (
        isinstance(parent, ast.Attribute)
        and parent.value is node
        and parent.attr in {"get", "keys", "values", "items"}
    ):
        return True
    return (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Name)
        and parent.func.id in {"len", "list", "set", "tuple", "frozenset"}
    )


def borrowed_arguments(sources):
    groups = {}
    for path, source in sources.items():
        if not path.endswith(".py"):
            continue
        for fn in ast.walk(ast.parse(source)):
            if (
                not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                or not fn.args.args
                or fn.args.args[0].arg not in {"self", "cls"}
            ):
                continue
            positions = set()
            body_nodes = [x for stmt in fn.body for x in ast.walk(stmt)]
            parents = {
                kid: par for par in body_nodes for kid in ast.iter_child_nodes(par)
            }
            for i, arg in enumerate(fn.args.args[1:]):
                uses = [
                    x for x in body_nodes if isinstance(x, ast.Name) and x.id == arg.arg
                ]
                if uses and all(
                    isinstance(x.ctx, ast.Load) and safe_argument(x, parents)
                    for x in uses
                ):
                    positions.add(i)
            groups[fn.name] = groups.get(fn.name, positions) & positions
    return groups


class Audit(ast.NodeVisitor):
    def __init__(self, candidates, borrowed=None):
        self.candidates = candidates
        self.unsafe = set()
        self.shape = set()
        self.stack = []
        self.borrowed = borrowed or {}

    def visit(self, node):
        name = (
            node.attr
            if isinstance(node, ast.Attribute)
            else node.id
            if isinstance(node, ast.Name)
            else None
        )
        if name in self.candidates and isinstance(node.ctx, ast.Load):
            self.check(name, node)
        self.stack.append(node)
        super().visit(node)
        self.stack.pop()

    def check(self, name, node):
        parent = self.stack[-1]
        if any(
            isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            for a in self.stack
        ):
            if (
                isinstance(parent, ast.Assign)
                and parent.value is node
                and len(parent.targets) == 1
                and isinstance(parent.targets[0], ast.Name)
            ):
                local = parent.targets[0].id
                fn = next(
                    x
                    for x in reversed(self.stack)
                    if isinstance(
                        x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                    )
                )
                nodes = list(ast.walk(fn))
                parents = {
                    kid: par for par in nodes for kid in ast.iter_child_nodes(par)
                }
                reads = [
                    x
                    for x in nodes
                    if isinstance(x, ast.Name)
                    and isinstance(x.ctx, ast.Load)
                    and x.id == local
                ]
                nested = any(
                    isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                    and x is not fn
                    for x in nodes
                )
                if (
                    reads
                    and not nested
                    and all(safe_argument(x, parents) for x in reads)
                ):
                    return
            if isinstance(parent, ast.Subscript) and parent.value is node:
                return
            if isinstance(parent, ast.Compare) and any(
                isinstance(op, (ast.In, ast.NotIn)) and right is node
                for op, right in zip(parent.ops, parent.comparators)
            ):
                return
            if (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and parent.attr
                in {
                    "get",
                    "keys",
                    "items",
                    "values",
                    "copy",
                    "setdefault",
                    "update",
                    "pop",
                    "clear",
                    "popitem",
                }
            ):
                return
            if (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Name)
                and parent.func.id
                in {"len", "list", "set", "tuple", "sorted", "frozenset", "reversed"}
            ):
                return
            expr = node
            for caller in reversed(self.stack):
                if isinstance(caller, ast.BoolOp):
                    expr = caller
                    continue
                if (
                    isinstance(caller, ast.Call)
                    and isinstance(caller.func, ast.Attribute)
                    and isinstance(caller.func.value, ast.Name)
                    and caller.func.value.id == "self"
                    and expr in caller.args
                    and caller.args.index(expr)
                    in self.borrowed.get(caller.func.attr, ())
                ):
                    return
                break
            self.unsafe.add(name)
        elif (
            self.source_path.startswith(("sqlglot/parser.py", "sqlglot/parsers/"))
            and any(
                isinstance(cls, ast.ClassDef) and cls.name.endswith("Parser")
                for cls in self.stack
            )
            and passthrough(name, node, self.stack)
        ):
            return
        elif isinstance(parent, ast.Starred):
            self.shape.add(name)
        else:
            self.unsafe.add(name)


def main(full_path, worker_path, coverage_path, inventory_path, out):
    full, worker, index = map(read, (full_path, worker_path, coverage_path))
    numbers = {n: i for i, n in enumerate(full["tests"])}
    if numbers.keys() != set(index["tests"]):
        raise ValueError("Incomplete pristine suite")
    definitions = json.loads(Path(inventory_path).read_text())
    inventory = set(definitions["tables"])
    audit = Audit(inventory, borrowed_arguments(index["sources"]))
    for path, source in index["sources"].items():
        if path.endswith(".py"):
            audit.source_path = path
            audit.visit(ast.parse(source))
    startup = set(full["startup"]) | set(worker["startup"])
    keys = set(full["owners"]) | set(worker["owners"]) | startup
    allowed = inventory - audit.unsafe
    mutators = {
        k.split("|", 1)[0].removeprefix("pk:")
        for k in keys
        if k.startswith("pk:") and "|mutate|" in k
    }
    allowed -= mutators
    rows = {}
    start = []
    for key in sorted(keys):
        if not key.startswith("pk:") or key[3:].split("|", 1)[0] not in allowed:
            continue
        if key in startup:
            start.append(key[3:])
        slots = set(full["owners"].get(key, ()))
        slots.update(numbers[worker["tests"][i]] for i in worker["owners"].get(key, ()))
        if slots:
            rows[key[3:]] = sorted(slots)
    owners = {
        path: {cls: sorted(set(names) & allowed) for cls, names in classes.items()}
        for path, classes in definitions["owners"].items()
    }
    payload = {
        "tests": full["tests"],
        "tables": sorted(allowed),
        "owners": owners,
        "expression_classes": definitions["expression_classes"],
        "shape_blocked": sorted(allowed & audit.shape),
        "startup": start,
        "rows": rows,
    }
    with open(out, "wb") as f:
        with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as z:
            z.write(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    print(
        "eligible",
        sorted(allowed),
        "value-only",
        sorted(allowed & audit.shape),
        "raw rows",
        len(rows),
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
