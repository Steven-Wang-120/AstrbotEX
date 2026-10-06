"""Native AstrbotEX 1f6e4a8 plugin: Core Actor + context.ros + VisionResult."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import sys
import time
import numpy as np
from astrbot_ex.core.models import Entity, VisionResult

HERE=Path(__file__).resolve().parent
PACKAGE='_ex_yolo'
if PACKAGE not in sys.modules:
    spec=importlib.util.spec_from_file_location(PACKAGE,HERE/'__init__.py',submodule_search_locations=[str(HERE)])
    module=importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE]=module
    spec.loader.exec_module(module)
from _ex_yolo.client import InferenceClient
from _ex_yolo.state import State
from _ex_yolo.aeb import AEBForwarder


class Plugin:
    id='yolo'
    name='YOLOv8 segmentation'
    def __init__(self,context):
        self.context=context
        self.config=context.config
        self.client=None
        self.forwarder=None
        self.state=None
        self.running=False
        self.last=None
        self.pending=None
        self.inboxes=[]

    def on_load(self):
        cfg=self.config
        path=Path(cfg.get('state_path','data/state.sqlite3'))
        self.state=State(path if path.is_absolute() else HERE/path)
        self.image=self.context.ros.subscribe('image')
        self.mark_input=self.context.ros.subscribe('mark')
        self.json_output=self.context.ros.publisher('json')
        self.jpg_output=self.context.ros.publisher('jpg')
        for subscription in cfg.get('pubsub',{}).get('subscriptions',[]):
            self.inboxes.append(self.context.subscribe(subscription['topic'],max_messages=32))

    def on_runtime_start(self):
        if self.running:return
        cfg=self.config
        model=HERE/cfg.get('model','models/yolov8n-seg.pt')
        if not model.resolve().is_relative_to((HERE/'models').resolve()):
            raise ValueError('Model weights must be bundled under this plugin models/ directory')
        if not model.is_file():raise FileNotFoundError(model)
        state_path=self.state.db.execute('PRAGMA database_list').fetchone()[2]
        self.client=InferenceClient(cfg.get('python',sys.executable),str(model),cfg.get('device','cpu'),
                                    cfg.get('conf',.35),cfg.get('imgsz',640),cfg.get('max_det',50),
                                    cfg.get('worker_timeout',60),state_path=state_path)
        try:
            self.client.start()
            aeb=cfg.get('aeb',{})
            if aeb.get('enabled'):
                self.forwarder=AEBForwarder(aeb['text_endpoint'],aeb['vision_endpoint'],self.state.stream_id,
                                            cfg.get('max_age',2),mark_handler=self.state.mark)
            self.running=True
            self.last_received=time.monotonic()
            self.stale_sent=False
        except BaseException:
            self.on_runtime_stop('startup failed')
            raise

    def _publish(self,packet,jpeg=b''):
        self.last=copy.deepcopy(packet)
        ps=self.config.get('pubsub',{})
        enabled=set(ps.get('enabled_topics',[])) if ps.get('publish_enabled',False) else set()
        metadata={key:packet[key] for key in ('timestamp','stream_id','frame_id')}
        if 'yolo.json' in enabled:
            self.context.topic_bus.publish_payload('yolo.json',timestamp=packet['timestamp'],source=self.id,
                payload=copy.deepcopy(packet),seq=packet['frame_id'],ttl_ms=int(self.config.get('max_age',2)*1000))
        if jpeg and 'yolo.jpg' in enabled:
            # Core TopicBus is JSON-serializable; ROS and A.E.B. keep actual bytes.
            self.context.topic_bus.publish_payload('yolo.jpg',timestamp=packet['timestamp'],source=self.id,
                payload={**metadata,'content_type':'image/jpeg','encoding':'base64','data':base64.b64encode(jpeg).decode()},
                seq=packet['frame_id'],ttl_ms=int(self.config.get('max_age',2)*1000))
        if self.json_output.status()['resource_created']:
            message=self.json_output.new_message()
            message.data=json.dumps(packet,ensure_ascii=False,allow_nan=False)
            self.json_output.publish(message)
        if jpeg and self.jpg_output.status()['resource_created']:
            message=self.jpg_output.new_message()
            message.format='jpeg'
            message.header.frame_id=packet['stream_id']+':'+str(packet['frame_id'])
            message.header.stamp.sec=int(packet['timestamp'])
            message.header.stamp.nanosec=int((packet['timestamp']%1)*1e9)
            message.data=jpeg
            self.jpg_output.publish(message)
        if self.forwarder:self.forwarder.submit(packet,jpeg)

    def _empty(self,status,error=None):
        revision,states,marks=self.state.snapshot()
        result={'timestamp':time.time(),'stream_id':self.state.stream_id,'frame_id':self.state.next_frame(),
                'status':status,'mark_rev':revision,'marks':marks,'objects':{}}
        if error:result['error']=error
        return result

    def on_worker_step(self):
        if not self.running:return
        try:
            if self.forwarder:self.forwarder.tick()
            controls=[]
            for _ in range(8):
                mark=self.mark_input.get_nowait()
                if mark is None:break
                try:controls.append(json.loads(mark.message.data))
                except (ValueError,TypeError):pass
            for inbox in self.inboxes:
                for _ in range(8):
                    mark=inbox.get_nowait()
                    if mark is None:break
                    controls.append(mark.payload)
            for command in controls:
                try:self.state.mark(command)
                except (ValueError,TypeError) as exc:
                    self.context.event_bus.emit('yolo_mark_rejected',str(exc),severity='warning')
            incoming=self.image.take_latest()
            now=time.monotonic()
            max_age=self.config.get('max_age',2)
            if incoming:
                message=incoming.message
                age=max(0,(time.monotonic_ns()-incoming.received_monotonic_ns)/1e9)
                if age<=max_age and 0<len(message.data)<=8*1024*1024:
                    self.last_received=now-age
                    self.stale_sent=False
                    metadata={'timestamp':time.time()-age,'stream_id':self.state.stream_id,'frame_id':self.state.next_frame(),
                              'source_stamp':message.header.stamp.sec+message.header.stamp.nanosec/1e9,
                              'source_frame':message.header.frame_id}
                    self.pending=(np.frombuffer(message.data,dtype=np.uint8).copy(),metadata,now-age)
            result=self.client.poll()
            if result:
                packet,jpeg=result
                if packet.get('mark_rev',self.state.revision)!=self.state.revision:
                    pass  # Mark changed during inference: never publish an outdated mark state.
                elif time.time()-packet['timestamp']<=max_age:
                    self._publish(packet,jpeg)
            if self.pending and self.client.ready:
                image,metadata,received=self.pending
                self.pending=None
                if now-received<=max_age:self.client.submit(image,metadata)
            if now-self.last_received>max_age and not self.stale_sent:
                self._publish(self._empty('stale'))
                self.stale_sent=True
            time.sleep(.005)
        except Exception as exc:
            try:self._publish(self._empty('error',str(exc)[:500]))
            finally:self.on_runtime_stop('inference error')
            raise

    def get_result(self):
        packet=copy.deepcopy(self.last) if self.last else self._empty('waiting')
        if time.time()-packet['timestamp']>self.config.get('max_age',2):
            packet.update(status='stale',objects={})
        revision,states,marked=self.state.snapshot(packet['objects'])
        packet.update(mark_rev=revision,marks=marked)
        for oid,obj in packet['objects'].items():obj['act']=states.get(oid,0)
        entities=[Entity(id=obj['id'],type=obj['name'],semantic=obj['name'],confidence=obj['conf'],
                         position=tuple(obj['bbox']['c']),bbox_px=tuple(obj['bbox']['px']),
                         metadata={'color':obj['color'],'act':obj['act'],'bbox':obj['bbox']})
                  for obj in packet['objects'].values()]
        return VisionResult(frame_id=packet['frame_id'],timestamp=packet['timestamp'],entities=entities,metadata=packet)

    def on_runtime_stop(self,reason='stopped'):
        self.running=False
        if self.client:self.client.stop();self.client=None
        if self.forwarder:self.forwarder.close();self.forwarder=None
        self.pending=None
        self.last=None

    def on_unload(self):
        self.on_runtime_stop('unload')
        for inbox in self.inboxes:inbox.close()
        self.inboxes=[]
        for handle in (self.image,self.mark_input,self.json_output,self.jpg_output):handle.close()
        if self.state:self.state.close();self.state=None


def create_plugin(context):return Plugin(context)
