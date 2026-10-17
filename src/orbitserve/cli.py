"""Reproducible acquisition, scientific baseline, four-node runs and acceptance."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from .evidence import append_jsonl, file_manifest, finish_manifest, new_run, start_manifest, write_json

DEFAULT_DATA=Path(os.environ.get('ORBITSERVE_DATA_ROOT',Path.home()/'.cache/orbitserve/data'))
DEFAULT_RUNS=Path(os.environ.get('ORBITSERVE_RUN_ROOT',Path.home()/'.cache/orbitserve/runs'))

def _read(path): return json.loads(Path(path).read_text())

def _cases(data_root, selected=None):
    paths=[Path(data_root)/'cases'/name for name in selected] if selected else sorted((Path(data_root)/'cases').iterdir())
    if not paths: raise ValueError('No cases acquired')
    for p in paths:
        if not (p/'task.json').exists(): raise FileNotFoundError(p/'task.json')
    return paths

def baseline(data_root, run_root, selected=None, benchmarks=True):
    from .harness import Harness
    from .verification import verify_science
    run_dir=new_run(run_root,'E0')
    inputs=[]
    if benchmarks:
        inputs += [('earthbench_tvdi',Path(data_root)/'earthbench'/q) for q in ['question2','question4']]
    inputs += [('water_full',p) for p in _cases(data_root,selected)]
    start_manifest(run_dir,'E0',[{'case_id':p.name,'files':file_manifest(p,exclude=('outputs','evaluation'))} for _,p in inputs], {'scientific_threshold':0.0,'method':'MNDWI'})
    harness=Harness(run_dir)
    results=[]
    try:
        for tool,p in inputs:
            output=run_dir/'outputs'/p.name
            ids=['sha256:'+r['sha256'] for r in file_manifest(p,exclude=('outputs','evaluation'))]
            job=harness.submit_tool('baseline',tool,ids,{},p.name,case_dir=p,output_dir=output)
            event=harness.wait(job)
            check=verify_science(p,output)
            results.append({'case_id':p.name,'job_id':job,'output_dir':str(output),'verification':check})
            write_json(run_dir/'acceptance.json',{'experiment':'E0','results':results,'status':'IN_PROGRESS'})
            if check.get('status')!='PASS': raise RuntimeError(f'{p.name} independent science verification failed; see {run_dir}/acceptance.json')
        write_json(run_dir/'acceptance.json',{'experiment':'E0','results':results,'status':'PASS'})
        finish_manifest(run_dir,'PASS',results=results)
    except Exception as e:
        write_json(run_dir/'acceptance.json',{'experiment':'E0','results':results,'status':'FAIL','failure_reason':str(e)})
        finish_manifest(run_dir,'FAIL',failure_reason=str(e))
        raise
    finally: harness.close()
    print(json.dumps({'run_id':run_dir.name,'run_dir':str(run_dir),'status':'PASS'}),flush=True)
    return run_dir

def _prepare_plan(case_dir, config=None, forecast_graph=None, history=None):
    from .forecast import forecast
    from .planning import plan
    from .runtime import default_config, observed_snapshot
    config=default_config() if config is None else config
    task=_read(Path(case_dir)/'task.json')
    assets=_read(Path(case_dir)/'assets.json')
    if history:
        previous=deepcopy(history[-1])
        cutoff=float(previous['timestamp'])
        previous['source_observation_timestamp']=cutoff
        previous['source_observation_clock']=previous.get('clock','prior_run_monotonic')
        previous['clock']='next_run_monotonic_epoch'
        previous['timestamp']=0.0
        previous['clock_rebase']='Prior completed run observation imported at next-run initialization; measurements unchanged'
        for node in previous['nodes']:
            node['source_observation_timestamp']=node.get('timestamp',cutoff)
            node['timestamp']=float(node['source_observation_timestamp'])-cutoff
            node['now']=0.0
            if 'busy_until_s' in node: node['busy_until_s']=max(0.0,float(node['busy_until_s'])-cutoff)
        history=[previous]
    else:
        history=[observed_snapshot(config)]
        history[0]['assets']=[{'artifact_id':'sha256:'+a['sha256'],'node_id':a.get('source_node',task.get('source_node','A')),'sha256':a['sha256'],'size_bytes':a.get('bytes',a.get('size_bytes')),'source':'declared_initial_preload_verified_before_plan_downlink'} for a in assets['assets']]
    contacts=config.get('known_contact_plan',config.get('contacts',config.get('links',[])))
    graph=forecast_graph or forecast(history,contacts,120)
    skills={'water_tile':{'capability':'water_tile'},'water_merge':{'capability':'water_merge'}}
    return config,graph,plan(task,assets,skills,graph)

def distributed(case_dir,run_root,scenario='normal',plan_value=None,forecast_graph=None,experiment='E1',history=None):
    from .runtime import run_distributed
    from .verification import verify_science,verify_run
    config,graph,generated=_prepare_plan(case_dir,forecast_graph=forecast_graph,history=history)
    dag=deepcopy(plan_value or generated)
    if dag['forecast_id'] != graph['forecast_id']:
        raise ValueError('A saved plan requires its matching ForecastGraph; supply --forecast with --plan')
    run_dir=new_run(run_root,experiment)
    start_manifest(run_dir,experiment,[{'case_id':Path(case_dir).name,'files':file_manifest(case_dir,exclude=('outputs','evaluation'))}],config)
    write_json(run_dir/'plan.json',dag)
    write_json(run_dir/'forecast.json',graph)
    append_jsonl(run_dir/'forecasts.jsonl',graph)
    append_jsonl(run_dir/'plans.jsonl',dag)
    try:
        result=run_distributed(dag,Path(case_dir),run_dir,scenario=scenario,config=config,forecast_graph=graph)
        write_json(run_dir/'runtime_result.json',result)
        if result.get('status')!='delivered': raise RuntimeError(f'Ground delivery did not finish: {result}')
        scientific=verify_science(case_dir,result['outputs_dir'])
        runtime_check=verify_run(run_dir)
        check={'experiment':experiment,'scenario':scenario,'science':scientific,'runtime':runtime_check,
               'status':'PASS' if scientific.get('status')=='PASS' and runtime_check.get('status')=='PASS' else 'FAIL'}
        write_json(run_dir/'acceptance.json',check)
        finish_manifest(run_dir,check['status'],outputs_dir=result['outputs_dir'],scenario=scenario)
        if check['status']!='PASS': raise RuntimeError(f'Independent run verification failed; see {run_dir}/acceptance.json')
    except Exception as e:
        finish_manifest(run_dir,'FAIL',failure_reason=str(e))
        raise
    print(json.dumps({'run_id':run_dir.name,'run_dir':str(run_dir),'status':'PASS','outputs_dir':result['outputs_dir']}),flush=True)
    return run_dir,result

def main(argv=None):
    p=argparse.ArgumentParser(prog='orbitserve')
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare-data'); a.add_argument('--data-root',type=Path,default=DEFAULT_DATA)
    n=sub.add_parser('nodes'); n.add_argument('--work-dir',type=Path,default=DEFAULT_RUNS/'node-check')
    b=sub.add_parser('baseline'); b.add_argument('--data-root',type=Path,default=DEFAULT_DATA); b.add_argument('--run-root',type=Path,default=DEFAULT_RUNS); b.add_argument('--case',action='append'); b.add_argument('--no-benchmarks',action='store_true')
    r=sub.add_parser('run'); r.add_argument('--data-root',type=Path,default=DEFAULT_DATA); r.add_argument('--run-root',type=Path,default=DEFAULT_RUNS); r.add_argument('--case',required=True); r.add_argument('--scenario',choices=['normal','busy_b','ground_outage','transfer_outage'],default='normal'); r.add_argument('--plan',type=Path); r.add_argument('--forecast',type=Path); r.add_argument('--experiment',default='E1')
    v=sub.add_parser('verify'); v.add_argument('run_dir',type=Path)
    s=sub.add_parser('accept'); s.add_argument('--data-root',type=Path,default=DEFAULT_DATA); s.add_argument('--run-root',type=Path,default=DEFAULT_RUNS)
    args=p.parse_args(argv)
    if args.command=='prepare-data':
        from .data import acquire
        print(json.dumps(acquire(args.data_root),indent=2))
    elif args.command=='nodes':
        from .runtime import check_nodes
        result=check_nodes(args.work_dir)
        print(json.dumps(result,indent=2))
        return 0 if result.get('status')=='checked' else 1
    elif args.command=='baseline': baseline(args.data_root,args.run_root,args.case,not args.no_benchmarks)
    elif args.command=='run': distributed(args.data_root/'cases'/args.case,args.run_root,args.scenario,_read(args.plan) if args.plan else None,_read(args.forecast) if args.forecast else None,args.experiment)
    elif args.command=='verify':
        from .verification import verify_run
        result=verify_run(args.run_dir); print(json.dumps(result,indent=2)); return 0 if result['status']=='PASS' else 1
    elif args.command=='accept':
        from .experiments import run_acceptance
        result=run_acceptance(args.data_root,args.run_root); print(json.dumps(result,indent=2)); return 0 if result['status']=='PASS' else 1
    return 0

if __name__=='__main__': raise SystemExit(main())
