"""Combine independent complete pristine runs of the cold import dependency profiler."""
from collections import defaultdict
import gzip
import json
from pathlib import Path
import sys


def merge(paths, output):
    modules = {}
    edges = defaultdict(set)
    for path in paths:
        with gzip.open(path, "rt") as handle:
            profile = json.load(handle)
        if profile["unknown"]:
            raise ValueError(f"Incomplete baseline {path}")
        if modules and modules.keys() != profile["modules"].keys():
            raise ValueError("Baseline test file sets differ")
        for filename, groups in profile["modules"].items():
            accumulated = modules.setdefault(filename, defaultdict(set))
            for scope, required in groups.items():
                accumulated[scope].update(required)
        for name, required in profile["edges"].items():
            edges[name].update(required)
    result = {
        "modules": {name: {scope:sorted(required) for scope,required in groups.items()}
                    for name,groups in modules.items()},
        "edges": {name: sorted(required) for name, required in edges.items()},
        "unknown": [],
    }
    payload = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    with Path(output).open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as encoded:
            encoded.write(payload)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("Expected two or more baseline profiles and the output path")
    merge(sys.argv[1:-1], sys.argv[-1])
