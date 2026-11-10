import asyncio
import json
from fastapi.testclient import TestClient
import pytest
from rewind.engine import Engine
from rewind.providers import Emulator
from rewind.store import Store
from rewind.replay import export,create_replay


def task(store):
    e=Engine(store,Emulator(store))
    async def case():
        t=e.create('delivery_loss');await e.order(t['id']);await e.capture(t['id']);await e.merchant(t['id'])
        await e.diagnose(t['id']);await e.recover(t['id']);return t['id']
    return asyncio.run(case())


def test_public_export_excludes_free_text_and_approval_url(tmp_path):
    s=Store(str(tmp_path/'private.db'));tid=task(s)
    t=s.get(tid);t['approval_url']='https://example.invalid/secret-token';s.save(t)
    s.event(tid,'MERCHANT_MESSAGE','untrusted_message',{'message':'PRIVATE CUSTOMER TEXT'})
    out=export(s,[tid],allow_emulator=True);encoded=json.dumps(out)
    assert 'PRIVATE CUSTOMER TEXT' not in encoded
    assert 'secret-token' not in encoded and 'approval_url' not in encoded
    assert 'content' not in out['tasks'][0]


def test_emulator_cannot_be_mislabeled_sandbox(tmp_path):
    s=Store(str(tmp_path/'p.db'));tid=task(s)
    with pytest.raises(ValueError,match='sandbox runs'):export(s,[tid])
    t=s.get(tid);t['provider']='sandbox';s.save(t)
    with pytest.raises(ValueError,match='Test identifiers'):export(s,[tid])


def test_replay_has_no_mutating_routes(tmp_path,monkeypatch):
    s=Store(str(tmp_path/'p.db'));tid=task(s);file=tmp_path/'replay.json'
    file.write_text(json.dumps(export(s,[tid],allow_emulator=True)))
    monkeypatch.setenv('REWIND_REPLAY_FILE',str(file))
    with TestClient(create_replay()) as c:
        assert c.get('/api/config').json()['read_only']
        assert c.get('/api/tasks/'+tid).status_code==200
        assert c.post('/api/tasks/'+tid+'/recover').status_code in {404,405}
        assert c.post('/api/tasks',json={}).status_code==405
        assert c.get('/api/tasks/'+tid+'/artifact').status_code==404
