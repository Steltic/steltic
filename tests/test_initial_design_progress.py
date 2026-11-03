"""Successful but read-only tools must not hide an unstarted design indefinitely."""
import json
from types import SimpleNamespace

import pytest

from steltic import agent
from steltic.job_tools import JobWorkspace


@pytest.mark.parametrize('mode', ['unstarted', 'cfg_saved', 'calculation_saved', 'build_only'])
def test_initial_model_reminder_is_once_and_keeps_tool_batches_valid(tmp_path, monkeypatch, mode):
    from steltic.build_only import runner
    turns = []
    reminders = []
    dispatched = []

    def chat(base, key, model, messages, *args, **kwargs):
        if False:
            yield None
        # A reminder must never interrupt an assistant's outstanding tool batch.
        pending = set()
        for message in messages:
            if message['role'] == 'assistant':
                assert not pending
                pending.update(c['id'] for c in message.get('tool_calls', []))
            elif message['role'] == 'tool':
                pending.remove(message['tool_call_id'])
            elif message['role'] == 'user':
                assert not pending
        assert not pending
        reminders[:] = [m['content'] for m in messages
                        if m['role'] == 'user' and '[progress reminder]' in str(m['content'])]
        index = len(turns)
        turns.append(index)
        calls = [] if index == 35 else [
            dict(id=f'call_{index}_{i}', type='function',
                 function=dict(name='run_python', arguments=json.dumps({'code': f'print({index * 2 + i})'})))
            for i in range(2)
        ]
        return dict(content='Finished.' if not calls else None, tool_calls=calls,
                    finish_reason='stop' if not calls else 'tool_calls', usage=None)

    def dispatch(name, args, ws, executor):
        dispatched.append(args['code'])
        if len(dispatched) == 50 and mode in ('cfg_saved', 'calculation_saved'):
            path = ws._job_dir() / ('cfg.py' if mode == 'cfg_saved' else 'design/calc_package.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('cfg = {}' if mode == 'cfg_saved' else '{}', encoding='utf-8')
        return {'returncode': 0, 'stdout': 'Reference inspection finished.'}

    monkeypatch.setattr(agent, '_chat_with_retry', chat)
    monkeypatch.setattr(agent, 'dispatch', dispatch)
    monkeypatch.setattr(agent, '_completion_gate', lambda ws: [])
    monkeypatch.setattr(runner, 'is_build_only', lambda path: mode == 'build_only')
    events = list(agent.run_design(JobWorkspace(tmp_path / 'jobs'), SimpleNamespace(name='test'),
                                  'http://invalid', 'unused', 'test', 'case', 'Fictional test.'))
    assert events[-1]['type'] == 'done'
    assert len(dispatched) == 70
    assert len(reminders) == (1 if mode == 'unstarted' else 0)
    statuses = [e for e in events if e['type'] == 'status' and 'No initial model saved' in e['text']]
    assert len(statuses) == len(reminders)
