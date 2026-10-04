"""One explicit 20-case Chinese probe, using the owned fixed Laya checkpoint."""
import argparse
import json
import math
import time
from pathlib import Path
from astrbot_ex.core.decision.owned_laya import Deployment, OwnedLayaService
from astrbot_ex.core.decision.backends.mock import MockBackend
from astrbot_ex.core.tasks.contracts import canonical, digest
from astrbot_ex.core.tasks.corpus import development_probes
from astrbot_ex.core.tasks.session import TaskSession
from astrbot_ex.core.tasks.laya_client import TaskModelSession

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--run-real',action='store_true',required=True)
    p.add_argument('--laya-python',type=Path,required=True)
    p.add_argument('--cache',type=Path,required=True)
    p.add_argument('--device',choices=('cuda','cpu'),default='cuda')
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    groups=development_probes()
    # Freeze inputs before starting the model. No test-set tuning or automatic retries.
    (args.output/'frozen-probes.json').write_text(json.dumps(groups,ensure_ascii=False,indent=2)+'\n')
    owner=OwnedLayaService(Deployment(args.laya_python,args.cache,args.output/'service',device=args.device,startup_timeout_s=1800,warmup_deadline_ms=1800000))
    selector=TaskModelSession(owner,execution_idle=lambda:True)
    mock=MockBackend(kind='start')  # Diagnostic stays pre-Goal; this backend is not called.
    calls=[];start=time.monotonic();error=None;exit_evidence=None
    try:
        selector.start()
        startup_ms=(time.monotonic()-start)*1000
        for group in groups:
            point=group['decision_points'][0]
            session=TaskSession(session_id=group['group_id'])
            context,status=session.feed(point['text'],point['observation'])
            row={'group_id':group['group_id'],'source':'SYNTHETIC','context':context.to_dict(),
                 'gold':point['gold'],'precheck':status,'selected':None,'model_correct':False,'error':None}
            try:
                # Deliberately probe clarification/unsupported choices too; no Goal sink exists.
                selected=selector.select(context)
                row['selected']=selected.to_dict()
                row['model_correct']=selected.option_id in point['gold']['accepted_options']
                try:
                    row['trusted_proposal']=session.accept(selected,service_generation=selector.generation)
                except ValueError as exc:
                    row['trusted_rejection']=str(exc)
            except Exception as exc:
                row['error']=getattr(exc,'code',type(exc).__name__)
            row['trace']=selector.client.last_record
            calls.append(row)
            with (args.output/'calls.jsonl').open('a') as out:out.write(canonical(row)+'\n')
            print(json.dumps({'case':len(calls),'group':group['group_id'],'correct':row['model_correct'],'error':row['error']}),flush=True)
            if owner.status()['restart_required']:
                error='service_quarantined';break
    except Exception as exc:
        startup_ms=(time.monotonic()-start)*1000
        error=getattr(exc,'code',type(exc).__name__)
    finally:
        selector.cancel()
        try:exit_evidence=owner.stop(expected_generation=owner.generation)
        except Exception as exc:exit_evidence={'error':getattr(exc,'code',type(exc).__name__)}
        mock.close()
        elapsed=sorted(r['selected']['elapsed_ms'] for r in calls if r['selected'])
        quant=lambda q:elapsed[max(0,math.ceil(len(elapsed)*q)-1)] if elapsed else None
        summary={'phase':'L1','planned_groups':20,'completed_groups':len(calls),'model_correct':sum(r['model_correct'] for r in calls),
            'model_errors':sum(bool(r['error']) for r in calls),'startup_ms':startup_ms,
            'cold_load_ready_ms':owner.history[-1].get('load_ready_ms') if owner.history else None,
            'cold_warmup_ms':owner.history[-1].get('warmup',{}).get('elapsed_ms') if owner.history else None,
            'latency_ms':{'p50':quant(.5),'p95':quant(.95),'max':max(elapsed) if elapsed else None},
            'input_hash':digest(groups),'goal_submissions':0,'motion_commands':0,'ex_backend':'mock',
            'model_choice_quality_is_not_robot_success':True,'error':error,'owner':owner.status(),'exit_evidence':exit_evidence}
        (args.output/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    if error or len(calls)!=20:raise SystemExit(1)
if __name__=='__main__':main()
