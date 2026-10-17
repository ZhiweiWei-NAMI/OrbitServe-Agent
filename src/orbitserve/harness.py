"""Actual asynchronous local scientific-tool harness used by E0.

Distributed workers implement the same job lifecycle with link-mediated inputs.
This harness never opens the evaluator's isolated reference directory.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import resource
import os
import time
import traceback
from uuid import uuid4
from .evidence import append_jsonl, file_manifest

class Harness:
    def __init__(self, run_dir):
        self.run_dir=Path(run_dir)
        self.run_dir.mkdir(parents=True,exist_ok=True)
        self.jobs={}
        self.futures={}
        self.lock=threading.Lock()
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='science-harness')
        self.started=time.monotonic()

    def _event(self, job, status, **extra):
        usage=resource.getrusage(resource.RUSAGE_SELF)
        observed_at=time.monotonic()-self.started
        event={**job, 'status':status,'timestamp':observed_at,
               'resource_observation':{'scope':'baseline_host_process_not_simulated_satellite',
                   'process_id':os.getpid(),'observed_at':observed_at,
                   'max_rss_bytes':usage.ru_maxrss*1024,'user_cpu_s':usage.ru_utime,
                   'system_cpu_s':usage.ru_stime,'method':'Linux getrusage(RUSAGE_SELF); RSS high-water mark'},**extra}
        with self.lock:
            self.jobs[job['job_id']]=event
            append_jsonl(self.run_dir/'tool_calls.jsonl',event)
        return event

    def submit_tool(self,node_id,tool_id,input_artifact_ids,params,mission_id,plan_version=1,*,case_dir,output_dir):
        job_id='job-'+uuid4().hex
        job={'job_id':job_id,'run_id':self.run_dir.name,'node_id':node_id,'actual_execution_node':node_id,
             'tool_id':tool_id,'input_artifact_ids':input_artifact_ids,'params':params,
             'mission_id':mission_id,'plan_id':mission_id+'-science-baseline','plan_version':plan_version,
             'step_id':tool_id,'process_id':__import__('os').getpid()}
        self._event(job,'accepted')
        self.futures[job_id]=self.pool.submit(self._run,job,Path(case_dir),Path(output_dir))
        return job_id

    def _run(self,job,case_dir,output_dir):
        try:
            self._event(job,'running',input_manifest=file_manifest(case_dir,exclude=('outputs','evaluation')))
            from .science import run_operator
            output_dir.mkdir(parents=True,exist_ok=True)
            result=run_operator(job['tool_id'],case_dir,output_dir,job['params'])
            outputs=file_manifest(output_dir)
            if not outputs: raise RuntimeError('Scientific operator produced no files')
            return self._event(job,'succeeded',result=result,output_dir=str(output_dir),
               output_artifacts=[dict(a,artifact_id='sha256:'+a['sha256']) for a in outputs],failure_reason=None)
        except Exception as e:
            self._event(job,'failed',failure_reason=f'{type(e).__name__}: {e}',traceback=traceback.format_exc())
            raise

    def get_job(self,job_id):
        with self.lock: return dict(self.jobs[job_id])

    def wait(self,job_id): return self.futures[job_id].result()
    def close(self): self.pool.shutdown(wait=True)
