"""Goal-conditioned agent request/response interface and typed DAG execution.

Agent plans are never converted to a case-specific canned workflow. The recipe
builder is used ONLY by the declared non-agent baseline and fixture generator.
"""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from .semantic_ops import CATALOG, execute_operator, write_json

SCHEMA_VERSION=1
ROOT=Path(__file__).resolve().parents[2]
SYSTEM_PROMPT='''You are an Earth-observation task planner. Infer the required products and their dependencies from the user's instruction. Produce ONE JSON object matching response_schema. Construct your own typed DAG using the operator catalog; nodes become executable pending tasks. Do not return prose, Python, tool calls, or a case ID. There are no hidden tools and no prewritten workflow selected from your answer.
Use only available_assets. Each input reference is the ID of a node whose declared output type matches that input port. Dependencies follow these references. A node may be listed before or after its dependencies; the executor topologically sorts it. Invent meaningful unique node IDs. Reuse intermediate artifacts where appropriate. Source data may be opened only by load_bands; all other operators consume their declared artifact inputs. Output references point at actual nodes. Never invent measurements, acquisition times, missing bands, QA, or resolution. If essential data are unavailable, return status needs_data, nodes=[], outputs={}, and explicit missing_assets. A fallback index is allowed only if the user's instruction authorizes it and your rationale identifies the changed method.
Preserve the requested scientific definitions, thresholds, phase/date support, validity rules, full-AOI TVDI fitting and product requirements. Different requested products may require different operators and graphs. Splitting is optional; if splitting, supply explicit legal windows and mosaic before any full-grid product. Do not sacrifice scientific quality to meet a deadline. Optional predicted_world_state contains modeled forecasts, not certain future truth. Current observations and tool cost profiles may guide decomposition; do not claim optimized results without evidence. Return a short rationale explaining the decomposition, data limitations, and any scheduling constraints. Hardware/contact estimates do not change the pixels or requested quality.'''


def read(path): return json.loads(Path(path).read_text())
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def load_case(case_id, cases_path=None):
 cases=read(cases_path or ROOT/'configs/semantic_cases.json')['cases']
 matches=[c for c in cases if c['id']==case_id]
 if len(matches)!=1: raise ValueError(f'Expected unique semantic case {case_id}')
 return matches[0]


def source_context(case,data_root):
 root=Path(data_root).resolve(); path=(root/case['input_case']).resolve()
 if not path.is_relative_to(root): raise ValueError('Input case escapes data root')
 task=read(path/'task.json'); assets=read(path/'assets.json')['assets']
 available=[a for a in assets if a['band'] not in case.get('withheld_bands',[])]
 return path,task,available


def response_schema():
 # A portable JSON Schema; runtime additionally validates type ports, grids,
 # availability, parameter bounds, cycles and requested output roles.
 return {'type':'object','additionalProperties':False,
  'required':['schema_version','status','nodes','outputs','rationale'],
  'properties':{'schema_version':{'const':1},'status':{'enum':['ready','needs_data']},
   'rationale':{'type':'string','maxLength':8000},
   'missing_assets':{'type':'array','items':{'type':'string'}},
   'nodes':{'type':'array','maxItems':256,'items':{'type':'object','additionalProperties':False,
    'required':['id','op','inputs','params'],'properties':{'id':{'type':'string','pattern':'^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
     'op':{'enum':list(CATALOG)},'inputs':{'type':'object','additionalProperties':{'oneOf':[
      {'type':'string'},{'type':'array','minItems':1,'items':{'type':'string'}}]}},
     'params':{'type':'object'},'allowed_nodes':{'type':'array','minItems':1,'items':{'enum':['A','C']}},
     'not_before_s':{'type':'number','minimum':0}}}},
   'outputs':{'type':'object','additionalProperties':{'type':'string'}}}}


def request(case,data_root,arm='agent_only',scenario='normal'):
 from .semantic_model import environment
 _,task,assets=source_context(case,data_root); env=environment(scenario)
 parameters=deepcopy(case.get('parameters',{}))
 if case.get('allow_fallback'):
  parameters.pop('water_method',None); parameters.pop('fallback_reason',None)
  parameters['preferred_method']='MNDWI'; parameters['allowed_methods']=['MNDWI','NDWI']
 desired_outputs=case['required_outputs'] or (['summary'] if case['goal']=='missing_mndwi' else [])
 context={'instruction':case['instruction'],'required_outputs':desired_outputs,
          'output_condition':'When feasible, deliver these roles; needs_data must instead have empty outputs and explicit missing_assets.',
          'scientific_parameters':parameters,
          'execution_policy':{'allowed_execution_nodes':['A','C'] if 'offload' in arm else ['A'],
             'boundary':'Actual science is serial host execution. Node assignment constraints apply to the explicit event model.'},
          'grid':{k:task[k] for k in ('crs','transform','height','width')},
          'data_caveats':[task[k] for k in ('caveat','erratum') if k in task],
          'available_assets':[{k:a[k] for k in ('asset_id','band','phase','bytes','scale','offset','sha256','observation_time') if k in a} for a in assets],
          'operator_catalog':deepcopy(CATALOG),'response_schema':response_schema(),
          'current_observation':env['current_observation'],
          'resource_model':env['parameters'],'deadline_s':env['deadline_s']}
 if arm in ('agent_wm_offload','external_agent_wm_offload','fixture_agent_wm_offload'):
  context['predicted_world_state']=env['forecast']
 return {'schema_version':1,'case_id':case['id'],'arm':arm,'scenario':scenario,
         'messages':[{'role':'system','content':SYSTEM_PROMPT},
                     {'role':'user','content':json.dumps(context,ensure_ascii=False,indent=2)}],
         'response_schema':response_schema(),
         'boundary':'No expected DAG, reference answers, recipes, quality oracle or future truth is included in messages.'}


def recipe(case):
 """Declared strong deterministic baseline, NOT an agent implementation.

 Composes required product roles. The same helper creates explicitly labelled
 replay fixtures to exercise the adapter, without pretending to infer a DAG.
 """
 nodes=[]; outputs={}; p=case.get('parameters',{}); roles=set(case['required_outputs']); goal=case['goal']
 def node(id,op,inputs=None,**params):
  nodes.append({'id':id,'op':op,'inputs':inputs or {},'params':params}); return id
 if goal=='missing_mndwi':
  return {'schema_version':1,'status':'needs_data','nodes':[],'outputs':{},
          'rationale':'Strict MNDWI needs withheld B11; no unapproved fallback. No future acquisition is fabricated.',
          'missing_assets':['before:B11','after:B11']}
 if goal in ('water_area','water_map','water_area_ndwi'):
  second='B08' if goal=='water_area_ndwi' else 'B11'; masks={}
  for phase in ('before','after'):
   b=node(phase+'_spectra','load_bands',phase=phase,bands=['B03',second],scl=True)
   i=node(phase+'_index','index',{'bands':b},x='B03',y=second)
   masks[phase]=node(phase+'_water','threshold',{'raster':i},value=p.get('threshold',0.0),comparison='>')
  change=node('changed','water_change',masks)
  if 'summary' in roles: outputs['summary']=node('areas','area_summary',{'change':change},**({'method_note':p.get('fallback_reason','Authorized NDWI fallback because B11 is unavailable.')} if goal=='water_area_ndwi' else {}))
  if 'polygons' in roles: outputs['polygons']=node('boundaries','polygonize',{'change':change})
 if goal=='ndvi_decline':
  ndvi={}
  for phase in ('before','after'):
   b=node(phase+'_spectra','load_bands',phase=phase,bands=['B08','B04'],scl=True)
   ndvi[phase]=node(phase+'_ndvi','index',{'bands':b},x='B08',y='B04')
  delta=node('delta','difference',ndvi)
  if 'delta' in roles: outputs['delta']=delta
  if 'summary' in roles:
   outputs['summary']=node('decline_summary','raster_summary',{'raster':delta},value=p.get('decline_threshold',-0.15),comparison='<=')
 if goal=='tvdi':
  b=node('products','load_bands',bands=['NDVI','LST'],scl=False)
  ndvi=node('ndvi','select_band',{'bands':b},band='NDVI')
  lst=node('lst','select_band',{'bands':b},band='LST')
  value=node('dryness','tvdi',{'ndvi':ndvi,'lst':lst},n_bins=p.get('n_bins',100))
  if 'tvdi' in roles: outputs['tvdi']=value
  if 'summary' in roles: outputs['summary']=node('dryness_summary','raster_summary',{'raster':value},value=p.get('threshold',.75),comparison='>')
 if 'rgb' in roles:
  b=node('display_spectra','load_bands',phase='after',bands=['B04','B03','B02'],scl=False)
  outputs['rgb']=node('display','rgb',{'bands':b},**({'max_side':p['max_side']} if 'max_side' in p else {}))
 return {'schema_version':1,'status':'ready','nodes':nodes,'outputs':outputs,
         'rationale':'Deterministic compositional product recipe. '+('Authorized NDWI fallback, not MNDWI.' if goal=='water_area_ndwi' else '')}


def _number(value,field,minimum=None,maximum=None,integer=False):
 if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value): raise ValueError(f'{field} must be finite number')
 if minimum is not None and value<minimum or maximum is not None and value>maximum: raise ValueError(f'{field} out of range')
 if integer and int(value)!=value: raise ValueError(f'{field} must be integer')


def validate(plan,case,task,assets):
 allowed={'schema_version','status','nodes','outputs','rationale','missing_assets'}
 if not isinstance(plan,dict) or set(plan)-allowed: raise ValueError('Unknown plan fields')
 if plan.get('schema_version')!=1 or plan.get('status') not in ('ready','needs_data'): raise ValueError('Invalid response schema/status')
 if not isinstance(plan.get('rationale'),str) or len(plan['rationale'])>8000: raise ValueError('Missing/oversized rationale')
 if not isinstance(plan.get('nodes'),list) or len(plan['nodes'])>256 or not isinstance(plan.get('outputs'),dict): raise ValueError('Invalid nodes/outputs')
 if plan['status']=='needs_data':
  if plan['nodes'] or plan['outputs'] or not plan.get('missing_assets'): raise ValueError('needs_data requires missing_assets and empty graph')
  if not all(isinstance(s,str) for s in plan['missing_assets']): raise ValueError('Invalid missing asset list')
  return []
 if not plan['nodes']: raise ValueError('Ready graph must have nodes')
 byid={}; types={}; deps={}
 for n in plan['nodes']:
  if not isinstance(n,dict) or set(n)-{'id','op','inputs','params','allowed_nodes','not_before_s'}: raise ValueError('Unknown node fields')
  id=n.get('id','')
  if not isinstance(id,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',id) or id in byid: raise ValueError('Invalid/duplicate node ID')
  op=n.get('op')
  if op not in CATALOG: raise ValueError(f'Unknown operator {op}')
  if not isinstance(n.get('params'),dict) or not isinstance(n.get('inputs'),dict): raise ValueError('Params/inputs must be objects')
  cat=CATALOG[op]; p=n['params']
  if set(p)-set(cat['params']): raise ValueError(f'Unknown parameters for {op}')
  if set(n['inputs'])!=set(cat['inputs']): raise ValueError(f'Incorrect input ports for {op}')
  if 'allowed_nodes' in n and (not isinstance(n['allowed_nodes'],list) or not n['allowed_nodes'] or set(n['allowed_nodes'])-{'A','C'}): raise ValueError('Invalid allowed_nodes')
  _number(n.get('not_before_s',0),'not_before_s',minimum=0,maximum=3600)
  if op=='load_bands':
   bands=p.get('bands')
   if not isinstance(bands,list) or not bands or len(bands)!=len(set(bands)) or not all(isinstance(x,str) for x in bands): raise ValueError('Invalid bands')
   if not isinstance(p.get('scl',False),bool): raise ValueError('scl must be boolean')
   for b in bands+(['SCL'] if p.get('scl',False) else []):
    if sum(a['band']==b and (p.get('phase') is None or a.get('phase')==p['phase']) for a in assets)!=1: raise ValueError(f'Missing/ambiguous source asset {p.get("phase")}:{b}')
   from .science import _window
   _window(task,p.get('window'))
  if op in ('threshold','raster_summary'):
   _number(p.get('value'),'value')
   if p.get('comparison') not in ('>','>=','<','<='): raise ValueError('Invalid comparison')
  if op in ('rgb','overlay'): _number(p.get('max_side',1024),'max_side',1,4096,True)
  if op=='tvdi': _number(p.get('n_bins',100),'n_bins',2,1000,True)
  if op=='index' and not all(isinstance(p.get(k),str) for k in ('x','y')): raise ValueError('index needs x/y bands')
  if op=='select_band' and not isinstance(p.get('band'),str): raise ValueError('select_band needs band')
  byid[id]=n; types[id]=cat['output']
 for id,n in byid.items():
  deps[id]=set()
  for port,expected in CATALOG[n['op']]['inputs'].items():
   v=n['inputs'][port]; plural=expected.endswith('[]'); kind=expected.removesuffix('[]')
   if plural:
    if not isinstance(v,list) or not v or len(v)!=len(set(v)): raise ValueError('Expected unique nonempty list of refs')
    refs=v
   else:
    if not isinstance(v,str): raise ValueError('Expected node reference')
    refs=[v]
   for ref in refs:
    if ref not in byid: raise ValueError(f'Unknown artifact dependency {ref}')
    if types[ref]!=kind: raise ValueError(f'Type mismatch: {port} requires {kind}, got {types[ref]}')
    deps[id].add(ref)
  if n['op'] in ('index','select_band','rgb'):
   upstream=byid[n['inputs']['bands']]
   available=upstream['params'].get('bands',[])
   needed=[n['params'][k] for k in ('x','y')] if n['op']=='index' else ([n['params']['band']] if n['op']=='select_band' else ['B04','B03','B02'])
   if not set(needed)<=set(available): raise ValueError('Required band absent from declared upstream artifact')
 output_types={'summary':{'summary'},'polygons':{'geojson'},'rgb':{'rgb','png'},'delta':{'raster'},'tvdi':{'raster'}}
 if set(plan['outputs'])!=set(case['required_outputs']): raise ValueError('Missing or unrequested output roles')
 for role,ref in plan['outputs'].items():
  if ref not in types or types[ref] not in output_types.get(role,set()): raise ValueError('Output role type mismatch')
 reachable=set(plan['outputs'].values()); frontier=list(reachable)
 while frontier:
  current=frontier.pop()
  for dep in deps[current]-reachable:
   reachable.add(dep); frontier.append(dep)
 if reachable!=set(byid): raise ValueError('Unrequested orphan nodes do not contribute to any output')
 pending=set(byid); order=[]
 while pending:
  ready=sorted(id for id in pending if deps[id]<=set(order))
  if not ready: raise ValueError('Cyclic graph')
  order.extend(ready); pending-=set(ready)
 return [byid[id] for id in order]


def execute(plan,case,data_root,run_dir,arm,scenario='normal',planning=None):
 from .semantic_quality import verify_quality
 from .semantic_model import simulate
 run=Path(run_dir)
 if run.exists() and any(run.iterdir()): raise ValueError('Run directory must be empty (no overwrite)')
 run.mkdir(parents=True,exist_ok=True)
 source,task,assets=source_context(case,data_root)
 write_json(run/'plan.json',plan); write_json(run/'case.json',case)
 mode=('non_agent_recipe' if arm=='non_agent' else 'fixture_replay' if arm.startswith('fixture_') else 'external_import')
 if planning and planning.get('mode')=='real_api': mode='real_api'
 result={'schema_version':1,'case_id':case['id'],'arm':arm,'mode':mode,'status':'FAIL',
         'semantic_status':plan.get('status'),'plan_sha256':digest(plan),'planning':planning or {'wall_s':None,'usage':None,'cost':None},
         'node_count':len(plan.get('nodes',[])),'operator_counts':dict(Counter(n.get('op') for n in plan.get('nodes',[]))),'outputs':{}}
 start=time.perf_counter(); artifacts={}; steps=[]
 try:
  order=validate(plan,case,task,assets)
  for node in order:
   inputs={k:([artifacts[r] for r in v] if isinstance(v,list) else artifacts[v]) for k,v in node['inputs'].items()}
   step_start=time.perf_counter()
   artifact=execute_operator(node['op'],node['params'],inputs,source,task,assets,run/'artifacts'/node['id'])
   artifacts[node['id']]=artifact
   flat=[a for v in inputs.values() for a in (v if isinstance(v,list) else [v])]
   source_bytes=sum(a['bytes'] for a in artifact['source_reads'])
   steps.append({'id':node['id'],'op':node['op'],'wall_s':time.perf_counter()-step_start,
                 'input_bytes':sum(a['bytes'] for a in flat)+source_bytes,'source_bytes_read':source_bytes,
                 'output_bytes':artifact['bytes'],'pixels':artifact['window'][2]*artifact['window'][3],
                 'dependencies':[a for v in node['inputs'].values() for a in (v if isinstance(v,list) else [v])],
                 'input_hashes':{k:([a['hashes'] for a in v] if isinstance(v,list) else v['hashes']) for k,v in inputs.items()},
                 'output_hashes':artifact['hashes']})
   write_json(run/'execution_steps.json',steps)
  result['outputs']={role:artifacts[id] for role,id in plan['outputs'].items()}
  result['quality']=verify_quality(case,source,task,assets,result['outputs'],plan)
  result['status']=('NEEDS_DATA' if plan['status']=='needs_data' else 'PASS') if result['quality']['status']=='PASS' else 'FAIL'
  result['modeled']=simulate(plan,steps,arm,scenario)
 except Exception as e:
  result['error']=f'{type(e).__name__}: {e}'
  result.setdefault('quality',{'status':'FAIL','checks':[{'check':'execution_and_validation','status':'FAIL','detail':str(e)}]})
  result.setdefault('modeled',{'status':'NOT_EXECUTED','boundary':'discrete_event_simulation_not_measured_hardware'})
 result['execution']={'wall_s':time.perf_counter()-start,'tool_wall_s':sum(s['wall_s'] for s in steps),
  'source_bytes_read':sum(s['source_bytes_read'] for s in steps),'intermediate_bytes':sum(s['output_bytes'] for s in steps),
  'delivered_bytes':sum(a['bytes'] for a in result['outputs'].values()),'steps':steps,
  'source_bytes_read_definition':'Sum of full declared source-file sizes per load (including repeats/windows), not OS I/O or decoded bytes.',
  'artifact_bytes_definition':'Exact on-disk data-file bytes, excluding artifact.json metadata; same convention for model and delivered_bytes.',
  'wall_s_definition':'End-to-end harness time including validation, operator execution, scientific quality oracle and discrete-event evaluation; excludes external API planning.',
  'tool_wall_s_definition':'Sum of actual scientific operator elapsed times, including artifact reads/writes.',
  'boundary':'Actual serial scientific computation on one host; no satellite or distributed speedup claimed.'}
 write_json(run/'result.json',result)
 return result


def call_openai_compatible(export,endpoint,model,key_env='ORBITSERVE_API_KEY',timeout=180):
 """Opt-in local request. No credentials or HTTP headers are persisted."""
 from urllib.parse import urlparse
 import requests
 parsed=urlparse(endpoint)
 if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1')):
  raise ValueError('API endpoint requires HTTPS, except explicit localhost endpoints')
 key=os.environ.get(key_env)
 if not key: raise ValueError(f'Missing environment variable {key_env}; no automatic mock fallback')
 if not model: raise ValueError('Explicit model is required')
 payload={'model':model,'messages':export['messages'],'response_format':{'type':'json_object'},'temperature':0}
 start=time.perf_counter()
 response=requests.post(endpoint,json=payload,headers={'Authorization':'Bearer '+key},timeout=timeout,allow_redirects=False)
 if response.status_code!=200: raise RuntimeError(f'Agent API HTTP {response.status_code}; response body suppressed to avoid secret echo')
 raw=response.json(); text=raw['choices'][0]['message']['content']
 if not isinstance(text,str): raise ValueError('Expected text JSON assistant message')
 plan=json.loads(text)
 return plan,{'mode':'real_api','wall_s':time.perf_counter()-start,'usage':raw.get('usage'),'cost':None,
              'model':model,'endpoint_host':parsed.hostname,'response_id':raw.get('id'),
              'cost_note':'No price assumed. Apply provider pricing separately to recorded token usage.'}
