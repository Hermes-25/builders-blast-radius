"""Precompute invariant compiler comparisons for the unmodified pinned checkout."""

import gzip
import hashlib
import json
from pathlib import Path
import sys

entry = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(entry))
from selector import code_hash, function_shape

with gzip.open(entry / "impact-index.json.gz") as handle:
    sources = json.load(handle)["sources"]
files = {}
for path, source in sources.items():
    if path.endswith(".py"):
        shape, functions = function_shape(source)
        files[path] = [
            hashlib.sha256(source.encode()).hexdigest(),
            code_hash(source, path),
            shape,
            functions,
        ]
payload = {
    "runtime": [sys.implementation.cache_tag, sys.hexversion, sys.flags.optimize],
    "files": files,
}
Path(sys.argv[1]).write_bytes(
    gzip.compress(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), mtime=0
    )
)
print(len(files), "verified pinned source files")
