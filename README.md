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

## Observed operand values and fixture rows

On the unmodified checkout, a separate profiling pass records primitive values at comparisons, the shape and object identity relationships in arguments passed to `seq_get`, and boolean inputs in branch conditions. A second pass covers the optimizer and executor workers independently. The merged profile contains 3,222 source positions from 1,620 passing tests. Runtime code interprets the submitted expression change against these observations. If an operand is unknown, a site ran at startup, or a separately traced expensive test reached a line without a corresponding observation, the selector retains its earlier coverage decision. The observation files contain baseline program values and test IDs. The method draws on [Just and colleagues' work on state infection](https://homes.cs.washington.edu/~mernst/pubs/state-infection-issta2014-abstract.html).

For TPC fixture changes, the selector uses the original fixture reader and replays each changed row through the original optimizer test method. SQL generation, optimizer assertions and worker result types must still agree. For assertions that use a database, unchanged SQL inputs establish that the database receives the same operations as the pristine test. A new or unsupported database operation keeps the test selected. A separate trace of the entire executor class records test-call file reads so class setup does not assign a shared file to whichever test ran first.

When one Python source file and one or more SQL fixtures change, the selector combines source observations with fixture readers. Every replay uses the complete patch. For TPC files, the selector checks which inputs each original executor test receives. A changed title or expected SQL can leave the production query and execution flag identical. Such a test still runs if the production change can affect it. A changed query string, an execution-flag change for a relevant method, an unverified reader, or failed fixture loading keeps the broader decision. For changes to two production files, a caller and its changed callee can interact. The selector can compare both sides of a direct sequence-index caller edit together when it has a baseline profile for the pure callee. Other combinations retain broader coverage.

For five small Python helpers, the selector records original call sites and argument shapes. When an eligible body changes, it evaluates the old and new restricted expressions on those same inputs. Sequence elements whose contents are opaque retain their identity relationships. Unsupported operations keep the test selected. A separate cold run records the entire import and test lifetime for three expensive cases.

For top-level literal and import-alias changes, the selector combines baseline external module-attribute reads with source locations that load the affected global name. Reading a name while defining a class or module can affect later users. Declaration reads, re-exports, namespace reflection and uncertain imports keep broader coverage. The selector also tries importing the patched module and its dialects in an isolated process before using a narrower selection.

Some tokenizer attributes travel through a constructor before they affect tokenization. For scalar options, a separate baseline trace records the tests that read the downstream field or directly access the original option. Options observed during startup retain broader coverage. The distinction between an executed source statement and a changed value reaching an observable result is related to work on regression-test selection and state infection. [Rothermel and Harrold](https://www.cs.purdue.edu/homes/xyzhang/spring07/Papers/p173-rothermel.pdf) and [Just and colleagues](https://homes.cs.washington.edu/~mernst/pubs/state-infection-issta2014-abstract.html).

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
BLAST_FIXTURE_INPUT="$PWD/fixture-attributes.json" BLAST_PROFILE_OUT="$PWD/fixture-attributes.json" python profiling/build_fixture_calls.py
python profiling/build_values.py "$SQLGLOT" values-full.gz
python profiling/build_values.py "$SQLGLOT" values-workers.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/build_predicates.py "$SQLGLOT" predicates-full.gz
python profiling/build_predicates.py "$SQLGLOT" predicates-workers.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/merge_values.py values-full.gz values-workers.gz operand-values.json.gz --extra predicates-full.gz predicates-workers.gz
python profiling/build_helper.py "$SQLGLOT" helper-full.gz
python profiling/build_helper.py "$SQLGLOT" helper-workers.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/build_helper_cold.py "$SQLGLOT" helper-parser.gz tests/test_parser.py::TestParser::test_max_nodes
python profiling/build_helper_cold.py "$SQLGLOT" helper-ds.gz tests/test_executor.py::TestExecutor::test_execute_tpcds
python profiling/build_helper_cold.py "$SQLGLOT" helper-h.gz tests/test_executor.py::TestExecutor::test_execute_tpch
python profiling/merge_helpers.py helper-full.gz helper-workers.gz helper-inputs.json.gz helper-parser.gz helper-ds.gz helper-h.gz
python profiling/build_bindings.py "$SQLGLOT" binding-full.gz
python profiling/build_bindings.py "$SQLGLOT" binding-workers.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/merge_bindings.py binding-full.gz binding-workers.gz binding-reads.json.gz impact-index.json.gz
python profiling/build_option_flow.py "$SQLGLOT" flow-full.gz
python profiling/build_option_flow.py "$SQLGLOT" flow-workers.gz --tests tests/test_optimizer.py tests/test_executor.py
python profiling/merge_option_flow.py flow-full.gz flow-workers.gz impact-index.json.gz forward-reads.json.gz
```

## Earlier measured revisions

An earlier official practice revision scored 288.0 seconds with zero missed commits on three runs. The public sample, locally generated holdouts and hidden practice batch contain different patches.

Three local runs under the official two-CPU limits each scheduled 98.011 seconds and missed zero of the 30 public commits. Median total time including selector CPU was 106.234 seconds. Running the same scheduler on only the public failing IDs costs 94.351 simulated seconds.

| Local workload | Preceding official revision, scheduled seconds | This entry, scheduled seconds | Observed failing test/patch pairs retained |
| --- | ---: | ---: | --- |
| 8 scalar, branch and index edits | 57.717 | 28.804 | 40 of 40, complete pinned suite |
| 6 additional branch and comparison edits | 40.737 | 29.534 | 5 of 5, complete pinned suite |
| 6 multi-file edits including an interacting caller and callee | 150.880 | 93.509 | 178 of 178, complete pinned suite |
| 5 independently combined source and TPC fixture edits | 130.955 | 87.656 | 19 of 19, complete pinned suite |
| 4 shared fixture edits | 63.760 | 35.578 | 63 of 63, complete pinned suite |
| 9 additional TPC fixture edits | 135.211 | 38.411 | 7 of 7, original executor and optimizer methods |
| 3 duplicate or invalid TPC query pairs | 26.703 | 19.647 | 3 of 3, original executor and optimizer methods |

Five earlier mixed edits and six structural mutations also retained all 248 failures from complete suite runs. The six multi-file edits above cost 152.132 seconds for the preceding official revision and 98.388 seconds for this entry, including measured CPU. The jointly changed caller and callee remained the costliest selection there. Its schedule cost 36.817 seconds against 6.633 seconds using only its known failures.

Across three alternating runs on a separate 72-patch mixed workload, median total time including CPU fell from 647.120 to 615.686 seconds. Median scheduled time fell from 617.921 to 590.096. Only two patches changed the selected IDs. Complete original-suite runs confirmed that all 2,814 newly omitted test/patch pairs passed. The entire 72-patch set has partial failure labels.

On 12 mutations targeting widely shared TPC code, three alternating runs produced median totals of 232.894 seconds for the preceding official revision and 213.702 for this one. All six failures observed over 36 original TPC-method executions were retained. Selector runtimes and probe deadlines can vary under load.

A 40-patch CPU stress batch returned every schedule in 51.561 wall seconds using 50.520 CPU seconds. The 60-patch wall-stall batch returned every schedule in 90.585 wall seconds using 9.093 CPU seconds. Both used a one-CPU container with a read-only root, 64 MB of temporary storage, a non-root user and networking disabled.

## Call-input and binding validation

Six fresh helper changes reduced scheduled time from 122.191 to 90.835 seconds, retaining all 16 failures found by the complete pinned suite. Six previously labelled multi-file changes fell from 93.509 to 86.110 seconds with all 178 failures retained. This includes a patch that changes a caller and its callee together.

The benchmark and source-profiling commands use separate directories. Only the unchanged pinned program is used to build the submitted profiles. Full-suite mutation outcomes are used for evaluation.

Across six additional top-level binding changes, scheduled cost fell from 204.182 to 150.928 seconds. All 1,527 independently observed failing pairs were retained, including tests from 34 files that failed to import.

## Forwarded tokenizer fields

Four edits to distinct tokenizer settings reduced scheduled time from 147.268 to 16.597 seconds. Selector CPU changed from 1.147 to 2.666 seconds. Full runs of the original test suite retained all 13 failing test/patch pairs. A fifth edit to a PostgreSQL subclass passed all 1,620 baseline-active tests.

On a separate set of 72 mixed patches the previous release scheduled 590.096 seconds and this entry scheduled 541.328. Two runs of each gave mean totals of 624.295 and 576.871 seconds after adding CPU. Three predictions changed. Independent full-suite runs verified that all 1,581 newly omitted test/patch pairs passed.

The 30 public sample patches scheduled 98.011 seconds in all three runs with the organizer's two-core restrictions. All 73 failing pairs were retained, with a median total of 103.718 seconds including CPU. A 40-patch CPU stress test took 46.202 wall seconds. A separate 60-patch wall-stall test returned every schedule in 86.421 seconds.

## Import failures and namespace reads

The baseline profiler runs each of the 58 test files in its own interpreter. It records imports during collection, method execution, class setup and teardown, and worker execution. It also records when the dialect registry returns a particular parser, generator, tokenizer, or dialect class, including inherited classes and registry enumeration. All 1,620 catalogue tests have an execution record. The profile comes from the unmodified SQLGlot revision.

To regenerate the graph in the organizer's locked Python environment, run the following from this repository with the clean pinned checkout and public catalogue as inputs.

```sh
export PYTHONHASHSEED=0 PYTHON_CPU_COUNT=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
python profiling/build_import_graph.py /path/to/pinned-sqlglot /path/to/sample/tests.json first.gz
python profiling/build_import_graph.py /path/to/pinned-sqlglot /path/to/sample/tests.json second.gz
python profiling/merge_import_graphs.py first.gz second.gz import-graph.json.gz
```

For a single import alias edit in a concrete dialect component, a short-lived subprocess tries to load the patched module. The narrower dependency model requires a fresh alias and a `NameError` for the original binding in the affected module. It keeps the tests that required that module or a dependent module and reserves the live test budget for other changes. On a timeout, alias collision, or different exception, normal selection and replay continue. Two baseline-proven class namespace reads can also be distinguished from inspection of module globals.

Four independently constructed changes in Athena, Exasol, Solr and StarRocks were run against the full original test suite. All 253 failing test/patch pairs were selected. Their simulated schedule took 54.242 seconds, matching the schedule constructed from the observed failures in each case. Total cost including selector CPU fell from 72.145 to 57.772 seconds.

On a 72-patch mixed workload, 12 import changes accounted for 269.972 seconds of the previous 583.933 simulated seconds. This revision reduced the import changes to 229.935 seconds. Total cost including CPU fell from 632.118 to 591.434 seconds in the paired restricted runs. This mixed workload measures scheduling cost. The independent full-suite results above measure failure coverage.

Three public sample runs each retained all 73 failure pairs and scheduled 98.011 seconds. Separate 40- and 60-patch stress runs returned every schedule in 54.038 and 89.282 wall seconds, respectively.
