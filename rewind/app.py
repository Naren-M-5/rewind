import os
import secrets
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai
from .engine import Engine
from .providers import Emulator, PayPalSandbox, ProviderError
from .store import Store

load_dotenv()


class NewTask(BaseModel):
    scenario: Literal['capture_timeout', 'delivery_loss', 'true_failure']


class CaptureRequest(BaseModel):
    drop_response: bool = False


class EvidenceRequest(BaseModel):
    message: str


def create_app(db_path=None, provider=None):
    mode = os.getenv('REWIND_PROVIDER', 'emulator')
    if mode not in ['emulator', 'sandbox']:
        raise RuntimeError('Only emulator or sandbox providers are supported')
    if mode == 'sandbox' and not os.getenv('REWIND_API_TOKEN'):
        raise RuntimeError('Set REWIND_API_TOKEN before enabling sandbox mode')
    store = Store(db_path or os.getenv('REWIND_DB', 'rewind.db'))
    pay = provider or (Emulator(store) if mode == 'emulator' else PayPalSandbox())
    engine = Engine(store, pay)
    app = FastAPI(title='REWIND', version='0.2.0')
    app.state.engine = engine

    async def access(request: Request, x_rewind_token: str = Header(default='')):
        expected = os.getenv('REWIND_API_TOKEN', '')
        if expected:
            if not secrets.compare_digest(x_rewind_token, expected):
                raise HTTPException(401, 'Enter your dashboard API token in Settings')
        elif request.client and request.client.host not in ['127.0.0.1', '::1', 'testclient']:
            raise HTTPException(403, 'Tokenless emulator is localhost-only')

    def chaos():
        if os.getenv('REWIND_CHAOS', 'true').lower() != 'true':
            raise HTTPException(403, 'Chaos controls disabled')

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': 'Task not found'}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=502)

    @app.exception_handler(httpx.TransportError)
    async def network_error(request, exc):
        return JSONResponse({'detail': 'Provider connection uncertain. Reconcile before retrying.'}, status_code=502)

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/api/config', dependencies=[Depends(access)])
    async def config():
        return {'provider': pay.name, 'ai': ai.mode(), 'ai_configuration': ai.configuration(),
                'merchant': engine.merchant_client.name, 'read_only': False,
                'chaos': os.getenv('REWIND_CHAOS', 'true').lower() == 'true'}

    @app.get('/api/tasks', dependencies=[Depends(access)])
    async def tasks():
        return [public(t) for t in store.tasks()]

    @app.post('/api/tasks', dependencies=[Depends(access)])
    async def create(body: NewTask):
        chaos()
        return public(engine.create(body.scenario))

    @app.get('/api/tasks/{tid}', dependencies=[Depends(access)])
    async def task(tid: str):
        return {**public(store.get(tid)), 'events': store.events(tid)}

    @app.post('/api/tasks/{tid}/order', dependencies=[Depends(access)])
    async def order(tid: str):
        return public(await engine.order(tid))

    @app.post('/api/tasks/{tid}/capture', dependencies=[Depends(access)])
    async def capture(tid: str, body: CaptureRequest):
        if body.drop_response:
            chaos()
        return public(await engine.capture(tid, body.drop_response))

    @app.get('/api/tasks/{tid}/fault-evidence', dependencies=[Depends(access)])
    async def fault_evidence(tid: str):
        engine.task(tid)
        key = 'capture-' + engine.key(tid, 'capture')
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            r = await client.get(os.getenv('REWIND_FAULT_PROXY_URL', 'http://127.0.0.1:8013').rstrip('/') + '/faults/' + key,
                headers={'X-Rewind-Service-Token': os.getenv('REWIND_SERVICE_TOKEN', '')})
        if r.status_code != 200:
            raise HTTPException(404, 'No completed capture fault observed by proxy')
        return r.json()

    @app.post('/api/tasks/{tid}/reconcile', dependencies=[Depends(access)])
    async def reconcile(tid: str):
        return public(await engine.reconcile(tid))

    @app.post('/api/tasks/{tid}/merchant', dependencies=[Depends(access)])
    async def merchant(tid: str):
        chaos()
        return public(await engine.merchant(tid))

    @app.post('/api/tasks/{tid}/evidence', dependencies=[Depends(access)])
    async def evidence(tid: str, body: EvidenceRequest):
        if len(body.message) > 4000:
            raise HTTPException(422, 'Message exceeds 4000 characters')
        await engine.add_evidence(tid, body.message)
        return {'saved': True}

    @app.post('/api/tasks/{tid}/diagnose', dependencies=[Depends(access)])
    async def diagnose(tid: str):
        return public(await engine.diagnose(tid))

    @app.post('/api/tasks/{tid}/recover', dependencies=[Depends(access)])
    async def recover(tid: str):
        return public(await engine.recover(tid))

    @app.get('/api/tasks/{tid}/artifact', dependencies=[Depends(access)])
    async def artifact(tid: str):
        task = store.get(tid)
        if task['delivery'] != 'delivered' or not task['artifact']:
            raise HTTPException(409, 'Result has not been delivered')
        return PlainTextResponse(task['artifact'])

    @app.get('/api/tasks/{tid}/evidence-package', dependencies=[Depends(access)])
    async def package(tid: str):
        task = public(store.get(tid))
        return {'task': task, 'events': store.events(tid),
            'disclosure': 'Local evidence export; not submitted to PayPal and not proof of dispute eligibility.',
            'financial_authority': 'PayPal sandbox' if pay.name == 'sandbox' else 'Local emulator'}

    @app.post('/api/webhooks/paypal')
    async def webhook(request: Request):
        if pay.name != 'sandbox':
            raise HTTPException(409, 'Webhooks require sandbox mode')
        if len(await request.body()) > 1_000_000:
            raise HTTPException(413, 'Webhook too large')
        event = await request.json()
        if not await pay.verify_webhook(request.headers, event):
            raise HTTPException(401, 'Webhook verification failed')
        event_id = event.get('id')
        if not isinstance(event_id, str) or not event_id:
            raise HTTPException(422, 'Webhook event ID missing')
        resource = event.get('resource', {})
        oid = resource.get('supplementary_data', {}).get('related_ids', {}).get('order_id')
        cid = resource.get('id')
        found = next((t for t in store.tasks() if (oid and t['order_id'] == oid) or (cid and t['capture_id'] == cid)), None)
        if found:
            store.event(found['id'], 'VERIFIED_WEBHOOK', 'paypal', {
                'event_id': event_id, 'type': event.get('event_type')}, dedupe='paypal:' + event_id)
            # Inbox first, then authoritative GET. Failures return 5xx so PayPal retries;
            # duplicate deliveries still reconcile (dedupe must not hide a prior failed GET).
            await engine.reconcile(found['id'])
        else:
            store.event('unmatched', 'VERIFIED_WEBHOOK', 'paypal', {
                'event_id': event_id, 'type': event.get('event_type')}, dedupe='paypal:' + event_id)
        return {'received': True}

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    async def index():
        return FileResponse(static / 'index.html')

    return app


def public(task):
    return {k: v for k, v in task.items() if k != 'artifact'}

