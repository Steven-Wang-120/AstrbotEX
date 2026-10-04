"""Build T1 data only after explicit review of the 14 sample groups."""
import argparse
import dataclasses
import random
import hashlib
import json
from pathlib import Path
from astrbot_ex.core.tasks.contracts import canonical, digest
from astrbot_ex.core.tasks.corpus import build_corpus, review_samples, validate_corpus
from astrbot_ex.core.tasks.session import TaskSession
from astrbot_ex.core.tasks.laya_client import TaskLayaClient

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--approved-review-sha256',required=True)
    parser.add_argument('--review-document',type=Path,required=True)
    args=parser.parse_args()
    actual=hashlib.sha256(args.review_document.read_bytes()).hexdigest()
    if actual != args.approved_review_sha256:
        parser.error('review document hash changed')
    groups=build_corpus()
    report=validate_corpus(groups)
    report.update(review_document_sha256=actual, review_scope='Original 14 representative patterns and user-approved rules; v2 repairs observed-input isolation and target-role balance without new vocabulary', training_performed=False)
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'groups.jsonl').write_text('\n'.join(canonical(g) for g in groups)+'\n')
    exported = []
    client = TaskLayaClient()
    for group in groups:
        for point_index, point in enumerate(group['decision_points']):
            for alias_index, alias in enumerate(point['paraphrases']):
                context,_ = TaskSession(session_id=group['group_id']).feed(alias, point['observation'])
                options = list(context.options)
                random.Random(int(digest({'group':group['group_id'],'point':point_index})[:16],16)).shuffle(options)
                context = dataclasses.replace(context, options=tuple(options))
                body, mapping, budget = client.request_payload(context)
                payload=json.loads(body)
                accepted=[letter for letter,option in mapping.items() if option in point['gold']['accepted_options']]
                exported.append({'group_id':group['group_id'],'split':group['split'],'point':point_index,'alias':alias_index,
                    'source':'SYNTHETIC','state':payload['state'],'questions':payload['questions'],
                    'gold':{'task':accepted[0]}, 'accepted_gold':{'task':accepted},
                    'expected':{'task':accepted[0]}, 'semantic_gold':point['gold'],'option_mapping':mapping,'budget':budget})
    client.close()
    (args.output/'laya-choice.jsonl').write_text('\n'.join(canonical(row) for row in exported)+'\n')
    report.update(exported_rows=len(exported),export_hash=digest(exported),paraphrases_keep_same_group=True)
    (args.output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':
    main()
