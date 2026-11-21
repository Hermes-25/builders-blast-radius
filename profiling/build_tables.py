import os, sys, gzip, json, pickle, time
from pathlib import Path

root = Path(os.environ.get("SQLGLOT_CHECKOUT", "/tmp/repo")).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
os.environ["SKIP_INTEGRATION"] = "0"
from tests.test_executor import (
    TestExecutor,
    initializer,
    mp_execute,
    TPCH_SCHEMA,
    TPCDS_SCHEMA,
)
from sqlglot import parse_one

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from output_core import digest_table

TestExecutor.setUpClass()
out = {}
try:
    for tag, family in [("execute_tpch", "tpc-h"), ("execute_tpcds", "tpc-ds")]:
        is_h = family == "tpc-h"
        case = TestExecutor("test_" + tag)
        initializer(
            TPCH_SCHEMA if is_h else TPCDS_SCHEMA,
            case.tpch_tables if is_h else case.tpcds_tables,
        )
        records = []
        for args, sql, _ in case.tpch_sqls if is_h else case.tpcds_sqls:
            ex, meta = pickle.loads(pickle.dumps((parse_one(sql), args)))
            start = time.process_time()
            table = pickle.loads(pickle.dumps(mp_execute(ex, meta)))
            records.append([digest_table(table), round(time.process_time() - start, 5)])
        out[tag] = records
        print(
            tag,
            len(records),
            "total CPU",
            round(sum(r[1] for r in records), 2),
            flush=True,
        )
finally:
    TestExecutor.tearDownClass()
with gzip.GzipFile(
    os.environ.get("BLAST_PROFILE_OUT", "/trace/table-outputs.json.gz"), "wb", mtime=0
) as f:
    f.write(json.dumps(out, sort_keys=True, separators=(",", ":")).encode())
