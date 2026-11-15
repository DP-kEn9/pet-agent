import asyncio, hashlib, os
import time
import inspect
from pydantic import ValidationError
from datetime import datetime,timezone
from pathlib import Path
from copy import deepcopy
from ..materials_v3.contracts import V3Error,VERSION

SKILLS=Path(__file__).parent/'skills'
BUNDLES={
 'pet-dialogue':['pet-personality','pet-supportive-response','pet-memory-proposal','pet-plan-handoff'],
 'pet-proactive':['pet-personality','pet-supportive-response'],
}

class Context:
    def __init__(self,store,row,tools,model=None):
        self.store,self.row,self.tools,self.model=store,row,tools,model
        self.checkpoint=deepcopy(row['checkpoint']);self.usage=deepcopy(row['usage']);self.owned=[]
        if row['kind'] in {'pet-reply-jobs','pet-proactive-jobs'} and os.getenv('PET_MODEL_URL'):
            self.model = None  # Select the dedicated gateway lazily, after a real model call is needed.

    def save(self,phase):
        self.row=self.store.save(self.row,phase=phase,checkpoint=self.checkpoint,usage=self.usage)

    def charge(self,kind,amount):
        if self.usage.get(kind,0)+amount>self.row['budget'][kind]:
            raise V3Error('BUDGET_APPROVAL_REQUIRED','处理额度不足，已保存检查点；请批准新增预算后继续。',state='PAUSED_BUDGET')
        self.usage[kind]=self.usage.get(kind,0)+amount
        self.save('budget')

    async def call(self,skill,role,context,output,images=None):
        self.store.assert_readable(self.row)
        from ..phase4.guards import live
        live(self)
        from .recovery import enabled,prepare_context,before_dispatch
        if enabled():context=prepare_context(self,role,context)
        key='model:'+skill
        names=[skill]+BUNDLES.get(skill,[])
        prompt='\n\n'.join((SKILLS/(name+'.md')).read_text(encoding='utf-8') for name in names)
        if self.model is None:
            if self.row['kind'] in {'pet-reply-jobs','pet-proactive-jobs'} and os.getenv('PET_MODEL_URL'):
                from ..gateways import HttpModelGateway, ModelConfig
                endpoint = os.environ['PET_MODEL_URL']
                secret = os.getenv('PET_MODEL_API_KEY')
                if not secret or not endpoint.startswith('https://') or not os.getenv('PET_MODEL_NAME'):
                    raise V3Error('PET_MODEL_NOT_CONFIGURED','灵宠模型配置不完整。')
                self.model = HttpModelGateway(ModelConfig(endpoint=endpoint, api_key=secret,
                    model=os.environ['PET_MODEL_NAME'], protocol='chat_completions', auth_mode='api_key', json_object_mode=True))
                self.owned = [self.model]
            else:
                raise V3Error('PET_MODEL_NOT_CONFIGURED','请设置 PET_MODEL_URL、PET_MODEL_NAME 和 PET_MODEL_API_KEY。')
        request_hash=__import__('cultivation.materials_v3.contracts',fromlist=['digest']).digest(dict(
            context=context,schema=output.model_json_schema(),skillHash=hashlib.sha256(prompt.encode()).hexdigest(),
            model=getattr(getattr(self.model,'config',None),'model',os.getenv('MODEL_NAME','gpt-6.1-sol')),
            reasoning=getattr(getattr(self.model,'config',None),'reasoning_effort',os.getenv('MODEL_REASONING_EFFORT','medium')),
            serviceTier=getattr(getattr(self.model,'config',None),'service_tier',os.getenv('MODEL_SERVICE_TIER','default')),
            imageHashes=[hashlib.sha256(i.encode()).hexdigest() for i in images or []]))
        cached=self.checkpoint.get(key)
        if cached and cached['requestHash']==request_hash:return output.model_validate(cached['output'])
        # Shared only for evidence-bound material digests. Owner and full prompt,
        # model profile, input/schema/images participate in the immutable key.
        shared=role=='M1' and skill=='material-content-navigation' and hasattr(self.store,'cached_model')
        from ..materials_v3.contracts import digest
        cachekey=digest([request_hash,os.getenv('MODEL_ENDPOINT',''),getattr(getattr(self.model,'config',None),'endpoint','')])
        if shared:
            hit=self.store.cached_model(self.row['owner'],cachekey)
            if hit is not None:
                result=output.model_validate(hit)
                self.checkpoint.setdefault('sharedDigestCacheHits',[]).append(dict(requestHash=request_hash,cacheKey=cachekey))
                self.save('digest_cache_hit');return result
        # Do not reserve an R0 dispatch when the user-approved job budget
        # already forbids the call. charge() below remains the durable ledger.
        if self.usage.get('modelCalls',0)+1>self.row['budget']['modelCalls']:
            self.charge('modelCalls',1)
        before_dispatch(self,role)
        self.charge('modelCalls',1)
        entry=dict(role=role,skill=skill,skillVersion=VERSION,skillSha256=hashlib.sha256(prompt.encode()).hexdigest(),
                   skills=[dict(name=n,sha256=hashlib.sha256((SKILLS/(n+'.md')).read_bytes()).hexdigest()) for n in names],requestHash=request_hash,
                   imageCount=len(images or []),inputTokenCount=None,outputTokenCount=None,cost=None)
        entry['context']=deepcopy(context)
        entry['startedAt']=datetime.now(timezone.utc).isoformat();started=time.monotonic()
        self.checkpoint.setdefault('modelCalls',[]).append(entry);self.save(role)
        kwargs=dict(role=role,system_prompt=prompt,context=context,output_schema=output.model_json_schema())
        metrics={}
        signature=inspect.signature(self.model.reply)
        if 'diagnostics_callback' in signature.parameters or any(p.kind==inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
            kwargs['diagnostics_callback']=lambda value:metrics.update(value)
        if images:kwargs['images']=images
        try:
            if getattr(self,'gate',None) and hasattr(self.gate,'slot'):
                async with self.gate.slot(0 if self.row['kind']=='pet-reply-jobs' else 5):
                    current=self.store.get(self.row['owner'],self.row['id'])
                    if current['state']!='RUNNING' or current['fence']!=self.row['fence']:raise V3Error('LEASE_LOST','排队期间作业已失效，未向模型发送。')
                    self.store.assert_readable(self.row);live(self)
                    raw=await self.model.reply(**kwargs)
            elif getattr(self,'gate',None):
                async with self.gate:
                    current=self.store.get(self.row['owner'],self.row['id'])
                    if current['state']!='RUNNING' or current['fence']!=self.row['fence']:raise V3Error('LEASE_LOST','排队期间作业已失效，未向模型发送。')
                    self.store.assert_readable(self.row);live(self)
                    raw=await self.model.reply(**kwargs)
            else:raw=await self.model.reply(**kwargs)
            live(self)
            result=output.model_validate(raw)
        except Exception as exc:
            entry['errorCode']='MODEL_SCHEMA_INVALID' if isinstance(exc,ValidationError) else getattr(exc,'code','MODEL_UNAVAILABLE')
            entry['exceptionType']=type(exc).__name__
            entry['gatewayRetryable']=getattr(exc,'retryable',False) is True
            if isinstance(exc,ValidationError):
                entry['validationErrors']=[dict(type=e['type'],loc=list(e['loc'])) for e in exc.errors(include_input=False,include_context=False)]
                entry['rejectedOutputHash']=digest(raw)
                entry['rejectedOutput']=deepcopy(raw)
            usage=metrics.get('usage') or {}
            entry.update(inputTokenCount=usage.get('input_tokens',usage.get('prompt_tokens')),
                outputTokenCount=usage.get('output_tokens',usage.get('completion_tokens')))
            entry['transportMetrics']=metrics;entry['finishedAt']=datetime.now(timezone.utc).isoformat();entry['elapsedSeconds']=round(time.monotonic()-started,3);self.save(role)
            retryable=entry['gatewayRetryable'] or entry['errorCode'] in {'MODEL_SCHEMA_INVALID','MODEL_INVALID_RESPONSE'}
            raise V3Error(entry['errorCode'],'模型服务未能完成处理，已保留材料和讨论。',retryable=retryable) from exc
        entry['output']=result.model_dump(mode='json')
        entry['transportMetrics']=metrics
        usage=metrics.get('usage') or {}
        entry.update(inputTokenCount=usage.get('input_tokens',usage.get('prompt_tokens')),
            outputTokenCount=usage.get('output_tokens',usage.get('completion_tokens')),providerModel=metrics.get('model'),
            reasoningEffort=metrics.get('reasoning_effort'),serviceTier=metrics.get('service_tier'),costStatus='UNKNOWN_NO_PRICE_CONFIG')
        entry['finishedAt']=datetime.now(timezone.utc).isoformat();entry['elapsedSeconds']=round(time.monotonic()-started,3)
        self.checkpoint[key]={'requestHash':request_hash,'output':entry['output']};self.save(role)
        if shared:self.store.cached_model(self.row['owner'],cachekey,entry['output'])
        return result

    async def close(self):
        if callable(getattr(self.tools,'close_job',None)):await self.tools.close_job(self.row)
        for g in self.owned:
            if callable(getattr(g,'aclose',None)):await g.aclose()

