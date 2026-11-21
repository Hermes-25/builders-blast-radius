"""Identify registry names with complete coverage across every original concrete parser."""

import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from sqlglot import Parser, exp
from sqlglot.dialects.dialect import Dialect
from build_parser_keys import encode

bases = {
    c
    for dialect in Dialect.classes.values()
    for c in dialect.parser_class.__mro__
    if issubclass(c, Parser)
}
seen = set()
blocked = set()
owners = {}
for base in bases:
    module = sys.modules[base.__module__]
    path = Path(module.__file__).resolve().relative_to(root).as_posix()
    declarations = []
    for name, value in vars(base).items():
        if not name.isupper():
            continue
        if type(value) is dict:
            seen.add(name)
            declarations.append(name)
        if type(value) is not dict or not value or any(encode(k) == "*" for k in value):
            blocked.add(name)
    if (
        path.startswith("sqlglot/parsers/")
        and getattr(module, "exp", None) is exp
        and "exp" not in vars(base)
    ):
        owners.setdefault(path, {})[base.__name__] = sorted(declarations)
classes = sorted(
    name
    for name, value in vars(exp).items()
    if isinstance(value, type) and issubclass(value, exp.Expression)
)
Path(sys.argv[2]).write_text(
    json.dumps(
        {
            "tables": sorted(seen - blocked),
            "owners": owners,
            "expression_classes": classes,
        },
        sort_keys=True,
    )
)
