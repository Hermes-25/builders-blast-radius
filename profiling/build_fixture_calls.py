"""Trace direct fixture file reads during each original executor test's call and setup phases."""

import collections, json, os, sys
from pathlib import Path
import pytest

root = Path(os.environ.get("SQLGLOT_CHECKOUT", "/tmp/repo")).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
os.environ["SKIP_INTEGRATION"] = "0"
fixtures = json.loads(Path(os.environ["BLAST_FIXTURE_INPUT"]).read_text())
targets = {str(root / path): path for path in fixtures}
seen = collections.defaultdict(set)
phase = ""


def audit(event, args):
    if event == "open" and phase and isinstance(args[0], str):
        path = targets.get(args[0])
        if path:
            seen[path].add(phase)


sys.addaudithook(audit)


class Profile:
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        global phase
        phase = item.nodeid
        try:
            return (yield)
        finally:
            phase = ""


if __name__ == "__main__":
    code = pytest.main(
        [
            "-q",
            "-p",
            "no:cacheprovider",
            "--import-mode=prepend",
            "tests/test_executor.py",
        ],
        plugins=[Profile()],
    )
    if code:
        raise SystemExit(code)
    for path, rec in fixtures.items():
        rec["direct"] = sorted(seen[path])
    Path(os.environ["BLAST_PROFILE_OUT"]).write_text(
        json.dumps(fixtures, indent=2, sort_keys=True)
    )
    print({p: v["direct"] for p, v in fixtures.items()})
