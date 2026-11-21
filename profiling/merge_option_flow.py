"""Combine forwarded field reads and direct references in pinned test sources."""

import gzip, json, sys
from pathlib import Path


def load(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


main, workers, cov = map(load, sys.argv[1:4])
tests = main["tests"]
slots = {k: i for i, k in enumerate(tests)}
startup = set(main["startup"]) | set(workers["startup"])
owners = {}
for key in main["owners"]:
    if key in startup:
        continue
    name = key.removeprefix("flow:")
    if name.startswith("_"):
        continue
    reads = set()
    for data in (main, workers):
        reads.update(slots[data["tests"][i]] for i in data["owners"].get(key, []))
    for path, src in cov["sources"].items():
        if path.startswith("tests/") and (name in src or name.lower() in src):
            reads.update(i for t, i in slots.items() if t.startswith(path + "::"))
    owners[name] = sorted(reads)
with gzip.GzipFile(sys.argv[4], "wb", mtime=0) as out:
    out.write(
        json.dumps(
            {"tests": tests, "owners": owners}, separators=(",", ":"), sort_keys=True
        ).encode()
    )
print({k: len(v) for k, v in owners.items()})
