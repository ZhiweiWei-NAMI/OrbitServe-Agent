"""Transparent event model. These are counterfactual timings, never host timings.

Measured artifact byte counts + declared pixel work feed an uncalibrated model.
It models dependency transfers, a single queue per node, changing contacts and
local memory constraints. It is not a learned WM, orbital simulator or ns-3.
"""
from __future__ import annotations
from copy import deepcopy
import math

WORK={'load_bands':4,'index':2,'select_band':.2,'threshold':.5,'difference':1,
      'water_change':1,'area_summary':.5,'raster_summary':1,'polygonize':8,
      'rgb':2,'overlay':1,'tvdi':20,'mosaic_raster':.5,'mosaic_mask':.5,'mosaic_change':.5}


def environment(scenario='normal'):
 if scenario not in ('normal','future_contact','busy_local'): raise ValueError('Unknown modeled scenario')
 parameters={'model_version':'explicit-event-model-2-current-busy-observation','horizon_s':600.0,
  'network_bytes_s':20_000_000.0,'latency_s':.005,'operator_startup_s':.01,
  'pixel_work_units':deepcopy(WORK),'node_rates_work_s':{'A':500_000.0,'C':4_000_000.0},
  'memory_bytes':{'A':512*1024*1024,'C':512*1024*1024},
  'memory_estimate':'8 MiB base + 8 bytes * pixels * 12; conservative common bound, not measured RSS',
  'completion_boundary':'required_products_available_at_A_no_separate_ground_downlink',
  'calibration':'Illustrative explicit parameters, not calibrated satellite hardware. Bytes measured from real artifacts; modeled seconds never added to actual host wall time.'}
 current={'timestamp_s':0,'nodes':[{'id':n,'queue_until_s':0,'memory_bytes':parameters['memory_bytes'][n],
       'work_units_s':parameters['node_rates_work_s'][n]} for n in ('A','C')],
       'links':[{'source':a,'destination':b,'available':True,'bytes_s':parameters['network_bytes_s']} for a,b in (('A','C'),('C','A'))],
       'source_holder':'A','ground_receives_from':'A'}
 contacts=[[0,600]] if scenario!='future_contact' else [[0,.025],[20,600]]
 queues={'A':0.,'C':0.}
 future_reservations=[{'node':'A','start_s':.2,'end_s':15.}] if scenario=='busy_local' else []
 forecast={'issued_at_s':0,'source':'declared_mock_contact_resource_forecast','contact_windows':contacts,
           'resource_reservations':future_reservations,'queue_until_s':queues,'perfect_forecast_for_control':True}
 return {'scenario':scenario,'current_observation':current,'parameters':parameters,'forecast':forecast,
         'truth':deepcopy(forecast),'deadline_s':60.0}


def _transfer(start,bytes,windows,p):
 remaining=float(bytes); cursor=float(start); first=None
 if remaining==0: return cursor,0.
 for left,right in windows:
  begin=max(cursor,left)
  if begin>=right: continue
  if first is None: first=begin
  amount=min(remaining,(right-begin)*p['network_bytes_s']); remaining-=amount
  cursor=begin+amount/p['network_bytes_s']
  if remaining<=1e-6: return cursor+p['latency_s'],cursor-start+p['latency_s']
 return math.inf,math.inf


def _reserve(start,duration,node,reservations):
 for r in reservations:
  if r['node']==node and start<r['end_s'] and start+duration>r['start_s']:
   start=r['end_s']
 return start


def simulate(plan,steps,arm,scenario='normal',*,forecast_enabled=None,offload_enabled=None,deadline_s=None,environment_override=None):
 env=deepcopy(environment_override) if environment_override is not None else environment(scenario); p=env['parameters']; truth=env['truth']
 forecast=('wm_offload' in arm) if forecast_enabled is None else forecast_enabled
 offload=('wm_offload' in arm or 'dynamic_no_wm' in arm) if offload_enabled is None else offload_enabled
 deadline=env['deadline_s'] if deadline_s is None else float(deadline_s)
 if plan['status']=='needs_data': return {'status':'NOT_APPLICABLE','completion_s':None,'deadline_met':None,'transferred_bytes':0,'schedule':[],
   'parameters':p,'boundary':'discrete_event_simulation_not_measured_hardware'}
 nodes={n['id']:n for n in plan['nodes']}; completed={}; queue={'A':0.,'C':0.}; schedule=[]; transferred=0
 for row in steps:
  node=nodes[row['id']]; candidates=[]
  ready=max([completed[r]['finish_s'] for r in row['dependencies']]+[float(node.get('not_before_s',0))])
  permitted=set(node.get('allowed_nodes',['A','C']))
  dependency_ready=ready; observations=[]
  while True:
   candidates=[]
   for target in (n for n in (['A','C'] if offload else ['A']) if n in permitted):
    memory=8*1024*1024+row['pixels']*8*12
    if memory>p['memory_bytes'][target]:
     candidates.append({'node':target,'feasible':False,'reason':'modeled_memory'}); continue
    start=ready; transfer_bytes=0
    # No-WM control extrapolates current link availability, without future truth.
    current_open=any(a<=ready<b for a,b in truth['contact_windows'])
    visible_windows=truth['contact_windows'] if forecast else ([[ready,p['horizon_s']]] if current_open else [])
    transfers=[]
    if row['source_bytes_read'] and target!='A': transfers.append(('A',row['source_bytes_read']))
    for ref in row['dependencies']:
     if completed[ref]['node']!=target: transfers.append((completed[ref]['node'],completed[ref]['output_bytes']))
    for _,bytes in transfers:
     start,_=_transfer(start,bytes,visible_windows,p); transfer_bytes+=bytes
    duration=row['pixels']*WORK[row['op']]/p['node_rates_work_s'][target]+p['operator_startup_s']
    visible_reservations=(truth['resource_reservations'] if forecast else
      [r for r in truth['resource_reservations'] if r['start_s']<=ready<r['end_s']])
    start=max(start,queue[target]); start=_reserve(start,duration,target,visible_reservations)
    finish=start+duration
    # Include returning a requested product to A in decision cost.
    delivered_finish=finish
    if row['id'] in plan['outputs'].values() and target!='A': delivered_finish,_=_transfer(finish,row['output_bytes'],visible_windows,p)
    candidates.append({'node':target,'feasible':math.isfinite(delivered_finish),'estimated_finish_s':delivered_finish if math.isfinite(delivered_finish) else None,
                       'estimated_memory_bytes':memory,'compute_s':duration,'transfers':transfers,'transfer_bytes':transfer_bytes})
   feasible=[c for c in candidates if c['feasible']]
   if feasible: break
   # Environment, not the no-WM decision function, wakes a blocked job when
   # a contact/resource event actually becomes observable. Future windows are
   # never exposed before that wake. This is bounded by the model horizon.
   observable_events=[a for a,b in truth['contact_windows'] if ready<a<p['horizon_s']]
   observable_events += [r['end_s'] for r in truth['resource_reservations'] if ready<r['end_s']<p['horizon_s']]
   retryable=bool(candidates) and any(c.get('reason')!='modeled_memory' for c in candidates)
   if not forecast and retryable and observable_events:
    wake=min(observable_events)
    observations.append({'wait_started_s':ready,'observed_event_s':wake,
      'reason':'No feasible currently observed contact; environment woke job on actual event, then current state was reevaluated.',
      'future_window_disclosed_to_policy':False})
    ready=wake
    continue
   return {'status':'INFEASIBLE' if forecast or not retryable else 'BLOCKED_THROUGH_HORIZON',
    'completion_s':None,'deadline_met':False,'transferred_bytes':transferred,'schedule':schedule,
    'blocked_step':row['id'],'candidates':candidates,'observation_waits':observations,'parameters':p,'scenario':scenario,
    'boundary':'discrete_event_simulation_not_measured_hardware'}
  chosen=min(feasible,key=lambda c:(c['estimated_finish_s'],c['node'])); target=chosen['node']; actual=ready
  for _,bytes in chosen['transfers']:
   actual,_=_transfer(actual,bytes,truth['contact_windows'],p); transferred+=bytes
  actual=max(actual,queue[target]); actual=_reserve(actual,chosen['compute_s'],target,truth['resource_reservations'])
  finish=actual+chosen['compute_s']; queue[target]=finish
  completed[row['id']]={'node':target,'finish_s':finish,'output_bytes':row['output_bytes']}
  schedule.append({'id':row['id'],'op':row['op'],'node':target,'ready_s':dependency_ready,'decision_observation_s':ready,'observation_waits':observations,'start_s':actual,'finish_s':finish,
                   'candidate_costs':candidates,'actual_input_transfer_bytes':chosen['transfer_bytes'],
                   'forecast_visible':forecast,'compute_work_basis':'pixels times declared operator coefficient, not actual wall speed'})
 delivered=[]
 for ref in dict.fromkeys(plan['outputs'].values()):
  c=completed[ref]; finish=c['finish_s']
  if c['node']!='A': finish,_=_transfer(finish,c['output_bytes'],truth['contact_windows'],p); transferred+=c['output_bytes']
  delivered.append(finish)
 completion=max(delivered,default=0.)
 return {'status':'COMPLETE' if math.isfinite(completion) else 'INFEASIBLE','completion_s':completion if math.isfinite(completion) else None,
         'deadline_met':completion<=deadline,'deadline_s':deadline,'transferred_bytes':transferred,'schedule':schedule,
         'scenario':scenario,'forecast_enabled':forecast,'offload_enabled':offload,'parameters':p,
         'truth_sha256':__import__('hashlib').sha256(__import__('json').dumps(truth,sort_keys=True).encode()).hexdigest(),
         'boundary':'discrete_event_simulation_not_measured_hardware',
         'limitations':['Transfer serialization/link contention and packet loss are not modeled globally.',
                       'Perfect declared forecasts are a control, not learned prediction accuracy.',
                       'Offline counterfactual replay uses completed-artifact sizes shared by all policies; online size prediction is not evaluated.',
                       'No automatic speedup is implied; strong recipes may tie agent DAGs.']}
