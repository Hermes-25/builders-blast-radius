"""Per-process, one-shot line coverage for an isolated baseline test and its workers."""

import atexit
from collections import defaultdict
import gzip
import json
import os
import sys
from multiprocessing.util import Finalize
from pathlib import Path

root = os.environ.get("ISO_ROOT")
out = os.environ.get("ISO_OUT")
if root and out:
    prefix = root.rstrip("/") + "/"
    lines = defaultdict(set)
    opened = set()
    tool = sys.monitoring.PROFILER_ID
    sys.monitoring.use_tool_id(tool, "pinned-isolated-coverage")

    def first_line(code, line):
        path = code.co_filename
        if path.startswith(prefix):
            lines[path[len(prefix) :]].add(line)
        return sys.monitoring.DISABLE

    def file_open(event, args):
        if event == "open" and isinstance(args[0], str):
            path = args[0]
            if path.startswith(prefix) and "__pycache__" not in path:
                opened.add(path[len(prefix) :])

    def save():
        dest = Path(out) / (str(os.getpid()) + ".json.gz")
        with gzip.GzipFile(str(dest), "wb", mtime=0) as f:
            f.write(
                json.dumps(
                    {
                        "lines": {p: sorted(n) for p, n in lines.items()},
                        "opened": sorted(opened),
                    },
                    separators=(",", ":"),
                ).encode()
            )

    sys.addaudithook(file_open)
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, first_line)
    sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
    atexit.register(save)
    Finalize(None, save, exitpriority=9)
