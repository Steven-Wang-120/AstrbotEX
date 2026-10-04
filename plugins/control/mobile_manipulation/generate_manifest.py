"""Regenerate the discoverable manifest from the sole action-schema source."""
import json
from pathlib import Path
from astrbot_ex.core.tasks.contracts import action_manifest

def manifest():
    actions=action_manifest().to_dict()['actions']
    ports=[]
    for name in ('command','lease','cancel','ack','feedback','result','status'):
        pub=name in {'command','lease','cancel'}
        ports.append({'id':name,'direction':'publish' if pub else 'subscribe',
            'message_types':['std_msgs/msg/String'],'default_topic':'/astrex/mobile_mvp/'+name,
            'execution_lane':'control' if pub else 'general','requires_runtime_running':pub,
            'qos_preset':'reliable_volatile','queue':{'capacity':8 if not pub else 4,
             'overflow':'reject_new','max_age_ms':200 if pub else 1000,
             'max_message_bytes':131072,'max_bytes':524288}})
    return {'id':'mobile_manipulation','name':'Simulation mobile manipulation', 'version':'0.1.0',
        'entry':'main.py','enabled_default':False,'provides':['action_owner'],'action_api_version':2,
        'observation_guide':'guide.md','actions':actions,'ros2':{'schema_version':1,'ports':ports}}

if __name__=='__main__':
    Path(__file__).with_name('plugin.json').write_text(json.dumps(manifest(),indent=2)+'\n')
