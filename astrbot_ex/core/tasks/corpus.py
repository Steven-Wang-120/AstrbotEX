"""Deterministic synthetic semantic labels; no simulation truth or model labels."""
from __future__ import annotations
import copy
import itertools
import json
import random
from .contracts import digest, skill_catalog

COUNTS = {
    "move": (14,3,3), "fetch": (14,3,3), "move_fetch": (14,3,3),
    "missing_place": (7,2,1), "ambiguous": (7,1,2),
    "unsupported": (7,1,2), "missing_observation": (7,2,1),
}
SPLITS = ("train", "validation", "test")
COLORS = (("红", "red"), ("蓝", "blue"), ("绿", "green"))

def _group(category, index, split, *, development=False):
    prefix = "DEV" if development else "T1"
    gid = f"{prefix}-{category}-{split}-{index:02d}"
    color_zh, color = COLORS[index % 3]
    shape_zh, shape = ("方块", "cube") if index % 2 == 0 else ("圆柱", "cylinder")
    region_zh, region = ("左托盘", "tray_left") if index % 2 == 0 else ("右托盘", "tray_right")
    oid = "obj-a"
    # A layout key denotes synthetic state variation; it is not an Isaac evaluation seed.
    observation = {"observation_id": gid + "-obs", "valid": True, "age_ms": 10,
        "max_age_ms": 500, "at_table": True, "stationary": True,
        "objects": [{"id": oid, "color": color, "shape": shape},
                    {"id": "obj-b", "color": COLORS[(index+1)%3][1], "shape": shape}]}
    phrase = {
        "train": f"把{color_zh}色{shape_zh}放到{region_zh}",
        "validation": f"将{color_zh}色{shape_zh}搬到{region_zh}",
        "test": f"抓取{color_zh}色{shape_zh}，目的地是{region_zh}",
        "development": f"把{color_zh}色{shape_zh}夹起后放到{region_zh}",
    }[split]
    gold = {"accepted_options": ["fetch:"+oid], "skill": "fetch", "object_id": oid,
            "region_id": region, "success_template": "fetch.pick_place_stable.v1"}
    reason = "目标唯一、放置区明确、观测有效且底盘停稳。"
    if category == "move":
        phrase = {"train":"移动到起点区", "validation":"前往起点区", "test":"去起点区", "development":"开到起点区"}[split]
        gold = {"accepted_options":["move:home"], "skill":"move", "object_id":None,
                "region_id":"home", "success_template":"move.arrive_and_stop.v1"}
        reason = "明确底盘移动到命名起点区，不调用机械臂。"
    elif category == "move_fetch":
        observation["at_table"] = False
        gold = {"accepted_options":["move:table_dock"], "skill":"move", "object_id":None,
                "region_id":"table_dock", "success_template":"move.arrive_and_stop.v1"}
        reason = "物体和放置区已说明；先移动到显式已知操作台区域，到位后再重新观察。"
    elif category == "missing_place":
        phrase = {"train":f"抓取{color_zh}色{shape_zh}", "validation":f"把{color_zh}色{shape_zh}夹起来",
                  "test":f"搬运{color_zh}色{shape_zh}", "development":f"抓起{color_zh}色{shape_zh}"}[split]
        gold = {"accepted_options":["clarification"], "skill":None, "object_id":None, "region_id":None, "success_template":None}
        reason = "缺少放置位置，必须追问；不能先移动。"
    elif category == "ambiguous":
        observation["objects"][1].update(color=color, shape=shape)
        gold = {"accepted_options":["clarification"], "skill":None, "object_id":None, "region_id":region, "success_template":None}
        reason = "两个对象颜色与类别相同，未给对象编号，不可任意选择。"
    elif category == "unsupported":
        phrase = {"train":"跳舞", "validation":"唱一首歌", "test":"给我画画", "development":"帮我做饭"}[split]
        gold = {"accepted_options":["unsupported"], "skill":None, "object_id":None, "region_id":None, "success_template":None}
        reason = "求不属于已声明的move/fetch技能。"
    elif category == "missing_observation":
        observation.update(valid=False, age_ms=2000)
        gold = {"accepted_options":["wait"], "skill":None, "object_id":None, "region_id":region, "success_template":None}
        reason = "任务完整，但观测已过期；等待新观测，不提交运动。"
    if category in {"fetch", "move_fetch", "ambiguous", "missing_observation"}:
        aliases = [f"把{color_zh}色{shape_zh}放到{region_zh}", f"在{region_zh}上放{color_zh}色{shape_zh}"]
        phrase = aliases[index % 2] if development else aliases[0]
    elif category == "move":
        aliases = [phrase, "到起点区去"]
    elif category == "missing_place":
        aliases = [phrase, f"把{color_zh}色{shape_zh}抓起来"]
    else:
        aliases = [phrase]
    point = {"text":phrase, "paraphrases":list(dict.fromkeys(aliases)), "observation":observation, "gold":gold, "label_reason":reason}
    points = [point]
    if category == "move_fetch":
        later = copy.deepcopy(point)
        later["observation"].update(at_table=True, observation_id=gid+"-after-move")
        later["gold"] = {"accepted_options":["fetch:"+oid], "skill":"fetch", "object_id":oid,
                         "region_id":region, "success_template":"fetch.pick_place_stable.v1"}
        later["label_reason"] = "同一任务后续决策点：移动已结束，资源释放、停车和新观测成立。"
        points.append(later)
    rng = random.Random(int(digest({"source":"T1-synthetic-scene", "group":gid})[:16],16))
    scene_definition = {"objects": [{"id":o["id"],"color":o["color"],"shape":o["shape"],
        "xy_m":[round(rng.uniform(-.30,.30),5),round(rng.uniform(.10,.55),5)]} for o in observation["objects"]],
        "tray_centers_m":{"tray_left":[-.22,.4],"tray_right":[.22,.4]},
        "robot_zone":"table_dock" if observation["at_table"] else "home"}
    return {"scene_definition":scene_definition, "group_id":gid, "category":category, "split":split, "source":"SYNTHETIC",
            "language_family":gid + "-paraphrases", "layout_family":gid,
            "synthetic_layout_id":digest(scene_definition),
            "catalog_revision":skill_catalog()["revision"], "decision_points":points}

def semantic_task_key(point):
    """Task/scene identity from MODEL-VISIBLE meaning, not IDs or random geometry.

    IDs obj-a/obj-b are stable binding roles. Observation IDs, timestamps, scene
    nonce, candidate order and paraphrase wording do not create new tasks.
    """
    from .contracts import COLORS as WORD_COLORS, SHAPES, REGIONS
    text=point['text'];obs=point['observation']
    fetch=any(word in text for word in ('抓','夹','搬','放'))
    move=any(word in text for word in ('移动','前往','开到','去')) and not fetch
    task={'kind':'fetch' if fetch else 'move' if move else 'unsupported',
          'colors':sorted(v for k,v in WORD_COLORS.items() if k in text) if fetch else [],
          'shapes':sorted(v for k,v in SHAPES.items() if k in text) if fetch else [],
          'regions':sorted(v for k,v in REGIONS.items() if k in text),
          'precision':'精确' in text,'yaw':'朝向' in text,
          'explicit_object_ids':sorted(o['id'] for o in obs['objects'] if o['id'] in text)}
    if not fetch and not move:task['unsupported_text']=text
    visible={'objects':sorted(obs['objects'],key=lambda o:o['id']),
             **{k:obs.get(k) for k in ('at_table','stationary','valid','task_success_proven')},
             'observation_fresh':obs.get('age_ms',float('inf'))<=obs.get('max_age_ms',500)}
    return digest({'task':task,'visible_scene':visible})


def actual_model_input_key(point, alias=None):
    """Actual serialized model state + semantic options; ignore letter ordering."""
    from .session import TaskSession
    from .laya_client import TaskLayaClient
    context,_=TaskSession().feed(alias or point['text'],point['observation'])
    client=TaskLayaClient()
    try:body,_,_=client.request_payload(context)
    finally:client.close()
    request=json.loads(body);state=json.loads(request['state'])
    if 'objects' in state.get('observation',{}):
        state['observation']['objects']=sorted(state['observation']['objects'],key=lambda o:o['id'])
    return digest({'state':state,'options':sorted(request['questions']['task']['criteria'].values())})


def _v2_candidate(category, values, split, index):
    ci,si,ri,role,di,ds,precise=values
    color_zh,color=COLORS[ci];shape_zh,shape=(('方块','cube'),('圆柱','cylinder'))[si]
    tray_zh,tray=(('左托盘','tray_left'),('右托盘','tray_right'))[ri]
    target_id=('obj-a','obj-b')[role];other_id=('obj-b','obj-a')[role]
    distractor={'id':other_id,'color':COLORS[di][1],'shape':('cube','cylinder')[ds]}
    if category=='ambiguous':distractor.update(color=color,shape=shape)
    elif distractor['color']==color and distractor['shape']==shape:return None
    gid=f'T1v2-{category}-{split}-{index:02d}'
    obs={'observation_id':gid+'-obs','valid':True,'age_ms':10,'max_age_ms':500,
         'at_table':category!='move_fetch','stationary':True,
         'objects':sorted([{'id':target_id,'color':color,'shape':shape},distractor],key=lambda o:o['id'])}
    prefix='精确' if precise else ''
    aliases=[f'{prefix}把{color_zh}色{shape_zh}放到{tray_zh}',f'{prefix}在{tray_zh}上放{color_zh}色{shape_zh}']
    gold={'accepted_options':['fetch:'+target_id],'skill':'fetch','object_id':target_id,
          'region_id':tray,'success_template':'fetch.pick_place_stable.v1'}
    reason='目标唯一、放置区明确、观测有效且底盘停稳。'
    if category=='move':
        region_zh,region=(('起点区','home'),('操作台停车区','table_dock'))[ri]
        aliases=[prefix+'移动到'+region_zh,prefix+'到'+region_zh+'去']
        obs['at_table']=bool(si)
        gold={'accepted_options':['move:'+region],'skill':'move','object_id':None,'region_id':region,
              'success_template':'move.arrive_and_stop.v1'}
        reason='明确移动到命名区域，使用任务中指定的标准或精确档。'
    elif category=='move_fetch':
        gold={'accepted_options':['move:table_dock'],'skill':'move','object_id':None,'region_id':'table_dock',
              'success_template':'move.arrive_and_stop.v1'}
        reason='目标与放置区完整；先到已知操作台区域，再观察并绑定对象。'
    elif category=='missing_place':
        aliases=[prefix+f'抓取{color_zh}色{shape_zh}',prefix+f'把{color_zh}色{shape_zh}抓起来']
        gold={'accepted_options':['clarification'],'skill':None,'object_id':None,'region_id':None,'success_template':None}
        reason='缺放置区，先追问，不移动。'
    elif category=='ambiguous':
        gold={'accepted_options':['clarification'],'skill':None,'object_id':None,'region_id':tray,'success_template':None}
        reason='两个同色同形对象均符合描述，必须明确对象编号。'
    elif category=='unsupported':
        aliases=[('跳舞','唱一首歌','给我画画')[ci]]
        gold={'accepted_options':['unsupported'],'skill':None,'object_id':None,'region_id':None,'success_template':None}
        reason='请求不属于move/fetch技能目录。'
    elif category=='missing_observation':
        obs.update(valid=False,age_ms=2000)
        gold={'accepted_options':['wait'],'skill':None,'object_id':None,'region_id':tray,'success_template':None}
        reason='任务完整，但观测过期，等待有效新观测。'
    point={'text':aliases[0],'paraphrases':aliases,'observation':obs,'gold':gold,'label_reason':reason}
    points=[point]
    if category=='move_fetch':
        later=copy.deepcopy(point);later['observation'].update(at_table=True,observation_id=gid+'-after-move')
        later['gold']={'accepted_options':['fetch:'+target_id],'skill':'fetch','object_id':target_id,'region_id':tray,
                       'success_template':'fetch.pick_place_stable.v1'}
        later['label_reason']='同任务到位后的新观测，底盘已停稳，绑定请求对象。';points.append(later)
    rng=random.Random(int(digest({'source':'T1v2-scene','group':gid})[:16],16))
    scene={'objects':[dict(o,xy_m=[round(rng.uniform(-.30,.30),5),round(rng.uniform(.10,.55),5)]) for o in obs['objects']],
           'tray_centers_m':{'tray_left':[-.22,.4],'tray_right':[.22,.4]},'robot_zone':'table_dock' if obs['at_table'] else 'home'}
    return {'dataset_version':2,'scene_definition':scene,'group_id':gid,'category':category,'split':split,'source':'SYNTHETIC',
            'language_family':gid+'-paraphrases','layout_family':gid,'synthetic_layout_id':digest(scene),
            'catalog_revision':skill_catalog()['revision'],'decision_points':points,
            'semantic_task_keys':[semantic_task_key(p) for p in points]}


def build_corpus():
    # Reserve every original DEV point before allocation. Those requests were
    # already observed, so they can never become an independent test example.
    used={semantic_task_key(p) for group in development_probes() for p in group['decision_points']}
    pool=list(itertools.product(range(3),range(2),range(2),range(2),range(3),range(2),range(2)))
    random.Random(20261004).shuffle(pool)
    groups=[]
    for category,counts in COUNTS.items():
        for split,count in zip(SPLITS,counts):
            for index in range(count):
                selected=None
                for values in pool:
                    # Alternate bound target role; letter shuffling alone cannot
                    # remove the shortcut 'always fetch obj-a'.
                    if values[3]!=(index%2):continue
                    candidate=_v2_candidate(category,values,split,index)
                    if candidate is None:continue
                    keys=candidate['semantic_task_keys']
                    if any(key in used for key in keys):continue
                    selected=candidate;used.update(keys);break
                if selected is None:raise ValueError('insufficient_distinct_semantic_tasks:'+category+':'+split)
                groups.append(selected)
    return groups

def review_samples():
    return [_group(category, i, "train") for category in COUNTS for i in range(2)]

def development_probes():
    return [_group(category, i, "development", development=True)
            for category in COUNTS for i in range(3 if category != "unsupported" else 2)]

def validate_corpus(groups):
    from .session import TaskSession
    if len(groups) != 100 or len({g["group_id"] for g in groups}) != 100:
        raise ValueError("expected_100_independent_groups")
    counts = {split:sum(g["split"] == split for g in groups) for split in SPLITS}
    if counts != dict(zip(SPLITS,(70,15,15))):
        raise ValueError("wrong_split")
    for field in ("language_family", "layout_family", "synthetic_layout_id"):
        owners = {}
        for group in groups:
            key = group[field]
            if key in owners and owners[key] != group["split"]:
                raise ValueError("cross_split_family_leak")
            owners[key] = group["split"]
    decisions = 0
    for group in groups:
        if digest(group["scene_definition"]) != group["synthetic_layout_id"]:
            raise ValueError("layout_identity_mismatch")
        if group["source"] != "SYNTHETIC" or group["catalog_revision"] != skill_catalog()["revision"]:
            raise ValueError("invalid_source_or_catalog")
        for point in group["decision_points"]:
            context, status = TaskSession().feed(point["text"], point["observation"])
            allowed = {o["option_id"] for o in context.options}
            if not set(point["gold"]["accepted_options"]) <= allowed:
                raise ValueError("gold_option_missing")
            if not point["label_reason"]:
                raise ValueError("missing_label_reason")
            if any(k in context.observation for k in ("gold", "actual_result", "truth_error")):
                raise ValueError("label_leaked_into_input")
            if "请" in point["text"] or any("请" in alias for alias in point["paraphrases"]):
                raise ValueError("politeness_prefix_not_allowed")
            for alias in point["paraphrases"]:
                alias_context, alias_status = TaskSession().feed(alias, point["observation"])
                if alias_status["state"] != status["state"]:
                    raise ValueError("paraphrase_changed_semantics")
            decisions += 1
    semantic_owners={};actual_owners={}
    dev_semantic={semantic_task_key(p) for g in development_probes() for p in g['decision_points']}
    dev_actual={actual_model_input_key(p,alias) for g in development_probes() for p in g['decision_points'] for alias in p['paraphrases']}
    fetch_roles={}
    for group in groups:
        for point in group['decision_points']:
            key=semantic_task_key(point)
            if key in dev_semantic:raise ValueError('development_semantic_leak')
            if key in semantic_owners and semantic_owners[key]!=group['group_id']:
                raise ValueError('duplicate_semantic_task_between_groups')
            semantic_owners[key]=group['group_id']
            if point['gold'].get('skill')=='fetch':
                role=point['gold']['object_id'];fetch_roles[role]=fetch_roles.get(role,0)+1
            for alias in point['paraphrases']:
                actual=actual_model_input_key(point,alias)
                if actual in dev_actual:raise ValueError('development_actual_input_leak')
                owner=actual_owners.get(actual)
                if owner is not None and owner!=group['group_id']:raise ValueError('duplicate_actual_model_input')
                actual_owners[actual]=group['group_id']
    if set(fetch_roles)!={'obj-a','obj-b'} or min(fetch_roles.values())<10:
        raise ValueError('target_object_role_shortcut')
    return {"dataset_version":2,"groups":100, "split_counts":counts, "decision_points":decisions,
            "unique_semantic_tasks":len(semantic_owners),"unique_actual_model_inputs":len(actual_owners),
            "cross_group_semantic_duplicates":0,"cross_group_actual_duplicates":0,"development_overlap":0,
            "fetch_target_roles":fetch_roles,
            "split_hash":digest([{k:g[k] for k in ("group_id","split","language_family","layout_family")} for g in groups]),
            "corpus_hash":digest(groups), "source":"SYNTHETIC", "physical_runs":0}
