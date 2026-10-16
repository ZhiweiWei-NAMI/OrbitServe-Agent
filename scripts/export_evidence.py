#!/usr/bin/env python3
"""Export small non-image evidence; raw data and full local traces stay external."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def read(path): return json.loads(Path(path).read_text())
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value): Path(path).write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')
def log(path): return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]

def summarize(suite_dir,output_dir):
    suite=Path(suite_dir);out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    index=read(suite/'suite_manifest.json');accept=read(suite/'acceptance.json')
    if index['status']!='PASS' or accept['status']!='PASS':raise RuntimeError('Only a completed passing suite can be exported as final evidence')
    paths=[]
    for value in index['experiments'].values(): paths.extend(value if isinstance(value,list) else [value])
    rows=[]
    for value in dict.fromkeys(paths):
        path=Path(value);manifest=read(path/'run_manifest.json');a=read(path/'acceptance.json')
        row={'run_id':path.name,'experiment':manifest['experiment'],'status':manifest['status'],
             'code_revision':manifest['code']['git_revision'],'source_sha256':manifest['code']['source_sha256'],
             'configuration_sha256':manifest['configuration_sha256'],
             'started_at':manifest['started_at'],'finished_at':manifest['finished_at'],
             'run_manifest_sha256':sha(path/'run_manifest.json'),'acceptance_sha256':sha(path/'acceptance.json')}
        if (path/'runtime_result.json').exists():
            rt=read(path/'runtime_result.json');dag=read(path/'plan.json');f=read(path/'forecast.json')
            row.update(scenario=rt['scenario'],elapsed_wall_s=rt['elapsed_wall_s'],
                       payload_bytes=rt['transferred_payload_bytes'],transfer_count=rt['transfer_count'],
                       forecast_id=f['forecast_id'],forecast_source=f['source_kind'],plan_id=dag['plan_id'],
                       grain=dag['grain'],tile_size=dag['tile_size'],step_count=len(dag['steps']),
                       execution_nodes={c['step_id']:c['node_id'] for c in log(path/'tool_calls.jsonl') if c.get('status')=='running'},
                       products=[{k:x[k] for k in ('path','size_bytes','sha256')} for x in rt['delivery']['files']],
                       statistics={k:v for k,v in read(Path(rt['outputs_dir'])/'stats.json').items() if k in {'case_id','method','threshold','valid_pixels','valid_common_pixels','total_pixels','valid_coverage_fraction','unknown_pixels','unknown_area_m2','gained_pixels','gained_area_m2','lost_pixels','lost_area_m2','net_change_area_m2','class_counts','class_area_m2','pixel_area_m2','area_method','tile_count'}})
            row['product_manifest_sha256']=hashlib.sha256(json.dumps(rt['delivery']['files'],sort_keys=True).encode()).hexdigest()
            if manifest['experiment']!='E1': row.pop('products')
            row['events']=[{k:e[k] for k in ('event','timestamp','source','destination','start_s','end_s','step_id','job_id','offset') if k in e}
                           for e in log(path/'node_events.jsonl') if e.get('event') in ('ground_link_closed','ground_link_opened','data_link_closed','data_link_opened','step_ready','dispatch','delivered')]
            checks=a.get('runtime',{}).get('checks',[])
            row['independent_check_counts']=dict(Counter(c['status'] for c in checks))
        rows.append(row)
    summary={'suite_id':suite.name,'status':'PASS','raw_and_binary_products_in_git':False,
             'experiment_run_ids':{k:[Path(x).name for x in v] if isinstance(v,list) else Path(v).name for k,v in index['experiments'].items()},
             'independent_suite_checks':[{k:c[k] for k in ('check','status') if k in c} for c in accept['checks']],
             'suite_acceptance_sha256':sha(suite/'acceptance.json'),'suite_manifest_sha256':sha(suite/'suite_manifest.json'),
             'feedback_links':[{'preceding_run_id':Path(x['preceding_run']).name,'consuming_run_id':Path(x['consuming_run']).name,'forecast_id':x['forecast_id'],'source_event_ids':x['observation'].get('feedback_event_ids',[])} for x in index['feedback_records']],
             'runs':rows,'limits':['Analytical reference predictor; no learned WM','Rule planner; no LLM','Local rule policies; no MARL','Synthetic contacts/resources/energy, single-host process isolation','Water change candidates; no externally validated accuracy claim']}
    write(out/'acceptance_summary.json',summary)
    data=Path(read(suite/'run_manifest.json')['configuration']['data_root']);acq=read(data/'acquisition_manifest.json')
    inputs=[]
    for c in acq['earthbench']+acq['cases']:
        inputs.append({'case_id':c['case_id'],'assets':[{k:a[k] for k in ('path','phase','band','sha256','bytes','source','item_id','observation_time','scale','offset','reported_offset','earthsearch_boa_offset_applied','scaling_authority_url') if k in a} for a in c['assets']]})
    ledger=log(data/'acquisition.jsonl')
    network_bytes=sum(e.get('network_body_bytes',0) for e in ledger if e.get('event')=='http_range')
    write(out/'data_manifest_summary.json',{'input_raster_bytes':acq['input_bytes'],'successful_cog_response_body_bytes_including_interrupted_attempt':network_bytes,'retained_asset_range_body_bytes':acq['cog_network_body_bytes'],'acquisition_run_id':acq['run_id'],'earthbench_revision':acq['earthbench_revision'],'earth_agent_revision':acq['earth_agent_revision'],'inputs':inputs,'reference_answers_in_repository':False})
    print(json.dumps({'status':'PASS','suite_id':suite.name,'files':['acceptance_summary.json','data_manifest_summary.json']}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('suite_dir',type=Path);p.add_argument('--output-dir',type=Path,default=Path('evidence'));a=p.parse_args();summarize(a.suite_dir,a.output_dir)
