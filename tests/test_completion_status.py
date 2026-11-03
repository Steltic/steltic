"""Unresolved gates must never be presented as successfully completed jobs."""
import json
from types import SimpleNamespace
import pytest
from steltic import agent
from steltic.job_tools import JobWorkspace


@pytest.mark.parametrize('problems,expected,calls',[(['stale analysis and missing physical data'],'paused',3),([], 'done',1)])
def test_final_status_respects_completion_checks(tmp_path,monkeypatch,problems,expected,calls):
    seen=[]
    def chat(*args,**kwargs):
        seen.append(1)
        if False:yield None
        return dict(content='The model claims it is finished.',tool_calls=[],finish_reason='stop',usage=None)
    monkeypatch.setattr(agent,'_chat_with_retry',chat)
    monkeypatch.setattr(agent,'_completion_gate',lambda ws:problems)
    ws=JobWorkspace(tmp_path/'jobs')
    events=list(agent.run_design(ws,SimpleNamespace(name='test'),'http://invalid','unused','test','case','A fictional design.'))
    assert len(seen)==calls
    assert events[-1]['type']==expected
    if problems:
        assert not any(e['type']=='done' for e in events)
        assert events[-1]['problems']==problems
        saved=json.loads((ws.jobs/'case/conversation.json').read_text())
        assert 'still incomplete' in saved[-1]['content']
