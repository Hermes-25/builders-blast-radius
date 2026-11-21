"""Union baseline observations, explicitly preserving input shapes seen during startup."""

from collections import defaultdict
import gzip
import json
from pathlib import Path
import sys


def load(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


def main():
    full, workers = map(load, sys.argv[1:3])
    tests = full["tests"]
    numbers = {name: i for i, name in enumerate(tests)}
    owners = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    for profile in (full, workers):
        for label, ids in profile["owners"].items():
            fnsite, _, raw = label[2:].partition("|")
            fn, _, site = fnsite.partition("@")
            owners[fn][site][raw].update(numbers[profile["tests"][i]] for i in ids)
        for label in profile["startup"]:
            fnsite, _, raw = label[2:].partition("|")
            fn, _, site = fnsite.partition("@")
            owners[fn][site][raw].add(-1)
    result = {
        fn: {
            site: {v: sorted(ids) for v, ids in values.items()}
            for site, values in sites.items()
        }
        for fn, sites in owners.items()
    }
    cold = {}
    for location in sys.argv[4:]:
        item = load(location)
        cold[item["test"]] = item["functions"]
    with gzip.GzipFile(str(Path(sys.argv[3])), "wb", mtime=0) as stream:
        stream.write(
            json.dumps(
                {"tests": tests, "functions": result, "cold": cold},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
    print([(k, len(v)) for k, v in result.items()])


if __name__ == "__main__":
    main()
