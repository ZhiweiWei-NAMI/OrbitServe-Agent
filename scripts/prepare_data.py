#!/usr/bin/env python3
"""Acquire only the small, pinned prototype inputs outside the repository."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from orbitserve.data import acquire
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--data-root',type=Path,default=Path(os.environ.get('ORBITSERVE_DATA_ROOT', '~/.cache/orbitserve/data')).expanduser())
p.add_argument('--cases',nargs='*',help='Optional case IDs; earthbench downloads only both benchmark pairs.')
a=p.parse_args()
r=acquire(a.data_root,a.cases)
print(json.dumps({k:v for k,v in r.items() if k not in ['earthbench','cases']},indent=2))
