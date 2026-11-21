"""Capture per-module startup, test and subprocess reachability on the pinned revision."""
import argparse
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

def build(checkout, catalogue, output):
    revision = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(
        ["git", "-C", str(checkout), "diff", "HEAD", "--name-only"]
    ):
        raise ValueError("Expected the pristine pinned SQLGlot checkout.")
    tests = json.loads(catalogue.read_text())["tests"]
    modules = sorted({test["file"] for test in tests})
    lines = defaultdict(dict)
    unknown = []
    for index, module in enumerate(modules):
        with TemporaryDirectory() as scratch:
            env = os.environ | {
                "ISO_ROOT": str(checkout),
                "ISO_OUT": scratch,
                "PYTHONPATH": os.pathsep.join((str(Path(__file__).resolve().parent), str(checkout))),
                "PYTHONDONTWRITEBYTECODE": "1",
                "SKIP_INTEGRATION": "0",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            }
            run = subprocess.run(
                [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", module],
                cwd=checkout, env=env, capture_output=True,
            )
            processes = list(Path(scratch).glob("*.json.gz"))
            if run.returncode or not processes:
                unknown.append(module)
                continue
            for path in processes:
                with gzip.open(path, "rt") as part:
                    profile = json.load(part)
                for source, executed in profile["lines"].items():
                    if source.startswith("sqlglot/"):
                        record = lines[source]
                        for line in executed:
                            key = str(line)
                            record[key] = record.get(key, 0) | (1 << index)
    profile = {"modules": modules, "unknown": unknown, "lines": lines}
    with gzip.GzipFile(str(output), "wb", mtime=0) as stream:
        stream.write(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("catalogue", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.checkout.resolve(), args.catalogue.resolve(), args.output.resolve())
