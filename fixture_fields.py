"""Replay changed fixture rows at the original test's optimizer and executor boundaries."""

import ast
from concurrent.futures import Future, wait, as_completed
from functools import lru_cache
import gzip
import json
from pathlib import Path
import sys
from types import MethodType
import unittest


class SerialPool:
    def submit(self, fn, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


class DatabaseInput:
    def execute(self, sql):
        self.sql = sql
        return self

    def df(self):
        return self.sql


@lru_cache(maxsize=2)
def optimizer_operations(root, family):
    """Use the pristine test methods themselves to observe the database operations."""
    from sqlglot import exp, parse_one, optimizer
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, string_to_bool

    path = root / "tests/test_optimizer.py"
    source = ast.parse(path.read_text())
    helper = next(
        n for n in source.body if getattr(n, "name", None) == "parse_and_optimize"
    )
    test_class = next(
        n for n in source.body if getattr(n, "name", None) == "TestOptimizer"
    )
    method = "test_tpc" + family
    declarations = [
        helper,
        *(
            n
            for n in test_class.body
            if getattr(n, "name", None) in ("check_file", method)
        ),
    ]
    ctx = dict(
        json=json,
        sys=sys,
        exp=exp,
        parse_one=parse_one,
        optimizer=optimizer,
        TPCDS_SCHEMA=TPCDS_SCHEMA,
        TPCH_SCHEMA=TPCH_SCHEMA,
        string_to_bool=string_to_bool,
        wait=wait,
        as_completed=as_completed,
    )
    exec(
        compile(ast.Module(body=declarations, type_ignores=[]), str(path), "exec"), ctx
    )
    case = unittest.TestCase()
    case.pool = SerialPool()
    case.conn = DatabaseInput()
    case.check_file = MethodType(ctx["check_file"], case)

    def run(row):
        database = []
        ctx["load_sql_fixture_pairs"] = lambda filename: iter((row,))
        ctx["assert_frame_equal"] = lambda a, b: database.append((a, b))
        ctx[method](case)
        return database

    return run


def verify(root, family, before, after, tags, output):
    from sqlglot import transpile
    from frontier_core import ensure_digest, parse_packet
    from output_core import compute

    def db(sql):
        return transpile(sql, write="duckdb")[0]

    def optimizer_row(old, new):
        run = optimizer_operations(root, family)
        actual = run(new)
        if not actual:
            return True
        return actual == run(old)

    def compare_expected(old, new):
        if old[1:] == new[1:]:
            return True
        actual = (db(new[1]), db(new[2]))
        return actual == (db(old[1]), db(old[2]))

    @lru_cache(maxsize=1)
    def table_hashes():
        with gzip.open(Path(__file__).with_name("table-outputs.json.gz"), "rt") as f:
            return json.load(f)["execute_tpc" + family]

    def executor_row(index, old, new):
        oldmeta, oldsql, _ = old
        meta, sql, _ = new
        enabled = bool(meta.get("execute"))
        if sql == oldsql:
            return not enabled or bool(oldmeta.get("execute"))
        expression = parse_packet(sql, meta)
        if not enabled:
            return True
        if not oldmeta.get("execute") or db(oldsql) != db(sql):
            return False
        if ensure_digest(parse_packet(oldsql, oldmeta)) == ensure_digest(expression):
            return True
        expected, expense = table_hashes()[index]
        return (
            expense < 0.8
            and compute(root, "tpc-" + family, expression, meta) == expected
        )

    changed = [
        (i, old, new) for i, (old, new) in enumerate(zip(before, after)) if old != new
    ]
    if len(before) != len(after) or len(changed) > 8:
        return
    # The executor's class cache is shared across the two database schemas.
    # A newly duplicated SQL string may reuse a result from the other schema.
    if any(old[1] != new[1] for _, old, new in changed):
        from collections import Counter
        from tests.helpers import load_sql_fixture_pairs

        other = "tpc-h" if family == "ds" else "tpc-ds"
        other_rows = load_sql_fixture_pairs(f"optimizer/{other}/{other}.sql")
        keys = Counter(row[1] for row in after)
        keys.update(row[1] for row in other_rows)
        if any(old[1] != new[1] and keys[new[1]] > 1 for _, old, new in changed):
            tags = [tag for tag in tags if tag == "optimizer_tpc" + family]
    decisions = {
        "execute_tpc" + family: executor_row,
        "optimized_tpch": lambda i, old, new: compare_expected(old, new),
        "optimizer_tpc" + family: lambda i, old, new: optimizer_row(old, new),
    }
    for tag in tags:
        try:
            if all(decisions[tag](index, old, new) for index, old, new in changed):
                with output.open("a") as f:
                    f.write(tag + "\n")
        except Exception:
            continue


if __name__ == "__main__":
    root, request, output = map(Path, sys.argv[1:])
    sys.path.insert(0, str(root))
    family, before, after, tags = json.loads(request.read_text())
    verify(root, family, before, after, tags, output)
