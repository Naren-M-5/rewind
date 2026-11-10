import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from rewind import ai
from rewind.app import create_app
from rewind.providers import Emulator, ProviderError
from rewind.store import Store


class FakeSandbox(Emulator):
    name = 'sandbox'
    verified = True

    async def verify_webhook(self, headers, event):
        return self.verified


def client(tmp_path, monkeypatch):
    monkeypatch.setenv('REWIND_PROVIDER','emulator')
    monkeypatch.delenv('REWIND_API_TOKEN',raising=False)
    store=Store(str(tmp_path/'provider.db'))
    provider=FakeSandbox(store)
    app=create_app(str(tmp_path/'app.db'),provider=provider)
    return TestClient(app), provider


def captured(c):
    t=c.post('/api/tasks',json={'scenario':'true_failure'}).json()
    c.post('/api/tasks/'+t['id']+'/order')
    return c.post('/api/tasks/'+t['id']+'/capture',json={}).json()


def event(t):
    return {'id':'WEBHOOK-1','event_type':'PAYMENT.CAPTURE.COMPLETED','resource':{
        'id':t['capture_id'],'status':'COMPLETED',
        'supplementary_data':{'related_ids':{'order_id':t['order_id']}}}}


def test_unverified_webhook_rejected(tmp_path,monkeypatch):
    c,p=client(tmp_path,monkeypatch)
    with c:
        t=captured(c);p.verified=False
        assert c.post('/api/webhooks/paypal',json=event(t)).status_code==401
        events=c.get('/api/tasks/'+t['id']).json()['events']
        assert not any(e['kind']=='VERIFIED_WEBHOOK' for e in events)


def test_duplicate_webhook_stored_once_and_reconciled_again(tmp_path,monkeypatch):
    c,p=client(tmp_path,monkeypatch)
    with c:
        t=captured(c)
        for _ in range(2):assert c.post('/api/webhooks/paypal',json=event(t)).status_code==200
        events=c.get('/api/tasks/'+t['id']).json()['events']
        assert len([e for e in events if e['kind']=='VERIFIED_WEBHOOK'])==1
        assert len([e for e in events if e['kind']=='STATE_RECONCILED'])>=3


def test_delayed_completed_webhook_does_not_undo_refund(tmp_path,monkeypatch):
    c,p=client(tmp_path,monkeypatch)
    with c:
        t=captured(c);url='/api/tasks/'+t['id']
        c.post(url+'/merchant');c.post(url+'/recover')
        assert c.post('/api/webhooks/paypal',json=event(t)).status_code==200
        assert c.get(url).json()['payment']=='refunded'


def test_webhook_retry_reconciles_after_first_provider_read_failure(tmp_path,monkeypatch):
    c,p=client(tmp_path,monkeypatch)
    with c:
        t=captured(c);original=p.order;calls=[]
        async def flaky(oid):
            calls.append(oid)
            if len(calls)==1:raise ProviderError('temporary read failure')
            return await original(oid)
        p.order=flaky
        assert c.post('/api/webhooks/paypal',json=event(t)).status_code==502
        assert c.post('/api/webhooks/paypal',json=event(t)).status_code==200
        assert len(calls)==2


def inference_payload(ids):
    return {'cause':'Merchant statement contradicts terminal failure log','recommendation':'review',
            'confidence':.9,'evidence_ids':ids,'contradictions':['Completion claimed despite terminal failure'],
            'explanation':'Investigate the conflicting completion claim before refunding.'}


def test_ollama_schema_request_and_citation_validation(monkeypatch):
    monkeypatch.setenv('REWIND_AI','ollama')
    captured_requests=[]
    def handler(request):
        body=json.loads(request.content);captured_requests.append(body)
        return httpx.Response(200,json={'done':True,'done_reason':'stop','message':{'content':json.dumps(inference_payload([7]))}})
    real=httpx.AsyncClient
    monkeypatch.setattr(ai.httpx,'AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(handler)))
    task={'id':'task','payment':'captured','fulfillment':'failed','delivery':'pending'}
    d=asyncio.run(ai.diagnose(task,[{'seq':7,'detail':{'message':'work completed'}}]))
    assert d['live_ai'] and d['recommendation']=='review'
    assert captured_requests[0]['format']['additionalProperties'] is False
    assert captured_requests[0]['stream'] is False
    assert 'PayPal' not in json.dumps(captured_requests[0].get('tools',[]))


def test_ollama_fabricated_citation_rejected(monkeypatch):
    import pytest
    monkeypatch.setenv('REWIND_AI','ollama')
    real=httpx.AsyncClient
    def handler(request):
        return httpx.Response(200,json={'done':True,'done_reason':'stop','message':{'content':json.dumps(inference_payload([999]))}})
    monkeypatch.setattr(ai.httpx,'AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError,match='nonexistent'):
        asyncio.run(ai.diagnose({'id':'t','payment':'captured','fulfillment':'failed','delivery':'pending'},[{'seq':7}]))

