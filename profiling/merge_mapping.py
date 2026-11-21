"""Merge guarded, observed reads of existing literal dictionary values into the pinned profile."""

import gzip, json, sys
from pathlib import Path

full, workers, semantic, out = map(Path, sys.argv[1:])


def load(path):
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


f, w, profile = map(load, (full, workers, semantic))
numbers = {node: i for i, node in enumerate(profile["tests"])}
if set(f["tests"]) != set(numbers):
    raise ValueError("Incomplete baseline")
blocked = set(f["startup"]) | set(w["startup"])
seen = {
    key for src in (f, w) for key, slots in src["owners"].items() if slots
} | blocked
touched = {key.partition("|")[0] for key in seen if key.endswith("|!")}
active = {key.partition("|")[0] for key in seen if key.partition("|")[2] != "!"}
keys = 0
sites = set()
for key in f["owners"]:
    site, _, suffix = key.partition("|")
    whole = site + "|*"
    if suffix in ("!", "*") or not suffix.startswith('"') or site not in active:
        continue
    if site in touched or whole in blocked or key in blocked:
        continue
    owner = {
        s["tests"][i]
        for s in (f, w)
        for v in (key, whole)
        for i in s["owners"].get(v, ())
    }
    profile["owners"][key] = sorted(numbers[node] for node in owner if node in numbers)
    keys += 1
    sites.add(site)
with gzip.GzipFile(str(out), "wb", mtime=0) as fh:
    fh.write(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode())
print("Recorded", keys, "key dependencies for", len(sites), "eligible mappings")
