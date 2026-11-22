"""Checkpoint inputs from the original executor test at its optimizer boundary."""

import argparse
import copyreg
import gzip
import io
import json
import os
from pathlib import Path
import pickle
import sys
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    entry = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path[:0] = [str(root), str(entry)]
    from frontier_core import ensure_digest, parse_packet, optimize_for_executor
    from sqlglot.expressions import Expr
    from tests.helpers import load_sql_fixture_pairs, TPCDS_SCHEMA

    class ExpressionPickler(pickle.Pickler):
        def reducer_override(self, value):
            if not isinstance(value, Expr):
                return NotImplemented
            slots = {}
            for cls in type(value).__mro__:
                for field in cls.__dict__.get("__slots__", ()):
                    if field not in {"__dict__", "__weakref__"} and hasattr(
                        value, field
                    ):
                        slots[field] = object.__getattribute__(value, field)
            return copyreg.__newobj__, (type(value),), (vars(value).copy(), slots)

    with gzip.open(entry / "frontier.json.gz") as stream:
        original = json.load(stream)["tests"]["execute_tpcds"]["hashes"]
    inputs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    assert len(inputs) == len(original)
    from zipfile import ZipInfo

    with ZipFile(output, "w") as archive:
        for i, ((meta, sql, _), states) in enumerate(zip(inputs, original)):
            if "opt" not in states:
                continue
            expr = parse_packet(sql, meta)
            assert ensure_digest(expr) == states["parse"]
            buffer = io.BytesIO()
            ExpressionPickler(buffer, protocol=5).dump(expr)
            payload = buffer.getvalue()
            restored = pickle.loads(payload)
            assert ensure_digest(restored) == states["parse"]
            assert (
                ensure_digest(optimize_for_executor(restored, TPCDS_SCHEMA))
                == states["opt"]
            )
            archive.writestr(
                ZipInfo(str(i)), gzip.compress(payload, compresslevel=6, mtime=0)
            )
    print(
        sum("opt" in row for row in original),
        "original optimizer input and result pairs verified",
    )


if __name__ == "__main__":
    main()
