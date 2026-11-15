"""Backend-owned privacy revisions, replay fences and minimal inference projection."""
import json,os,time
from copy import deepcopy
from ..materials_v3.contracts import V3Error,digest
from .contracts import PetInput,ProactiveInput,Invalidation

PET_KINDS={'pet-reply-jobs','pet-proactive-jobs'}

def prepare(kind,payload):
    p=deepcopy(payload)
    if kind not in PET_KINDS:return p
    if p['scope'].get('petId')!=p['petId'] or p['scope'].get('conversationId')!=p['conversationId']:
        raise V3Error('PET_SCOPE_MISMATCH','灵宠和会话必须与请求范围一致。')
    if p['mode']=='preview':
        if kind=='pet-proactive-jobs':raise V3Error('INVALID_REQUEST','预览不能成为主动触发。')
        p.update(history=[],memories=[],facts=[],pendingProposals=[],conversationSummary=None,compactThrough=None)
        p['message']['text']='今天复习没有进展，我有点累。'
        return p
    c=p['consentSnapshot'];allowed=set(c['allowedFields'])
    if (p['history'] or p.get('conversationSummary')) and 'history' not in allowed:raise V3Error('FIELD_NOT_AUTHORIZED','未授权读取会话历史。')
    if p['memories'] and (not c['memoryRead'] or 'memories' not in allowed):raise V3Error('FIELD_NOT_AUTHORIZED','未授权读取长期偏好。')
    if any(not c['planningRead'] or 'facts.'+f['kind'] not in allowed for f in p['facts']):raise V3Error('FIELD_NOT_AUTHORIZED','学习摘要超出本轮许可字段。')
    if len({f['evidenceRef'] for f in p['facts']})!=len(p['facts']):raise V3Error('EVIDENCE_REFERENCE_CONFLICT','事实引用重复。')
    if len({m['memoryId'] for m in p['memories']})!=len(p['memories']):raise V3Error('MEMORY_VERSION_CONFLICT','偏好引用重复。')
    seq=[m['sequence'] for m in p['history']]
    prior=p.get('conversationSummary')
    if prior and (prior['sequence']>=2*p['conversationRevision']-1 or seq and prior['sequence']>=seq[0]):
        raise V3Error('CONVERSATION_SEQUENCE_INVALID','摘要覆盖范围与当前对话不符。')
    if seq!=sorted(set(seq)):raise V3Error('CONVERSATION_SEQUENCE_INVALID','会话窗口必须有序且不重复。')
    if any(not m['explicitlyConfirmed'] for m in p['memories']):raise V3Error('MEMORY_NOT_CONFIRMED','不能把推断当作长期偏好。')
    return p

def ensure(store):
    with store.tx() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS pet_privacy(owner TEXT,pet TEXT,scope TEXT,ref TEXT,revision INTEGER,deleted TEXT,PRIMARY KEY(owner,pet,scope,ref));
            CREATE TABLE IF NOT EXISTS pet_privacy_events(owner TEXT,event TEXT,hash TEXT,result TEXT,PRIMARY KEY(owner,event));
            CREATE TABLE IF NOT EXISTS pet_private_retention(owner TEXT,job TEXT,expires REAL,PRIMARY KEY(owner,job));''')

def retain(store,row):
    if row['kind'] not in PET_KINDS:return
    ensure(store)
    duration=int(os.getenv('ALGORITHM_V4_PRIVATE_RETENTION_SECONDS','86400'))
    if not 60<=duration<=2592000:raise V3Error('RETENTION_CONFIGURATION_INVALID','私聊作业副本保留期须为60秒至30天。')
    with store.tx() as c:c.execute('INSERT OR IGNORE INTO pet_private_retention VALUES(?,?,?)',(row['owner'],row['id'],time.time()+duration))

def purge(store):
    ensure(store);count=0
    with store.tx() as c:
        for x in c.execute('SELECT * FROM pet_private_retention WHERE expires<=?',(time.time(),)).fetchall():
            row=store._get(c,x['owner'],x['job'])
            if not row['payload'].get('privacyTombstone'):
                c.execute("UPDATE jobs SET fence=fence+1,state='CANCELLED',payload=?,checkpoint='{}',result=NULL,error=? WHERE id=?",
                    (json.dumps({'privacyTombstone':True}),json.dumps({'code':'PET_CONTEXT_EXPIRED','message':'本地私有作业副本已过保留期，请从后端取得新上下文。','retryable':False}),row['id']))
                store._event(c,store._get(c,x['owner'],x['job']),'pet_private_copy_expired');count+=1
            c.execute('DELETE FROM pet_private_retention WHERE owner=? AND job=?',(x['owner'],x['job']))
    return count

def assert_current(store,owner,p):
    if not p.get('petId') or p.get('mode')=='preview':return
    p=(ProactiveInput if 'trigger' in p else PetInput).model_validate(p).model_dump(mode='json')
    ensure(store)
    with store.tx() as c:rows=c.execute('SELECT * FROM pet_privacy WHERE owner=? AND pet=?',(owner,p['petId'])).fetchall()
    for r in rows:
        scope=r['scope']
        if scope=='consent':current=p['consentSnapshot']['consentRevision']
        elif scope=='memory':current=p['consentSnapshot']['memoryRevision']
        elif scope=='settings':current=p['styleSnapshot']['styleRevision']
        elif scope=='conversation' and r['ref']==p['conversationId']:current=p['conversationRevision']
        else:continue
        if current<r['revision']:raise V3Error('CONSENT_STALE' if scope=='consent' else 'PET_CONTEXT_STALE','输入依赖已失效，请按新授权或记忆版本重建上下文。')
        if scope=='memory' and set(json.loads(r['deleted']))&{m['memoryId'] for m in p.get('memories',[])}:
            raise V3Error('MEMORY_REVOKED','已经遗忘的偏好不能重新提供给模型。')

def invalidate(store,owner,raw):
    req=Invalidation.model_validate(raw);ensure(store);fingerprint=digest(req.model_dump(mode='json'))
    with store.tx() as c:
        old=c.execute('SELECT * FROM pet_privacy_events WHERE owner=? AND event=?',(owner,req.eventId)).fetchone()
        if old:
            if old['hash']!=fingerprint:raise V3Error('IDEMPOTENCY_CONFLICT','同一撤销事件内容不一致。')
            return json.loads(old['result'])
        ref=req.conversationId or req.ref if req.scope=='conversation' else '*'
        prior=c.execute('SELECT * FROM pet_privacy WHERE owner=? AND pet=? AND scope=? AND ref=?',(owner,req.petId,req.scope,ref)).fetchone()
        if prior and req.revision<prior['revision']:raise V3Error('PET_CONTEXT_STALE','不能回退隐私修订。')
        deleted=sorted(set(req.deletedMemoryIds)|set(json.loads(prior['deleted']) if prior else []))
        c.execute('INSERT OR REPLACE INTO pet_privacy VALUES(?,?,?,?,?,?)',(owner,req.petId,req.scope,ref,req.revision,json.dumps(deleted)))
        ids=[]
        for rawrow in c.execute('SELECT * FROM jobs WHERE owner=?',(owner,)).fetchall():
            row=store._get(c,owner,rawrow['id']);p=row['payload']
            if row['kind'] not in PET_KINDS or p.get('petId')!=req.petId:continue
            if p.get('privacyTombstone'):continue
            if req.scope=='conversation' and p.get('conversationId')!=ref:continue
            version={'consent':p.get('consentSnapshot',{}).get('consentRevision',0),'memory':p.get('consentSnapshot',{}).get('memoryRevision',0),'settings':p.get('styleSnapshot',{}).get('styleRevision',0)}.get(req.scope)
            if version is not None and version>=req.revision and not (req.scope=='memory' and set(deleted)&{m['memoryId'] for m in p.get('memories',[])}):continue
            # Cancel and erase every dependent materialized copy, including
            # completed jobs. Backend retains canonical messages under its policy.
            ids.append(row['id'])
            tombstone=dict(petId=req.petId,conversationId=p.get('conversationId'),privacyTombstone=True)
            error=dict(code='PET_CONTEXT_REVOKED',message='会话、偏好或许可已更新，旧结果不可使用。',retryable=False)
            c.execute("UPDATE jobs SET fence=fence+1,state='CANCELLED',checkpoint='{}',payload=?,result=NULL,error=? WHERE id=?",(json.dumps(tombstone),json.dumps(error,ensure_ascii=False),row['id']))
            now=store._get(c,owner,row['id']);store._event(c,now,'pet_context_revoked')
        result=dict(eventId=req.eventId,revision=req.revision,invalidatedJobIds=ids,localCopiesErased=True,requiresBackendCanonicalDataCleanup=True)
        c.execute('INSERT INTO pet_privacy_events VALUES(?,?,?,?)',(owner,req.eventId,fingerprint,json.dumps(result)))
        return result

def live(ctx):
    if ctx.row['kind'] not in PET_KINDS:return
    current=ctx.store.get(ctx.row['owner'],ctx.row['id'])
    if current['fence']!=ctx.row['fence'] or current['state']!='RUNNING':raise V3Error('LEASE_LOST','旧消息生成已失效。')
    assert_current(ctx.store,ctx.row['owner'],ctx.row['payload'])

def projection(req):
    p=prepare('pet-proactive-jobs' if hasattr(req,'trigger') else 'pet-reply-jobs',req.model_dump(mode='json'))
    revision=req.consentSnapshot.memoryRevision
    # Safe rebuild after forgetting: old windows need explicit backend sanitation
    # at the current memory revision. A changed cache key alone is insufficient.
    p['history']=[h for h in p['history'] if h['sanitizedAtMemoryRevision']>=revision and (req.consentSnapshot.memoryRead or not h['sourceMemoryIds'])]
    p['memories']=[m for m in p['memories'] if req.consentSnapshot.memoryRead]
    allowed={'goal':{'goalId','stageId','status','deadline','subjects'},'stage':{'goalId','stageId','goalRevision','status','remainingMinutes'},
        'task':{'goalId','stageId','goalRevision','status','action','remainingMinutes','plannedDate'},'progress':{'goalId','stageId','completedCount','totalCount','effectiveFocusMinutes'},
        'risk':{'goalId','stageId','level','gapMinutes','asOf'},'weekly':{'goalId','stageId','due','completed','knownOwedMinutes'},
        'capacity':{'goalId','stageId','confirmedNetMinutes','coverage'}}
    for f in p['facts']:f['data']={k:v for k,v in f['data'].items() if k in allowed[f['kind']]}
    return p

def echo(req):
    return dict(schemaVersion=req.schemaVersion,requestId=req.requestId,inputRevision=req.inputRevision,asOf=req.asOf.isoformat(),timeZone=req.timeZone,scope=req.scope,formalWrites=False)

def refs(refs,facts):
    if not set(refs)<={f.evidenceRef for f in facts}:raise V3Error('EVIDENCE_NOT_ALLOWED','输出引用超出本次许可事实。')
