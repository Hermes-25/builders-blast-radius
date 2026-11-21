"""Map files opened by unittest class setup to the tests that share that fixture."""

import argparse
import ast
import gzip
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    parser.add_argument("catalogue", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    catalogue = args.catalogue.resolve()
    output = args.output.resolve()
    if subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip() != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(
        ["git", "-C", str(root), "diff", "HEAD", "--name-only"]
    ):
        raise ValueError("Expected pristine SQLGlot base revision")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    tests = json.loads(catalogue.read_text())["tests"]
    files = set(t["file"] for t in tests)
    selected = set()
    for path in files:
        if any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "setUpClass"
            for n in ast.walk(ast.parse((root / path).read_bytes()))
        ):
            selected.add(path)
    current = []
    readers = {}

    def audit(event, values):
        if event != "open" or not current or not isinstance(values[0], str):
            return
        path = os.path.abspath(values[0])
        base = str(root) + os.sep
        if path.startswith(base) and not path.endswith((".py", ".pyc")):
            readers.setdefault(path[len(base) :], set()).update(current)

    sys.addaudithook(audit)
    for path in sorted(selected):
        module = importlib.import_module(path[:-3].replace("/", "."))
        classes = sorted(
            {
                t["id"].split("::")[1]
                for t in tests
                if t["file"] == path and len(t["id"].split("::")) == 3
            }
        )
        for name in classes:
            cls = getattr(module, name)
            current = [
                t["id"] for t in tests if t["id"].startswith(path + "::" + name + "::")
            ]
            try:
                cls.setUpClass()
            finally:
                current = []
                cls.tearDownClass()
                cls.doClassCleanups()
    with gzip.GzipFile(str(output), "wb", mtime=0) as stream:
        stream.write(
            json.dumps(
                {p: sorted(v) for p, v in readers.items()},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    print("Class setup fixture files", len(readers), "classes from", sorted(selected))


if __name__ == "__main__":
    main()
