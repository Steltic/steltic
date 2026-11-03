"""An enforced completion retry requests tools, without forcing ordinary answers."""
import json
from types import SimpleNamespace
import httpx
import pytest
from steltic import agent
from steltic.job_tools import JobWorkspace


GATE='COMPLETION GATE (enforced): the design is NOT done -- unresolved physical data'


def test_rejected_promise_requests_actual_step_then_rechecks_completion(tmp_path,monkeypatch):
    choices=[];executed=[]
    def chat(base_url,api_key,model,messages,max_tok,*args,**kwargs):
        choices.append(agent._build_chat_payload(model,messages,max_tok,None,'',base_url,set())['tool_choice'])
        if False:yield None
        calls=[]
        if choices[-1]=='required':
            calls=[dict(id='fix',type='function',function=dict(name='write_file',arguments=json.dumps(dict(path='note.txt',content='actual saved work'))))]
        return dict(content='I will fix it.' if not executed else 'Checked.',tool_calls=calls,finish_reason='stop',usage=None)
    monkeypatch.setattr(agent,'_chat_with_retry',chat)
    monkeypatch.setattr(agent,'_completion_gate',lambda ws:[] if executed else ['missing work'])
    monkeypatch.setattr(agent,'dispatch',lambda name,args,ws,executor:(executed.append(name),{'ok':True,'path':args['path']})[1])
    ws=JobWorkspace(tmp_path/'jobs')
    events=list(agent.run_design(ws,SimpleNamespace(name='test'),'http://invalid','unused','test','case','Fictional design.'))
    assert choices==['auto','required','auto']
    assert executed==['write_file']
    assert events[-1]['type']=='done'


@pytest.mark.parametrize('content',[GATE,[dict(type='text',text=GATE,cache_control={'type':'ephemeral'})]])
def test_only_immediate_gate_retry_requires_a_tool(content):
    messages=[dict(role='user',content=content)]
    payload=agent._build_chat_payload('test',messages,100,None,'','',set())
    assert payload['tool_choice']=='required'
    assert payload['messages']==messages
    # After the tool, a checked final answer is allowed again.
    messages.append(dict(role='tool',tool_call_id='x',content='{"returncode":0}'))
    assert agent._build_chat_payload('test',messages,100,None,'','',set())['tool_choice']=='auto'
    messages.append(dict(role='user',content='Explain the remaining questions.'))
    assert agent._build_chat_payload('test',messages,100,None,'','',set())['tool_choice']=='auto'


def test_unsupported_required_choice_falls_back_once_and_remembers(monkeypatch):
    seen=[]
    def serve(request):
        payload=json.loads(request.content);seen.append(payload)
        if payload['tool_choice']=='required':
            return httpx.Response(400,json={'error':{'message':'tool_choice required is not supported by this model'}})
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"needs attention"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n')
    client=httpx.Client
    monkeypatch.setattr(agent.httpx,'Client',lambda **kw:client(transport=httpx.MockTransport(serve)))
    monkeypatch.setattr(agent,'_MODEL_QUIRKS',{})
    messages=[dict(role='user',content=GATE)]
    for _ in range(2):
        list(agent._stream_chat('https://model.invalid','unused','test',messages,100))
    assert [p['tool_choice'] for p in seen]==['required','auto','auto']
    assert agent._MODEL_QUIRKS[('https://model.invalid','test')]=={'no_required_tool_choice'}
    assert agent._detect_quirk('tool_choice required requires at least one tool') is None
