import gzip, hashlib, json, os, pickle, sys, time
from pathlib import Path
from collections import defaultdict

root = Path(os.environ.get("SQLGLOT_CHECKOUT", "/tmp/repo")).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frontier_core import ensure_digest, parse_packet

prefix = str(root) + "/"
pathlines = defaultdict(lambda: defaultdict(set))
active = "startup"
tool = sys.monitoring.PROFILER_ID
sys.monitoring.use_tool_id(tool, "phase footprints")


def mark(code, line):
    name = code.co_filename
    if active and name.startswith(prefix + "sqlglot/"):
        pathlines[active][name[len(prefix) :]].add(line)
    return sys.monitoring.DISABLE


def phase(name):
    global active
    active = name
    sys.monitoring.restart_events()


sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, mark)
sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
from tests.test_executor import (
    TestExecutor,
    initializer,
    mp_execute,
    TPCH_SCHEMA,
    TPCDS_SCHEMA,
)
from pandas.testing import assert_frame_equal
from sqlglot import transpile
import sqlglot.executor as executor

TestExecutor.setUpClass()
startup = {k: sorted(v) for k, v in pathlines["startup"].items()}
old_opt = executor.optimize
hashes = {}
clocks = {}


def optim(*a, **kw):
    phase("opt")
    start = time.process_time()
    ex = old_opt(*a, **kw)
    clocks["opt"] = time.process_time() - start
    phase("")
    hashes["opt"] = ensure_digest(ex)
    phase("rest")
    return ex


executor.optimize = optim
all_results = {}
for tag in ("execute_tpch", "execute_tpcds", "optimized_tpch"):
    case = TestExecutor("test_" + tag)
    h = tag != "execute_tpcds"
    seq = case.tpch_sqls if h else case.tpcds_sqls
    header = defaultdict(lambda: defaultdict(dict))
    snap = []
    prices = []
    errors = []
    for i, (meta, sql, optimized) in enumerate(seq):
        hashes = {}
        clocks = {}
        pathlines.clear()
        case.cache.clear()
        try:
            if tag != "optimized_tpch":
                phase("parse")
                t = time.process_time()
                ex = parse_packet(sql, meta)
                clocks["parse"] = time.process_time() - t
                phase("")
                hashes["parse"] = ensure_digest(ex)
                phase("rest")
                initializer(
                    TPCH_SCHEMA if h else TPCDS_SCHEMA,
                    case.tpch_tables if h else case.tpcds_tables,
                )
                result = pickle.loads(pickle.dumps(mp_execute(ex, meta)))
            if tag == "optimized_tpch" or result is not None:
                phase("oracle")
                t = time.process_time()
                a = transpile(sql, write="duckdb")[0]
                b = (
                    transpile(optimized, write="duckdb")[0]
                    if tag == "optimized_tpch"
                    else ""
                )
                clocks["oracle"] = time.process_time() - t
                phase("")
                hashes["oracle"] = hashlib.sha256(repr((a, b)).encode()).hexdigest()
                phase("rest")
                # Fill the original cache using the already captured DuckDB statement, preserving the remaining assertions.
                conn = case.tpch_conn if h else case.tpcds_conn
                case.cache[sql] = conn.execute(a).fetchdf()
                if tag == "optimized_tpch":
                    actual = case.cache[sql]
                    expected = conn.execute(b).fetchdf()
                    case.rename_anonymous(expected, actual)
                    assert_frame_equal(actual, expected)
                else:
                    case.subtestHelper(i, result, tpch=h)
        except Exception as e:
            errors.append((i, type(e).__name__, str(e)[:100]))
        for stage, paths in pathlines.items():
            for path, lines in paths.items():
                rec = header[stage][path]
                for l in lines:
                    rec[str(l)] = rec.get(str(l), 0) | (1 << i)
        snap.append(hashes)
        prices.append(clocks)
    all_results[tag] = {
        "lines": header,
        "hashes": snap,
        "cpu": prices,
        "errors": errors,
    }
    print(
        tag,
        len(seq),
        "errors",
        errors[:3],
        "phase CPU",
        {
            k: round(sum(v.get(k, 0) for v in prices), 3)
            for k in ("parse", "opt", "oracle")
        },
        flush=True,
    )
TestExecutor.tearDownClass()
with gzip.GzipFile(
    os.environ.get("BLAST_PROFILE_OUT", "/trace/frontier.json.gz"), "wb", mtime=0
) as fo:
    fo.write(
        json.dumps(
            {"startup": startup, "tests": all_results},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
