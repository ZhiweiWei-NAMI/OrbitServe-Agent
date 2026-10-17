#!/usr/bin/env python3
"""Run semantic workflow contracts without confusing fixture replay with an LLM.

Actual executor wall time and discrete-event modeled completion are separate
columns. External plans must be supplied explicitly; missing imports never fall
back to fixtures. A correct needs_data response is not a fabricated delivery.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / 'configs' / 'semantic_cases.json'
CLI = REPO / 'scripts' / 'run_semantic.py'


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_inventory():
    paths = sorted({p for pattern in ('src/orbitserve/*.py', 'scripts/run_semantic*.py', 'configs/semantic_cases.json')
                    for p in REPO.glob(pattern) if p.is_file()})
    entries = [{'path': str(p.relative_to(REPO)), 'sha256': sha(p)} for p in paths]
    return {'files': entries, 'sha256': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()}


def flatten(result, record):
    quality = result.get('quality', {})
    execution, modeled = result.get('execution', {}), result.get('modeled', {})
    planning = result.get('planning', {})
    row = {k: record.get(k) for k in ('case_id', 'scenario', 'arm', 'order', 'run_dir', 'command_exit_code', 'command_wall_s')}
    row.update(status=result.get('status', 'MISSING_RESULT'), mode=result.get('mode'),
        quality_status=quality.get('status'), semantic_status=result.get('semantic_status'),
        contract_pass=quality.get('status') == 'PASS' and result.get('status') in ('PASS', 'NEEDS_DATA'),
        products_produced=result.get('status') == 'PASS' and quality.get('status') == 'PASS',
        needs_data=result.get('status') == 'NEEDS_DATA',
        plan_sha256=result.get('plan_sha256'), node_count=result.get('node_count'),
        operator_counts=result.get('operator_counts'), output_roles=sorted(result.get('outputs', {})),
        actual_execution_wall_s=execution.get('wall_s'), actual_tool_wall_s=execution.get('tool_wall_s'),
        nominal_source_file_bytes=execution.get('source_bytes_read'), step_artifact_bytes=execution.get('intermediate_bytes'),
        produced_requested_artifact_bytes=execution.get('delivered_bytes'),
        modeled_status=modeled.get('status'), modeled_completion_s=modeled.get('completion_s'),
        modeled_deadline_met=modeled.get('deadline_met'), modeled_transferred_bytes=modeled.get('transferred_bytes'),
        modeling_boundary=modeled.get('boundary'), planning_wall_s=planning.get('wall_s'),
        planning_usage=planning.get('usage'), planning_cost=planning.get('cost'))
    return row


def write_csv(path, records):
    if not records:
        return
    keys = list(dict.fromkeys(k for record in records for k in record))
    with Path(path).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys); writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v
                         for k, v in record.items()} for record in records)


def summarize(root):
    manifest = read(root / 'study_manifest.json')
    records = [read(path) for path in sorted((root / 'runs').glob('*/runner_record.json'))]
    metrics, failures = [], []
    for record in records:
        result_path = Path(record['run_dir']) / 'result.json'
        if result_path.exists():
            result = read(result_path)
        else:
            result = {'status': 'EXECUTION_ERROR', 'quality': {'status': 'FAIL'}, 'error': record.get('stderr_tail')}
        row = flatten(result, record); metrics.append(row)
        if not row['contract_pass']:
            failures.append({'case_id': row['case_id'], 'scenario': row['scenario'], 'arm': row['arm'],
                'run_dir': row['run_dir'], 'quality': result.get('quality'), 'error': result.get('error', record.get('stderr_tail'))})
    write_csv(root / 'metrics.csv', metrics)
    write(root / 'metrics.json', metrics)
    groups = defaultdict(list)
    for row in metrics:
        groups[(row['scenario'], row['arm'])].append(row)
    grouped = [{'scenario': scenario, 'arm': arm, 'attempts': len(batch),
        'contract_passes': sum(r['contract_pass'] for r in batch),
        'produced_results': sum(r['products_produced'] for r in batch),
        'correct_needs_data': sum(r['contract_pass'] and r['needs_data'] for r in batch),
        'failed_contracts': sum(not r['contract_pass'] for r in batch)}
        for (scenario, arm), batch in sorted(groups.items())]
    contrasts = []
    paired = defaultdict(dict)
    for row in metrics:
        paired[(row['case_id'], row['scenario'])][row['arm']] = row
    for (case_id, scenario), variants in paired.items():
        baseline = variants.get('non_agent')
        if not baseline:
            continue
        for arm, row in variants.items():
            if arm == 'non_agent':
                continue
            item = {'case_id': case_id, 'scenario': scenario, 'reference': 'non_agent', 'alternative': arm,
                    'both_contracts_pass': baseline['contract_pass'] and row['contract_pass'],
                    'both_produced_results': baseline['products_produced'] and row['products_produced']}
            if item['both_produced_results']:
                for metric in ('actual_execution_wall_s', 'actual_tool_wall_s', 'nominal_source_file_bytes',
                               'step_artifact_bytes', 'produced_requested_artifact_bytes',
                               'modeled_completion_s', 'modeled_transferred_bytes'):
                    a, b = baseline.get(metric), row.get(metric)
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                        item[metric + '_reference_minus_alternative'] = a - b
            contrasts.append(item)
    write_csv(root / 'contrasts.csv', contrasts)
    report = {'study_status': 'COMPLETE' if len(records) == len(manifest['schedule']) else 'IN_PROGRESS',
        'planned_attempts': len(manifest['schedule']), 'observed_attempts': len(metrics),
        'status_counts': dict(Counter(r['status'] for r in metrics)),
        'contract_passes': sum(r['contract_pass'] for r in metrics),
        'produced_results': sum(r['products_produced'] for r in metrics),
        'correct_needs_data': sum(r['contract_pass'] and r['needs_data'] for r in metrics),
        'groups': grouped, 'failures': failures,
        'interpretation': manifest['interpretation']}
    write(root / 'summary.json', report)
    return report



def model_controls(root):
    """Same-plan, same-measured-artifact model ablation; no scientific re-execution."""
    from orbitserve.semantic_model import simulate
    entries = []
    for record_path in sorted((root / 'runs').glob('*/runner_record.json')):
        record = read(record_path); run = Path(record['run_dir'])
        if not (run / 'result.json').exists() or not (run / 'plan.json').exists():
            continue
        result, plan = read(run / 'result.json'), read(run / 'plan.json')
        if result.get('quality', {}).get('status') != 'PASS':
            continue
        controls = {}
        for name, forecast, offload in (('local_current', False, False), ('local_forecast', True, False),
                                        ('dynamic_current', False, True), ('dynamic_forecast', True, True)):
            control = simulate(plan, result['execution']['steps'], record['arm'], record['scenario'],
                               forecast_enabled=forecast, offload_enabled=offload)
            controls[name] = control
            entries.append({'case_id': record['case_id'], 'scenario': record['scenario'],
                'source_arm': record['arm'], 'source_run_dir': str(run),
                'plan_sha256': result['plan_sha256'], 'control': name,
                'forecast_enabled': forecast, 'offload_enabled': offload,
                'modeled_status': control['status'], 'modeled_completion_s': control.get('completion_s'),
                'modeled_transferred_bytes': control.get('transferred_bytes'),
                'modeled_deadline_met': control.get('deadline_met'),
                'chosen_nodes': [step['node'] for step in control.get('schedule', [])],
                'boundary': 'Same real-artifact byte counts and plan; model-only replay, no new physical execution or transfer.'})
        write(run / 'model_controls.json', controls)
    write(root / 'model_controls.json', entries)
    write_csv(root / 'model_controls.csv', entries)
    effects = []
    grouped = defaultdict(dict)
    for item in entries:
        grouped[(item['case_id'], item['scenario'], item['source_arm'])][item['control']] = item
    for (case_id, scenario, source_arm), controls in grouped.items():
        for effect, before, after in (('offload_given_current', 'local_current', 'dynamic_current'),
                                      ('forecast_given_offload', 'dynamic_current', 'dynamic_forecast'),
                                      ('combined', 'local_current', 'dynamic_forecast')):
            a, b = controls[before], controls[after]
            row = {'case_id': case_id, 'scenario': scenario, 'source_arm': source_arm, 'effect': effect,
                   'reference_control': before, 'alternative_control': after,
                   'reference_status': a['modeled_status'], 'alternative_status': b['modeled_status']}
            if isinstance(a['modeled_completion_s'], (int, float)) and isinstance(b['modeled_completion_s'], (int, float)):
                row['modeled_seconds_saved'] = a['modeled_completion_s'] - b['modeled_completion_s']
                row['modeled_transfer_bytes_saved'] = a['modeled_transferred_bytes'] - b['modeled_transferred_bytes']
            effects.append(row)
    write(root / 'model_effects.json', effects); write_csv(root / 'model_effects.csv', effects)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path('/workspace/shared/orbitserve-data'))
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--cases', type=Path, default=CASES)
    parser.add_argument('--case-id', action='append', dest='case_ids')
    parser.add_argument('--scenario', action='append', choices=['normal', 'future_contact', 'busy_local'], dest='scenarios')
    parser.add_argument('--mode', choices=['fixtures', 'external'], default='fixtures')
    parser.add_argument('--plans-dir', type=Path,
                        help='External plan JSONs named CASE_ID__SCENARIO__agent_only.json and CASE_ID__SCENARIO__agent_wm_offload.json; no scenario fallback.')
    parser.add_argument('--timeout-s', type=float, default=300)
    parser.add_argument('--summarize-only', action='store_true')
    parser.add_argument('--model-controls', action='store_true', help='Replay four model controls on the same measured steps, without rerunning science.')
    args = parser.parse_args()
    root = args.run_root.resolve(); root.mkdir(parents=True, exist_ok=True)
    if args.summarize_only:
        report = summarize(root)
        if args.model_controls: model_controls(root)
        print(json.dumps(report, ensure_ascii=False)); return
    if not CLI.exists():
        parser.error('Semantic CLI is not ready: ' + str(CLI))
    cases = read(args.cases)['cases']; known = {case['id'] for case in cases}
    selected = args.case_ids or [case['id'] for case in cases]
    if set(selected) - known:
        parser.error('Unknown case IDs: ' + ', '.join(sorted(set(selected) - known)))
    scenarios = args.scenarios or ['normal']
    arms = ['non_agent'] + (['fixture_agent_only', 'fixture_agent_wm_offload'] if args.mode == 'fixtures'
                             else ['external_agent_only', 'external_agent_wm_offload'])
    if args.mode == 'external':
        if args.plans_dir is None:
            parser.error('--mode external requires --plans-dir; no fixture fallback is permitted.')
        missing = [args.plans_dir / f'{case_id}__{scenario}__{kind}.json' for case_id in selected
                   for scenario in scenarios for kind in ('agent_only', 'agent_wm_offload')
                   if not (args.plans_dir / f'{case_id}__{scenario}__{kind}.json').is_file()]
        if missing:
            parser.error('External plans are missing: ' + ', '.join(map(str, missing)))
    inventory = source_inventory()
    schedule = []
    for scenario in scenarios:
        for case_index, case_id in enumerate(selected):
            rotated = arms[case_index % len(arms):] + arms[:case_index % len(arms)]
            for arm in rotated:
                schedule.append({'case_id': case_id, 'scenario': scenario, 'arm': arm, 'order': len(schedule) + 1})
    manifest_path = root / 'study_manifest.json'
    if manifest_path.exists():
        manifest = read(manifest_path)
        if manifest['source']['sha256'] != inventory['sha256'] or manifest['schedule'] != schedule:
            parser.error('Existing root has a different source inventory or schedule. Use a new run root.')
    else:
        write(manifest_path, {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
            'source': inventory, 'cases_sha256': sha(args.cases), 'cases_path': str(args.cases.resolve()),
            'data_root': str(args.data_root.resolve()), 'python': sys.version, 'platform': platform.platform(),
            'schedule': schedule, 'mode': args.mode,
            'interpretation': [
                'Fixture replay validates plumbing and executable contract diversity, not intelligent planning, real-model inference, or generalization.',
                'External imports are only called real-model evidence when their source, request, response, and provenance are independently retained.',
                'Actual serial executor wall time (including validation and bookkeeping), summed scientific-operator wall time, and discrete-event modeled completion are separate metrics; do not add or substitute them.',
                'No measured satellite hardware, flight, ns-3, learned world model, or radio transmission is implied by model schedule outputs.',
                'Modeled completion means required outputs available back at A; no independent A-to-G downlink is modeled.',
                'Produced requested artifact bytes are local required-output file sizes, not observed inter-node network traffic in this semantic harness.',
                'Nominal source file bytes sum the full referenced file sizes per load, not OS I/O or window bytes. Step artifact bytes include every emitted package, including requested final outputs.',
                'A correct needs_data response passes its task contract but is counted separately from delivered scientific products.',
                'One deterministic pass per case/arm/scenario is a smoke demonstration; no statistical significance or robust wall-time speedup claim.',
                'Competent goal-aware fixed recipes are valid baselines and may equal or outperform generated plans.',
                'No expected numerical answers or reference DAG are provided in the user instruction cases.'
            ]})
        write(root / 'cases_snapshot.json', read(args.cases))
    (root / 'runs').mkdir(exist_ok=True)
    report = summarize(root)
    for spec in schedule:
        if (root / 'STOP_AFTER_CURRENT').exists():
            report['study_status'] = 'STOPPED_AFTER_CURRENT_RUN'
            write(root / 'summary.json', report)
            print(json.dumps({'status': report['study_status'], 'run_root': str(root)}), flush=True)
            return
        if source_inventory()['sha256'] != inventory['sha256']:
            report['study_status'] = 'STOPPED_SOURCE_CHANGED'
            write(root / 'summary.json', report)
            raise RuntimeError('Source changed between runs; retain completed evidence and use a new run root.')
        run = root / 'runs' / f"{spec['order']:03d}-{spec['case_id']}-{spec['scenario']}-{spec['arm']}"
        if (run / 'runner_record.json').exists():
            continue
        if run.exists():
            parser.error('Unfinished run directory exists; inspect before retrying: ' + str(run))
        # CLI owns its run directory and result artifact. Runner logs are saved
        # after exit so no output overwrites or fake successful rows are hidden.
        command = [sys.executable, str(CLI), 'run', '--cases', str(args.cases.resolve()),
            '--case-id', spec['case_id'], '--arm', spec['arm'], '--scenario', spec['scenario'],
            '--data-root', str(args.data_root.resolve()), '--run-dir', str(run)]
        if args.mode == 'external' and spec['arm'] != 'non_agent':
            kind = spec['arm'].removeprefix('external_')
            plan_path = (args.plans_dir / f"{spec['case_id']}__{spec['scenario']}__{kind}.json").resolve()
            command += ['--plan', str(plan_path)]
            request_sidecar = plan_path.with_suffix('.request.json')
            if request_sidecar.is_file():
                request_context = read(request_sidecar)
                expected = {'case_id': spec['case_id'], 'scenario': spec['scenario'], 'arm': kind}
                if any(request_context.get(key) != value for key, value in expected.items()):
                    parser.error('External request sidecar case/scenario/arm mismatch: ' + str(request_sidecar))
            planning_sidecar = plan_path.with_suffix('.planning.json')
            if planning_sidecar.is_file():
                command += ['--planning', str(planning_sidecar)]
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, text=True, capture_output=True, timeout=args.timeout_s)
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            code = 124
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ''
            stderr = (exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or '') + '\nRunner timeout reached.'
        run.mkdir(parents=True, exist_ok=True)
        (run / 'stdout.log').write_text(stdout); (run / 'stderr.log').write_text(stderr)
        record = dict(spec, run_dir=str(run), command=command, command_exit_code=code,
                      command_wall_s=time.perf_counter() - started, stderr_tail=stderr[-3000:])
        write(run / 'runner_record.json', record)
        report = summarize(root)
        print(json.dumps(dict(spec, command_exit_code=code,
            status=read(run / 'result.json').get('status') if (run / 'result.json').exists() else 'EXECUTION_ERROR'), ensure_ascii=False), flush=True)
    if args.model_controls: model_controls(root)
    print(json.dumps({'status': report['study_status'], 'run_root': str(root),
                      'contract_passes': report['contract_passes'], 'attempts': report['observed_attempts']}), flush=True)


if __name__ == '__main__':
    main()
