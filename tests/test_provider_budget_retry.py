import json
import httpx
import pytest
from steltic import agent,config


def temporary(headers=None):
    return json.dumps({'error':{'metadata':{'reason':'in_flight_budget_exhausted','headers':headers or {'Retry-After':'120'}}}})


def test_only_explicit_temporary_402_is_retryable():
    assert agent._http_retry_policy(402,{},temporary())==(True,120.,True)
    assert agent._http_retry_policy(402,{'Retry-After':'180'},temporary())==(True,180.,True)
    assert agent._http_retry_policy(402,{},'{"error":{"message":"Insufficient credits"}}')[0] is False
    assert agent._http_retry_policy(401,{},temporary())[0] is False
    assert agent._http_retry_policy(402,{},'{broken')[0] is False
    assert agent._http_retry_policy(402,{},temporary({'Retry-After':'NaN'}))==(True,120.,True)
    assert agent._http_retry_policy(429,{'Retry-After':'35'},'{}')==(True,35.,False)


def test_real_http_path_honours_cooldown_before_success(monkeypatch):
    calls=[];waits=[]
    def serve(request):
        calls.append(request)
        if len(calls)==1:return httpx.Response(402,text=temporary())
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n')
    real=httpx.Client
    monkeypatch.setattr(agent.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(serve)))
    monkeypatch.setattr(agent,'_retry_wait',lambda seconds,cancel:waits.append(seconds))
    monkeypatch.setattr(config,'RETRIES',3)
    events=list(agent._chat_with_retry('https://model.invalid','test','test',[],100))
    assert len(calls)==2 and waits==[120.]
    assert events[-1]['text']=='ok'
    assert 'in-flight credit reservation' in events[0]['text']


def test_real_account_credit_failure_is_not_blindly_retried(monkeypatch):
    calls=[]
    def serve(request):
        calls.append(request);return httpx.Response(402,json={'error':{'message':'Insufficient credits'}})
    real=httpx.Client
    monkeypatch.setattr(agent.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(serve)))
    monkeypatch.setattr(config,'RETRIES',3)
    with pytest.raises(RuntimeError,match='Insufficient credits'):
        list(agent._chat_with_retry('https://model.invalid','test','test',[],100))
    assert len(calls)==1


def test_stop_interrupts_long_retry_wait(monkeypatch):
    sleeps=[];checks=iter([False,False,True])
    monkeypatch.setattr(agent.time,'sleep',lambda seconds:sleeps.append(seconds))
    with pytest.raises(agent.UserStop):agent._retry_wait(120,lambda:next(checks))
    assert sleeps==[.25,.25]


def test_transient_retries_remain_bounded(monkeypatch):
    calls=[]
    def fail(*args,**kw):
        calls.append(1)
        if False:yield None
        raise agent.LLMHTTPError('temporary budget',retry_after=120)
    monkeypatch.setattr(agent,'_stream_chat',fail)
    monkeypatch.setattr(agent,'_retry_wait',lambda *args:None)
    monkeypatch.setattr(config,'RETRIES',2)
    with pytest.raises(RuntimeError,match='temporary budget'):
        list(agent._chat_with_retry('url','key','model',[],100))
    assert len(calls)==2
