"""Small, explicit run provenance helpers; no verdicts are inferred from log prose."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

LOG_NAMES = ('forecasts','plans','decisions','tool_calls','transfers','node_events','control_messages','observations')

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''):
            h.update(b)
    return h.hexdigest()

def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':')).encode()).hexdigest()

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n')

def append_jsonl(path, value):
    with open(path, 'a') as f:
        f.write(json.dumps(value, sort_keys=True, allow_nan=False)+'\n')

def new_run(root, experiment):
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+experiment+'-'+uuid4().hex[:8]
    path=Path(root)/run_id
    path.mkdir(parents=True, exist_ok=False)
    for name in LOG_NAMES: (path/(name+'.jsonl')).touch()
    return path

def file_manifest(root, exclude=()):
    root=Path(root)
    return [{'path':str(p.relative_to(root)), 'size_bytes':p.stat().st_size,'sha256':sha256(p)}
            for p in sorted(root.rglob('*')) if p.is_file() and not any(x in p.parts for x in exclude)]

def code_inventory():
    root=Path(__file__).resolve().parents[2]
    rows=[]
    for d in ['src','configs','scripts','contracts']:
        rows += [{'path':str(p.relative_to(root)), 'sha256':sha256(p)} for p in sorted((root/d).rglob('*')) if p.is_file() and '__pycache__' not in p.parts]
    try: revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,stderr=subprocess.DEVNULL,text=True).strip()
    except (subprocess.CalledProcessError,FileNotFoundError): revision='uncommitted'
    return {'git_revision':revision,'source_sha256':canonical_hash(rows),'source_files':rows}

def start_manifest(run_dir, experiment, inputs, config=None):
    run_dir=Path(run_dir)
    manifest={'run_id':run_dir.name,'experiment':experiment,'started_at':datetime.now(timezone.utc).isoformat(),
      'command':sys.argv,'status':'IN_PROGRESS','clock_mode':'monotonic_wall_clock_1x',
      'code':code_inventory(),'configuration':config or {},'configuration_sha256':canonical_hash(config or {}),
      'inputs': inputs, 'python':platform.python_version(),'platform':platform.platform(),
      'dependencies':{p:importlib.metadata.version(p) for p in ['numpy','rasterio','scipy','Pillow','pyproj']}}
    write_json(run_dir/'run_manifest.json',manifest)
    return manifest

def finish_manifest(run_dir, status, **kwargs):
    p=Path(run_dir)/'run_manifest.json'
    m=json.loads(p.read_text())
    m.update(status=status,finished_at=datetime.now(timezone.utc).isoformat(),**kwargs)
    write_json(p,m)
    return m
