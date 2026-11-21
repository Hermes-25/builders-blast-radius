"""Use pristine branch-entry dependencies for a scope-preserving single-line replacement."""

import ast
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
SIMPLE = (
    ast.Return,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Raise,
    ast.Assert,
    ast.Expr,
    ast.If,
    ast.While,
)
DEFERRED = (
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.Lambda,
    ast.GeneratorExp,
    ast.NamedExpr,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
)


def shape(line):
    line = line.strip()
    if not line or ";" in line or "\\" in line:
        return None
    if line.startswith("elif "):
        line = "if " + line[5:]
    if line.startswith(("if ", "while ")):
        if not line.endswith(":"):
            return None
        line += "\n    pass"
    module = ast.parse(line, type_comments=True)
    if len(module.body) != 1 or not isinstance(module.body[0], SIMPLE):
        return None
    stmt = module.body[0]
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return None
    if any(
        isinstance(x, DEFERRED)
        or isinstance(x, ast.Name)
        and x.id in {"super", "__class__"}
        for x in ast.walk(stmt)
    ):
        return None
    bindings = sorted(
        (type(x.ctx).__name__, x.id)
        for x in ast.walk(stmt)
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del))
    )
    return type(stmt).__name__, bindings


def replacement_lines(hunks):
    cursor = 0
    removed = []
    added = []
    pairs = []
    for line in [*hunks, " "]:
        if line.startswith("-"):
            removed.append((cursor, line[1:]))
            cursor += 1
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            if len(removed) != len(added):
                return None
            pairs.extend((num, old, new) for (num, old), new in zip(removed, added))
            removed.clear()
            added.clear()
            match = HUNK.match(line)
            if match:
                cursor = int(match[1])
            elif line.startswith(" "):
                cursor += 1
    return pairs


class LineGate:
    def __init__(self, data, numbers):
        slots = [numbers.get(n) for n in data["tests"]]
        self.unknown = set(numbers.values()) - set(slots)
        self.slots = slots
        self.groups = data["groups"]
        self.cache = {}
        self.original_lines = {}
        self.rows = data["lines"]

    def selection(self, sections, selector):
        if not 1 <= len(sections) <= 5 or len({p for p, _ in sections}) != len(
            sections
        ):
            return None
        risk = self.unknown | selector.unprofiled | selector.untraced_reads
        proven = False
        for path, hunks in sections:
            risk.update(selector.source_readers.get(path, ()))
            risk.update(selector.class_fixtures.get(path, ()))
            if path.startswith("tests/fixtures/") and path.endswith(".sql"):
                readers = selector.numbers_for(selector.reads.get(path, ()))
                if not readers:
                    return None
                risk.update(readers)
                continue
            rows = self.rows.get(path)
            if not rows:
                return None
            edits = replacement_lines(hunks)
            if not edits or len(edits) > 12:
                return None
            if path not in self.original_lines:
                self.original_lines[path] = selector.sources[path].splitlines()
            lines = self.original_lines[path]
            for num, old, new in edits:
                if not 0 < num <= len(lines) or old != lines[num - 1]:
                    return None
                if old == new:
                    continue
                if old.lstrip().startswith("#") and new.lstrip().startswith("#"):
                    continue
                group = rows.get(str(num))
                indent = len(old) - len(old.lstrip())
                if group is None or old[:indent] != new[: len(new) - len(new.lstrip())]:
                    return None
                expected = shape(old)
                if not expected or expected != shape(new):
                    return None
                proven = True
                if group not in self.cache:
                    self.cache[group] = frozenset(
                        self.slots[i]
                        for i in self.groups[group]
                        if self.slots[i] is not None
                    )
                risk.update(self.cache[group])
        return risk if proven else None
