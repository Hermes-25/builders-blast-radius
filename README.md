# Blast Radius

This entry selects SQLGlot regression tests for each submitted patch and distributes them over up to eight workers. The Docker image contains the pinned SQLGlot source, test catalogue dependency profiles, and a small runner that can execute actual tests on the patched program.

## Selection and scheduling

1. Apply each diff to the pinned source and compare its Python syntax and compiled code. The baseline profiles record executable lines, fixture reads, operand types, expression metadata, dialect settings and parser dispatch callbacks. Worker processes carry the submitting test's identity. Class-level setup reads belong to the class that shares those fixtures.
2. For the twelve highest-duration public test IDs, an independent run records dependencies from interpreter startup through the test and its workers. This distinguishes a module loaded by another test's startup from code needed by the expensive test itself.
3. For promising selections, run real, bounded unittest cases on the patched checkout in a disposable process. A matching passing result can remove a test. A failing case adds it. A skipped, unavailable, timed-out or worker-dependent case retains its original decision. Cold execution uses a temporary tree with symlinks to unchanged files. For eligible changes the child reuses pristine imported modules and replaces only compatible function bodies. Tests that directly inspect a changed source file retain their static decision.
4. Compare longest-first schedules for all supported worker counts. Add plausible coverage to the shorter workers while preserving the longest worker's load. The default also allows 120 ms per patch for additional tests near the change. Candidate probes that already passed are excluded from this schedule padding.

Replay starts with a 45-second CPU allowance and an 85-second wall deadline for the batch. Each probe has its own CPU timer and parent-enforced wall timeout. The selector has a 135-second fallback deadline for remaining commits. The scorer rebuilds and starts the image with a read-only filesystem, a private 64 MB temporary directory and networking disabled.

## Running the entry

Build from this directory. Set `KIT` to the extracted [official starter kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz).

```sh
docker build --platform linux/amd64 -t blast-radius .
docker run --rm --network none --cpus 2 --memory 4g --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --user 65534:65534 -e HOME=/tmp \
  -v "$KIT/sample/commits:/data/commits:ro" \
  -v "$KIT/sample/tests.json:/data/tests.json:ro" blast-radius > output.jsonl
python "$KIT/score.py" "$KIT/sample" "$KIT/sample/answers.json" output.jsonl
```

For a static-only comparison, append `python /app/selector.py --static` to the Docker command. To measure scheduling without padding, add `--guard -1`. A zero allowance permits only additions that leave the simulated score unchanged.

## Provenance and regeneration

All coverage and state profiles come from the unmodified [SQLGlot revision eb11aec4](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). The pinned source archive contains `sqlglot/`, `tests/`, `pyproject.toml`, and SQLGlot's MIT license. Regenerate it with `git -C "$SQLGLOT" archive --format=tar.gz --output="$OUT/pinned-source.tar.gz" eb11aec4be8cb2122afe713a4816662c3f0fb2fa sqlglot tests pyproject.toml LICENSE`. Its SHA-256 is `70c7f2a4ac03e6779101f56057bcff1cf5d79b1e9a6df21f6c92fde8166974a9`.

The vendored `pytz` 2026.6 wheel supplies the version specified in the organizer's dependency lock. Its SHA-256 is `7fed3fee8fd6bd4a2b7b75a8d56021d0624127bf69bd5691f115db929ac570b3`. It includes its upstream license and can be fetched with `python -m pip download --only-binary :all: --no-deps pytz==2026.6`. The container installs it offline.

Use the kit's Python 3.13 image and dependency lock plus `coverage==7.17.4` for profiling. The build-time scripts accept a pristine checkout. The public catalogue supplies test IDs and timing estimates. The scripts do not take mutation patches or answer files as input.

- `build_index.py` records test lines and file reads, including pool workers. `build_semantic_index.py` records operand categories and class consumers.
- `profiling/build_options.py` observes literal dialect options. `profiling/merge_options.py` joins those observations to the semantic profile. The submitted indexes combine full-suite runs with a separate worker-aware pass on the optimizer and executor suites.
- `profiling/profile_reads.py` records source files explicitly read outside the import machinery. `profiling/profile_class_fixtures.py` records reads during class setup. The source-read index includes both datasets. `profiling/build_isolated.py` runs the expensive tests individually and uses the adjacent `sitecustomize.py` to capture startup and child processes.
- The organizer's starter defines the image digest and command contract. The selector, scheduler and replay implementation are original MIT-licensed code. The regression-selection research that informed the design includes [Gligoric, Eloussi and Marinov](https://users.ece.utexas.edu/~gligoric/papers/GligoricETAL15Ekstazi.pdf) and [Liu and colleagues](https://www.cs.cornell.edu/~legunsen/pubs/LiuETAL23FineRTS.pdf).

One regeneration sequence is below. All output paths are local files. Use the generated runtime indexes at the repository root when building the image.

```sh
python build_index.py "$SQLGLOT" impact-index.json.gz
python build_semantic_index.py "$SQLGLOT" base-semantic.gz
python build_semantic_index.py "$SQLGLOT" worker-semantic.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/build_options.py "$SQLGLOT" base-options.gz
python profiling/build_options.py "$SQLGLOT" worker-options.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/merge_options.py base-options.gz impact-index.json.gz base-semantic.gz semantic-index.json.gz \
  --semantic-workers worker-semantic.gz --option-workers worker-options.gz
python profiling/profile_class_fixtures.py "$SQLGLOT" "$KIT/sample/tests.json" class-fixtures.gz
python profiling/profile_reads.py "$SQLGLOT" source-reads.json.gz class-fixtures.gz
python profiling/build_isolated.py "$SQLGLOT" "$KIT/sample/tests.json" isolated.json.gz
```

## Measured performance

Three paired Linux runs used the official 30 public patches, read-only mounts and resource limits. The replay candidate scheduled all expected failures. Its median total was 122.905 seconds including selector and child-process CPU. Each run used 115.976 simulated seconds and completed within eight wall-clock seconds. The previous published entry measured 151.047 seconds on the same host. Practice and final scores depend on hidden data.

Five independent mutations were each run against the complete pinned test suite. They produced eight failing test/patch pairs. The candidate scheduled all eight and scored 38.501 seconds including CPU on that batch. The earlier entry scored 323.922 seconds including one missed commit. Its schedule overlooked an executor test that reads a TPC-H query through class setup. On this batch the candidate spent 12.239 additional simulated test and worker seconds to retain the shared-fixture coverage.
