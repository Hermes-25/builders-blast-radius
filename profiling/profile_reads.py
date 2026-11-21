"""Profile explicit Python-source reads separately from Python import machinery."""

import gzip
import json
import os
import sys
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

reads = defaultdict(set)
context = ""


def install(root):
    prefix = str(root) + os.sep

    def audit(event, args):
        if event != "open" or not context:
            return
        path = args[0]
        if not isinstance(path, str) or not path.endswith(".py"):
            return
        caller = sys._getframe(1)
        if caller.f_globals.get("__name__") in (
            "_frozen_importlib_external",
            "importlib._bootstrap_external",
        ):
            return
        path = os.path.abspath(path)
        if path.startswith(prefix):
            reads[path[len(prefix) :]].add(context)

    sys.addaudithook(audit)


def flush():
    dest = Path(os.environ["READ_TRACE_DIR"]) / (str(os.getpid()) + ".json")
    dest.write_text(json.dumps({k: sorted(v) for k, v in reads.items()}))


def child(node, fn, args, kwargs):
    global context
    if not getattr(child, "started", False):
        Finalize(None, flush, exitpriority=9)
        child.started = True
    context = node
    try:
        return fn(*args, **kwargs)
    finally:
        context = ""


class Plugin:
    def __init__(self):
        self.passed = set()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        global context
        context = item.nodeid
        try:
            return (yield)
        finally:
            context = ""

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)


def main():
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    fixtures = {}
    if len(sys.argv) > 3:
        with gzip.open(Path(sys.argv[3]).resolve(), "rt", encoding="utf-8") as stream:
            fixtures = json.load(stream)
    if subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip() != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(
        ["git", "-C", str(root), "diff", "HEAD", "--name-only"]
    ):
        raise ValueError("Expected the pristine pinned SQLGlot checkout.")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(
        READ_TRACE_ROOT=str(root),
        SKIP_INTEGRATION="0",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    install(root)
    submit = ProcessPoolExecutor.submit

    def launch(pool, fn, /, *args, **kwargs):
        return submit(pool, child, context, fn, args, kwargs)

    ProcessPoolExecutor.submit = launch
    plugin = Plugin()
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["READ_TRACE_DIR"] = scratch
        result = pytest.main(
            ["-q", "-p", "no:cacheprovider", "--import-mode=prepend", "tests"],
            plugins=[plugin],
        )
        flush()
        merged = defaultdict(set)
        for part in Path(scratch).glob("*.json"):
            for path, tests in json.loads(part.read_text()).items():
                merged[path].update(tests)
        with gzip.GzipFile(str(out), "wb", mtime=0) as stream:
            stream.write(
                json.dumps(
                    {
                        "tests": sorted(plugin.passed),
                        "class_fixtures": fixtures,
                        "reads": {p: sorted(v) for p, v in merged.items()},
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
        print("Passing tests", len(plugin.passed), "explicit files", len(merged))
        sys.exit(result)


if __name__ == "__mp_main__" and "READ_TRACE_ROOT" in os.environ:
    install(Path(os.environ["READ_TRACE_ROOT"]))
if __name__ == "__main__":
    main()
