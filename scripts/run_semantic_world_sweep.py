#!/usr/bin/env python3
"""Prespecified paired synthetic-world sensitivity analysis of completed real DAGs.

Prepare first, inspect protocol.json/worlds.json, then execute --run. A world is
one statistical cluster containing every product and all four policy controls.
No scientific computation or model API request is repeated by this driver.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from orbitserve.semantic_model import environment, simulate

CASES = ['water_area_mead', 'rgb_quicklook_mead', 'water_vector_rgb_mead']
FAMILIES = ['normal', 'busy_local', 'future_contact']
CONTROLS = {'local_current': (False, False), 'local_forecast': (True, False),
            'dynamic_current': (False, True), 'dynamic_forecast': (True, True)}
COMPARISONS = [('local_current', 'dynamic_current', 'offloading_without_forecast'),
               ('dynamic_current', 'dynamic_forecast', 'incremental_forecast'),
               ('local_current', 'dynamic_forecast', 'forecast_plus_offloading'),
               ('local_current', 'local_forecast', 'forecast_without_offloading')]


def read(path): return json.loads(Path(path).read_text())
def write(path, value): Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def csvwrite(path, rows):
    with Path(path).open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        w.writeheader(); w.writerows(rows)


def inventory():
    paths = [REPO / 'src/orbitserve/semantic_model.py', Path(__file__), REPO / 'configs/semantic_cases.json']
    return {str(p.relative_to(REPO)): sha(p) for p in paths}


def prepare(root, source, count, seed):
    if (root / 'protocol.json').exists(): raise ValueError('Protocol exists. Use --run or a new root; no silent resampling.')
    root.mkdir(parents=True, exist_ok=True)
    inputs = []
    for record_path in sorted((source / 'runs').glob('*/runner_record.json')):
        rec = read(record_path)
        if rec['case_id'] not in CASES or rec['arm'] != 'non_agent' or rec['scenario'] != 'normal': continue
        run = record_path.parent; result = read(run / 'result.json')
        if result['status'] != 'PASS' or result['quality']['status'] != 'PASS': raise ValueError('Invalid source science')
        inputs.append({'case_id': rec['case_id'], 'plan_path': str(run / 'plan.json'),
            'result_path': str(run / 'result.json'), 'plan_file_sha256': sha(run / 'plan.json'),
            'result_file_sha256': sha(run / 'result.json'), 'plan_sha256': result['plan_sha256']})
    if sorted(x['case_id'] for x in inputs) != sorted(CASES): raise ValueError('Missing or duplicate product sources')
    # Separate spawned streams: families are independently sampled, not copied.
    streams = np.random.SeedSequence(seed).spawn(len(FAMILIES)); worlds = []
    for family, stream in zip(FAMILIES, streams):
        rng = np.random.default_rng(stream)
        for i in range(count):
            rate = float(np.exp(rng.uniform(np.log(250_000), np.log(1_000_000))))
            ratio = float(np.exp(rng.uniform(np.log(.5), np.log(12))))
            bandwidth = float(np.exp(rng.uniform(np.log(200_000), np.log(50_000_000))))
            reservation_start, reservation_duration = float(rng.uniform(.02,.15)), float(rng.uniform(10,60))
            contact_open, contact_closed = float(rng.uniform(.01,.2)), float(rng.uniform(5,60))
            env = environment(family); p = env['parameters']
            p['node_rates_work_s'] = {'A': rate, 'C': rate * ratio}; p['network_bytes_s'] = bandwidth
            for n in env['current_observation']['nodes']: n['work_units_s'] = p['node_rates_work_s'][n['id']]
            for link in env['current_observation']['links']: link['bytes_s'] = bandwidth
            forecast = env['forecast']
            forecast['contact_windows'] = [[0,contact_open],[contact_open+contact_closed,600]] if family == 'future_contact' else [[0,600]]
            forecast['resource_reservations'] = [{'node':'A','start_s':reservation_start,'end_s':reservation_start+reservation_duration}] if family == 'busy_local' else []
            env['truth'] = deepcopy(forecast); env['deadline_s'] = 60.0
            worlds.append({'world_id': f'{family}-{i+1:03d}', 'family': family,
                'sampled_parameters': {'A_rate_work_s': rate, 'C_to_A_rate_ratio': ratio, 'bandwidth_bytes_s': bandwidth,
                    'reservation_start_s': reservation_start, 'reservation_duration_s': reservation_duration,
                    'first_contact_duration_s': contact_open, 'contact_closed_duration_s': contact_closed},
                'environment': env})
    write(root / 'worlds.json', worlds); write(root / 'source_inputs.json', inputs)
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'stage': 'PRESPECIFIED_BEFORE_SWEEP_EXECUTION',
        'seed': seed, 'worlds_per_family': count, 'families': FAMILIES, 'cases': CASES,
        'controls': CONTROLS, 'comparisons': COMPARISONS, 'deadline_s': 60,
        'expected_simulations': count * len(FAMILIES) * len(CASES) * len(CONTROLS),
        'sampling': {'A_rate_work_s': 'loguniform(250000,1000000)', 'C_to_A_rate_ratio': 'loguniform(0.5,12)',
            'bandwidth_bytes_s': 'loguniform(200000,50000000)', 'reservation_start_s': 'uniform(0.02,0.15)',
            'reservation_duration_s': 'uniform(10,60)', 'first_contact_duration_s': 'uniform(0.01,0.2)',
            'contact_closed_duration_s': 'uniform(5,60)', 'streams': 'independent numpy SeedSequence child stream per family'},
        'analysis': {'unit': 'independent sampled world; three product cases are within-world repeated measures',
            'bootstrap_resamples': 5000, 'bootstrap_seed': seed + 1, 'interval': '95% percentile CI over world-cluster means',
            'pooled_percentage': 'equal-weight mean of three within-world product percentages, then mean across worlds',
            'absolute_saving': 'reference completion minus alternative completion; positive means improvement',
            'relative_saving_pct': '100 * (reference - alternative) / reference',
            'material_improvement': 'relative saving strictly greater than 10%',
            'material_degradation': 'relative saving strictly less than -10%',
            'failure_policy': 'All attempted cases remain in status/deadline denominators. Finite paired timing effects are explicitly conditional on both complete; incomplete counts and missing pooled worlds are reported.',
            'primary_outputs': ['paired mean seconds saved with CI', 'paired mean percentage saved with CI',
                'each-control median completion', 'deadline success rate', '>10% improvement fraction', '>10% degradation fraction'],
            'family_policy': 'Report normal, busy_local, future_contact separately; no across-family aggregate.'},
        'interpretation': [
            'Synthetic uncalibrated sensitivity distributions, not estimated satellite operating distributions. CI describes Monte Carlo uncertainty under these declared distributions only.',
            'Fixed real executable DAGs and measured produced-artifact sizes are reused identically across controls. No new host performance sample or real-agent inference is created.',
            'Current-only decisions receive actual observable link state and already-active reservation release times, and can wait for an observed event. Future unstarted reservations/windows are visible only to forecast controls.',
            'Forecast is perfect mock information. The offloading-only contrast is distinct from the incremental forecast contrast.',
            'Completion means requested outputs back at A. Independent ground downlink, global link contention and packet loss are outside this model.',
            'Fixture agent-only and competent recipe use identical graphs, so no semantic-planning advantage is claimed.',
            'No resampling or parameter-range revision based on favorable outcomes is allowed.'
        ], 'source_inventory': inventory(), 'source_study': str(source.resolve()),
        'source_study_manifest_sha256': sha(source / 'study_manifest.json'),
        'worlds_sha256': sha(root / 'worlds.json'), 'source_inputs_sha256': sha(root / 'source_inputs.json')}
    write(root / 'protocol.json', protocol)
    print(json.dumps({'status':'PREPARED', 'root':str(root), 'expected_simulations':protocol['expected_simulations']}))


def estimate(values, bootstrap_indices):
    a = np.asarray(values, dtype=float)
    if not len(a): return {'mean':None,'ci95_low':None,'ci95_high':None,'n_worlds':0}
    # Each value is already one independent world or within-world product mean.
    indices = bootstrap_indices if bootstrap_indices.shape[1] == len(a) else np.random.default_rng(817).integers(0,len(a),(5000,len(a)))
    means = a[indices].mean(axis=1)
    lo, hi = np.quantile(means, [.025,.975])
    return {'mean':float(a.mean()),'ci95_low':float(lo),'ci95_high':float(hi),'n_worlds':len(a)}


def summarize(root, rows, protocol):
    lookup = {(r['world_id'],r['case_id'],r['control']):r for r in rows}
    rng = np.random.default_rng(protocol['analysis']['bootstrap_seed']); effects=[]; arms=[]
    world_order = {f: sorted({r['world_id'] for r in rows if r['family']==f}) for f in FAMILIES}
    for family, worlds in world_order.items():
        boot = rng.integers(0,len(worlds),(5000,len(worlds)))
        for case in CASES + ['ALL_PRODUCTS_EQUAL_WEIGHT']:
            cases = CASES if case == 'ALL_PRODUCTS_EQUAL_WEIGHT' else [case]
            for control in CONTROLS:
                batch=[lookup[w,c,control] for w in worlds for c in cases]
                finite=[r['completion_s'] for r in batch if r['completion_s'] is not None]
                arms.append({'family':family,'case_id':case,'control':control,'attempts':len(batch),
                    'complete_count':sum(r['status']=='COMPLETE' for r in batch),
                    'deadline_success_count':sum(bool(r['deadline_met']) for r in batch),
                    'deadline_success_rate':sum(bool(r['deadline_met']) for r in batch)/len(batch),
                    'median_completion_s_complete_only':float(np.median(finite)) if finite else None,
                    'mean_transferred_bytes':float(np.mean([r['transferred_bytes'] for r in batch]))})
            for reference, alternative, name in COMPARISONS:
                absolute=[]; relative=[]; deadline=[]; improves=[]; worsens=[]; incomplete_pairs=0
                for w in worlds:
                    abs_w=[]; rel_w=[]; deadlines=[]; imp=[]; bad=[]
                    for c in cases:
                        a,b=lookup[w,c,reference],lookup[w,c,alternative]
                        deadlines.append(float(bool(b['deadline_met']))-float(bool(a['deadline_met'])))
                        if a['completion_s'] is None or b['completion_s'] is None:
                            incomplete_pairs+=1; continue
                        delta=a['completion_s']-b['completion_s']; pct=100*delta/a['completion_s']
                        abs_w.append(delta); rel_w.append(pct); imp.append(float(pct>10)); bad.append(float(pct < -10))
                    deadline.append(float(np.mean(deadlines)))
                    # Never quietly average fewer products for pooled timing.
                    if len(abs_w)==len(cases):
                        absolute.append(float(np.mean(abs_w)));relative.append(float(np.mean(rel_w)))
                    improves.append(float(sum(imp)/len(cases)));worsens.append(float(sum(bad)/len(cases)))
                item={'family':family,'case_id':case,'comparison':name,'reference':reference,'alternative':alternative,
                    'world_count':len(worlds),'product_pair_count':len(worlds)*len(cases),
                    'incomplete_timing_pairs':incomplete_pairs,'excluded_incomplete_timing_worlds':len(worlds)-len(absolute),
                    'seconds_saved':estimate(absolute,boot),'percentage_saved':estimate(relative,boot),
                    'deadline_success_rate_difference':estimate(deadline,boot),
                    'improvement_over_10pct_fraction_all_attempts':estimate(improves,boot),
                    'degradation_over_10pct_fraction_all_attempts':estimate(worsens,boot)}
                effects.append(item)
    write(root / 'effects.json', effects); write(root / 'arm_summary.json', arms); csvwrite(root / 'arm_summary.csv', arms)
    flat=[]
    for effect in effects:
        row={k:v for k,v in effect.items() if not isinstance(v,dict)}
        for k,v in effect.items():
            if isinstance(v,dict): row.update({k+'_'+m:n for m,n in v.items()})
        flat.append(row)
    csvwrite(root / 'effects.csv',flat)
    summary={'status':'COMPLETE','simulations':len(rows),'worlds':len(world_order['normal'])*len(FAMILIES),
        'status_counts':dict(Counter(r['status'] for r in rows)), 'actual_scientific_reruns':0,'real_model_calls':0,
        'protocol_sha256':sha(root/'protocol.json'),'source_inventory':inventory(),
        'pooled_effects':[r for r in effects if r['case_id']=='ALL_PRODUCTS_EQUAL_WEIGHT'],
        'interpretation':protocol['interpretation']}
    write(root/'summary.json',summary)
    return summary


def run(root):
    protocol=read(root/'protocol.json')
    if protocol['source_inventory'] != inventory(): raise ValueError('Source changed after protocol freeze')
    for file,key in [('worlds.json','worlds_sha256'),('source_inputs.json','source_inputs_sha256')]:
        if sha(root/file)!=protocol[key]: raise ValueError('Frozen input changed: '+file)
    if (root/'raw_results.jsonl').exists(): raise ValueError('Raw results already exist; refuse overwrite')
    inputs=read(root/'source_inputs.json'); loaded={}
    for source in inputs:
        if sha(source['plan_path'])!=source['plan_file_sha256'] or sha(source['result_path'])!=source['result_file_sha256']: raise ValueError('Source science changed')
        loaded[source['case_id']] = (read(source['plan_path']), read(source['result_path'])['execution']['steps'])
    rows=[]
    with (root/'raw_results.jsonl').open('w') as raw:
        for world in read(root/'worlds.json'):
            env=world['environment']
            truth_hash=hashlib.sha256(json.dumps(env['truth'],sort_keys=True).encode()).hexdigest()
            for case in CASES:
                plan,steps=loaded[case]
                for control,(forecast,offload) in CONTROLS.items():
                    result=simulate(plan,steps,control,world['family'],forecast_enabled=forecast,
                        offload_enabled=offload,environment_override=env)
                    item={'world_id':world['world_id'],'family':world['family'],'case_id':case,'control':control,
                          'truth_sha256':truth_hash,'result':result}
                    raw.write(json.dumps(item,sort_keys=True,allow_nan=False)+'\n')
                    rows.append({k:item[k] for k in ('world_id','family','case_id','control','truth_sha256')} | {
                        'status':result['status'],'completion_s':result['completion_s'],'deadline_met':result['deadline_met'],
                        'transferred_bytes':result['transferred_bytes'],
                        'observed_event_waits':sum(len(s.get('observation_waits',[])) for s in result['schedule']),
                        'remote_steps':sum(s['node']=='C' for s in result['schedule'])})
    csvwrite(root/'metrics.csv',rows);write(root/'metrics.json',rows)
    summary=summarize(root,rows,protocol)
    print(json.dumps({'status':summary['status'],'simulations':len(rows),'root':str(root)}))
    for effect in summary['pooled_effects']:
        if effect['comparison'] in ('offloading_without_forecast','incremental_forecast'):
            print(json.dumps({k:effect[k] for k in ('family','comparison','percentage_saved','seconds_saved','incomplete_timing_pairs')}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root',type=Path,required=True)
    parser.add_argument('--source-study',type=Path,default=Path('/workspace/shared/orbitserve-semantic-runs/normal-final'))
    parser.add_argument('--worlds-per-family',type=int,default=50)
    parser.add_argument('--seed',type=int,default=20261017)
    parser.add_argument('--run',action='store_true',help='Execute previously prepared and reviewed protocol')
    args=parser.parse_args()
    if args.run: run(args.run_root.resolve())
    else: prepare(args.run_root.resolve(),args.source_study.resolve(),args.worlds_per_family,args.seed)

if __name__ == '__main__': main()
