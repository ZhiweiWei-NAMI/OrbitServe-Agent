from pathlib import Path
import json,hashlib,os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
BASE=Path(__file__).resolve().parent
STUDY=Path(os.environ['ORBITSERVE_STUDY_ROOT'])
RUN=STUDY/'normal-final'
rows=json.loads((RUN/'metrics.json').read_text())
def run(case):return RUN/'runs'/Path(next(r['run_dir'] for r in rows if r['case_id']==case and r['arm']=='fixture_agent_only')).name
def arr(p):return np.load(p,allow_pickle=False)['value']
plt.rcParams.update({'font.family':'Liberation Serif','font.size':12,'axes.titlesize':13,'axes.labelsize':12,'figure.facecolor':'white','savefig.facecolor':'white'})
# Scientific figure from exact executed intermediate arrays, not a generative image.
w=run('water_vector_rgb_mead')
b=np.load(BASE/'sources/mead_before_rgb_for_display.npz');before=np.moveaxis(b['rgb'],0,2).copy();before[~b['valid']]=[165,165,165]
a=arr(w/'artifacts/display/array.npz');after=np.moveaxis(a[:3],0,2).copy();after[a[3]==0]=[165,165,165]
c=arr(w/'artifacts/changed/array.npz');overlay=after.copy().astype(float)
for code,color in [(3,(20,169,145)),(4,(218,89,56))]:overlay[c==code]=.5*overlay[c==code]+.5*np.array(color)
overlay[c==0]=[165,165,165];overlay=np.rint(overlay).astype('uint8')
fig,axs=plt.subplots(1,3,figsize=(8.3,3.3))
for ax,img,title in zip(axs,[before,after,overlay],['Before RGB | 2020-06-09','After RGB | 2021-06-14','Computed water-change candidates']):ax.imshow(img,interpolation='nearest');ax.set_title(title,pad=7);ax.axis('off')
fig.legend(handles=[Patch(color=np.array([20,169,145])/255,label='Gained'),Patch(color=np.array([218,89,56])/255,label='Lost'),Patch(color=np.array([165,165,165])/255,label='Unknown')],loc='lower center',ncol=3,frameon=False,bbox_to_anchor=(.5,-.02))
fig.subplots_adjust(left=.01,right=.99,top=.94,bottom=.12,wspace=.06)
fig.savefig(BASE/'figures/semantic_mead_before_after_change.png',dpi=230,bbox_inches='tight');plt.close(fig)
# NDVI raster semantics: per-date validity and common-valid delta.
w=run('ndvi_decline_shasta');ims=[arr(w/f'artifacts/{k}/array.npz') for k in ('before_ndvi','after_ndvi','delta')]
fig,axs=plt.subplots(1,3,figsize=(8.3,3.55))
for i,(ax,v,title) in enumerate(zip(axs,ims,['Before NDVI | 2020-06-26','After NDVI | 2021-06-18','NDVI delta | after - before'])):
 cmap=plt.get_cmap('RdYlGn' if i<2 else 'RdBu').copy();cmap.set_bad('#a5a5a5')
 im=ax.imshow(v,cmap=cmap,vmin=-1 if i<2 else -.5,vmax=1 if i<2 else .5,interpolation='nearest');ax.set_title(title,pad=7);ax.axis('off')
 cb=fig.colorbar(im,ax=ax,orientation='horizontal',fraction=.048,pad=.035);cb.ax.tick_params(labelsize=10)
fig.subplots_adjust(left=.01,right=.99,top=.93,bottom=.08,wspace=.22)
fig.savefig(BASE/'figures/semantic_shasta_ndvi.png',dpi=230,bbox_inches='tight');plt.close(fig)
# Independent AOIs, never a before/after pair.
fig,axs=plt.subplots(2,1,figsize=(8.3,5.8),gridspec_kw={'height_ratios':[1,2]})
for ax,case,title in zip(axs,['tvdi_sichuan_2021','tvdi_chengdu_2022'],['Sichuan | 2021-07-12 | independent AOI','Chengdu | 2022-08-13 | independent AOI']):
 v=arr(run(case)/'artifacts/dryness/array.npz');cm=plt.get_cmap('YlOrRd').copy();cm.set_bad('#a5a5a5');im=ax.imshow(v,cmap=cm,vmin=0,vmax=1,interpolation='nearest');ax.set_title(title,pad=5);ax.axis('off')
fig.subplots_adjust(left=.02,right=.88,top=.94,bottom=.03,hspace=.22)
cax=fig.add_axes([.91,.17,.018,.65]);fig.colorbar(im,cax=cax,label='TVDI')
fig.savefig(BASE/'figures/semantic_tvdi_independent_aois.png',dpi=190,bbox_inches='tight');plt.close(fig)
# Modeled times, not measured times; focused three-case comparison.
v2=json.loads((STUDY/'model-v2-replay/model_controls.json').read_text())
allrows=[dict(case_id=r['case_id'],scenario=r['scenario'],arm=arm,modeled_completion_s=r['modeled']['completion_s']) for r in v2 if r['arm']=='non_agent' for arm in (['non_agent','fixture_agent_only'] if r['control']=='local_current' else ['fixture_agent_wm_offload'] if r['control']=='dynamic_forecast' else [])]
fig,axs=plt.subplots(1,3,figsize=(8.3,3.4))
cases=['water_area_mead','rgb_quicklook_mead','water_vector_rgb_mead'];names=['Area','RGB','Vector + RGB']
arms=['non_agent','fixture_agent_only','fixture_agent_wm_offload'];colors=['#616f7a','#2b83a8','#e78939']
for ax,scene,title in zip(axs,['normal','future_contact','busy_local'],['Continuous contacts','Future contact gap','Future local reservation']):
 x=np.arange(3)
 for i,(arm,col) in enumerate(zip(arms,colors)):
  v=[next(r['modeled_completion_s'] for r in allrows if r['case_id']==c and r['scenario']==scene and r['arm']==arm) for c in cases]
  ax.bar(x+(i-1)*.24,v,width=.22,color=col,label=['Non-agent recipe','Agent only','Agent + forecast/offload'][i])
 ax.set_title(title);ax.set_xticks(x,names,rotation=20);ax.set_ylabel('Modeled seconds');ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True);ax.spines[['top','right']].set_visible(False)
 if scene=='normal':ax.set_ylim(0,4.3)
 else:ax.set_ylim(0,23)
fig.legend(*axs[0].get_legend_handles_labels(),loc='lower center',ncol=3,frameon=False,bbox_to_anchor=(.5,-.05),fontsize=10)
fig.subplots_adjust(left=.07,right=.99,top=.88,bottom=.2,wspace=.42)
fig.savefig(BASE/'figures/semantic_modeled_comparison.png',dpi=240,bbox_inches='tight');plt.close(fig)
# Paired world-cluster effects: mean seconds saved, 95% percentile intervals.
sweep=json.loads((BASE/'sources/sweep_summary.json').read_text())
fig,axs=plt.subplots(1,3,figsize=(8.3,3.2))
comparisons=['offloading_without_forecast','incremental_forecast','forecast_plus_offloading']
labels=['Offload vs local','Forecast increment','Combined vs local']
for ax,fam,title in zip(axs,['normal','busy_local','future_contact'],['Continuous','Local reservation','Contact gaps']):
 for i,(comp,col) in enumerate(zip(comparisons,['#616f7a','#2b83a8','#e78939'])):
  v=next(r['seconds_saved'] for r in sweep['pooled_effects'] if r['family']==fam and r['comparison']==comp)
  ax.errorbar(v['mean'],2-i,xerr=[[v['mean']-v['ci95_low']],[v['ci95_high']-v['mean']]],fmt='o',color=col,capsize=4,markersize=5)
 ax.axvline(0,color='black',linewidth=.8);ax.set_title(title);ax.set_yticks([2,1,0],labels if fam=='normal' else []);ax.set_xlabel('Modeled seconds saved');ax.set_ylim(-.6,2.6);ax.grid(axis='x',alpha=.2);ax.spines[['top','right']].set_visible(False)
fig.subplots_adjust(left=.18,right=.98,top=.84,bottom=.24,wspace=.35)
fig.savefig(BASE/'figures/semantic_world_sweep.png',dpi=240,bbox_inches='tight');plt.close(fig)

print("Rebuilt 3 scientific figures and 2 model-comparison figures. The concept illustration is a separately supplied asset.")
