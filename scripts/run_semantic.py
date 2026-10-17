#!/usr/bin/env python3
"""Export a real agent request, import arbitrary DAG JSON, or replay fixtures."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from orbitserve.semantic import load_case,request,recipe,execute,read,call_openai_compatible
from orbitserve.semantic_ops import write_json


def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='command',required=True)
 for name in ('export-request','fixture','run','call-api'):
  q=sub.add_parser(name); q.add_argument('--case-id',required=True); q.add_argument('--cases',type=Path)
  q.add_argument('--data-root',type=Path,default=Path(os.environ.get('ORBITSERVE_DATA_ROOT','/workspace/shared/orbitserve-data')))
  q.add_argument('--scenario',choices=['normal','future_contact','busy_local'],default='normal')
  if name!='fixture': q.add_argument('--arm',default='agent_only' if name in ('export-request','call-api') else 'non_agent')
  if name in ('export-request','fixture','call-api'): q.add_argument('--output',type=Path,required=True)
  if name=='run': q.add_argument('--plan',type=Path); q.add_argument('--run-dir',type=Path,required=True); q.add_argument('--planning',type=Path)
  if name=='call-api':
   q.add_argument('--endpoint',default=os.environ.get('ORBITSERVE_API_ENDPOINT'))
   q.add_argument('--model',default=os.environ.get('ORBITSERVE_API_MODEL'))
   q.add_argument('--key-env',default='ORBITSERVE_API_KEY'); q.add_argument('--timeout',type=float,default=180)
 args=p.parse_args(argv); case=load_case(args.case_id,args.cases)
 if args.command=='export-request':
  args.output.parent.mkdir(parents=True,exist_ok=True); write_json(args.output,request(case,args.data_root,args.arm,args.scenario)); print(args.output)
 elif args.command=='fixture':
  args.output.parent.mkdir(parents=True,exist_ok=True); write_json(args.output,recipe(case)); print(json.dumps({'path':str(args.output),'mode':'fixture_replay','intelligence_claim':False}))
 elif args.command=='call-api':
  if not args.endpoint: p.error('--endpoint or ORBITSERVE_API_ENDPOINT is required')
  exported=request(case,args.data_root,args.arm,args.scenario)
  plan,planning=call_openai_compatible(exported,args.endpoint,args.model,args.key_env,args.timeout)
  args.output.parent.mkdir(parents=True,exist_ok=True); write_json(args.output,plan); write_json(args.output.with_suffix('.planning.json'),planning)
  write_json(args.output.with_suffix('.request.json'),exported)
  print(json.dumps({'path':str(args.output),'mode':'real_api','usage':planning['usage']}))
 else:
  allowed={'non_agent','fixture_agent_only','fixture_agent_wm_offload','fixture_agent_dynamic_no_wm','external_agent_only','external_agent_wm_offload','external_agent_dynamic_no_wm'}
  if args.arm not in allowed: p.error('Unsupported execution arm')
  if args.arm.startswith('external_') and not args.plan: p.error('External execution requires --plan; no fixture fallback')
  if args.arm=='non_agent' and args.plan: p.error('non_agent uses the declared recipe, not an external plan')
  plan=read(args.plan) if args.plan else recipe(case)
  result=execute(plan,case,args.data_root,args.run_dir,args.arm,args.scenario,read(args.planning) if args.planning else None)
  print(json.dumps({'case_id':case['id'],'status':result['status'],'mode':result['mode'],'node_count':result['node_count'],
                    'wall_s':result['execution']['wall_s'],'result':str(args.run_dir/'result.json'),'error':result.get('error')}))
  return 0 if result['status'] in ('PASS','NEEDS_DATA') else 1
 return 0

if __name__=='__main__': raise SystemExit(main())
