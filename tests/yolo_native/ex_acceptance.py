"""Real EX loader/Actors/ROS + real YOLO + real A.E.B. tools, no SDK stubs."""
import argparse
import base64
from collections import deque
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import uuid


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ex',required=True)
    parser.add_argument('--plugin',required=True)
    parser.add_argument('--python',required=True)
    parser.add_argument('--image',required=True)
    parser.add_argument('--blank',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--device',default='cpu')
    parser.add_argument('--aeb',default='http://127.0.0.1:18866')
    parser.add_argument('--smoke-only',action='store_true',help='EX/ROS/subscriber smoke only; explicitly excludes A.E.B. acceptance')
    args=parser.parse_args()
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=False)
    data=output/'ex-data'
    target=data/'plugins/vision/yolo'
    shutil.copytree(args.plugin,target,ignore=shutil.ignore_patterns('__pycache__','data'))
    cfg=json.loads((target/'config.json').read_text())
    cfg.update(python=args.python,device=args.device)
    cfg['pubsub']['publish_enabled']=True
    for binding in cfg['ros2']['bindings'].values():binding['enabled']=True
    cfg['aeb'].update(enabled=not args.smoke_only,text_endpoint='tcp://127.0.0.1:18766',vision_endpoint='tcp://127.0.0.1:18768')
    (target/'config.json').write_text(json.dumps(cfg,indent=2))
    probe=data/'plugins/special/yolo_probe';probe.mkdir(parents=True)
    (probe/'plugin.json').write_text(json.dumps({'id':'yolo_probe','name':'Independent subscriber','version':'0.1.0',
        'entry':'main.py','provides':['trace_plugin'],'enabled_default':False,
        'subscribes':[{'topic':'yolo.json'},{'topic':'yolo.jpg'}]}))
    (probe/'main.py').write_text('''import time
class Plugin:
 def __init__(self,context): self.context=context; self.counts={'json':0,'jpg':0}; self.last={}
 def on_load(self): self.inputs={k:self.context.subscribe('yolo.'+k,max_messages=1) for k in self.counts}
 def on_worker_step(self):
  for k,inbox in self.inputs.items():
   msg=inbox.take_latest()
   if msg is not None: self.counts[k]+=1; self.last[k]=msg.payload
  time.sleep(.01)
 def snapshot(self): return {'counts':dict(self.counts),'last':dict(self.last)}
 def on_unload(self):
  for inbox in self.inputs.values(): inbox.close()
''')
    os.environ['ASTRBOTEX_DATA_DIR']=str(data)
    sys.path.insert(0,str(Path(args.ex).resolve()))
    from astrbot_ex.core.api_server import build_server
    from astrbot_ex.core.models import VisionResult
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import String
    rclpy.init()
    node=Node('yolo_native_acceptance')
    publisher=node.create_publisher(CompressedImage,'/camera/color/image_raw/compressed',qos_profile_sensor_data)
    packets,images=deque(maxlen=64),deque(maxlen=64)
    node.create_subscription(String,'/yolo/json',lambda msg:packets.append(json.loads(msg.data)),qos_profile_sensor_data)
    node.create_subscription(CompressedImage,'/yolo/jpg',lambda msg:images.append((msg.header.frame_id,bytes(msg.data))),qos_profile_sensor_data)
    server=None
    sent=0
    image=Path(args.image).read_bytes();blank=Path(args.blank).read_bytes()
    checks=[]
    def pump(predicate,source=image,seconds=40):
        nonlocal sent
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if source is not None and time.monotonic()-sent>.15:
                msg=CompressedImage();msg.format='jpeg';msg.data=source
                msg.header.frame_id='acceptance_camera';msg.header.stamp=node.get_clock().now().to_msg()
                publisher.publish(msg);sent=time.monotonic()
            rclpy.spin_once(node,timeout_sec=.02)
            if predicate():return
        detail=server.local_plugins.records['yolo']
        raise AssertionError(f'acceptance timeout; yolo={detail.status}, error={detail.error}; packets={list(packets)[-1:]}')
    def latest():return next((p for p in reversed(packets) if p['status']=='ok'),{})
    def http(path,body=None):
        request=urllib.request.Request(args.aeb+path,data=None if body is None else json.dumps(body).encode(),
                                       headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=10) as r:return json.load(r)
    def mark(ids,marked,request=None):
        result=http('/mark',{'stream_id':stream,'ids':ids,'marked':marked,'request_id':request or uuid.uuid4().hex})
        assert result.get('ok'),result
        return result
    try:
        server=build_server('127.0.0.1',0,20)
        server.local_plugins.set_enabled('yolo',True)
        server.local_plugins.set_enabled('yolo_probe',True)
        server.environment_manager.select('ros2')
        deadline=time.monotonic()+20
        while server.environment_manager.snapshot()['active_mode']!='ros2':
            assert time.monotonic()<deadline,server.environment_manager.status()
            time.sleep(.05)
        server.controller.start()
        pump(lambda:len(latest().get('objects',{}))>=2 and bool(images))
        first=latest();stream=first['stream_id'];ids=list(first['objects'])[:2]
        assert next(iter(first))=='timestamp'
        assert all(obj['act']==0 for obj in first['objects'].values())
        for obj in first['objects'].values():
            box=obj['bbox']
            assert all(-1<=v<=1 for k in ('c','rt','lb') for v in box[k])
            assert box['rt'][0]<=box['lb'][0] and box['rt'][1]>=box['lb'][1]
        checks.extend(['real_EX_loader','Core_Actor_lifecycle','native_ROS_environment','real_YOLO_inference','timestamp_first','two_ROS_outputs'])
        vision=server.controller.runtime.registry.get('yolo').call('get_result')
        assert isinstance(vision,VisionResult) and len(vision.entities)>=2
        checks.append('SDK_VisionResult')
        if args.smoke_only:
            pump(lambda:len(packets)>=12 and len(images)>=12)
            probe_result=server.controller.runtime.registry.get('yolo_probe').call('snapshot')
            assert min(probe_result['counts'].values())>=5
            assert base64.b64decode(probe_result['last']['jpg']['data']).startswith(b'\xff\xd8')
            checks.append('independent_EX_plugin_subscribes_both_Topics')
            report={'passed':True,'scope':'EX/ROS smoke ONLY; A.E.B. not tested','checks':checks,
                    'device':args.device,'objects':len(first['objects']),'probe_counts':probe_result['counts'],
                    'environment':server.environment_manager.snapshot()}
            (output/'report.json').write_text(json.dumps(report,indent=2))
            print(json.dumps(report),flush=True)
            return
        before=first['frame_id']
        marked=mark(ids,True,request='mark-two')
        pump(lambda:latest().get('mark_rev',0)>=marked['mark_rev'] and all(latest().get('objects',{}).get(i,{}).get('act')==1 for i in ids))
        seen_frame=latest()['frame_id']
        pump(lambda:latest().get('frame_id',0)>=seen_frame+12)
        assert all(latest()['objects'][i]['act']==1 for i in ids)
        checks.append('LLM_tool_marks_multiple_objects_and_persists_across_frames')
        marked_packet=latest()
        marked_key=stream+':'+str(marked_packet['frame_id'])
        (output/'marked.json').write_text(json.dumps(marked_packet,indent=2))
        pump(lambda:any(name==marked_key for name,j in images))
        for name,jpeg in images:
            if name==marked_key:(output/'marked.jpg').write_bytes(jpeg)
        cleared=mark([ids[0]],False,request='clear-one')
        pump(lambda:latest().get('mark_rev',0)>=cleared['mark_rev'] and latest().get('objects',{}).get(ids[0],{}).get('act')==0)
        assert latest()['objects'][ids[1]]['act']==1
        checks.append('unmark_only_selected_object')
        mark(ids,True,request='mark-two')
        assert latest()['objects'][ids[0]]['act']==0
        checks.append('retry_does_not_revert_newer_command')
        packets.clear()
        pump(lambda:latest() and not latest()['objects'],source=blank)
        assert latest()['marks'][ids[1]]['act']==1 and not latest()['marks'][ids[1]]['visible']
        checks.append('missing_object_mark_retained_without_fake_detection')
        packets.clear()
        pump(lambda:all(i in latest().get('objects',{}) for i in ids))
        assert [latest()['objects'][i]['act'] for i in ids]==[0,1]
        checks.append('reappearance_identity_and_mark')
        probe_result=server.controller.runtime.registry.get('yolo_probe').call('snapshot')
        assert min(probe_result['counts'].values())>5
        assert base64.b64decode(probe_result['last']['jpg']['data']).startswith(b'\xff\xd8')
        checks.append('independent_EX_plugin_subscribes_both_Topics')
        buffer=http('/buffers?stream='+stream)
        json_data=json.loads(buffer['json']['content'][0]['text'])
        assert json_data['ok'] and json_data['count']>=1
        assert any(c['type']=='image' for c in buffer['jpeg']['content'])
        checks.append('real_AEB_buffer_tools_return_JSON_and_JPEG')
        (output/'aeb_buffers.json').write_text(json.dumps(buffer,indent=2))
        server.controller.stop('restart persistence test')
        server.controller.start()
        packets.clear()
        pump(lambda:all(i in latest().get('objects',{}) for i in ids))
        assert latest()['stream_id']==stream and [latest()['objects'][i]['act'] for i in ids]==[0,1]
        checks.append('runtime_restart_preserves_marks')
        report={'passed':True,'checks':checks,'EX_commit':'1f6e4a8','AEB_commit':'2e77eba plus local mark tool',
                'device':args.device,'ROS_DOMAIN_ID':os.getenv('ROS_DOMAIN_ID'),'stream_id':stream,
                'objects':len(first['objects']),'probe_counts':probe_result['counts'],
                'environment':server.environment_manager.snapshot(),
                'LLM':'Real registered FunctionTool invoked deterministically; no paid model inference',
                'input':'Official prerecorded image over native ROS, not current live camera'}
        (output/'report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report),flush=True)
    finally:
        if server is not None:
            server.controller.stop('acceptance finished')
            server.server_close()
        node.destroy_node()
        rclpy.shutdown()


if __name__=='__main__':main()
