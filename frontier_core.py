"""Stable object-state fingerprints across an executor pipeline boundary."""

import enum, hashlib, marshal, collections


def ensure_digest(expr):
    from sqlglot.expressions import Expr

    slots = (
        "args",
        "parent",
        "arg_key",
        "index",
        "comments",
        "_type",
        "_meta",
        "_hash",
    )
    seen = {}
    pending = []
    objects = []

    def encode(x):
        if x is None or type(x) in (str, int, float, bool, bytes):
            return (type(x).__name__, x)
        if isinstance(x, enum.Enum):
            return ("enum", type(x).__module__, type(x).__qualname__, x.name)
        if isinstance(x, Expr):
            ident = id(x)
            if ident not in seen:
                seen[ident] = len(seen)
                pending.append(x)
            return ("node", seen[ident])
        if type(x) in (list, tuple):
            return (type(x).__name__, tuple(encode(z) for z in x))
        if type(x) is dict:
            return ("dict", tuple((encode(k), encode(v)) for k, v in x.items()))
        raise ValueError(f"Unsupported snapshot value {type(x)}")

    encode(expr)
    i = 0
    while i < len(pending):
        item = pending[i]
        i += 1
        values = tuple(encode(object.__getattribute__(item, field)) for field in slots)
        state = getattr(item, "__dict__", {})
        objects.append(
            (type(item).__module__, type(item).__qualname__, values, encode(state))
        )
    # repr is deterministic for the primitive tuples generated here. marshal may encode shared primitive identities.
    return hashlib.sha256(repr(objects).encode()).hexdigest()


def parse_packet(sql, meta):
    import pickle
    from sqlglot import parse_one

    return pickle.loads(pickle.dumps((parse_one(sql), meta)))[0]


def optimize_for_executor(expression, schema):
    from sqlglot.schema import ensure_schema
    from sqlglot.optimizer import optimize

    return optimize(
        expression, ensure_schema(schema), leave_tables_isolated=True, dialect=None
    )
