"""Follow a boolean schema change through missing-field checks in the original tests."""

import ast
from semantic import source_difference


class SchemaChecks:
    def __init__(self, profile, numbers):
        slots = [numbers.get(node) for node in profile['tests']]
        self.unknown = set(numbers.values()) - set(slots)
        self.rows = {k:{slots[i] for i in group if slots[i] is not None}
                     for k, group in profile['rows'].items()}
        self.blocked = set(profile['startup'])

    def selection(self, path, source, updated, selector):
        if not path.startswith('sqlglot/expressions/') or path.endswith('/__init__.py'):
            return None
        diff = source_difference(source, updated) or []
        if not diff or type(diff[0][0]) is not bool or type(diff[0][1]) is not bool:
            return None
        parents = {type(a): (a,b) for a,b in diff if isinstance(a,ast.AST)}
        if not all(k in parents for k in (ast.Constant, ast.Dict, ast.ClassDef, ast.Assign, ast.Module)):
            return None
        old, new = parents[ast.Dict]
        cls = parents[ast.ClassDef][0]
        stmt = parents[ast.Assign][0]
        module = parents[ast.Module][0]
        value = parents[ast.Constant][0]
        if (cls not in module.body or stmt not in cls.body or stmt.value is not old or
            len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name) or
            stmt.targets[0].id != 'arg_types' or value not in old.values):
            return None
        if (not all(isinstance(k,ast.Constant) and type(k.value) is str for k in old.keys)
            or not all(isinstance(v,ast.Constant) and type(v.value) is bool for v in old.values)):
            return None
        keys = [k.value for k in old.keys]
        if len(set(keys)) != len(keys):
            return None
        name = path[:-3].replace('/', '.') + '.' + cls.name
        key = name + '|' + keys[old.values.index(value)]
        wildcard = name + '|*'
        if (key in self.blocked or wildcard in self.blocked or selector.semantic is None or
            'schema:' + name not in selector.semantic.owners):
            return None
        return self.unknown | self.rows.get(key, set()) | self.rows.get(wildcard, set())
