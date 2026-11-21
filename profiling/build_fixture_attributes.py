"""Attribute fixture collections allocated at class setup to the test methods that access them."""

import gzip, json, os, sys
from pathlib import Path

root = Path(os.environ.get("SQLGLOT_CHECKOUT", "/tmp/repo")).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
os.environ["SKIP_INTEGRATION"] = "0"
from tests import test_executor as target
from tests.helpers import FIXTURES_DIR
import pytest

C = target.TestExecutor
load = target.load_sql_fixture_pairs
setup = C.setUpClass.__func__
get = C.__getattribute__
files = {}
attrs = {}
owners = {}
ctx = ""


def loader(name):
    rows = list(load(name))
    if rows:
        files[id(rows[0])] = (Path(FIXTURES_DIR) / name).relative_to(root).as_posix()
    return iter(rows)


def start(cls):
    setup(cls)
    for key, val in vars(cls).items():
        if type(val) is list and val and id(val[0]) in files:
            path = files[id(val[0])]
            attrs[key] = path
            owners.setdefault(path, set())


def reader(self, name):
    if ctx and name in attrs:
        owners[attrs[name]].add(ctx)
    return get(self, name)


target.load_sql_fixture_pairs = loader
C.setUpClass = classmethod(start)
C.__getattribute__ = reader


class P:
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        global ctx
        ctx = item.nodeid
        try:
            return (yield)
        finally:
            ctx = ""


if __name__ != "__main__":
    pass
else:
    p = P()
    code = pytest.main(
        [
            "-q",
            "-p",
            "no:cacheprovider",
            "--import-mode=prepend",
            "tests/test_executor.py",
        ],
        plugins=[p],
    )
    if code:
        raise ValueError("Pristine executor suite failed")
    out = {
        p: {
            "owner": "tests/test_executor.py::TestExecutor::",
            "attr": a,
            "readers": sorted(owners[p]),
        }
        for a, p in attrs.items()
    }
    Path(
        os.environ.get("BLAST_PROFILE_OUT", "/trace/fixture-attributes.json")
    ).write_text(json.dumps(out, indent=2, sort_keys=True))
    print(out, flush=True)
