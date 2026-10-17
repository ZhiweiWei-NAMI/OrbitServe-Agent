#!/usr/bin/env python3
"""Paired prototype comparisons with real imagery and real isolated computation.

This is an experiment driver, not a learned-agent benchmark or a hardware model.
No flight, ns-3, LLM, MARL, or learned world model is claimed. See protocol.json.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import time

from orbitserve.evidence import (append_jsonl, canonical_hash, code_inventory,
    file_manifest, finish_manifest, new_run, start_manifest, write_json)
from orbitserve.forecast import forecast
from orbitserve.planning import plan
from orbitserve.policy import choose_action
from orbitserve.runtime import default_config, observed_snapshot, run_distributed
from orbitserve.verification import verify_run, verify_science

ARMS = {
    'no_agent_fixed': {'grain': 'fixed128', 'placement': 'static_B', 'label': 'Fixed128 + fixed B'},
    'agent_plan_static': {'grain': 'adaptive', 'placement': 'static_B', 'label': 'Adaptive grain + fixed B'},
    'fixed_dynamic': {'grain': 'fixed128', 'placement': 'dynamic', 'label': 'Fixed128 + dynamic B/C'},
    'agent_plan_dynamic': {'grain': 'adaptive', 'placement': 'dynamic', 'label': 'Adaptive grain + dynamic B/C'},
    'full_grid_static': {'grain': 'fixed_whole', 'placement': 'static_B', 'label': 'Full-grid + fixed B (sensitivity)'},
}
SCENARIOS = ['normal', 'memory_11mib', 'busy_b_25s', 'link_375kbps']
PAIRS = [('planning_static', 'no_agent_fixed', 'agent_plan_static'),
         ('planning_dynamic', 'fixed_dynamic', 'agent_plan_dynamic'),
         ('dynamic_fixed128', 'no_agent_fixed', 'fixed_dynamic'),
         ('dynamic_adaptive', 'agent_plan_static', 'agent_plan_dynamic'),
         ('combined', 'no_agent_fixed', 'agent_plan_dynamic')]


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def write_csv(path, records):
    if not records:
        return
    keys = list(dict.fromkeys(k for r in records for k in r))
    with Path(path).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                         for k, v in r.items()} for r in records)


def scenario_config(scenario):
    cfg = default_config()
    if scenario == 'memory_11mib':
        for node in cfg['nodes']:
            if node['node_id'] in ('B', 'C'):
                node['memory_bytes'] = 11 * 1024 * 1024
    if scenario == 'link_375kbps':
        cfg['bandwidth_bytes_s'] = 375000
        for edge in cfg['known_contact_plan']:
            edge['bandwidth_bytes_s'] = edge['throughput_bytes_s'] = 375000
    return cfg


def construct(case, cfg, arm):
    task, assets = read(case / 'task.json'), read(case / 'assets.json')
    task = deepcopy(task)
    if arm['grain'] != 'adaptive':
        task['planning_config'] = {'candidate_tile_sizes': [128 if arm['grain'] == 'fixed128' else 0]}
    observation = observed_snapshot(cfg)
    observation['assets'] = [dict(artifact_id='sha256:' + a['sha256'], node_id='A',
        sha256=a['sha256'], size_bytes=a.get('bytes', a.get('size_bytes')),
        source='declared_initial_preload_verified_before_plan_downlink') for a in assets['assets']]
    started = time.perf_counter()
    graph = forecast([observation], cfg['known_contact_plan'], 120)
    forecast_s = time.perf_counter() - started
    started = time.perf_counter()
    try:
        dag = plan(task, assets, {'water_tile': {}, 'water_merge': {}}, graph)
    except ValueError as exc:
        if not hasattr(exc, 'candidate_evaluations'):
            raise
        return graph, None, forecast_s, time.perf_counter() - started, {
            'error': str(exc), 'candidate_evaluations': exc.candidate_evaluations}
    plan_s = time.perf_counter() - started
    # Hold grain selection's candidate set constant. This is a deliberate
    # placement ablation AFTER planning, not a hidden planner preference.
    if arm['placement'] == 'static_B':
        for step in dag['steps']:
            step['allowed_nodes'] = ['B']
    dag['comparison_mode'] = deepcopy(arm)
    dag['comparison_mode']['fixed_workflow_definition'] = (
        'One predetermined candidate, same DAG builder and feasibility checks; no adaptive grain choice.'
        if arm['grain'] != 'adaptive' else 'Select among original grid, 128, and 64 using observed resources/contact forecast.')
    return graph, dag, forecast_s, plan_s, None


def comparative_verdict(run, raw, arm):
    check_rows = deepcopy(raw['checks'])
    substitutions = []
    if arm['placement'] == 'static_B':
        calls = rows(run / 'tool_calls.jsonl')
        actual = [x for x in calls if x.get('status') == 'running']
        dag = read(run / 'plan.json')
        static_ok = bool(actual) and all(x['node_id'] == 'B' for x in actual) and all(
            x['allowed_nodes'] == ['B'] for x in dag['steps'])
        for item in check_rows:
            if item['check'] == 'DAG_not_preassigned':
                substitutions.append(item)
                item.update(check='comparison_static_B_placement_exact', status='PASS' if static_ok else 'FAIL',
                    detail='Only this dynamic-design assertion is replaced for the intentionally static-placement control.')
    # Verify the recorded cost decision can be reproduced exactly from the
    # received local observation, advertisements, task, and persisted forecast.
    graph = read(run / 'forecast.json')
    decisions = rows(run / 'decisions.jsonl')
    mismatch = []
    for index, event in enumerate(decisions):
        replay = choose_action(event['local_observation'], event['task'], event['received_neighbors'], graph)
        if replay != event['decision']:
            mismatch.append(index)
    check_rows.append({'check': 'comparison_exact_policy_replay', 'status': 'PASS' if not mismatch else 'FAIL',
                       'detail': {'decisions': len(decisions), 'mismatched_indices': mismatch}})
    return {'status': 'PASS' if all(r['status'] == 'PASS' for r in check_rows) else 'FAIL',
            'dynamic_verifier_original_status': raw['status'], 'explicit_replaced_assertions': substitutions,
            'checks': check_rows}


def run_one(root, data, spec):
    case = data / 'cases' / spec['case_id']
    cfg = scenario_config(spec['scenario'])
    arm = ARMS[spec['arm']]
    run = new_run(root / 'runs', f"cmp-{spec['case_id']}-{spec['scenario']}-{spec['arm']}-r{spec['repeat']}")
    write_json(run / 'comparison_spec.json', spec)
    start_manifest(run, 'COMPARISON', [{'case_id': case.name,
        'files': file_manifest(case, exclude=('outputs', 'evaluation'))}], cfg)
    base = dict(spec, run_dir=str(run), configuration_sha256=canonical_hash(cfg),
                grain_strategy=arm['grain'], placement_strategy=arm['placement'])
    graph, dag, forecast_s, plan_s, rejection = construct(case, cfg, arm)
    base.update(forecast_wall_s=forecast_s, plan_wall_s=plan_s,
                planning_wall_s=forecast_s + plan_s)
    write_json(run / 'forecast.json', graph)
    append_jsonl(run / 'forecasts.jsonl', graph)
    if rejection:
        write_json(run / 'preflight_rejection.json', rejection)
        base.update(status='PREFLIGHT_INFEASIBLE', completed=0, error=rejection['error'],
                    delivery_wall_s=None, total_delivery_wall_s=None, science_status='NOT_EXECUTED',
                    runtime_verification='NOT_EXECUTED', runtime_status='NOT_STARTED')
        finish_manifest(run, 'PREFLIGHT_INFEASIBLE', failure_reason=rejection['error'])
        write_json(run / 'comparison_metrics.json', base)
        return base
    write_json(run / 'plan.json', dag)
    append_jsonl(run / 'plans.jsonl', dag)
    runtime_scenario = 'busy_b' if spec['scenario'] == 'busy_b_25s' else 'normal'
    result = run_distributed(dag, case, run, scenario=runtime_scenario, config=cfg, forecast_graph=graph)
    raw = verify_run(run)
    write_json(run / 'runtime_verification_original.json', raw)
    verdict = comparative_verdict(run, raw, arm)
    write_json(run / 'runtime_verification_comparative.json', verdict)
    science = verify_science(case, result['outputs_dir']) if result.get('outputs_dir') else {'status': 'NOT_EXECUTED'}
    write_json(run / 'science_verification.json', science)
    calls, events = rows(run / 'tool_calls.jsonl'), rows(run / 'node_events.jsonl')
    transfers, controls = rows(run / 'transfers.jsonl'), rows(run / 'control_messages.jsonl')
    decisions = rows(run / 'decisions.jsonl')
    delivered = [e for e in events if e.get('event') == 'delivered']
    starts = [e for e in calls if e.get('status') == 'running']
    ends = [e for e in calls if e.get('status') == 'succeeded']
    chunks = [e for e in transfers if e.get('event') == 'chunk']
    received_controls = [e for e in controls if e.get('event') == 'delivered']
    payload = sum(e['chunk_bytes'] for e in chunks)
    control_bytes = sum(e['size_bytes'] for e in received_controls)
    delivery_s = delivered[0]['timestamp'] if delivered else None
    by_purpose, by_edge = Counter(), Counter()
    for event in chunks:
        by_purpose[event['purpose']] += event['chunk_bytes']
        by_edge[event['source'] + '->' + event['destination']] += event['chunk_bytes']
    quality_ok = science['status'] == verdict['status'] == 'PASS' and result['status'] == 'delivered'
    base.update(status='PASS' if quality_ok else 'FAIL', completed=int(quality_ok),
        runtime_status=result['status'], science_status=science['status'],
        runtime_verification=verdict['status'], error=result.get('error'),
        delivery_wall_s=delivery_s, total_delivery_wall_s=None if delivery_s is None else delivery_s + forecast_s + plan_s,
        runtime_including_shutdown_wall_s=result['elapsed_wall_s'],
        first_job_start_s=min((e['timestamp'] for e in starts), default=None),
        final_job_end_s=max((e['timestamp'] for e in ends), default=None),
        tile_size=dag['tile_size'], tile_count=len(dag['tile_windows']), job_count=len(starts),
        job_execution_nodes={e['step_id']: e['node_id'] for e in starts},
        job_node_counts=dict(Counter(e['node_id'] for e in starts)),
        job_elapsed_sum_s=sum(e['actual_elapsed_s'] for e in ends),
        estimated_plan_completion_s=dag['selection']['estimated_completion_s'],
        payload_bytes=payload, delivered_control_bytes=control_bytes,
        charged_transport_bytes=sum(e['charged_bytes'] for e in transfers if e.get('event') == 'capacity_charge'),
        payload_by_purpose=dict(by_purpose), payload_by_edge=dict(by_edge),
        transfer_count=result['transfer_count'], policy_decision_count=len(decisions),
        policy_wall_sum_s=sum(e['policy_wall_s'] for e in decisions),
        policy_cpu_sum_s=sum(e['policy_cpu_s'] for e in decisions),
        owner_wait_decisions=sum(e['actor_node'] == 'A' and e['action'] == 'wait' for e in decisions),
        outputs_dir=result.get('outputs_dir'))
    if payload != result['transferred_payload_bytes']:
        base.update(status='FAIL', completed=0, error='Payload accounting mismatch')
    if result.get('outputs_dir'):
        output = Path(result['outputs_dir'])
        base['change_sha256'] = hashlib.sha256((output / 'change.tif').read_bytes()).hexdigest()
        base['statistics'] = {k: v for k, v in read(output / 'stats.json').items() if k in (
            'valid_common_pixels', 'unknown_pixels', 'gained_pixels', 'lost_pixels',
            'gained_area_m2', 'lost_area_m2', 'net_change_area_m2', 'valid_coverage_fraction')}
    write_json(run / 'comparison_metrics.json', base)
    write_json(run / 'acceptance.json', {'status': base['status'], 'science': science['status'],
        'runtime': verdict['status'], 'fixed_placement_assertion_substituted': arm['placement'] == 'static_B'})
    finish_manifest(run, base['status'], outputs_dir=result.get('outputs_dir'), scenario=spec['scenario'])
    return base


def describe(values):
    return {'n': len(values), 'mean': statistics.mean(values), 'median': statistics.median(values),
            'sd': statistics.stdev(values) if len(values) > 1 else 0,
            'min': min(values), 'max': max(values)} if values else {'n': 0}


def summarize(root):
    records = rows(root / 'runs.jsonl') if (root / 'runs.jsonl').exists() else []
    write_csv(root / 'runs.csv', records)
    metrics = ['delivery_wall_s', 'total_delivery_wall_s', 'payload_bytes', 'delivered_control_bytes',
               'planning_wall_s', 'policy_wall_sum_s', 'job_elapsed_sum_s', 'tile_count']
    groups = []
    for scope in ('pooled_3cases', 'per_case'):
        grouped = defaultdict(list)
        for record in records:
            key = (record['scenario'], record['arm'], record['case_id'] if scope == 'per_case' else 'ALL')
            grouped[key].append(record)
        for (scenario, arm, case_id), batch in sorted(grouped.items()):
            complete = [r for r in batch if r['completed']]
            row = {'scope': scope, 'case_id': case_id, 'scenario': scenario, 'arm': arm,
                'attempted': len(batch), 'completed': len(complete), 'completion_fraction': len(complete) / len(batch),
                'preflight_infeasible': sum(r['status'] == 'PREFLIGHT_INFEASIBLE' for r in batch),
                'executed_failures': sum(r['status'] == 'FAIL' for r in batch)}
            for metric in metrics:
                values = [r[metric] for r in complete if r.get(metric) is not None]
                for stat, value in describe(values).items():
                    row[metric + '_' + stat] = value
            groups.append(row)
    paired = []
    by_key = {(r['case_id'], r['scenario'], r['repeat'], r['arm']): r for r in records}
    blocks = sorted(set((r['case_id'], r['scenario'], r['repeat']) for r in records))
    for case_id, scenario, repeat in blocks:
        for name, before, after in PAIRS:
            a = by_key.get((case_id, scenario, repeat, before)); b = by_key.get((case_id, scenario, repeat, after))
            if not a or not b:
                continue
            row = dict(case_id=case_id, scenario=scenario, repeat=repeat, comparison=name,
                reference_arm=before, alternative_arm=after, reference_completed=a['completed'],
                alternative_completed=b['completed'])
            if a['completed'] and b['completed']:
                row.update(delivery_saved_s=a['delivery_wall_s'] - b['delivery_wall_s'],
                    delivery_reduction_pct=100 * (1 - b['delivery_wall_s'] / a['delivery_wall_s']),
                    payload_saved_bytes=a['payload_bytes'] - b['payload_bytes'],
                    reference_delivery_s=a['delivery_wall_s'], alternative_delivery_s=b['delivery_wall_s'])
            paired.append(row)
    write_csv(root / 'groups.csv', groups)
    write_csv(root / 'paired_effects.csv', paired)
    pair_groups = []
    for scenario in SCENARIOS:
        for name, _, _ in PAIRS:
            batch = [p for p in paired if p['scenario'] == scenario and p['comparison'] == name]
            successful = [p for p in batch if 'delivery_saved_s' in p]
            if batch:
                pair_groups.append({'scenario': scenario, 'comparison': name, 'paired_attempts': len(batch),
                    'paired_successes': len(successful), 'delivery_saved_s': describe([p['delivery_saved_s'] for p in successful]),
                    'delivery_reduction_pct': describe([p['delivery_reduction_pct'] for p in successful]),
                    'payload_saved_bytes': describe([p['payload_saved_bytes'] for p in successful])})
    protocol = read(root / 'protocol.json')
    summary = {'study_status': 'COMPLETE' if len(records) == len(protocol['schedule']) else 'IN_PROGRESS',
        'planned_outcomes': len(protocol['schedule']), 'observed_outcomes': len(records),
        'completed_deliveries': sum(r['completed'] for r in records),
        'status_counts': dict(Counter(r['status'] for r in records)), 'groups': groups,
        'paired_effects': pair_groups, 'methodology': protocol['limitations'],
        'science_hashes_by_case': {case: sorted(set(r['change_sha256'] for r in records
            if r['case_id'] == case and r.get('change_sha256'))) for case in protocol['cases']}}
    write_json(root / 'summary.json', summary)
    return summary


def figures(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    summary = read(root / 'summary.json')
    groups = summary['groups']
    out = root / 'figures'; out.mkdir(exist_ok=True)
    arms = list(ARMS)[:4]
    colors = ['#858585', '#377eb8', '#e8a82e', '#269b86']
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for ax, scenario in zip(axes.flat, SCENARIOS):
        for i, arm in enumerate(arms):
            matches = [g for g in groups if g['scope'] == 'pooled_3cases' and g['scenario'] == scenario and g['arm'] == arm]
            if not matches or not matches[0].get('delivery_wall_s_n'):
                continue
            g = matches[0]
            ax.bar(i, g['delivery_wall_s_mean'], yerr=g['delivery_wall_s_sd'], color=colors[i], capsize=4)
            ax.text(i, g['delivery_wall_s_mean'] + g['delivery_wall_s_sd'] + .6,
                    f"{g['delivery_wall_s_mean']:.2f}s", ha='center', fontsize=9)
        ax.set_title(scenario); ax.set_ylabel('Ground delivery wall time (s)')
        ax.set_xticks(range(4), ['Fixed128\nFixed B', 'Adaptive\nFixed B', 'Fixed128\nDynamic', 'Adaptive\nDynamic'])
        ax.grid(axis='y', alpha=.18); ax.set_axisbelow(True)
    fig.suptitle('Real isolated processing + application-byte transport simulation\n3 AOIs × 3 repeats; error bars: sample SD across 9 deliveries', fontsize=13)
    fig.savefig(out / 'delivery_latency.png', dpi=180); fig.savefig(out / 'delivery_latency.pdf'); plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), constrained_layout=True)
    for j, metric in enumerate(['payload_bytes', 'planning_wall_s']):
        for i, arm in enumerate(arms):
            values = [g[metric + '_mean'] for g in groups if g['scope'] == 'pooled_3cases' and g['arm'] == arm and metric + '_mean' in g]
            scale = 1e6 if metric == 'payload_bytes' else .001
            axes[j].plot(range(len(values)), [v / scale for v in values], marker='o', label=ARMS[arm]['label'], color=colors[i])
        labels = sorted(SCENARIOS)
        axes[j].set_xticks(range(len(labels)), labels, rotation=15)
        axes[j].set_ylabel('Payload MB (decimal)' if metric == 'payload_bytes' else 'Forecast + DAG construction (ms)')
        axes[j].grid(alpha=.18)
    axes[0].legend(fontsize=8); fig.suptitle('Communication and planning cost (same science and data)')
    fig.savefig(out / 'costs.png', dpi=180); fig.savefig(out / 'costs.pdf'); plt.close(fig)
    # Select a fresh delivered output per case. Copy existing science-generated
    # images unchanged; do not synthesize or repaint remote-sensing pixels.
    import shutil
    products = []
    for case in read(root / 'protocol.json')['cases']:
        available = [r for r in rows(root / 'runs.jsonl') if r['case_id'] == case and r['completed']
                     and r['scenario'] == 'normal' and r['arm'] == 'agent_plan_dynamic']
        if not available:
            continue
        selected = available[0]; folder = out / case; folder.mkdir(exist_ok=True)
        for filename in ('map.png', 'before_rgb.tif', 'after_rgb.tif', 'change.tif', 'stats.json', 'changes.geojson'):
            shutil.copyfile(Path(selected['outputs_dir']) / filename, folder / filename)
        products.append({'case_id': case, 'source_run_dir': selected['run_dir'], 'source_outputs_dir': selected['outputs_dir'],
                         'copied_products_dir': str(folder), 'statistics': selected['statistics']})
    write_json(root / 'science_products.json', products)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path('/workspace/shared/orbitserve-data'))
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--summarize-only', action='store_true')
    args = parser.parse_args()
    root = args.run_root.resolve(); root.mkdir(parents=True, exist_ok=True)
    if args.summarize_only:
        summarize(root); figures(root); return
    protocol_path = root / 'protocol.json'
    if not protocol_path.exists():
        cases = sorted(p.name for p in (args.data_root / 'cases').iterdir() if (p / 'task.json').exists())
        schedule = []
        # Rotating Latin-style order across case/repeat blocks. Entire scenarios
        # are also rotated by repeat; processes never overlap across runs.
        block_id = 0
        for repeat in range(1, args.repeats + 1):
            scenarios = SCENARIOS[repeat - 1:] + SCENARIOS[:repeat - 1]
            for scenario in scenarios:
                for case_index, case in enumerate(cases):
                    selected = list(ARMS)[:4]
                    if scenario in ('normal', 'memory_11mib'):
                        selected += ['full_grid_static']
                    shift = (repeat - 1 + case_index) % len(selected)
                    selected = selected[shift:] + selected[:shift]
                    block_id += 1
                    for position, arm in enumerate(selected):
                        schedule.append(dict(block_id=block_id, case_id=case, scenario=scenario, repeat=repeat,
                                             order_in_block=position + 1, arm=arm))
        protocol = {'schema_version': 1, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'arms': ARMS, 'cases': cases, 'scenarios': SCENARIOS, 'repeats': args.repeats,
            'schedule': schedule, 'source': code_inventory(), 'host': platform.platform(),
            'input_data_root': str(args.data_root.resolve()), 'randomness': 'No stochastic scientific or policy algorithm; deterministic rotated order, no random seed required.',
            'metrics': {'delivery_wall_s': 'Broker epoch to G validated delivery event, excludes planner and teardown.',
                'total_delivery_wall_s': 'delivery_wall_s plus measured forecast + DAG construction time.',
                'payload_bytes': 'Sum of actual transferred ZIP_STORED chunk_bytes once per edge; no double counting receipts.',
                'delivered_control_bytes': 'Serialized control, transfer headers, and ACKs delivered by broker; base64/pipe carrier excluded.',
                'planning_wall_s': 'forecast plus DAG construction. Fixed arms use same builder restricted to one predetermined grain.',
                'policy_wall_sum_s': 'Measured choose_action calls summed over nodes, excluding logging, ads and broker work.'},
            'fairness': ['Same TIFF files, grid, threshold, invalid-pixel logic and science operators across arms.',
                'Source A retains 4 MiB; B nominal fastest; all static controls compute on B, not an incapable A.',
                'Fixed128 is robust across the disclosed memory regimes; fixed-whole sensitivity prevents an artificially over-tiled comparison.',
                'Only memory11MiB changes actual configured B/C admission budgets; forecast is built from those observed budgets, not test_fixture.',
                'Busy B is hidden from initial forecast and revealed via actual received B observations; same25s gate for all arms.',
                'Planning sees same original allowed set; fixed B restriction applied afterward as explicit placement ablation.',
                'All arms use the same isolated node runtime, admission, caches, advertisements and exact product validation.',
                'Every failed/preflight-infeasible outcome remains in denominator; delivery time undefined for missing delivery.',
                'Per-run raw dynamic verifier is retained. Static controls replace only DAG_not_preassigned with actual fixed-B validation.'],
            'limitations': ['Rule-based planner/policy; analytical forecast, no LLM, MARL or learned world model.',
                'Single shared Linux host. Configured compute rates affect estimates, not CPU throttling; measured processing is host wall time.',
                'Memory and energy are modeled reservations, not measured satellite RSS or power.',
                'Synthetic contact capacity and25s busy admission gate; real scientific computation and byte transfer, no orbital flight or ns-3.',
                'Application-byte token bucket over stdio/base64 carrier; not physical link-wire measurement.',
                'Small selected AOIs and3 repeats give descriptive evidence, not population-level performance proof.',
                'Fixed no-agent label means no adaptive decomposition/placement decisions, not zero runtime control or zero A-to-B transfer.',
                'No ground-truth flood labels; independent science verifier checks implementation and pixel equivalence, not field accuracy.']}
        write_json(protocol_path, protocol)
    protocol = read(protocol_path)
    if protocol['source']['source_sha256'] != code_inventory()['source_sha256']:
        raise RuntimeError('Source inventory changed since study protocol; start a new run root instead of mixing code versions.')
    previous = rows(root / 'runs.jsonl') if (root / 'runs.jsonl').exists() else []
    completed_keys = {(r['case_id'], r['scenario'], r['repeat'], r['arm']) for r in previous}
    for spec in protocol['schedule']:
        key = (spec['case_id'], spec['scenario'], spec['repeat'], spec['arm'])
        if key in completed_keys:
            continue
        result = run_one(root, args.data_root, spec)
        append_jsonl(root / 'runs.jsonl', result)
        summary = summarize(root)
        print(json.dumps({k: result.get(k) for k in ('case_id', 'scenario', 'repeat', 'arm', 'status', 'delivery_wall_s', 'payload_bytes', 'tile_count')}, sort_keys=True), flush=True)
        if result['status'] == 'FAIL':
            raise RuntimeError(f"Run failed; retain outcome and diagnose before continuation: {result['run_dir']}")
    figures(root)
    print(json.dumps({'status': 'COMPLETE', 'run_root': str(root), 'outcomes': summary['observed_outcomes']}), flush=True)


if __name__ == '__main__':
    main()
