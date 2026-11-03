import json
import httpx
import pytest
from steltic import agent,config


def test_deprecated_temperature_retries_once_and_remembers_model(monkeypatch):
    payloads=[]
    def serve(request):
        body=json.loads(request.content);payloads.append(body)
        if 'temperature' in body:
            return httpx.Response(400,json={'error':{'code':'invalid_request_error','message':'`temperature` is deprecated for this model.','param':None}})
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n')
    real_client=httpx.Client
    monkeypatch.setattr(agent.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(serve)))
    monkeypatch.setattr(agent,'_MODEL_QUIRKS',{})
    monkeypatch.setattr(config,'TEMPERATURE',0.3)
    assert list(agent._stream_chat('https://model.invalid','test','claude-test',[],100))[0]['text']=='ok'
    assert 'temperature' in payloads[0] and 'temperature' not in payloads[1]
    assert list(agent._stream_chat('https://model.invalid','test','claude-test',[],100))[0]['text']=='ok'
    assert len(payloads)==3 and 'temperature' not in payloads[2]
    assert agent._MODEL_QUIRKS[('https://model.invalid','claude-test')]=={'no_temperature'}
    body=agent._build_chat_payload('claude-test',[],100,None,'','https://model.invalid',agent._MODEL_QUIRKS[('https://model.invalid','claude-test')])
    assert 'temperature' not in body


def test_explicit_omit_and_unrelated_error(monkeypatch):
    monkeypatch.setattr(config,'TEMPERATURE',None)
    assert 'temperature' not in agent._build_chat_payload('model',[],100,None,'','',set())
    assert agent._detect_quirk('temperature must be between 0 and 1') is None


def test_non400_is_not_retried_as_parameter_error(monkeypatch):
    calls=[]
    def serve(request):
        calls.append(request)
        return httpx.Response(401,text='temperature deprecated; authentication required')
    client=httpx.Client(transport=httpx.MockTransport(serve))
    monkeypatch.setattr(agent.httpx,'Client',lambda **kw:client)
    monkeypatch.setattr(agent,'_MODEL_QUIRKS',{})
    with pytest.raises(agent.LLMHTTPError):
        list(agent._stream_chat('https://model.invalid','test','claude-test',[],100))
    assert len(calls)==1
