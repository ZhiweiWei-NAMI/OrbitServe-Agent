"""Four isolated node processes and a non-scheduling, capacity-gated link broker.

The broker owns only transport/time and hidden environment disturbances. All DAG
ownership, policy choices, admission, scientific execution and delivery checks
run in node processes. There is no broker call to ExecutionPolicy or run_operator.
"""
from __future__ import annotations
from collections import defaultdict, deque
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from .isolation import isolation_probe, sandbox_command

CHANNELS=('forecasts','plans','decisions','tool_calls','transfers','node_events','control_messages','observations')


def default_config() -> dict:
    path=Path(__file__).resolve().parents[2]/'configs'/'nodes.json'
    cfg=json.loads(path.read_text())
    cfg['known_contact_plan']=[{'source':a['node_id'],'destination':b['node_id'],
        'start_s':0.,'end_s':float(cfg['timeout_s']),'bandwidth_bytes_s':cfg['bandwidth_bytes_s'],
        'throughput_bytes_s':cfg['bandwidth_bytes_s'],'latency_s':cfg['control_latency_s'],
        'source_kind':'synthetic_contact_trace'} for a in cfg['nodes'] for b in cfg['nodes'] if a!=b]
    return cfg


def observed_snapshot(config:dict|None=None,timestamp:float=0.) -> dict:
    cfg=config or default_config()
    return {'timestamp':timestamp,'nodes':[dict(n,timestamp=timestamp,busy_until_s=0.) for n in cfg['nodes']],
            'assets':[],'edges':copy.deepcopy(cfg.get('known_contact_plan',[])),
            'source_kind':'observed_configuration','clock_mode':'monotonic_wall_clock_1x'}


def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def _worker_pid(launcher):
    """Read namespace descendants externally; /proc in workers contains no host PIDs."""
    processes={}
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():continue
        try:
            status=(item/'status').read_text()
            ppid=int(next(x.split(':',1)[1].strip() for x in status.splitlines() if x.startswith('PPid:')))
            cmd=(item/'cmdline').read_bytes().replace(b'\0',b' ').decode()
            processes[int(item.name)]=(ppid,cmd)
        except (OSError,ValueError,StopIteration,UnicodeError):pass
    descendants={launcher};changed=True
    while changed:
        new={pid for pid,(parent,_) in processes.items() if parent in descendants}
        changed=not new.issubset(descendants);descendants.update(new)
    candidates=[pid for pid in descendants if pid in processes and
                'orbitserve.worker_node' in processes[pid][1] and 'python' in processes[pid][1].split(' ')[0]]
    return max(candidates) if candidates else None



class Broker:
    def __init__(self,run_dir,config,scenario,plan=None,case_dir=None,forecast_graph=None):
        self.run_dir=Path(run_dir).resolve();self.run_dir.mkdir(parents=True,exist_ok=True)
        self.config=config;self.scenario=scenario;self.plan=plan;self.case_dir=case_dir;self.forecast_graph=forecast_graph
        self.run_id=self.run_dir.name;self.t0=time.monotonic();self.procs={};self.stderr={};self.threads=[]
        self.messages=queue.Queue();self.edge_queues=defaultdict(deque);self.tokens=defaultdict(float)
        self.last_tick=self.t0;self.tick_id=0;self.transfers={};self.events=[];self.failure=None;self.delivered=None
        self.ground_down=None;self.transfer_down=None;self.triggered_transfer_outage=False
        self.process_records={};self.final_observations={};self.started=set();self.stopped=set()
        self.context={'run_id':self.run_id}
        if plan:self.context.update({k:plan.get(k) for k in ('mission_id','plan_id','case_id')});self.context['plan_version']=plan.get('version',1)
        self.handles={n:(self.run_dir/f'{n}.jsonl').open('a') for n in CHANNELS}
        self.edges={(e['source'],e['destination']):e for e in config['known_contact_plan']}
    def now(self):return time.monotonic()-self.t0
    def log(self,channel,**fields):
        entry={**self.context,'timestamp':self.now(),'event_id':f'e-{uuid.uuid4().hex}',**fields}
        self.handles[channel].write(json.dumps(entry,sort_keys=True,allow_nan=False)+'\n');self.handles[channel].flush()
        return entry
    def send(self,node,message):
        p=self.procs[node]
        try:p.stdin.write(json.dumps(message,separators=(',',':'))+'\n');p.stdin.flush()
        except (BrokenPipeError,OSError) as e:self.failure=f'{node} IPC failed: {e}'
    def launch(self):
        nodes=self.config['nodes'];roots={n['node_id']:self.run_dir/'nodes'/n['node_id'] for n in nodes}
        for root in roots.values():root.mkdir(parents=True,exist_ok=True)
        if self.case_dir is not None:
            src=Path(self.case_dir)
            if not (src/'task.json').exists() or not (src/'assets.json').exists():raise ValueError('source case metadata missing')
            target=roots['A']/'case';target.mkdir()
            # Only metadata and declared imagery enter source A; never evaluation/output trees.
            for fn in ('task.json','assets.json'):shutil.copyfile(src/fn,target/fn)
            cat=json.loads((src/'assets.json').read_text())
            for asset in cat['assets']:
                rel=Path(asset['path'])
                if rel.is_absolute() or '..' in rel.parts:raise ValueError('unsafe source asset path')
                source=src/rel;dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True)
                if asset.get('sha256') and _sha(source)!=asset['sha256']:raise ValueError(f'input hash mismatch: {rel}')
                shutil.copyfile(source,dest)
        worker_source=self.run_dir/'worker_source';(worker_source/'orbitserve').mkdir(parents=True,exist_ok=True)
        source_files={}
        for filename in ('__init__.py','worker_node.py','policy.py','science.py'):
            original=Path(__file__).parent/filename;copy=worker_source/'orbitserve'/filename
            shutil.copyfile(original,copy);source_files[filename]=_sha(copy)
        self.log('node_events',event='worker_source_allowlist',modules=source_files,hidden_simulator_mounted=False)
        proof=isolation_probe(roots['A'],source_root=worker_source);(self.run_dir/'isolation.json').write_text(json.dumps(proof,indent=2))
        self.log('node_events',event='isolation_probe',evidence=proof)
        for n in nodes:
            node=n['node_id'];err=(self.run_dir/f'worker_{node}.stderr.log').open('w');self.stderr[node]=err
            p=subprocess.Popen(sandbox_command(roots[node],source_root=worker_source),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                               stderr=err,text=True,bufsize=1)
            self.procs[node]=p
            init={'node':n,'peers':[x['node_id'] for x in nodes if x['node_id']!=node],
                  't0':self.t0,'run_id':self.run_id,'host_pid':p.pid}
            if node=='G' and self.plan:
                init['plan']=self.plan;init['forecast_view']=self.forecast_graph
            if self.scenario=='busy_b' and node=='B':
                init['current_environment']={'busy_until_s':self.now()+25.,'queue_work_pixels':25*n['compute_pixels_s']}
                self.log('node_events',event='environment_disturbance',target_node=node,
                         observed_at=self.now(),queue_work_pixels=init['current_environment']['queue_work_pixels'],
                         busy_until_s=init['current_environment']['busy_until_s'],visibility='target_local_only')
            self.send(node,init)
            def reader(which,process):
                for line in process.stdout:
                    try:self.messages.put((which,json.loads(line)))
                    except json.JSONDecodeError:self.messages.put((which,{'kind':'invalid_output','line':line}))
                self.messages.put((which,{'kind':'process_eof'}))
            th=threading.Thread(target=reader,args=(node,p),daemon=True);th.start();self.threads.append(th)
        self.log('node_events',event='scenario_started',scenario=self.scenario,contact_source='synthetic_contact_trace',
                 clock_mode='monotonic_wall_clock_1x',bandwidth_bytes_s=self.config['bandwidth_bytes_s'],
                 transport_carrier='stdio_pipes_base64_payloads',capacity_accounting='application_bytes_plus_serialized_control_bytes',
                 burst_cap_bytes=2*self.config['chunk_bytes'])
    def edge_up(self,edge,now):
        e=self.edges.get(edge)
        if not e or not(float(e.get('start_s',0))<=now<float(e.get('end_s',self.config['timeout_s']))):return False
        if self.ground_down and self.ground_down[0]<=now<self.ground_down[1] and 'G' in edge:return False
        if self.transfer_down and self.transfer_down['start']<=now<self.transfer_down['end'] and set(edge)==set(self.transfer_down['edge']):return False
        return True
    def enqueue(self,source,destination,msg,kind,wire_bytes,meta=None):
        edge=(source,destination)
        if edge not in self.edges:raise ValueError(f'unknown directed edge {edge}')
        self.edge_queues[edge].append({'message':msg,'kind':kind,'bytes':wire_bytes,'remaining':wire_bytes,
            'queued_at':self.now(),'earliest':self.now()+self.config['control_latency_s'],'meta':meta or {},'started_at':None})
    def on_message(self,node,msg):
        kind=msg['kind']
        if kind=='event':
            ev=msg['event_data'];channel=msg['channel']
            if channel not in CHANNELS:raise ValueError('unknown log channel')
            if ev.get('node_id')!=node:raise ValueError('worker identity mismatch')
            if ev.get('event')=='worker_started':
                self.started.add(node);host=_worker_pid(self.procs[node].pid)
                ev.update(host_pid=host,launcher_pid=self.procs[node].pid,host_private_root=str(self.run_dir/'nodes'/node))
                self.process_records[node]={k:ev[k] for k in ('host_pid','launcher_pid','namespace_pid','pid_namespace','net_namespace','mount_namespace','own_root','host_private_root','role','policy_instance')}
            if ev.get('event')=='worker_stopped':self.stopped.add(node)
            if ev.get('event')=='worker_failed':self.failure=f"{node}: {ev['error']}"
            if ev.get('event')=='delivered':
                if node!='G':raise RuntimeError('delivery may only be validated by G')
                self.delivered=ev
            logged=self.log(channel,**ev)
            if self.scenario=='ground_outage' and node=='A' and ev.get('event')=='plan_received' and self.ground_down is None:
                start=self.now();end=start+self.config['ground_outage_duration_s'];self.ground_down=(start,end)
                self.log('node_events',event='ground_link_closed',timestamp=start,start_s=start,end_s=end,
                         edges='all_G_incident',trigger='source_confirmed_plan_received',source_event_id=logged['event_id'])
            if ev.get('event') in ('final_local_observation','local_observation'):
                self.final_observations[node]=dict(ev['observation'],source_event_id=logged['event_id'],source_run_id=self.run_id)
        elif kind=='send_control':
            if msg['source']!=node:raise ValueError('control source mismatch')
            dest=msg['destination'];edge=(node,dest);payload=msg['payload'];wire=len(json.dumps(payload,separators=(',',':')).encode())
            packet={'kind':'control','source':node,'destination':dest,'message_id':msg['message_id'],'payload':payload,
                    'edge_rate_bytes_s':self.edges[edge]['bandwidth_bytes_s']}
            meta={'message_id':msg['message_id'],'message_type':payload['kind'],'source':node,'destination':dest,'payload':payload}
            self.log('control_messages',event='queued',size_bytes=wire,**meta)
            self.enqueue(node,dest,packet,'control',wire,meta)
        elif kind=='offer_transfer':
            if msg['source']!=node:raise ValueError('transfer source mismatch')
            tid=msg['transfer_id'];info={k:v for k,v in msg.items() if k!='kind'}
            if tid in self.transfers:raise ValueError('duplicate transfer id')
            self.transfers[tid]={'info':info,'offset':0,'pending':False,'begun':False,'paused':False,'complete':False}
            self.log('transfers',event='started',offset=0,bytes_sent=0,**info)
            packet={'kind':'transfer_begin','info':info};wire=len(json.dumps(packet,separators=(',',':')).encode())
            self.enqueue(node,info['destination'],packet,'transfer_begin',wire,{'transfer_id':tid})
        elif kind=='transfer_chunk':
            tr=self.transfers[msg['transfer_id']];info=tr['info'];data=base64.b64decode(msg['data'])
            if node!=info['source'] or msg['offset']!=tr['offset']:raise RuntimeError('source chunk sequence mismatch')
            if hashlib.sha256(data).hexdigest()!=msg['chunk_sha256']:raise RuntimeError('broker chunk hash mismatch')
            if not data:raise RuntimeError('unexpected empty source chunk')
            packet={'kind':'transfer_chunk',**{k:v for k,v in msg.items() if k!='kind'}}
            self.enqueue(node,info['destination'],packet,'data',len(data),{'transfer_id':msg['transfer_id'],'chunk_sha256':msg['chunk_sha256']})
        elif kind=='chunk_received':
            tr=self.transfers[msg['transfer_id']]
            if node!=tr['info']['destination'] or msg['offset']!=tr['offset']:raise RuntimeError('receiver transport ACK mismatch')
            source=tr['info']['source']
            ack={'kind':'transport_ack','transfer_id':msg['transfer_id'],'offset':msg['offset']}
            self.enqueue(node,source,ack,'transport_ack',len(json.dumps(ack).encode()),
                         {'transfer_id':msg['transfer_id'],'offset':msg['offset']})
        elif kind=='process_eof':
            if node not in self.stopped:self.failure=f'{node} exited unexpectedly (see worker_{node}.stderr.log)'
        elif kind=='invalid_output':self.failure=f'{node} invalid worker stdout: {msg["line"][:200]}'
        else:raise RuntimeError(f'unknown worker message {kind}')
    def deliver_packet(self,edge,packet):
        source,dest=edge;kind=packet['kind'];now=self.now()
        self.send(dest,packet['message'])
        if kind=='control':
            self.log('control_messages',event='delivered',size_bytes=packet['bytes'],
                     queued_at=packet['queued_at'],transmission_start=packet['started_at'],transmission_end=now,**packet['meta'])
        elif kind=='transfer_begin':
            tr=self.transfers[packet['meta']['transfer_id']];tr['begun']=True
            self.log('control_messages',event='delivered',message_type='transfer_begin',source=source,destination=dest,
                     size_bytes=packet['bytes'],transfer_id=packet['meta']['transfer_id'],
                     transmission_start=packet['started_at'],transmission_end=now)
        elif kind=='transport_ack':
            tr=self.transfers[packet['meta']['transfer_id']];tr['pending']=False
            self.log('control_messages',event='delivered',message_type='transport_ack',source=source,destination=dest,
                     size_bytes=packet['bytes'],transmission_start=packet['started_at'],transmission_end=now,**packet['meta'])
        elif kind=='data':
            tr=self.transfers[packet['meta']['transfer_id']];info=tr['info'];start=tr['offset'];tr['offset']+=packet['bytes']
            self.log('transfers',event='chunk',**info,offset=start,end_offset=tr['offset'],chunk_bytes=packet['bytes'],
                     bytes_sent=tr['offset'],chunk_sha256=packet['meta']['chunk_sha256'],
                     transmission_start=packet['started_at'],transmission_end=now,edge_rate_bytes_s=self.edges[edge]['bandwidth_bytes_s'])
            if tr['offset']==info['size_bytes']:
                tr['complete']=True
                self.log('transfers',event='completed',**info,offset=tr['offset'],bytes_sent=tr['offset'],
                         retransmitted_bytes=0,awaiting_destination_validation=True)
            if self.scenario=='transfer_outage' and not self.triggered_transfer_outage and source=='A' and dest in ('B','C') and .15*info['size_bytes']<=tr['offset']<info['size_bytes']:
                self.triggered_transfer_outage=True;cutoff=self.now();end=cutoff+self.config['transfer_outage_duration_s']
                self.transfer_down={'start':cutoff,'end':end,'edge':edge}
                self.log('node_events',event='data_link_closed',timestamp=cutoff,source=source,destination=dest,start_s=cutoff,end_s=end,
                         transfer_id=info['transfer_id'],offset=tr['offset'],trigger='actual_partial_bytes')
    def progress(self):
        current=time.monotonic();interval_start=self.last_tick-self.t0;dt=current-self.last_tick;self.last_tick=current;now=current-self.t0;self.tick_id+=1
        if self.ground_down and now>=self.ground_down[1] and not getattr(self,'ground_reopened',False):
            self.ground_reopened=True;self.log('node_events',event='ground_link_opened',start_s=self.ground_down[1])
        if self.transfer_down and now>=self.transfer_down['end'] and not getattr(self,'data_reopened',False):
            self.data_reopened=True;self.log('node_events',event='data_link_opened',source=self.transfer_down['edge'][0],destination=self.transfer_down['edge'][1],start_s=self.transfer_down['end'])
        for edge,e in self.edges.items():
            up=self.edge_up(edge,self.now());before_refill=self.tokens[edge]
            self.tokens[edge]=min(2*self.config['chunk_bytes'],self.tokens[edge]+dt*e['bandwidth_bytes_s']) if up else 0
            q=self.edge_queues[edge]
            if q:
                self.log('transfers',event='capacity_budget',timestamp=self.now(),source=edge[0],destination=edge[1],
                         tick_id=self.tick_id,interval_start=interval_start,interval_end=now,elapsed_s=dt,
                         edge_up=up,edge_rate_bytes_s=e['bandwidth_bytes_s'],bucket_before_refill=before_refill,
                         refill_bytes=dt*e['bandwidth_bytes_s'] if up else 0,
                         bucket_after_refill=self.tokens[edge],burst_cap_bytes=2*self.config['chunk_bytes'])
            while up and q and q[0]['earliest']<=now and self.tokens[edge]>0:
                p=q[0]
                if p['started_at'] is None:p['started_at']=now
                # Capacity shared by every file and control on the directed edge.
                amount=min(p['remaining'],int(self.tokens[edge]))
                if amount<=0:break
                before_charge=self.tokens[edge];p['remaining']-=amount;self.tokens[edge]-=amount
                self.log('transfers',event='capacity_charge',timestamp=self.now(),source=edge[0],destination=edge[1],
                         tick_id=self.tick_id,charged_bytes=amount,bucket_before=before_charge,bucket_after=self.tokens[edge],
                         packet_kind=p['kind'],transfer_id=p['meta'].get('transfer_id'),message_id=p['meta'].get('message_id'),
                         edge_rate_bytes_s=e['bandwidth_bytes_s'],edge_up=True)
                if p['remaining']==0:
                    q.popleft();self.deliver_packet(edge,p)
                    up=self.edge_up(edge,self.now())
                    if not up:self.tokens[edge]=0
                else:break
        for tid,tr in self.transfers.items():
            if tr['complete'] or not tr['begun']:continue
            info=tr['info'];up=self.edge_up((info['source'],info['destination']),self.now())
            if not up and not tr['paused']:
                tr['paused']=True;self.log('transfers',event='paused',**info,offset=tr['offset'],bytes_sent=tr['offset'],partial_computable=False)
            if up and tr['paused']:
                tr['paused']=False;self.log('transfers',event='resumed',**info,offset=tr['offset'],resume_offset=tr['offset'],retained_bytes=tr['offset'],retransmitted_bytes=0)
            if up and not tr['pending']:
                tr['pending']=True;self.send(info['source'],{'kind':'pull_chunk','transfer_id':tid,'offset':tr['offset'],
                                                        'length':min(self.config['chunk_bytes'],info['size_bytes']-tr['offset'])})
    def drain(self,timeout=.01):
        try:
            node,msg=self.messages.get(timeout=timeout);self.on_message(node,msg)
            for _ in range(1000):
                try:node,msg=self.messages.get_nowait();self.on_message(node,msg)
                except queue.Empty:break
        except queue.Empty:pass
    def shutdown(self):
        for n,p in self.procs.items():
            if p.poll() is None:self.send(n,{'kind':'stop'})
        deadline=time.monotonic()+5
        while len(self.stopped)<len(self.procs) and time.monotonic()<deadline:self.drain(.02)
        for n,p in self.procs.items():
            try:p.wait(timeout=2)
            except subprocess.TimeoutExpired:p.terminate();p.wait(timeout=2)
            if n in self.process_records:self.process_records[n]['exit_code']=p.returncode
        for f in self.stderr.values():f.close()
        for f in self.handles.values():f.close()
    def execute(self,check_only=False):
        try:
            self.launch()
            while self.now()<self.config['timeout_s']:
                self.drain();self.progress()
                if self.failure:break
                if check_only and len(self.started)==4 and self.now()>.7:break
                if self.delivered:break
            if not check_only and not self.delivered and not self.failure:self.failure='runtime deadline exceeded'
        except Exception as exc:
            self.failure=f'{type(exc).__name__}: {exc}'
            self.log('node_events',event='runtime_failed',error=self.failure)
        finally:self.shutdown()
        elapsed=self.now();snapshot={'timestamp':elapsed,'nodes':[self.final_observations[n] for n in sorted(self.final_observations)],
            'assets':[a for o in self.final_observations.values() for a in o.get('assets',[])],
            'edges':copy.deepcopy(self.config['known_contact_plan']),'source_kind':'actual_worker_observations','source_run_id':self.run_id,
            'feedback_event_ids':[o['source_event_id'] for o in self.final_observations.values()],
            'clock_mode':'monotonic_wall_clock_1x'}
        status='failed' if self.failure else ('checked' if check_only else ('delivered' if self.delivered else 'partial'))
        result={'run_id':self.run_id,'status':status,'scenario':self.scenario,'error':self.failure,'elapsed_wall_s':elapsed,
            'processes':self.process_records,'observed_snapshot':snapshot,'isolation_evidence':str(self.run_dir/'isolation.json'),
            'transfer_count':len(self.transfers),'transferred_payload_bytes':sum(t['offset'] for t in self.transfers.values()),
            'contact_source':'synthetic_contact_trace','clock_mode':'monotonic_wall_clock_1x',
            'outputs_dir':str(self.run_dir/'nodes'/'G'/self.delivered['outputs_dir'].removeprefix('/node/')) if self.delivered else None,
            'delivery':self.delivered,'known_contact_plan':self.config['known_contact_plan'],
            'config_sha256':hashlib.sha256(json.dumps(self.config,sort_keys=True).encode()).hexdigest()}
        (self.run_dir/'runtime_result.json').write_text(json.dumps(result,indent=2,sort_keys=True))
        (self.run_dir/'node_config.json').write_text(json.dumps(self.config,indent=2,sort_keys=True))
        return result


def run_distributed(plan:dict,case_dir:Path,run_dir:Path,scenario:str='normal',config:dict|None=None,forecast_graph:dict|None=None)->dict:
    if scenario not in ('normal','busy_b','ground_outage','transfer_outage'):raise ValueError(f'unknown scenario {scenario}')
    if any('assigned_node' in s for s in plan['steps']):raise ValueError('PlanDAG must not preassign execution nodes')
    cfg=copy.deepcopy(config or default_config())
    if forecast_graph is None:
        persisted=Path(run_dir)/'forecast.json'
        if not persisted.is_file():raise ValueError('Pass forecast_graph explicitly or persist the actual graph in run_dir/forecast.json')
        forecast_graph=json.loads(persisted.read_text())
    if forecast_graph.get('forecast_id')!=plan.get('forecast_id'):
        raise ValueError('Execution forecast identity must match the submitted plan')
    return Broker(run_dir,cfg,scenario,plan,Path(case_dir),copy.deepcopy(forecast_graph)).execute()


def check_nodes(work_dir:Path,config:dict|None=None)->dict:
    """Start all four real isolated processes, exchange gated ads, then stop cleanly."""
    return Broker(work_dir,copy.deepcopy(config or default_config()),'normal').execute(check_only=True)


def get_job(run_dir:Path,job_id:str)->dict:
    entries=[json.loads(line) for line in (Path(run_dir)/'tool_calls.jsonl').read_text().splitlines()]
    matched=[e for e in entries if e.get('job_id')==job_id]
    if not matched:raise KeyError(job_id)
    return matched[-1]


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');parser.add_argument('--work-dir',type=Path,required=True)
    args=parser.parse_args()
    if not args.check:parser.error('use --check or the project task CLI')
    result=check_nodes(args.work_dir);print(json.dumps(result,indent=2));raise SystemExit(0 if result['status']=='checked' else 1)
