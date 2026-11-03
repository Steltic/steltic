"""Legitimate calculation edits allow the same checker to be called again."""
import json
from types import SimpleNamespace
import pytest
from steltic import agent
from steltic.job_tools import JobWorkspace


def call(name,arguments,index):
    return dict(id='c'+str(index),type='function',function=dict(name=name,arguments=json.dumps(arguments)))


@pytest.mark.parametrize('path',['cfg.py','design/calc_package.json','design/calc_package_cfs.json'])
def test_recheck_after_each_saved_input_edit_is_not_a_repeat_loop(tmp_path,monkeypatch,path):
    turns=[];checks=[]
    def chat(*args,**kwargs):
        if False:yield None
        n=len(turns);turns.append(n)
        calls=[] if n==5 else [call('write_file',dict(path=path,content=str(n)),2*n),
                              call('run_python',dict(code='consistency.check("case")'),2*n+1)]
        return dict(content='Checked.',tool_calls=calls,finish_reason='stop',usage=None)
    def dispatch(name,args,ws,executor):
        if name=='write_file':
            target=ws._job_dir()/args['path'];target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(args['content'],encoding='utf-8')
            return {'ok':True}
        checks.append(args['code']);return {'returncode':0,'stdout':'check complete'}
    monkeypatch.setattr(agent,'_chat_with_retry',chat)
    monkeypatch.setattr(agent,'dispatch',dispatch)
    monkeypatch.setattr(agent,'_completion_gate',lambda ws:[])
    monkeypatch.setattr(agent.config,'REPEAT_LIMIT',3)
    events=list(agent.run_design(JobWorkspace(tmp_path/'jobs'),SimpleNamespace(name='test'),'http://invalid','unused','test','case','Fictional fixture.'))
    assert len(checks)==5 and len(set(checks))==1
    assert events[-1]['type']=='done'


@pytest.mark.parametrize('rewrite_identical_input',[False,True])
def test_repeated_success_with_only_output_updates_still_pauses(tmp_path,monkeypatch,rewrite_identical_input):
    calls=[]
    def chat(*args,**kwargs):
        if False:yield None
        return dict(content=None,tool_calls=[call('run_python',dict(code='consistency.check("case")'),len(calls))],finish_reason='tool_calls',usage=None)
    def dispatch(name,args,ws,executor):
        calls.append(name);root=ws._job_dir();(root/'design').mkdir(exist_ok=True)
        (root/'design/consistency_result.json').write_text(json.dumps({'time':len(calls)}))
        (root/'report.html').write_text(str(len(calls)))
        if rewrite_identical_input:(root/'design/calc_package.json').write_text('{"members":[]}')
        return {'returncode':0,'stdout':'same check'}
    monkeypatch.setattr(agent,'_chat_with_retry',chat)
    monkeypatch.setattr(agent,'dispatch',dispatch)
    monkeypatch.setattr(agent,'_completion_gate',lambda ws:[])
    monkeypatch.setattr(agent.config,'REPEAT_LIMIT',3)
    events=list(agent.run_design(JobWorkspace(tmp_path/'jobs'),SimpleNamespace(name='test'),'http://invalid','unused','test','case','Fictional fixture.'))
    assert events[-1]['type']=='paused'
    assert 'repeated 3x' in events[-1]['reason']
    assert len(calls)==(3 if rewrite_identical_input else 2)
