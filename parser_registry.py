"""Follow literal changes to materialized parser dispatch maps, including missing keys."""

import ast
import subprocess
import time
from binding_reach import cold_import
from semantic import parsed, source_difference


def key_name(node):
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and len(node.value) <= 128
    ):
        return "s:" + node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "TokenType"
    ):
        return "t:" + node.attr
    return None


def reference(node, expression_classes):
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "exp"
            and node.attr in expression_classes
        )
    if isinstance(node, ast.Lambda):
        return (
            not node.args.defaults
            and not any(node.args.kw_defaults)
            and not any(
                isinstance(name, ast.Name) and name.id in {"__class__", "super"}
                for name in ast.walk(node)
            )
        )
    return False


def entries(dictionary):
    result = {}
    order = []
    for key, value in zip(dictionary.keys, dictionary.values):
        if key is None:
            # Spreads have to retain their identity and relative position.
            tag = ("spread", ast.dump(value))
        else:
            tag = key_name(key) or ("other", ast.dump(key))
        if tag in result:
            return None
        result[tag] = value
        order.append(tag)
    return result, order


def changed_entries(path, source, updated, expression_classes):
    parts = path.split("/")
    if (
        len(parts) != 3
        or parts[:2] != ["sqlglot", "parsers"]
        or parts[-1] in {"base.py", "__init__.py"}
    ):
        return None
    changes = source_difference(source, updated) or []
    cls = next((a for a, b in changes if isinstance(a, ast.ClassDef)), None)
    if cls is None or cls not in parsed(source).body:
        return None
    for a, b in changes:
        if (
            not isinstance(a, (ast.Assign, ast.AnnAssign))
            or a not in cls.body
            or type(a) is not type(b)
        ):
            continue
        old_targets = a.targets if isinstance(a, ast.Assign) else [a.target]
        new_targets = b.targets if isinstance(b, ast.Assign) else [b.target]
        if (
            len(old_targets) != 1
            or len(new_targets) != 1
            or not isinstance(old_targets[0], ast.Name)
            or ast.dump(old_targets[0]) != ast.dump(new_targets[0])
            or not isinstance(a.value, ast.Dict)
            or not isinstance(b.value, ast.Dict)
        ):
            return None
        if isinstance(a, ast.AnnAssign) and ast.dump(a.annotation) != ast.dump(
            b.annotation
        ):
            return None
        old, new = entries(a.value), entries(b.value)
        if not old or not new:
            return None
        previous, order = old
        current, new_order = new
        if [x for x in order if x in current] != [
            x for x in new_order if x in previous
        ]:
            return None
        keys = previous.keys() ^ current.keys()
        keys.update(
            k
            for k in previous.keys() & current.keys()
            if ast.dump(previous[k]) != ast.dump(current[k])
        )
        if not 1 <= len(keys) <= 8 or not all(
            isinstance(k, str)
            and (k not in previous or reference(previous[k], expression_classes))
            and (k not in current or reference(current[k], expression_classes))
            for k in keys
        ):
            return None
        return cls.name, old_targets[0].id, keys, previous.keys() != current.keys()
    return None


class ParserRegistry:
    def __init__(self, profile, numbers):
        self.owners = profile["owners"]
        self.expression_classes = set(profile["expression_classes"])
        self.shape_blocked = set(profile["shape_blocked"])
        self.startup = set(profile["startup"])
        self.rows = profile["rows"]
        self.slots = [numbers.get(n) for n in profile["tests"]]
        self.unseen = set(numbers.values()) - set(self.slots)
        self.probe_seconds = 0.0

    def selection(self, path, source, updated, selector):
        changed = changed_entries(path, source, updated, self.expression_classes)
        if changed is None:
            return None
        cls, name, keys, shape = changed
        if (
            name not in self.owners.get(path, {}).get(cls, ())
            or shape
            and name in self.shape_blocked
        ):
            return None
        modes = ("read", "has") if shape else ("read",)
        wanted = {
            name + "|" + kind + "|" + key for kind in modes for key in {*keys, "*"}
        }
        if (
            self.startup.intersection(wanted)
            or selector.binding is None
            or selector.binding.project is None
            or self.probe_seconds >= 12
        ):
            return None
        module = path[:-3].replace("/", ".")
        start = time.monotonic()
        try:
            verified = cold_import(
                selector.binding.project,
                path,
                updated,
                module,
                min(5, 12 - self.probe_seconds),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            verified = False
        finally:
            self.probe_seconds += time.monotonic() - start
        if not verified:
            return None
        selected = self.unseen | selector.untraced_reads | selector.unprofiled
        selected.update(selector.source_readers.get(path, ()))
        selected.update(selector.class_fixtures.get(path, ()))
        for key in wanted:
            selected.update(self.slots[i] for i in self.rows.get(key, ()))
        selected.discard(None)
        return selected
