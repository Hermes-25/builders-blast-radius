"""Capture helper calls in a fresh interpreter for each independently measured expensive test."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
import build_options as profiler
import build_helper


def main():
    root, dest, node = (
        Path(sys.argv[1]).resolve(),
        Path(sys.argv[2]).resolve(),
        sys.argv[3],
    )
    if subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip() != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(
        ["git", "-C", str(root), "diff", "HEAD", "--name-only"]
    ):
        raise ValueError("Expected unmodified base revision")
    os.environ.update(
        BR_ROOT=str(root),
        BR_PARENT_PID=str(os.getpid()),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        SKIP_INTEGRATION="0",
    )
    build_helper.install(root)
    submit = ProcessPoolExecutor.submit

    def tracked(pool, fn, /, *args, **kwargs):
        return submit(pool, profiler.child, profiler.context, fn, args, kwargs)

    ProcessPoolExecutor.submit = tracked
    plugin = profiler.Plugin()
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["BR_OPTIONS_DIR"] = scratch
        result = pytest.main(
            ["-q", "-p", "no:cacheprovider", "--import-mode=prepend", node],
            plugins=[plugin],
        )
        profiler.flush()
        if result or plugin.passed != {node}:
            raise SystemExit(1)
        observations = defaultdict(lambda: defaultdict(set))
        for p in Path(scratch).glob("*.json"):
            for label in json.loads(p.read_text()):
                if not label.startswith("h:"):
                    continue
                key, _, value = label[2:].partition("|")
                name, _, site = key.partition("@")
                observations[name][site].add(value)
        with gzip.GzipFile(str(dest), "wb", mtime=0) as handle:
            handle.write(
                json.dumps(
                    {
                        "test": node,
                        "functions": {
                            name: {
                                site: sorted(values) for site, values in rows.items()
                            }
                            for name, rows in observations.items()
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )


if __name__ == "__mp_main__" and "BR_ROOT" in os.environ:
    build_helper.install(Path(os.environ["BR_ROOT"]))

if __name__ == "__main__":
    main()
