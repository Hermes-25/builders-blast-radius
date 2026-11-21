"""Combine external module reads in the pristine serial and worker suites."""

import ast, gzip, json, sys
from collections import defaultdict


def load(p):
    with gzip.open(p, "rt") as stream:
        return json.load(stream)


full, workers = map(load, sys.argv[1:3])
tests = full["tests"]
slots = {n: i for i, n in enumerate(tests)}
reads = defaultdict(lambda: defaultdict(set))
startup = set()
for prof in (full, workers):
    for key in prof["startup"]:
        if key.startswith("s:"):
            startup.add(key[2:])
    for key, items in prof["owners"].items():
        if key.startswith("r:"):
            module, attr = key[2:].split(":", 1)
            reads[module][attr].update(slots[prof["tests"][i]] for i in items)
exports = defaultdict(set)
static_attrs = set()
for path, source in load(sys.argv[4])["sources"].items():
    if not path.endswith(".py"):
        continue

    def visit(node, inside=False):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                package = path[:-3].split("/")[:-1]
                prefix = package[: len(package) - node.level + 1]
                module = ".".join([*prefix, node.module])
            else:
                module = node.module
            exports[module].update(a.name for a in node.names)
        if isinstance(node, ast.Attribute) and not inside:
            static_attrs.add(node.attr)
        for child in ast.iter_child_nodes(node):
            starts = (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child in node.body
            )
            visit(child, inside or starts)

    visit(ast.parse(source))
with gzip.GzipFile(sys.argv[3], "wb", mtime=0) as stream:
    stream.write(
        json.dumps(
            {
                "tests": tests,
                "readers": {
                    m: {a: sorted(ids) for a, ids in d.items()}
                    for m, d in reads.items()
                },
                "startup": sorted(startup),
                "exports": {
                    module: sorted(names)
                    for module, names in exports.items()
                    if module.startswith("sqlglot")
                },
                "static_attrs": sorted(static_attrs),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
print("modules", len(reads), "startup attributes", len(startup))
