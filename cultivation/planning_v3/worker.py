"""Pet-only entry point for the production job loop."""
import asyncio
import logging
import os
from pathlib import Path

from pydantic import ValidationError

from ..materials_v3.contracts import V3Error
from ..pet_chat.workflow import reply
from ..phase4.results import PetResult
from .runtime import Context

class Worker:
    def __init__(self, store, model=None, gate=None):
        self.store = store
        self.model = model
        self.gate = gate
        self.kinds = ('pet-reply-jobs', 'pet-proactive-jobs')

    async def once(self):
        row=self.store.claim(self.kinds)
        if row is None:return False
        ctx=Context(self.store,row,None,self.model)
        ctx.gate=self.gate
        async def heartbeat():
            while True:
                await asyncio.sleep(20);self.store.heartbeat(row)
        async def execute():
            from ..phase4.guards import retain
            retain(self.store,row)
            self.store.assert_readable(row)
            from .recovery import execute as recoverable_execute
            async def invoke():
                result = await reply(ctx, row['payload'])
                try:
                    PetResult.model_validate(result)
                    return result
                except ValidationError as exc:raise V3Error('RESULT_SCHEMA_INVALID','算法结果未满足公开合同，未发布候选。') from exc
            return await recoverable_execute(ctx,invoke)
        work=asyncio.create_task(execute());beat=asyncio.create_task(heartbeat())
        async def watch_cancel():
            while not work.done():
                await asyncio.sleep(.5)
                current=self.store.get(row['owner'],row['id'])
                if current['fence']!=row['fence'] or current['state']!='RUNNING':work.cancel();return
        watch=asyncio.create_task(watch_cancel())
        try:
            timeout=os.getenv('ALGORITHM_V4_PET_TIMEOUT_SECONDS','120') if row['kind'].startswith('pet-') else os.getenv('ALGORITHM_V3_JOB_TIMEOUT_SECONDS','600')
            result=await asyncio.wait_for(work,float(timeout))
            state='SUCCEEDED'
            self.store.save(ctx.row,phase='finished',checkpoint=ctx.checkpoint,usage=ctx.usage,state=state,result=result)
        except V3Error as exc:
            if exc.code!='LEASE_LOST':
                try:
                    from ..pet_chat.workflow import safety_fallback
                    fallback=safety_fallback(ctx,exc.code)
                    if fallback:self.store.save(ctx.row,phase='pet_safety_template_degraded',checkpoint=ctx.checkpoint,usage=ctx.usage,state='SUCCEEDED',result=fallback)
                    else:self.store.save(ctx.row,phase='blocked' if exc.state in {'WAITING_AUTH','WAITING_USER','PAUSED_BUDGET'} else 'failed',checkpoint=ctx.checkpoint,usage=ctx.usage,state=exc.state,error=exc.public())
                except V3Error:pass
        except asyncio.CancelledError:
            # Cancellation endpoint owns terminal state; shutdown requeues under a
            # new fence so a late result from this worker cannot overwrite it.
            current=self.store.get(row['owner'],row['id'])
            if not asyncio.current_task().cancelling():return True
            if current['state']=='RUNNING' and current['fence']==row['fence']:
                with self.store.tx() as c:c.execute("UPDATE jobs SET state='QUEUED',fence=fence+1 WHERE id=?",(row['id'],))
            raise
        except Exception as exc:
            error=V3Error('JOB_TIMEOUT' if isinstance(exc,TimeoutError) else 'JOB_EXECUTION_FAILED','处理未完成，检查点和原输入已保留。',retryable=True)
            try:
                import traceback
                ctx.checkpoint['fatalDiagnostic']=dict(exceptionType=type(exc).__name__,frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(exc.__traceback__)])
                from .recovery import record_terminal
                record_terminal(ctx,error.code)
            except (V3Error,OSError):pass
            try:
                from ..pet_chat.workflow import safety_fallback
                fallback=safety_fallback(ctx,error.code)
                if fallback:self.store.save(ctx.row,phase='pet_safety_template_degraded',checkpoint=ctx.checkpoint,usage=ctx.usage,state='SUCCEEDED',result=fallback)
                else:self.store.save(ctx.row,phase='failed',checkpoint=ctx.checkpoint,usage=ctx.usage,state='FAILED',error=error.public())
            except V3Error:pass
            logging.getLogger(__name__).warning('V3 job failed: %s',type(exc).__name__)
        finally:
            for t in (work,beat,watch):
                if not t.done():t.cancel()
            await asyncio.gather(work,beat,watch,return_exceptions=True);await ctx.close()
        return True
