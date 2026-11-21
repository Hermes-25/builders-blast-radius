"""Check patched pipeline boundary values using the pinned SQLGlot production operations."""

import gzip, hashlib, json, os, sys, unittest
from pathlib import Path
from frontier_core import ensure_digest, parse_packet, optimize_for_executor


def check(root, plans, output):
    os.chdir(root)
    sys.path.insert(0, str(root))
    from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA, load_sql_fixture_pairs
    from sqlglot import transpile

    with gzip.open(Path(__file__).with_name("frontier.json.gz"), "rt") as f:
        profile = json.load(f)["tests"]
    from output_core import compute

    with gzip.open(Path(__file__).with_name("table-outputs.json.gz"), "rt") as stream:
        tables = json.load(stream)
    budget = 5.0
    for tag, stages in plans.items():
        try:
            if tag == "generator_memory":
                from tests import test_generator as mod

                dialects = list(mod.Dialects)
                mod.Dialects = tuple(dialects[i] for i in stages["run"])
                case = mod.TestGenerator("test_generator_memory")
                result = unittest.TestResult()
                case.run(result)
                good = (
                    result.wasSuccessful()
                    and result.testsRun == 1
                    and not result.skipped
                    and not result.expectedFailures
                )
            else:
                family = "tpc-ds" if tag == "execute_tpcds" else "tpc-h"
                schema = TPCDS_SCHEMA if family == "tpc-ds" else TPCH_SCHEMA
                rows = list(load_sql_fixture_pairs(f"optimizer/{family}/{family}.sql"))
                hashes = profile[tag]["hashes"]
                good = len(hashes) == len(rows)
                changed_rows = []
                for i in sorted(set().union(*map(set, stages.values()))):
                    if not good:
                        break
                    meta, sql, optimized = rows[i]
                    if i in stages.get("parse", ()) or i in stages.get("opt", ()):
                        expression = parse_packet(sql, meta)
                        agrees = i not in stages.get("parse", ()) or ensure_digest(
                            expression
                        ) == hashes[i].get("parse")
                        if agrees and i in stages.get("opt", ()):
                            optimized_state = optimize_for_executor(expression, schema)
                            agrees = ensure_digest(optimized_state) == hashes[i].get(
                                "opt"
                            )
                        if not agrees:
                            if tag not in tables:
                                good = False
                                break
                            expected, expense = tables[tag][i]
                            budget -= expense
                            if budget < 0:
                                good = False
                                break
                            changed_rows.append((expense, i, expected))
                    if i in stages.get("oracle", ()):
                        a = transpile(sql, write="duckdb")[0]
                        b = (
                            transpile(optimized, write="duckdb")[0]
                            if tag == "optimized_tpch"
                            else ""
                        )
                        if hashlib.sha256(repr((a, b)).encode()).hexdigest() != hashes[
                            i
                        ].get("oracle"):
                            good = False
                            break
                if good:
                    for _, i, expected in sorted(changed_rows):
                        meta, sql, _ = rows[i]
                        if (
                            compute(root, family, parse_packet(sql, meta), meta)
                            != expected
                        ):
                            good = False
                            break
            if good:
                with output.open("a") as stream:
                    stream.write(tag + "\n")
        except Exception:
            continue


if __name__ == "__main__":
    check(
        Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3])
    )
