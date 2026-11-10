"""Read-only judge replay. No payment, model, or merchant adapter is instantiated."""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .store import Store

TASK_FIELDS = {'id','scenario','created','amount','provider','payment','fulfillment','delivery',
               'order_id','capture_id','refund_id','artifact_hash','receipt_id','last_verified',
               'capture_count','merchant_mode','contradiction','delivery_gap','conflict_kind'}
DETAIL_FIELDS = {'payment','order_id','capture_id','refund_id','receipt_id','sha256','status','terminal',
                 'fault_mode','fault_requested','http_transport','action','ai_overridden'}
DIAGNOSIS_FIELDS = {'provider','model','live_ai','latency_ms','recommendation','confidence','evidence_ids','conflict_type'}


def export(store, tids, allow_emulator=False):
    tasks=[]
    for tid in tids:
        t=store.get(tid)
        if t['provider']!='sandbox' and not allow_emulator:
            raise ValueError('Replay requires sandbox runs; --allow-emulator explicitly labels rehearsal data')
        if t['provider']=='sandbox' and any(str(t.get(k,'')).startswith(('SIM-','STUB-')) for k in ['order_id','capture_id','refund_id']):
            raise ValueError('Test identifiers cannot be exported as a sandbox replay')
        if not t.get('capture_id'):
            raise ValueError('Only tasks with an observed capture can be exported')
        clean={k:v for k,v in t.items() if k in TASK_FIELDS}
        d=t.get('diagnosis')
        clean['diagnosis']={**{k:v for k,v in d.items() if k in DIAGNOSIS_FIELDS},
            'cause':'Recorded diagnosis','explanation':'Free-text evidence and diagnosis details are redacted in this public replay.',
            'contradictions':[]} if d else None
        dec=t.get('decision')
        clean['decision']={k:dec[k] for k in ['action','ai_overridden'] if k in dec} if dec else None
        if clean['decision']:clean['decision']['reason']='Recorded policy result; this replay cannot execute it.'
        clean['events']=[{k:e[k] for k in ['seq','task_id','kind','source','time']} | {
            'detail':{k:v for k,v in e['detail'].items() if k in DETAIL_FIELDS}}
            for e in store.events(tid)]
        tasks.append(clean)
    return {'format':'rewind-replay-v1','exported_at':datetime.now(timezone.utc).isoformat(),
            'disclosure':'Recorded task snapshots, not live provider verification. Free text, artifacts, and approval URLs are removed.',
            'tasks':tasks}


def create_replay():
    path=Path(os.getenv('REWIND_REPLAY_FILE','judge-replay.json'))
    if not path.is_file():
        raise RuntimeError('Export a redacted replay first and set REWIND_REPLAY_FILE')
    replay=json.loads(path.read_text())
    if replay.get('format')!='rewind-replay-v1':raise RuntimeError('Unsupported replay format')
    tasks={t['id']:t for t in replay['tasks']}
    app=FastAPI(title='REWIND read-only judge replay',version='0.2.0')

    @app.get('/health')
    async def health():return {'status':'ok','read_only':True}

    @app.get('/api/config')
    async def config():
        provider='sandbox' if tasks and all(t['provider']=='sandbox' for t in tasks.values()) else 'emulator'
        return {'provider':provider,'ai':'recorded','chaos':False,'read_only':True,'disclosure':replay['disclosure']}

    @app.get('/api/tasks')
    async def list_tasks():return [{k:v for k,v in t.items() if k!='events'} for t in tasks.values()]

    @app.get('/api/tasks/{tid}')
    async def task(tid:str):
        if tid not in tasks:raise HTTPException(404,'Recorded task not found')
        return tasks[tid]

    @app.get('/api/tasks/{tid}/evidence-package')
    async def package(tid:str):
        return {'task':await task(tid),'disclosure':replay['disclosure'],'read_only':True}

    static=Path(__file__).parent/'static'
    app.mount('/static',StaticFiles(directory=static),name='static')
    @app.get('/')
    async def index():return FileResponse(static/'index.html')
    return app


def main():
    from dotenv import load_dotenv
    load_dotenv();p=argparse.ArgumentParser(description='Export allowlisted fields for a credential-free judge replay')
    p.add_argument('--task',action='append',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--db',default=os.getenv('REWIND_DB','rewind.db'));p.add_argument('--allow-emulator',action='store_true')
    a=p.parse_args();data=export(Store(a.db),a.task,a.allow_emulator)
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(data,indent=2))
    print('Redacted replay saved: '+str(a.output))


if __name__=='__main__':main()
