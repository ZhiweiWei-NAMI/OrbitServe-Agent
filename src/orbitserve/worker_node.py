"""A single isolated node. Only broker-delivered bytes/controls enter this process."""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid
import zipfile

from .policy import choose_action


def digest(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):h.update(b)
    return h.hexdigest()


def safe_name(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


class Node:
    def __init__(self, init):
        self.id=init["node"]["node_id"]; self.config=init["node"]
        self.peers=init["peers"]; self.t0=init["t0"]; self.run_id=init["run_id"]
        self.context={"run_id":self.run_id}; self.lock=threading.Lock()
        self.root=Path('/node'); self.artifacts={}; self.outgoing={}; self.incoming={}
        self.policy=choose_action; self.neighbors={}; self.jobs={}; self.completed={}; self.receipts={}; self.known_remote={n:set() for n in self.peers}
        self.plan=init.get('plan'); self.forecast_view=init.get('forecast_view',{})
        self.busy_until_s=float(init.get('current_environment',{}).get('busy_until_s',0))
        self.external_queue=float(init.get('current_environment',{}).get('queue_work_pixels',0))
        self.energy=float(self.config['energy_j']); self.last_ad=-100.; self.next_decision=0
        self.future=None; self.active=None; self.inflight={}; self.final_sent=False
        self.pool=ThreadPoolExecutor(max_workers=1); self.running=True
        for d in ('artifacts','partial','outgoing','jobs'): (self.root/d).mkdir(exist_ok=True)
        if self.plan:self.set_context()
        self.emit('node_events',event='worker_started',namespace_pid=os.getpid(),host_pid=init['host_pid'],
                  role=self.config['role'],own_root='/node',policy_instance=f'{self.id}:{uuid.uuid4().hex}',
                  pid_namespace=os.readlink('/proc/self/ns/pid'),net_namespace=os.readlink('/proc/self/ns/net'),
                  mount_namespace=os.readlink('/proc/self/ns/mnt'),mountinfo=Path('/proc/self/mountinfo').read_text())
        if self.busy_until_s:
            self.emit('node_events',event='local_environment_observed',busy_until_s=self.busy_until_s,
                      queue_work_pixels=self.external_queue,source='current_local_environment')
        if self.id=='A' and (self.root/'case').exists():
            self.register_directory('case',self.root/'case','preloaded_source',0)
        self.advertise()
        if self.id=='G' and self.plan:self.control('A',{'kind':'plan','plan':self.plan,'forecast_view':self.forecast_view})

    def now(self):return time.monotonic()-self.t0
    def set_context(self):
        self.context.update({k:self.plan.get(k) for k in ('mission_id','plan_id','case_id')})
        self.context['plan_version']=self.plan.get('version',1)
    def send(self,obj):
        with self.lock:
            sys.stdout.write(json.dumps(obj,separators=(',',':'),allow_nan=False)+'\n');sys.stdout.flush()
    def emit(self,channel,**fields):
        self.send({'kind':'event','channel':channel,'event_data':dict(self.context,node_id=self.id,actor_node=self.id,
                    timestamp=self.now(),pid=os.getpid(),**fields)})
    def control(self,destination,payload):
        mid=f'{self.id}-{uuid.uuid4().hex}'
        if destination==self.id:
            self.emit('control_messages',event='local_control',source=self.id,destination=self.id,message_id=mid,message_type=payload['kind'])
            self.handle_control({'source':self.id,'message_id':mid,'payload':payload,'edge_rate_bytes_s':0})
            return mid
        self.send({'kind':'send_control','source':self.id,'destination':destination,'message_id':mid,'payload':payload,**self.context})
        return mid
    def observation(self):
        work=sum(j['step'].get('estimated_compute_pixels',0) for j in self.jobs.values() if j['status'] in ('accepted','queued','running','waiting_link'))
        occupied=sum(j['step'].get('estimated_memory_bytes',0) for j in self.jobs.values() if j['status'] in ('accepted','queued','waiting_link','running'))
        used=sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        pending=sum(j['status'] in ('accepted','queued','waiting_link','running') for j in self.jobs.values())
        assets=[{'artifact_id':aid,'node_id':self.id,'sha256':a['sha256'],
                 'total_bytes':sum(f['size_bytes'] for f in a['files']),'file_count':len(a['files']),
                 'verified_at':a['verified_at'],'location_kind':'node_private_store'}
                for aid,a in self.artifacts.items()]
        result=dict(self.config,timestamp=self.now(),now=self.now(),queue_work_pixels=work+(self.external_queue if self.now()<self.busy_until_s else 0),
                    busy_until_s=self.busy_until_s,available_after_s=max(0,self.busy_until_s-self.now()),
                    assets=assets,pending_job_count=pending,service_available=pending<self.config['max_queued_jobs'] and self.now()>=self.busy_until_s,
                    memory_bytes=self.config['memory_bytes']-occupied,storage_bytes=max(0,self.config['storage_bytes']-used),energy_j=self.energy)
        return result
    def advertise(self):
        obs=self.observation();self.emit('observations',event='local_observation',observation=obs)
        for n in self.peers:self.control(n,{'kind':'advertisement','observation':obs})
        self.last_ad=self.now()
    def decide(self,task,neighbors=None):
        obs=self.observation()
        ads=list(self.neighbors.values()) if neighbors is None else neighbors
        action=self.policy(obs,task,ads,self.forecast_view)
        self.emit('decisions',event='policy_decision',step_id=task.get('step_id'),job_id=task.get('job_id'),
                  local_observation=obs,received_neighbors=ads,task=task,decision=action,
                  action=action.get('action'),destination=action.get('destination'),
                  observation_timestamp=obs['timestamp'])
        return action
    def manifest(self,path):
        return [{'path':str(p.relative_to(path)),'size_bytes':p.stat().st_size,'sha256':digest(p)}
                for p in sorted(path.rglob('*')) if p.is_file()]
    def register_directory(self,artifact_id,path,source,verified_at,expected_files=None):
        files=self.manifest(path)
        if expected_files is not None and files!=expected_files:raise RuntimeError('artifact member hash mismatch')
        self.artifacts[artifact_id]={'artifact_id':artifact_id,'path':str(path),'files':files,
                                    'verified_at':verified_at,'source':source,
                                    'sha256':hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()}
        return self.artifacts[artifact_id]
    def make_bundle(self,artifact_id):
        art=self.artifacts[artifact_id]
        dest=self.root/'outgoing'/f'{safe_name(artifact_id)}.zip'
        if not dest.exists():
            with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_STORED) as z:
                for f in art['files']:z.write(Path(art['path'])/f['path'],f['path'])
        return dest
    def transfer(self,artifact_id,destination,purpose,job=None):
        if destination==self.id:return None
        for prior in self.outgoing.values():
            if prior['artifact_id']==artifact_id and prior['destination']==destination:return prior['transfer_id']
        path=self.make_bundle(artifact_id);tid=f't-{uuid.uuid4().hex}'
        info=dict(transfer_id=tid,source=self.id,destination=destination,artifact_id=artifact_id,
                  size_bytes=path.stat().st_size,sha256=digest(path),files=self.artifacts[artifact_id]['files'],
                  purpose=purpose,job_id=job.get('job_id') if job else None,step_id=job.get('step',{}).get('step_id') if job else None,**self.context)
        self.outgoing[tid]={'path':path,**info}
        self.send({'kind':'offer_transfer',**info})
        return tid
    def on_pull(self,msg):
        info=self.outgoing[msg['transfer_id']]
        with info['path'].open('rb') as f:
            f.seek(msg['offset']);data=f.read(msg['length'])
        self.send({'kind':'transfer_chunk','transfer_id':msg['transfer_id'],'offset':msg['offset'],
                   'data':base64.b64encode(data).decode(),'chunk_sha256':hashlib.sha256(data).hexdigest()})
    def on_begin(self,info):
        tid=info['transfer_id'];p=self.root/'partial'/f'{tid}.part'
        if tid not in self.incoming:
            if info['size_bytes'] > self.observation()['storage_bytes']:
                raise RuntimeError('incoming artifact exceeds actual remaining storage capacity')
            p.touch(exist_ok=False);self.incoming[tid]={'info':info,'path':p,'offset':0}
            self.emit('node_events',event='artifact_incomplete',artifact_id=info['artifact_id'],transfer_id=tid,
                      partial_path=str(p),computable=False,size_bytes=0)
    def on_chunk(self,msg):
        incoming=self.incoming[msg['transfer_id']];info=incoming['info'];data=base64.b64decode(msg['data'])
        if msg['offset']!=incoming['offset']:raise RuntimeError('noncontiguous receive offset')
        if hashlib.sha256(data).hexdigest()!=msg['chunk_sha256']:raise RuntimeError('chunk hash mismatch')
        with incoming['path'].open('ab') as f:f.write(data)
        incoming['offset']+=len(data)
        self.send({'kind':'chunk_received','transfer_id':msg['transfer_id'],'offset':incoming['offset']})
        if incoming['offset']==info['size_bytes']:
            actual=digest(incoming['path'])
            if actual!=info['sha256']:raise RuntimeError('completed transfer hash mismatch')
            dest=self.root/'artifacts'/safe_name(info['artifact_id']);dest.mkdir(exist_ok=True)
            with zipfile.ZipFile(incoming['path']) as z:
                for member in z.infolist():
                    name=Path(member.filename)
                    if name.is_absolute() or '..' in name.parts or member.is_dir():
                        raise RuntimeError('unsafe archive entry')
                z.extractall(dest)
            registered=self.register_directory(info['artifact_id'],dest,info['source'],self.now(),expected_files=info['files'])
            incoming['path'].unlink()
            self.emit('transfers',event='verified',**{k:v for k,v in info.items() if k not in self.context},
                      received_sha256=actual,received_bytes=incoming['offset'],computable=True,verified_at=registered['verified_at'])
            self.emit('node_events',event='artifact_registered',artifact_id=info['artifact_id'],transfer_id=info['transfer_id'],
                      files=registered['files'],sha256=registered['sha256'],verified_at=registered['verified_at'],computable=True)
            self.control(info['source'],{'kind':'transfer_receipt','transfer_id':info['transfer_id'],
                         'artifact_id':info['artifact_id'],'sha256':actual,'received_bytes':incoming['offset']})
            if info['purpose']=='delivery':self.validate_delivery(info)
            self.maybe_complete_owner()
    def validate_delivery(self,info):
        from PIL import Image
        import rasterio
        art=self.artifacts[info['artifact_id']];p=Path(art['path'])
        required=['change.tif','before_water.tif','after_water.tif','valid_common.tif','stats.json','changes.geojson','map.png']
        for fn in required:
            if not (p/fn).is_file():raise RuntimeError(f'ground delivery missing {fn}')
        raster_checks={}
        for fn in required[:4]:
            with rasterio.open(p/fn) as ds:
                a=ds.read(1);raster_checks[fn]={'shape':list(a.shape),'crs':str(ds.crs),'pixels':a.size}
        stats=json.loads((p/'stats.json').read_text());geo=json.loads((p/'changes.geojson').read_text())
        if geo.get('type')!='FeatureCollection':raise RuntimeError('invalid GeoJSON delivery')
        with Image.open(p/'map.png') as im:im.verify()
        task={'step_id':'ground_delivery','required_capability':'receive_delivery','allowed_nodes':['G'],
              'estimated_compute_pixels':0,'estimated_memory_bytes':1024,'estimated_storage_bytes':0,
              'input_bytes':0,'output_bytes':0,'result_owner':'G'}
        if self.decide(task,[])['action']!='local':raise RuntimeError('ground admission failed')
        self.emit('node_events',event='delivered',artifact_id=info['artifact_id'],outputs_dir=str(p),
                  files=art['files'],sha256=art['sha256'],raster_validation=raster_checks,statistics=stats,
                  delivery_transfer_id=info['transfer_id'],status='delivered')
        self.control('A',{'kind':'delivery_receipt','artifact_id':info['artifact_id']})
    def handle_control(self,packet):
        payload=packet['payload'];src=packet['source'];kind=payload['kind']
        if kind=='advertisement':
            obs=dict(payload['observation'],received_at=self.now(),message_id=packet['message_id'],observation_source='advertisement')
            self.neighbors[src]=obs
        elif kind=='plan':
            if self.id!='A':raise RuntimeError('only source accepts plan')
            if self.plan and self.plan['plan_id']!=payload['plan']['plan_id']:raise RuntimeError('different plan already installed')
            self.plan=payload['plan'];self.forecast_view=payload.get('forecast_view',{});self.set_context()
            self.emit('plans',event='plan_received',plan=self.plan,message_id=packet['message_id'],source=src)
        elif kind=='request':self.admit(payload,src)
        elif kind=='request_receipt':
            pending=self.inflight.get(payload['job_id'])
            if pending:
                pending['accepted']=payload['status']=='accepted'
                if payload.get('observation') and src!=self.id:
                    self.neighbors[src]=dict(payload['observation'],received_at=self.now(),message_id=packet['message_id'],observation_source='request_receipt')
                if payload['status']=='rejected':
                    self.emit('node_events',event='dispatch_rejected',job_id=payload['job_id'],step_id=pending['step']['step_id'],reason=payload['reason'])
                    self.inflight.pop(payload['job_id']);self.next_decision=self.now()+.1
        elif kind=='tool_receipt':
            if payload['status']=='failed':raise RuntimeError(f"remote job failed {payload}")
            self.receipts[payload['step_id']]=payload;self.known_remote.setdefault(src,set()).add(payload['artifact_id'])
            self.maybe_complete_owner()
        elif kind=='transfer_receipt':
            self.known_remote.setdefault(src,set()).add(payload['artifact_id'])
            self.emit('node_events',event='transfer_receipt_received',source=src,**{k:v for k,v in payload.items() if k!='kind'})
        elif kind=='delivery_receipt':
            self.emit('node_events',event='delivery_receipt_received',artifact_id=payload['artifact_id'])
        else:raise RuntimeError(f'unknown control kind {kind}')
    def admit(self,payload,source):
        jid=payload['job_id'];step=payload['step']
        if jid in self.jobs:
            self.control(source,{'kind':'request_receipt','job_id':jid,'status':'accepted','idempotent':True});return
        if not self.plan:
            self.plan=payload['plan_metadata'];self.set_context()
        self.forecast_view=payload.get('forecast_view',self.forecast_view)
        own_task=dict(step,job_id=jid,result_owner=source,allowed_nodes=[self.id],input_bytes=0,output_bytes=0)
        action=self.decide(own_task,[]);obs=self.observation();reasons=[]
        if action['action']!='local':reasons.append('own_policy_resource_or_capability_rejected')
        if self.now()<self.busy_until_s:reasons.append('current_queue_busy')
        pending=sum(j['status'] in ('accepted','queued','waiting_link','running') for j in self.jobs.values())
        if pending>=self.config['max_queued_jobs']:reasons.append('queue_capacity')
        if reasons:
            self.emit('node_events',event='admission_rejected',job_id=jid,step_id=step['step_id'],reasons=reasons,observation=obs)
            self.control(source,{'kind':'request_receipt','job_id':jid,'status':'rejected','reason':reasons,'observation':obs});return
        job={'job_id':jid,'step':step,'owner':source,'input_artifact_ids':payload['input_artifact_ids'],
             'status':'accepted','artifact_id':payload['artifact_id']}
        self.jobs[jid]=job
        self.job_event(job,'accepted',resource_observation=obs)
        self.control(source,{'kind':'request_receipt','job_id':jid,'status':'accepted','observation':self.observation()})
        self.job_event(job,'queued')
        self.advertise()
    def job_event(self,job,status,**extra):
        job['status']=status
        self.emit('tool_calls',event='job_status',status=status,job_id=job['job_id'],step_id=job['step']['step_id'],
                  tool_id=job['step']['tool_id'],params=job['step'].get('params',{}),
                  input_artifact_ids=job['input_artifact_ids'],output_artifact_ids=[job['artifact_id']] if status=='succeeded' else [],**extra)
    def start_ready_job(self):
        if self.future is not None or self.now()<self.busy_until_s:return
        for job in self.jobs.values():
            if job['status'] not in ('queued','waiting_link'):continue
            if any(a not in self.artifacts for a in job['input_artifact_ids']):
                if job['status']!='waiting_link':self.job_event(job,'waiting_link')
                continue
            self.active=job;params=dict(job['step'].get('params',{}))
            if job['step']['tool_id']=='water_merge':
                params['tile_dirs']=[self.artifacts[a]['path'] for a in job['input_artifact_ids'] if a!='case']
            output=self.root/'jobs'/safe_name(job['job_id']);output.mkdir(exist_ok=True)
            inputs=[{k:v for k,v in self.artifacts[a].items() if k!='path'} for a in job['input_artifact_ids']]
            self.job_event(job,'running',input_artifacts=inputs,actual_params=params,
                           reserved_memory_bytes=job['step'].get('estimated_memory_bytes',0),resource_observation=self.observation())
            self.active['started_at']=self.now()
            def calculate():
                from .science import run_operator
                return run_operator(job['step']['tool_id'],Path(self.artifacts['case']['path']),output,params)
            self.future=self.pool.submit(calculate);return
    def collect_job(self):
        if self.future is None or not self.future.done():return
        job=self.active
        try:
            result=self.future.result();out=self.root/'jobs'/safe_name(job['job_id'])
            art=self.register_directory(job['artifact_id'],out,self.id,self.now())
            if not art['files']:raise RuntimeError('operator returned no files')
            self.energy-=float(job['step'].get('estimated_energy_j',job['step'].get('estimated_compute_pixels',0)*.00001))
            self.job_event(job,'succeeded',output_artifacts=[{k:v for k,v in art.items() if k!='path'}],
                           result=result,actual_elapsed_s=self.now()-job['started_at'],
                           simulated_extra_compute_delay_s=0,energy_source='configured_model_estimate',remaining_energy_j=self.energy)
            self.transfer(job['artifact_id'],job['owner'],'job_result',job)
            self.control(job['owner'],{'kind':'tool_receipt','job_id':job['job_id'],'step_id':job['step']['step_id'],
                         'status':'succeeded','artifact_id':job['artifact_id'],'artifact_sha256':art['sha256']})
        except Exception as exc:
            self.job_event(job,'failed',error=str(exc),traceback=traceback.format_exc())
            self.control(job['owner'],{'kind':'tool_receipt','job_id':job['job_id'],'step_id':job['step']['step_id'],
                         'status':'failed','error':str(exc)})
        self.active=None;self.future=None;self.advertise()
    def maybe_complete_owner(self):
        if self.id!='A' or not self.plan:return
        for step_id,receipt in self.receipts.items():
            if step_id not in self.completed and receipt['artifact_id'] in self.artifacts:
                if self.artifacts[receipt['artifact_id']]['sha256']!=receipt['artifact_sha256']:raise RuntimeError('result receipt artifact hash mismatch')
                self.completed[step_id]=receipt['artifact_id']
                self.emit('node_events',event='step_completed',step_id=step_id,job_id=receipt['job_id'],artifact_id=receipt['artifact_id'])
                self.inflight.pop(receipt['job_id'],None)
    def schedule(self):
        if self.id!='A' or not self.plan or len(self.inflight)>=self.config.get('max_owned_inflight',2) or self.now()<self.next_decision:return
        steps=self.plan['steps']
        if len(self.completed)==len(steps):
            if not self.final_sent:
                final=steps[-1]['step_id'];self.transfer(self.completed[final],'G','delivery')
                self.final_sent=True;self.emit('node_events',event='mission_partial',status='partial',reason='satellite product awaiting ground receipt')
            return
        # Any received feasible neighbor suffices; no dependency on ground advertisements.
        for step in steps:
            sid=step['step_id']
            if sid in self.completed or any(j['step']['step_id']==sid for j in self.inflight.values()) or any(d not in self.completed for d in step.get('dependencies',[])):continue
            inputs=['case']+[self.completed[d] for d in step.get('dependencies',[])]
            task=dict(step,input_bytes=sum(sum(f['size_bytes'] for f in self.artifacts[a]['files']) for a in inputs),
                      output_bytes=step.get('estimated_output_bytes',250000),result_owner='A',owner_node='A')
            # Cached input locations are local knowledge learned from transfer/result receipts.
            neighbors=[]
            for node,obs in self.neighbors.items():
                item=dict(obs);item['input_bytes']=sum(sum(f['size_bytes'] for f in self.artifacts[a]['files']) for a in inputs if a not in self.known_remote[node])
                neighbors.append(item)
            self.emit('node_events',event='step_ready',step_id=sid,dependencies=step.get('dependencies',[]),input_artifact_ids=inputs)
            action=self.decide(task,neighbors)
            if action['action']=='wait':self.next_decision=self.now()+.3;return
            destination=action.get('destination')
            if action['action']=='local':destination=self.id
            jid=f"{self.run_id}:{sid}:{uuid.uuid4().hex[:10]}";artifact_id=f"{self.plan['plan_id']}:{sid}"
            job={'job_id':jid,'step':step,'destination':destination,'artifact_id':artifact_id,'accepted':False}
            self.inflight[jid]=job
            self.emit('node_events',event='dispatch',job_id=jid,step_id=sid,destination=destination,input_artifact_ids=inputs)
            metadata={k:v for k,v in self.plan.items() if k!='steps'}
            self.control(destination,{'kind':'request','job_id':jid,'step':step,'input_artifact_ids':inputs,
                        'artifact_id':artifact_id,'plan_metadata':metadata,'forecast_view':self.forecast_view})
            for a in inputs:
                if destination!=self.id and a not in self.known_remote[destination]:self.transfer(a,destination,'job_input',job)
            return
    def tick(self):
        self.collect_job();self.start_ready_job();self.schedule()
        if self.now()-self.last_ad>.75:self.advertise()
    def handle(self,msg):
        kind=msg['kind']
        if kind=='control':self.handle_control(msg)
        elif kind=='transfer_begin':self.on_begin(msg['info'])
        elif kind=='transfer_chunk':self.on_chunk(msg)
        elif kind=='pull_chunk':self.on_pull(msg)
        elif kind=='transport_ack':pass
        elif kind=='stop':self.running=False
        else:raise RuntimeError(f'unknown broker packet {kind}')


def main():
    init=json.loads(sys.stdin.readline());node=Node(init);messages=queue.Queue()
    def reader():
        for line in sys.stdin:
            messages.put(json.loads(line))
        messages.put({'kind':'stop'})
    threading.Thread(target=reader,daemon=True).start()
    try:
        while node.running:
            try:node.handle(messages.get(timeout=.01))
            except queue.Empty:pass
            node.tick()
        node.emit('observations',event='final_local_observation',observation=node.observation())
        node.emit('node_events',event='worker_stopped')
    except Exception as exc:
        node.emit('node_events',event='worker_failed',error=str(exc),traceback=traceback.format_exc())
        traceback.print_exc(file=sys.stderr);raise
    finally:node.pool.shutdown(wait=True)

if __name__=='__main__':main()
