from types import SimpleNamespace

from steltic.agent import _artifact_state, _milestones


def test_source_inspection_does_not_claim_design_work(tmp_path):
    workspace=SimpleNamespace(_job_dir=lambda: tmp_path)
    before=_artifact_state(workspace)
    result=dict(returncode=0,stdout='def design_and_report(): ... consistency.check ... build_report ...')
    args=dict(code='import inspect, consistency; print(inspect.getsource(consistency.check))')
    assert not _milestones('run_python',args,result,before,_artifact_state(workspace))


def test_actual_saved_files_are_reported_without_claiming_pass(tmp_path):
    workspace=SimpleNamespace(_job_dir=lambda: tmp_path)
    before=_artifact_state(workspace)
    (tmp_path/'design').mkdir()
    (tmp_path/'design/consistency_result.json').write_text('{"result":"FAIL"}')
    (tmp_path/'design/calc_package_cfs.json').write_text('{}')
    (tmp_path/'report.html').write_text('<p>Current report</p>')
    after=_artifact_state(workspace)
    assert _milestones('run_python',{},dict(returncode=0),before,after)==[
        'Calculation package updated','Consistency result updated','Report updated']
    assert not _milestones('run_python',{},dict(returncode=0),after,after)


def test_failed_write_or_python_call_has_no_success_milestone(tmp_path):
    workspace=SimpleNamespace(_job_dir=lambda: tmp_path)
    (tmp_path/'cfg.py').write_text('cfg = {}')
    after=_artifact_state(workspace)
    for name,result in [('write_file',dict(ok=False,error='denied')),
                        ('run_python',dict(returncode=1)),
                        ('run_python',dict(returncode=0,timed_out=True))]:
        assert not _milestones(name,dict(path='cfg.py'),result,{},after)
    assert _milestones('write_file',dict(path='cfg.py'),dict(ok=True),{},after)==['cfg.py saved']


def test_no_job_does_not_generate_milestones():
    assert _artifact_state(object())=={}
    assert not _milestones('run_python',dict(code='pipeline.design_and_report()'),dict(returncode=0))
