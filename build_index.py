"""Build a test dependency index from an unmodified SQLGlot checkout."""

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import coverage
import pytest


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
child_tracer = None


def run_worker_test(node, config, function, args, kwargs):
    """Carry the submitting test's context into reused multiprocessing workers."""
    global child_tracer
    if child_tracer is None:
        child_tracer = coverage.Coverage(config_file=False, data_suffix=True, **config)
        child_tracer.start()
        Finalize(None, child_tracer.save, exitpriority=10)
    child_tracer.switch_context(node)
    try:
        return function(*args, **kwargs)
    finally:
        child_tracer.switch_context("")


class RecordDependencies:
    def __init__(self, root, tracer):
        self.prefix = os.path.normcase(str(root)) + os.sep
        self.tracer = tracer
        self.node = ""
        self.opens = defaultdict(set)
        self.completed = set()

    def relative(self, filename):
        filename = os.path.normcase(os.path.abspath(os.fsdecode(filename)))
        if filename.startswith(self.prefix):
            return filename[len(self.prefix):].replace(os.sep, "/")
        return ""

    def audit(self, event, args):
        if event != "open" or not self.node or isinstance(args[0], int):
            return
        path = self.relative(args[0])
        if path and not path.endswith((".pyc", ".pyd", ".so")):
            self.opens[path].add(self.node)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        self.node = item.nodeid
        self.tracer.switch_context(self.node)
        try:
            return (yield)
        finally:
            self.tracer.switch_context("")
            self.node = ""

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.completed.add(report.nodeid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("index", type=Path)
    parser.add_argument("--tests", nargs="+", default=["tests"])
    args = parser.parse_args()
    root, index = args.checkout.resolve(), args.index.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != REVISION:
        parser.error(f"expected clean SQLGlot revision {REVISION}, got {revision}")
    if subprocess.check_output(["git", "-C", str(root), "diff", "HEAD", "--name-only"]):
        parser.error("the SQLGlot checkout has tracked edits")

    source_files = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"], text=True, encoding="utf-8"
    ).split("\0")
    sources = {
        name: (root / name).read_text(encoding="utf-8")
        for name in source_files
        if name.endswith(".py") and name.startswith(("sqlglot/", "tests/"))
    }
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.chdir(root)
    sys.path.insert(0, str(root))
    with TemporaryDirectory() as scratch:
        config = {
            "data_file": str(Path(scratch) / "coverage"),
            "include": [str(root / "sqlglot" / "*"), str(root / "tests" / "*")],
        }
        tracer = coverage.Coverage(config_file=False, **config)
        recorder = RecordDependencies(root, tracer)
        sys.addaudithook(recorder.audit)
        submit = ProcessPoolExecutor.submit

        def tracked_submit(pool, function, /, *args, **kwargs):
            if not recorder.node:
                return submit(pool, function, *args, **kwargs)
            return submit(pool, run_worker_test, recorder.node, config, function, args, kwargs)

        ProcessPoolExecutor.submit = tracked_submit
        try:
            with tracer.collect():
                result = pytest.main(
                    ["-q", "-p", "no:cacheprovider", "--import-mode=prepend", *args.tests],
                    plugins=[recorder],
                )
        finally:
            ProcessPoolExecutor.submit = submit

        tracer.save()
        tracer.combine([scratch])

        data = tracer.get_data()
        tests = sorted(recorder.completed)
        slots = {test: number for number, test in enumerate(tests)}
        lines = {}
        startup = {}
        for filename in data.measured_files():
            path = recorder.relative(filename)
            if path not in sources:
                continue
            file_lines = {}
            for line, contexts in data.contexts_by_lineno(filename).items():
                if "" in contexts:
                    startup.setdefault(path, []).append(line)
                nodes = sorted({slots[test] for test in contexts if test in slots})
                if nodes:
                    file_lines[line] = nodes
            lines[path] = file_lines
        reads = {
            path: sorted(slots[test] for test in nodes if test in slots)
            for path, nodes in recorder.opens.items()
        }
        with gzip.GzipFile(filename=str(index), mode="wb", mtime=0) as file:
            file.write(json.dumps({
                "revision": REVISION,
                "tests": tests,
                "lines": lines,
                "startup": startup,
                "reads": reads,
                "sources": sources,
            }, separators=(",", ":"), sort_keys=True).encode())
        print(f"Wrote {index} with {len(tests)} passing test contexts and {len(lines)} files")
        if result != pytest.ExitCode.OK:
            print(f"Profiling suite exited {int(result)}. Inspect failures before reuse.", file=sys.stderr)
            sys.exit(result)


if __name__ == "__main__":
    main()
