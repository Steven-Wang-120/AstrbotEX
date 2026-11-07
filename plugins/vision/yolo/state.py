"""Durable object identities and explicit, idempotent mark commands.

Inference updates geometry only; it never resets act. All writes are atomic SQLite
transactions. Marked but invisible objects remain in the marks ledger indefinitely.
"""
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,name TEXT NOT NULL,color TEXT NOT NULL,
          box TEXT NOT NULL,feature TEXT NOT NULL,seen REAL NOT NULL,act INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,payload TEXT NOT NULL,result TEXT NOT NULL);
        ''')
        with self.db:
            for key,value in [('stream','yolo-'+uuid.uuid4().hex),('frame','0'),('revision','0')]:
                self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',(key,value))

    def value(self,key):
        return self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0]

    @property
    def stream_id(self): return self.value('stream')

    @property
    def revision(self): return int(self.value('revision'))

    def next_frame(self):
        with self.db:
            self.db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='frame'")
        return int(self.value('frame'))

    @staticmethod
    def distance(a,b):
        return math.sqrt(sum((x-y)**2 for x,y in zip(a,b))) if len(a)==len(b) and a else 1

    @staticmethod
    def overlap(a,b):
        intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
        union=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
        return intersection/union if union>0 else 0

    def associate(self, observations, now=None):
        now=time.time() if now is None else now
        rows=self.db.execute('SELECT id,name,color,box,feature,seen,act FROM objects').fetchall()
        candidates=[]
        for index,obj in enumerate(observations):
            scores=[]
            for oid,name,color,box,feature,seen,act in rows:
                if name!=obj['name']:continue
                overlap=self.overlap(obj['box'],json.loads(box))
                distance=self.distance(obj['feature'],json.loads(feature))
                age=now-seen
                # Short term spatial continuity; longer gaps require stronger appearance.
                if age<=2 and overlap>=.25 and distance<.45:
                    score=.65*overlap+.35*(1-distance)
                elif age<=300 and distance<.10 and color==obj['color']:
                    score=.55*(1-distance)+.2*overlap
                else:continue
                scores.append((score,index,oid))
            scores.sort(reverse=True)
            # Ambiguous lookalikes get a new ID rather than inheriting a mark.
            if scores and (len(scores)==1 or scores[0][0]-scores[1][0]>.08):
                candidates.append(scores[0])
        assigned,used={},set()
        for score,index,oid in sorted(candidates,reverse=True):
            if index not in assigned and oid not in used:
                assigned[index]=oid
                used.add(oid)
        result=[]
        with self.db:
            for index,obj in enumerate(observations):
                oid=assigned.get(index,uuid.uuid4().hex[:16])
                self.db.execute('''INSERT INTO objects(id,name,color,box,feature,seen) VALUES(?,?,?,?,?,?)
                  ON CONFLICT(id) DO UPDATE SET color=excluded.color,box=excluded.box,feature=excluded.feature,seen=excluded.seen''',
                  (oid,obj['name'],obj['color'],json.dumps(obj['box']),json.dumps(obj['feature']),now))
                result.append(oid)
            # Only old unmarked identities may be pruned; marked identities are never expired.
            self.db.execute('DELETE FROM objects WHERE act=0 AND seen<?',(now-86400,))
        return result

    def snapshot(self, visible_ids=()):
        visible=set(visible_ids)
        self.db.execute('BEGIN')
        try:
            revision=self.revision
            rows=self.db.execute('SELECT id,name,act,seen FROM objects').fetchall()
        finally:self.db.commit()
        states={oid:int(act) for oid,name,act,seen in rows}
        marked={oid:{'name':name,'act':1,'visible':oid in visible,'last_seen':seen}
                for oid,name,act,seen in rows if act}
        return revision,states,marked

    def mark(self, payload):
        if not isinstance(payload,dict):raise ValueError('mark payload must be an object')
        if set(payload)-{'request_id','stream_id','ids','marked'}:raise ValueError('unknown mark fields')
        request=payload.get('request_id')
        ids=payload.get('ids')
        marked=payload.get('marked')
        if not isinstance(request,str) or not 1<=len(request)<=128:raise ValueError('request_id required')
        if payload.get('stream_id')!=self.stream_id:raise ValueError('unknown stream_id')
        if type(marked) is not bool:raise ValueError('marked must be boolean')
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(x,str) for x in ids):
            raise ValueError('ids must contain 1..100 object IDs')
        if len(set(ids))!=len(ids):raise ValueError('duplicate object ID')
        canonical=json.dumps(payload,sort_keys=True,separators=(',',':'))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior=self.db.execute('SELECT payload,result FROM commands WHERE id=?',(request,)).fetchone()
            if prior:
                if prior[0]!=canonical:raise ValueError('request_id reused with different command')
                result=json.loads(prior[1])
                self.db.commit()
                return result
            for oid in ids:
                if self.db.execute('SELECT 1 FROM objects WHERE id=?',(oid,)).fetchone() is None:
                    raise ValueError('unknown object ID: '+oid)
            self.db.executemany('UPDATE objects SET act=? WHERE id=?',[(int(marked),oid) for oid in ids])
            self.db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            result={'ok':True,'stream_id':self.stream_id,'ids':ids,'marked':marked,'mark_rev':self.revision,'request_id':request}
            self.db.execute('INSERT INTO commands VALUES(?,?,?)',(request,canonical,json.dumps(result)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def close(self):self.db.close()
