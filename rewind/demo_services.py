"""Independent demo merchant and buyer inbox. These are test businesses, with real HTTP delivery."""
import hashlib
import json
import os
import secrets
import sqlite3
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal


async def service_access(request: Request, x_rewind_service_token: str = Header(default='')):
    expected = os.getenv('REWIND_SERVICE_TOKEN', '')
    if expected and not secrets.compare_digest(x_rewind_service_token, expected):
        raise HTTPException(401, 'Invalid service token')
    if not expected and request.client and request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
        raise HTTPException(403, 'Unconfigured demo services are localhost-only')


def headers():
    return {'X-Rewind-Service-Token': os.getenv('REWIND_SERVICE_TOKEN', '')}


def database(path, table):
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute(f'CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    db.commit()
    return db


def read(db, table, tid):
    row = db.execute(f'SELECT body FROM {table} WHERE id=?', (tid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Record not found')
    return json.loads(row[0])


def save(db, table, tid, body):
    with db:
        db.execute(f'INSERT INTO {table} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (tid, json.dumps(body)))


class JobRequest(BaseModel):
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    scenario: Literal['capture_timeout', 'delivery_loss', 'true_failure']


class DeliveryRequest(BaseModel):
    fault: Literal['none', 'drop_before', 'drop_after'] = 'none'


class ReceiptRequest(BaseModel):
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    content: str = Field(max_length=100_000)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def create_inbox():
    app = FastAPI(title='REWIND demo buyer inbox')
    db = database(os.getenv('REWIND_INBOX_DB', 'rewind-inbox.db'), 'receipts')

    @app.get('/health')
    async def health():
        return {'service': 'buyer-inbox'}

    @app.post('/receipts', dependencies=[Depends(service_access)])
    async def receive(body: ReceiptRequest):
        if hashlib.sha256(body.content.encode()).hexdigest() != body.sha256:
            raise HTTPException(409, 'Content digest mismatch')
        # No await inside read/insert: one service worker owns this atomic operation.
        row = db.execute('SELECT body FROM receipts WHERE id=?', (body.task_id,)).fetchone()
        if row:
            receipt = json.loads(row[0])
            if receipt['sha256'] != body.sha256:
                raise HTTPException(409, 'A different artifact already exists for this task')
        else:
            receipt = {**body.model_dump(), 'receipt_id': 'RCPT-' + uuid4().hex, 'delivered': True}
            save(db, 'receipts', body.task_id, receipt)
        return {k: v for k, v in receipt.items() if k != 'content'}

    @app.get('/receipts/{tid}', dependencies=[Depends(service_access)])
    async def receipt(tid: str):
        return read(db, 'receipts', tid)

    return app


def create_merchant():
    app = FastAPI(title='REWIND demo merchant')
    db = database(os.getenv('REWIND_MERCHANT_DB', 'rewind-merchant.db'), 'jobs')
    buyer = os.getenv('REWIND_INBOX_URL', 'http://127.0.0.1:8012').rstrip('/')

    @app.get('/health')
    async def health():
        return {'service': 'merchant'}

    @app.post('/jobs', dependencies=[Depends(service_access)])
    async def execute(body: JobRequest):
        row = db.execute('SELECT body FROM jobs WHERE id=?', (body.task_id,)).fetchone()
        if row:
            job = json.loads(row[0])
            if job['scenario'] != body.scenario:
                raise HTTPException(409, 'Task scenario cannot change')
            return job
        content = None if body.scenario == 'true_failure' else (
            'REWIND demonstration report\n\nTask ' + body.task_id + '\n\n'
            'Generated and persisted by the independent demo merchant process.\n'
            'Recovery sends these same bytes to a separate buyer inbox over HTTP.\n')
        job = {'task_id': body.task_id, 'scenario': body.scenario,
            'status': 'failed' if content is None else 'completed', 'terminal': True,
            'artifact': content, 'sha256': hashlib.sha256(content.encode()).hexdigest() if content else None,
            'delivery': 'pending', 'receipt_id': None}
        save(db, 'jobs', body.task_id, job)
        return job

    @app.get('/jobs/{tid}', dependencies=[Depends(service_access)])
    async def status(tid: str):
        job = read(db, 'jobs', tid)
        # Receipt is buyer authority. A lost acknowledgement can be recovered by this read.
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            try:
                r = await client.get(buyer + '/receipts/' + tid, headers=headers())
                if r.status_code == 200:
                    receipt = r.json()
                    if receipt['sha256'] != job['sha256'] or receipt['task_id'] != tid:
                        raise HTTPException(409, 'Buyer receipt conflicts with merchant artifact')
                    job.update(delivery='delivered', receipt_id=receipt['receipt_id'])
                    save(db, 'jobs', tid, job)
                elif r.status_code != 404:
                    raise HTTPException(502, 'Buyer receipt verification failed')
            except httpx.TransportError:
                job['delivery'] = 'unknown'
        return job

    @app.post('/jobs/{tid}/deliver', dependencies=[Depends(service_access)])
    async def deliver(tid: str, body: DeliveryRequest):
        job = read(db, 'jobs', tid)
        if job['status'] != 'completed' or not job['artifact']:
            raise HTTPException(409, 'No completed artifact to deliver')
        target = buyer
        req_headers = headers()
        if body.fault != 'none':
            if os.getenv('REWIND_CHAOS', 'true').lower() != 'true':
                raise HTTPException(403, 'Fault injection disabled')
            target = os.getenv('REWIND_FAULT_PROXY_URL', 'http://127.0.0.1:8013').rstrip('/') + '/buyer'
            req_headers.update({'X-Rewind-Fault': body.fault, 'X-Rewind-Fault-Id': 'delivery-' + tid + '-' + body.fault})
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            try:
                r = await client.post(target + '/receipts', headers=req_headers,
                    json={'task_id': tid, 'content': job['artifact'], 'sha256': job['sha256']})
                if r.status_code != 200:
                    raise HTTPException(502, 'Buyer inbox rejected delivery')
                receipt = r.json()
                if receipt.get('task_id') != tid or receipt.get('sha256') != job['sha256'] or not receipt.get('delivered'):
                    raise HTTPException(409, 'Invalid buyer receipt')
                job.update(delivery='delivered', receipt_id=receipt['receipt_id'])
            except httpx.TransportError:
                job.update(delivery='unknown')
        save(db, 'jobs', tid, job)
        return job

    return app
