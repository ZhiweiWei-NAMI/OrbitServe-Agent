#!/usr/bin/env python3
"""Required A16 evidence: unavailable learned modes fail, without rule fallback."""
import json
from orbitserve.forecast import forecast
from orbitserve.planning import plan
from orbitserve.policy import choose_action

checks=[]
for label,invoke in [
    ('learned_wm',lambda:forecast([],[],120,source_kind='learned_wm')),
    ('llm_planner',lambda:plan({'planner_mode':'llm'},{},{},{})),
    ('marl_policy',lambda:choose_action({'policy_mode':'marl'},{},[],{})),
]:
    try:
        invoke()
    except NotImplementedError as e:
        checks.append({'mode':label,'status':'PASS','exception':type(e).__name__,'message':str(e)})
    else:
        raise RuntimeError(label+' silently returned instead of failing')
print(json.dumps({'status':'PASS','checks':checks},indent=2))
