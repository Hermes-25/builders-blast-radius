"""Attach locations that inspected only plain class namespaces in all baseline runs."""

import gzip, json, sys
from collections import defaultdict


def read(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


full, workers, base = map(read, sys.argv[1:4])
if set(full["tests"]) != set(base["tests"]):
    raise ValueError("Incomplete full-suite namespace profile")
seen = defaultdict(set)
for dataset in (full, workers):
    for key in set(dataset["startup"]) | set(dataset["owners"]):
        site, mode = key[2:].split("|")
        seen[site].add(mode)
sites = defaultdict(list)
for site, modes in seen.items():
    if modes == {"class"}:
        path, line, col = site.rsplit(":", 2)
        sites[path].append((int(line), int(col)))
base["class_sites"] = {p: sorted(coords) for p, coords in sites.items()}
with gzip.GzipFile(sys.argv[4], "wb", mtime=0) as stream:
    stream.write(json.dumps(base, sort_keys=True, separators=(",", ":")).encode())
print(base["class_sites"])
