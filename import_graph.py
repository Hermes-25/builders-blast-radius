"""Map a proven module-initialization failure to its cold imports and dialect clients."""

import ast
import builtins
from collections import defaultdict
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from semantic import parsed, source_difference


COMPONENTS = {"dialects", "generators", "parsers", "typing"}
PROBE = """
import importlib, sys
sys.path.insert(0, sys.argv[1])
try:
    importlib.import_module(sys.argv[2])
except NameError as exc:
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_globals.get('__name__') == sys.argv[2] and exc.name == sys.argv[3]:
            raise SystemExit(73)
        tb = tb.tb_next
"""


def removed_alias(source, updated):
    tree = parsed(source)
    for old, new in source_difference(source, updated) or ():
        if not isinstance(old, ast.ImportFrom) or old not in tree.body or not isinstance(new, ast.ImportFrom):
            continue
        if (old.module, old.level, len(old.names)) != (new.module, new.level, len(new.names)):
            return None
        edits = [(left, right) for left, right in zip(old.names, new.names) if ast.dump(left) != ast.dump(right)]
        if len(edits) == 1:
            left, right = edits[0]
            if left.name == right.name and left.name != "*":
                added = right.asname or right.name
                if hasattr(builtins, added):
                    return None
                # A collision could change initialization before the old
                # name is first read. Require a completely fresh binding.
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id == added:
                        return None
                    if isinstance(node, ast.alias) and (node.asname or node.name) == added:
                        return None
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == added:
                        return None
                return left.asname or left.name
    return None


class ImportGraph:
    def __init__(self, profile, tests, project):
        self.project = project
        self.confirmed_path = None
        self.spent = 0.0
        self.reverse = defaultdict(set)
        self.clients = defaultdict(set)
        self.unknown = set()
        for importer, dependencies in profile["edges"].items():
            for dependency in dependencies:
                self.reverse[dependency].add(importer)
        for test in tests:
            number, nodeid = test["number"], test["id"]
            groups = profile["modules"].get(test["file"], {})
            if nodeid not in groups or test["file"] in profile["unknown"]:
                self.unknown.add(number)
                continue
            scopes = ("*", "class:" + nodeid.rsplit("::", 1)[0], nodeid)
            for scope in scopes:
                for module in groups.get(scope, ()):
                    self.clients[module].add(number)

    def confirms(self, path, updated, module, name):
        if self.spent >= 5.0 or not self.project.is_dir():
            return False
        from replay import mirror
        start = time.monotonic()
        try:
            with TemporaryDirectory(prefix="br-graph-") as scratch:
                root = Path(scratch)
                mirror(self.project, root, {path: updated})
                run = subprocess.run([sys.executable, "-B", "-c", PROBE, str(root), module, name],
                                     cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=1.5)
                return run.returncode == 73
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        finally:
            self.spent += time.monotonic() - start

    def selection(self, path, source, updated, selector):
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "sqlglot" or parts[1] not in COMPONENTS:
            return None
        if parts[2] in {"__init__.py", "dialect.py"} or not parts[2].endswith(".py"):
            return None
        name = removed_alias(source, updated)
        module = path[:-3].replace("/", ".")
        if not name or module not in self.clients or not self.confirms(path, updated, module, name):
            return None
        self.confirmed_path = path
        unavailable = {module}
        pending = [module]
        while pending:
            further = self.reverse[pending.pop()] - unavailable
            pending.extend(further)
            unavailable.update(further)
        risk = self.unknown | selector.untraced_reads | set(selector.source_readers.get(path, ()))
        risk.update(selector.class_fixtures.get(path, ()))
        for name in unavailable:
            risk.update(self.clients.get(name, ()))
            risk.update(selector.file_tests.get(name.replace(".", "/") + ".py", ()))
        return risk
