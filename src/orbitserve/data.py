"""Reproducible public remote-sensing input acquisition; never supplies reference answers.

COGs are read through a bounded HTTP Range opener. The resulting AOI files are
explicitly derived, aligned DN rasters; per-asset physical scale is applied later.
"""
from __future__ import annotations
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlencode

import numpy as np
import requests
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window, from_bounds, transform as window_transform
from rasterio.warp import reproject, Resampling
from pyproj import Transformer

HF_REVISION = '49ce6b784c038b63b0817cbfa8211eb053b47e37'
GITHUB_REVISION = 'ca1dfaef750654331308a434966d307f1728fd90'
STAC_ROOT = 'https://earth-search.aws.element84.com/v1'
BANDS = {'B02':'blue','B03':'green','B04':'red','B08':'nir','B11':'swir16','SCL':'scl'}
DEFAULT_CASES = [
 {'case_id':'lake_mead','name':'Lake Mead, Boulder Basin shoreline','center_lonlat':[-114.78,36.09], 'crs':'EPSG:32611','before_year':2020,'after_year':2021},
 {'case_id':'lake_powell','name':'Lake Powell, Wahweap Bay shoreline','center_lonlat':[-111.49,36.98], 'crs':'EPSG:32612','before_year':2020,'after_year':2021},
 {'case_id':'lake_shasta','name':'Shasta Lake central arms','center_lonlat':[-122.31,40.77], 'crs':'EPSG:32610','before_year':2020,'after_year':2021},
]
_LOCK=threading.Lock()

def utcnow(): return dt.datetime.now(dt.timezone.utc).isoformat()
def sha256(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def dump(path,obj):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def log(root,event):
 record={'timestamp':utcnow(),**event}
 with _LOCK:
  with (Path(root)/'acquisition.jsonl').open('a') as f:f.write(json.dumps(record,allow_nan=False)+'\n')
 return record
def get(url,**kwargs):
 for attempt in range(4):
  try:
   r=requests.get(url,timeout=90,**kwargs);r.raise_for_status();return r
  except requests.RequestException:
   if attempt==3:raise
   time.sleep(2**attempt)

def download_receipt(root,url,digest):
 """Find the earliest actual successful receipt for these source bytes.

 Cache validation cannot establish an acquisition time. Ignore incomplete or
 malformed events from an interrupted run rather than fabricating a timestamp.
 """
 ledger=Path(root)/'acquisition.jsonl'
 if not ledger.exists():return None
 receipts=[]
 for line in ledger.read_text().splitlines():
  try:
   event=json.loads(line)
   if event.get('event')!='download' or event.get('url')!=url or event.get('sha256')!=digest or event.get('http_status')!=200:continue
   observed=dt.datetime.fromisoformat(event['timestamp'].replace('Z','+00:00'))
   if observed.tzinfo is None:continue
   receipts.append((observed,event))
  except (ValueError,TypeError,KeyError,AttributeError):continue
 return min(receipts,key=lambda pair:pair[0])[1] if receipts else None

def download(url,path,root,expected_size=None,expected_sha=None):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists() and (expected_size is None or path.stat().st_size==expected_size) and (expected_sha is None or sha256(path)==expected_sha):
  digest=sha256(path);receipt=download_receipt(root,url,digest)
  if receipt is not None:
   log(root,{'event':'cached_file_validated','url':url,'path':str(path),'bytes':path.stat().st_size,'sha256':digest,'acquired_at':receipt['timestamp']});return receipt
  # A cache without a source receipt needs one real download, not a guessed time.
 r=get(url);b=r.content
 if expected_size is not None and len(b)!=expected_size:raise ValueError(f'size mismatch {url}')
 if expected_sha and hashlib.sha256(b).hexdigest()!=expected_sha:raise ValueError(f'hash mismatch {url}')
 temp=path.with_suffix(path.suffix+'.partial');temp.write_bytes(b);temp.replace(path)
 return log(root,{'event':'download','url':url,'path':str(path),'network_body_bytes':len(b),'http_status':r.status_code,'sha256':sha256(path)})

class RangeSource:
 """Count and hash HTTP response bodies; never issue an unbounded object GET."""
 def __init__(self,url,root,block_size=262144):
  self.url=url;self.root=root;self.block_size=block_size;self.cache={};self.n=None;self.body_bytes=0;self.requests=0
  r=get(url,headers={'Range':'bytes=0-0'})
  if r.status_code!=206 or not r.headers.get('Content-Range','').startswith('bytes 0-0/'):
   raise RuntimeError('Server did not honor the required Range request: '+url)
  self.n=int(r.headers['Content-Range'].split('/')[-1]);self.record(r,0,0)
 def record(self,r,start,end):
  self.body_bytes+=len(r.content);self.requests+=1
  log(self.root,{'event':'http_range','url':self.url,'range_start':start,'range_end':end,'network_body_bytes':len(r.content),'http_status':r.status_code,'content_range':r.headers.get('Content-Range'),'response_sha256':hashlib.sha256(r.content).hexdigest()})
 def block(self,k):
  if k not in self.cache:
   start=k*self.block_size;end=min(start+self.block_size,self.n)-1
   r=get(self.url,headers={'Range':f'bytes={start}-{end}'})
   if r.status_code!=206 or len(r.content)!=end-start+1:raise RuntimeError('Range response mismatch')
   self.record(r,start,end);self.cache[k]=r.content
  return self.cache[k]
 def opener(self,path,mode='rb'):
  if path!=self.url:raise FileNotFoundError(path)
  return RangeHandle(self)
class RangeHandle(io.RawIOBase):
 def __init__(self,source):self.source=source;self.pos=0;self.name=source.url
 def readable(self):return True
 def seekable(self):return True
 def tell(self):return self.pos
 def seek(self,offset,whence=0):
  self.pos=offset if whence==0 else (self.pos+offset if whence==1 else self.source.n+offset)
  return self.pos
 def read(self,n=-1):
  s=self.source
  if n<0:n=s.n-self.pos
  n=min(n,s.n-self.pos)
  if n<=0:return b''
  first=self.pos//s.block_size;last=(self.pos+n-1)//s.block_size
  raw=b''.join(s.block(k) for k in range(first,last+1));off=self.pos-first*s.block_size;self.pos+=n
  return raw[off:off+n]

def raster_metadata(path):
 with rasterio.open(path) as s:
  a=s.read(1);valid=s.read_masks(1)>0;vals=a[valid]
  return {'width':s.width,'height':s.height,'crs':s.crs.to_string(),'transform':list(s.transform)[:6], 'resolution':list(s.res),'dtype':s.dtypes[0],'nodata':s.nodata,'rasterio_scales':list(s.scales),'rasterio_offsets':list(s.offsets),'dataset_tags':s.tags(),'band_tags':s.tags(1),'sample_valid_min':float(vals.min()) if vals.size else None,'sample_valid_max':float(vals.max()) if vals.size else None,'valid_pixels':int(valid.sum()),'sha256':sha256(path),'bytes':Path(path).stat().st_size}

def acquire_earthbench(root):
 """Only TIFF inputs and sanitized tasks enter earthbench; references stay evaluation."""
 root=Path(root);evaluation=root/'evaluation';evaluation.mkdir(parents=True,exist_ok=True)
 qurl=f'https://raw.githubusercontent.com/opendatalab/Earth-Agent/{GITHUB_REVISION}/benchmark/question.json'
 download(qurl,evaluation/'upstream_question.json',root)
 refs=json.loads((evaluation/'upstream_question.json').read_text());results=[]
 for q in ['question2','question4']:
  u=f'https://huggingface.co/api/datasets/Sssunset/Earth-Bench/tree/{HF_REVISION}/{q}'
  response=get(u);tree=response.json();dump(root/'catalog'/f'{q}_hf_tree.json',tree)
  log(root,{'event':'catalog','url':u,'network_body_bytes':len(response.content),'revision':HF_REVISION})
  directory=root/'earthbench'/q;directory.mkdir(parents=True,exist_ok=True);assets=[]
  for f in tree:
   if f['type']!='file' or not f['path'].endswith('.tif'):continue
   url=f'https://huggingface.co/datasets/Sssunset/Earth-Bench/resolve/{HF_REVISION}/{f["path"]}'
   path=root/'earthbench'/f['path'];receipt=download(url,path,root,f['size'],f.get('lfs',{}).get('oid'))
   m=raster_metadata(path);tag=m['band_tags'];band='NDVI' if '_NDVI' in path.name else 'LST'
   if 'scale' not in tag:raise ValueError('Missing physical scale in Earth-Bench band tags')
   assets.append({**m,'path':path.name,'phase':'single','band':band,'scale':float(tag['scale']),'offset':0.0,'source':url,'source_revision':HF_REVISION,'acquired_at':receipt['timestamp'],'acquisition_time_basis':'successful_source_download_receipt','original_dtype':m['dtype'],'observation_time':'2021-07-12' if q=='question2' else '2022-08-13','observation_time_precision':'date','source_node':'A','initial_holder':'A','processing_level':'supplied MODIS derived product, upstream resampled 30m','native_source_resolution_m':float(tag['gsd']),'observation_date':'2021-07-12' if q=='question2' else '2022-08-13','units':'K' if band=='LST' else 'dimensionless','scaling_authority':'TIFF band metadata scale; raw integer samples verify unscaled DN'})
  date='2021-07-12' if q=='question2' else '2022-08-13';place='Sichuan Plain' if q=='question2' else 'Chengdu and surroundings'
  task={'case_id':q,'question':f'Using the supplied NDVI and LST products for {place} on {date}, compute TVDI and the percentage of valid pixels whose TVDI exceeds 0.75.','method':'TVDI','threshold':0.75,'source_node':'A','width':assets[0]['width'],'height':assets[0]['height'],'crs':assets[0]['crs'],'transform':assets[0]['transform'],'pixel_size_m':30,'caveat':'Input grid is 30m Web Mercator, but source MODIS NDVI and LST resolutions are 250m and 1000m. No 30m physical information or agricultural land-cover mask is supplied.'}
  if q=='question2':task['erratum']='Upstream evaluation prose says July12,2022 and EVI. Actual authoritative inputs and user task use July12,2021 and NDVI; this is a corrected derived task.'
  dump(directory/'task.json',task);dump(directory/'assets.json',{'assets':assets,'reference_free':True});dump(evaluation/f'{q}_reference.json',refs[q.removeprefix('question')]);results.append({'case_id':q,'directory':str(directory),'bytes':sum(a['bytes'] for a in assets),'assets':assets})
 return results

def target_grid(case):
 lon,lat=case['center_lonlat'];x,y=Transformer.from_crs('EPSG:4326',case['crs'],always_xy=True).transform(lon,lat)
 left=math.floor(x/20)*20-2500;top=math.ceil(y/20)*20+2500
 return Affine(20,0,left,0,-20,top)

def covers(item,grid,crs):
 if item['properties'].get('proj:epsg')!=int(crs.split(':')[-1]):return False
 a=item['assets']['scl'];t=Affine(*a['proj:transform'][:6]);h,w=a['proj:shape']
 return grid.c>=t.c and grid.f<=t.f and grid.c+5000<=t.c+t.a*w and grid.f-5000>=t.f+t.e*h

def select_item(root,case,phase,grid):
 pinned=case.get('items',{}).get(phase)
 if pinned:
  u=f'{STAC_ROOT}/collections/sentinel-2-l2a/items/{pinned}';r=get(u);item=r.json();candidates=[item]
 else:
  lon,lat=case['center_lonlat'];year=case[phase+'_year']
  params={'collections':'sentinel-2-l2a','bbox':f'{lon-.04},{lat-.04},{lon+.04},{lat+.04}','datetime':f'{year}-06-01T00:00:00Z/{year}-06-30T23:59:59Z','limit':100}
  u=STAC_ROOT+'/search?'+urlencode(params);r=get(u);candidates=r.json()['features']
  eligible=[i for i in candidates if all(v in i['assets'] for v in BANDS.values()) and covers(i,grid,case['crs'])]
  if not eligible:raise RuntimeError('No fully covered complete candidate for '+case['case_id']+'/'+phase)
  item=min(eligible,key=lambda i:i['properties']['eo:cloud_cover'])
 if not covers(item,grid,case['crs']):raise RuntimeError('Pinned item does not fully cover AOI')
 dump(root/'catalog'/f'{case["case_id"]}_{phase}_item.json',item)
 log(root,{'event':'scene_selection','case_id':case['case_id'],'phase':phase,'catalog_url':u,'network_body_bytes':len(r.content),'selected_item':item['id'],'candidates':[{'id':i['id'],'cloud_cover':i['properties'].get('eo:cloud_cover'),'fully_covers_target':covers(i,grid,case['crs'])} for i in candidates],'reason':'Lowest scene cloud fraction among candidates with complete bands and full target grid coverage; June matched seasons, 2020 vs2021 reservoir shoreline comparison. No pixels edited to produce change.'})
 return item

def effective_scaling(item, band):
 """Resolve the provider's documented harmonization flag, not date or outcomes."""
 rb=item['assets'][BANDS[band]].get('raster:bands',[{}])[0]
 reported_scale=float(rb.get('scale',1));reported_offset=float(rb.get('offset',0))
 applied=item['properties'].get('earthsearch:boa_offset_applied')
 if band=='SCL':scale,offset=1.0,0.0
 elif applied is True:scale,offset=reported_scale,0.0
 elif applied is False:scale,offset=reported_scale,reported_offset
 else:
  if float(item['properties'].get('s2:processing_baseline','99'))>=4 and reported_offset!=0:
   raise ValueError('Ambiguous Sentinel offset: missing harmonization flag; refusing to guess')
  scale,offset=reported_scale,reported_offset
 return {'scale':scale,'offset':offset,'reported_scale':reported_scale,'reported_offset':reported_offset,'earthsearch_boa_offset_applied':applied,'processing_baseline':item['properties'].get('s2:processing_baseline'),'scaling_version':'earthsearch-boa-v1','scaling_authority_url':'https://github.com/Element84/earth-search/issues/9','scaling_rationale':'When earthsearch:boa_offset_applied is true the COG DN is already harmonized; effective offset is zero despite stale asset raster:bands.offset. This follows provider maintainer guidance and avoids double subtraction. SCL is categorical and unscaled.'}

def reconcile_effective_scaling(root):
 """Repair previously acquired metadata after the provider-offset audit.

 Pixels are unchanged. Updating TIFF tags changes file hashes, which are refreshed.
 References and scientific outputs are never consulted.
 """
 root=Path(root);updated=[]
 for p in sorted((root/'cases').glob('*/assets.json')):
  directory=p.parent;payload=json.loads(p.read_text())
  for asset in payload['assets']:
   item=json.loads((root/'catalog'/f'{directory.name}_{asset["phase"]}_item.json').read_text())
   info=effective_scaling(item,asset['band']);path=directory/asset['path'];old_hash=sha256(path)
   with rasterio.open(path,'r+') as raster:
    tags=raster.tags(1)
    if tags.get('offset')!=str(info['offset']) or 'source_reported_offset' not in tags:
     before_pixels_hash=hashlib.sha256(raster.read(1).tobytes()).hexdigest()
     raster.update_tags(1,scale=str(info['scale']),offset=str(info['offset']),source_reported_offset=str(info['reported_offset']),boa_offset_applied=str(info['earthsearch_boa_offset_applied']),scaling_version=info['scaling_version'])
    else:before_pixels_hash=None
   if before_pixels_hash:
    with rasterio.open(path) as raster:
     if hashlib.sha256(raster.read(1).tobytes()).hexdigest()!=before_pixels_hash:raise RuntimeError('Pixel data changed during metadata repair')
   asset.update(info);asset.update(raster_metadata(path));asset['asset_id']=f'{directory.name}:{asset["phase"]}:{asset["band"]}'
   if old_hash!=asset['sha256']:
    log(root,{'event':'scaling_metadata_corrected','case_id':directory.name,'path':str(path),'prior_sha256':old_hash,'new_sha256':asset['sha256'],'pixel_values_unchanged':True,**info})
    log(root,{'event':'crop_complete','case_id':directory.name,**asset})
  dump(p,payload);updated.append(directory.name)
 manifest_path=root/'acquisition_manifest.json'
 if manifest_path.exists():
  manifest=json.loads(manifest_path.read_text())
  for case in manifest.get('cases',[]):
   if case['case_id'] in updated:
    case['assets']=json.loads((root/'cases'/case['case_id']/'assets.json').read_text())['assets'];case['bytes']=sum(a['bytes'] for a in case['assets'])
  manifest['scaling_audit']={'version':'earthsearch-boa-v1','provider_evidence':'https://github.com/Element84/earth-search/issues/9','resolved_at':utcnow(),'pixel_values_changed':False,'previous_water_products_require_recompute':True}
  manifest['input_bytes']=sum(x['bytes'] for x in manifest.get('earthbench',[])+manifest.get('cases',[]));dump(manifest_path,manifest)
 return updated

def crop_band(root,case,phase,item,band,grid):
 directory=root/'cases'/case['case_id'];path=directory/'imagery'/phase/(band+'.tif');path.parent.mkdir(parents=True,exist_ok=True)
 asset=item['assets'][BANDS[band]];url=asset['href']
 # A previously completed and hashed crop may survive an interrupted acquisition.
 ledger=root/'acquisition.jsonl'
 if path.exists() and ledger.exists():
  for line in reversed(ledger.read_text().splitlines()):
   try: previous=json.loads(line)
   except json.JSONDecodeError: continue
   if previous.get('event')=='crop_complete' and previous.get('case_id')==case['case_id'] and previous.get('phase')==phase and previous.get('band')==band and previous.get('source')==url and previous.get('transform')==list(grid)[:6] and sha256(path)==previous.get('sha256'):
    record={k:v for k,v in previous.items() if k not in ['event','timestamp','case_id']}
    record['observation_time_precision']='instant';record['acquisition_time_basis']='derived_AOI_written_after_source_ranges'
    record['original_acquisition_network_body_bytes']=record.get('original_acquisition_network_body_bytes',record.get('network_body_bytes',0));record['network_body_bytes']=0;record['cached_verified']=True
    log(root,{'event':'cached_crop_validated','case_id':case['case_id'],'path':str(path),'sha256':record['sha256'],'source':url});return record
 source=RangeSource(url,root)
 rb=asset.get('raster:bands',[{}])[0];scaling=effective_scaling(item,band);scale=scaling['scale'];offset=scaling['offset']
 with rasterio.open(url,opener=source.opener) as s:
  if s.crs.to_string()!=case['crs']:raise ValueError('Unexpected source CRS')
  w=from_bounds(grid.c,grid.f-5000,grid.c+5000,grid.f,s.transform)
  margin=2;col=max(0,math.floor(w.col_off)-margin);row=max(0,math.floor(w.row_off)-margin)
  right=min(s.width,math.ceil(w.col_off+w.width)+margin);bottom=min(s.height,math.ceil(w.row_off+w.height)+margin)
  win=Window(col,row,right-col,bottom-row);a=s.read(1,window=win)
  dtype='uint8' if band=='SCL' else 'float32';out=np.zeros((250,250),dtype=dtype)
  resampling=Resampling.nearest if band=='SCL' else Resampling.bilinear
  reproject(a,out,src_transform=window_transform(win,s.transform),src_crs=s.crs,src_nodata=s.nodata,dst_transform=grid,dst_crs=case['crs'],dst_nodata=0,resampling=resampling)
  source_meta={'width':s.width,'height':s.height,'crs':s.crs.to_string(),'transform':list(s.transform)[:6],'resolution':list(s.res),'dtype':s.dtypes[0],'nodata':s.nodata,'window':[col,row,right-col,bottom-row],'file_size_bytes':source.n,'full_file_sha256':None,'full_file_sha256_reason':'No complete source-object hash computed; bounded received ranges and derived crop hashes are recorded.'}
 with rasterio.open(path,'w',driver='GTiff',width=250,height=250,count=1,dtype=dtype,crs=case['crs'],transform=grid,nodata=0,compress='deflate',predictor=1 if band=='SCL' else 3) as dst:
  dst.write(out,1);dst.update_tags(source_item=item['id'],source_url=url,processing='AOI window and reprojection only; preserved DN values');dst.update_tags(1,scale=str(scale),offset=str(offset),source_reported_offset=str(scaling['reported_offset']),boa_offset_applied=str(scaling['earthsearch_boa_offset_applied']),scaling_version=scaling['scaling_version'])
 m=raster_metadata(path);record={**m,'path':str(path.relative_to(directory)),'asset_id':f'{case["case_id"]}:{phase}:{band}','phase':phase,'band':band,'scale':scale,'offset':offset,'source':url,'item_id':item['id'],'source_item':item['id'],'observation_time':item['properties']['datetime'],'observation_time_precision':'instant','acquired_at':utcnow(),'acquisition_time_basis':'derived_AOI_written_after_source_ranges','processing_level':'Sentinel-2 L2A bottom-of-atmosphere; aligned AOI derivative','source_node':'A','initial_holder':'A','resampling':'nearest' if band=='SCL' else 'bilinear','source_metadata':source_meta,'source_raster_band':rb,'network_body_bytes':source.body_bytes,'range_requests':source.requests,'scaling_applied':False,'original_dtype':source_meta['dtype'],**scaling}
 log(root,{'event':'crop_complete','case_id':case['case_id'],**record});return record

def acquire_case(root,case):
 root=Path(root);grid=target_grid(case);items={p:select_item(root,case,p,grid) for p in ['before','after']}
 jobs=[(p,band) for p in ['before','after'] for band in BANDS]
 with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
  assets=list(executor.map(lambda pb:crop_band(root,case,pb[0],items[pb[0]],pb[1],grid),jobs))
 directory=root/'cases'/case['case_id']
 task={'case_id':case['case_id'],'title':case['name'],'question':'Map water change candidates between the two supplied dates within the fixed AOI.','width':250,'height':250,'crs':case['crs'],'transform':list(grid)[:6],'threshold':0.0,'method':'MNDWI','source_node':'A','pixel_size_m':20,'area_m2':25000000,'center_lonlat':case['center_lonlat'],'dates':{p:i['properties']['datetime'] for p,i in items.items()},'common_valid_rule':'Intersection of both phase valid reflectance and SCL classes4,5,6,7; denominator nonzero; no-data/cloud/shadow unknown.','aoi_selection_reason':'Real reservoir shoreline within a 5km projected square; matched June observations in2020 and2021; results are candidates, not a validated flood classification.'}
 dump(directory/'task.json',task);dump(directory/'assets.json',{'assets':assets,'reference_free':True})
 # Input quality checks select no pixels and change no values; independent operators own final outputs.
 a={}
 for asset in assets:
  with rasterio.open(directory/asset['path']) as s:a[(asset['phase'],asset['band'])]=s.read(1)
 valid=[];water=[]
 for phase in ['before','after']:
  ok=np.isin(a[(phase,'SCL')],[4,5,6,7]);green_asset=next(x for x in assets if x['phase']==phase and x['band']=='B03');swir_asset=next(x for x in assets if x['phase']==phase and x['band']=='B11');green=a[(phase,'B03')]*green_asset['scale']+green_asset['offset'];swir=a[(phase,'B11')]*swir_asset['scale']+swir_asset['offset']
  for band in BANDS:
   if band!='SCL':ok &= np.isfinite(a[(phase,band)])&(a[(phase,band)]!=0)
  ok &= (green+swir)!=0;valid.append(ok);index=np.divide(green-swir,green+swir,out=np.zeros_like(green),where=(green+swir)!=0);water.append(index>0)
 common=valid[0]&valid[1];quality={'case_id':case['case_id'],'valid_before':int(valid[0].sum()),'valid_after':int(valid[1].sum()),'valid_common':int(common.sum()),'valid_common_fraction':float(common.mean()),'water_before_common':int((water[0]&common).sum()),'water_after_common':int((water[1]&common).sum()),'change_candidate_pixels':int(((water[0]!=water[1])&common).sum()),'purpose':'Acquisition quality screening, not scientific accuracy validation; no reference labels used.'}
 if quality['valid_common']==0:raise ValueError('No common valid pixels')
 dump(root/'catalog'/f'{case["case_id"]}_quality.json',quality)
 result={'case_id':case['case_id'],'directory':str(directory),'items':{p:i['id'] for p,i in items.items()},'bytes':sum(a['bytes'] for a in assets),'network_body_bytes':sum(a['network_body_bytes'] for a in assets),'quality':quality,'assets':assets}
 log(root,{'event':'case_complete',**{k:v for k,v in result.items() if k!='assets'}})
 return result

def acquire(data_root: Path,cases=None)->dict:
 root=Path(data_root).expanduser().resolve();root.mkdir(parents=True,exist_ok=True)
 config=Path(__file__).resolve().parents[2]/'configs'/'data_selection.json'
 selection=json.loads(config.read_text())['cases'] if config.exists() else DEFAULT_CASES
 if cases is not None:
  wanted={cases} if isinstance(cases,str) else set(cases);selection=[c for c in selection if c['case_id'] in wanted]
  unknown=wanted-{'earthbench'}-{c['case_id'] for c in selection}
  if unknown:raise ValueError(f'Unknown cases: {unknown}')
 manifest={'schema_version':1,'started_at':utcnow(),'run_id':'acquire-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S'),'data_root':str(root),'earthbench_revision':HF_REVISION,'earth_agent_revision':GITHUB_REVISION,'question100':'Excluded: upstream trajectory incorrect; not needed in this prototype.','reference_isolation':'Only evaluation/ holds upstream dialogs, answers and trajectories. Runtime source copying must whitelist task/assets/imagery, never evaluation or catalog.','network_accounting':'Recorded HTTP response body bytes (not TLS/header overhead); cached prior acquisitions excluded from current network count. COG Range bytes hashed individually.','earthbench':acquire_earthbench(root),'cases':[]}
 dump(root/'acquisition_manifest.json',manifest)
 for case in selection:
  result=acquire_case(root,case);manifest['cases'].append(result);dump(root/'acquisition_manifest.json',manifest);print(json.dumps({'event':'ready','case_id':case['case_id'],'directory':result['directory'],'quality':result['quality']}),flush=True)
 reconcile_effective_scaling(root);manifest=json.loads((root/'acquisition_manifest.json').read_text());manifest['completed_at']=utcnow();manifest['input_bytes']=sum(x['bytes'] for x in manifest['earthbench']+manifest['cases']);manifest['cog_network_body_bytes']=sum(x['network_body_bytes'] for x in manifest['cases']);events=[json.loads(line) for line in (root/'acquisition.jsonl').read_text().splitlines()];manifest['cumulative_successful_cog_response_body_bytes']=sum(e.get('network_body_bytes',0) for e in events if e['event']=='http_range');manifest['input_raster_files']=sum(len(x['assets']) for x in manifest['earthbench']+manifest['cases']);dump(root/'acquisition_manifest.json',manifest)
 return manifest
