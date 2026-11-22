"""Combine original execution footprints of changed function bodies and class inputs."""

import ast
import re

from private_helper import footprint


OPTION_LINE = re.compile(r'''^[-+]\s+([A-Z][A-Z_0-9]*)\s*(?::[^=\n]+)?=\s*(?:["'0-9]|True\b|False\b)''')
HELPER_LINE = re.compile(r"(?m)^[-+]def _[a-zA-Z]")
SCALAR = (bool, int, float, str)


class ClassInputs(ast.NodeTransformer):
    def __init__(self, module, functions):
        self.module = module
        self.scope = []
        self.values = {}
        self.functions = functions()

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign):
                target = stmt.target
            else:
                continue
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            if not isinstance(stmt.value, ast.Constant) or type(stmt.value.value) not in SCALAR:
                continue
            key = '.'.join((self.module, *self.scope, target.id))
            if key in self.values:
                raise ValueError('ambiguous repeated class declaration')
            self.values[key] = (type(stmt.value.value), stmt.value.value)
            stmt.value = ast.Constant(None)
        self.generic_visit(node)
        self.scope.pop()
        return node

    def visit_FunctionDef(self, node):
        return self.functions.visit_FunctionDef(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        return self.functions.visit_Lambda(node)


def observed_bodies(path, spans, selector):
    seen = set()
    startup = selector.startup.get(path, ())
    lines = selector.lines.get(path, {})
    for start, end in spans:
        if any(start <= line <= end for line in startup):
            seen.update(selector.structural_risk(path, [(start, end)]))
        else:
            slots = {i for line, readers in lines.items() if start <= line <= end for i in readers}
            seen.update(selector.numbers_for(slots))
            for number, row in selector.isolated.items():
                if any(start <= line <= end for line in row['lines'].get(path, ())):
                    seen.add(number)
    return seen


def class_risk(path, module, source, updated, selector, bodies):
    before, after = ClassInputs(module, bodies), ClassInputs(module, bodies)
    old, new = (visitor.visit(ast.parse(text, type_comments=True))
                for visitor, text in ((before, source), (after, updated)))
    if ast.dump(old) != ast.dump(new) or before.values.keys() != after.values.keys():
        return None
    keys = [key for key in before.values if before.values[key] != after.values[key]]
    if not keys or any(before.values[k][0] is not after.values[k][0] for k in keys):
        return None
    semantic = selector.semantic
    if semantic is None or any('option:' + k not in semantic.owners for k in keys):
        return None
    spans = [(start, end) for (start, end, v), (_, _, w) in
             zip(before.functions.bodies, after.functions.bodies, strict=True) if v != w]
    risk = observed_bodies(path, spans, selector)
    for k in keys:
        risk.update(semantic.consumers('option:' + k))
    return risk


def helper_risk(path, module, source, updated, selector, bodies):
    change = footprint(source, updated, bodies)
    binding = selector.binding
    if change is None or binding is None or module not in binding.readers:
        return None
    names, spans = change
    watched = names | {'__dict__', '__all__'}
    exports = set(binding.exports.get(module, ()))
    if ('*' in exports or names & exports or
        any(module + ':' + n in binding.startup for n in watched) or
        any(any(n in text for n in names) for p, text in selector.sources.items() if p != path)):
        return None
    if not binding.accepts_import(path, updated, module):
        return None
    risk = observed_bodies(path, spans, selector) | binding.unseen
    for n in watched:
        risk.update(binding.ids[i] for i in binding.readers[module].get(n, ()))
    risk.discard(None)
    return risk


def selection(path, hunks, source, updated, selector, bodies):
    option = any(OPTION_LINE.match(line) for line in hunks)
    private = any(HELPER_LINE.match(line) for line in hunks)
    if not option and not private:
        return None
    if path not in selector.sources or not path.startswith('sqlglot/') or path.endswith('/__init__.py'):
        return None
    module = path[:-3].replace('/', '.')
    risk = class_risk(path, module, source, updated, selector, bodies) if option else None
    if risk is None and private:
        risk = helper_risk(path, module, source, updated, selector, bodies)
    if risk is None:
        return None
    return (risk | selector.unprofiled | selector.untraced_reads |
            set(selector.source_readers.get(path, ())) | set(selector.class_fixtures.get(path, ())))
