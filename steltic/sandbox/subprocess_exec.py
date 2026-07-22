"""Subprocess executor: runs model code in a child process of the BACKEND interpreter with rlimits +
timeout. This is NOT a security boundary (no namespace/network isolation) and requires openseespy +
matplotlib installed in the app environment. Fine for a local single-user install running your own
designs; prefer DockerExecutor for isolation, and never expose the app publicly with this executor.
"""
import os, sys, subprocess
from pathlib import Path
from .base import Executor, ExecResult, prepare_script


class SubprocessExecutor(Executor):
    name = "subprocess"

    def __init__(self, engine_dir: Path):
        self.engine = str(engine_dir)

    def healthcheck(self) -> tuple[bool, str]:
        try:
            r = subprocess.run([sys.executable, "-c", "import openseespy.opensees"],
                               capture_output=True, text=True, timeout=60,
                               env={**os.environ, "PYTHONPATH": self.engine})
            return (r.returncode == 0,
                    "" if r.returncode == 0 else "openseespy not importable in backend venv (subprocess mode)")
        except Exception as e:
            return False, str(e)

    def run(self, code: str, jobs_dir: Path, building: str, timeout: int) -> ExecResult:
        wd = jobs_dir / building
        run_dir = wd / ".run"
        run_dir.mkdir(parents=True, exist_ok=True)
        script = run_dir / "_exec.py"
        script.write_text(prepare_script(code), encoding="utf-8")
        env = {**os.environ,
               "PYTHONPATH": self.engine,
               "MPLBACKEND": "Agg", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "STEEL_BUILDER_JOBS": str(jobs_dir),
               "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPYCACHEPREFIX": str(run_dir / "pyc")}

        def _limits():                       # POSIX-only resource caps
            try:
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 5))
                resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
            except Exception:
                pass

        try:
            p = subprocess.run([sys.executable, str(script)], cwd=str(wd), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout,
                               preexec_fn=_limits if os.name == "posix" else None)
        except subprocess.TimeoutExpired:
            return ExecResult(returncode=124, timed_out=True, error=f"timeout after {timeout}s")
        finally:
            try: script.unlink()
            except Exception: pass
        return ExecResult(stdout=p.stdout or "", stderr=p.stderr or "", returncode=p.returncode)
