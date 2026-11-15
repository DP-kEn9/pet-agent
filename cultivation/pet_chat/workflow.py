import json,os,re
from datetime import datetime,timezone
from copy import deepcopy
from ..materials_v3.contracts import V3Error,identity,digest
from ..phase4.contracts import PetInput,ProactiveInput,PetDraft,PetReview
from ..phase4.guards import prepare,projection,live,echo,refs

def safety_fallback(ctx,error_code):
    if ctx.row['kind']!='pet-reply-jobs' or error_code not in {'JOB_TIMEOUT','MODEL_TIMEOUT','MODEL_DEADLINE','MODEL_UNAVAILABLE'}:return None
    req=PetInput.model_validate(ctx.row['payload'])
    if req.mode=='preview' or not re.search(r'(?:现在|马上|立刻).{0,16}(?:伤害自己|自杀|自残)|(?:伤害自己|自杀|自残).{0,16}(?:现在|马上|立刻)',req.message.text):return None
    live(ctx)
    return dict(echo(req),status='degraded',source='reviewed_safety_template_v1',canPublish=True,modelGenerationSucceeded=False,generationFailure=error_code,
        reply=dict(messageId=req.message.replyMessageId,replyToMessageId=req.message.userMessageId,text='听起来你现在很难受。请先联系身边可信的人，让对方陪着你，并尽快联系当地紧急帮助；先暂停学习。如果可以，请先远离可能伤害自己的物品。'),
        memoryProposals=[],handoffProposals=[],requiresBackendPublicationCheck=True)

def skip(req):
    if not hasattr(req,'trigger'):return None
    t=req.trigger
    checks=[(not t.enabled,'INTERACTION_DISABLED'),(not t.sourceActive or t.alreadyStarted,'SOURCE_INACTIVE'),
        (t.currentSourceRevision!=t.sourceRevision,'SOURCE_VERSION_CHANGED'),(t.validUntil<=req.asOf,'TRIGGER_EXPIRED'),
        (t.quietHours,'QUIET_HOURS'),(t.activeFocus,'ACTIVE_FOCUS_SESSION'),(t.remainingQuota==0,'FREQUENCY_LIMIT_REACHED')]
    if t.checkedAt>req.asOf:raise V3Error('TRIGGER_TIME_INVALID','触发校验不能晚于输入截点。')
    return next((code for failed,code in checks if failed),None)

def token_count(value):
    import tiktoken
    encoded=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    return len(tiktoken.get_encoding('o200k_base').encode_ordinary(encoded))


async def summarize(ctx,p):
    from ..phase4.contracts import RollingDigest
    budget=int(os.getenv('ALGORITHM_V4_CONTEXT_TOKEN_BUDGET','12288'))
    if budget<4096:raise V3Error('CONTEXT_CONFIGURATION_INVALID','上下文预算过小。')
    p=deepcopy(p)
    history=p['history']
    watermark=p.pop('compactThrough',None)
    prior=p.get('conversationSummary')
    # Reserve space for the dialogue instructions, schema and response.
    available=budget-1400
    retain=4
    if len(history)<=24 and token_count(p)<=available:
        return p,None
    cut=0
    for i,m in enumerate(history):
        if watermark is not None and m['sequence']>watermark:break
        if m['role']!='assistant':continue
        cut=i+1
        if len(history)-cut<=retain and token_count({**p,'history':history[cut:]})<=available-1200:
            break
    prefix=history[:cut]
    if not prefix:
        if token_count(p)<=available:return p,None
        raise V3Error('CONTEXT_LIMIT','当前消息或尚未完成的对话超出上下文预算。',state='WAITING_USER')
    # Only commit coverage after all chunks and the publishable reply succeed.
    groups=[];group=[];used=0
    for m in prefix:
        text=m['text']
        for start in range(0,len(text),600):
            item={'messageId':m['messageId'],'role':m['role'],'text':text[start:start+600]}
            size=token_count(item)+1
            if group and used+size>available-700:
                groups.append(group);group=[];used=0
            group.append(item);used+=size
    if group:groups.append(group)
    summary=prior['text'] if prior else ''
    cache=ctx.checkpoint.setdefault('petRollingDigests',{})
    for group in groups:
        key=digest([summary,group])
        if key not in cache:
            out=await ctx.call('pet-conversation-context','U0',dict(previousSummary=summary,messages=group),RollingDigest)
            cache[key]=out.text;ctx.save('pet_history_summarizing')
        summary=cache[key]
    last=prefix[-1]
    update=dict(messageId=last['messageId'],sequence=last['sequence'],text=summary)
    p.update(history=history[len(prefix):],conversationSummary=update)
    if token_count(p)>available:
        raise V3Error('CONTEXT_LIMIT','必要事实与近期消息超过预算，请缩小本轮输入。',state='WAITING_USER')
    return p,update if watermark is not None else None

def validate(ctx,req,p,draft):
    facts={f['evidenceRef']:f for f in p['facts']}
    if not set(draft.evidenceRefs)<=facts.keys():raise V3Error('EVIDENCE_NOT_ALLOWED','灵宠引用了未许可事实。')
    for claim in draft.factualClaims:
        f=facts.get(claim.evidenceRef)
        if not f or claim.evidenceRef not in draft.evidenceRefs:raise V3Error('EVIDENCE_NOT_ALLOWED','事实声明没有有效引用。')
        # The public fact is nested, so both level and data.level are valid
        # references. Only this finite path syntax is accepted; no eval/traversal.
        key=claim.field.removeprefix('data.').removeprefix('/data/').removeprefix('/')
        data=f if claim.field in {'title','objectId','revision','/title','/objectId','/revision'} else f['data']
        if claim.field!=key:
            ctx.checkpoint.setdefault('factClaimNormalizations',[]).append(dict(evidenceRef=claim.evidenceRef,field=claim.field,canonicalField=key))
        if key not in data or data[key]!=claim.value:
            ctx.checkpoint['recoveryEvidence']=dict(kind='fact_claim',evidenceRef=claim.evidenceRef,field=claim.field,canonicalField=key,
                fieldExists=key in data,expected=data.get(key),received=claim.value)
            raise V3Error('UNSUPPORTED_FACT_CLAIM','事实声明与源字段不一致。')
    ids={m['memoryId'] for m in p['memories']}
    for m in draft.memoryProposals:
        if m.sourceQuote not in p['message']['text']:raise V3Error('MEMORY_INTENT_UNVERIFIED','偏好提案须引用用户本轮原话。')
        if m.memoryId is not None and m.memoryId not in ids:raise V3Error('MEMORY_REFERENCE_INVALID','偏好提案指向不可见记录。')
        if m.operation in {'remember','correct'} and not req.consentSnapshot.memoryWrite:
            raise V3Error('MEMORY_PERMISSION_REQUIRED','长期偏好写入未开启；请开启后再保存。',state='WAITING_AUTH')
    if req.mode=='preview' and (draft.memoryProposals or draft.editGoalRef or draft.evidenceRefs):raise V3Error('ACTION_NOT_ALLOWED','预览不能产生业务提案或真实事实引用。')
    if re.search(r'(?:已经为你|已为你|我已)(?:保存|修改|安排|转交|挪好)',draft.replyText):
        raise V3Error('UNCONFIRMED_ACTION_CLAIMED_DONE','尚未取得后端动作回执，不能宣称执行成功。')

async def reply(ctx,payload):
    cls=ProactiveInput if ctx.row['kind']=='pet-proactive-jobs' else PetInput
    req=cls.model_validate(payload)
    req=cls.model_validate(prepare(ctx.row['kind'],req.model_dump(mode='json')));live(ctx)
    reason=skip(req)
    if reason:return dict(echo(req),status='skipped',skipReason=reason,logicalMessageId=req.trigger.logicalMessageId,canPublish=False)
    if hasattr(req,'trigger') and req.trigger.deterministicTemplate and req.trigger.type=='reminder':
        return dict(echo(req),status='ok',reply={'messageId':req.trigger.logicalMessageId,'text':f'提醒：{req.trigger.title}即将开始，请以最新日程为准。'},source='backend_template',canPublish=True,requiresBackendPublicationCheck=True)
    p=projection(req);ctx.checkpoint['petProjectionManifest']=dict(fields=sorted(p),historyIds=[h['messageId'] for h in p['history']],
        memoryIds=[m['memoryId'] for m in p['memories']],evidenceRefs=[f['evidenceRef'] for f in p['facts']],consentRevision=req.consentSnapshot.consentRevision)
    ctx.save('pet_context_ready');p,summary_update=await summarize(ctx,p)
    skill='pet-proactive' if hasattr(req,'trigger') else 'pet-dialogue'
    draft=await ctx.call(skill,'C0',dict(request=p,policy=dict(formalWrites=False,latestTurnOverridesStyle=True,apiIsNotMemory=True,
        stylePriority=['safety_and_permission','explicit_current_turn','saved_style','allowed_memories','defaults'])),PetDraft)
    if hasattr(req,'trigger') and draft.skipReason:
        return dict(echo(req),status='skipped',skipReason=draft.skipReason,logicalMessageId=req.trigger.logicalMessageId,canPublish=False)
    ctx.save('pet_validating');validate(ctx,req,p,draft)
    edit_goal_id=None
    if (not hasattr(req,'trigger') and req.mode=='reply' and req.consentSnapshot.planningRead
            and draft.responsePriority!='urgent' and not draft.clarifications and 'planning' in draft.intents
            and draft.editAction and draft.editGoalRef and draft.editGoalRef in draft.evidenceRefs):
        goal=next((f for f in p['facts'] if f['kind']=='goal' and f['evidenceRef']==draft.editGoalRef),None)
        if goal and goal['data'].get('status') not in ('archived','completed'):
            matching=[f for f in p['facts'] if f['kind']=='goal' and f['title']==goal['title'] and f['data'].get('status') not in ('archived','completed')]
            if len(matching)==1:
                edit_goal_id=goal['objectId']
                if not draft.memoryProposals and draft.responsePriority=='ordinary':
                    title=goal['title']
                    if draft.editAction=='refinePlan':
                        draft.replyText=f"去《{title}》的规划精进页调整吧，由你确认修改。"
                    else:
                        draft.replyText=f"你可以在《{title}》的功法页里直接修改。"
            else:
                draft.editAction=None
                draft.editGoalRef=None
                draft.clarifications=['这两门功法同名，你要修改的是哪一门？']
                draft.replyText=draft.clarifications[0]
    needs_review=req.mode!='preview' and bool(draft.evidenceRefs or draft.factualClaims or draft.memoryProposals or draft.editGoalRef or draft.responsePriority=='urgent')
    review=None
    if needs_review:
        review=await ctx.call('pet-response-review','C1',dict(request=p,candidate=draft.model_dump(),formalWrites=False),PetReview)
        if not set(review.evidenceRefs)<={f['evidenceRef'] for f in p['facts']}:
            raise V3Error('PET_REVIEW_INVALID','复核引用不合法，未放行。')
    can_publish=review is None or review.accepted and not review.issues
    live(ctx)
    memory=[dict(m.model_dump(),proposalId=identity(ctx.row['id'],'memory',i),proposalRevision=1,status='PROPOSED',sourceMessageId=req.message.userMessageId,
                 expectedMemoryRevision=req.consentSnapshot.memoryRevision,requiresBackendPermissionCheck=True) for i,m in enumerate(draft.memoryProposals)]
    ctx.save('pet_ready' if can_publish else 'pet_review_rejected')
    return dict(echo(req),status='needs_clarification' if draft.clarifications else 'ok',petId=req.petId,conversationId=req.conversationId,
        conversationRevision=req.conversationRevision,consentRevision=req.consentSnapshot.consentRevision,memoryRevision=req.consentSnapshot.memoryRevision,
        styleRevision=req.styleSnapshot.styleRevision,reply=dict(messageId=req.trigger.logicalMessageId if hasattr(req,'trigger') else req.message.replyMessageId,replyToMessageId=req.message.userMessageId,text=draft.replyText),
        responsePriority=draft.responsePriority,intents=draft.intents,evidenceRefs=draft.evidenceRefs,factualClaims=[c.model_dump() for c in draft.factualClaims],
        clarifications=draft.clarifications,memoryProposals=memory if can_publish else [],handoffProposals=[],editGoalId=edit_goal_id if can_publish else None,
        editAction=draft.editAction if edit_goal_id and can_publish else None,
        **({'conversationSummary':summary_update} if summary_update and can_publish else {}),
        review=review.model_dump() if review else None,canPublish=can_publish,requiresBackendPublicationCheck=True,
        **({'logicalMessageId':req.trigger.logicalMessageId,'sourceId':req.trigger.sourceId,'sourceRevision':req.trigger.sourceRevision} if hasattr(req,'trigger') else {}))
