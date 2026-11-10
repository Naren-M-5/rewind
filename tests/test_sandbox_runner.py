import json
from copy import deepcopy
import httpx
from rewind import verify_sandbox


def transport(monkeypatch,provider='sandbox',live=True):
    t={'id':'a'*32,'provider':provider,'scenario':'true_failure','order_id':None,'capture_id':None,
       'refund_id':None,'payment':'none','delivery':'pending','diagnosis':None,'capture_count':0,
       'needs_diagnosis':False,'decision':None}
    calls=[]
    def handle(r):
        path=r.url.path;calls.append(path)
        if path=='/api/config':return httpx.Response(200,json={'provider':provider,'merchant':'http','read_only':False})
        if path=='/api/tasks':return httpx.Response(200,json=deepcopy(t))
        if path.endswith('/order'):t.update(order_id='ORDER-TEST',approval_url='https://www.sandbox.paypal.com/checkoutnow?token=ORDER-TEST',payment='created')
        elif path.endswith('/capture'):t.update(capture_id='CAPTURE-TEST',payment='captured',capture_count=1)
        elif path.endswith('/diagnose'):t['diagnosis']={'live_ai':live,'provider':'openai_compatible' if live else 'unavailable','evidence_ids':[1]}
        elif path.endswith('/recover'):t.update(payment='refunded',refund_id='REFUND-TEST')
        return httpx.Response(200,json=deepcopy(t))
    real=httpx.Client
    monkeypatch.setattr(verify_sandbox.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(handle),**kw))
    monkeypatch.setenv('REWIND_API_TOKEN','DO-NOT-EXPORT-THIS')
    return calls


def test_verification_runner_rejects_emulator_and_records_no_success(tmp_path,monkeypatch):
    transport(monkeypatch,provider='emulator');p=tmp_path/'report.json'
    assert verify_sandbox.main(['start','--report',str(p)])==2
    data=json.loads(p.read_text());assert not data['complete'] and not data['checks']


def test_start_resume_records_ids_without_credentials(tmp_path,monkeypatch):
    calls=transport(monkeypatch);p=tmp_path/'report.json'
    assert verify_sandbox.main(['start','--report',str(p)])==0
    assert '/api/tasks/'+'a'*32+'/capture' not in calls
    assert verify_sandbox.main(['resume','--report',str(p)])==0
    data=json.loads(p.read_text())
    assert data['complete'] and data['order_id']=='ORDER-TEST' and data['refund_id']=='REFUND-TEST'
    assert 'DO-NOT-EXPORT-THIS' not in p.read_text()
    assert 'checkoutnow' not in p.read_text()


def test_runner_stops_before_recovery_without_live_ai(tmp_path,monkeypatch):
    calls=transport(monkeypatch,live=False);p=tmp_path/'report.json'
    verify_sandbox.main(['start','--report',str(p)])
    assert verify_sandbox.main(['resume','--report',str(p)])==2
    assert not any(c.endswith('/recover') for c in calls)
    assert json.loads(p.read_text())['capture_id']=='CAPTURE-TEST'
