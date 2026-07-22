"""Pick the executor from config.EXECUTOR (auto|docker|subprocess)."""
from .. import config
from .docker_exec import DockerExecutor
from .subprocess_exec import SubprocessExecutor


def _docker():
    return DockerExecutor(config.SANDBOX_IMAGE, config.SANDBOX_MEM, config.SANDBOX_CPUS, config.SANDBOX_PIDS)

def _sub():
    return SubprocessExecutor(config.STEEL_ENGINE)


def make_executor(log=print):
    mode = (config.EXECUTOR or "auto").lower()
    if mode == "docker":
        return _docker()
    if mode == "subprocess":
        return _sub()
    # auto: prefer Docker if it's actually usable, else fall back to the subprocess executor
    # (no container isolation -- fine for a local single-user install running your own designs).
    d = _docker()
    ok, why = d.healthcheck()
    if ok:
        log(f"[sandbox] using DockerExecutor (image={config.SANDBOX_IMAGE})")
        return d
    s = _sub()
    log(f"[sandbox] Docker unavailable ({why}); using SubprocessExecutor -- no container "
        f"isolation. Needs openseespy/numpy/matplotlib in this environment (installed with Steltic).")
    return s
