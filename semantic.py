"""Use observed dependencies of the pinned program for changes to declarations and comparisons."""

import ast


def only_difference(before, after):
    """Find a single changed node and its enclosing syntax on each side."""
    if type(before) is not type(after):
        return [(before, after)]
    if isinstance(before, list):
        if len(before) != len(after):
            return [(before, after)]
        pairs = zip(before, after)
    elif isinstance(before, ast.AST):
        pairs = ((getattr(before, key), getattr(after, key)) for key in before._fields)
    else:
        return None if before == after else [(before, after)]
    difference = None
    for old, new in pairs:
        child = only_difference(old, new)
        if child:
            if difference:
                return [(before, after)]
            difference = child
    if difference and isinstance(before, ast.AST):
        difference.append((before, after))
    return difference


def same_equality(types, site):
    lhs = set(types.get(site + "L", ()))
    rhs = set(types.get(site + "R", ()))
    if not lhs or not rhs:
        return False
    if rhs == {"None"}:
        return lhs <= {"None", "identity", "builtin"} or all(
            x.startswith("enum:") or x in {"None", "identity", "builtin"} for x in lhs
        )
    if lhs == rhs and len(lhs) == 1 and next(iter(lhs)).startswith("enum:"):
        return True
    return lhs <= {"identity", "None"} and rhs <= {"identity", "None"}


def literal_call(node):
    if not isinstance(node.func, ast.Name) or any(kw.arg is None for kw in node.keywords):
        return None
    if not all(isinstance(v, ast.Constant) for v in [*node.args, *(kw.value for kw in node.keywords)]):
        return None
    return node.func.id, len(node.args), tuple(kw.arg for kw in node.keywords)


def closure_factory(path, name, tree, sources):
    # Trace a simple local function or one direct import to its definition.
    for decl in tree.body:
        if isinstance(decl, ast.ImportFrom) and decl.module and not decl.level:
            for alias in decl.names:
                if (alias.asname or alias.name) == name:
                    origin = sources.get(decl.module.replace(".", "/") + ".py")
                    if origin is None:
                        return False
                    name, tree = alias.name, ast.parse(origin)
                    break
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name != name or fn.decorator_list:
            continue
        body = fn.body
        if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
            return True
        if (len(body) == 2 and isinstance(body[0], ast.FunctionDef) and not body[0].decorator_list
            and not body[0].args.defaults and not any(body[0].args.kw_defaults) and isinstance(body[1], ast.Return)
            and isinstance(body[1].value, ast.Name) and body[1].value.id == body[0].name):
            return True
    return False


class SemanticProfile:
    def __init__(self, profile, numbers):
        self.types = profile["types"]
        self.slots = [numbers.get(name) for name in profile["tests"]]
        self.missing = set(numbers.values()).difference(self.slots)
        self.owners = profile["owners"]
        self.declared = set(profile["declared"])
        self.startup = set(profile["startup"])

    def consumers(self, key):
        return self.missing | {self.slots[slot] for slot in self.owners.get(key, ()) if self.slots[slot] is not None}

    def selection(self, path, original, updated, sources):
        old, new = ast.parse(original, type_comments=True), ast.parse(updated, type_comments=True)
        differences = only_difference(old, new)
        if not differences:
            return set()
        first, replacement = differences[0]
        parents = {type(a): (a, b) for a, b in differences if isinstance(a, ast.AST)}
        if (type(first), type(replacement)) in {
            (ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)
        } and ast.Compare in parents:
            expr, _ = parents[ast.Compare]
            if same_equality(self.types, f"{path}:{expr.lineno}:{expr.col_offset}"):
                return self.missing
        if not isinstance(first, (str, bool, int, float, type(None))):
            return None
        classes = [a for a, _ in reversed(differences) if isinstance(a, ast.ClassDef)]
        if not classes or any(isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) for a, _ in differences):
            return None
        if ast.Dict not in parents:
            return None
        dictionary, changed = parents[ast.Dict]
        if [ast.dump(key) if key else "" for key in dictionary.keys] != [ast.dump(key) if key else "" for key in changed.keys]:
            return None
        if ast.Call in parents:
            call, new_call = parents[ast.Call]
            if call not in dictionary.values:
                return None
            key = f"{path}:{call.lineno}:{call.col_offset}"
            spec = literal_call(call)
            if (spec and spec == literal_call(new_call) and key in self.declared and key not in self.startup
                and closure_factory(path, spec[0], old, sources)):
                return self.consumers(key)
        if type(first) is bool and type(replacement) is bool and path.startswith("sqlglot/expressions/"):
            assign = parents.get(ast.Assign)
            if assign:
                decl = assign[0]
                if (decl.value is dictionary and len(decl.targets) == 1 and isinstance(decl.targets[0], ast.Name)
                    and decl.targets[0].id == "arg_types"):
                    name = path[:-3].replace("/", ".") + "." + ".".join(cls.name for cls in classes)
                    key = "schema:" + name
                    if key in self.owners:
                        return self.consumers(key)
        return None
