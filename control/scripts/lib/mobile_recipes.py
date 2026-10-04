"""Small ORACLE fetch Grounder for the fixed simulation development scenes.

Every arm target is planned against collision geometry by the existing gateway.
An attached collision object is only a planning representation; Isaac never
attaches the object to the gripper and must prove physical contact and lift.
"""
import copy
import math
import time

FINGER_MESH_MIN_Y = -.02600320242345333
DRIVE_ORIGIN_Y = .035
FINGER_OFFSET_Y = .035465
FINGER_OFFSET_Z = .042039


def aperture_from_joint(q):
    """Metres from the pinned xArm STL pad plane and URDF linkage, q radians."""
    if not math.isfinite(q) or not 0 <= q <= .85:
        raise ValueError('gripper_joint_out_of_bounds')
    return 2*(DRIVE_ORIGIN_Y+FINGER_MESH_MIN_Y+FINGER_OFFSET_Y*math.cos(q)-FINGER_OFFSET_Z*math.sin(q))


def joint_from_aperture(aperture):
    low,high = aperture_from_joint(.85),aperture_from_joint(0.)
    if not math.isfinite(aperture) or not low-1e-9 <= aperture <= high+1e-9:
        raise ValueError('gripper_aperture_out_of_bounds')
    radius=math.hypot(FINGER_OFFSET_Y,FINGER_OFFSET_Z)
    phase=math.atan2(FINGER_OFFSET_Z,FINGER_OFFSET_Y)
    value=(aperture/2-DRIVE_ORIGIN_Y-FINGER_MESH_MIN_Y)/radius
    return min(.85,max(0.,math.acos(value)-phase))


def oracle_scene(observation, profile, scene_config):
    if observation.get('input_source')!='ORACLE_EVALUATION_ONLY':
        raise ValueError('oracle_grounder_requires_explicit_truth_source')
    mount=profile['robot_source']['arm_mount_world']
    def local(position):return [float(p)-float(m) for p,m in zip(position,mount)]
    def box(name,position,dimensions):
        return {'id':name,'shape':'box','position':local(position),'orientation':[0.,0.,0.,1.],
                'dimensions':dimensions}
    objects=[box('table',[.60,0,.375],[1.,.8,.75]),
             box('red_cube',observation['objects']['red_cube']['position'],[.05]*3),
             {'id':'blue_cylinder','shape':'cylinder','position':local(observation['objects']['blue_cylinder']['position']),
              'orientation':[0.,0.,0.,1.],'dimensions':[.05,.025]},
             box('place_region',observation['place_pose']['position'],[.14,.14,.005])]
    for i,(position,dimensions) in enumerate([
        ((-.8,-.3,.25),(.35,.35,.5)),((1.3,-.65,.30),(.3,.3,.6)),((.7,1.,.25),(.4,.3,.5))]):
        objects.append(box('obstacle_'+str(i),position,list(dimensions)))
    for i,wall in enumerate(scene_config.get('channel',{}).get('walls',[])):
        objects.append(box('channel_wall_'+str(i),wall['position'],wall['dimensions']))
    return {'objects':objects,'attached':[{'id':'red_cube','remove':True}],
            'allow_contacts':[{'object_id':'red_cube','links':profile['gripper_touch_links'],'allowed':False}]}


def fetch_recipe(parameters, observation, profile, *, scene_config=None, task_kind='PLACE'):
    scene_config=scene_config or {}
    if parameters.get('object_id')!='red_cube':
        raise ValueError('development_grounder_only_red_cube')
    if parameters.get('place_region_id')!='tray_left':
        raise ValueError('development_grounder_only_tray_left')
    if time.monotonic()-observation['received_monotonic'] > .2:
        raise ValueError('oracle_snapshot_stale')
    if observation.get('guard_active'):
        raise ValueError('cannot_ground_while_another_stage_active')
    mount=profile['robot_source']['arm_mount_world']
    p=observation['objects']['red_cube']['position']
    place=observation['place_pose']['position']
    # TCP is at the finger tips; put it 5 mm above the table, not at the
    # block centre. The pads then enclose the 50 mm block without scraping.
    grasp=[p[0],p[1],p[2]-.020]
    lift=[grasp[0],grasp[1],grasp[2]+.14]
    dest=[place[0],place[1],place[2]+.0025+.005]
    above=[dest[0],dest[1],lift[2]]
    def arm(name,position,scene,cartesian=False):
        return {'name':name,'op':'arm','payload':{'target_pose':{
            'position':[float(x)-float(m) for x,m in zip(position,mount)],
            'orientation':[1.,0.,0.,0.]},'cartesian':cartesian,'scene':copy.deepcopy(scene)}}
    initial=oracle_scene(observation,profile,scene_config)
    touch=copy.deepcopy(initial)
    touch['allow_contacts'][0]['allowed']=True
    attached=copy.deepcopy(touch)
    attached['objects']=[row for row in attached['objects'] if row['id']!='red_cube']+[{'id':'red_cube','remove':True}]
    # Grasp centre is 20 mm above the downward pointing TCP, hence -Z in TCP.
    attached['attached']=[{'id':'red_cube','shape':'box','dimensions':[.05]*3,
        'position':[0.,0.,-.020],'orientation':[0.,0.,0.,1.],
        'frame_id':profile['tcp_frame'],'touch_links':profile['gripper_touch_links']}]
    lifting=copy.deepcopy(attached)
    lifting['support_contacts']=[{'object_id':'red_cube','support_id':'table','allowed':True}]
    stages=[{'name':'open','op':'gripper','payload':{'position':joint_from_aperture(aperture_from_joint(0.)),
                                                  'max_effort':profile['joint_limits']['drive_joint']['effort']}},
            arm('pregrasp',[grasp[0],grasp[1],grasp[2]+.15],initial),
            arm('descend',grasp,touch,True),
            {'name':'close','op':'gripper','payload':{'position':joint_from_aperture(aperture_from_joint(.85)),
                                                   'max_effort':profile['joint_limits']['drive_joint']['effort']}},
            {**arm('lift',lift,lifting,True),'settle_sim_seconds':1.1}]
    if task_kind=='G':
        return stages
    if task_kind=='C':
        channel=scene_config['channel']
        middle_x=sum(w['position'][0] for w in channel['walls'])/2
        stages.extend([arm('channel_entry',[middle_x,-.05,lift[2]],attached),
                       arm('channel_traverse',[middle_x,.23,lift[2]],attached,True)])
    stages.append(arm('transport',above,attached))
    placing=copy.deepcopy(attached)
    placing['support_contacts']=[{'object_id':'red_cube','support_id':'place_region','allowed':True}]
    stages.append(arm('place',dest,placing,True))
    stages.append({'name':'release','op':'gripper','payload':{'position':0.,
        'max_effort':profile['joint_limits']['drive_joint']['effort']}})
    detached=copy.deepcopy(initial)
    for row in detached['objects']:
        if row['id']=='red_cube':
            row['position']=[dest[i]-mount[i]+(.02 if i==2 else 0) for i in range(3)]
    detached['allow_contacts'][0]['allowed']=True
    stages.append({**arm('retract',above,detached,True),'settle_sim_seconds':1.1})
    return stages
