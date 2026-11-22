"""Execute the complete patched optimizer and verify original executor graph outputs."""

import gzip
import importlib
import json
from pathlib import Path
import pickle
import sys
from zipfile import ZipFile


def main():
    root = Path(sys.argv[1])
    outcome = Path(sys.argv[2])
    mask = int(sys.argv[3])
    sys.path.insert(0, str(root))
    for path in sys.argv[4:]:
        importlib.import_module(path[:-3].replace("/", "."))
    importlib.import_module("sqlglot.executor")
    from sqlglot.dialects.dialect import Dialect
    from tests.helpers import TPCDS_SCHEMA
    from frontier_core import ensure_digest, optimize_for_executor

    list(Dialect.classes.values())
    with gzip.open(Path(__file__).with_name("frontier.json.gz")) as stream:
        expected = json.load(stream)["tests"]["execute_tpcds"]["hashes"]
    if mask >> len(expected):
        return 1
    with ZipFile(Path(__file__).with_name("optimizer-packets.zip")) as inputs:
        for i, row in enumerate(expected):
            if not mask & (1 << i):
                continue
            expr = pickle.loads(gzip.decompress(inputs.read(str(i))))
            if ensure_digest(optimize_for_executor(expr, TPCDS_SCHEMA)) != row["opt"]:
                return 1
    outcome.write_bytes(b"complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
