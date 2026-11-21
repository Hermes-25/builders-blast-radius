"""Record pristine collection, setup, registry and import dependencies in separate test-file runs."""

import argparse
import atexit
import builtins
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
import gzip
import importlib
import importlib.machinery
import importlib.util
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import FunctionType,ModuleType

CURRENT="*"
INITIALIZING=[]
REQUIRE=defaultdict(set)
EDGES=defaultdict(set)
STARTED=False


def relevant(name):
    return type(name) is str and (name=="sqlglot" or name.startswith("sqlglot."))


def observe(name):
    if relevant(name):
        REQUIRE[CURRENT].add(name)
        if INITIALIZING and INITIALIZING[-1]!=name:
            EDGES[INITIALIZING[-1]].add(name)


def package_of(obj):
    if isinstance(obj,ModuleType):
        return ModuleType.__getattribute__(obj,"__name__")
    if isinstance(obj,type):
        return type.__getattribute__(obj,"__module__")
    if isinstance(obj,FunctionType):
        return obj.__module__
    return None


@lru_cache(maxsize=1024)
def dialect_lineage(cls):
    values=set()
    for branch in (cls,getattr(cls,"generator_class",None),getattr(cls,"parser_class",None),getattr(cls,"tokenizer_class",None)):
        if isinstance(branch,type):
            values.update(x.__module__ for x in branch.__mro__ if relevant(x.__module__))
    return tuple(values)


def observe_dialect(value):
    if isinstance(value,type):
        for name in dialect_lineage(value):
            observe(name)
    return value


def registry(module):
    meta=module._Dialect
    # Inspect the property's descriptor without triggering class registration.
    prop=vars(meta)["classes"]
    get=vars(meta)["get"].__func__
    item=vars(meta)["__getitem__"].__func__

    def classes(obj):
        ret=prop.fget(obj)
        for value in ret.values():
            observe_dialect(value)
        return ret

    def get_one(obj,*args,**kwargs):
        return observe_dialect(get(obj,*args,**kwargs))

    def index(obj,key):
        return observe_dialect(item(obj,key))

    meta.classes=property(classes)
    meta.get=classmethod(get_one)
    meta.__getitem__=classmethod(index)


def install():
    global STARTED
    if STARTED:return
    STARTED=True
    orig_import=builtins.__import__
    orig_module=importlib.import_module
    loader=importlib.machinery.SourceFileLoader
    orig_exec=loader.exec_module

    def import_one(name,globals=None,locals=None,fromlist=(),level=0):
        absolute=name
        if level and globals:
            try:absolute=importlib.util.resolve_name("."*level+name,globals.get("__package__") or globals.get("__name__"))
            except (ImportError,ValueError,TypeError):pass
        if relevant(absolute):
            observe(absolute)
        result=orig_import(name,globals,locals,fromlist,level)
        if relevant(absolute) and fromlist and isinstance(result,ModuleType):
            mapping=ModuleType.__getattribute__(result,"__dict__")
            items=mapping.values() if "*" in fromlist else (mapping.get(n) for n in fromlist)
            for item in items:observe(package_of(item))
        return result

    def from_path(name,package=None):
        absolute=importlib.util.resolve_name(name,package) if name.startswith(".") else name
        observe(absolute)
        return orig_module(name,package)

    def execute(self,module):
        name=module.__name__
        if not relevant(name):return orig_exec(self,module)
        observe(name)
        INITIALIZING.append(name)
        try:
            result=orig_exec(self,module)
            if name=="sqlglot.dialects.dialect":
                registry(module)
            return result
        finally:INITIALIZING.pop()

    builtins.__import__=import_one
    importlib.import_module=from_path
    loader.exec_module=execute
    atexit.register(flush)


def flush():
    dest=os.environ.get("BR_IMPORT_TMP")
    if not dest:return
    p=Path(dest)/f"{os.getpid()}.json"
    p.write_text(json.dumps({"required":{k:sorted(v) for k,v in REQUIRE.items()},
                             "edges":{k:sorted(v) for k,v in EDGES.items()}}))


def child(context,fn,args,kwargs):
    global CURRENT
    if not getattr(child,"started",False):
        Finalize(None,flush,exitpriority=8)
        child.started=True
    old=CURRENT;CURRENT=context
    try:return fn(*args,**kwargs)
    finally:CURRENT=old


def run_file(root,filename):
    import pytest
    global CURRENT
    os.chdir(root);sys.path.insert(0,str(root));sys.dont_write_bytecode=True
    install()
    submit=ProcessPoolExecutor.submit
    def tracked(pool,fn,/,*args,**kwargs):
        return submit(pool,child,CURRENT,fn,args,kwargs)
    ProcessPoolExecutor.submit=tracked

    class Plugin:
        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self,item):
            global CURRENT
            owner=item.nodeid.rsplit("::",1)[0]
            CURRENT="class:"+owner
            try:return (yield)
            finally:CURRENT="*"

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_call(self,item):
            global CURRENT
            old=CURRENT;CURRENT=item.nodeid
            try:return (yield)
            finally:CURRENT=old

        def pytest_runtest_logreport(self,report):
            if report.when=="call" and report.passed:
                REQUIRE[report.nodeid]
    ret=pytest.main(["-q","--tb=no","-p","no:cacheprovider","--import-mode=prepend",filename],plugins=[Plugin()])
    flush()
    return ret


def build(root,catalogue,out,repair=False):
    rev=subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],text=True).strip()
    if rev!="eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or subprocess.check_output(["git","-C",str(root),"diff","HEAD","--name-only"]):
        raise ValueError("Expected clean pinned checkout")
    specs=json.loads(catalogue.read_text())["tests"]
    modules=sorted({t["file"] for t in specs})
    result={"modules":{},"edges":defaultdict(set),"unknown":[]}
    if repair and out.exists():
        with gzip.open(out,"rt") as stream: previous=json.load(stream)
        modules=previous["unknown"]
        result["modules"]=previous["modules"]
        result["edges"].update({k:set(v) for k,v in previous["edges"].items()})
    for i,filename in enumerate(modules):
        with TemporaryDirectory() as tmp:
            env=os.environ|dict(BR_IMPORT_ROOT=str(root),BR_IMPORT_TMP=tmp,PYTHONDONTWRITEBYTECODE="1",PYTHONPATH=str(root),PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",SKIP_INTEGRATION="0")
            run=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),"--file",filename,str(root)],cwd=root,env=env,capture_output=True)
            if run.returncode:
                result["unknown"].append(filename)
                print(f"unprofiled {filename}: {run.stdout.decode(errors='replace')[-250:]} {run.stderr.decode(errors='replace')[-250:]}",flush=True)
                continue
            groups=defaultdict(set)
            for path in Path(tmp).glob("*.json"):
                info=json.loads(path.read_text())
                for key,names in info["required"].items():groups[key].update(names)
                for key,names in info["edges"].items():result["edges"][key].update(names)
            result["modules"][filename]={k:sorted(v) for k,v in groups.items()}
        if i%10==0:print(f"{i+1}/{len(modules)} files",flush=True)
    result["edges"]={k:sorted(v) for k,v in result["edges"].items()}
    with gzip.GzipFile(str(out),"wb",mtime=0) as stream:
        stream.write(json.dumps(result,sort_keys=True,separators=(",",":")).encode())


if __name__=="__mp_main__" and "BR_IMPORT_ROOT" in os.environ:
    os.chdir(os.environ["BR_IMPORT_ROOT"]);sys.path.insert(0,os.environ["BR_IMPORT_ROOT"]);install()
if __name__=="__main__":
    if sys.argv[1]=="--file":
        raise SystemExit(run_file(Path(sys.argv[3]).resolve(),sys.argv[2]))
    build(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve(),Path(sys.argv[3]).resolve(),"--repair" in sys.argv[4:])
