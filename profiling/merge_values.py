"""Collect observations from the full baseline and independent worker-suite pass."""

import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path


def load(p):
    with gzip.open(p, "rt") as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("full", type=Path)
    p.add_argument("workers", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--extra", type=Path, nargs="*", default=[])
    args = p.parse_args()
    main, workers = load(args.full), load(args.workers)
    labels = main["tests"]
    slots = {v: i for i, v in enumerate(labels)}
    profiles = [main, workers, *map(load, args.extra)]
    startup = {key.partition("|")[0] for p in profiles for key in p["startup"]}
    grouped = defaultdict(lambda: defaultdict(set))
    for profile in profiles:
        for key, observed in profile["owners"].items():
            site, _, raw = key.partition("|")
            if site not in startup:
                grouped[site][raw].update(
                    slots[t] for i in observed if (t := profile["tests"][i]) in slots
                )
    sites = {
        k: {value: sorted(tests) for value, tests in values.items()}
        for k, values in grouped.items()
    }
    with gzip.GzipFile(str(args.out), "wb", mtime=0) as f:
        f.write(
            json.dumps(
                {"tests": labels, "sites": sites}, separators=(",", ":"), sort_keys=True
            ).encode()
        )
    print(
        f"Compiled {len(sites)} sites from {len(labels)} baseline tests; excluded {len(startup)} startup sites."
    )


if __name__ == "__main__":
    main()
