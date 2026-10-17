"""Small real-pixel operators for the semantic DAG harness (separate from V0).

Only load_bands opens source imagery. All other operators consume hash-verified
artifacts passed through explicit DAG edges. No operator knows a benchmark ID.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import numpy as np
import rasterio
from PIL import Image
from . import science

VERSION = 'semantic-operators-1.0'

# Input names and types are the executable interface, not suggested DAGs.
CATALOG = {
 'load_bands': {'inputs': {}, 'output': 'bands', 'params': ['bands', 'phase', 'scl', 'window'],
  'description': 'Load only named source bands, apply explicit scale once; optional SCL [4,5,6,7]. Window=[row,col,height,width]; omitted means full grid.'},
 'select_band': {'inputs': {'bands':'bands'}, 'output':'raster', 'params':['band'],
  'description':'Extract one decoded band, retaining declared validity.'},
 'index': {'inputs': {'bands':'bands'}, 'output':'raster', 'params':['x','y'],
  'description':'(x-y)/(x+y), finite nonzero denominator and bundle validity. E.g. B03/B11 MNDWI, B08/B04 NDVI, B03/B08 NDWI.'},
 'threshold': {'inputs': {'raster':'raster'}, 'output':'mask', 'params':['value','comparison'],
  'description':'Comparison >, >=, < or <=; unknown remains 255, true 1, false 0.'},
 'difference': {'inputs': {'before':'raster','after':'raster'}, 'output':'raster', 'params':[],
  'description':'after-before on intersection of valid pixels; requires identical grid and window.'},
 'water_change': {'inputs': {'before':'mask','after':'mask'}, 'output':'change', 'params':[],
  'description':'Common-valid change: 0 unknown, 1 stable nonwater, 2 stable water, 3 gained, 4 lost.'},
 'area_summary': {'inputs': {'change':'change'}, 'output':'summary', 'params':['method_note'],
  'description':'Count classes and projected-grid areas; common-valid footprint.'},
 'raster_summary': {'inputs': {'raster':'raster'}, 'output':'summary', 'params':['value','comparison'],
  'description':'Valid mean and threshold count/percentage/map-grid area; unknown excluded.'},
 'polygonize': {'inputs': {'change':'change'}, 'output':'geojson', 'params':[],
  'description':'Exact 4-connected gained/lost polygons, RFC7946; raster area in properties.'},
 'rgb': {'inputs': {'bands':'bands'}, 'output':'rgb', 'params':['max_side'],
  'description':'Requires B04/B03/B02, fixed [0,0.30] stretch, RGBA TIFF and nearest-neighbor PNG (long side max_side, default original).'},
 'overlay': {'inputs': {'rgb':'rgb','change':'change'}, 'output':'png', 'params':['max_side'],
  'description':'Gained green/lost orange-red at opacity .5 over after RGB; common unknown gray.'},
 'mosaic_raster': {'inputs': {'parts':'raster[]'}, 'output':'raster', 'params':[],
  'description':'Exact nonoverlapping full-grid union; no holes. Enables arbitrary tiled graphs.'},
 'mosaic_mask': {'inputs': {'parts':'mask[]'}, 'output':'mask', 'params':[],
  'description':'Exact nonoverlapping full-grid union; no holes.'},
 'mosaic_change': {'inputs': {'parts':'change[]'}, 'output':'change', 'params':[],
  'description':'Exact nonoverlapping full-grid union; no holes; polygonize AFTER mosaic for boundary correctness.'},
 'tvdi': {'inputs': {'ndvi':'raster','lst':'raster'}, 'output':'raster', 'params':['n_bins'],
  'description':'Global full-AOI NDVI/LST fit, bin extrema and unweighted OLS, clip [0,1]; no per-tile fitting.'},
}


# Concrete parameter schemas travel with the tool catalog to real planners.
_STRING = {'type':'string','minLength':1}
_PARAMETER_FIELDS = {
 'bands':{'type':'array','minItems':1,'uniqueItems':True,'items':_STRING},
 'phase':{'enum':['before','after']},'scl':{'type':'boolean'},
 'window':{'type':'array','minItems':4,'maxItems':4,'items':{'type':'integer','minimum':0}},
 'x':_STRING,'y':_STRING,'band':_STRING,'method_note':{'type':'string','maxLength':2000},
 'value':{'type':'number'},'comparison':{'enum':['>','>=','<','<=']},
 'max_side':{'type':'integer','minimum':1,'maximum':4096},
 'n_bins':{'type':'integer','minimum':2,'maximum':1000}}
_REQUIRED_PARAMS = {'load_bands':['bands'],'index':['x','y'],'select_band':['band'],
                    'threshold':['value','comparison'],'raster_summary':['value','comparison']}
for _op,_entry in CATALOG.items():
 _entry['parameter_schema']={'type':'object','additionalProperties':False,
  'properties':{key:deepcopy(_PARAMETER_FIELDS[key]) for key in _entry['params']},
  'required':_REQUIRED_PARAMS.get(_op,[])}


def write_json(path, value):
 Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n')


def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()


def verify_artifact(artifact):
 root=Path(artifact['directory']).resolve()
 for name, digest in artifact['hashes'].items():
  p=(root/name).resolve()
  if not p.is_relative_to(root) or not p.is_file() or sha(p)!=digest:
   raise ValueError(f'Artifact integrity failure: {name}')
 return root


def array(artifact):
 root=verify_artifact(artifact)
 with np.load(root/'array.npz', allow_pickle=False) as z: return z['value']


def _same(inputs):
 first=inputs[0]
 for other in inputs[1:]:
  if other['grid']!=first['grid'] or other['window']!=first['window']:
   raise ValueError('Input grid/CRS/window mismatch')
 return first['grid'], first['window']


def _compare(values, comparison, threshold):
 return {'>':np.greater,'>=':np.greater_equal,'<':np.less,'<=':np.less_equal}[comparison](values,threshold)


def _save_array(out, value, grid, window, kind):
 np.savez_compressed(out/'array.npz',value=value)
 if kind=='bands': return
 if kind=='rgb': science._write_rgb_raster(out/'rgb.tif',value,grid,window)
 else: science._write_raster(out/'value.tif',value,grid,window,nodata=255 if kind=='mask' else None,
                            tags={'semantic_implementation':VERSION})


def _png(value, out, max_side):
 image=Image.fromarray(np.moveaxis(value,0,2))
 scale=(max_side or max(image.size))/max(image.size)
 size=(max(1,round(image.width*scale)), max(1,round(image.height*scale)))
 image.resize(size,Image.Resampling.NEAREST).save(out/'image.png')


def _tvdi(ndvi,lst,n_bins):
 valid=np.isfinite(ndvi)&np.isfinite(lst)&(ndvi>=0)&(ndvi<=1)&(lst>0)
 x,y=ndvi[valid],lst[valid]
 if x.size<100 or np.ptp(x)==0: raise ValueError('Insufficient TVDI input support')
 edges=np.linspace(x.min(),x.max(),n_bins+1)
 ids=np.minimum(np.searchsorted(edges,x,side='right')-1,n_bins-1)
 low=np.full(n_bins,np.inf); high=np.full(n_bins,-np.inf)
 np.minimum.at(low,ids,y); np.maximum.at(high,ids,y)
 counts=np.bincount(ids,minlength=n_bins); use=counts>0
 if use.sum()<2: raise ValueError('Insufficient populated TVDI bins')
 centers=(edges[:-1]+edges[1:])/2
 dry=np.polyfit(centers[use],high[use],1); wet=np.polyfit(centers[use],low[use],1)
 span=np.polyval(dry,ndvi)-np.polyval(wet,ndvi)
 valid &= np.isfinite(span)&(span>0)
 result=np.where(valid,np.clip(science._ratio(lst-np.polyval(wet,ndvi),span),0,1),np.nan)
 return result,{'n_bins':n_bins,'fit_scope':'full_AOI','dry':dry.tolist(),'wet':wet.tolist(),
                'bin_edges':edges.tolist(),'bin_counts':counts.tolist()}


def execute_operator(op, params, inputs, case_dir, task, available_assets, out):
 """Execute one node. Non-source operators cannot consult case_dir/assets."""
 out=Path(out); out.mkdir(parents=True,exist_ok=False)
 for value in inputs.values():
  for a in value if isinstance(value,list) else [value]: verify_artifact(a)
 kind=CATALOG[op]['output']; meta={}; source_reads=[]; value=None
 if op=='load_bands':
  grid={k:deepcopy(task[k]) for k in ('crs','transform','height','width')}
  window=list(science._window(task,params.get('window')))
  bands=list(params['bands']); phase=params.get('phase')
  names=list(dict.fromkeys(bands+(['SCL'] if params.get('scl',False) else [])))
  loaded={}; provenance=[]
  for name in names:
   asset=science._asset(available_assets,name,phase)
   path=science._safe_asset(Path(case_dir),asset)
   if asset.get('sha256') and sha(path)!=asset['sha256']: raise ValueError('Source hash mismatch')
   loaded[name],record=science._read_asset(Path(case_dir),asset,task,tuple(window))
   provenance.append(record)
   source_reads.append({'asset_id':asset.get('asset_id',asset['path']),'path':asset['path'],
                        'bytes':path.stat().st_size,'sha256':sha(path)})
  valid=np.ones((window[2],window[3]),bool)
  for name in bands: valid &= np.isfinite(loaded[name])
  if params.get('scl',False): valid &= np.isin(loaded['SCL'],science.SCL_VALID)
  value=np.stack([np.where(valid,loaded[name],np.nan) for name in bands])
  meta.update(bands=bands,phase=phase,scl=params.get('scl',False),input_scaling=provenance)
 elif op.startswith('mosaic_'):
  parts=inputs['parts']; grid=deepcopy(parts[0]['grid']); window=[0,0,grid['height'],grid['width']]
  value=np.empty((grid['height'],grid['width']),dtype=array(parts[0]).dtype)
  occupied=np.zeros(value.shape,bool)
  for part in parts:
   if part['grid']!=grid: raise ValueError('Mosaic grid mismatch')
   r,c,h,w=part['window']; region=np.s_[r:r+h,c:c+w]
   if occupied[region].any(): raise ValueError('Mosaic windows overlap')
   value[region]=array(part); occupied[region]=True
  if not occupied.all(): raise ValueError('Mosaic has gaps')
  meta=deepcopy(parts[0]['metadata']); meta['source_parts']=len(parts)
 else:
  flat=[a for x in inputs.values() for a in (x if isinstance(x,list) else [x])]
  grid,window=_same(flat)
  if op in ('select_band','index','rgb'):
   bundle=inputs['bands']; values=array(bundle); bands=dict(zip(bundle['metadata']['bands'],values))
   meta.update(phase=bundle['metadata'].get('phase'))
   if op=='select_band': value=bands[params['band']]; meta['band']=params['band']
   elif op=='index':
    x,y=bands[params['x']],bands[params['y']]
    value=science._ratio(x-y,x+y); meta.update(formula=f"({params['x']}-{params['y']})/({params['x']}+{params['y']})",method={('B03','B11'):'MNDWI',('B03','B08'):'NDWI',('B08','B04'):'NDVI'}.get((params['x'],params['y']),'normalized_difference'))
   else:
    valid=np.isfinite(bands['B04'])&np.isfinite(bands['B03'])&np.isfinite(bands['B02'])
    value=science._display_rgb(bands['B04'],bands['B03'],bands['B02'],valid)
    _png(value,out,params.get('max_side')); meta['display']=science.RGB_DISPLAY
  elif op=='threshold':
   meta=deepcopy(inputs['raster']['metadata']); meta['threshold']=params['value']; meta['comparison']=params['comparison']
   a=array(inputs['raster']); value=np.where(np.isfinite(a),_compare(a,params['comparison'],params['value']),255).astype('uint8')
  elif op=='difference':
   before,after=array(inputs['before']),array(inputs['after'])
   if inputs['before']['metadata'].get('phase')!='before' or inputs['after']['metadata'].get('phase')!='after': raise ValueError('difference requires before/after phase order')
   meta={'method':'after_minus_before','index':inputs['before']['metadata'].get('method')}
   value=np.where(np.isfinite(before)&np.isfinite(after),after-before,np.nan)
  elif op=='water_change':
   before,after=array(inputs['before']),array(inputs['after']); valid=(before!=255)&(after!=255)
   bm,am=inputs['before']['metadata'],inputs['after']['metadata']
   if bm.get('phase')!='before' or am.get('phase')!='after' or any(bm.get(k)!=am.get(k) for k in ('method','threshold','comparison')): raise ValueError('Water phases/methods/thresholds mismatch')
   meta={k:bm.get(k) for k in ('method','threshold','comparison')}
   value=np.zeros(before.shape,'uint8'); value[valid]=1
   value[valid&(before==1)&(after==1)]=2
   value[valid&(before==0)&(after==1)]=3
   value[valid&(before==1)&(after==0)]=4
  elif op=='area_summary':
   a=array(inputs['change']); area=science._pixel_area(grid)
   counts={name:int((a==code).sum()) for code,name in science.CLASS_NAMES.items()}
   valid=int((a!=0).sum())
   if not valid: raise ValueError('No common-valid observations')
   meta={**inputs['change']['metadata'],'valid_pixels':valid,'valid_area_m2':valid*area,'valid_fraction':valid/a.size,'total_pixels':int(a.size),'class_counts':counts,
         'fallback_reason':params.get('method_note'),
         'gained_area_m2':counts['gained']*area,'lost_area_m2':counts['lost']*area,
         'net_change_area_m2':(counts['gained']-counts['lost'])*area,'pixel_area_m2':area}
   write_json(out/'summary.json',meta)
  elif op=='raster_summary':
   a=array(inputs['raster']); valid=np.isfinite(a); selected=valid&_compare(a,params['comparison'],params['value'])
   if not valid.any(): raise ValueError('No valid raster observations')
   meta={'valid_pixels':int(valid.sum()),'valid_area_m2':int(valid.sum())*science._pixel_area(grid),'valid_fraction':float(valid.mean()),'total_pixels':int(a.size),'mean':float(a[valid].mean()),'threshold':params['value'],
         'comparison':params['comparison'],'selected_pixels':int(selected.sum()),
         'selected_percentage':100*int(selected.sum())/int(valid.sum()),
         'selected_area_m2':int(selected.sum())*science._pixel_area(grid),
         'area_basis':'projected map-grid area, not ground-truth surface area'}
   write_json(out/'summary.json',meta)
  elif op=='polygonize':
   local=deepcopy(grid); r,c,h,w=window
   from affine import Affine
   local['transform']=list(Affine(*grid['transform'][:6])*Affine.translation(c,r))[:6]
   local['height'],local['width']=h,w
   meta=science._vectorize(array(inputs['change']),local,out/'polygons.geojson')
  elif op=='overlay':
   rgba=array(inputs['rgb']); change=array(inputs['change']); value=rgba.copy()
   for code,color in ((3,[20,169,145]),(4,[218,89,56])):
    selected=change==code
    value[:3,selected]=np.rint(.5*value[:3,selected]+.5*np.asarray(color)[:,None]).astype('uint8')
   value[:3,change==0]=165; value[3]=255
   _png(value,out,params.get('max_side')); value=None
  elif op=='tvdi':
   if window!=[0,0,grid['height'],grid['width']]: raise ValueError('TVDI fit must use full AOI')
   value,meta=_tvdi(array(inputs['ndvi']),array(inputs['lst']),params.get('n_bins',100))
  else: raise ValueError('Unknown operator')
 if value is not None: _save_array(out,value,grid,window,kind)
 meta['parameters']=deepcopy(params)
 hashes={p.name:sha(p) for p in sorted(out.iterdir()) if p.is_file()}
 artifact={'type':kind,'directory':str(out.resolve()),'grid':grid,'window':window,
           'metadata':meta,'hashes':hashes,'bytes':sum(p.stat().st_size for p in out.iterdir()),
           'source_reads':source_reads,'implementation':VERSION}
 write_json(out/'artifact.json',artifact)
 return artifact
