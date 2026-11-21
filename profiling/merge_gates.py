"""Merge full-suite and supplemental worker observations from the unmodified pinned source."""

import gzip
import json
from pathlib import Path
import sys


def read(path):
    with gzip.open(path, "rt") as handle:
        return json.load(handle)


def main(full_path, worker_path, catalogue_path, output):
    full, worker = read(full_path), read(worker_path)
    tests = full["tests"]
    catalogue = json.loads(Path(catalogue_path).read_text())["tests"]
    if set(tests) != {t["id"] for t in catalogue}:
        raise ValueError("Incomplete original full-suite run")
    numbered = {name: i for i, name in enumerate(tests)}
    declared = set()
    blocked = set()
    for run in (full, worker):
        declared.update(
            k.removeprefix("decl:")
            for k in run["startup"]
            if k.startswith("decl:gate:")
        )
        blocked.update(k for k in run["startup"] if k.startswith("gate:"))
    groups = []
    group_ids = {}
    owners = {}
    for key in sorted(declared - blocked):
        seen = set(full["owners"].get(key, ()))
        seen.update(numbered[worker["tests"][i]] for i in worker["owners"].get(key, ()))
        slots = tuple(sorted(seen))
        if slots not in group_ids:
            group_ids[slots] = len(groups)
            groups.append(slots)
        owners[key.removeprefix("gate:")] = group_ids[slots]
    payload = json.dumps(
        {"tests": tests, "groups": groups, "owners": owners},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with open(output, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as handle:
            handle.write(payload)
    print(
        len(owners),
        "safe gates,",
        len(blocked),
        "startup gates,",
        len(groups),
        "distinct groups",
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
