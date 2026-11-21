"""Reproduce the pristine executor's CSV handling and hash the values passed to its oracle."""

import ast, csv, gzip, hashlib, pickle
from pathlib import Path
from functools import lru_cache
from datetime import date, datetime, time, timedelta
from decimal import Decimal


def digest_table(table):
    if table is None:
        return "skipped"

    def plain(x):
        if x is None or type(x) in (
            str,
            int,
            float,
            bool,
            bytes,
            Decimal,
            date,
            datetime,
            time,
            timedelta,
        ):
            return (type(x).__name__, repr(x))
        if type(x) in (tuple, list):
            return (type(x).__name__, tuple(plain(v) for v in x))
        if type(x) is dict:
            return ("dict", tuple((plain(k), plain(v)) for k, v in x.items()))
        raise ValueError(type(x))

    rows = (tuple(table.columns), [tuple(plain(v) for v in row) for row in table.rows])
    return hashlib.sha256(repr(rows).encode()).hexdigest()


@lru_cache(maxsize=None)
def load_table(root, family, name):
    from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA
    from sqlglot.executor.table import Table

    schema = TPCH_SCHEMA if family == "tpc-h" else TPCDS_SCHEMA
    path = Path(root) / "tests" / "fixtures" / "optimizer" / family / (name + ".csv.gz")
    with path.open("rb") as stream:
        compressed = stream.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    with opener(path, "rt", encoding="utf8", newline="") as stream:
        reader = csv.reader(stream, delimiter="|")
        next(reader)
        typ = []
        rows = []
        for row in reader:
            if not typ:
                for value in row:
                    try:
                        typ.append(type(ast.literal_eval(value)))
                    except (ValueError, SyntaxError):
                        typ.append(str)
            rows.append(
                tuple(
                    None if (t is not str and v == "") else t(v)
                    for t, v in zip(typ, row)
                )
            )
    return Table(columns=schema[name], rows=rows)


def compute(root, family, expression, meta):
    from sqlglot import find_tables
    from sqlglot.executor import execute
    from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA

    if not meta.get("execute"):
        return "skipped"
    schema = TPCH_SCHEMA if family == "tpc-h" else TPCDS_SCHEMA
    tables = {t.name: load_table(root, family, t.name) for t in find_tables(expression)}
    result = pickle.loads(
        pickle.dumps(execute(expression, schema=schema, tables=tables))
    )
    return digest_table(result)
