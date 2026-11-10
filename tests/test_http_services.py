import asyncio
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

from rewind.engine import Engine
from rewind.merchants import HTTPMerchant
from rewind.providers import Emulator,PayPalSandbox
from rewind.store import Store


def freeport():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


@pytest.fixture(scope='module')
def services(tmp_path_factory):
    folder=tmp_path_factory.mktemp('http-services')
    inbox,merchant,proxy,payment=[freeport() for _ in range(4)]
    env=os.environ.copy()
    env.update(REWIND_SERVICE_TOKEN='integration-test-token',REWIND_CHAOS='true',
        REWIND_INBOX_DB=str(folder/'inbox.db'),REWIND_MERCHANT_DB=str(folder/'merchant.db'),
        REWIND_PROXY_DB=str(folder/'proxy.db'),REWIND_INBOX_URL=f'http://127.0.0.1:{inbox}',
        REWIND_FAULT_PROXY_URL=f'http://127.0.0.1:{proxy}',TEST_PROVIDER_PORT=str(payment))
    commands=[
        [sys.executable,'-m','uvicorn','rewind.demo_services:create_inbox','--factory','--host','127.0.0.1','--port',str(inbox)],
        [sys.executable,'-m','uvicorn','rewind.demo_services:create_merchant','--factory','--host','127.0.0.1','--port',str(merchant)],
        [sys.executable,'tests/http_provider.py'],
        [sys.executable,'-c',f"from rewind.fault_proxy import serve; serve(port={proxy},paypal_base='http://127.0.0.1:{payment}')"],
    ]
    processes=[subprocess.Popen(cmd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for cmd in commands]
    try:
        with httpx.Client(timeout=1,trust_env=False,headers={'X-Rewind-Service-Token':'integration-test-token'}) as c:
            for port in [inbox,merchant,proxy,payment]:
                for _ in range(100):
                    try:
                        if c.get(f'http://127.0.0.1:{port}/health').status_code==200:break
                    except httpx.HTTPError:pass
                    if any(p.poll() is not None for p in processes):raise RuntimeError('Test service exited')
                    time.sleep(.05)
                else:raise RuntimeError('Test service startup timed out')
        yield {'inbox':f'http://127.0.0.1:{inbox}','merchant':f'http://127.0.0.1:{merchant}',
               'proxy':f'http://127.0.0.1:{proxy}','payment':f'http://127.0.0.1:{payment}'}
    finally:
        for p in processes:p.terminate()
        for p in processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill()


def configured_engine(services,monkeypatch,tmp_path,wire_payment=False):
    monkeypatch.setenv('REWIND_MERCHANT_URL',services['merchant'])
    monkeypatch.setenv('REWIND_FAULT_PROXY_URL',services['proxy'])
    monkeypatch.setenv('REWIND_SERVICE_TOKEN','integration-test-token')
    store=Store(str(tmp_path/'engine.db'))
    provider=Emulator(store)
    if wire_payment:
        monkeypatch.setenv('PAYPAL_CLIENT_ID','TEST-CLIENT');monkeypatch.setenv('PAYPAL_CLIENT_SECRET','TEST-SECRET')
        provider=PayPalSandbox();provider.base=services['payment']
    return Engine(store,provider,merchant=HTTPMerchant())


def test_real_http_redelivery_requires_matching_inbox_receipt(services,monkeypatch,tmp_path):
    e=configured_engine(services,monkeypatch,tmp_path)
    async def case():
        t=e.create('delivery_loss');await e.order(t['id']);await e.capture(t['id'])
        lost=await e.merchant(t['id'])
        assert lost['delivery']=='unknown' and lost['delivery_gap'] and not lost['contradiction']
        with httpx.Client(trust_env=False,headers={'X-Rewind-Service-Token':'integration-test-token'}) as c:
            assert c.get(services['inbox']+'/receipts/'+t['id']).status_code==404
        result=await e.recover(t['id'])
        assert result['delivery']=='delivered' and result['receipt_id'].startswith('RCPT-')
        with httpx.Client(trust_env=False,headers={'X-Rewind-Service-Token':'integration-test-token'}) as c:
            receipt=c.get(services['inbox']+'/receipts/'+t['id']).json()
        assert receipt['content']==result['artifact']
        assert receipt['sha256']==hashlib.sha256(result['artifact'].encode()).hexdigest()
        again=await e.recover(t['id']);assert again['receipt_id']==result['receipt_id']
    asyncio.run(case())


def test_capture_socket_really_closes_after_success_then_reconciles(services,monkeypatch,tmp_path):
    e=configured_engine(services,monkeypatch,tmp_path,wire_payment=True)
    async def case():
        t=e.create('capture_timeout');await e.order(t['id'])
        lost=await e.capture(t['id'],drop=True)
        assert lost['payment']=='unknown'
        with httpx.Client(trust_env=False,headers={'X-Rewind-Service-Token':'integration-test-token'}) as c:
            fault=c.get(services['proxy']+'/faults/capture-'+e.key(t['id'],'capture')).json()
        assert fault=={'mode':'drop_after','upstream_status':201}
        recovered=await e.capture(t['id'])
        assert recovered['payment']=='captured' and recovered['capture_count']==1
        assert any(v['kind']=='DUPLICATE_INTENT_BLOCKED' for v in e.store.events(t['id']))
    asyncio.run(case())


def test_lost_buyer_ack_recovers_receipt_without_second_artifact(services,monkeypatch,tmp_path):
    e=configured_engine(services,monkeypatch,tmp_path)
    async def case():
        t=e.create('capture_timeout');await e.order(t['id']);await e.capture(t['id'])
        t=e.store.get(t['id']);job=await e.merchant_client.execute(t);e.sync_merchant(t,job)
        uncertain=await e.merchant_client.deliver(t,fault='drop_after')
        assert uncertain['delivery']=='unknown'
        e.sync_merchant(t,uncertain)
        r=await e.recover(t['id'])
        assert r['delivery']=='delivered' and r['decision']['action']=='none'
        assert not any(v['kind']=='RESULT_REDELIVERED' for v in e.store.events(t['id']))
    asyncio.run(case())


def test_buyer_rejects_different_artifact_for_same_task(services):
    tid='a'*32
    with httpx.Client(trust_env=False,headers={'X-Rewind-Service-Token':'integration-test-token'}) as c:
        def send(content):
            return c.post(services['inbox']+'/receipts',json={'task_id':tid,'content':content,'sha256':hashlib.sha256(content.encode()).hexdigest()})
        first=send('first');again=send('first')
        assert first.status_code==again.status_code==200
        assert first.json()['receipt_id']==again.json()['receipt_id']
        assert send('different').status_code==409


def test_inbox_service_token_required(services):
    assert httpx.post(services['inbox']+'/receipts',trust_env=False,json={'task_id':'b'*32,'content':'x','sha256':hashlib.sha256(b'x').hexdigest()}).status_code==401
