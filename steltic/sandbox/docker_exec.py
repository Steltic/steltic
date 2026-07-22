"""Self-host executor: one throwaway container per run_python call.

Hardening: --network none (no egress), --read-only root fs (only the mounted jobs/ dir + tmpfs are
writable), --cap-drop ALL, --security-opt no-new-privileges, memory/cpu/pids caps, non-root uid, and
NO credentials in the environment. A runaway is killed by name on timeout so no container is orphaned.
"""
import os, shutil, subprocess, uuid
from pathlib import Path
from .base import Executor, ExecResult, prepare_script


class DockerExecutor(Executor):
    name = "docker"

    def __init__(self, image: str, mem: str = "2g", cpus: str = "2", pids: str = "256"):
        self.image, self.mem, self.cpus, self.pids = image, mem, cpus, pids

    def healthcheck(self) -> tuple[bool, str]:
        if not shutil.which("docker"):
            return False, "docker not on PATH"
        r = subprocess.run(["docker", "image", "inspect", self.image], capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"sandbox image '{self.image}' not built -- run sandbox_image/build.sh"
        d = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if d.returncode != 0:
            return False, "docker daemon not reachable"
        return True, ""

    def run(self, code: str, jobs_dir: Path, building: str, timeout: int) -> ExecResult:
        wd = jobs_dir / building
        (wd / ".run").mkdir(parents=True, exist_ok=True)
        script = wd / ".run" / "_exec.py"
        script.write_text(prepare_script(code), encoding="utf-8")
        uid = os.getuid() if hasattr(os, "getuid") else 0
        gid = os.getgid() if hasattr(os, "getgid") else 0
        name = f"steelrun_{uuid.uuid4().hex[:12]}"
        cmd = ["docker", "run", "--rm", "--name", name,
               "--network", "none",
               "--read-only", "--tmpfs", "/tmp:rw,size=512m", "--tmpfs", "/run:rw,size=8m",
               "--memory", self.mem, "--memory-swap", self.mem,
               "--cpus", self.cpus, "--pids-limit", self.pids,
               "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
               "-u", f"{uid}:{gid}",
               "-v", f"{jobs_dir}:/jobs:rw",
               "-e", "STEEL_BUILDER_JOBS=/jobs", "-e", "MPLBACKEND=Agg", "-e", "PYTHONUTF8=1",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
               "-w", f"/jobs/{building}",
               self.image, "python", "/sandbox/runner.py", f"/jobs/{building}/.run/_exec.py"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)   # don't orphan the container
            return ExecResult(returncode=124, timed_out=True, error=f"timeout after {timeout}s")
        finally:
            try: script.unlink()
            except Exception: pass
        # distinguish a docker-level failure (image/daemon) from the user code's own non-zero exit
        if p.returncode != 0 and (p.stderr or "").lower().startswith(("docker:", "unable to find", "error response")):
            return ExecResult(returncode=p.returncode, error="sandbox launch failed: " + (p.stderr or "")[:300])
        return ExecResult(stdout=p.stdout or "", stderr=p.stderr or "", returncode=p.returncode)
