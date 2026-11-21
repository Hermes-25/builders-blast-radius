"""Collect disjoint startup, execution and worker dependencies for expensive base tests."""

import argparse
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

parser = argparse.ArgumentParser()
parser.add_argument("checkout", type=Path)
parser.add_argument("catalogue", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
root = args.checkout.resolve()
out = args.output.resolve()
if subprocess.check_output(
    ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
).strip() != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(
    ["git", "-C", str(root), "diff", "HEAD", "--name-only"]
):
    raise ValueError("Expected the pristine pinned SQLGlot checkout.")
catalog = json.loads(args.catalogue.read_text())["tests"]
longest = sorted(catalog, key=lambda x: -x["seconds"])[:12]
profiles = {}
for entry in longest:
    node = entry["id"]
    with TemporaryDirectory() as scratch:
        env = os.environ | {
            "ISO_ROOT": str(root),
            "ISO_OUT": scratch,
            "PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).resolve().parent), str(root)]
            ),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "SKIP_INTEGRATION": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--import-mode=prepend",
                node,
            ],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        parts = list(Path(scratch).glob("*.json.gz"))
        if result.returncode or not parts:
            print(
                "Failed isolation",
                node,
                result.returncode,
                result.stdout[-400:],
                len(parts),
                flush=True,
            )
            continue
        lines = defaultdict(set)
        opened = set()
        for part in parts:
            with gzip.open(part, "rt") as f:
                data = json.load(f)
            for path, ids in data["lines"].items():
                lines[path].update(ids)
            opened.update(data["opened"])
        profiles[node] = {
            "lines": {p: sorted(v) for p, v in lines.items()},
            "opened": sorted(opened),
        }
        with gzip.GzipFile(str(out), "wb", mtime=0) as f:
            f.write(
                json.dumps(profiles, sort_keys=True, separators=(",", ":")).encode()
            )
        print(
            "Recorded",
            node,
            "processes",
            len(parts),
            "files",
            len(lines),
            "tests done",
            len(profiles),
            flush=True,
        )
