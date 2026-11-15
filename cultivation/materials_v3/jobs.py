"""Durable job ledger with idempotency, leases, fencing and resumable checkpoints."""
import json, sqlite3, time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from .contracts import V3Error, digest

TERMINAL={'SUCCEEDED','PARTIAL_FAILED','FAILED','CANCELLED'}
PAUSED={'WAITING_USER','WAITING_AUTH','PAUSED_BUDGET'}

class JobStore:
    def __init__(self, path):
        self.path=str(path);Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.tx() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, operation TEXT NOT NULL, hash TEXT NOT NULL,
            kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, budget TEXT NOT NULL,
            checkpoint TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT, usage TEXT NOT NULL DEFAULT '{}',
            fence INTEGER NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0, UNIQUE(owner,operation));
            CREATE TABLE IF NOT EXISTS events(job TEXT, sequence INTEGER, data TEXT, PRIMARY KEY(job,sequence));
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT,owner TEXT,data BLOB,filename TEXT,PRIMARY KEY(id,owner));''')
            c.execute('CREATE TABLE IF NOT EXISTS model_cache(owner TEXT,key TEXT,value TEXT,expires REAL,PRIMARY KEY(owner,key))')
            if 'expires' not in {r[1] for r in c.execute('PRAGMA table_info(artifacts)')}:c.execute('ALTER TABLE artifacts ADD COLUMN expires REAL')
            cols={r[1] for r in c.execute('PRAGMA table_info(artifacts)')}
            for name,typ in [('purpose','TEXT'),('sha256','TEXT'),('byte_size','INTEGER')]:
                if name not in cols:c.execute('ALTER TABLE artifacts ADD COLUMN '+name+' '+typ)
            c.executescript('''CREATE TABLE IF NOT EXISTS artifact_links(owner TEXT,origin TEXT,artifact TEXT,purpose TEXT,job TEXT,proof TEXT,PRIMARY KEY(owner,origin,artifact,job));
                CREATE TABLE IF NOT EXISTS media_revocations(owner TEXT,scope TEXT,ref TEXT,PRIMARY KEY(owner,scope,ref));
                CREATE TABLE IF NOT EXISTS revocation_events(owner TEXT,event TEXT,hash TEXT,result TEXT,PRIMARY KEY(owner,event));''')

    @contextmanager
    def tx(self):
        c=sqlite3.connect(self.path,timeout=20);c.row_factory=sqlite3.Row
        try:
            c.execute('PRAGMA secure_delete=ON');c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()

    def _get(self,c,owner,job):
        row=c.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(job,owner)).fetchone()
        if row is None:raise V3Error('NOT_FOUND','作业不存在或不属于当前用户。')
        d=dict(row)
        for k in ('payload','budget','checkpoint','result','error','usage'):d[k]=json.loads(d[k]) if d[k] else None
        return d

    def _event(self,c,row,phase,message=''):
        seq=c.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE job=?',(row['id'],)).fetchone()[0]
        data=dict(jobId=row['id'],eventSequence=seq,phase=phase,state=row['state'],processedCount=len(row['checkpoint'].get('processed',[])),
                  knownTotal=None,message=message,recoverable=row['state'] in PAUSED,checkpointRevision=row['revision'],usageSummary=row['usage'])
        data['occurredAt']=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        c.execute('INSERT INTO events VALUES(?,?,?)',(row['id'],seq,json.dumps(data,ensure_ascii=False)))

    def create(self,owner,operation,kind,payload,budget):
        self.assert_readable(dict(owner=owner,kind=kind,payload=payload))
        h=digest(dict(kind=kind,payload=payload,budget=budget))
        with self.tx() as c:
            if c.execute("SELECT 1 FROM media_revocations WHERE owner=? AND scope='account'",(owner,)).fetchone():raise V3Error('MEDIA_REVOKED','账户资料访问已撤销。')
            old=c.execute('SELECT id,hash FROM jobs WHERE owner=? AND operation=?',(owner,operation)).fetchone()
            if old:
                if old['hash']!=h:raise V3Error('IDEMPOTENCY_CONFLICT','同一操作身份的内容发生变化。')
                return self._get(c,owner,old['id'])
            jid=str(uuid4())
            c.execute('INSERT INTO jobs(id,owner,operation,hash,kind,state,payload,budget) VALUES(?,?,?,?,?,?,?,?)',
                      (jid,owner,operation,h,kind,'QUEUED',json.dumps(payload,ensure_ascii=False),json.dumps(budget)))
            row=self._get(c,owner,jid);self._event(c,row,'queued');return row

    def get(self,owner,jid):
        with self.tx() as c:return self._get(c,owner,jid)

    def events(self,owner,jid,after=0):
        with self.tx() as c:
            self._get(c,owner,jid)
            return [json.loads(r[0]) for r in c.execute('SELECT data FROM events WHERE job=? AND sequence>? ORDER BY sequence',(jid,after))]

    def claim(self,kinds=None):
        with self.tx() as c:
            clause=' AND kind IN ('+','.join('?' for _ in kinds)+')' if kinds else ''
            r=c.execute("SELECT * FROM jobs WHERE (state='QUEUED' OR (state='RUNNING' AND lease<?))"+clause+" ORDER BY rowid LIMIT 1",(time.time(),*(kinds or []))).fetchone()
            if not r:return None
            if r['attempts']>=3:
                row=self._get(c,r['owner'],r['id']);c.execute("UPDATE jobs SET state='FAILED',error=? WHERE id=?",(json.dumps({'code':'RETRY_LIMIT','message':'作业恢复次数已达上限。'}),r['id']));row['state']='FAILED';self._event(c,row,'dead_letter');return None
            c.execute("UPDATE jobs SET state='RUNNING',fence=fence+1,attempts=attempts+1,lease=? WHERE id=?",(time.time()+90,r['id']))
            row=self._get(c,r['owner'],r['id']);self._event(c,row,'running');return row

    def save(self,row,*,phase,checkpoint=None,usage=None,state=None,result=None,error=None):
        with self.tx() as c:
            current=self._get(c,row['owner'],row['id'])
            if current['fence']!=row['fence'] or current['state']!='RUNNING':raise V3Error('LEASE_LOST','作业已取消或由其他worker接管。')
            values=dict(checkpoint=checkpoint if checkpoint is not None else current['checkpoint'],usage=usage if usage is not None else current['usage'],
                        state=state or current['state'],result=result,error=error)
            c.execute('UPDATE jobs SET checkpoint=?,usage=?,state=?,result=?,error=?,revision=revision+1,lease=? WHERE id=?',
                      (json.dumps(values['checkpoint'],ensure_ascii=False),json.dumps(values['usage']),values['state'],json.dumps(result,ensure_ascii=False) if result is not None else None,
                       json.dumps(error,ensure_ascii=False) if error else None,time.time()+90,row['id']))
            current=self._get(c,row['owner'],row['id']);self._event(c,current,phase);return current

    def heartbeat(self,row):
        with self.tx() as c:
            c.execute("UPDATE jobs SET lease=? WHERE id=? AND fence=? AND state='RUNNING'",(time.time()+90,row['id'],row['fence']))

    def command(self,owner,jid,action,budget=None):
        with self.tx() as c:
            row=self._get(c,owner,jid)
            if action=='cancel':
                if row['state'] not in TERMINAL:c.execute("UPDATE jobs SET state='CANCELLED',fence=fence+1 WHERE id=?",(jid,))
            elif action=='resume':
                if row['state'] not in PAUSED|{'PARTIAL_FAILED','FAILED'}:return row
                b=budget or row['budget']
                if any(b.get(k,0)<v for k,v in row['usage'].items() if k in b):raise V3Error('BUDGET_APPROVAL_REQUIRED','新预算低于已使用额度。')
                c.execute("UPDATE jobs SET state='QUEUED',budget=?,error=NULL,attempts=0,fence=fence+1 WHERE id=?",(json.dumps(b),jid))
            row=self._get(c,owner,jid);self._event(c,row,action);return row

    def assert_readable(self,row,origin=None):
        with self.tx() as c:
            proof=(row.get('payload') or {}).get('proofId') if row.get('kind')=='proof-review-jobs' else None
            if c.execute("SELECT 1 FROM media_revocations WHERE owner=? AND (scope='account' OR (scope='artifact' AND ref=?) OR (scope='proof' AND ref=?))",(row['owner'],origin,proof)).fetchone():raise V3Error('MEDIA_REVOKED','图片或账户访问已撤销。')

