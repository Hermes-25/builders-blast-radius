"""Use observed dependencies of the pinned program for changes to declarations and comparisons."""

import ast
from functools import lru_cache


@lru_cache(maxsize=12)
def parsed(source):
    return ast.parse(source, type_comments=True)


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
    if not isinstance(node.func, ast.Name) or any(
        kw.arg is None for kw in node.keywords
    ):
        return None
    if not all(
        isinstance(v, ast.Constant)
        for v in [*node.args, *(kw.value for kw in node.keywords)]
    ):
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
        if (
            len(body) == 1
            and isinstance(body[0], ast.Return)
            and isinstance(body[0].value, ast.Lambda)
        ):
            return True
        if (
            len(body) == 2
            and isinstance(body[0], ast.FunctionDef)
            and not body[0].decorator_list
            and not body[0].args.defaults
            and not any(body[0].args.kw_defaults)
            and isinstance(body[1], ast.Return)
            and isinstance(body[1].value, ast.Name)
            and body[1].value.id == body[0].name
        ):
            return True
    return False


def option_dependency(path, differences):
    """Identify a single literal option edit on a production class."""
    if not path.startswith("sqlglot/"):
        return None
    classes = []
    pair = None
    for old, new in reversed(differences):
        if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return None
        if isinstance(old, ast.ClassDef):
            classes.append(old.name)
        if isinstance(old, ast.Assign) and isinstance(new, ast.Assign):
            pair = old, new
    if not classes or not pair:
        return None
    before, after = pair
    if len(before.targets) != 1 or len(after.targets) != 1:
        return None
    lhs, rhs = before.targets[0], after.targets[0]
    if (
        not isinstance(lhs, ast.Name)
        or not isinstance(rhs, ast.Name)
        or not lhs.id.isupper()
        or lhs.id != rhs.id
    ):
        return None
    try:
        old_value = ast.literal_eval(before.value)
        new_value = ast.literal_eval(after.value)
    except (ValueError, TypeError):
        return None
    if type(old_value) is not type(new_value) or not isinstance(
        old_value, (bool, str, int, float)
    ):
        return None
    return "option:" + ".".join((path[:-3].replace("/", "."), *classes, lhs.id))


class SemanticProfile:
    def __init__(self, profile, numbers):
        self.types = profile["types"]
        self.slots = [numbers.get(name) for name in profile["tests"]]
        self.missing = set(numbers.values()).difference(self.slots)
        self.owners = profile["owners"]
        self.declared = set(profile["declared"])
        self.startup = set(profile["startup"])

    def consumers(self, key):
        return self.missing | {
            self.slots[slot]
            for slot in self.owners.get(key, ())
            if self.slots[slot] is not None
        }

    def selection(self, path, original, updated, sources):
        old, new = parsed(original), parsed(updated)
        differences = only_difference(old, new)
        if not differences:
            return set()
        first, replacement = differences[0]
        parents = {type(a): (a, b) for a, b in differences if isinstance(a, ast.AST)}
        if (type(first), type(replacement)) in {
            (ast.Is, ast.Eq),
            (ast.Eq, ast.Is),
            (ast.IsNot, ast.NotEq),
            (ast.NotEq, ast.IsNot),
        } and ast.Compare in parents:
            expr, _ = parents[ast.Compare]
            if same_equality(self.types, f"{path}:{expr.lineno}:{expr.col_offset}"):
                return self.missing
        option = option_dependency(path, differences)
        if option in self.owners:
            return self.consumers(option)
        if ast.Constant in parents and type(first) is type(replacement) and isinstance(
            first, (bool, int, float, str)
        ):
            for old_node, new_node in differences:
                if not isinstance(old_node, ast.Lambda) or not isinstance(new_node, ast.Lambda):
                    continue
                if ast.dump(old_node.args) != ast.dump(new_node.args):
                    continue
                callback = f"lambda:{path}:{old_node.lineno}"
                if callback in self.owners:
                    return self.consumers(callback)
        if not isinstance(first, (str, bool, int, float, type(None))):
            return None
        classes = [a for a, _ in reversed(differences) if isinstance(a, ast.ClassDef)]
        if not classes or any(
            isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            for a, _ in differences
        ):
            return None
        if ast.Dict not in parents:
            return None
        dictionary, changed = parents[ast.Dict]
        if [ast.dump(key) if key else "" for key in dictionary.keys] != [
            ast.dump(key) if key else "" for key in changed.keys
        ]:
            return None
        if ast.Call in parents:
            call, new_call = parents[ast.Call]
            if call not in dictionary.values:
                return None
            key = f"{path}:{call.lineno}:{call.col_offset}"
            spec = literal_call(call)
            if (
                spec
                and spec == literal_call(new_call)
                and key in self.declared
                and key not in self.startup
                and closure_factory(path, spec[0], old, sources)
            ):
                return self.consumers(key)
        if (
            type(first) is bool
            and type(replacement) is bool
            and path.startswith("sqlglot/expressions/")
        ):
            assign = parents.get(ast.Assign)
            if assign:
                decl = assign[0]
                if (
                    decl.value is dictionary
                    and len(decl.targets) == 1
                    and isinstance(decl.targets[0], ast.Name)
                    and decl.targets[0].id == "arg_types"
                ):
                    name = (
                        path[:-3].replace("/", ".")
                        + "."
                        + ".".join(cls.name for cls in classes)
                    )
                    key = "schema:" + name
                    if key in self.owners:
                        return self.consumers(key)
        return None


def statement_span(original, updated):
    """Return the old executable statement containing a scope-preserving change."""
    diff = only_difference(parsed(original), parsed(updated))
    if not diff or not any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n, _ in diff
    ):
        return None
    stmt = next(((a, b) for a, b in diff if isinstance(a, ast.stmt)), None)
    if not stmt:
        return None
    old, new = stmt
    if type(old) is not type(new):
        return None
    if isinstance(old, (ast.If, ast.While)):
        if not any(a is old.test and b is new.test for a, b in diff):
            return None
        bounds = old.lineno, old.test.end_lineno
    elif isinstance(
        old, (ast.Assign, ast.AugAssign, ast.Return, ast.Expr, ast.Assert, ast.Raise)
    ):
        bounds = old.lineno, old.end_lineno
    else:
        return None

    def scope(node):
        return sorted(
            (type(n).__name__, getattr(n, "id", ""))
            for n in ast.walk(node)
            if isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await))
            or isinstance(n, ast.Name)
            and isinstance(n.ctx, (ast.Store, ast.Del))
        )

    return bounds if scope(old) == scope(new) else None
