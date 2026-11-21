"""Bounded replay of real pinned SQLGlot unittest cases in fresh patched interpreters."""

import gc
import importlib
import importlib.util
import json
import types
import logging
import os
from pathlib import Path
import select
import signal
import sys
from tempfile import TemporaryDirectory
import time
import unittest


def mirror(base, dest, replacements):
    """Share unchanged files with the image while giving each mutation a fresh tree."""
    dest.mkdir(exist_ok=True)
    for source in base.iterdir():
        name = source.name
        target = dest / name
        if name == "__pycache__":
            changed = {Path(path).stem for path in replacements if "/" not in path}
            target.mkdir()
            for cached in source.iterdir():
                if cached.name.partition(".")[0] not in changed:
                    (target / cached.name).symlink_to(cached)
            continue
        if name in replacements:
            target.write_text(replacements[name], encoding="utf-8")
            continue
        prefix = name + "/"
        children = {
            p[len(prefix) :]: text
            for p, text in replacements.items()
            if p.startswith(prefix)
        }
        if children:
            mirror(source, target, children)
        else:
            target.symlink_to(source, target_is_directory=source.is_dir())


class Results(unittest.TestResult):
    def __init__(self, channel, ids, isolated):
        super().__init__()
        self.channel = channel
        self.ids = ids
        self.uncertain = set()
        self.isolated = isolated

    def emit(self, case, word):
        if not self.isolated[0]:
            return
        number = self.ids.get(case.id())
        if number is not None:
            os.write(self.channel, f"{word} {number}\n".encode())

    def addSuccess(self, test):
        if test.id() not in self.uncertain:
            self.emit(test, "pass")

    def addFailure(self, test, err):
        self.emit(test, "fail")

    def addError(self, test, err):
        self.emit(test, "fail")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.emit(test, "fail")

    def addSkip(self, test, reason):
        self.uncertain.add(test.id())
        parent = getattr(test, "test_case", None)
        if parent is not None:
            self.uncertain.add(parent.id())


def module_name(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")


class PatchedImports:
    def __init__(self, root, replacements):
        self.paths = {module_name(path): root / path for path in replacements}

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.paths:
            return None
        location = self.paths[fullname]
        options = (
            {"submodule_search_locations": [str(location.parent)]}
            if location.name == "__init__.py"
            else {}
        )
        return importlib.util.spec_from_file_location(fullname, location, **options)


def install_in_child(base, root, replacements, safe_hot):
    """Replace live, non-startup function bodies while preserving aliased function identity."""
    needs_hot = {path for path in replacements if module_name(path) in sys.modules}
    if not needs_hot.issubset(safe_hot):
        return False
    for path in needs_hot:
        filename = str(base / path)
        tree = compile(replacements[path], filename, "exec", dont_inherit=True)
        pending = [tree]
        by_location = {}
        while pending:
            code = pending.pop()
            for child in code.co_consts:
                if isinstance(child, types.CodeType):
                    pending.append(child)
                    key = child.co_qualname, child.co_firstlineno
                    if key in by_location:
                        # Multiple lambdas on one line cannot be identified by this key.
                        by_location[key] = None
                    else:
                        by_location[key] = child
        matched = 0
        for obj in gc.get_objects():
            if not isinstance(obj, types.FunctionType):
                continue
            old = obj.__code__
            if old.co_filename != filename:
                continue
            new = by_location.get((old.co_qualname, old.co_firstlineno))
            if new is None:
                # A changed lambda or shifted definition requires fresh imports.
                return False
            if (
                new.co_freevars,
                new.co_cellvars,
                new.co_argcount,
                new.co_kwonlyargcount,
                new.co_flags,
            ) != (
                old.co_freevars,
                old.co_cellvars,
                old.co_argcount,
                old.co_kwonlyargcount,
                old.co_flags,
            ):
                return False
            obj.__code__ = new
            matched += 1
        if not matched:
            return False
    sys.meta_path.insert(0, PatchedImports(root, replacements))
    return True


def run_cases(root, channel, tests):
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PYTHONPATH"] = str(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
        os.dup2(null, 2)
        os.close(null)
        # A worker-dependent test stays in the static plan. A completed
        # subprocess-free case is the only case eligible for removal.
        isolated = [True]

        def process_event(event, args):
            if event in {
                "subprocess.Popen",
                "os.fork",
                "os.forkpty",
                "os.posix_spawn",
                "os.exec",
            }:
                isolated[0] = False
                raise OSError("replay requires a single process")

        sys.addaudithook(process_event)
        cases = []
        ids = {}
        for test in tests:
            pieces = test["id"].split("::")
            if len(pieces) != 3:
                continue
            filename, cls, method = pieces
            module = filename[:-3].replace("/", ".")
            try:
                case_class = getattr(importlib.import_module(module), cls)
                if not issubclass(case_class, unittest.TestCase):
                    continue
                case = case_class(method)
            except (AttributeError, ImportError, TypeError, ValueError, SyntaxError):
                continue
            ids[case.id()] = test["number"]
            cases.append(case)
        unittest.TestSuite(cases).run(Results(channel, ids, isolated))
    finally:
        os._exit(0)


def replay(base, replacements, tests, cpu_budget, deadline, safe_hot):
    passed, failed = set(), set()
    if not tests:
        return passed, failed
    with TemporaryDirectory(prefix="br-") as scratch:
        root = Path(scratch)
        mirror(base, root, replacements)
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if not pid:
            try:
                os.close(read_fd)
                os.setsid()
                signal.signal(signal.SIGPROF, signal.SIG_DFL)
                signal.setitimer(signal.ITIMER_PROF, cpu_budget)
                if install_in_child(base, root, replacements, safe_hot):
                    run_cases(root, write_fd, tests)
                plan = root / "replay-plan.json"
                plan.write_text(json.dumps(tests), encoding="utf-8")
                os.set_inheritable(write_fd, True)
                os.execv(
                    sys.executable,
                    [sys.executable, "-B", __file__, str(root), str(write_fd)],
                )
            finally:
                os._exit(1)
        os.close(write_fd)
        data = bytearray()
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                if not select.select([read_fd], [], [], remaining)[0]:
                    break
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                data.extend(chunk)
        finally:
            os.close(read_fd)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os.waitpid(pid, 0)
        for message in data.splitlines():
            status, sep, number = message.partition(b" ")
            if sep and number.isdigit():
                if status == b"pass":
                    passed.add(int(number))
                elif status == b"fail":
                    failed.add(int(number))
    return passed - failed, failed


def schedule_cost(numbers, duration, schedule):
    workers = schedule(numbers, duration)
    return max(
        (sum(duration[n] for n in worker) for worker in workers), default=0
    ) + 2000 * len(workers)


def candidates(selected, duration, tests, schedule):
    original = schedule_cost(selected, duration, schedule)
    if original < 300:
        return [], 0.0
    eligible = [
        n
        for n in selected
        if tests[n]["file"] not in {"tests/test_optimizer.py", "tests/test_executor.py"}
        and duration[n] <= 1400
    ]
    ordered = sorted(eligible, key=lambda n: (-duration[n], n))
    counts = sorted(
        {n for n in (1, 3, 8, 20, 48, 100, 240, len(ordered)) if 0 < n <= len(ordered)}
    )
    best_gain = 0.0
    best = []
    for count in counts:
        group = ordered[:count]
        expected = sum(duration[n] for n in group) / 1000
        if expected > 4:
            continue
        savings = (
            original - schedule_cost(selected.difference(group), duration, schedule)
        ) / 1000
        gain = 0.8 * savings - expected - 0.18
        if gain > best_gain:
            best_gain, best = gain, group
    return best, best_gain


def cpu_used():
    ticks = os.times()
    return ticks.user + ticks.system + ticks.children_user + ticks.children_system


class Verifier:
    def __init__(self, root, tests, schedule):
        self.root = root
        self.tests = {test["number"]: test for test in tests}
        self.durations = {n: round(1000 * t["seconds"]) for n, t in self.tests.items()}
        self.schedule = schedule
        self.deadline = time.monotonic() + 85
        self.cpu_start = cpu_used()
        self.warmed = False
        self.passed = set()

    def check(self, selected, patch, selector, parse_changes, apply_hunks):
        self.passed = set()
        if cpu_used() - self.cpu_start >= 45 or time.monotonic() > self.deadline - 1:
            return selected
        try:
            sections = parse_changes(patch)
            if not sections or any(
                path not in selector.sources or not path.endswith(".py")
                for path, _ in sections
            ):
                return selected
            replacements = {
                path: apply_hunks(selector.sources[path], hunks)
                for path, hunks in sections
            }
        except (SyntaxError, ValueError, KeyError):
            return selected
        group, gain = candidates(selected, self.durations, self.tests, self.schedule)
        if not group:
            return selected
        # Add independently executed cross-checks for changes to shared components.
        # Unverified out-of-set tests never count as confirmed passes or failures.
        extra = set()
        for path in replacements:
            extra.update(selector.file_tests.get(path, ()))
        extra.difference_update(selected)
        probes = [
            n
            for n in extra
            if self.durations[n] <= 35
            and self.tests[n]["file"].startswith("tests/dialects/")
        ]
        probes.sort(
            key=lambda n: (
                self.tests[n]["file"] != self.tests[group[0]]["file"],
                self.durations[n],
                n,
            )
        )
        requested = [*group, *probes[:8]]
        if not self.warmed:
            sys.path.insert(0, str(self.root))
            os.environ["SKIP_INTEGRATION"] = "0"
            level = logging.root.manager.disable
            try:
                logging.disable(logging.CRITICAL)
                importlib.import_module("tests.dialects.test_dialect")
            finally:
                logging.disable(level)
            self.warmed = True
        from selector import function_ranges

        safe_hot = set()
        for path, updated in replacements.items():
            original = selector.sources[path]
            if len(original.splitlines()) != len(updated.splitlines()):
                continue
            spans = function_ranges(original, updated)
            if spans and all(
                not any(a <= line <= b for line in selector.startup.get(path, ()))
                for a, b in spans
            ):
                safe_hot.add(path)
        forbidden = set().union(
            *(selector.source_readers.get(path, set()) for path in replacements)
        )
        requested = [n for n in requested if n not in forbidden]
        start = os.times()
        allowance = min(
            5.5, max(1.8, 1.0 + 1.5 * sum(self.durations[n] for n in requested) / 1000)
        )
        allowance = min(allowance, 45 - (cpu_used() - self.cpu_start))
        if allowance < 0.25:
            return selected
        try:
            passed, failed = replay(
                self.root,
                replacements,
                [self.tests[n] for n in requested],
                allowance,
                min(self.deadline, time.monotonic() + min(10, 1 + 2.4 * allowance)),
                safe_hot,
            )
        except (OSError, ValueError):
            return selected
        end = os.times()
        if os.environ.get("REPLAY_DEBUG"):
            print(
                f"replay {list(replacements)} base={len(selected)} group={len(group)} extra={len(requested) - len(group)} pass={len(passed)} fail={len(failed)} saved={len(selected & passed)} cpu={end.children_user + end.children_system - start.children_user - start.children_system:.2f}",
                file=sys.stderr,
            )
        self.passed = passed
        return selected.difference(passed).union(failed)


if __name__ == "__main__":
    checkout = Path(sys.argv[1])
    run_cases(
        checkout,
        int(sys.argv[2]),
        json.loads((checkout / "replay-plan.json").read_text()),
    )
