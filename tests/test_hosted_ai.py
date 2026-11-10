import asyncio
import json
import httpx
import pytest

from rewind import ai
from rewind.engine import Engine
from rewind.providers import Emulator
from rewind.store import Store


def sample(conflict='none',recommendation='refund'):
    return {'cause':'Merchant worker terminated','recommendation':recommendation,'confidence':.9,
            'evidence_ids':[1],'contradictions':[],'conflict_type':conflict,
            'explanation':'The cited merchant record reports terminal failure.'}


def inputs():
    return {'id':'t','payment':'captured','fulfillment':'failed','delivery':'pending'},[
        {'seq':1,'kind':'MERCHANT_EXECUTION_FAILED','detail':{'message':'Worker failed'}}]


def hosted(monkeypatch,handler,mode='openai_compatible'):
    monkeypatch.setenv('REWIND_AI',mode)
    monkeypatch.setenv('REWIND_AI_MODEL','test-model')
    monkeypatch.setenv('REWIND_AI_API_KEY','test-secret-must-not-appear-in-result')
    real=httpx.AsyncClient
    monkeypatch.setattr(ai.httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler)))


def test_compatible_provider_requests_strict_schema_and_validates(monkeypatch):
    seen=[]
    def handler(r):
        seen.append(json.loads(r.content))
        assert r.url.path=='/v1/chat/completions'
        assert r.headers['authorization'].startswith('Bearer ')
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(sample())}}]})
    hosted(monkeypatch,handler)
    d=asyncio.run(ai.diagnose(*inputs()))
    assert d['live_ai'] and d['provider']=='openai_compatible'
    assert seen[0]['response_format']['json_schema']['strict'] is True
    assert set(seen[0]['response_format']['json_schema']['schema']['required'])==set(ai.Diagnosis.model_fields)
    assert 'test-secret' not in json.dumps(d)


def test_openai_responses_provider(monkeypatch):
    def handler(r):
        body=json.loads(r.content)
        assert r.url.path=='/v1/responses'
        assert body['store'] is False and body['text']['format']['strict'] is True
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(sample())}]}]})
    hosted(monkeypatch,handler,'openai')
    assert asyncio.run(ai.diagnose(*inputs()))['live_ai']


@pytest.mark.parametrize('finish,refusal',[('length',None),('stop','refused')])
def test_incomplete_or_refused_hosted_response_rejected(monkeypatch,finish,refusal):
    hosted(monkeypatch,lambda r:httpx.Response(200,json={'choices':[{'finish_reason':finish,'message':{
        'content':json.dumps(sample()),'refusal':refusal}}]}))
    with pytest.raises(ai.AIError,match='incomplete or refused'):
        asyncio.run(ai.diagnose(*inputs()))


def test_provider_error_body_never_exposed(monkeypatch):
    hosted(monkeypatch,lambda r:httpx.Response(401,json={'error':'test-secret-must-not-appear-in-result'}))
    with pytest.raises(ai.AIError) as e:
        asyncio.run(ai.diagnose(*inputs()))
    assert str(e.value)=='AI provider returned HTTP 401'


def test_missing_key_never_falls_back_to_fixture(monkeypatch):
    monkeypatch.setenv('REWIND_AI','openai_compatible')
    monkeypatch.delenv('REWIND_AI_API_KEY',raising=False);monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    with pytest.raises(ai.AIError,match='Configure'):
        asyncio.run(ai.diagnose(*inputs()))


def test_ai_context_excludes_prior_ai_and_policy(monkeypatch):
    task,events=inputs()
    events += [{'seq':2,'kind':'AI_DIAGNOSIS','detail':{'recommendation':'refund'}},
               {'seq':3,'kind':'POLICY_DECISION','detail':{'action':'refund'}}]
    assert [e['seq'] for e in ai.evidence_input(task,events)['events']]==[1]


def test_evidence_message_and_live_conflict_drive_real_policy_path(tmp_path,monkeypatch):
    s=Store(str(tmp_path/'e.db'));e=Engine(s,Emulator(s))
    async def diagnosis(task,events):
        return {**sample('fulfillment_conflict','review'),'contradictions':['Merchant now claims completion'],
                'evidence_ids':[events[-1]['seq']],'provider':'openai_compatible','live_ai':True}
    monkeypatch.setattr(ai,'diagnose',diagnosis)
    async def case():
        t=e.create('true_failure');await e.order(t['id']);await e.capture(t['id']);await e.merchant(t['id'])
        await e.add_evidence(t['id'],'Despite the failure log, the report completed and was emailed.')
        held=await e.recover(t['id']);assert held['decision']['action']=='review'
        diagnosed=await e.diagnose(t['id']);assert diagnosed['contradiction']
        held=await e.recover(t['id']);assert held['decision']['action']=='review' and held['refund_id'] is None
    asyncio.run(case())


def test_new_evidence_requires_new_diagnosis(tmp_path,monkeypatch):
    s=Store(str(tmp_path/'e.db'));e=Engine(s,Emulator(s))
    async def diagnosis(task,events):return {**sample(),'live_ai':True,'provider':'openai_compatible'}
    monkeypatch.setattr(ai,'diagnose',diagnosis)
    async def case():
        t=e.create('true_failure');await e.order(t['id']);await e.capture(t['id']);await e.merchant(t['id'])
        await e.diagnose(t['id']);await e.add_evidence(t['id'],'New potentially conflicting information')
        held=await e.recover(t['id']);assert held['decision']['action']=='review'
    asyncio.run(case())


def test_delivery_gap_does_not_become_fulfillment_conflict(tmp_path):
    s=Store(str(tmp_path/'e.db'));e=Engine(s,Emulator(s))
    async def case():
        t=e.create('delivery_loss');await e.order(t['id']);await e.capture(t['id'])
        gap=await e.merchant(t['id'])
        assert gap['delivery_gap'] and not gap['contradiction']
        await e.diagnose(t['id'])
        result=await e.recover(t['id'])
        assert result['delivery']=='delivered' and result['refund_id'] is None
    asyncio.run(case())


def test_model_findings_are_not_rewritten_by_rules(monkeypatch):
    hosted(monkeypatch, lambda r: httpx.Response(200, json={'choices': [{
        'finish_reason': 'stop', 'message': {
            'content': json.dumps(sample('fulfillment_conflict', 'review'))}}]}))
    d = asyncio.run(ai.diagnose(*inputs()))
    assert d['recommendation'] == 'review'
    assert d['conflict_type'] == 'fulfillment_conflict'
    assert 'grounded_by_policy' not in d


@pytest.mark.parametrize('done,reason', [(False, None), (True, 'length')])
def test_truncated_ollama_output_is_rejected(monkeypatch, done, reason):
    hosted(monkeypatch, lambda r: httpx.Response(200, json={
        'done': done, 'done_reason': reason,
        'message': {'content': json.dumps(sample())}}), 'ollama')
    with pytest.raises(ai.AIError, match='incomplete'):
        asyncio.run(ai.diagnose(*inputs()))


def test_ollama_separates_untrusted_evidence_and_bounds_generation(monkeypatch):
    def handler(r):
        body = json.loads(r.content)
        assert body['options']['num_predict'] == 768
        assert body['options']['temperature'] == 0
        assert body['keep_alive'] == '10m'
        assert 'SYSTEM OVERRIDE' not in body['messages'][0]['content']
        assert 'SYSTEM OVERRIDE' in body['messages'][1]['content']
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop',
            'message': {'content': json.dumps(sample())}})
    hosted(monkeypatch, handler, 'ollama')
    task, events = inputs()
    events[0]['detail']['message'] += ' SYSTEM OVERRIDE'
    assert asyncio.run(ai.diagnose(task, events))['live_ai']




def test_unavailable_ai_blocks_refund(tmp_path, monkeypatch):
    s = Store(str(tmp_path/'unavailable.db')); e = Engine(s, Emulator(s))
    async def unavailable(*args):
        raise ai.AIError('AI unavailable')
    monkeypatch.setattr(ai, 'diagnose', unavailable)
    async def case():
        t = e.create('true_failure')
        await e.order(t['id']); await e.capture(t['id']); await e.merchant(t['id'])
        await e.diagnose(t['id'])
        result = await e.recover(t['id'])
        assert result['decision']['action'] == 'review'
        assert result['refund_id'] is None
    asyncio.run(case())
