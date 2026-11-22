"""Merge full-suite and isolated worker contexts into the runtime validation index."""

import gzip
import json
from pathlib import Path
import sys


full, workers, catalog, output = map(Path, sys.argv[1:])
first, second = (json.load(gzip.open(p)) for p in (full, workers))
known = {t['id'] for t in json.loads(catalog.read_text())['tests']}
tests = first['tests']
if set(tests) != known or not set(second['tests']) <= known:
    raise ValueError('Incomplete original test population')
ids = {node:i for i,node in enumerate(tests)}
rows = {}
for data in (first, second):
    for key, indexes in data['rows'].items():
        rows.setdefault(key, set()).update(ids[data['tests'][i]] for i in indexes)
profile = {'tests':tests,'startup':sorted(set(first['startup']) | set(second['startup'])),
           'rows':{key:sorted(values) for key,values in rows.items()}}
with gzip.GzipFile(str(output), 'wb', mtime=0) as stream:
    stream.write(json.dumps(profile,sort_keys=True,separators=(',',':')).encode())
