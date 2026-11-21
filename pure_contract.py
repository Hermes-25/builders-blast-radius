"""Restricted, side-effect-free Python helpers whose original and patched calls can be compared."""

import ast

BUILTINS = {
    "len",
    "isinstance",
    "int",
    "float",
    "str",
    "bool",
    "list",
    "tuple",
    "min",
    "max",
    "abs",
    "IndexError",
    "TypeError",
    "ValueError",
    "KeyError",
}
METHODS = {
    "lower",
    "upper",
    "strip",
    "lstrip",
    "rstrip",
    "startswith",
    "endswith",
    "find",
    "count",
    "capitalize",
    "casefold",
    "isalnum",
    "isdigit",
    "isidentifier",
}
ALLOWED = (
    ast.Return,
    ast.If,
    ast.Try,
    ast.ExceptHandler,
    ast.Assign,
    ast.AnnAssign,
    ast.Pass,
    ast.Expr,
    ast.Name,
    ast.Constant,
    ast.Subscript,
    ast.Slice,
    ast.Load,
    ast.Store,
    ast.Tuple,
    ast.List,
    ast.IfExp,
    ast.Compare,
    ast.cmpop,
    ast.BoolOp,
    ast.boolop,
    ast.UnaryOp,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.FloorDiv,
    ast.Mod,
    ast.Call,
    ast.Attribute,
    ast.keyword,
)


def signature(fn):
    args = fn.args
    if (
        fn.decorator_list
        or args.vararg
        or args.kwarg
        or args.kwonlyargs
        or args.posonlyargs
    ):
        return None
    names = [a.arg for a in args.args]
    return tuple(names) if 1 <= len(names) <= 4 else None


def allowed(fn):
    names = signature(fn)
    if not names:
        return False
    groups = [
        s
        for s in fn.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    nodes = [n for s in groups for n in ast.walk(s)]
    if len(nodes) > 145 or not any(isinstance(n, ast.Return) for n in nodes):
        return False
    local = set(names) | {
        n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }
    for n in nodes:
        if not isinstance(n, ALLOWED):
            return False
        if isinstance(n, ast.Name) and n.id not in local | BUILTINS:
            return False
        if isinstance(n, ast.Attribute) and n.attr not in METHODS:
            return False
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name) and n.func.id not in BUILTINS:
                return False
            if not isinstance(n.func, (ast.Name, ast.Attribute)) or any(
                k.arg is None for k in n.keywords
            ):
                return False
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if not all(isinstance(t, ast.Name) for t in targets):
                return False
        if isinstance(n, ast.ExceptHandler):
            if (
                not isinstance(n.type, ast.Name)
                or n.type.id
                not in {"IndexError", "TypeError", "ValueError", "KeyError"}
                or n.name
            ):
                return False
        if isinstance(n, ast.Compare):
            for op, right in zip(n.ops, n.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)) and not (
                    isinstance(right, ast.Constant)
                    and (right.value is None or type(right.value) is bool)
                ):
                    return False
        if isinstance(n, ast.Constant):
            v = n.value
            if type(v) is int and abs(v) > 100000:
                return False
            if (
                type(v)
                not in (str, bytes, int, float, bool, type(None), type(Ellipsis))
                or type(v) in (str, bytes)
                and len(v) > 128
            ):
                return False
        if isinstance(n, ast.BinOp) and not isinstance(n.right, ast.Constant):
            return False
        if isinstance(n, ast.BinOp) and type(n.right.value) is not int:
            return False
    return True


def calls_in_source(root):
    out = {}
    for p in sorted((root / "sqlglot").rglob("*.py")):
        tree = ast.parse(p.read_bytes())
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for fn in fns.values():
            if allowed(fn):
                out[(p.relative_to(root).as_posix(), fn.name)] = fn
    return out
