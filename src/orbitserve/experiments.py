"""The seven requested experiments, not a generic testing framework."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
from .evidence import new_run,write_json,start_manifest,finish_manifest
from .cli import baseline,distributed,_cases,_prepare_plan,_read


def run_acceptance(data_root,run_root):
    from .forecast import paired_test_forecasts
    from .runtime import default_config,observed_snapshot
    from .verification import verify_suite
    suite=new_run(run_root,'acceptance')
    start_manifest(suite,'E0-E6',[],{'data_root':str(data_root),'run_root':str(run_root)})
    records={'suite_id':suite.name,'status':'IN_PROGRESS','experiments':{},'feedback_records':[],
             'tolerances':{'categorical':'exact','continuous_absolute':1e-6,'area_absolute_m2':1e-6}}
    def save(): write_json(suite/'suite_manifest.json',records)
    save()
    try:
        records['experiments']['E0']=str(baseline(data_root,run_root)); save()
        cases=_cases(data_root)
        records['experiments']['E1']=[]
        history=None
        previous=None
        for case in cases:
            path,result=distributed(case,run_root,history=history)
            records['experiments']['E1'].append(str(path))
            if history:
                records['feedback_records'].append({'preceding_run':str(previous),'observation':history[-1],
                    'forecast_id':_read(path/'forecast.json')['forecast_id'],'consuming_run':str(path)})
            observation=result.get('observed_snapshot',result.get('observation'))
            if observation is None: raise RuntimeError('Runtime supplied no actual completion observation for feedback')
            history=[observation]  # Latest actual observation; each run has its own monotonic epoch.
            previous=path; save()
        case=cases[0]
        config=default_config(); _,initial_graph,_=_prepare_plan(case,config); observation=initial_graph['observed_snapshot']
        contacts=config.get('known_contact_plan',config.get('contacts',config.get('links',[])))
        ample,constrained=paired_test_forecasts([observation],contacts,120)
        whole,_=distributed(case,run_root,forecast_graph=ample,experiment='E2-whole')
        tiled,_=distributed(case,run_root,forecast_graph=constrained,experiment='E2-tiled')
        records['experiments']['E2']=[str(whole),str(tiled)]; save()
        busy,_=distributed(case,run_root,scenario='busy_b',plan_value=_read(whole/'plan.json'),forecast_graph=ample,experiment='E3')
        records['experiments']['E3']=[str(whole),str(busy)]; save()
        disconnected,_=distributed(case,run_root,scenario='ground_outage',plan_value=_read(tiled/'plan.json'),forecast_graph=constrained,experiment='E4')
        records['experiments']['E4']=str(disconnected); save()
        resumed,_=distributed(case,run_root,scenario='transfer_outage',plan_value=_read(whole/'plan.json'),forecast_graph=ample,experiment='E5')
        records['experiments']['E5']=str(resumed); save()
        rerun_root=suite/'independent-rerun'
        if rerun_root.exists(): raise RuntimeError('Independent rerun directory unexpectedly exists')
        cmd=[sys.executable,'-m','orbitserve','run','--data-root',str(data_root),'--run-root',str(rerun_root),
             '--case',case.name,'--plan',str(whole/'plan.json'),'--forecast',str(whole/'forecast.json'),'--experiment','E6']
        proc=subprocess.run(cmd,text=True,capture_output=True)
        (suite/'E6.stdout.log').write_text(proc.stdout); (suite/'E6.stderr.log').write_text(proc.stderr)
        write_json(suite/'E6_command.json',{'argv':cmd,'exit_code':proc.returncode,'clean_root_absent_before':True})
        if proc.returncode: raise RuntimeError(f'E6 command failed ({proc.returncode}): {proc.stderr[-2000:]}')
        dirs=[p for p in rerun_root.iterdir() if p.is_dir() and (p/'run_manifest.json').exists()]
        if len(dirs)!=1: raise RuntimeError('E6 did not produce exactly one new run directory')
        records['experiments']['E6']=str(dirs[0]); save()
        verdict=verify_suite(suite)
        records['status']=verdict['status']; save()
        write_json(suite/'acceptance.json',verdict)
        finish_manifest(suite,verdict['status'])
        return {'status':verdict['status'],'suite_dir':str(suite),'suite_id':suite.name,'acceptance':verdict}
    except Exception as e:
        records.update(status='FAIL',failure_reason=str(e)); save()
        finish_manifest(suite,'FAIL',failure_reason=str(e))
        raise
