# Blast Radius

This entry selects SQLGlot regression tests for each submitted patch and distributes them over up to eight workers. The Docker image contains the pinned SQLGlot source, test catalogue dependency profiles, and a small runner that can execute actual tests on the patched program.

## Selection and scheduling

1. Apply each diff to the pinned source and compare its Python syntax and compiled code. The baseline profiles record executable lines, fixture reads, operand types, expression metadata, dialect settings and parser dispatch callbacks. Worker processes carry the submitting test's identity. Class-level setup reads belong to the class that shares those fixtures.
2. For the twelve highest-duration public test IDs, an independent run records dependencies from interpreter startup through the test and its workers. This distinguishes a module loaded by another test's startup from code needed by the expensive test itself.
3. For broad declaration edits, intersect the candidate set with separately recorded startup, pytest and worker activity for each test module. Whole-suite runtime coverage, source readers, unprofiled tests and the independent expensive-test profiles remain eligible. Scalar class options use directly observed attribute reads. Literal edits inside lambda bodies use the tests that invoked the corresponding callback. The profile tracks class ownership across inheritance and excludes keys read outside test context.
4. For promising selections, run real, bounded unittest cases on the patched checkout in a disposable process. A matching passing result can remove a test. A failing case adds it. A skipped, unavailable, timed-out or worker-dependent case retains its original decision. Cold execution uses a temporary tree with symlinks to unchanged files. For eligible changes the child reuses pristine imported modules and replaces only compatible function bodies. Tests that directly inspect a changed source file retain their static decision.
5. Compare longest-first schedules for all supported worker counts. Add plausible coverage to the shorter workers while preserving the longest worker's load. The default also allows 120 ms per patch for additional tests near the change. Candidate probes that already passed are excluded from this schedule padding.

Replay starts with a 45-second CPU allowance and an 85-second wall deadline for the batch. Each probe has its own CPU timer and parent-enforced wall timeout. The selector has a 135-second fallback deadline for remaining commits. The scorer rebuilds and starts the image with a read-only filesystem, a private 64 MB temporary directory and networking disabled.

For a change to one existing string key's literal value in a class dictionary, the selector can use baseline reads for that key. Operations that can observe every value contribute all their readers. A recorded map mutation or a shared startup read of the changed value disables that refinement. The profiling pass uses tracked dictionaries on the pristine source. Submitted patches execute on the original dictionary implementation.

## Verifying expensive tests

For the executor's TPC-H, TPC-DS and optimized TPC-H methods, the profile records which query reaches an edited line in each processing stage. It distinguishes parsing, optimization, DuckDB SQL generation and the remaining assertions. A patched child recomputes only the affected parts. It compares complete expression graphs, including metadata and parent links, and the actual strings sent to DuckDB. When an expression changes, the child can execute the affected SQLGlot query on the pinned CSV data and compare typed result values, columns and row order. The precomputed output comes from the real SQLGlot test helper on the pristine checkout. The runtime reproduced all 121 baseline results independently.

A changed downstream stage, an unexplained worker read, an unsupported value, a changed database query or a timeout preserves the affected test. The generator memory test runs its relevant dialect subtests in the patched child. For SQL fixture edits, the original fixture reader must materialize the complete replacement. The selector then consults class-attribute reads and the actual input fields consumed by each executor method. Fixture errors retain the full class selection.

The verifier prioritizes cases whose possible scheduled saving exceeds its measured execution cost. Query verification starts within a 40-second CPU and 65-second wall budget for the batch. A per-patch estimate caps the extra table executions. The independent ordinary-test replay still observes its own deadlines. Timings from profiling influence these choices and can vary by machine.

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
- `profiling/build_modules.py` runs the pristine test files in separate interpreters and merges line events from their child processes. `profiling/build_dispatch.py` records scalar class-option reads and callback invocation sites. `profiling/merge_dispatch.py` combines the full-suite and worker-specific observations with direct option reads in test code.
- `profiling/build_mapping.py` records dictionary readers. `profiling/build_frontier.py` traces SQLGlot queries phase by phase. `profiling/build_tables.py` captures the pristine executor results. `profiling/build_memory.py` records each generator dialect and `profiling/build_fixture_attributes.py` observes the complete executor test class.
- The organizer's starter defines the image digest and command contract. The selector, scheduler and replay implementation are original MIT-licensed code. The regression-selection research that informed the design includes [Gligoric, Eloussi and Marinov](https://users.ece.utexas.edu/~gligoric/papers/GligoricETAL15Ekstazi.pdf) and [Liu and colleagues](https://www.cs.cornell.edu/~legunsen/pubs/LiuETAL23FineRTS.pdf).

The following commands regenerate the local data files. The callback profiles were generated with PYTHON_CPU_COUNT=2, OPENBLAS_NUM_THREADS=1, OMP_NUM_THREADS=1 and NUMEXPR_MAX_THREADS=1. A second run of the submitted profiling code produced the same complete compressed semantic index. Place the runtime indexes at the repository root when building the image.

```sh
export SQLGLOT_CHECKOUT="$SQLGLOT" PYTHONHASHSEED=0 PYTHON_CPU_COUNT=2
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMEXPR_MAX_THREADS=1
python build_index.py "$SQLGLOT" impact-index.json.gz
python build_semantic_index.py "$SQLGLOT" base-semantic.gz
python build_semantic_index.py "$SQLGLOT" worker-semantic.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/build_options.py "$SQLGLOT" base-options.gz
python profiling/build_options.py "$SQLGLOT" worker-options.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/merge_options.py base-options.gz impact-index.json.gz base-semantic.gz option-semantic.gz \
  --semantic-workers worker-semantic.gz --option-workers worker-options.gz
python profiling/profile_class_fixtures.py "$SQLGLOT" "$KIT/sample/tests.json" class-fixtures.gz
python profiling/profile_reads.py "$SQLGLOT" source-reads.json.gz class-fixtures.gz
python profiling/build_isolated.py "$SQLGLOT" "$KIT/sample/tests.json" isolated.json.gz
python profiling/build_modules.py "$SQLGLOT" "$KIT/sample/tests.json" module-profile.json.gz
python profiling/build_dispatch.py "$SQLGLOT" base-dispatch.gz
python profiling/build_dispatch.py "$SQLGLOT" worker-dispatch.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/merge_dispatch.py base-dispatch.gz worker-dispatch.gz impact-index.json.gz option-semantic.gz dispatch-semantic.gz
python profiling/build_mapping.py "$SQLGLOT" full-mapping.gz
python profiling/build_mapping.py "$SQLGLOT" worker-mapping.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/merge_mapping.py full-mapping.gz worker-mapping.gz dispatch-semantic.gz semantic-index.json.gz
BLAST_PROFILE_OUT="$PWD/frontier.json.gz" python profiling/build_frontier.py
BLAST_PROFILE_OUT="$PWD/memory-profile.json.gz" python profiling/build_memory.py
BLAST_PROFILE_OUT="$PWD/fixture-attributes.json" python profiling/build_fixture_attributes.py
BLAST_PROFILE_OUT="$PWD/table-outputs.json.gz" python profiling/build_tables.py
```

## Measured performance

The organizer's scorer reported zero missed commits on all three runs of the 30-patch public set. Each run scheduled 98.011 seconds. Median total time was 102.565 seconds including parent and child CPU under the official two-CPU, four-GB container limits. The same scheduling algorithm supplied with the actual public failures needs 94.351 simulated seconds.

The table compares this entry with its preceding selector. Each independently labelled row lists failures counted as test and patch pairs. A single test can fail on multiple patches.

| Local workload | Previous scheduled seconds | This entry | Observed failures retained |
| --- | ---: | ---: | --- |
| 12 changes in widely used parser and optimizer code | 318.924 | 209.475 | 6 of 6 across 36 full TPC-method executions |
| 4 shared fixture edits | 101.816 | 63.760 | 63 of 63 from complete suite runs |
| 5 mixed code and fixture edits | 34.425 | 16.227 | 8 of 8 from complete suite runs |
| 6 import and class edits | 68.598 | 62.996 | 240 of 240 from complete suite runs |
| 72 mixed changes | 659.925 | 617.921 | All 5 newly omitted expensive test methods passed when executed independently on their respective patches |

On the 12 parser and optimizer changes, measured selector CPU rose from 6.447 to 22.853 seconds. The reduction including CPU was 93.043 seconds. On the 72 mixed changes, CPU fell from 19.085 to 17.427 seconds, a 43.662-second reduction including CPU. The 72-patch batch has no complete failure labels. All nonpublic workloads are locally constructed mutations.

A 40-patch CPU stress batch used 46.554 CPU seconds and 46.738 wall seconds. A 60-patch workload containing stalled probes used 13.340 CPU seconds and 92.820 wall seconds. Both completed in a single-CPU container with a read-only root, a 64 MB temporary filesystem, no network, and a non-root user.
