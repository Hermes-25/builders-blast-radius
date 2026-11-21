# Blast Radius

A regression-test selector for the RWX Builders Buildathon track. It reads the supplied SQLGlot patches and test catalogue from `/data` and writes one scheduling decision per patch.

The dependency index was built from the unchanged [SQLGlot revision](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). It contains Python source for matching patches to the pinned revision, test coverage, and file dependencies. The submitted program uses the Python standard library and runs with the network disabled.

## How selection works

- File reads cover fixture changes and tests that inspect source.
- AST comparison distinguishes changes to functions and callbacks from changes to declarations or module initialization. Coverage includes SQLGlot work delegated to multiprocessing workers, linked back to the test that submitted it.
- A value-only change in a specialized SQL dialect's literal class settings selects tests that exercise that dialect's implementation across parsing, generation, and typing.
- When patch application, syntax, or dependency evidence leaves too much uncertainty, the selector schedules the entire supplied test catalogue.
- For each supported worker count, the scheduler considers the busiest worker's test time plus two seconds per worker. It assigns longer tests first and keeps the cheapest schedule it finds.

## Run with the official sample

Download and extract the [official sample kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz) into a temporary folder. With Python 3.13, run the following from the entry directory, replacing `KIT` with the extracted kit location.

```sh
python selector.py --data KIT/sample > output.jsonl
python KIT/score.py KIT/sample KIT/sample/answers.json output.jsonl
```

To test the container, build with `docker build --platform linux/amd64 -t blast-radius .`. The sample kit's Makefile documents the scorer's read-only mounts and resource limits.

Three runs of the entry in the pinned Linux image with those limits produced identical outputs. The official public scorer counted zero misses across 30 patches and 274.041 simulated seconds per run, excluding the selector's CPU time. The unmodified starter scored 1,210.470 simulated seconds on the same sample.

## Rebuild the index

Use the Python base image and `requirements.lock` from the official kit plus `coverage==7.17.4`. Check out the pinned revision and run `python build_index.py PATH_TO_SQLGLOT impact-index.json.gz`. The generator reads the unchanged source and runs its tests to collect dependencies. It takes neither patches nor answer files as inputs.

The organizer's Python starter supplied the Docker base and CLI contract. SQLGlot source embedded in the index is MIT-licensed under [SQLGLOT-LICENSE](SQLGLOT-LICENSE). The selector and index generator are MIT-licensed under [LICENSE](LICENSE).
