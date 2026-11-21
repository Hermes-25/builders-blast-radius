"""Select expensive tests using measured phase reach, explicit state equivalence and bounded real subtests."""

import gzip, json, os, signal, subprocess, sys, time
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from replay import cpu_used, mirror, schedule_cost
from semantic import statement_span


class FrontierVerifier:
    def __init__(self, root, tests, schedule):
        self.root = root
        self.schedule = schedule
        self.fixture_attributes = json.loads(
            Path(__file__).with_name("fixture-attributes.json").read_text()
        )
        self.durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.records = {}
        for filename in ("frontier.json.gz", "memory-profile.json.gz"):
            with gzip.open(Path(__file__).with_name(filename), "rt") as f:
                raw = json.load(f)
            for tag, rec in raw["tests"].items():
                if not rec["errors"]:
                    self.records[tag] = (raw["startup"], rec)
        self.tags = {}
        for t in tests:
            tag = t["id"].split("::")[-1].removeprefix("test_")
            if tag in self.records and t["file"] in (
                "tests/test_executor.py",
                "tests/test_generator.py",
            ):
                self.tags[t["number"]] = tag
        self.passed = set()
        self.start = time.monotonic()
        self.cpu = cpu_used()

    def plan(self, n, spans, selector):
        tag = self.tags[n]
        startup, record = self.records[tag]
        memory = tag == "generator_memory"
        full = len(record["cpu"])
        layers = {"run": record["lines"]} if memory else record["lines"]
        masks = {}
        for path, positions in spans.items():
            if positions is None:
                return (
                    ({"run": list(range(full))}, sum(record["cpu"]))
                    if memory
                    else (None, 0)
                )
            isolated = selector.isolated[n]["lines"].get(path, ())
            available = {k: v.get(path, {}) for k, v in layers.items()}
            for a, b in positions:
                if any(a <= j <= b for j in startup.get(path, ())):
                    return (
                        ({"run": list(range(full))}, sum(record["cpu"]))
                        if memory
                        else (None, 0)
                    )
                for j in isolated:
                    if a <= j <= b and not any(
                        str(j) in lines for lines in available.values()
                    ):
                        return (
                            ({"run": list(range(full))}, sum(record["cpu"]))
                            if memory
                            else (None, 0)
                        )
                for phase, lines in available.items():
                    for line, mask in lines.items():
                        if a <= int(line) <= b:
                            masks[phase] = masks.get(phase, 0) | mask
        if masks.get("rest") or any(
            k not in ("run", "parse", "opt", "oracle") for k in masks
        ):
            return None, 0
        indices = {
            phase: [i for i in range(full) if mask & (1 << i)]
            for phase, mask in masks.items()
        }
        if memory:
            cost = sum(record["cpu"][i] for i in indices.get("run", ()))
        else:
            cost = sum(
                record["cpu"][i].get(k, 0)
                for k, items in indices.items()
                for i in items
            )
        return indices, cost

    def check(self, selected, text, selector, parse, apply, functions):
        self.passed = set()
        try:
            sections = parse(text)
            if len(sections) == 1 and sections[0][0] in self.fixture_attributes:
                return self.fixture_check(selected, sections[0], selector, apply)
        except (ValueError, UnicodeError):
            return selected
        impacted = self.tags.keys() & selected & selector.isolated.keys()
        if (
            not impacted
            or time.monotonic() - self.start > 65
            or cpu_used() - self.cpu > 40
        ):
            return selected
        cost = lambda group: schedule_cost(group, self.durations, self.schedule) / 1000
        if cost(selected) - cost(selected - impacted) < 1.1:
            return selected
        spans = {}
        changed = {}
        try:
            for path, hunks in parse(text):
                if path not in selector.sources or not path.startswith("sqlglot/"):
                    return selected
                original = selector.sources[path]
                new = apply(original, hunks)
                changed[path] = new
                ranges = functions(original, new)
                if ranges and len(ranges) == 1:
                    part = statement_span(original, new)
                    if part and ranges[0][0] <= part[0] <= part[1] <= ranges[0][1]:
                        ranges = [part]
                spans[path] = ranges
        except (SyntaxError, ValueError, KeyError):
            return selected
        blocked = set().union(*(selector.source_readers.get(p, set()) for p in changed))
        options = {}
        prices = {}
        for n in impacted - blocked:
            plan, price = self.plan(n, spans, selector)
            if plan is None:
                continue
            if not plan:
                self.passed.add(n)
                continue
            options[n] = plan
            prices[n] = price
        remaining = selected - self.passed
        current = cost(remaining)
        chosen = ()
        benefit = 0
        for size in range(1, len(options) + 1):
            for group in combinations(options, size):
                expense = sum(prices[n] for n in group)
                if expense > 5.6:
                    continue
                net = 0.85 * (current - cost(remaining - set(group))) - expense - 0.3
                if net > benefit:
                    benefit = net
                    chosen = group
        if not chosen:
            return remaining
        order = sorted(chosen, key=lambda n: prices[n])
        plan = {self.tags[n]: options[n] for n in order}
        timeout = min(
            7.5,
            max(2.5, 3.8 + 2 * sum(prices[n] for n in chosen))
            if "execute_tpcds" in plan
            else max(2.5, 1.3 + 2 * sum(prices[n] for n in chosen)),
            65 + self.start - time.monotonic(),
            40 - (cpu_used() - self.cpu),
        )
        if timeout < 0.5:
            return remaining
        start = cpu_used()
        try:
            with TemporaryDirectory(prefix="br-stage-") as temp:
                root = Path(temp)
                mirror(self.root, root, changed)
                req = root / "r.json"
                out = root / "verdict"
                req.write_text(json.dumps(plan))
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        str(Path(__file__).with_name("frontier_child.py")),
                        str(root),
                        str(req),
                        str(out),
                    ],
                    env=os.environ
                    | {
                        "PYTHONHASHSEED": "0",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPATH": str(root),
                    },
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                if out.exists():
                    passed = set(out.read_text().splitlines())
                    self.passed.update(n for n in chosen if self.tags[n] in passed)
        except (OSError, ValueError):
            pass
        if os.environ.get("SLICE_DEBUG"):
            print(
                "frontier",
                [
                    (self.tags[n], {s: len(i) for s, i in options[n].items()})
                    for n in chosen
                ],
                sorted(self.passed),
                round(cpu_used() - start, 3),
                file=sys.stderr,
            )
        return selected - self.passed

    def fixture_check(self, selected, section, selector, apply):
        path, hunks = section
        target = self.root / path
        info = self.fixture_attributes[path]
        try:
            if str(self.root) not in sys.path:
                sys.path.insert(0, str(self.root))
            from tests.helpers import load_sql_fixture_pairs

            original = target.read_text(encoding="utf8")
            updated = apply(original, hunks)
            with TemporaryDirectory(prefix="br-fixture-") as tmp:
                newfile = Path(tmp) / "input.sql"
                newfile.write_text(updated, encoding="utf8")
                before = list(load_sql_fixture_pairs(str(target)))
                after = list(load_sql_fixture_pairs(str(newfile)))
        except (OSError, UnicodeError, IndexError, ValueError, KeyError):
            return selected
        class_tests = {
            n for key, n in selector.numbers.items() if key.startswith(info["owner"])
        }
        direct = selector.numbers_for(selector.reads.get(path, ()))
        readers = {
            selector.numbers[k] for k in info["readers"] if k in selector.numbers
        }
        if len(before) == len(after):
            # The real execution pool receives the metadata and input SQL. The
            # independent DuckDB oracle consumes the input and expected SQL.
            for number in list(readers):
                tag = self.tags.get(number)
                if tag in ("execute_tpch", "execute_tpcds") and all(
                    a[:2] == b[:2] for a, b in zip(before, after)
                ):
                    readers.discard(number)
                if tag == "optimized_tpch" and all(
                    a[1:] == b[1:] for a, b in zip(before, after)
                ):
                    readers.discard(number)
        self.passed = (selected & class_tests) - direct - readers
        return selected - self.passed
