"""Resolve scalar tokenizer defaults using the consumers of their forwarded fields."""

import ast
from semantic import parsed, source_difference


class FlowProfile:
    def __init__(self, data, numbers):
        slots = [numbers.get(n) for n in data["tests"]]
        self.unseen = set(numbers.values()) - set(slots)
        self.rows = {
            k: {slots[i] for i in ids if slots[i] is not None}
            for k, ids in data["owners"].items()
        }

    def selection(self, path, source, updated):
        if path != "sqlglot/tokens.py" and not path.startswith("sqlglot/dialects/"):
            return None
        diff = source_difference(source, updated) or []
        parents = {type(a): a for a, b in diff if isinstance(a, ast.AST)}
        stmt = parents.get(ast.Assign) or parents.get(ast.AnnAssign)
        cls = next((a for a, b in diff if isinstance(a, ast.ClassDef)), None)
        if not cls or cls.name != "Tokenizer" or stmt not in cls.body:
            return None
        if not (
            isinstance(stmt.value, ast.Constant)
            and type(stmt.value.value) in (bool, str)
        ):
            return None
        replacement = next((b for a, b in diff if a is stmt.value), None)
        if not isinstance(replacement, ast.Constant) or type(replacement.value) != type(
            stmt.value.value
        ):
            return None
        lhs = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if (
            len(lhs) != 1
            or not isinstance(lhs[0], ast.Name)
            or lhs[0].id not in self.rows
        ):
            return None
        return self.unseen | self.rows[lhs[0].id]
