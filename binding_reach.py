"""Trace a changed import alias or top-level literal to its actual global-name consumers."""

import ast
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from semantic import parsed, source_difference

REFLECT = {
    "eval",
    "exec",
    "globals",
    "locals",
    "vars",
    "dir",
    "get_type_hints",
    "_getframe",
    "currentframe",
}


def changed_names(source, updated):
    diff = source_difference(source, updated) or []
    tree = parsed(source)
    for a, b in diff:
        if a not in tree.body:
            continue
        if isinstance(a, ast.ImportFrom) and isinstance(b, ast.ImportFrom):
            if (a.level, a.module) != (b.level, b.module) or len(a.names) != len(
                b.names
            ):
                return None
            changed = [
                (x, y) for x, y in zip(a.names, b.names) if ast.dump(x) != ast.dump(y)
            ]
            if len(changed) != 1:
                return None
            x, y = changed[0]
            if x.name != y.name or x.name == "*":
                return None
            names = {x.asname or x.name, y.asname or y.name}
        elif isinstance(a, (ast.Assign, ast.AnnAssign)) and type(a) is type(b):
            ax = a.targets if isinstance(a, ast.Assign) else [a.target]
            bx = b.targets if isinstance(b, ast.Assign) else [b.target]
            if (
                len(ax) != 1
                or len(bx) != 1
                or not isinstance(ax[0], ast.Name)
                or not isinstance(bx[0], ast.Name)
                or ax[0].id != bx[0].id
            ):
                return None
            if isinstance(a, ast.AnnAssign) and ast.dump(a.annotation) != ast.dump(
                b.annotation
            ):
                return None
            if not (
                isinstance(a.value, ast.Constant)
                and isinstance(b.value, ast.Constant)
                and type(a.value.value) is type(b.value.value)
                and type(a.value.value) in (str, bytes, int, bool, float, type(None))
            ):
                return None
            names = {ax[0].id}
        else:
            return None
        if any(n.startswith("_") for n in names):
            return None
        return names
    return None


@lru_cache(maxsize=64)
def local_reader_lines(source, names, class_sites=()):
    tree = parsed(source)
    lines = set()
    targets = set(names) | REFLECT
    class_sites = set(class_sites)

    declaration_read = False

    def visit(node, stmt=None, inside=False):
        nonlocal declaration_read
        if isinstance(node, ast.stmt):
            stmt = node
        if (
            (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in targets
            )
            or isinstance(node, ast.Attribute)
            and node.attr in REFLECT
        ):
            lines.update(range(node.lineno, node.end_lineno + 1))
            if stmt is not None:
                lines.add(stmt.lineno)
            if not inside:
                declaration_read = True
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(node, ast.Call)
                and child is node.func
                and isinstance(child, ast.Name)
                and child.id in {"vars", "dir"}
                and child.id not in names
                and (node.lineno, node.col_offset) in class_sites
            ):
                continue
            starts_body = (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child in node.body
            )
            visit(child, stmt, inside or starts_body)

    visit(tree)
    return lines, declaration_read


def cold_import(project, path, updated, module, timeout):
    from replay import mirror
    with TemporaryDirectory(prefix="br-import-") as scratch:
        root=Path(scratch)
        mirror(project,root,{path:updated})
        code="import importlib,sys;sys.path.insert(0,sys.argv[1]);importlib.import_module(sys.argv[2]);from sqlglot.dialects.dialect import Dialect;list(Dialect.classes.values())"
        run=subprocess.run([sys.executable,"-B","-c",code,str(root),module],cwd=root,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout)
        return run.returncode==0


class BindingProfile:
    def __init__(self, data, numbers, project=None):
        self.readers = data["readers"]
        self.startup = set(data["startup"])
        self.ids = [numbers.get(n) for n in data["tests"]]
        self.unseen = set(numbers.values()) - set(self.ids)
        self.project = project
        self.import_seconds = 0.0
        self.exports = data.get("exports", {})
        self.static_attrs = set(data.get("static_attrs", ()))
        self.class_sites = {
            p: tuple(map(tuple, coords))
            for p, coords in data.get("class_sites", {}).items()
        }

    def accepts_import(self, path, updated, module):
        if (
            self.project is None
            or not self.project.is_dir()
            or self.import_seconds >= 7
        ):
            return False
        start = time.monotonic()
        try:
            return cold_import(self.project,path,updated,module,1.4)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        finally:
            self.import_seconds += time.monotonic() - start

    def selection(self, path, source, updated, selector):
        names = changed_names(source, updated)
        if not names:
            return None
        module = path[:-3].replace("/", ".").removesuffix(".__init__")
        if module not in self.readers:
            return None
        watched = names | {"__dict__", "__all__"}
        if any(module + ":" + name in self.startup for name in watched):
            return None
        lines, declared = local_reader_lines(
            source, tuple(sorted(names)), self.class_sites.get(path, ())
        )
        exports = set(self.exports.get(module, ()))
        if (
            declared
            or "*" in exports
            or names & exports
            or names & self.static_attrs
            or lines.intersection(selector.startup.get(path, ()))
        ):
            return None
        if not self.accepts_import(path, updated, module):
            return None
        risk = (
            self.unseen
            | selector.untraced_reads
            | set(selector.source_readers.get(path, ()))
        )
        risk.update(selector.class_fixtures.get(path, ()))
        risk.update(selector.by_file.get(path, ()))
        for n in watched:
            risk.update(self.ids[j] for j in self.readers[module].get(n, ()))
        for line in lines:
            risk.update(
                selector.numbers_for(selector.lines.get(path, {}).get(line, ()))
            )
        for n, rec in selector.isolated.items():
            if not lines.isdisjoint(rec["lines"].get(path, ())):
                risk.add(n)
        risk.discard(None)
        return risk
