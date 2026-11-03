from steel_engine.bim_export import tracked_sources
import pytest


def test_data_dependencies_and_generated_output_exclusion(tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'schedule.json').write_text('{"size": 1}')
    (tmp_path / 'design').mkdir()
    (tmp_path / 'design' / 'bim_package.json').write_text('{}')
    (tmp_path / 'design' / 'helper.py').write_text('x=1')
    before = tracked_sources(tmp_path)
    assert 'data/schedule.json' in before
    assert 'design/helper.py' in before
    assert 'design/bim_package.json' not in before
    (tmp_path / 'data' / 'schedule.json').write_text('{"size": 2}')
    assert tracked_sources(tmp_path) != before


def test_explicit_inputs_stay_inside_job(tmp_path):
    (tmp_path / 'design').mkdir()
    (tmp_path / 'design' / 'layout.json').write_text('{}')
    (tmp_path / 'bim_inputs.json').write_text('["design/layout.json"]')
    assert 'design/layout.json' in tracked_sources(tmp_path)
    (tmp_path / 'bim_inputs.json').write_text('["../outside.json"]')
    with pytest.raises(ValueError, match='inside the job'):
        tracked_sources(tmp_path)


def test_runner_scratch_and_conversation_do_not_make_completed_export_stale(tmp_path):
    (tmp_path/'cfg.py').write_text('cfg={}')
    (tmp_path/'.run').mkdir()
    scratch=tmp_path/'.run/_exec.py';scratch.write_text('print(1)')
    conversation=tmp_path/'conversation.json';conversation.write_text('{}')
    before=tracked_sources(tmp_path)
    scratch.unlink();conversation.write_text('{"finished":true}')
    assert before==tracked_sources(tmp_path)
    assert set(before)=={'cfg.py'}
    (tmp_path/'bim_inputs.json').write_text('["conversation.json"]')
    assert 'conversation.json' in tracked_sources(tmp_path)
