"""Record the original generator-memory assertion per dialect while retaining its complete warmup and measurement loops."""

import gzip, json, os, sys, time
from pathlib import Path
from collections import defaultdict

root = Path(os.environ.get("SQLGLOT_CHECKOUT", "/tmp/repo")).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
prefix = str(root) + "/"
cur = defaultdict(set)
tool = sys.monitoring.PROFILER_ID
sys.monitoring.use_tool_id(tool, "per-dialect memory assertion")


def line(code, n):
    if code.co_filename.startswith(prefix + "sqlglot/"):
        cur[code.co_filename[len(prefix) :]].add(n)
    return sys.monitoring.DISABLE


sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, line)
sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
from tests import test_generator as target

startup = {p: sorted(ls) for p, ls in cur.items()}
members = list(target.Dialects)
groups = defaultdict(dict)
errors = []
costs = []
for idx, member in enumerate(members):
    target.Dialects = (member,)
    cur.clear()
    sys.monitoring.restart_events()
    st = time.process_time()
    try:
        target.TestGenerator("test_generator_memory").test_generator_memory()
    except Exception as e:
        errors.append((str(member), type(e).__name__, str(e)[:100]))
    costs.append(round(time.process_time() - st, 5))
    for path, ls in cur.items():
        for l in ls:
            group = groups[path]
            key = str(l)
            group[key] = group.get(key, 0) | (1 << idx)
record = {
    "startup": startup,
    "tests": {
        "generator_memory": {
            "count": len(members),
            "members": [member.value for member in members],
            "cpu": costs,
            "lines": groups,
            "errors": errors,
        }
    },
}
with gzip.GzipFile(
    os.environ.get("BLAST_PROFILE_OUT", "/trace/memory-profile.json.gz"), "wb", mtime=0
) as out:
    out.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
print(
    "Memory subtests", len(members), "failures", errors, "cpu", sum(costs), flush=True
)
