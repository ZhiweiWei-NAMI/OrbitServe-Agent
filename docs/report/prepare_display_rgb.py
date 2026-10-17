"""Rebuild the display-only before RGB from the frozen real input cache."""
from pathlib import Path
import argparse,json
import numpy as np
from orbitserve import science
ap=argparse.ArgumentParser();ap.add_argument('--data-root',required=True);ap.add_argument('--output',default=str(Path(__file__).parent/'sources/mead_before_rgb_for_display.npz'));a=ap.parse_args()
case=Path(a.data_root)/'cases/lake_mead';task=json.loads((case/'task.json').read_text());assets=json.loads((case/'assets.json').read_text())['assets']
window=(0,0,task['height'],task['width']);v=[]
for band in ['B04','B03','B02']:
 asset=science._asset(assets,band,'before');values,_=science._read_asset(case,asset,task,window);v.append(values)
valid=np.logical_and.reduce([np.isfinite(x) for x in v]);rgba=science._display_rgb(*v,valid)
out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);np.savez_compressed(out,rgb=rgba[:3],valid=valid)
print(out)
