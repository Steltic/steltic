"""The solver retry gate must distinguish source inspection from runtime errors."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from steltic.agent import _r21_gate, _r21_track


def test_successful_source_inspection_does_not_block_python():
    ws = SimpleNamespace()
    source = 'import openseespy.opensees as ops\nraise RuntimeError("OpenSees analysis failed")'
    for _ in range(3):
        result = _r21_track(ws, dict(returncode=0, stdout=source, stderr=''))
        assert 'r21' not in result
    assert _r21_gate(ws, 'print("hello")') is None


def test_zero_exit_solver_stderr_still_triggers_gate():
    ws = SimpleNamespace()
    for _ in range(2):
        _r21_track(ws, dict(returncode=0, stdout='', stderr='WARNING ArpackSolver::solve() - failed to do eigen'))
    assert _r21_gate(ws, 'ops.eigen(3)')['blocked'].startswith('R21 gate:')


def test_failed_process_stdout_still_triggers_gate():
    ws = SimpleNamespace()
    for _ in range(2):
        _r21_track(ws, dict(returncode=1, stdout='OpenSees analysis failed', stderr=''))
    assert _r21_gate(ws, 'ops.analyze(1)') is not None
    ws._os_docs_since_err = True
    assert _r21_gate(ws, 'ops.analyze(1)') is None


def test_successful_runtime_resets_previous_error_streak():
    ws = SimpleNamespace(_os_err_streak=1, _os_docs_since_err=False)
    _r21_track(ws, dict(returncode=0, stdout='analysis complete', stderr=''))
    assert ws._os_err_streak == 0
    assert ws._os_docs_since_err is True
