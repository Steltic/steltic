"""Native physical decision API. Uses the host app's session and origin guards."""
import json
from pathlib import Path
import re

from fastapi import APIRouter,HTTPException,Request
from fastapi.concurrency import run_in_threadpool
from filelock import Timeout


def router(job_root):
    """job_root(request) resolves the authenticated session's job directory."""
    r=APIRouter()

    def folder(request,job):
        if not re.fullmatch(r'[A-Za-z0-9_-]+',job):raise HTTPException(422,'Invalid job name')
        root=Path(job_root(request)).resolve();path=(root/job).resolve()
        if not path.is_relative_to(root) or not path.is_dir():raise HTTPException(404,'Unknown job')
        return path

    def call(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except Timeout as ex:raise HTTPException(409,'The native job is updating; retry after it completes') from ex
        except ValueError as ex:
            raise HTTPException(409 if 'refresh' in str(ex) or 'already belongs' in str(ex) else 422,str(ex)) from ex
        except (KeyError,TypeError) as ex:raise HTTPException(422,str(ex)) from ex

    async def body(request):
        data=await request.body()
        if len(data)>2*1024*1024:raise HTTPException(413,'Decision exceeds 2 MB')
        value=call(json.loads,data)
        if not isinstance(value,dict):raise HTTPException(422,'Provide a decision object')
        return value

    @r.get('/api/physical/{job}/requests')
    def requests(job:str,request:Request):
        from steel_engine.physical_inputs import request_register
        value=call(request_register,folder(request,job))
        if value is None:raise HTTPException(404,'Job predates the native physical input register')
        return value

    @r.post('/api/physical/{job}/answers')
    async def answer(job:str,request:Request):
        from steel_engine.physical_inputs import apply_decision
        path=folder(request,job);value=await body(request)
        return await run_in_threadpool(call,apply_decision,path,**value)

    @r.post('/api/physical/{job}/replay')
    async def replay(job:str,request:Request):
        path=folder(request,job);value=await body(request)
        def export():
            from filelock import FileLock
            from steel_engine import physical_inputs,bim_export
            with FileLock(str(path/'.physical-inputs.lock'),timeout=10):
                current=physical_inputs.read(path)
                if current is None:raise ValueError('Job has no physical inputs')
                if current['revision']!=value.get('expected_revision') or physical_inputs.fingerprint(current)!=value.get('expected_sha256'):
                    raise ValueError('Physical inputs changed; refresh before replay')
                p=bim_export.replay(path)
                return dict(readiness=p['readiness'],revision=current['revision'],analysis_executed=False,
                            package_path='design/bim_package.json')
        return await run_in_threadpool(call,export)

    return r
