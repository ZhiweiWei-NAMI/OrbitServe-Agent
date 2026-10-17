"""Output-contract oracle, intentionally absent from agent requests.

Reconstructs science from source samples independently of semantic operators.
It scores requested products and semantics, never a reference DAG topology.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.features import rasterize
from rasterio.warp import transform_geom
from PIL import Image
from .semantic_ops import verify_artifact, array


def _band(source,task,assets,name,phase=None):
 matches=[a for a in assets if a['band']==name and (phase is None or a.get('phase')==phase)]
 if len(matches)!=1: raise ValueError('Oracle source not uniquely available')
 a=matches[0]; p=(Path(source)/a['path']).resolve()
 if not p.is_relative_to(Path(source).resolve()): raise ValueError('Oracle path escape')
 with rasterio.open(p) as src:
  transform=Affine(*task['transform'][:6]); crs=rasterio.crs.CRS.from_user_input(task['crs'])
  if src.crs==crs and src.transform==transform and (src.height,src.width)==(task['height'],task['width']):
   raw=src.read(1,masked=True).astype('float64')
  else:
   with WarpedVRT(src,crs=crs,transform=transform,width=task['width'],height=task['height'],
      dtype='float64',nodata=float('nan'),src_nodata=a.get('nodata',src.nodata),
      resampling=Resampling.nearest if name=='SCL' else Resampling.bilinear) as vrt:
    raw=vrt.read(1,masked=True)
  good=~np.ma.getmaskarray(raw)&np.isfinite(raw.data)
  if a.get('nodata',src.nodata) is not None: good &= raw.data!=a.get('nodata',src.nodata)
  return np.where(good,raw.data*float(a['scale'])+float(a['offset']),np.nan)


def _ratio(a,b):
 good=np.isfinite(a)&np.isfinite(b)&((a+b)!=0)
 out=np.full(a.shape,np.nan); np.divide(a-b,a+b,out=out,where=good)
 return out


def _tvdi_reference(ndvi,lst,bins):
 valid=np.isfinite(ndvi)&np.isfinite(lst)&(ndvi>=0)&(ndvi<=1)&(lst>0)
 x,y=ndvi[valid],lst[valid]; edges=np.linspace(x.min(),x.max(),bins+1)
 centers=[]; lo=[]; hi=[]
 # Independent grouping implementation, deliberately not semantic_ops._tvdi.
 for i in range(bins):
  keep=(x>=edges[i])&((x<=edges[i+1]) if i==bins-1 else (x<edges[i+1]))
  if keep.any(): centers.append((edges[i]+edges[i+1])/2); lo.append(y[keep].min()); hi.append(y[keep].max())
 design=np.column_stack([centers,np.ones(len(centers))])
 dry=np.linalg.lstsq(design,np.asarray(hi),rcond=None)[0]
 wet=np.linalg.lstsq(design,np.asarray(lo),rcond=None)[0]
 span=(dry[0]-wet[0])*ndvi+(dry[1]-wet[1]); good=valid&np.isfinite(span)&(span>0)
 value=np.full(ndvi.shape,np.nan)
 value[good]=np.clip((lst[good]-(wet[0]*ndvi[good]+wet[1]))/span[good],0,1)
 return value


def verify_quality(case,source,task,assets,outputs,plan):
 checks=[]
 def check(name,ok,detail=None): checks.append({'check':name,'status':'PASS' if bool(ok) else 'FAIL','detail':detail})
 goal=case['goal']; p=case.get('parameters',{})
 check('requested_output_roles',set(outputs)==set(case['required_outputs']))
 if plan['status']=='needs_data':
  missing={f'{phase}:B11' for phase in ('before','after') if not any(a['band']=='B11' and a.get('phase')==phase for a in assets)}
  check('correct_missing_data_no_products',goal=='missing_mndwi' and bool(missing) and set(plan['missing_assets'])==missing and not outputs and not plan['nodes'])
  return {'status':'PASS' if all(c['status']=='PASS' for c in checks) else 'FAIL','checks':checks}
 check('strict_missing_mndwi_not_fabricated',goal!='missing_mndwi')
 for role,a in outputs.items():
  verify_artifact(a)
  check(role+'_full_grid',a['grid']=={k:task[k] for k in ('crs','transform','height','width')} and a['window']==[0,0,task['height'],task['width']])
 crs=rasterio.crs.CRS.from_user_input(task['crs']); transform=Affine(*task['transform'][:6])
 if not crs.is_projected: raise ValueError('Area oracle requires projected CRS')
 pixel_area=abs(transform.determinant)*crs.linear_units_factor[1]**2
 shape=(task['height'],task['width']); reference=None; change=None
 if goal in ('water_area','water_map','water_area_ndwi'):
  second='B08' if goal=='water_area_ndwi' else 'B11'; water={}; valid={}
  for phase in ('before','after'):
   value=_ratio(_band(source,task,assets,'B03',phase),_band(source,task,assets,second,phase))
   valid[phase]=np.isfinite(value)&np.isin(_band(source,task,assets,'SCL',phase),[4,5,6,7])
   water[phase]=value>p.get('threshold',0.)
  common=valid['before']&valid['after']; b,a=water['before'],water['after']
  change=np.zeros(shape,'uint8'); change[common]=1
  change[common&b&a]=2; change[common&~b&a]=3; change[common&b&~a]=4
  summary=json.loads((verify_artifact(outputs['summary'])/'summary.json').read_text())
  expected={'gained_area_m2':int((change==3).sum())*pixel_area,'lost_area_m2':int((change==4).sum())*pixel_area,
            'net_change_area_m2':int((change==3).sum()-(change==4).sum())*pixel_area,
            'valid_area_m2':int(common.sum())*pixel_area,'valid_pixels':int(common.sum()),
            'valid_fraction':float(common.mean())}
  for k,v in expected.items(): check('water_'+k,k in summary and abs(summary[k]-v)<=1e-6,{'expected':v,'observed':summary.get(k)})
  check('actual_method_identity',summary.get('method')==p.get('water_method','MNDWI') and summary.get('threshold')==p.get('threshold',0.))
  if goal=='water_area_ndwi': check('authorized_fallback_disclosed',case.get('allow_fallback') is True and bool(summary.get('fallback_reason')))
 if goal=='ndvi_decline':
  dates={}
  for phase in ('before','after'):
   ndvi=_ratio(_band(source,task,assets,'B08',phase),_band(source,task,assets,'B04',phase))
   dates[phase]=np.where(np.isin(_band(source,task,assets,'SCL',phase),[4,5,6,7]),ndvi,np.nan)
  reference=dates['after']-dates['before']; threshold=p.get('decline_threshold',-.15); comparison='<='
 if goal=='tvdi':
  reference=_tvdi_reference(_band(source,task,assets,'NDVI'),_band(source,task,assets,'LST'),p.get('n_bins',100))
  threshold=p.get('threshold',.75); comparison='>'
 if reference is not None:
  valid=np.isfinite(reference); selected=valid&((reference<=threshold) if comparison=='<=' else (reference>threshold))
  role='delta' if goal=='ndvi_decline' else 'tvdi'
  if role in outputs:
   actual=array(outputs[role]); check(role+'_unknown_mask',np.array_equal(np.isfinite(actual),valid))
   check(role+'_values',np.allclose(actual[valid],reference[valid],rtol=0,atol=1e-6),{'absolute_tolerance':1e-6})
   with rasterio.open(verify_artifact(outputs[role])/'value.tif') as tif:
    published=tif.read(1,masked=True).filled(np.nan)
    check(role+'_published_tiff',np.array_equal(np.isfinite(published),valid) and np.allclose(published[valid],reference[valid],rtol=0,atol=1e-6))
  summary=json.loads((verify_artifact(outputs['summary'])/'summary.json').read_text())
  expected={'valid_pixels':int(valid.sum()),'mean':float(reference[valid].mean()),'selected_pixels':int(selected.sum()),
            'selected_area_m2':int(selected.sum())*pixel_area,'selected_percentage':100*int(selected.sum())/int(valid.sum()),
            'valid_area_m2':int(valid.sum())*pixel_area,'valid_fraction':float(valid.mean())}
  for k,v in expected.items(): check('raster_'+k,k in summary and abs(summary[k]-v)<=1e-6,{'expected':v,'observed':summary.get(k)})
  check('threshold_contract',summary.get('threshold')==threshold and summary.get('comparison')==comparison)
 if 'polygons' in outputs:
  geo=json.loads((verify_artifact(outputs['polygons'])/'polygons.geojson').read_text()); geometries=[]; count={3:0,4:0}; areas={3:0.,4:0.}
  from shapely.geometry import shape as geometry_shape
  for feature in geo['features']:
   code=feature['properties']['class_code']; geometry=feature['geometry']; bounds=geometry_shape(geometry).bounds
   check('polygon_lonlat_bounds',code in (3,4) and -180<=bounds[0]<=bounds[2]<=180 and -90<=bounds[1]<=bounds[3]<=90)
   projected=transform_geom('EPSG:4326',crs,geometry)
   check('polygon_geometry_area',abs(geometry_shape(projected).area-feature['properties']['area_m2'])<=max(1.,feature['properties']['area_m2']*1e-5))
   geometries.append((projected,code)); count[code]+=feature['properties']['pixel_count']; areas[code]+=feature['properties']['area_m2']
  rendered=rasterize(geometries,out_shape=shape,transform=transform,fill=0,dtype='uint8') if geometries else np.zeros(shape,'uint8')
  check('polygon_exact_raster_support',np.array_equal(rendered,np.where(np.isin(change,[3,4]),change,0)))
  for code in (3,4): check('polygon_class_'+str(code),count[code]==int((change==code).sum()) and abs(areas[code]-int((change==code).sum())*pixel_area)<=1e-6)
 if 'rgb' in outputs:
  channels=[_band(source,task,assets,b,'after') for b in ('B04','B03','B02')]
  valid=np.logical_and.reduce([np.isfinite(c) for c in channels]); rgba=np.zeros((4,*shape),'uint8')
  for i,c in enumerate(channels): rgba[i,valid]=np.rint(255*np.clip(c[valid]/.3,0,1)).astype('uint8')
  rgba[3,valid]=255
  out=outputs['rgb']; check('rgb_type_matches_plain_quicklook',out['type']=='rgb')
  if out['type']=='rgb':
   check('rgb_real_source_values',np.array_equal(array(out),rgba))
   with rasterio.open(verify_artifact(out)/'rgb.tif') as tif: check('rgb_published_tiff',np.array_equal(tif.read(),rgba))
   expected=Image.fromarray(np.moveaxis(rgba,0,2)); size=p.get('max_side')
   if size:
    scale=size/max(expected.size); expected=expected.resize((round(expected.width*scale),round(expected.height*scale)),Image.Resampling.NEAREST)
   with Image.open(verify_artifact(out)/'image.png') as image: check('rgb_png_dimensions_alpha_pixels',image.mode=='RGBA' and np.array_equal(np.asarray(image),np.asarray(expected)))
 if goal=='rgb':
  sources=[n for n in plan['nodes'] if n['op']=='load_bands']
  check('rgb_only_requested_sources',all(n['params'].get('phase')=='after' and set(n['params']['bands'])<=set(['B04','B03','B02']) and not n['params'].get('scl',False) for n in sources))
  check('rgb_no_unrequested_science',all(n['op'] in ('load_bands','rgb') for n in plan['nodes']))
 if set(case['required_outputs'])=={'summary'} and goal in ('water_area','water_area_ndwi'):
  check('scalar_no_unrequested_display_vectors',not any(n['op'] in ('rgb','overlay','polygonize') for n in plan['nodes']))
 return {'status':'PASS' if all(c['status']=='PASS' for c in checks) else 'FAIL','checks':checks,
         'boundary':'Scientific contract equivalence on actual samples; not external geophysical accuracy or reference-answer matching.'}
